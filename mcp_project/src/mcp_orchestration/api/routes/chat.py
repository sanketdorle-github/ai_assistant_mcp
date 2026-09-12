import asyncio
import json
import logging
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse
from langchain_core.messages import HumanMessage

from mcp_orchestration.api.routes.auth import get_current_user
from mcp_orchestration.agents.client import MCPClientManager
from mcp_orchestration.agents.agent import create_agent
from mcp_orchestration.core.guardrails import check_text
from mcp_orchestration.mcp.config import load_mcp_server_configs

logger = logging.getLogger("mcp_orchestration.api.routes.chat")

router = APIRouter(prefix="/chat", tags=["Chat"])

# In-memory "please stop" signal per thread
_stop_flags: dict[str, asyncio.Event] = {}

# Sentinel put on the queue to signal the producer task is completely done
# (success or failure) so the SSE generator knows to stop reading.
_DONE = object()


def _get_stop_flag(thread_id: str) -> asyncio.Event:
    flag = _stop_flags.get(thread_id)
    if flag is None:
        flag = asyncio.Event()
        _stop_flags[thread_id] = flag
    return flag


class ChatRequest(BaseModel):
    message: str
    thread_id: str


class ResumeRequest(BaseModel):
    thread_id: str
    approve: bool
    modified_arguments: Optional[dict] = None


class StopRequest(BaseModel):
    thread_id: str


