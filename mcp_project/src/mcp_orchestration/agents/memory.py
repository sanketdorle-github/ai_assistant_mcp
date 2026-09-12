"""Long-term (cross-thread) memory, via mem0 (https://github.com/mem0ai/mem0)
backed by Qdrant as the vector store.

This is a different concern from `services/memory_service.py`'s
`MongoCheckpointSaver`, which is *session* memory - the message/plan state
for one `thread_id`, replayed on resume. Nothing there carries over to a
different thread with the same user. This module is what does: durable
facts about a user ("semantic" memory) and summaries of past conversations
("episodic" memory), both retrieved by relevance to the current message and
injected into the planner's system prompt (see `agents/agent.py`).

mem0's own `MemoryType` enum (`semantic_memory`/`episodic_memory`/
`procedural_memory`) is NOT used here - only `procedural_memory` is
actually wired up in the installed SDK version; passing either of the other
two as `memory_type` raises a validation error. Instead:
  - semantic memory = mem0's default `add()`/`search()`, scoped by `user_id`
  - episodic memory = the same `add()`/`search()`, additionally scoped by
    `run_id` (mem0's session/run dimension, set to the thread_id) and
    tagged via `metadata={"kind": "episode", ...}` so it can be told apart
    from plain facts at retrieval time - built on mem0's real, working
    primitives rather than its unfinished type tag.

Configuration mirrors `agents/llm.py`'s `get_llm()`: the LLM mem0 uses for
its own extraction step points at OpenRouter (same `OPENROUTER_API` key as
the rest of the app); the embedder calls OpenAI directly, since mem0's OSS
embedder doesn't support OpenRouter or Anthropic/Voyage. Requires
`OPENAI_API_KEY` - without it, long-term memory is disabled (logged once,
not a hard failure) the same way `mcp/config.py` disables Gmail without
OAuth credentials configured.
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from mcp_orchestration.core.config import settings

logger = logging.getLogger("mcp_orchestration.agents.memory")

_memory: Optional[Any] = None
_memory_init_attempted = False


def get_memory():
    """Return the shared mem0 `Memory` instance, or `None` if long-term
    memory isn't configured (no usable credential) or failed to initialize
    (e.g. Qdrant unreachable). Never raises - every caller in this module
    treats `None` as "long-term memory unavailable this run" and degrades
    gracefully, the same way a DB outage degrades `agents/llm.py`'s
    token-usage logging rather than failing the request.

    Embedder credential: prefers `OPENAI_API_KEY` (real OpenAI - known to
    work, since mem0's OSS embedder is built against OpenAI's actual
    `/embeddings` endpoint) if set; otherwise falls back to routing through
    OpenRouter with `OPENROUTER_API`, same as the `llm` block below.
    **Caveat, unverified**: OpenRouter has historically only proxied
    `/chat/completions`, not `/embeddings` - if this falls back to
    OpenRouter and you see an "invalid model ID" or 404-ish error coming
    from the embedder specifically (not the planner's own LLM call), that's
    almost certainly why, and `OPENAI_API_KEY` (real OpenAI) is the fix.
    """
    global _memory, _memory_init_attempted

    if _memory is not None:
        return _memory
    if _memory_init_attempted:
        return None
    _memory_init_attempted = True

    use_openai_for_embeddings = bool(settings.openai_api_key)
    if not settings.openai_api_key and not settings.openrouter_api:
        logger.info(
            "Neither OPENAI_API_KEY nor OPENROUTER_API set; long-term memory (mem0) disabled."
        )
        return None

    embedder_config = (
        {"api_key": settings.openai_api_key, "model": settings.openai_embed_model}
        if use_openai_for_embeddings
        else {
            "api_key": settings.openrouter_api,
            "openai_base_url": "https://openrouter.ai/api/v1",
            "model": settings.openai_embed_model,
        }
    )

    # mem0's own extraction LLM (decides ADD/UPDATE/DELETE/NOOP per fact) -
    # same OpenAI-preferred, OpenRouter-fallback pattern as the embedder
    # above, and the same model agents/llm.py's get_llm() uses when calling
    # OpenAI directly.
    llm_config = (
        {"api_key": settings.openai_api_key, "model": settings.openai_chat_model}
        if settings.openai_api_key
        else {
            "api_key": settings.openrouter_api,
            "openai_base_url": "https://openrouter.ai/api/v1",
            "model": os.getenv("OPENROUTER_DEFAULT_MODEL", "google/gemini-2-flash"),
        }
    )

    try:
        from mem0 import Memory

        _memory = Memory.from_config(
            {
                "vector_store": {
                    "provider": "qdrant",
                    "config": {
                        "host": settings.qdrant_host,
                        "port": settings.qdrant_port,
                    },
                },
                "llm": {
                    "provider": "openai",
                    "config": llm_config,
                },
                "embedder": {
                    "provider": "openai",
                    "config": embedder_config,
                },
            }
        )
        logger.info(
            "✅ Long-term memory (mem0 + Qdrant) initialized - embedder via %s",
            "OpenAI" if use_openai_for_embeddings else "OpenRouter (unverified for embeddings)",
        )
    except Exception as e:
        logger.error(f"Failed to initialize long-term memory (mem0): {e}", exc_info=True)
        _memory = None

    return _memory


def remember(
    user_id: str,
    messages: list[dict],
    *,
    thread_id: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> None:
    """Write to long-term memory. Best-effort: swallows and logs any
    failure rather than raising, since a memory-write failure must never
    break a chat response (same tolerance as token-usage logging)."""
    memory = get_memory()
    if memory is None:
        return
    try:
        memory.add(messages, user_id=user_id, run_id=thread_id, metadata=metadata)
    except Exception as e:
        logger.warning(f"Long-term memory write failed for user '{user_id}': {e}")


def recall(
    user_id: str,
    query: str,
    *,
    thread_id: Optional[str] = None,
    top_k: int = 5,
    kind: Optional[str] = None,
) -> list[dict]:
    """Read from long-term memory: the `top_k` entries most relevant to
    `query` for this user. `kind` (e.g. "episode") filters to memories
    written with that `metadata.kind` - omit it for plain facts. Returns
    `[]` on any failure or if long-term memory isn't configured - callers
    treat an empty result as "nothing to add to the prompt," not an error.
    """
    memory = get_memory()
    if memory is None:
        return []

    filters: dict = {"user_id": user_id}
    if thread_id:
        filters["run_id"] = thread_id
    if kind:
        # mem0's Qdrant filter builder wants a flat dot-path key
        # ("metadata.kind"), not a nested {"metadata": {"kind": ...}}
        # dict - the latter raises "Unsupported filter operator(s)".
        filters["metadata.kind"] = kind

    try:
        result = memory.search(query=query, filters=filters, top_k=top_k)
        # mem0's search() returns either a list or {"results": [...]}
        # depending on version/config - normalize to a plain list.
        return result.get("results", result) if isinstance(result, dict) else result
    except Exception as e:
        logger.warning(f"Long-term memory read failed for user '{user_id}': {e}")
        return []


def list_all(user_id: str) -> list[dict]:
    """All long-term memories for a user (facts + episodes), for `GET /memory`."""
    memory = get_memory()
    if memory is None:
        return []
    try:
        result = memory.get_all(filters={"user_id": user_id})
        return result.get("results", result) if isinstance(result, dict) else result
    except Exception as e:
        logger.warning(f"Long-term memory list failed for user '{user_id}': {e}")
        return []


def forget(user_id: str, memory_id: str) -> bool:
    """Delete one memory, after confirming it belongs to `user_id` - mem0's
    own `delete()` takes only a memory_id, with no ownership check built
    in. Returns False (not an error) if it doesn't belong to this user or
    doesn't exist, so callers can turn that into a 404."""
    memory = get_memory()
    if memory is None:
        return False
    if not any(m.get("id") == memory_id for m in list_all(user_id)):
        return False
    try:
        memory.delete(memory_id)
        return True
    except Exception as e:
        logger.warning(f"Long-term memory delete failed for user '{user_id}': {e}")
        return False


def forget_all(user_id: str) -> None:
    """Delete every long-term memory for a user (account deletion / GDPR,
    or a user-initiated 'clear what you remember about me')."""
    memory = get_memory()
    if memory is None:
        return
    try:
        memory.delete_all(user_id=user_id)
    except Exception as e:
        logger.warning(f"Long-term memory delete_all failed for user '{user_id}': {e}")


def has_episode(user_id: str, thread_id: str) -> bool:
    """Whether an episode memory already exists for this thread - so
    `maybe_summarize_idle_thread` doesn't re-summarize a thread it (or an
    earlier process) already handled."""
    memory = get_memory()
    if memory is None:
        return False
    try:
        result = memory.get_all(
            filters={"user_id": user_id, "run_id": thread_id, "metadata.kind": "episode"}
        )
        entries = result.get("results", result) if isinstance(result, dict) else result
        return bool(entries)
    except Exception as e:
        logger.warning(f"Episode-existence check failed for thread '{thread_id}': {e}")
        return False


# Threads already checked this process lifetime, so a busy sidebar doesn't
# re-check (and re-hit mem0/Qdrant for) the same idle thread on every poll.
# Process-local only - restarting the app just means those threads get
# re-checked once, which `has_episode` then short-circuits if they were
# already summarized in an earlier run.
_episode_checked_threads: set[str] = set()


async def maybe_summarize_idle_thread(
    user_id: str,
    thread_id: str,
    last_activity: Optional[datetime],
    *,
    idle_minutes: int = 30,
) -> None:
    """Best-effort episodic-memory write: if a thread has gone idle and
    doesn't have an episode summary yet, summarize it and store the result.

    Fire-and-forget from callers (see api/routes/chat.py's `list_threads`
    endpoint) - this app has no dedicated "thread ended" signal, so this
    piggybacks on whenever the thread list is fetched (the sidebar) as the
    simplest v1 trigger, per the long-term-memory plan.
    """
    if get_memory() is None:
        return
    if thread_id in _episode_checked_threads:
        return
    if last_activity is None:
        return

    # ObjectId.generation_time (the source of last_activity - see
    # list_threads_for_user) is always timezone-aware UTC.
    if datetime.now(timezone.utc) - last_activity < timedelta(minutes=idle_minutes):
        return

    _episode_checked_threads.add(thread_id)

    try:
        if await asyncio.to_thread(has_episode, user_id, thread_id):
            return

        from mcp_orchestration.agents.llm import get_llm
        from mcp_orchestration.services.memory_service import get_thread_messages

        transcript = await asyncio.to_thread(get_thread_messages, thread_id)
        if not transcript:
            return

        transcript_text = "\n".join(
            f"{m['role']}: {m['content']}" for m in transcript if m.get("content")
        )
        llm = get_llm(temperature=0)
        summary_response = await llm.ainvoke(
            "Summarize the following conversation in 2-3 sentences, focused on "
            "what would be useful to remember about the user and what was "
            "discussed/accomplished. If there's nothing worth remembering "
            "(e.g. small talk only), respond with exactly 'NOTHING'.\n\n"
            f"{transcript_text}"
        )
        summary = (summary_response.content or "").strip()
        if not summary or summary.upper() == "NOTHING":
            return

        remember(
            user_id,
            [{"role": "system", "content": summary}],
            thread_id=thread_id,
            metadata={"kind": "episode", "thread_id": thread_id},
        )
    except Exception as e:
        logger.warning(f"Episodic summarization failed for thread '{thread_id}': {e}")
