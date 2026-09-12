"""Unit tests for `agents/memory.py` (long-term memory: mem0 + Qdrant).

Fully offline, per this suite's established pattern (see `tests/conftest.py`
docstring) - no real mem0/Qdrant/OpenAI call. Two things are exercised:
  1. Graceful no-op behavior when long-term memory isn't configured
     (OPENAI_API_KEY unset) - every public function must degrade to a safe
     default (`[]`, `False`, `None`) rather than raise.
  2. The exact call shape each function makes against a mem0 `Memory`
     instance (via a fake standing in for it), since agents/memory.py's
     write path (top-level `user_id`/`run_id`/`metadata` kwargs) and read
     path (`filters` dict + `top_k`) intentionally differ - see the module
     docstring's note on mem0's `add`/`search`/`get_all` signatures.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from mcp_orchestration.agents import memory as memory_module


def _settings_without_openai_key():
    # `settings` is a frozen dataclass - can't monkeypatch an attribute on
    # the shared instance directly (raises FrozenInstanceError). Swap the
    # whole `settings` name in this module for a copy instead, via
    # dataclasses.replace (which works fine on frozen dataclasses since it
    # builds a new instance rather than mutating the existing one).
    #
    # Clears BOTH openai_api_key and openrouter_api: get_memory() falls
    # back to OpenRouter when there's no OpenAI key (see its docstring), so
    # "not configured" means neither credential is present - clearing only
    # openai_api_key would hit the fallback path and try a real network
    # call to whatever OPENROUTER_API happens to be set to locally.
    return dataclasses.replace(
        memory_module.settings, openai_api_key=None, openrouter_api=None
    )


@pytest.fixture(autouse=True)
def _reset_memory_singleton(monkeypatch):
    """agents/memory.py caches its mem0 Memory instance at module level -
    reset that cache before/after every test so tests don't leak state."""
    monkeypatch.setattr(memory_module, "_memory", None)
    monkeypatch.setattr(memory_module, "_memory_init_attempted", False)
    memory_module._episode_checked_threads.clear()
    yield
    memory_module._episode_checked_threads.clear()


class FakeMem0Memory:
    """Stands in for mem0.Memory, recording every call it receives."""

    def __init__(self):
        self.add_calls: list[dict] = []
        self.search_calls: list[dict] = []
        self.get_all_calls: list[dict] = []
        self.delete_calls: list[str] = []
        self.delete_all_calls: list[dict] = []
        self.search_return: Any = []
        self.get_all_return: Any = []

    def add(self, messages, **kwargs):
        self.add_calls.append({"messages": messages, **kwargs})

    def search(self, query, **kwargs):
        self.search_calls.append({"query": query, **kwargs})
        return self.search_return

    def get_all(self, **kwargs):
        self.get_all_calls.append(kwargs)
        return self.get_all_return

    def delete(self, memory_id):
        self.delete_calls.append(memory_id)

    def delete_all(self, **kwargs):
        self.delete_all_calls.append(kwargs)


def _install_fake_memory(monkeypatch) -> FakeMem0Memory:
    """Bypass get_memory()'s real init (which needs OPENAI_API_KEY and a
    real mem0/Qdrant) by seeding the module-level cache directly."""
    fake = FakeMem0Memory()
    monkeypatch.setattr(memory_module, "_memory", fake)
    monkeypatch.setattr(memory_module, "_memory_init_attempted", True)
    return fake


# --- Not configured: every function degrades safely ------------------------


class TestNotConfigured:
    def test_get_memory_returns_none_without_openai_key(self, monkeypatch):
        monkeypatch.setattr(memory_module, "settings", _settings_without_openai_key())
        assert memory_module.get_memory() is None

    def test_remember_is_a_noop(self, monkeypatch):
        monkeypatch.setattr(memory_module, "settings", _settings_without_openai_key())
        # Must not raise.
        memory_module.remember("user-1", [{"role": "user", "content": "hi"}])

    def test_recall_returns_empty_list(self, monkeypatch):
        monkeypatch.setattr(memory_module, "settings", _settings_without_openai_key())
        assert memory_module.recall("user-1", "what do you know about me?") == []

    def test_list_all_returns_empty_list(self, monkeypatch):
        monkeypatch.setattr(memory_module, "settings", _settings_without_openai_key())
        assert memory_module.list_all("user-1") == []

    def test_forget_returns_false(self, monkeypatch):
        monkeypatch.setattr(memory_module, "settings", _settings_without_openai_key())
        assert memory_module.forget("user-1", "mem-123") is False

    def test_forget_all_does_not_raise(self, monkeypatch):
        monkeypatch.setattr(memory_module, "settings", _settings_without_openai_key())
        memory_module.forget_all("user-1")


