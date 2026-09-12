# MCP Orchestration — Backend

FastAPI backend for a personal AI assistant: auth, streaming chat, a
LangGraph plan → act → respond agent, MongoDB-backed session memory plus
mem0+Qdrant-backed long-term memory, and an MCP client that calls tools from
any MCP server (a bundled demo server, plus a real Gmail connector).

> See the [root README](../README.md) for how this fits together with the
> frontend, and [`CLAUDE.md`](CLAUDE.md) for a full architecture deep-dive
> (agent graph, streaming/HITL, memory internals, Gmail OAuth flow, guardrails,
> evals, and known gaps).

## Setup

```bash
docker compose up -d          # starts MongoDB (:27017) + Qdrant (:6333)
cp .env.example .env          # fill in SECRET_KEY and OPENAI_API_KEY or
                               # OPENROUTER_API at minimum
uv sync
uv run uvicorn mcp_orchestration.main:app --reload
```

API docs at `http://127.0.0.1:8000/docs`. Everything except `/`,
`/auth/register`, `/auth/login`, and `/auth/token` requires a Bearer JWT from
login.

## Environment variables

`.env.example` is the source of truth, with an inline comment on every
variable explaining what it's for and what happens if it's left unset. In
short:

- **Required**: `SECRET_KEY`, `MONGO_URI`/`DB_NAME`, `CORS_ALLOWED_ORIGINS`,
  and one of `OPENAI_API_KEY` or `OPENROUTER_API`/`OPENROUTER_DEFAULT_MODEL`.
- **Optional — Gmail MCP server**: `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET`,
  `GOOGLE_REDIRECT_URI`, `TOKEN_ENCRYPTION_KEY`, `FRONTEND_URL`. Without
  these, the app still starts — Gmail tools are just disabled.
- **Optional — long-term memory**: `QDRANT_HOST`/`QDRANT_PORT`,
  `OPENAI_EMBED_MODEL`. Disabled entirely without an OpenAI/OpenRouter key.
- **Optional — MCP servers**: `MCP_SERVERS_CONFIG` (extra server connections).
- **Optional — observability**: `LANGSMITH_TRACING`, `LANGSMITH_API_KEY`,
  `LANGSMITH_PROJECT`, `LANGSMITH_ENDPOINT`.
- A few vars near the bottom of `.env.example` are marked vestigial — present
  in some local `.env` files from earlier experiments but not read by any
  code in `src/` today.

## Project structure

```
src/mcp_orchestration/
  main.py               App factory, router registration, CORS, middleware
  api/routes/            HTTP boundary: auth, users, chat
  core/                  Config, DB connection, JWT, request logging
  schemas/                Pydantic I/O models
  repositories/            MongoDB persistence
  services/                Business logic + LangGraph checkpoint saver
  agents/                  LLM setup, long-term memory (mem0+Qdrant), MCP
                          client manager, the LangGraph agent graph itself
  auth/                    Gmail OAuth flow
  mcp/                     Bundled demo MCP server + Gmail MCP server + config
evals/                   LangSmith-based end-to-end agent evals (not pytest)
tests/                   Offline pytest suite (no real Mongo/LLM/MCP calls)
docker-compose.yaml      MongoDB + Qdrant for local dev
```

See [`CLAUDE.md`](CLAUDE.md) for what each module actually does.

## Testing

```bash
uv run pytest
```

Runs fully offline. Evals (hit a real LLM, cost money, not deterministic) run
separately:

```bash
uv run --group evals python -m evals.run_evals
```

## Gmail integration

Requires `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` (a Google Cloud OAuth 2.0
Web application client) and `TOKEN_ENCRYPTION_KEY`. Generate an encryption
key with:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

See `CLAUDE.md`'s "Gmail integration" section for the full OAuth flow and
storage design.
