"""Test doubles used across the test suite.

`FakeMCPClientManager` stands in for `agents.client.MCPClientManager` so
tests never spawn a real MCP subprocess or talk to a real database.
`ScriptedLLM` stands in for the object `agents.llm.get_llm(...)` returns,
so tests never call OpenRouter. Both implement just enough of the real
interface for `agent.py` to run against them completely unmodified -
these tests exercise the REAL `planner_node` / `tool_executor_node` /
`route_step` / `responder_node` / `create_agent` code, only the network
edges (LLM API, MCP subprocess) are swapped out.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Fake MCP tool layer
# ---------------------------------------------------------------------------

MOVIE_TOOLS: List[Dict[str, Any]] = [
    {
        "name": "get_movies_by_genre",
        "description": "Return top movies for a genre, ranked by IMDB rating.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "genre": {"type": "string", "description": "Genre to search"},
                "limit": {"type": "integer", "description": "Max results"},
            },
            "required": ["genre"],
        },
        "server_name": "movies",
    },
    {
        "name": "get_movie_comments",
        "description": "Return recent user comments/reviews for a movie by title.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Exact movie title"},
                "limit": {"type": "integer", "description": "Max results"},
            },
            "required": ["title"],
        },
        "server_name": "movies",
    },
]

# Mirrors the exact console/frontend log from the bug report: querying
# genre="Action" surfaces "Band of Brothers (2001) | IMDB: 9.6" as the
# highest-rated result.
ACTION_MOVIES_RESULT = (
    "- SuperBob (2015) | IMDB: 5.1\n"
    "- Another World (2015) | IMDB: 4.8\n"
    "- The Masked Saint (2016) | IMDB: 5.6\n"
    "- Band of Brothers (2001) | IMDB: 9.6\n"
    "- The Real Miyagi (2015) | IMDB: 9.3"
)

MOVIE_COMMENTS: Dict[str, str] = {
    "Band of Brothers": (
        "- 'One of the best war miniseries ever made.' (5/5)\n"
        "- 'Incredible performances and production value.' (5/5)\n"
        "- 'Heavy, but essential viewing.' (4/5)"
    ),
}


class FakeMCPClientManager:
    """Drop-in stand-in for `agents.client.MCPClientManager`.

    Records every `execute_tool` call (name + the exact args actually
    sent) so tests can assert on what was ACTUALLY sent to the tool -
    which is exactly what the original bug report depended on: the
    literal placeholder string `"<selected_action_movie_title>"` was
    being sent as a real argument, producing "No movie found matching
    '<selected_action_movie_title>'.".
    """

    def __init__(self, tools: Optional[List[Dict[str, Any]]] = None):
        self.tools = tools if tools is not None else MOVIE_TOOLS
        self.calls: List[Dict[str, Any]] = []
        self.connected_servers: List[str] = []
        self.closed = False

    async def connect_stdio_server(self, server_name, command, args, env=None) -> bool:
        self.connected_servers.append(server_name)
        return True

    async def list_tools(self) -> List[Dict[str, Any]]:
        return self.tools

    async def execute_tool(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        # Deep-ish copy so later in-place mutation of the caller's dict
        # can't retroactively change what we recorded as "actually sent".
        self.calls.append({"tool_name": tool_name, "arguments": dict(arguments)})

        if tool_name == "get_movies_by_genre":
            return ACTION_MOVIES_RESULT

        if tool_name == "get_movie_comments":
            title = arguments.get("title")
            if title in MOVIE_COMMENTS:
                return MOVIE_COMMENTS[title]
            # Real behaviour of the movie MCP server when nothing matches -
            # this is the literal string the bug report showed in the
            # frontend when the placeholder was sent verbatim.
            return f"No movie found matching '{title}'."

        raise ValueError(f"FakeMCPClientManager: unknown tool '{tool_name}'")

    async def close_all(self):
        self.closed = True


# ---------------------------------------------------------------------------
# Fake LLM layer
# ---------------------------------------------------------------------------


class ScriptedLLM:
    """Stand-in for the object `agents.llm.get_llm(...)` normally returns.

    Supports the same fluent chain the real code uses -
    `.with_structured_output(Schema).with_config(tags=[...])` (planner,
    argument resolver) or just `.with_config(tags=[...])` (responder) -
    and resolves `.ainvoke(messages)` calls against a queue of canned
    responses, consumed strictly in the order the real graph makes LLM
    calls (planner -> [resolver, once per step needing it] -> responder).

    Pass real pydantic model instances (`TaskPlan`, `ResolvedArguments`)
    for structured-output calls, and a real `AIMessage` for the responder
    call, matching exactly what LangChain would hand back in each case.
    """

    def __init__(self, responses: List[Any]):
        self._responses = list(responses)
        self.calls: List[List[Any]] = []
        # Read via getattr(llm, "model_name", ...) in agent.py's token
        # logging - must exist so that code path doesn't blow up.
        self.model_name = "fake-test-model"

    def with_structured_output(self, schema, **kwargs):
        return self

    def with_config(self, **kwargs):
        return self

    async def ainvoke(self, messages):
        self.calls.append(messages)
        if not self._responses:
            raise AssertionError(
                "ScriptedLLM.ainvoke() was called more times than canned "
                "responses were provided. Either the code under test made "
                "an extra/unexpected LLM call, or this test needs one more "
                "scripted response."
            )
        return self._responses.pop(0)

    def make_factory(self):
        """Return a `get_llm`-compatible callable bound to this instance,
        so every `get_llm(...)` call across a whole graph run shares one
        response queue (`monkeypatch.setattr(agent_module, "get_llm",
        scripted.make_factory())`)."""

        def _factory(*args, **kwargs):
            return self

        return _factory