# --- Configured: call shape against the (fake) mem0 Memory -----------------


class TestCallShape:
    def test_remember_passes_user_id_run_id_metadata_top_level(self, monkeypatch):
        fake = _install_fake_memory(monkeypatch)
        memory_module.remember(
            "user-1",
            [{"role": "user", "content": "I'm vegetarian"}],
            thread_id="thread-1",
            metadata={"kind": "episode"},
        )
        assert len(fake.add_calls) == 1
        call = fake.add_calls[0]
        assert call["user_id"] == "user-1"
        assert call["run_id"] == "thread-1"
        assert call["metadata"] == {"kind": "episode"}

    def test_recall_passes_user_id_via_filters_and_top_k(self, monkeypatch):
        fake = _install_fake_memory(monkeypatch)
        fake.search_return = {"results": [{"id": "m1", "memory": "vegetarian"}]}

        result = memory_module.recall("user-1", "what do I eat?", top_k=5)

        assert len(fake.search_calls) == 1
        call = fake.search_calls[0]
        assert call["filters"] == {"user_id": "user-1"}
        assert call["top_k"] == 5
        assert result == [{"id": "m1", "memory": "vegetarian"}]

    def test_recall_with_thread_id_and_kind_scopes_filters(self, monkeypatch):
        fake = _install_fake_memory(monkeypatch)
        memory_module.recall("user-1", "query", thread_id="thread-1", kind="episode")

        call = fake.search_calls[0]
        assert call["filters"] == {
            "user_id": "user-1",
            "run_id": "thread-1",
            "metadata.kind": "episode",
        }

    def test_recall_handles_plain_list_return(self, monkeypatch):
        fake = _install_fake_memory(monkeypatch)
        fake.search_return = [{"id": "m1"}]
        assert memory_module.recall("user-1", "q") == [{"id": "m1"}]

    def test_recall_swallows_search_errors(self, monkeypatch):
        fake = _install_fake_memory(monkeypatch)

        def _boom(*a, **k):
            raise RuntimeError("qdrant unreachable")

        fake.search = _boom
        assert memory_module.recall("user-1", "q") == []

    def test_list_all_uses_user_id_filter(self, monkeypatch):
        fake = _install_fake_memory(monkeypatch)
        fake.get_all_return = {"results": [{"id": "m1"}, {"id": "m2"}]}

        result = memory_module.list_all("user-1")

        assert fake.get_all_calls[0] == {"filters": {"user_id": "user-1"}}
        assert result == [{"id": "m1"}, {"id": "m2"}]

    def test_forget_deletes_only_owned_memory(self, monkeypatch):
        fake = _install_fake_memory(monkeypatch)
        fake.get_all_return = [{"id": "m1"}, {"id": "m2"}]

        assert memory_module.forget("user-1", "m1") is True
        assert fake.delete_calls == ["m1"]

    def test_forget_refuses_memory_not_owned_by_user(self, monkeypatch):
        fake = _install_fake_memory(monkeypatch)
        fake.get_all_return = [{"id": "m1"}]

        assert memory_module.forget("user-1", "someone-elses-memory") is False
        assert fake.delete_calls == []

    def test_forget_all_passes_user_id(self, monkeypatch):
        fake = _install_fake_memory(monkeypatch)
        memory_module.forget_all("user-1")
        assert fake.delete_all_calls == [{"user_id": "user-1"}]

    def test_has_episode_true_when_entries_found(self, monkeypatch):
        fake = _install_fake_memory(monkeypatch)
        fake.get_all_return = [{"id": "m1"}]
        assert memory_module.has_episode("user-1", "thread-1") is True
        assert fake.get_all_calls[0] == {
            "filters": {
                "user_id": "user-1",
                "run_id": "thread-1",
                "metadata.kind": "episode",
            }
        }

    def test_has_episode_false_when_no_entries(self, monkeypatch):
        fake = _install_fake_memory(monkeypatch)
        fake.get_all_return = []
        assert memory_module.has_episode("user-1", "thread-1") is False
