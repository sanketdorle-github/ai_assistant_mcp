"""Eval dataset: cases against the bundled demo MCP server
(`mcp/demo_server.py` - real, deterministic, no external creds needed).

Each case's `outputs` becomes the LangSmith example's reference output,
handed to evaluators as `reference_outputs` (see `evals/run_evals.py`).
Covers the exact failure classes this app has already hit in production:

- `calculator_basic_arithmetic`: tool-choice correctness (does the planner
  pick `calculator`, not invent a tool or answer without one).
- `remember_then_recall`: placeholder/argument-resolution correctness
  within a single turn's multi-step plan - `_resolve_step_arguments`
  (agents/agent.py) exists specifically because a step can depend on an
  earlier step's real result, not a value known at plan time. Same class
  of bug as `tests/test_agent_graph_movie_scenario.py`, exercised here
  against the real model instead of `ScriptedLLM`.
- `plain_greeting`: regression guard for the structured-output crash
  fixed earlier this session (`with_structured_output(..., method="json_schema")`
  crashing on a reply with nothing to plan) - expects no tool call and no
  crash.

Gmail-tool scenarios aren't covered yet - would need a connected test
account; a natural v2 expansion once that's available.
"""

CASES: list[dict] = [
    {
        "inputs": {"message": "What is 15 multiplied by 23?"},
        "outputs": {
            "expected_tools": ["calculator"],
            "requires_approval_tools": [],
        },
    },
    {
        "inputs": {
            "message": (
                "Remember that my favorite color is blue, then tell me "
                "what color I just asked you to remember."
            )
        },
        "outputs": {
            "expected_tools": ["remember_note", "recall_notes"],
            "requires_approval_tools": [],
        },
    },
    {
        "inputs": {"message": "Good morning! How are you today?"},
        "outputs": {
            "expected_tools": [],
            "requires_approval_tools": [],
        },
    },
]

DATASET_NAME = "mcp-orchestration-core-scenarios"
DATASET_DESCRIPTION = (
    "Core agent-behavior scenarios for mcp_orchestration - tool-choice, "
    "argument-resolution, and structured-output-crash regressions, run "
    "against the real LLM + bundled demo MCP server. See CLAUDE.md's "
    "Evals section."
)