async def _run_agent_worker(
    queue: "asyncio.Queue[object]",
    message: Optional[str],
    thread_id: str,
    user_id: str,
    approve: Optional[bool] = None,
    modified_arguments: Optional[dict] = None,
):
    """Owns the ENTIRE MCP connection lifecycle: connect, use, close — all in
    this one task, from start to finish. This is the fix for the anyio
    cancel-scope error: `streamablehttp_client`/`stdio_client` open internal
    task groups that are bound to whatever task is running when they're
    entered. As long as the *same* task also performs the exit (via
    `mcp_manager.close_all()` in the `finally` below), anyio's "same task
    checks a scope in and out" rule is always satisfied.

    Previously this logic ran directly inside the async generator that
    `EventSourceResponse` iterates. `sse_starlette` runs/cancels that
    generator using its own internal task group, so the task identity at
    generator-teardown time could differ from the task identity at
    connect-time — that mismatch is what raised:
        "Attempted to exit a cancel scope that isn't the current task's
         current cancel scope"

    Running everything here, in a task we create ourselves with
    `asyncio.create_task(...)`, and only ever awaiting/cancelling that task
    from outside, means connect and close always happen on the same task.
    Results are pushed onto `queue`; the SSE generator only ever reads from
    the queue, so it never touches the MCP task's cancel scopes directly.
    """
    mcp_manager = MCPClientManager()
    try:
        # Connect to MCP servers
        for server in load_mcp_server_configs(user_id):
            try:
                connected = await mcp_manager.connect_server(server)
                if not connected:
                    logger.warning(
                        f"Continuing without MCP server '{server.name}' (failed to connect)."
                    )
                else:
                    logger.info(f"✅ MCP server '{server.name}' connected successfully")
            except Exception as e:
                logger.error(
                    f"Error connecting to MCP server '{server.name}': {e}",
                    exc_info=True,
                )
                logger.warning(f"Continuing without MCP server '{server.name}'")

        agent = create_agent(mcp_manager)
        config = {"configurable": {"thread_id": thread_id}}

        if message is not None:
            inputs = {
                "messages": [HumanMessage(content=message)],
                "user_id": user_id,
                "session_id": thread_id,
                "plan": [],
                "current_step_index": 0,
            }
        else:
            inputs = None
            if approve is not None:
                state = await agent.aget_state(config)
                if state and state.values:
                    plan = state.values.get("plan") or []
                    idx = state.values.get("current_step_index", 0)
                    paused_node = state.next[0] if state.next else "tool_executor_gated"

                    if idx < len(plan):
                        if approve:
                            if modified_arguments:
                                plan[idx]["tool_arguments"] = modified_arguments
                                logger.info(
                                    f"Modifying arguments for step {idx+1}: {modified_arguments}"
                                )
                        else:
                            plan[idx]["status"] = "failed"
                            plan[idx]["result"] = "Execution rejected by user."
                            logger.info(f"User rejected execution of step {idx+1}")

                        await agent.aupdate_state(
                            config, {"plan": plan}, as_node=paused_node
                        )

        stop_flag = _get_stop_flag(thread_id)
        stop_flag.clear()
        stopped = False

        async for event in agent.astream_events(inputs, config=config, version="v2"):
            if stop_flag.is_set():
                stopped = True
                break

            event_type = event["event"]
            name = event.get("name", "")

            if event_type == "on_chain_end" and name == "planner":
                output = event["data"].get("output")
                if output and "plan" in output:
                    await queue.put(
                        {"event": "plan", "data": json.dumps({"plan": output["plan"]})}
                    )

            elif event_type == "on_chain_end" and name in (
                "tool_executor",
                "tool_executor_gated",
            ):
                output = event["data"].get("output")
                if output and "plan" in output:
                    await queue.put(
                        {"event": "plan", "data": json.dumps({"plan": output["plan"]})}
                    )

            elif event_type == "on_chat_model_stream":
                tags = event.get("tags") or []
                if "responder_llm" in tags or name == "ChatOpenAI":
                    chunk = event["data"].get("chunk")
                    if chunk and chunk.content:
                        await queue.put(
                            {
                                "event": "token",
                                "data": json.dumps({"token": chunk.content}),
                            }
                        )

        if stopped:
            logger.info(f"Stream for thread '{thread_id}' halted by stop request.")
            await queue.put(
                {
                    "event": "stopped",
                    "data": json.dumps({"message": "Execution stopped by user."}),
                }
            )
        else:
            final_state = await agent.aget_state(config)
            interrupted = False
            if final_state and final_state.next:
                if any(
                    n in final_state.next
                    for n in ("tool_executor_gated", "tool_executor")
                ):
                    plan = final_state.values.get("plan") or []
                    idx = final_state.values.get("current_step_index", 0)
                    if idx < len(plan):
                        pending_step = plan[idx]
                        interrupted = True
                        await queue.put(
                            {
                                "event": "interrupt",
                                "data": json.dumps(
                                    {
                                        "message": f"Step requires approval: {pending_step['description']}",
                                        "step_id": pending_step["id"],
                                        "tool_name": pending_step["tool_name"],
                                        "tool_arguments": pending_step[
                                            "tool_arguments"
                                        ],
                                    }
                                ),
                            }
                        )

            # Output guardrail: log-and-flag only, not a hard block - by
            # this point the responder's tokens have already been streamed
            # to the client via on_chat_model_stream events (see the loop
            # above), so there's nothing left to prevent. True "block
            # before the user sees it" would require buffering the whole
            # response instead of streaming it token-by-token, which this
            # app deliberately doesn't do. This exists for
            # monitoring/alerting, not prevention.
            if not interrupted:
                final_messages = final_state.values.get("messages") if final_state else None
                if final_messages:
                    check_text(
                        getattr(final_messages[-1], "content", "") or "",
                        context="output",
                    )

            # A paused/interrupted run isn't "finished" - sending both
            # events back-to-back let `done` immediately clear
            # pendingApproval on the frontend right after `interrupt` set
            # it, so the approval card never had a chance to render even
            # though the interrupt event itself was delivered correctly.
            if not interrupted:
                await queue.put(
                    {
                        "event": "done",
                        "data": json.dumps({"message": "Execution finished"}),
                    }
                )

    except Exception as e:
        logger.error(f"Error in stream run: {str(e)}", exc_info=True)
        await queue.put({"event": "error", "data": json.dumps({"error": str(e)})})
    finally:
        _stop_flags.pop(thread_id, None)
        # Same task that connected is the one closing — no cross-task
        # cancel-scope mismatch.
        await mcp_manager.close_all()
        await queue.put(_DONE)


