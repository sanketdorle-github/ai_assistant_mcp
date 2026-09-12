import asyncio
import logging
import re
from typing import Annotated, Dict, List, Any, Optional, TypedDict
from pydantic import BaseModel, Field
from langchain_core.messages import (
    BaseMessage,
    HumanMessage,
    AIMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from mcp_orchestration.agents.llm import get_llm, log_token_usage, estimate_tokens
from mcp_orchestration.agents.client import MCPClientManager
from mcp_orchestration.agents import memory as long_term_memory
from mcp_orchestration.core.guardrails import check_text
from mcp_orchestration.services.memory_service import MongoCheckpointSaver

logger = logging.getLogger("mcp_orchestration.agents.agent")


# --- Structured Output Planning Schemas ---


class TaskStep(BaseModel):
    id: int = Field(..., description="Unique sequential ID of the step starting at 1")
    description: str = Field(
        ..., description="Action description of what this step will accomplish"
    )
    tool_name: Optional[str] = Field(
        None, description="Name of the MCP tool to call if any"
    )
    tool_arguments: Optional[dict] = Field(
        None, description="Arguments to pass to the MCP tool"
    )
    requires_approval: bool = Field(
        False,
        description="Set to True if this step executes a mutation or sensitive action needing user sign-off",
    )
    status: str = Field(
        "pending", description="Status of the step (pending, active, completed, failed)"
    )
    result: Optional[str] = Field(
        None, description="Execution result or output of this step"
    )


class TaskPlan(BaseModel):
    rationale: str = Field(
        ..., description="Thinking process and plan summary for answering the query"
    )
    steps: List[TaskStep] = Field(
        default_factory=list, description="Ordered steps needed to solve the task"
    )


class ResolvedArguments(BaseModel):
    arguments: dict = Field(
        ...,
        description="Concrete key-value arguments to call the tool with. "
        "Every value must be a real, specific value - never placeholder "
        "text such as '<selected_movie_title>'.",
    )


# The planner drafts tool_arguments for ALL steps in a single upfront LLM
# call, before any tool has actually executed. When a later step depends on
# an earlier step's result (e.g. "get comments for the movie we just
# picked"), the planner can't know that value yet and writes a placeholder
# token instead, e.g. "<selected_action_movie_title>". If that placeholder
# is passed straight through to the tool call, the tool receives the
# literal string and fails ("No movie found matching
# '<selected_action_movie_title>'"). This regex flags that pattern so
# tool_executor_node can re-resolve it against real prior results instead
# of executing it literally.
_PLACEHOLDER_RE = re.compile(r"^\s*<[^<>]+>\s*$")


# near the top of agent.py, alongside _PLACEHOLDER_RE
# Tools that must always pause for human approval before executing,
# regardless of whether the planner LLM remembered to set
# requires_approval=True on that step. Relying on the LLM alone for this
# is exactly how a send_email step slipped through ungated - the field
# description in TaskStep is just a hint to the model, not an enforced
# rule, so anything genuinely irreversible/sensitive belongs in this set
# instead of only in the prompt.
SENSITIVE_TOOL_NAMES = {"send_email", "write_file", "delete_file"}


def _sanitize_messages_for_llm(messages: List[BaseMessage]) -> List[BaseMessage]:
    """Replace raw `ToolMessage`s with `SystemMessage`s carrying the same
    content, for any message list about to be sent to a chat-completion
    call (planner, argument resolver, responder).

    This app's planner/executor is a hand-rolled step-by-step dispatcher,
    not OpenAI's native tool-calling protocol - `tool_executor_node`
    appends a `ToolMessage` straight into `state["messages"]` with no
    preceding `AIMessage.tool_calls` entry referencing it (there isn't
    one; a `TaskStep` isn't a tool call in LangChain's sense). That's fine
    for OpenRouter-proxied models, which are generally lenient about
    message-list shape, but real OpenAI's Chat Completions API strictly
    validates it and 400s with "messages with role 'tool' must be a
    response to a preceding message with 'tool_calls'" the moment a
    ToolMessage shows up without one - which happens on turn 2+ of any
    thread that executed a tool, since that ToolMessage is still sitting
    in history. SystemMessage carries the same content without that
    pairing requirement.
    """
    sanitized = []
    for m in messages:
        if isinstance(m, ToolMessage):
            sanitized.append(SystemMessage(content=f"[Tool result] {m.content}"))
        else:
            sanitized.append(m)
    return sanitized


def _contains_placeholder(value: Any) -> bool:
    """Recursively check whether a tool-argument value (or nested
    dict/list) still contains unresolved planner placeholder text."""
    if isinstance(value, str):
        return bool(_PLACEHOLDER_RE.match(value))
    if isinstance(value, dict):
        return any(_contains_placeholder(v) for v in value.values())
    if isinstance(value, list):
        return any(_contains_placeholder(v) for v in value)
    return False


def _missing_required_args(args: dict, schema: Optional[dict]) -> List[str]:
    """Return the required argument names (per the tool's JSON schema) that
    are absent, null, or blank in `args`.

    Complements `_contains_placeholder`: when a step depends on an earlier
    step's result, the planner sometimes leaves the dependent argument out
    entirely (or writes null/"") instead of a '<placeholder>' string. That
    doesn't match `_PLACEHOLDER_RE`, so without this check the tool call
    would sail through with a missing required argument and fail outright
    instead of being re-resolved against prior step results.
    """
    if not schema:
        return []
    required = schema.get("required") or []
    missing = []
    for key in required:
        if key not in args:
            missing.append(key)
            continue
        value = args[key]
        if value is None or (isinstance(value, str) and value.strip() == ""):
            missing.append(key)
    return missing


def _sanitize_args_to_schema(
    tool_name: str, args: dict, schema: Optional[dict]
) -> dict:
    """Drop any argument keys not declared in the tool's JSON schema
    `properties`.

    Both the planner and `_resolve_step_arguments` are free-form LLM calls -
    the resolver in particular is given the full text of prior tool results
    as context (e.g. a movie-details blob containing a trailing "_id: ..."
    line) and can hallucinate an extra key from that text (seen in practice:
    "is_id") alongside the correct ones. MCP tool calls are validated
    strictly server-side, so a single unexpected keyword rejects the whole
    call - even when every other argument was resolved correctly. Filtering
    to known keys here prevents one stray hallucinated field from silently
    sinking an otherwise-correct call.
    """
    if not schema:
        return args
    properties = schema.get("properties")
    if not properties:
        return args
    allowed = set(properties.keys())
    dropped = set(args.keys()) - allowed
    if dropped:
        logger.warning(
            f"Dropping argument key(s) {sorted(dropped)} not accepted by "
            f"tool '{tool_name}' (allowed: {sorted(allowed)})"
        )
    return {k: v for k, v in args.items() if k in allowed}


# --- LangGraph State Schema ---


class AgentState(TypedDict):
    messages: Annotated[List[BaseMessage], add_messages]
    plan: List[dict]  # List of TaskStep serialized as dict
    current_step_index: int
    user_id: str
    session_id: str
    tokens: Dict[str, int]  # Tracks prompt_tokens and completion_tokens


# --- Workflow Nodes & Logic ---


class AgentEngine:
    """Orchestrates LangGraph execution nodes and state transitions."""

    def __init__(self, mcp_client_manager: MCPClientManager):
        self.mcp = mcp_client_manager

    async def planner_node(
        self, state: AgentState, config: RunnableConfig
    ) -> Dict[str, Any]:
        """Planner node: Decomposes the user query into a structured execution plan."""
        logger.info("Running Planner Node...")

        user_id = state.get("user_id", "default_user")
        session_id = state.get("session_id", "default_session")
        messages = state["messages"]

        # Instantiate LLM and bind structured output schema.
        # method="function_calling" (rather than ChatOpenAI's own default of
        # "json_schema", i.e. OpenAI's strict Structured Outputs API) - that
        # default routes through the OpenAI SDK's own strict JSON parser
        # (openai.lib._parsing._completions), which raises a raw
        # pydantic_core.ValidationError and kills the whole run if the
        # model doesn't emit conformant JSON. Models proxied through
        # OpenRouter (e.g. Gemini) don't reliably honor that strict mode -
        # for a plain greeting with nothing to plan, the model sometimes
        # replies with ordinary text instead of a TaskPlan JSON object,
        # which crashed here. function_calling uses tool-call argument
        # extraction instead, which is far more broadly supported across
        # OpenRouter-proxied models.
        llm = get_llm(temperature=0.1)
        planner_llm = llm.with_structured_output(
            TaskPlan, method="function_calling"
        ).with_config(tags=["planner_llm"])

        system_prompt = (
            "You are a master planner agent for a personal assistant. "
            "Examine the conversation history and construct a clear step-by-step plan "
            "to answer the user's latest request. "
            "Decide which tools (if any) are required. Available tools:\n"
        )

        # Dynamically append tools metadata for the planner
        tools = await self.mcp.list_tools()
        logger.info(
            "Planner discovered %d MCP tool(s): %s",
            len(tools),
            [tool["name"] for tool in tools],
        )
        if tools:
            for t in tools:
                system_prompt += f"- {t['name']}: {t['description']}. Arguments Schema: {t['inputSchema']}\n"
        else:
            system_prompt += (
                "(No tools are currently connected. Do not invent a tool_name.)\n"
            )

        # Long-term (cross-thread) memory - see agents/memory.py. Retrieved
        # by relevance to the latest user message, not "load everything",
        # and kept in two separate blocks so the model doesn't conflate a
        # standing fact about the user with a specific past conversation.
        # No-ops (returns []) if long-term memory isn't configured
        # (OPENAI_API_KEY unset) or the read fails - never blocks planning.
        latest_user_text = next(
            (
                m.content
                for m in reversed(messages)
                if isinstance(m, HumanMessage) and m.content
            ),
            "",
        )
        if latest_user_text:
            facts = long_term_memory.recall(user_id, latest_user_text, top_k=5)
            if facts:
                system_prompt += "\nWhat you know about this user:\n"
                for f in facts:
                    system_prompt += f"- {f.get('memory', f)}\n"

            episodes = long_term_memory.recall(
                user_id, latest_user_text, top_k=3, kind="episode"
            )
            if episodes:
                system_prompt += "\nRelevant past conversations with this user:\n"
                for e in episodes:
                    system_prompt += f"- {e.get('memory', e)}\n"

        system_prompt += (
            "\nIMPORTANT - you are ONLY planning, not executing:\n"
            "- `tool_name` must be either the exact name of one of the tools listed above, "
            "or omitted (null) if this step needs no tool call. Never write the literal "
            'text "none" or any other placeholder as a tool_name.\n'
            '- Every step\'s `status` must be left as "pending" and `result` must be left '
            "as null - you are not the one who runs tools or produces final answers, that "
            "happens in later steps. Do not pre-fill results or mark steps completed.\n"
        )

        full_messages = [SystemMessage(content=system_prompt)] + _sanitize_messages_for_llm(
            messages
        )

        # Invoke Planner model
        response = await planner_llm.ainvoke(full_messages)

        # Track and log token usage (approximated or using metadata)
        prompt_text = "".join([getattr(m, "content", "") for m in full_messages])
        completion_text = str(response)

        prompt_tokens = estimate_tokens(prompt_text)
        completion_tokens = estimate_tokens(completion_text)

        log_token_usage(
            user_id=user_id,
            session_id=session_id,
            model_name=getattr(
                llm, "model_name", getattr(llm, "model", "unknown-model")
            ),
            input_tokens=prompt_tokens,
            output_tokens=completion_tokens,
        )

        # Convert steps to dict format for storage in state.
        #
        # The planner LLM is only supposed to plan, not execute - but
        # structured-output schemas don't stop a model from ignoring the
        # prompt and pre-filling `status`/`result` as if steps were already
        # run (we've seen it return "COMPLETED" with a finished answer
        # baked in, bypassing tool_executor_node entirely). Force these
        # fields back to their real, un-executed state regardless of what
        # the model produced, so execution always actually happens.
        #
        # Also normalize `tool_name`: the model sometimes writes the
        # literal string "none" instead of leaving the field null, which
        # is truthy in Python and would make tool_executor_node try to
        # call a real MCP tool literally named "none".
        _NO_TOOL_VALUES = {"", "none", "null", "n/a", "na"}
        serialized_steps = []
        for step in response.steps:
            step_dict = step.model_dump()
            tool_name = step_dict.get("tool_name")
            if (
                isinstance(tool_name, str)
                and tool_name.strip().lower() in _NO_TOOL_VALUES
            ):
                step_dict["tool_name"] = None
            step_dict["status"] = "pending"
            step_dict["result"] = None
            # Don't trust the planner LLM's own requires_approval judgment
            # for known-sensitive tools - force it regardless of what the
            # model decided this run, so gating can't silently vary
            # request-to-request the way it just did for send_email.
            if step_dict.get("tool_name") in SENSITIVE_TOOL_NAMES:
                step_dict["requires_approval"] = True
            serialized_steps.append(step_dict)

        # Add assistant planning explanation to message thread
        plan_explanation = (
            f"### Rationale\n{response.rationale}\n\n### Proposed Execution Plan\n"
        )
        for step_dict in serialized_steps:
            tool_info = (
                f" (Tool: `{step_dict['tool_name']}`)"
                if step_dict.get("tool_name")
                else ""
            )
            plan_explanation += f"{step_dict['id']}. [{step_dict['status'].upper()}] {step_dict['description']}{tool_info}\n"

        return {
            "plan": serialized_steps,
            "current_step_index": 0,
            # Tagged "internal" so this raw rationale/step-list scaffold
            # stays in the graph's own message history (useful context for
            # later planner/resolver/responder LLM calls) but is filtered
            # out of the user-facing transcript - see
            # memory_service.get_thread_messages. The structured plan is
            # already sent to the frontend separately as a "plan" SSE event
            # (see chat.py), so this text duplicate shouldn't be shown too.
            "messages": [
                AIMessage(
                    content=plan_explanation, additional_kwargs={"internal": "plan"}
                )
            ],
            "tokens": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            },
        }

    async def tool_executor_node(
        self, state: AgentState, config: RunnableConfig
    ) -> Dict[str, Any]:
        """Executor node: Executes the current active tool step in the plan."""
        plan = state["plan"]
        idx = state["current_step_index"]

        if idx >= len(plan):
            return {}

        current_step = plan[idx]
        tool_name = current_step.get("tool_name")
        if isinstance(tool_name, str) and tool_name.strip().lower() in (
            "",
            "none",
            "null",
            "n/a",
            "na",
        ):
            tool_name = None
            current_step["tool_name"] = (
                None  # keep stored state consistent with what we actually do
            )
        args = current_step.get("tool_arguments") or {}

        schema = None
        if tool_name:
            tools = await self.mcp.list_tools()
            schema = next(
                (t["inputSchema"] for t in tools if t["name"] == tool_name), None
            )

        # The planner draws up tool_arguments for every step at plan time,
        # before earlier steps have run - so a step that depends on an
        # earlier step's output (e.g. "look up comments for the movie we
        # just picked") gets a placeholder like
        # "<selected_action_movie_title>" instead of a real value. Detect
        # that and re-resolve the arguments now, using the real prior tool
        # results that are already in `state["messages"]`, instead of
        # calling the tool with the literal placeholder text.
        resolution_reason = None
        if tool_name and _contains_placeholder(args):
            resolution_reason = "unresolved placeholder text"
        elif tool_name:
            missing = _missing_required_args(args, schema)
            if missing:
                resolution_reason = f"missing required argument(s) {missing}"

        if resolution_reason:
            logger.info(
                f"Step {idx+1} tool_arguments for '{tool_name}' has "
                f"{resolution_reason} ({args}); re-resolving against prior "
                "step results before executing."
            )
            args = await self._resolve_step_arguments(
                tool_name=tool_name,
                draft_args=args,
                step_description=current_step.get("description", ""),
                messages=state["messages"],
                schema=schema,
            )
            plan[idx]["tool_arguments"] = args

            # The resolver's merge falls back to the draft value for any
            # key it didn't touch (see `_resolve_step_arguments`) - if it
            # omits a key from its structured output entirely rather than
            # actually resolving it, that fallback silently keeps the
            # original placeholder text (e.g. "<details_from_step_2>")
            # instead of a real value. Executing at that point would mean
            # the tool call literally writes/uses the placeholder string -
            # verify it's actually gone rather than trusting the resolver.
            if _contains_placeholder(args):
                logger.error(
                    f"Step {idx+1} tool_arguments for '{tool_name}' still "
                    f"contain unresolved placeholder text after resolution "
                    f"({args}); refusing to execute with placeholder data."
                )
                plan[idx]["status"] = "failed"
                plan[idx]["result"] = (
                    "Could not resolve all arguments from prior step "
                    "results; execution skipped to avoid running with "
                    "placeholder text."
                )
                tool_msg = ToolMessage(
                    content=f"Error: {plan[idx]['result']}",
                    name=tool_name or "system_step",
                    tool_call_id=f"step_{idx+1}",
                )
                return {
                    "plan": plan,
                    "current_step_index": idx + 1,
                    "messages": [tool_msg],
                }

        # Whether or not resolution ran above, guard against any argument
        # key that isn't actually declared on the tool - a hallucinated or
        # mis-named key (from the planner OR the resolver) would otherwise
        # sink the entire tool call at the MCP validation layer. See
        # `_sanitize_args_to_schema`.
        if tool_name and schema:
            args = _sanitize_args_to_schema(tool_name, args, schema)
            plan[idx]["tool_arguments"] = args

        # Guardrail check on sensitive-tool arguments before execution -
        # attaches a warning to the step (surfaced through the existing
        # `plan` SSE event, alongside the requires_approval gate already
        # forced for these tools in planner_node) rather than blocking;
        # the actual block point for sensitive tools is the HITL approval
        # gate (route_step/interrupt_before), not this check.
        if tool_name in SENSITIVE_TOOL_NAMES:
            guardrail = check_text(str(args), context="tool_arguments")
            if guardrail.blocked or guardrail.matched_patterns:
                plan[idx]["guardrail_warning"] = (
                    guardrail.reason
                    or f"Possible sensitive content in arguments: {', '.join(guardrail.matched_patterns)}"
                )

        logger.info(f"Running Tool Executor Node on step {idx+1}: calling {tool_name}")

        # Update step status to active
        plan[idx]["status"] = "active"

        tool_result = ""
        if tool_name:
            try:
                # Call tool via MCP Client Manager
                tool_result = await self.mcp.execute_tool(tool_name, args)
                plan[idx]["status"] = "completed"
                plan[idx]["result"] = tool_result
            except Exception as e:
                logger.error(f"Error executing tool {tool_name}: {str(e)}")
                plan[idx]["status"] = "failed"
                plan[idx]["result"] = str(e)
                tool_result = f"Error: {str(e)}"
        else:
            plan[idx]["status"] = "completed"
            tool_result = "No tool execution required for this step."

        # Append a message representing the tool execution output
        tool_msg = ToolMessage(
            content=tool_result,
            name=tool_name or "system_step",
            tool_call_id=f"step_{idx+1}",
        )

        return {"plan": plan, "current_step_index": idx + 1, "messages": [tool_msg]}

    async def _resolve_step_arguments(
        self,
        tool_name: str,
        draft_args: dict,
        step_description: str,
        messages: List[BaseMessage],
        schema: Optional[dict] = None,
    ) -> dict:
        """Fill in placeholder tool_arguments left by the planner using the
        real results of earlier steps (present in `messages` as
        ToolMessages), instead of executing the tool with literal
        placeholder text like '<selected_action_movie_title>'."""
        if schema is None:
            tools = await self.mcp.list_tools()
            schema = next(
                (t["inputSchema"] for t in tools if t["name"] == tool_name), {}
            )
        allowed_keys = sorted((schema or {}).get("properties", {}).keys())

        system_prompt = (
            "You are resolving arguments for an MCP tool call that is part of "
            "a multi-step plan. The draft arguments below were written before "
            "earlier steps executed, so some values are placeholder text "
            "(e.g. '<selected_action_movie_title>') or missing entirely, "
            "standing in for a real value that should come from an earlier "
            "step's tool result.\n\n"
            f"Tool: {tool_name}\n"
            f"Arguments schema: {schema}\n"
            f"Allowed argument keys (use ONLY these, exactly as spelled - do "
            f"not invent, rename, or add any other key such as an id field "
            f"you notice in the conversation): {allowed_keys}\n"
            f"Step description: {step_description}\n"
            f"Draft arguments: {draft_args}\n\n"
            "Look at the conversation so far, including prior tool results, "
            "and output the REAL, concrete arguments to call this tool with. "
            "Never output placeholder text wrapped in '<...>' - use the "
            "actual value found in the conversation. Your output MUST "
            "include every key present in Draft arguments above, each with "
            "either its already-correct original value or the real "
            "resolved value - never drop or skip a key, even a long piece "
            "of text like a full plot summary or file content."
        )

        # method="function_calling" - see the identical note on the
        # planner's with_structured_output call above.
        llm = (
            get_llm(temperature=0.0)
            .with_structured_output(ResolvedArguments, method="function_calling")
            .with_config(tags=["arg_resolver_llm"])
        )

        full_messages = [SystemMessage(content=system_prompt)] + _sanitize_messages_for_llm(
            messages
        )

        try:
            response = await llm.ainvoke(full_messages)
            resolved = response.arguments or {}
            # Merge: resolved values win, but fall back to the draft for any
            # key the resolver didn't touch. Any key outside the tool's
            # schema (hallucinated by either the planner or this resolver)
            # is stripped later in tool_executor_node via
            # `_sanitize_args_to_schema`, not here, so both sources go
            # through the same single guard.
            return {**draft_args, **resolved}
        except Exception as e:
            logger.error(
                f"Failed to resolve placeholder arguments for tool "
                f"'{tool_name}': {e}"
            )
            return draft_args

    async def responder_node(
        self, state: AgentState, config: RunnableConfig
    ) -> Dict[str, Any]:
        """Responder node: Synthesizes final response incorporating execution logs."""
        logger.info("Running Responder Node...")

        user_id = state.get("user_id", "default_user")
        session_id = state.get("session_id", "default_session")
        messages = state["messages"]
        plan = state["plan"]

        # `streaming=True` is required here (not just the "responder_llm"
        # tag) - without it, ChatOpenAI never emits token-by-token callback
        # events, so astream_events in chat.py has nothing to forward as
        # "token" SSE events even though the model call itself succeeds and
        # produces a full answer.
        llm = get_llm(temperature=0.3, streaming=True).with_config(
            tags=["responder_llm"]
        )

        # Provide execution details to context
        context_prompt = (
            "You are a personal assistant. Review the user's prompt, the steps planned, "
            "and the results from the executed tools below. Synthesize a comprehensive final response.\n\n"
            "Execution Plan Details:\n"
        )
        for s in plan:
            context_prompt += (
                f"- Step {s['id']}: {s['description']} | Status: {s['status']}\n"
            )
            if s.get("result"):
                context_prompt += f"  Result: {s['result']}\n"

        system_msg = SystemMessage(content=context_prompt)
        full_messages = [system_msg] + _sanitize_messages_for_llm(messages)

        response = await llm.ainvoke(full_messages)

        prompt_text = "".join([getattr(m, "content", "") for m in full_messages])
        completion_text = response.content

        prompt_tokens = estimate_tokens(prompt_text)
        completion_tokens = estimate_tokens(completion_text)

        log_token_usage(
            user_id=user_id,
            session_id=session_id,
            model_name=getattr(
                llm, "model_name", getattr(llm, "model", "unknown-model")
            ),
            input_tokens=prompt_tokens,
            output_tokens=completion_tokens,
        )

        # Accumulate token counts
        existing_tokens = state.get("tokens") or {
            "prompt_tokens": 0,
            "completion_tokens": 0,
        }
        total_p = existing_tokens.get("prompt_tokens", 0) + prompt_tokens
        total_c = existing_tokens.get("completion_tokens", 0) + completion_tokens

        # Write this turn to long-term (semantic) memory - fire-and-forget,
        # never awaited, so a slow/failed memory write can't add latency to
        # the streamed response or fail the chat request. mem0's own
        # extraction pipeline decides what (if anything) is durable enough
        # to keep; most turns produce nothing, which is expected.
        latest_user_text = next(
            (
                m.content
                for m in reversed(messages)
                if isinstance(m, HumanMessage) and m.content
            ),
            "",
        )
        if latest_user_text and response.content:
            asyncio.create_task(
                asyncio.to_thread(
                    long_term_memory.remember,
                    user_id,
                    [
                        {"role": "user", "content": latest_user_text},
                        {"role": "assistant", "content": response.content},
                    ],
                    thread_id=session_id,
                )
            )

        return {
            "messages": [response],
            "tokens": {"prompt_tokens": total_p, "completion_tokens": total_c},
        }


