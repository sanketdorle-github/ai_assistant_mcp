"""Eval harness: runs `evals/datasets/core_scenarios.py`'s cases through
the real compiled agent graph (real LLM, real bundled demo MCP server) and
scores them in LangSmith.

Deliberately NOT part of `uv run pytest` - this hits the real LLM/OpenAI,
costs money, and isn't deterministic; it's a periodic/CI-gate activity.
Requires the `evals` dependency group (`langsmith`) and `LANGSMITH_API_KEY`
set (see `.env.example`).

Run:
    uv run --group evals python -m evals.run_evals
"""

import asyncio
import uuid

from langchain_core.messages import HumanMessage
from langsmith import Client

from evals.datasets.core_scenarios import CASES, DATASET_DESCRIPTION, DATASET_NAME
from mcp_orchestration.agents.agent import _contains_placeholder, create_agent
from mcp_orchestration.agents.client import MCPClientManager
from mcp_orchestration.agents.llm import get_llm
from mcp_orchestration.mcp.config import load_mcp_server_configs


async def _run_agent_once(message: str) -> dict:
    """Connect the demo MCP server, run one turn through the real
    compiled graph, and return the final response + executed plan.

    `load_mcp_server_configs(None)` (no user_id) always includes the demo
    server and never includes Gmail (which requires a user_id and OAuth
    credentials) - exactly the "no external creds needed" scope this
    dataset targets.
    """
    mcp_manager = MCPClientManager()
    try:
        for server in load_mcp_server_configs(None):
            await mcp_manager.connect_server(server)

        agent = create_agent(mcp_manager)
        thread_id = f"eval-{uuid.uuid4()}"
        result = await agent.ainvoke(
            {
                "messages": [HumanMessage(content=message)],
                "user_id": "eval-user",
                "session_id": thread_id,
                "plan": [],
                "current_step_index": 0,
            },
            config={"configurable": {"thread_id": thread_id}},
        )
        final_message = result["messages"][-1]
        return {
            "response": getattr(final_message, "content", "") or "",
            "plan": result.get("plan") or [],
        }
    finally:
        await mcp_manager.close_all()


def target(inputs: dict) -> dict:
    """Sync wrapper LangSmith's `Client.evaluate()` calls per case -
    `client.evaluate()`'s target signature is sync, so the async graph run
    is driven via `asyncio.run()` here rather than making the whole script
    async."""
    return asyncio.run(_run_agent_once(inputs["message"]))


# --- Evaluators -------------------------------------------------------
# Each takes (inputs, outputs, reference_outputs) - `outputs` is target()'s
# return value, `reference_outputs` is the dataset example's `outputs`
# field (see core_scenarios.py) - and returns {"key": ..., "score": ...}.


def tool_choice_matches(inputs: dict, outputs: dict, reference_outputs: dict) -> dict:
    expected = set(reference_outputs.get("expected_tools") or [])
    actual = {s.get("tool_name") for s in outputs.get("plan", []) if s.get("tool_name")}
    if not expected:
        score = 1.0 if not actual else 0.0
    else:
        score = 1.0 if expected.issubset(actual) else 0.0
    return {"key": "tool_choice_matches", "score": score}


def no_placeholder_leak(inputs: dict, outputs: dict, reference_outputs: dict) -> dict:
    """Reuses `_contains_placeholder` directly - the exact function that
    fixed the original production bug (see `tests/test_argument_resolution.py`),
    checked here against real model output instead of `ScriptedLLM`."""
    plan = outputs.get("plan", [])
    leaked = any(
        _contains_placeholder(step.get("tool_arguments"))
        or _contains_placeholder(step.get("result"))
        for step in plan
    )
    return {"key": "no_placeholder_leak", "score": 0.0 if leaked else 1.0}


def approval_gating_correct(inputs: dict, outputs: dict, reference_outputs: dict) -> dict:
    expected_gated = set(reference_outputs.get("requires_approval_tools") or [])
    if not expected_gated:
        return {"key": "approval_gating_correct", "score": 1.0}
    plan = outputs.get("plan", [])
    ok = all(
        step.get("requires_approval") is True
        for step in plan
        if step.get("tool_name") in expected_gated
    )
    return {"key": "approval_gating_correct", "score": 1.0 if ok else 0.0}


def response_quality(inputs: dict, outputs: dict, reference_outputs: dict) -> dict:
    """LLM-as-judge, built on this app's own `get_llm()` - no new
    model-provider dependency for evals beyond what the app already uses."""
    judge = get_llm(temperature=0)
    prompt = (
        "Grade this AI assistant's response for helpfulness and "
        "correctness given the user's request. Respond with ONLY a "
        "single digit from 1 (poor) to 5 (excellent) - no other text.\n\n"
        f"User request: {inputs.get('message', '')}\n"
        f"Assistant response: {outputs.get('response', '')}"
    )
    try:
        result = judge.invoke(prompt)
        digit = "".join(c for c in str(result.content) if c.isdigit())[:1]
        score = int(digit) / 5.0 if digit else None
    except Exception:
        score = None
    return {"key": "response_quality", "score": score}


EVALUATORS = [
    tool_choice_matches,
    no_placeholder_leak,
    approval_gating_correct,
    response_quality,
]


def _get_or_create_dataset(client: Client):
    try:
        return client.read_dataset(dataset_name=DATASET_NAME)
    except Exception:
        dataset = client.create_dataset(
            dataset_name=DATASET_NAME, description=DATASET_DESCRIPTION
        )
        client.create_examples(dataset_id=dataset.id, examples=CASES)
        return dataset


def main() -> None:
    # This script runs the agent graph directly, bypassing main.py's
    # FastAPI `lifespan` - which is normally what calls `database.connect()`
    # on startup. Without it, every checkpoint save inside the graph run
    # (services/memory_service.py's MongoCheckpointSaver) would fail with
    # DatabaseUnavailableError. Requires MongoDB reachable (docker compose
    # up -d), same as running the actual API.
    from mcp_orchestration.core.database import database

    database.connect()
    try:
        client = Client()
        _get_or_create_dataset(client)

        results = client.evaluate(
            target,
            data=DATASET_NAME,
            evaluators=EVALUATORS,
            experiment_prefix="core-scenarios",
            max_concurrency=2,
        )
        print(results)
    finally:
        database.close()


if __name__ == "__main__":
    main()