async def run_agent_stream(
    message: Optional[str],
    thread_id: str,
    user_id: str,
    approve: Optional[bool] = None,
    modified_arguments: Optional[dict] = None,
):
    """Async generator that `EventSourceResponse` iterates.

    This no longer does any MCP work itself. It just launches
    `_run_agent_worker` as its own standalone task (so its lifetime and
    cancel scopes are independent of whatever task/task-group
    `sse_starlette` uses to drive this generator) and relays whatever that
    worker puts on the queue. If the SSE connection is dropped and this
    generator is torn down early, we cancel the worker task explicitly
    ourselves — cleanly, from the *outside* of the worker task, which is a
    perfectly normal thing to do to a task you created (unlike reaching
    into a task's internal anyio task group from an unrelated task).
    """
    queue: "asyncio.Queue[object]" = asyncio.Queue()
    worker = asyncio.create_task(
        _run_agent_worker(
            queue, message, thread_id, user_id, approve, modified_arguments
        )
    )

    try:
        while True:
            item = await queue.get()
            if item is _DONE:
                break
            yield item
    finally:
        if not worker.done():
            worker.cancel()
            try:
                await worker
            except (asyncio.CancelledError, Exception):
                pass


@router.post("/stream")
async def chat_stream(
    request: ChatRequest, current_user: dict = Depends(get_current_user)
):
    """Post message and stream real-time agent responses & state changes."""
    guardrail = check_text(request.message, context="input")
    if guardrail.blocked:
        raise HTTPException(status_code=400, detail=guardrail.reason)

    user_id = current_user["id"]
    generator = run_agent_stream(
        message=request.message, thread_id=request.thread_id, user_id=user_id
    )
    return EventSourceResponse(generator)


@router.post("/resume")
async def chat_resume(
    request: ResumeRequest, current_user: dict = Depends(get_current_user)
):
    """Resume execution of an interrupted agent session."""
    user_id = current_user["id"]
    generator = run_agent_stream(
        message=None,
        thread_id=request.thread_id,
        user_id=user_id,
        approve=request.approve,
        modified_arguments=request.modified_arguments,
    )
    return EventSourceResponse(generator)


@router.post("/stop")
async def chat_stop(
    request: StopRequest, current_user: dict = Depends(get_current_user)
):
    """Signal an in-flight run to halt."""
    stop_flag = _get_stop_flag(request.thread_id)
    stop_flag.set()
    return {"stopped": True}


@router.get("/threads")
async def list_threads(current_user: dict = Depends(get_current_user)):
    """List this user's chat threads.

    Also opportunistically triggers episodic-memory summarization for any
    idle thread that doesn't have one yet (see
    agents/memory.py:maybe_summarize_idle_thread) - fire-and-forget, this
    endpoint's own response never waits on it. This is the simplest v1
    trigger point for episodic memory: the app has no dedicated "thread
    ended" signal, so piggybacking on "the sidebar just asked for the
    thread list" stands in for one.
    """
    from mcp_orchestration.agents.memory import maybe_summarize_idle_thread
    from mcp_orchestration.services.memory_service import list_threads_for_user

    threads = list_threads_for_user(current_user["id"])
    for t in threads:
        asyncio.create_task(
            maybe_summarize_idle_thread(
                current_user["id"], t["thread_id"], t.get("last_activity")
            )
        )
    return threads


@router.get("/threads/{thread_id}/messages")
def get_thread_messages(thread_id: str, current_user: dict = Depends(get_current_user)):
    """Fetch the reconstructed message transcript for one thread."""
    from mcp_orchestration.services.memory_service import (
        get_thread_messages as _get_messages,
    )

    return _get_messages(thread_id)
