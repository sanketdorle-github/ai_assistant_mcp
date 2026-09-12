# CLAUDE.md — AI Personal Assistant Backend

This file documents the project for whoever (human or Claude) picks it up next.
Read this before touching `agents/` or `api/routes/chat.py`.

## What this is

A FastAPI backend for a personal AI assistant: auth, chat with streaming
responses, a LangGraph plan → act → respond agent, structured LLM output,
MongoDB-backed session memory plus mem0+Qdrant-backed long-term
(cross-thread) memory, and an MCP client that can call tools from any MCP
server (a demo one is bundled, plus a real Gmail connector).

Run it:
```
uv sync
docker compose up -d          # starts MongoDB + Qdrant
cp .env.example .env          # fill in OPENROUTER_API and SECRET_KEY at minimum;
                               # OPENAI_API_KEY too if you want long-term memory
uv run uvicorn mcp_orchestration.main:app --reload
```
Docs at `/docs`. Everything except `/`, `/auth/register`, `/auth/login`,
`/auth/token` requires a Bearer JWT from login.

## Architecture

```
api/routes/          FastAPI routers (HTTP boundary only, no business logic)
  auth.py              register / login / token / me, get_current_user dependency
  users.py             list users (protected)
  chat.py              stream, resume, list threads, fetch thread messages
core/                 Cross-cutting: config, db connection, JWT, request logging
  config.py             Settings dataclass, reads env vars via python-dotenv
  database.py            Database class (connect/close/collection) + get_collection()
  security.py            bcrypt hashing + JWT encode/decode
  middleware.py           RequestLoggingMiddleware (logs method/path/status/duration,
                        adds X-Process-Time-Ms header)
  logging_config.py       configure_logging(), must run before any other project
                        import (see main.py comment)
schemas/user.py       Pydantic I/O models for the user-facing API
repositories/users.py MongoDB persistence for users, isolated from services
services/
  user_service.py      user business rules (hashing, serialization)
  memory_service.py     MongoCheckpointSaver — LangGraph state persisted in Mongo,
                        plus list_threads_for_user / get_thread_messages helpers
agents/
  llm.py                get_llm() -> ChatOpenAI, OpenAI-direct if OPENAI_API_KEY
                        is set (falls back to OpenRouter otherwise); token
                        estimation + usage logging
  memory.py              Long-term (cross-thread) memory - mem0 + Qdrant,
                        see the dedicated section below
  client.py             MCPClientManager: connects to MCP servers over stdio,
                        lists/calls their tools, can convert them to LangChain
                        StructuredTools
  agent.py              The LangGraph graph itself (see below)
auth/
  gmail_oauth.py          FastAPI router (`/auth/gmail/login`, `/callback`, `/status`)
                        that runs the real Google OAuth flow and writes the resulting
                        access/refresh token to a local JSON file (see Gmail section)
mcp/
  demo_server.py         A tiny bundled MCP server (time, calculator, notes) so
                        the graph always has a real tool to call, no API keys needed
  gmail_mcp_server.py     Custom Gmail MCP server (stdio) — search/read/draft/send
                        via the standard Gmail API, using the token gmail_oauth.py wrote
  config.py              Which MCP servers to connect to (env-driven; always includes
                        the demo server, plus Gmail when OAuth credentials are set)
```

`llm/` and `models/` exist as empty package directories — nothing lives there yet, don't be
surprised they show up in a file listing.

## The agent graph (`agents/agent.py`)

`planner → tool_executor (loops) → responder`, compiled with
`interrupt_before=["tool_executor_gated"]` for human-in-the-loop approval
(see the router bullet below — `tool_executor_gated` and `tool_executor`
run the same node function under two names, and only the gated one is
listed in `interrupt_before`).

- **planner**: calls the LLM with `.with_structured_output(TaskPlan)` — this
  is the structured-output piece. `TaskPlan` is a Pydantic model with a
  `rationale` and a list of `TaskStep`s (id, description, tool_name,
  tool_arguments, requires_approval, status, result). The planner sees the
  live tool list from `MCPClientManager.list_tools()`, so it can only plan
  calls to tools that are actually connected.
