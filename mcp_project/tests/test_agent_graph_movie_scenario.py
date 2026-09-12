"""End-to-end test of the compiled LangGraph agent (`create_agent`)
reproducing the exact query from the original bug report:

    "get me the reviews of best action movie ?"

Original (broken, pre-fix) behavior, taken verbatim from the reported
frontend/console output:
    tool: get_movie_comments called with
          {"selected_movie_title": "Band of Brothers"}  # planner's stated
          intent
      -> but the ACTUAL call sent title="<selected_action_movie_title>"
      -> "No movie found matching '<selected_action_movie_title>'."

Expected (fixed) behavior, asserted below:
    tool_executor_node detects the unresolved placeholder in step 2's
    drafted arguments, resolves it against step 1's real tool result, and
    calls get_movie_comments with the actual top-rated title
    ("Band of Brothers") - producing real reviews, not an error string.

Everything except the LLM and MCP layers is the real production code:
`create_agent`, `planner_node`, `route_step`, `tool_executor_node`
(including the placeholder-resolution fix), and `responder_node` all run
unmodified.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver

from mcp_orchestration.agents import agent as agent_module
from mcp_orchestration.agents.agent import (
    ResolvedArguments,
    TaskPlan,
    TaskStep,
    create_agent,
)

from tests.fakes import (
    MOVIE_COMMENTS,
    FakeMCPClientManager,
    ScriptedLLM,
)

pytestmark = pytest.mark.asyncio


# What a planner LLM plausibly produces for this query: it can see the
# tool schemas but NOT step 1's real output yet, so step 2's title is a
# placeholder - exactly matching the original bug report.
PLANNER_RESPONSE = TaskPlan(
    rationale=(
        "First fetch the top-rated action movies, then pull reviews for "
        "whichever one comes out on top."
    ),
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
    content=(
        "Band of Brothers (2001) is the top-rated action pick at 9.6 on "
        "IMDB. Reviewers call it one of the best war miniseries ever made "
        "and praise the performances and production value, though a "
        "couple note it's a heavy watch."
    )
)


@pytest.fixture
def wired_agent(monkeypatch):
    """Build the real compiled graph from `create_agent`, with only the
    LLM and MCP layers swapped for test doubles."""
    fake_mcp = FakeMCPClientManager()
    scripted_llm = ScriptedLLM(
        [PLANNER_RESPONSE, RESOLVER_RESPONSE, RESPONDER_RESPONSE]
    )

    monkeypatch.setattr(agent_module, "get_llm", scripted_llm.make_factory())
    # Swap the Mongo-backed checkpointer for an in-memory one so this test
    # needs no real MongoDB - `create_agent` is otherwise exercised exactly
    # as `api/routes/chat.py` uses it in production.
    monkeypatch.setattr(agent_module, "MongoCheckpointSaver", MemorySaver)

    graph = create_agent(fake_mcp)
    return graph, fake_mcp, scripted_llm


async def test_movie_review_query_end_to_end(wired_agent):
    graph, fake_mcp, scripted_llm = wired_agent
    config = {"configurable": {"thread_id": "test-thread-movies"}}

    inputs = {
        "messages": [HumanMessage(content="get me the reviews of best action movie ?")],
        "user_id": "test-user",
        "session_id": "test-thread-movies",
        "plan": [],
        "current_step_index": 0,
    }

    final_state = await graph.ainvoke(inputs, config=config)

    # --- Tool-call level: what was ACTUALLY sent to the MCP server -----
    assert [c["tool_name"] for c in fake_mcp.calls] == [
        "get_movies_by_genre",
        "get_movie_comments",
    ]
    assert fake_mcp.calls[0]["arguments"] == {"genre": "Action", "limit": 5}
    # This is the exact line that failed in the bug report: it used to be
    # {"title": "<selected_action_movie_title>", "limit": 5}.
    assert fake_mcp.calls[1]["arguments"] == {
        "title": "Band of Brothers",
        "limit": 5,
    }

    # --- Plan/state level ------------------------------------------------
    plan = final_state["plan"]
    assert plan[0]["status"] == "completed"
    assert plan[1]["status"] == "completed"
    assert plan[1]["result"] == MOVIE_COMMENTS["Band of Brothers"]
    assert "No movie found matching" not in (plan[1]["result"] or "")
    # The placeholder must be gone from the stored plan, not just from the
    # call that was made with it.
    assert plan[1]["tool_arguments"]["title"] == "Band of Brothers"

    # --- Final responder answer ------------------------------------------
    final_message = final_state["messages"][-1]
    assert final_message.content == RESPONDER_RESPONSE.content
    assert "Band of Brothers" in final_message.content

    # Confirm all three scripted LLM calls (planner, resolver, responder)
    # were actually consumed in order, not skipped or short-circuited.
    assert len(scripted_llm.calls) == 3


async def test_single_step_query_does_not_trigger_resolver_call(monkeypatch):
    """Sanity check the fix is targeted: a plan with no cross-step
    dependency should NOT trigger an extra LLM call."""
    fake_mcp = FakeMCPClientManager()

    single_step_plan = TaskPlan(
        rationale="Just fetch the movies, no follow-up needed.",
        steps=[
            TaskStep(
                id=1,
                description="Fetch top-rated action movies",
                tool_name="get_movies_by_genre",
                tool_arguments={"genre": "Action", "limit": 5},
                requires_approval=False,
            ),
        ],
    )
    responder_response = AIMessage(content="Here are the top action movies.")

    scripted_llm = ScriptedLLM([single_step_plan, responder_response])
    monkeypatch.setattr(agent_module, "get_llm", scripted_llm.make_factory())
    monkeypatch.setattr(agent_module, "MongoCheckpointSaver", MemorySaver)

    graph = create_agent(fake_mcp)
    config = {"configurable": {"thread_id": "test-thread-no-resolve"}}
    inputs = {
        "messages": [HumanMessage(content="show me top action movies")],
        "user_id": "test-user",
        "session_id": "test-thread-no-resolve",
        "plan": [],
        "current_step_index": 0,
    }

    await graph.ainvoke(inputs, config=config)

    # Only planner + responder should have run - no resolver call.
    assert len(scripted_llm.calls) == 2
    assert [c["tool_name"] for c in fake_mcp.calls] == ["get_movies_by_genre"]