# --- Router Logic ---

# Human-in-the-loop note:
# LangGraph's `interrupt_before=[...]` (set at compile time, below) pauses
# execution before *every* visit to the named node(s) - it has no way to look
# at which plan step is about to run, so it can't be conditioned on that
# step's own `requires_approval` flag. To get per-step approval instead of
# "every tool call always pauses", `tool_executor_node` is registered under
# two different node names pointing at the identical function:
#   - "tool_executor"        -> runs immediately, no pause
#   - "tool_executor_gated"  -> the only name listed in interrupt_before, so
#                                the graph halts right before it runs
# `route_step` picks which name to go to based on the step's own flag.
GATED_TOOL_EXECUTOR_NODE = "tool_executor_gated"
TOOL_EXECUTOR_NODE = "tool_executor"


def route_step(state: AgentState) -> str:
    """Conditional router: Decides next node based on execution plan state."""
    plan = state.get("plan") or []
    idx = state.get("current_step_index", 0)

    # If all planned steps have been iterated, route to responder
    if idx >= len(plan):
        return "responder"

    next_step = plan[idx]

    # Only route through the gated (interrupted-before) node when this
    # specific step needs approval and hasn't been approved/run yet.
    if next_step.get("requires_approval") and next_step.get("status") == "pending":
        return GATED_TOOL_EXECUTOR_NODE

    return TOOL_EXECUTOR_NODE


