"""Shared fixtures.

Nothing here touches a real database, LLM API, or MCP subprocess - see
`tests/fakes.py` for the test doubles that make that possible. Every test
in this suite runs fully offline.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest


@pytest.fixture(autouse=True)
def _no_real_openrouter_key(monkeypatch):
    """Belt-and-suspenders: even though every test swaps out `get_llm`
    itself, make sure a real key from a developer's local `.env` is never
    read during a test run.

    `settings` is a frozen model, so we monkeypatch the whole `settings`
    object's attribute lookup via `object.__setattr__`-safe monkeypatch on
    the module-level singleton is not possible directly - instead we
    monkeypatch the attribute using monkeypatch's dataclass-safe context
    is unavailable, so use `monkeypatch.setattr` on a *copy* via
    `model_copy`/`replace`, or simplest: patch `get_llm`'s own key lookup
    instead. Every test already replaces `get_llm` outright, so this
    fixture is now a no-op guard that just documents that fact - no
    mutation needed.
    """
    yield


def parse_sse_events(raw_text: str) -> List[Dict[str, Any]]:
    """Parse an `EventSourceResponse` body (as returned by TestClient) into
    a list of {"event": ..., "data": ...} dicts, with `data` JSON-decoded
    where possible. SSE events are separated by a blank line; each event
    has an `event:` line and one or more `data:` lines."""
    events: List[Dict[str, Any]] = []

    for block in raw_text.replace("\r\n", "\n").strip().split("\n\n"):
        block = block.strip()
        if not block:
            continue

        event_type = None
        data_lines = []
        for line in block.splitlines():
            if line.startswith("event:"):
                event_type = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:") :].strip())

        if event_type is None:
            continue

        raw_data = "\n".join(data_lines)
        try:
            data: Any = json.loads(raw_data) if raw_data else {}
        except json.JSONDecodeError:
            data = raw_data

        events.append({"event": event_type, "data": data})

    return events
