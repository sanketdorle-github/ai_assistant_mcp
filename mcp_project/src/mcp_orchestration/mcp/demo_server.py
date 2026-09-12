"""A tiny demo MCP server, run as a stdio subprocess.

This exists so the agent graph has at least one real MCP connector to call
during development, without depending on any external service or API key.
Swap this out (or add more servers) via `mcp/config.py` once you have real
connectors (filesystem, search, a database, etc).

Run directly for a smoke test of the tool implementations:
    python -m mcp_orchestration.mcp.demo_server --selftest

Run as an MCP stdio server (what the agent actually does):
    python -m mcp_orchestration.mcp.demo_server
"""

import sys
from datetime import datetime, timezone

# This project's `mcp` dependency (>=2.0.0) no longer bundles the FastMCP
# server framework under `mcp.server.fastmcp` - that now lives in the
# separately-versioned standalone `fastmcp` package, which is also pinned in
# pyproject.toml (fastmcp>=2.14.1). Import from there instead.
from fastmcp import FastMCP

mcp = FastMCP("demo-tools")

# In-memory scratch store so `remember_note` / `recall_notes` have somewhere
# to write. Resets every time the server subprocess restarts - swap for a
# real collection if you want notes to persist across chat turns.
_NOTES: list[str] = []


@mcp.tool()
def get_current_time() -> str:
    """Return the current UTC date and time in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


@mcp.tool()
def calculator(operation: str, a: float, b: float) -> str:
    """Perform a basic arithmetic operation on two numbers.

    operation: one of "add", "subtract", "multiply", "divide"
    """
    ops = {
        "add": lambda x, y: x + y,
        "subtract": lambda x, y: x - y,
        "multiply": lambda x, y: x * y,
        "divide": lambda x, y: x / y if y != 0 else float("nan"),
    }
    fn = ops.get(operation.lower().strip())
    if fn is None:
        return f"Unsupported operation '{operation}'. Use one of {list(ops)}."
    return str(fn(a, b))


@mcp.tool()
def remember_note(note: str) -> str:
    """Store a short text note for later recall in this server session."""
    _NOTES.append(note)
    return f"Stored note #{len(_NOTES)}."


@mcp.tool()
def recall_notes() -> str:
    """List every note stored so far in this server session."""
    if not _NOTES:
        return "No notes stored yet."
    return "\n".join(f"{i + 1}. {n}" for i, n in enumerate(_NOTES))


def _selftest() -> None:
    """Exercise the tool functions directly, bypassing MCP transport."""
    print("get_current_time ->", get_current_time())
    print("calculator(add, 2, 3) ->", calculator("add", 2, 3))
    print("remember_note ->", remember_note("hello from selftest"))
    print("recall_notes ->", recall_notes())
    print("OK: demo_server tools run without error.")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        mcp.run(transport="stdio")
