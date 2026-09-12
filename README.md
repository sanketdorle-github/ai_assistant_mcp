# MCP Orchestration

An AI personal-assistant platform: a FastAPI backend running a LangGraph
**plan → act → respond** agent that calls tools through the **Model Context
Protocol (MCP)**, paired with a React chat frontend. It supports streaming
responses, human-in-the-loop approval for sensitive tool calls (like sending
email), short-term (per-thread) and long-term (cross-thread) memory, and a
real Gmail integration (search/read/draft/send).

```
┌─────────────────┐      REST + SSE        ┌─────────────────────────┐
│  mcp_frontend   │ ────────────────────▶ │   mcp_project           │
│  Vite + React   │ ◀──────────────────── │   FastAPI backend       │
│  :5173          │                        │   :8000                 │
└─────────────────┘                        └──────┬────────┬─────────┘
                                                    │        │
                                       stdio (MCP)  │        │  LangGraph agent
                                     ┌──────────────┘        └───────────────┐
                                     ▼                                       ▼
                          demo / Gmail MCP servers                 OpenAI / OpenRouter
                                     │
                          ┌──────────┴──────────┐
                          ▼                      ▼
                     MongoDB (:27017)      Qdrant (:6333, mem0
                (users, checkpoints,        long-term memory)
                 gmail credentials)
```

## Repository layout

```
mcp_project/       FastAPI backend (Python, uv-managed) — see mcp_project/README.md
                    and mcp_project/CLAUDE.md for a full architecture deep-dive
mcp_frontend/       Vite + React frontend — see mcp_frontend/FRONTEND.md for the
                    full API contract, SSE event vocabulary, and component map
```

## Prerequisites

