"""Unit tests for the placeholder-argument fix in `agents/agent.py`.

Background (see CLAUDE.md / commit history): the planner drafts
`tool_arguments` for every plan step in a single upfront LLM call, before
any tool has run. When step N depends on step N-1's result (e.g. "get
reviews for the movie we just picked"), the planner can't know the real
value yet and writes a placeholder like `"<selected_action_movie_title>"`.
Without the fix, `tool_executor_node` called the tool with that literal
string, producing `"No movie found matching
'<selected_action_movie_title>'."` in production.

These tests exercise `_contains_placeholder`, `AgentEngine.
_resolve_step_arguments`, and `AgentEngine.tool_executor_node` directly
(no full graph), each against fake MCP/LLM layers.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage, ToolMessage

from mcp_orchestration.agents import agent as agent_module
from mcp_orchestration.agents.agent import (
    AgentEngine,
    ResolvedArguments,
    _contains_placeholder,
)

from tests.fakes import (
    ACTION_MOVIES_RESULT,
    MOVIE_COMMENTS,
    FakeMCPClientManager,
    ScriptedLLM,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# _contains_placeholder
# ---------------------------------------------------------------------------


class TestContainsPlaceholder:
    def test_flat_placeholder_detected(self):
        assert _contains_placeholder("<selected_action_movie_title>") is True

    def test_ordinary_string_not_flagged(self):
        assert _contains_placeholder("Band of Brothers") is False

    def test_empty_string_not_flagged(self):
        assert _contains_placeholder("") is False

    def test_nested_in_dict_value(self):
        assert (
            _contains_placeholder({"title": "<selected_movie_title>", "limit": 5})
            is True
        )

    def test_nested_in_list(self):
        assert _contains_placeholder(["ok", "<placeholder>"]) is True

    def test_non_string_values_ignored(self):
        assert _contains_placeholder({"limit": 5, "strict": True}) is False

    def test_angle_brackets_mid_string_not_flagged(self):
        # Only a value that IS ENTIRELY "<...>" should be treated as an
        # unresolved placeholder - a value that merely contains angle
        # brackets (e.g. a comparison a user typed) should not be.
        assert _contains_placeholder("rating < 5 and > 1") is False


# ---------------------------------------------------------------------------
# AgentEngine._resolve_step_arguments
# ---------------------------------------------------------------------------


class TestResolveStepArguments:
    async def test_fills_placeholder_from_prior_tool_result(self, monkeypatch):
        fake_mcp = FakeMCPClientManager()
        resolved = ResolvedArguments(
            arguments={"title": "Band of Brothers", "limit": 5}
        )
        scripted = ScriptedLLM([resolved])
        monkeypatch.setattr(agent_module, "get_llm", scripted.make_factory())

        engine = AgentEngine(fake_mcp)
        messages = [
            HumanMessage(content="get me the reviews of best action movie ?"),
            ToolMessage(
                content=ACTION_MOVIES_RESULT,
                name="get_movies_by_genre",
                tool_call_id="step_1",
            ),
        ]

        result_args = await engine._resolve_step_arguments(
            tool_name="get_movie_comments",
            draft_args={"title": "<selected_action_movie_title>", "limit": 5},
            step_description="Get reviews for the top action movie",
            messages=messages,
        )

        assert result_args == {"title": "Band of Brothers", "limit": 5}
        assert len(scripted.calls) == 1

    async def test_keeps_draft_keys_the_resolver_did_not_touch(self, monkeypatch):
        fake_mcp = FakeMCPClientManager()
        # Resolver only returns the placeholder key; `limit` should be
        # carried over unchanged from the draft rather than dropped.
        resolved = ResolvedArguments(arguments={"title": "Band of Brothers"})
        scripted = ScriptedLLM([resolved])
        monkeypatch.setattr(agent_module, "get_llm", scripted.make_factory())

        engine = AgentEngine(fake_mcp)
        result_args = await engine._resolve_step_arguments(
            tool_name="get_movie_comments",
            draft_args={"title": "<selected_action_movie_title>", "limit": 5},
            step_description="Get reviews for the top action movie",
            messages=[HumanMessage(content="hi")],
        )

        assert result_args == {"title": "Band of Brothers", "limit": 5}

    async def test_falls_back_to_draft_args_on_llm_error(self, monkeypatch):
        fake_mcp = FakeMCPClientManager()

        class ExplodingLLM(ScriptedLLM):
            async def ainvoke(self, messages):
                raise RuntimeError("simulated OpenRouter outage")

        exploding = ExplodingLLM([])
        monkeypatch.setattr(agent_module, "get_llm", exploding.make_factory())

        engine = AgentEngine(fake_mcp)
        draft = {"title": "<selected_action_movie_title>", "limit": 5}

        result_args = await engine._resolve_step_arguments(
            tool_name="get_movie_comments",
            draft_args=draft,
            step_description="Get reviews for the top action movie",
            messages=[HumanMessage(content="hi")],
        )

        # Resolution failing must not raise out of tool_executor_node's
        # caller - fall back to the (still-placeholder) draft so the tool
        # call still happens and surfaces a clear tool-side error, rather
        # than the whole request 500ing.
        assert result_args == draft


# ---------------------------------------------------------------------------
# AgentEngine.tool_executor_node
# ---------------------------------------------------------------------------


class TestToolExecutorNode:
    async def _base_state(self, extra_messages=None):
        return {
            "messages": [
                HumanMessage(content="get me the reviews of best action movie ?"),
                *(extra_messages or []),
            ],
            "plan": [],
            "current_step_index": 0,
            "user_id": "test-user",
            "session_id": "test-session",
            "tokens": {},
        }

    async def test_resolves_placeholder_before_calling_tool(self, monkeypatch):
        """The core regression test: step 2's tool call must receive the
        REAL movie title, never the literal placeholder string."""
        fake_mcp = FakeMCPClientManager()
        resolved = ResolvedArguments(
            arguments={"title": "Band of Brothers", "limit": 5}
        )
        scripted = ScriptedLLM([resolved])
        monkeypatch.setattr(agent_module, "get_llm", scripted.make_factory())

        engine = AgentEngine(fake_mcp)
        plan = [
            {
                "id": 1,
                "description": "Fetch top-rated action movies",
                "tool_name": "get_movies_by_genre",
                "tool_arguments": {"genre": "Action", "limit": 5},
                "requires_approval": False,
                "status": "completed",
                "result": ACTION_MOVIES_RESULT,
            },
            {
                "id": 2,
                "description": "Get reviews for the top action movie",
                "tool_name": "get_movie_comments",
                "tool_arguments": {
                    "title": "<selected_action_movie_title>",
                    "limit": 5,
                },
                "requires_approval": False,
                "status": "pending",
                "result": None,
            },
        ]
        state = await self._base_state(
            extra_messages=[
                ToolMessage(
                    content=ACTION_MOVIES_RESULT,
                    name="get_movies_by_genre",
                    tool_call_id="step_1",
                )
            ]
        )
        state["plan"] = plan
        state["current_step_index"] = 1

        out = await engine.tool_executor_node(state, config={})

        # The literal placeholder must never reach the tool call.
        assert fake_mcp.calls == [
            {
                "tool_name": "get_movie_comments",
                "arguments": {"title": "Band of Brothers", "limit": 5},
            }
        ]
        assert out["current_step_index"] == 2
        assert out["plan"][1]["status"] == "completed"
        assert out["plan"][1]["tool_arguments"] == {
            "title": "Band of Brothers",
            "limit": 5,
        }
        assert out["plan"][1]["result"] == MOVIE_COMMENTS["Band of Brothers"]
        assert "No movie found matching" not in out["plan"][1]["result"]

    async def test_step_without_placeholder_skips_resolver_call(self, monkeypatch):
        fake_mcp = FakeMCPClientManager()
        scripted = ScriptedLLM([])  # must not be called at all
        monkeypatch.setattr(agent_module, "get_llm", scripted.make_factory())

        engine = AgentEngine(fake_mcp)
        plan = [
            {
                "id": 1,
                "description": "Fetch top-rated action movies",
                "tool_name": "get_movies_by_genre",
                "tool_arguments": {"genre": "Action", "limit": 5},
                "requires_approval": False,
                "status": "pending",
                "result": None,
            }
        ]
        state = await self._base_state()
        state["plan"] = plan
        state["current_step_index"] = 0

        out = await engine.tool_executor_node(state, config={})

        assert len(scripted.calls) == 0
        assert fake_mcp.calls == [
            {
                "tool_name": "get_movies_by_genre",
                "arguments": {"genre": "Action", "limit": 5},
            }
        ]
        assert out["plan"][0]["status"] == "completed"
        assert out["plan"][0]["result"] == ACTION_MOVIES_RESULT

    async def test_step_with_no_tool_name_marks_completed_without_calling_mcp(
        self, monkeypatch
    ):
        fake_mcp = FakeMCPClientManager()
        engine = AgentEngine(fake_mcp)
        plan = [
            {
                "id": 1,
                "description": "Just acknowledge the request",
                "tool_name": None,
                "tool_arguments": None,
                "requires_approval": False,
                "status": "pending",
                "result": None,
            }
        ]
        state = await self._base_state()
        state["plan"] = plan
        state["current_step_index"] = 0

        out = await engine.tool_executor_node(state, config={})

        assert fake_mcp.calls == []
        assert out["plan"][0]["status"] == "completed"

    async def test_tool_exception_marks_step_failed_with_error_result(
        self, monkeypatch
    ):
        class BoomMCP(FakeMCPClientManager):
            async def execute_tool(self, tool_name, arguments):
                self.calls.append({"tool_name": tool_name, "arguments": arguments})
                raise RuntimeError("movie server unreachable")

        fake_mcp = BoomMCP()
        engine = AgentEngine(fake_mcp)
        plan = [
            {
                "id": 1,
                "description": "Fetch top-rated action movies",
                "tool_name": "get_movies_by_genre",
                "tool_arguments": {"genre": "Action", "limit": 5},
                "requires_approval": False,
                "status": "pending",
                "result": None,
            }
        ]
        state = await self._base_state()
        state["plan"] = plan
        state["current_step_index"] = 0

        out = await engine.tool_executor_node(state, config={})

        assert out["plan"][0]["status"] == "failed"
        assert "movie server unreachable" in out["plan"][0]["result"]
