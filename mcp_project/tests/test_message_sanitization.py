"""Unit test for `agents/agent.py:_sanitize_messages_for_llm`.

Regression test for a real production bug: `tool_executor_node` appends a
`ToolMessage` straight into `state["messages"]` with no preceding
`AIMessage.tool_calls` entry (this app's planner/executor is a hand-rolled
step dispatcher, not OpenAI's native tool-calling protocol - there is no
such entry to add). OpenRouter-proxied models tolerated that; calling
OpenAI directly does not - it 400s with "messages with role 'tool' must be
a response to a preceding message with 'tool_calls'" the moment a
ToolMessage shows up in history (i.e. turn 2+ of any thread that executed
a tool). `_sanitize_messages_for_llm` is what fixes that, by replacing each
ToolMessage with a SystemMessage carrying the same content before any
`ainvoke()` call.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from mcp_orchestration.agents.agent import _sanitize_messages_for_llm


def test_tool_messages_are_converted_to_system_messages():
    messages = [
        HumanMessage(content="what's the weather?"),
        AIMessage(content="Let me check."),
        ToolMessage(content="Sunny, 72F", tool_call_id="step_1"),
    ]

    sanitized = _sanitize_messages_for_llm(messages)

    assert not any(isinstance(m, ToolMessage) for m in sanitized)
    assert isinstance(sanitized[2], SystemMessage)
    assert "Sunny, 72F" in sanitized[2].content


def test_non_tool_messages_pass_through_unchanged():
    human = HumanMessage(content="hi")
    ai = AIMessage(content="hello")
    system = SystemMessage(content="be helpful")

    sanitized = _sanitize_messages_for_llm([human, ai, system])

    assert sanitized == [human, ai, system]


def test_empty_list_returns_empty_list():
    assert _sanitize_messages_for_llm([]) == []


def test_preserves_message_order_and_count():
    messages = [
        HumanMessage(content="1"),
        ToolMessage(content="2", tool_call_id="a"),
        AIMessage(content="3"),
        ToolMessage(content="4", tool_call_id="b"),
    ]

    sanitized = _sanitize_messages_for_llm(messages)

    assert len(sanitized) == 4
    assert [m.content if not isinstance(m, ToolMessage) else None for m in sanitized][
        0
    ] == "1"
    assert sanitized[1].content == "[Tool result] 2"
    assert sanitized[3].content == "[Tool result] 4"