# --- Graph Construction Builder ---


def create_agent(mcp_client_manager: MCPClientManager) -> Any:
    """Build and compile the StateGraph agent workflow with MongoDB checkpoint savers."""
    engine = AgentEngine(mcp_client_manager)

    workflow = StateGraph(AgentState)

    # Register graph nodes. `tool_executor` and `tool_executor_gated` both
    # run the exact same function - see the note above route_step for why
    # there are two names for one implementation.
    workflow.add_node("planner", engine.planner_node)
    workflow.add_node(TOOL_EXECUTOR_NODE, engine.tool_executor_node)
    workflow.add_node(GATED_TOOL_EXECUTOR_NODE, engine.tool_executor_node)
    workflow.add_node("responder", engine.responder_node)

    # Configure graph paths
    workflow.add_edge(START, "planner")

    # Router routes output of planner and executor
    workflow.add_conditional_edges(
        "planner",
        route_step,
        {
            TOOL_EXECUTOR_NODE: TOOL_EXECUTOR_NODE,
            GATED_TOOL_EXECUTOR_NODE: GATED_TOOL_EXECUTOR_NODE,
            "responder": "responder",
        },
    )

    workflow.add_conditional_edges(
        TOOL_EXECUTOR_NODE,
        route_step,
        {
            TOOL_EXECUTOR_NODE: TOOL_EXECUTOR_NODE,
            GATED_TOOL_EXECUTOR_NODE: GATED_TOOL_EXECUTOR_NODE,
            "responder": "responder",
        },
    )

    # The gated node needs the exact same outgoing edges as the ungated one -
    # once it actually runs (after approval), it routes onward identically.
    workflow.add_conditional_edges(
        GATED_TOOL_EXECUTOR_NODE,
        route_step,
        {
            TOOL_EXECUTOR_NODE: TOOL_EXECUTOR_NODE,
            GATED_TOOL_EXECUTOR_NODE: GATED_TOOL_EXECUTOR_NODE,
            "responder": "responder",
        },
    )

    workflow.add_edge("responder", END)

    # Memory Checkpointer
    checkpointer = MongoCheckpointSaver()

    # We compile the graph.
    # To implement Human-in-the-Loop, we interrupt BEFORE executing tools -
    # but only before the *gated* node, so only steps that were actually
    # flagged `requires_approval` cause a pause. Ordinary steps run straight
    # through via the "tool_executor" node/name instead.
    compiled_agent = workflow.compile(
        checkpointer=checkpointer, interrupt_before=[GATED_TOOL_EXECUTOR_NODE]
    )

    return compiled_agent
