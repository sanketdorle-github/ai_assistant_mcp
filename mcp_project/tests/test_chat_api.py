"""API-level tests for `POST /chat/stream`.

These hit the real FastAPI route (`api/routes/chat.py:chat_stream` /
`run_agent_stream`) through `TestClient`, with only three things swapped
for test doubles via monkeypatch:
  - `MCPClientManager`  -> `FakeMCPClientManager` (no subprocess spawned)
  - `load_mcp_server_configs` -> returns an empty list (nothing to connect)
  - `agents.agent.get_llm` / `MongoCheckpointSaver` -> `ScriptedLLM` /
    in-memory `MemorySaver` (no OpenRouter call, no real MongoDB)

Auth is bypassed via FastAPI's `dependency_overrides` on `get_current_user`
- this suite is about the chat/agent pipeline, not the auth layer.

Caveat this suite is written to work around: the fake responder LLM
(`ScriptedLLM`) isn't a real LangChain `Runnable`/`ChatOpenAI`, so it
never emits the `on_chat_model_stream` callback events that
`run_agent_stream` listens for to build "token" SSE events. That means no
"token" events show up in these tests - which is expected and fine, since
the full text of the final answer (and that it's driven from the real
resolved tool result rather than the placeholder) is already asserted
end-to-end in `test_agent_graph_movie_scenario.py` via `graph.ainvoke`.
What THIS suite checks that that one can't: the actual HTTP/SSE contract
- status code, event names, and the exact `plan` payload (including
`tool_arguments`/`result` per step) the frontend would receive.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import MemorySaver

from mcp_orchestration.agents import agent as agent_module
from mcp_orchestration.agents.agent import ResolvedArguments, TaskPlan, TaskStep
from mcp_orchestration.api.routes import chat as chat_module
from mcp_orchestration.api.routes.auth import get_current_user

from tests.conftest import parse_sse_events
from tests.fakes import FakeMCPClientManager, ScriptedLLM

PLANNER_RESPONSE = TaskPlan(
    rationale="First fetch top-rated action movies, then get its reviews.",
    steps=[
        TaskStep(
            id=1,
            description="Fetch top-rated action movies",
            tool_name="get_movies_by_genre",
            tool_arguments={"genre": "Action", "limit": 5},
            requires_approval=False,
        ),
        TaskStep(
            id=2,
            description="Get reviews for the top action movie",
            tool_name="get_movie_comments",
            tool_arguments={
                "title": "<selected_action_movie_title>",
                "limit": 5,
            },
            requires_approval=False,
        ),
    ],
)

RESOLVER_RESPONSE = ResolvedArguments(
    arguments={"title": "Band of Brothers", "limit": 5}
)

RESPONDER_RESPONSE = AIMessage(
    content="Band of Brothers is the top action pick, with strong reviews."
)


def _build_test_app() -> FastAPI:
    """A minimal app that only mounts the chat router - avoids the full
    `main.create_app()` lifespan, which connects to a real MongoDB on
    startup and isn't relevant to testing the chat route's own logic."""
    app = FastAPI()
    app.include_router(chat_module.router)
    app.dependency_overrides[get_current_user] = lambda: {
        "id": "test-user",
        "email": "test@example.com",
    }
    return app


@pytest.fixture
def api_client(monkeypatch):
    fake_mcp = FakeMCPClientManager()
    scripted_llm = ScriptedLLM(
        [PLANNER_RESPONSE, RESOLVER_RESPONSE, RESPONDER_RESPONSE]
    )

    monkeypatch.setattr(agent_module, "get_llm", scripted_llm.make_factory())
    monkeypatch.setattr(agent_module, "MongoCheckpointSaver", MemorySaver)
    monkeypatch.setattr(chat_module, "MCPClientManager", lambda: fake_mcp)
    monkeypatch.setattr(chat_module, "load_mcp_server_configs", lambda user_id=None: [])

    app = _build_test_app()
    client = TestClient(app)
    return client, fake_mcp, scripted_llm


def test_chat_stream_status_and_no_error_event(api_client):
    client, fake_mcp, scripted_llm = api_client

    response = client.post(
        "/chat/stream",
        json={
            "message": "get me the reviews of best action movie ?",
            "thread_id": "api-test-thread-1",
        },
    )

    assert response.status_code == 200
    events = parse_sse_events(response.text)
    event_types = [e["event"] for e in events]

    assert "error" not in event_types, [e for e in events if e["event"] == "error"]
    assert "done" in event_types


def test_chat_stream_plan_reflects_resolved_arguments_not_placeholder(api_client):
    """The API-level version of the core regression check: the `plan`
    payload the frontend receives for step 2 must carry the REAL resolved
    title and a real result - never the placeholder or its error
    message."""
    client, fake_mcp, scripted_llm = api_client

    response = client.post(
        "/chat/stream",
        json={
            "message": "get me the reviews of best action movie ?",
            "thread_id": "api-test-thread-2",
        },
    )

    events = parse_sse_events(response.text)
    plan_events = [e for e in events if e["event"] == "plan"]
    assert plan_events, "expected at least one 'plan' SSE event"

    final_plan = plan_events[-1]["data"]["plan"]
    assert len(final_plan) == 2

    step1, step2 = final_plan
    assert step1["tool_name"] == "get_movies_by_genre"
    assert step1["status"] == "completed"

    assert step2["tool_name"] == "get_movie_comments"
    assert step2["status"] == "completed"
    assert step2["tool_arguments"]["title"] == "Band of Brothers"
    assert step2["result"] is not None
    assert "No movie found matching" not in step2["result"]

    # And the underlying fake MCP layer was actually called with the
    # resolved title, matching what the plan payload reports.
    assert fake_mcp.calls[-1]["arguments"]["title"] == "Band of Brothers"


def test_chat_stream_query_needing_no_tools(monkeypatch):
    """Edge case: a query the planner answers with zero tool steps should
    stream cleanly (plan + done, no tool calls, no error)."""
    fake_mcp = FakeMCPClientManager(tools=[])  # nothing connected
    no_tool_plan = TaskPlan(
        rationale="This is a general question, no tool needed.",
        steps=[
            TaskStep(
                id=1,
                description="Answer directly from general knowledge",
                tool_name=None,
                tool_arguments=None,
                requires_approval=False,
            )
        ],
    )
    responder_response = AIMessage(content="Sure, here's the answer directly.")
    scripted_llm = ScriptedLLM([no_tool_plan, responder_response])

    monkeypatch.setattr(agent_module, "get_llm", scripted_llm.make_factory())
    monkeypatch.setattr(agent_module, "MongoCheckpointSaver", MemorySaver)
    monkeypatch.setattr(chat_module, "MCPClientManager", lambda: fake_mcp)
    monkeypatch.setattr(chat_module, "load_mcp_server_configs", lambda user_id=None: [])

    app = _build_test_app()
    client = TestClient(app)

    response = client.post(
        "/chat/stream",
        json={
            "message": "what's the capital of France?",
            "thread_id": "api-test-thread-3",
        },
    )

    assert response.status_code == 200
    events = parse_sse_events(response.text)
    event_types = [e["event"] for e in events]

    assert "error" not in event_types
    assert "done" in event_types
    assert fake_mcp.calls == []