- Python **3.12+** and [uv](https://docs.astral.sh/uv/) (backend package/venv manager)
- Node.js **18+** and npm (frontend)
- Docker (to run MongoDB + Qdrant via the bundled `docker-compose.yaml`) —
  or point the backend at your own MongoDB/Qdrant instances instead
- An OpenAI and/or OpenRouter API key (for the LLM calls — see env vars below)

## Quick start

### 1. Clone

```bash
git clone <this-repo-url>
cd mcp_project_updated
```

### 2. Start infrastructure (MongoDB + Qdrant)

```bash
cd mcp_project
docker compose up -d
```

This starts MongoDB on `:27017` (dev-only default credentials `admin`/`admin`
— fine for local use, **do not reuse these in any shared or deployed
environment**) and Qdrant on `:6333` (vector store for long-term memory).

### 3. Backend

```bash
# still inside mcp_project/
cp .env.example .env
# Edit .env — at minimum set SECRET_KEY and one of OPENAI_API_KEY / OPENROUTER_API.
# See "Environment variables" below for what's required vs optional.

uv sync
uv run uvicorn mcp_orchestration.main:app --reload
```

The API is now at `http://127.0.0.1:8000`, with interactive docs at
`http://127.0.0.1:8000/docs`. Everything except `/`, `/auth/register`,
`/auth/login`, and `/auth/token` requires a Bearer JWT from login.

### 4. Frontend

```bash
cd mcp_frontend
cp .env.example .env
npm install
npm run dev
```

The app is now at `http://localhost:5173`. It talks to the backend via
`VITE_API_BASE_URL` (defaults to `http://127.0.0.1:8000`, which matches the
backend's default `CORS_ALLOWED_ORIGINS`, so no changes are needed for local
dev with the defaults on both sides).

## Environment variables

### Backend (`mcp_project/.env`, copied from `mcp_project/.env.example`)

| Variable | Required? | Purpose |
|---|---|---|
| `SECRET_KEY` | **Required** | Signs JWTs. Any long random string. |
| `OPENAI_API_KEY` | Recommended | Used for chat completions if set (preferred path); also required for the long-term-memory embedder. |
| `OPENROUTER_API` / `OPENROUTER_DEFAULT_MODEL` | Required if no `OPENAI_API_KEY` | Fallback chat-completion provider. Set the model explicitly — the code's built-in fallback model is not a confirmed-valid OpenRouter slug. |
| `MONGO_URI` / `DB_NAME` | Required | Defaults match the bundled `docker-compose.yaml` (`mongodb://localhost:27017`). |
| `CORS_ALLOWED_ORIGINS` | Required | Must include the frontend's origin (default `http://localhost:5173`). |
| `FRONTEND_URL` | Required for Gmail OAuth | Where the backend redirects the browser back to after the Google consent flow. |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` / `GOOGLE_REDIRECT_URI` | Optional | Enables the Gmail MCP server. App runs fine without these — Gmail tools are just disabled. |
| `TOKEN_ENCRYPTION_KEY` | Required with Gmail | Encrypts stored Gmail tokens. Generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. |
| `QDRANT_HOST` / `QDRANT_PORT` / `OPENAI_EMBED_MODEL` | Optional | Long-term (cross-thread) memory via mem0 + Qdrant. Disabled entirely if neither `OPENAI_API_KEY` nor `OPENROUTER_API` is set. |
| `MCP_SERVERS_CONFIG` | Optional | Path to a JSON file adding/overriding MCP server connections beyond the bundled demo + Gmail servers. |
| `LANGSMITH_TRACING` / `LANGSMITH_API_KEY` / `LANGSMITH_PROJECT` / `LANGSMITH_ENDPOINT` | Optional | Tracing/evals via LangSmith. |

`mcp_project/.env.example` is the source of truth and has full inline comments
for every variable, including a few marked vestigial/unused by current code.

### Frontend (`mcp_frontend/.env`, copied from `mcp_frontend/.env.example`)

| Variable | Default | Purpose |
|---|---|---|
| `VITE_API_BASE_URL` | `http://127.0.0.1:8000` | Base URL of the FastAPI backend. |

## Key features

- **LangGraph agent** (`planner → tool_executor → responder`) with structured
  planning output and per-tool human-in-the-loop approval for sensitive
  actions (`send_email`, `write_file`, `delete_file`).
- **Streaming** chat responses over Server-Sent Events, including live
  plan/progress updates and interrupt/approval events.
- **MCP tool integration** — a bundled demo server (time, calculator, notes)
  plus a real Gmail MCP server (search/read/draft/send), connected over stdio.
- **Session memory** (MongoDB-backed LangGraph checkpoints, per-thread) and
  **long-term memory** (mem0 + Qdrant, semantic facts and episodic summaries
  that persist across threads).
- **Auth**: email/password with JWT, plus a separate Google OAuth flow for
  Gmail.
- **Guardrails**: rule-based input/output/tool-argument checks (prompt-
  injection phrases, PII patterns).

## Testing

```bash
cd mcp_project
uv run pytest
```

Tests run fully offline (no real Mongo/LLM/MCP subprocess — see
`mcp_project/CLAUDE.md`'s Tests section). Evals (which hit a real LLM and
cost money) run separately: `uv run --group evals python -m evals.run_evals`.

## Further documentation

- [`mcp_project/README.md`](mcp_project/README.md) — backend setup details
- [`mcp_project/CLAUDE.md`](mcp_project/CLAUDE.md) — full backend architecture,
  agent graph design, memory system, Gmail integration, and known gaps
- [`mcp_frontend/FRONTEND.md`](mcp_frontend/FRONTEND.md) — frontend project
  structure, full REST/SSE API contract, and Gmail-connect flow

## Security notes

- Never commit a real `.env` file — only `.env.example` files belong in git.
  Both `mcp_project/.env` and `mcp_frontend/.env` are gitignored.
- The `docker-compose.yaml` Mongo credentials (`admin`/`admin`) are dev-only
  defaults — change them (and use TLS/auth properly) before deploying anywhere
  reachable outside your own machine.
- If you're setting this repo up from an existing local checkout that already
  had a filled-in `.env`, treat every key in it as potentially exposed and
  consider rotating them before making the repo public.

## License

[MIT](LICENSE)