- **tool_executor**: runs the current step's tool via MCP, advances
  `current_step_index`, loops back through the router until the plan is
  exhausted.
- **responder**: synthesizes the final answer from the conversation +
  executed plan, with `streaming` tagged `responder_llm` so the API layer
  knows which token stream to forward to the client.
- **router (`route_step`)**: decides `responder` vs one of two tool-executor
  node names for the same underlying function — `tool_executor` (runs
  immediately) or `tool_executor_gated` (the only node listed in
  `interrupt_before`, so the graph halts before it). It picks the gated
  name only when the current step's own `requires_approval` is true and
  its `status` is still `"pending"`; this is what makes per-step approval
  actually per-step instead of "every tool call always pauses" (see
  "What changed this session" below — this used to be dead code).

State (`AgentState`) is a `TypedDict`: messages (auto-merged via
`add_messages`), plan, current_step_index, user_id, session_id, tokens.

**Placeholder / missing-argument resolution**: the planner drafts every
step's `tool_arguments` in one upfront LLM call, before any tool has run —
so a step that depends on an earlier step's output (e.g. "get reviews for
the movie we just picked") gets a placeholder like
`"<selected_action_movie_title>"` instead of a real value. `tool_executor_node`
detects this (`_contains_placeholder`, plus a missing-required-argument
check against the tool's schema) and calls `_resolve_step_arguments` to
re-derive real values from the prior tool results already in
`state["messages"]`, before executing. If the resolver still leaves a
placeholder behind, execution is refused rather than calling the tool with
literal placeholder text. See `tests/test_argument_resolution.py` and
`tests/test_agent_graph_movie_scenario.py`.

**Structured-output method matters**: both `.with_structured_output(...)` calls
(planner's `TaskPlan`, the argument resolver's `ResolvedArguments`) pass
`method="function_calling"` explicitly. `ChatOpenAI`'s own default is
`method="json_schema"` (OpenAI's strict Structured Outputs API), which
routes through the OpenAI SDK's own strict JSON parser
(`openai.lib._parsing._completions`) instead of LangChain's normal,
recoverable output-parsing path. Models proxied through OpenRouter (e.g.
Gemini) don't reliably honor that strict mode — for a plain greeting with
nothing to plan, the model would sometimes reply with ordinary
conversational text instead of a JSON `TaskPlan`, which crashed the whole
run with a raw `pydantic_core.ValidationError` ("Invalid JSON: expected
value at line 1 column 1") instead of being handled gracefully.
`function_calling` uses tool-call argument extraction instead, which is far
more broadly supported across OpenRouter-proxied models. If you add a
third `with_structured_output` call anywhere, pass this explicitly too.

**Message-list sanitization for real OpenAI**: `_sanitize_messages_for_llm`
converts every `ToolMessage` in `state["messages"]` to a `SystemMessage`
with the same content before any `.ainvoke()` call (planner, argument
resolver, responder). This app's planner/executor is a hand-rolled
step-by-step dispatcher, not OpenAI's native tool-calling protocol —
`tool_executor_node` appends a `ToolMessage` straight into state with no
preceding `AIMessage.tool_calls` entry referencing it (there isn't one; a
`TaskStep` isn't a LangChain tool call). OpenRouter-proxied models
tolerated that; real OpenAI's Chat Completions API strictly validates it
and 400s with `"messages with role 'tool' must be a response to a
preceding message with 'tool_calls'"` the moment a `ToolMessage` shows up
in history without one — which happens on turn 2+ of any thread that
executed a tool, since that `ToolMessage` is still sitting there. See
`tests/test_message_sanitization.py`.

**Sensitive-tool approval is enforced, not just prompted**: `SENSITIVE_TOOL_NAMES`
(`send_email`, `write_file`, `delete_file`) is a hardcoded set in
`agent.py`. `planner_node` forces `requires_approval = True` on any step
using one of these tools regardless of what the planner LLM decided —
because relying on the model's own judgment let approval silently vary
run-to-run for `send_email`.

## Streaming + HITL (`api/routes/chat.py`)

`POST /chat/stream` runs `agent.astream_events(..., version="v2")` and turns
selected LangGraph events into SSE events:
- `plan` — emitted when planner or tool_executor finishes, carries the plan array
- `token` — individual response tokens from the `responder_llm`-tagged stream
- `interrupt` — emitted when the graph paused before a step that
  `requires_approval`; includes step id, tool name, and arguments
- `done` / `error` — terminal events

`POST /chat/resume` continues an interrupted thread: `approve=true` runs the
step as planned (optionally with `modified_arguments` swapped in first),
`approve=false` marks the step failed and lets the graph move on. Both
routes reconnect MCP servers fresh per request (`load_mcp_server_configs()`)
and disconnect them in a `finally` block.

`GET /chat/threads` and `GET /chat/threads/{id}/messages` are new this
session — they read straight from the checkpoint collection so a client can
show a thread list / reload history without keeping a separate transcript
table in sync with LangGraph's own state.

## Session memory (`services/memory_service.py`)

Not to be confused with **long-term memory** (next section) — this is
per-thread conversation state, not anything that carries over to a
different `thread_id`.

`MongoCheckpointSaver` implements LangGraph's `BaseCheckpointSaver` against
a `checkpoints` collection (one doc per checkpoint, `serde`-serialized) and
a `checkpoint_writes` collection (pending tool-call writes staged before an
interrupt — required for `interrupt_before` to actually survive a request
boundary). Both sync methods (`put`, `put_writes`, `get_tuple`, `list`) and
their async counterparts (`aput`, `aput_writes`, `aget_tuple`, `alist`) are
implemented; the async ones bridge to sync pymongo via `asyncio.to_thread`
since pymongo has no native async driver here.

Each checkpoint doc also stores `user_id` (pulled out of the graph state at
write time) purely so `list_threads_for_user` can filter without
deserializing every checkpoint.

**Known simplification**: `list()`'s `before` filter treats
`before.checkpoint_id` as an exact match rather than "checkpoints created
strictly before this one in the chain." Fine for the current use (nothing
calls `list()` yet outside LangGraph internals), but fix this before
building a "view checkpoint history" UI on top of it.

Separately, `agents/llm.py` logs every LLM call's estimated token usage to a
`token_usage` collection — best-effort, a DB outage there is logged and
swallowed rather than failing the chat request.

## Long-term memory (`agents/memory.py`)

Cross-thread memory — what the agent still knows about a user in a *brand
new* conversation, as opposed to session memory above (which disappears
the moment a thread ends, invisible to any other thread). Built on
[mem0](https://github.com/mem0ai/mem0) (extraction, dedup/update, and
semantic retrieval) backed by [Qdrant](https://qdrant.tech) as the vector
store, self-hosted via `docker-compose.yaml` alongside Mongo.

- **Semantic memory** (durable facts — "vegetarian," "works as a nurse"):
  mem0's default `add()`/`search()` scoped by `user_id`. mem0's own
  extraction pipeline (one LLM call) decides per-turn what's actually
  worth keeping and whether it's a new fact or an update to an existing
  one — most turns produce nothing, which is correct, not a bug.
- **Episodic memory** (summaries of past conversations): also mem0's
  `add()`/`search()`, additionally scoped by `run_id` (set to the
  `thread_id`) and tagged `metadata={"kind": "episode", ...}`.
  **Important**: mem0 defines a `MemoryType` enum with
  `semantic_memory`/`episodic_memory`/`procedural_memory`, but only
  `procedural_memory` is actually wired up in the installed SDK — passing
  either of the other two as `memory_type` raises a validation error, with
  no documented roadmap for finishing them. So this app does **not** use
  that enum; episodic memory here is built on mem0's real, working
  `run_id`/`metadata` primitives instead. No procedural memory is
  implemented in this app (explicitly out of scope).

**Write path**: fire-and-forget (`asyncio.create_task` wrapping
`asyncio.to_thread`, since mem0's `Memory` is sync) from
`agent.py`'s `responder_node` after each turn — never awaited, so a slow
or failed memory write can't add latency to the streamed response or fail
the chat request, same tolerance as `agents/llm.py`'s token-usage logging.
Episodic summaries are triggered differently — there's no "thread ended"
signal in this app, so `api/routes/chat.py`'s `GET /chat/threads` (the
sidebar's own polling) opportunistically checks each thread's
`last_activity` and fires `agents/memory.py:maybe_summarize_idle_thread`
for any that have gone idle (30 min) and don't have an episode yet. A
process-local `_episode_checked_threads` set avoids re-checking the same
thread every time the sidebar polls; `has_episode()` is the actual
source of truth (checked once per thread per process lifetime).

**Read path**: `agent.py`'s `planner_node`, alongside the MCP tool list —
`recall(user_id, latest_user_message, top_k=5)` for facts and
`recall(..., kind="episode")` for episodes, each in its own labeled block
in the system prompt so the model doesn't conflate a standing fact with a
specific past event. Retrieval is by relevance to the current message
(mem0's own vector search), not "load everything."

**Config**: `QDRANT_HOST`/`QDRANT_PORT` (Qdrant, self-hosted). The embedder
credential is `OPENAI_API_KEY` (real OpenAI, known to work — mem0's OSS
embedder is built against OpenAI's actual `/embeddings` endpoint) if set,
**falling back to `OPENROUTER_API`** (same key the rest of the app already
uses for chat completions) if not. That fallback is **unverified** —
OpenRouter has historically only proxied `/chat/completions`, not
`/embeddings` — see `get_memory()`'s docstring for the exact symptom
("invalid model ID" from the embedder specifically, not the planner's own
LLM call) if it turns out not to work; the fix is setting `OPENAI_API_KEY`.
`OPENROUTER_DEFAULT_MODEL` also matters here (and for the planner/responder
generally) — the code's own fallback default is not a confirmed-valid
OpenRouter slug, set it explicitly (see `.env.example`). Without *either*
`OPENAI_API_KEY` or `OPENROUTER_API` set, `agents/memory.py:get_memory()`
returns `None` and every function in the module degrades to a safe no-op
(`[]`/`False`) — long-term memory is an optional feature, the same
tolerance `mcp/config.py` already has for Gmail without OAuth credentials.

**User control**: `GET /memory` (list), `DELETE /memory/{id}` (one — mem0's
own `delete()` has no ownership check built in, so `forget()` verifies the
id belongs to the requesting user first), `DELETE /memory` (everything —
account-deletion/GDPR hook). Same ownership-check-then-delete shape as
`DELETE /auth/gmail/disconnect`.

## Guardrails (`core/guardrails.py`)

Rule-based only — a keyword/phrase blocklist (prompt-injection phrases)
plus PII pattern detection (email/phone/SSN/credit-card-shaped regex). No
external moderation API call: zero added latency/cost, fully offline. One
function, `check_text(text, *, context)`, reused at three call sites
rather than three near-duplicate checkers:

- **Input** (`api/routes/chat.py`'s `chat_stream`): checked before
  `run_agent_stream` is ever called. If blocked, `400` before any
  LLM/tool cost is incurred — the one call site that actually *prevents*
  something.
- **Output** (`chat.py`'s `_run_agent_worker`, after the responder's final
  message is assembled): **log-and-flag only, not a hard block** — by the
  time the full response exists, its tokens have already been streamed to
  the client via `on_chat_model_stream` events. True "block before the
  user sees it" would require buffering the whole response instead of
  streaming it token-by-token, which this app deliberately doesn't do.
  Don't read this as more protective than it is.
- **Sensitive-tool arguments** (`agents/agent.py`'s `tool_executor_node`,
  only for steps whose `tool_name` is in `SENSITIVE_TOOL_NAMES`): a
  warning is attached to the step as `guardrail_warning`, surfaced through
  the existing `plan` SSE event alongside the `requires_approval` gate.
  The actual block point for these tools is still the HITL approval gate
  (`interrupt_before`), not this check — this only adds visibility into
  *why* a step might warrant extra scrutiny before approving it.

PII matches (`matched_patterns`) are flagged but don't by themselves set
`blocked=True` — an email address in a message isn't inherently
malicious (drafting an email to a real address is the whole point of the
Gmail tools). Only the phrase blocklist blocks. See `tests/test_guardrails.py`.

## Evals

Distinct from `tests/` (which tests individual functions/code paths
deterministically, offline) — evals test the *agent's end-to-end
behavior* against real models, via [LangSmith](https://smith.langchain.com).

**Tracing** (zero code change): `langchain_core` auto-instruments every
`ainvoke`/`astream_events` call once `LANGSMITH_TRACING=true` and
`LANGSMITH_API_KEY` are set as environment variables (see `.env.example`)
— gives a trace UI for every planner/tool/responder step, tool arguments,
latency, and token counts, for free.

**Eval harness** (`evals/`, new top-level dir, not under `src/` or
`tests/`): `langsmith` lives in its own `[dependency-groups] evals` entry
in `pyproject.toml`, not the base `dependencies` list — evals hit the real
LLM/OpenAI, cost money, and aren't deterministic, so they're a periodic/CI
activity run via `uv run --group evals python -m evals.run_evals`,
deliberately separate from `uv run pytest`.

- `evals/datasets/core_scenarios.py` — test cases against the bundled demo
  MCP server (real, deterministic, no external creds needed), covering
  the exact failure classes this app has already hit in production: a
  calculator request (tool-choice correctness), a "remember X" /
  "recall X" chain (placeholder/argument-resolution correctness against a
  real model, not `ScriptedLLM`), and a plain greeting (regression guard
  for the structured-output crash fixed earlier this session).
- `evals/run_evals.py` — `target()` runs a real `create_agent(...)` against
  those cases; four evaluators: `tool_choice_matches` (exact-match),
  `no_placeholder_leak` (reuses `agents.agent._contains_placeholder`
  directly against real output), `approval_gating_correct` (asserts
  `requires_approval` was forced for `SENSITIVE_TOOL_NAMES` tools), and
  one LLM-as-judge `response_quality` evaluator built on this app's own
  `agents.llm.get_llm()` (no new model-provider dependency).

Gmail-tool scenarios aren't covered yet (would need a connected test
account) — a natural v2 expansion once that's available.

## MCP integration

`MCPClientManager` (`agents/client.py`) connects to MCP servers over stdio,
lists their tools, executes them, and can convert MCP tool schemas into
LangChain `StructuredTool`s (used if you want to hand tools to a
`create_react_agent`-style flow instead of the planner's manual dispatch).

Which servers to connect is decided by `mcp/config.py`, not hardcoded in the
route. `load_mcp_server_configs()` always includes `mcp/demo_server.py` (a
`FastMCP` server with `get_current_time`, `calculator`, `remember_note`,
`recall_notes` — no external dependencies or API keys), and adds the Gmail
server automatically when Google OAuth credentials are present in the
environment (logs and skips it otherwise — this is not a hard failure). To
add another connector, either point `MCP_SERVERS_CONFIG` at a JSON file of
`{name, command, args, env?}` objects (a `"gmail"` entry there merges into,
rather than replaces, the built-in Gmail env), or edit `mcp/config.py`
directly.

Smoke-test the demo server's tool logic without spinning up stdio transport:
```
uv run python -m mcp_orchestration.mcp.demo_server --selftest
```

### Gmail integration

Gmail is served by our **own** MCP server (`mcp/gmail_mcp_server.py`), not
Google's Workspace Developer Preview "Gmail MCP" endpoint
(`gmailmcp.googleapis.com`) — that required special enrollment and was the
source of 404s in an earlier version of this project. The current design:

```
AI Agent -> MCPClientManager (stdio) -> gmail_mcp_server.py -> Gmail API -> Gmail
```

- **Storage**: credentials are per-user, encrypted, and live in Mongo — the
  `gmail_credentials` collection (`repositories/gmail_credentials.py`), one
  document per `user_id`, with `access_token`/`refresh_token` encrypted via
  `core/token_encryption.py` (`cryptography.fernet.Fernet`, key from
  `TOKEN_ENCRYPTION_KEY`). There is no shared local token file anymore.
- **Auth flow** (`auth/gmail_oauth.py`, mounted at `/auth/gmail/*`, all
  routes require a Bearer JWT via `get_current_user`): `/login` (HTML) and
  `/authorize` (JSON) both build the same Google consent URL via
  `_build_consent_url(state)`, with `state` a short-lived signed token
  (`core/security.py`'s `create_access_token`) carrying the current user's
  id — this is both how `/callback` (which Google redirects to directly,
  with no Authorization header) knows which user the tokens belong to, and
  CSRF protection for the flow. `/login` is for manual/curl/Swagger
  testing (returns a clickable HTML page); `/authorize` is what the React
  frontend calls — a `fetch()` can attach the Bearer header a plain
  browser navigation can't, so the frontend fetches `{"auth_url": ...}`
  from `/authorize` and then does `window.location.href = auth_url`
  itself. `/callback` exchanges the code for tokens, encrypts them,
  upserts the user's `gmail_credentials` doc, and **redirects the browser
  to `settings.frontend_url`** (`?gmail=connected` on success,
  `?gmail=error&reason=...` on any failure) rather than rendering HTML
  itself — the SPA owns what the user sees, not this backend. `/status`
  reports that user's connection state. `/disconnect` revokes the token at
  Google and deletes the stored doc. This is real, working Google OAuth
  (`accounts.google.com` / `oauth2.googleapis.com`) — only the storage
  layer and callback response changed, not the OAuth mechanics. Requires
  `FRONTEND_URL` (`core/config.py`, default `http://localhost:5173`) to
  know where to send the browser back to.
- **The MCP server** (`mcp/gmail_mcp_server.py`) does not implement OAuth
  itself; it runs as its own subprocess (over stdio) so it opens its own
  short-lived pymongo connection rather than sharing the FastAPI app's
  `Database` singleton, reads the encrypted doc for `GOOGLE_USER_ID`,
  decrypts it, and refreshes via `google-auth` when near expiry
  (persisting the refreshed access token back to the same Mongo doc, still
  encrypted). Tools: `search_emails`, `get_email`, `get_thread`,
  `get_latest_emails`, `create_draft`, `send_email`, `list_labels`. Scopes
  are the minimum needed for that set — `gmail.readonly` (all reads) +
  `gmail.compose` (covers both drafting and sending) — deliberately not the
  broader `gmail.modify`.
- **Required env vars**: `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` (or
  the legacy `GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET` names, still
  supported as fallbacks), `TOKEN_ENCRYPTION_KEY`, optionally
  `GOOGLE_REDIRECT_URI`. Without all of client id/secret/encryption key
  set, `mcp/config.py` just disables the Gmail server — the app still
  starts and the demo server still works. It's also disabled per-connection
  when there's no `user_id` for the session (see `load_mcp_server_configs`)
  — there's no shared/anonymous Gmail identity anymore.
- `send_email` is in `SENSITIVE_TOOL_NAMES` (see the agent graph section),
  so the planner can never skip human approval before it actually sends
  mail, no matter what it plans.

## What changed this session

The repo already had most of this scaffolded but broken in a few places:

1. **`agents/llm.py` couldn't import** — it referenced
   `core.database.token_usage_collection`, which didn't exist (only a
   `Database` class did). Fixed by adding a lazy `get_collection(name)`
   helper to `core/database.py` and making token-usage logging catch
   `DatabaseUnavailableError` instead of crashing the request.
2. **`chat.py` had a hardcoded Windows path** to a mock MCP server on
   someone's laptop, so tool calls silently no-op'd on any other machine.
   Replaced with `mcp/config.py` + a real bundled `mcp/demo_server.py`,
   connected via `sys.executable` (portable across dev/Docker/CI).
3. **`MongoCheckpointSaver` was missing `put_writes`/`aput_writes` and any
   async methods** (`aput`, `aget_tuple`, `alist`) — LangGraph's async
   graph execution (`astream_events`, `aget_state`, `aupdate_state`, and
   `interrupt_before` HITL flow) needs these; without them the graph would
   fail the first time it actually paused for approval. Added all four,
   backed by the existing sync implementation via `asyncio.to_thread`.
4. **`get_tuple`'s "latest checkpoint" lookup loaded every checkpoint for a
   thread into memory to take the last one.** Changed to a single
   `find_one(..., sort=[("_id", -1)])`.
5. Added `user_id` tracking on checkpoint docs and two new endpoints
   (`GET /chat/threads`, `GET /chat/threads/{id}/messages`) so a frontend
   can list and reload conversations — this didn't exist before at all.
6. Fixed a checkpoint-saver signature typo (`new_releases` → `new_versions`,
   matching what LangGraph actually passes).

Since then, further work (also reflected above, not re-numbered here since
it landed incrementally rather than as one session): the placeholder/
missing-argument resolution in `tool_executor_node`, the `SENSITIVE_TOOL_NAMES`
forced-approval gate, splitting `tool_executor` into gated/ungated node
names so `route_step`'s `requires_approval` branch actually does something
(previously dead code — both branches returned the same node), the custom
Gmail MCP server + `/auth/gmail/*` OAuth flow replacing the old
Developer-Preview Gmail MCP dependency, and the `tests/` suite (previously
nonexistent). Most recently: moved Gmail token storage from a shared local
file to per-user encrypted MongoDB storage (see the Gmail integration
section); fixed `with_structured_output(...)` defaulting to
`ChatOpenAI`'s strict `json_schema` method, which crashed the planner on
any reply an OpenRouter-proxied model didn't format as strict JSON (now
`method="function_calling"` on both structured-output call sites); and
fixed `MCPClientManager.close_all` in `agents/client.py`, which was
mis-indented into a local function nested inside `execute_tool` instead of
being a real class method — `mcp_manager.close_all()` raised
`AttributeError` at the end of every `/chat/stream` run as a result, which
tests never caught since `test_chat_api.py` uses `FakeMCPClientManager`.
Also added: a "Connect Gmail" button in the React frontend
(`mcp_frontend/src/components/GmailConnectButton.jsx`) driving the new
`GET /auth/gmail/authorize` (JSON) endpoint, with `/auth/gmail/callback`
redirecting back to `FRONTEND_URL` instead of rendering HTML itself; and
long-term (cross-thread) memory via mem0 + Qdrant (`agents/memory.py`,
`api/routes/memory.py`) — see the Long-term memory section above. Also:
`agents/llm.py`'s `get_llm()` switched from OpenRouter-only to OpenAI-direct
(via `OPENAI_API_KEY`, default model `gpt-4o-mini`) with OpenRouter as a
fallback rather than the primary path — the OpenRouter default model
(`"google/gemini-2-flash"`) was never confirmed to be a real OpenRouter
slug and could 400 with "invalid model ID"; fixed the same class of bug in
`agents/memory.py`'s episodic-memory `recall()`/`has_episode()` filters,
which used a nested `{"metadata": {"kind": ...}}` shape Qdrant's filter
parser rejects — confirmed against mem0's source, the correct shape is a
flat dot-path key (`{"metadata.kind": ...}`).

## Tests

`tests/` (pytest, `asyncio_mode = "auto"` per `pyproject.toml`) runs fully
offline — no real Mongo, LLM API, or MCP subprocess. `tests/fakes.py` holds
the test doubles (`FakeMCPClientManager`, `ScriptedLLM`); `tests/conftest.py`
has shared fixtures and an SSE-body parser.

- `test_argument_resolution.py` — unit tests for `_contains_placeholder`
  and `AgentEngine._resolve_step_arguments`/`tool_executor_node` directly.
- `test_agent_graph_movie_scenario.py` — end-to-end `graph.ainvoke` test
  reproducing the original placeholder bug (`get_movie_comments` called
  with the literal string `"<selected_action_movie_title>"` instead of the
  resolved title) and asserting the fix.
- `test_chat_api.py` — hits `POST /chat/stream` through `TestClient` with
  MCP/LLM/checkpointer swapped for fakes and auth bypassed via
  `dependency_overrides`; asserts the SSE contract (status, event names,
  the `plan` payload). Doesn't see `token` events because `ScriptedLLM`
  isn't a real LangChain `Runnable` and never fires
  `on_chat_model_stream` — that's covered by the `graph.ainvoke` test above.
- `test_token_encryption.py` — round-trips `core/token_encryption.py`
  (encrypt/decrypt, missing/invalid key, wrong key, garbage ciphertext).
  Doesn't cover `GmailCredentialsRepository` itself — there's no real/fake
  Mongo fixture in this suite yet (everything else is offline-only by
  design), so repository round-tripping is manual (see CLAUDE.md's Gmail
  integration section, verification step 2).
- `test_memory.py` — unit tests for `agents/memory.py` against a fake
  standing in for mem0's `Memory` (no real mem0/Qdrant/OpenAI call): every
  function's graceful no-op behavior when `OPENAI_API_KEY` is unset, plus
  the exact call shape of `remember`/`recall`/`list_all`/`forget`/
  `forget_all`/`has_episode` (in particular that reads scope by a
  `filters` dict + `top_k` while writes take `user_id`/`run_id`/`metadata`
  top-level — mem0's `add()` and `search()`/`get_all()` genuinely differ
  here, easy to get wrong).
- `test_message_sanitization.py` — `_sanitize_messages_for_llm` converts
  `ToolMessage`s to `SystemMessage`s (order/count preserved, non-tool
  messages untouched) — see the agent graph section's note on why real
  OpenAI rejects raw `ToolMessage`s this app's executor produces.
- `test_guardrails.py` — `core/guardrails.py`'s blocklist (case-insensitive
  match) and PII regex patterns (email/phone/SSN), and that PII alone
  doesn't set `blocked=True` while a blocklist phrase does.

Run with `uv run pytest`. Evals (a different thing — see the Evals
section above) run separately via `uv run --group evals python -m evals.run_evals`.

## Honest gaps / next steps

- No rate limiting / request size limits on `/chat/stream`.
- `list()`'s `before` semantics (see Session memory section) — fine today, will
  bite you if you build checkpoint-history browsing.
- The demo MCP server's notes store is in-process memory, not persisted —
  fine for a demo, swap for a real connector when you're ready.
- No refresh tokens; JWTs just expire per `ACCESS_TOKEN_EXPIRE_MINUTES`.
- `route_step`'s gated-vs-ungated tool-executor split (see agent graph
  section) is implemented and exercised by the sensitive-tool-forcing
  logic, but there's no test yet that actually drives an interrupt through
  `/chat/resume` end-to-end with a `requires_approval` step — worth adding
  alongside the existing `test_chat_api.py` suite.
- Episodic-memory summarization (`agents/memory.py:maybe_summarize_idle_thread`)
  is triggered opportunistically off `GET /chat/threads` (see Long-term
  memory section) rather than a real "thread ended" event — fine for a v1,
  but means a thread only gets summarized once someone happens to reopen
  the sidebar after it's gone idle, not promptly when it actually ends. A
  scheduled job would be the more correct version of this.
- No account-deletion cascade wired up yet across `users`/`checkpoints`/
  `checkpoint_writes`/`gmail_credentials`/mem0's Qdrant store — `forget_all()`
  and `GmailCredentialsRepository.delete()` exist as the building blocks,
  but nothing calls them together on "delete my account."
- No retry/backoff beyond `langchain_openai`/the `openai` SDK's own default
  (2 retries) around the planner/responder's LLM calls. A transient DNS or
  network blip reaching OpenAI (`getaddrinfo failed` observed in practice)
  surfaces as an `error` SSE event and fails the whole turn rather than
  retrying with backoff — `agents/memory.py`'s reads/writes already degrade
  gracefully on the same failure (empty result / logged-and-swallowed), but
  the main chat completion has no equivalent fallback, nor an obvious one
  (there's no reasonable way to answer without the LLM). Worth revisiting
  if transient connection errors turn out to be a recurring pattern rather
  than one-off network blips.
