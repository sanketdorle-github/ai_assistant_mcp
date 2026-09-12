# MCP Orchestration — Frontend

A standalone Vite + React (JavaScript) single-page app for the `mcp_orchestration`
FastAPI backend. It provides email/password auth, a chat UI built on
[`@assistant-ui/react`](https://www.assistant-ui.com/) wired to the backend's
SSE streaming endpoints, a live execution-plan sidebar, a
human-in-the-loop (HITL) approval flow for gated tool calls, and a Gmail
connect button that lets a logged-in user link their Gmail account so the
agent's Gmail tools (search/read/draft/send) become available.

`npm run build` has been run and verified against this codebase.

---

## 1. Quick start

```bash
cd frontend
cp .env.example .env        # defaults to http://127.0.0.1:8000, edit if needed
npm install
npm run dev                 # http://localhost:5173
```

You'll also need the backend running (see its `CLAUDE.md`), with
`CORS_ALLOWED_ORIGINS` including this app's origin (`http://localhost:5173`
by default — already the backend's own default too) and, if you want the
Gmail connect button to work, `FRONTEND_URL` set to this app's origin as
well (see §5a).

## 2. CORS

The backend (`src/mcp_orchestration/main.py`) already registers
`CORSMiddleware` with `allow_origins=settings.cors_allowed_origins`
(`core/config.py`, env var `CORS_ALLOWED_ORIGINS`, defaults to
`http://localhost:5173`) — no backend change needed for local dev. If you
deploy this app to a different origin, add that origin to the backend's
`CORS_ALLOWED_ORIGINS` (comma-separated) or every request will fail with a
network/CORS error.

## 3. Configuration

All config is one env var, read by Vite at build/dev time:

| Variable              | Default                 | Purpose                          |
|------------------------|--------------------------|-----------------------------------|
| `VITE_API_BASE_URL`   | `http://127.0.0.1:8000` | Base URL of the FastAPI backend  |

Set it in `frontend/.env` (copy from `.env.example`). Change it per-environment
(local/staging/prod) without touching code.

## 4. Project structure

```
src/
  main.jsx              Entry point: BrowserRouter mount
  App.jsx                Routes: /login, /register, / (protected)
  index.css               Tailwind directives + small chat-markdown styles
  lib/
    api.js                 Full REST + SSE client (every backend endpoint)
    sse.js                 Hand-rolled SSE parser (fetch streams, not EventSource)
    auth.jsx                AuthContext: login/register/logout/session-restore
    useChatRuntime.js       Owns thread/message/plan/approval state; adapts it
                            into an assistant-ui runtime via useExternalStoreRuntime
  components/
    LoginPage.jsx, RegisterPage.jsx, ProtectedRoute.jsx
    ChatPage.jsx            Layout: Sidebar + Thread + PlanPanel + ApprovalPanel
    Sidebar.jsx             Thread list (GET /chat/threads), new chat, logout,
                            mounts GmailConnectButton
    GmailConnectButton.jsx   Connect/disconnect Gmail, shows status + the
                            one-time redirect notice (see §5a)
    Thread.jsx               Message list + composer (assistant-ui primitives)
    PlanPanel.jsx            Live step-by-step plan/progress sidebar
    ApprovalPanel.jsx        HITL approve/deny/edit-arguments card
    MarkdownText.jsx         react-markdown renderer for assistant replies
```

## 5. API integration — exact contract used

All of this lives in `src/lib/api.js`. JWT is stored in `localStorage`
(`mcp_orchestration_token`) and sent as `Authorization: Bearer <token>` on
every request except register/login.

| Method | Path                              | Body                                             | Notes |
|--------|-------------------------------------|---------------------------------------------------|-------|
| POST   | `/auth/register`                   | `{name, email, password}`                        | `password` must be ≥6 chars |
| POST   | `/auth/login`                       | `{email, password}`                               | returns `{access_token, token_type}` |
| GET    | `/auth/me`                          | —                                                  | used to restore session on load |
| GET    | `/chat/threads`                    | —                                                  | `[{thread_id}]`, most-recent first |
| GET    | `/chat/threads/{id}/messages`      | —                                                  | `[{role, content}]`, `role` is langchain's `human`/`ai`/`system`/`tool` |
| POST   | `/chat/stream`                     | `{message, thread_id}`                            | SSE stream (see below) |
| POST   | `/chat/resume`                     | `{thread_id, approve, modified_arguments}`        | SSE stream (see below), continues an interrupted run |
| GET    | `/auth/gmail/status`               | —                                                  | `{authenticated, has_refresh_token?, ...}` for the current user |
| GET    | `/auth/gmail/authorize`            | —                                                  | `{auth_url}` — see §5a, not a navigation target itself |
| DELETE | `/auth/gmail/disconnect`           | —                                                  | revokes at Google, deletes the stored credential |

**Thread IDs are generated client-side** (`crypto.randomUUID()`) — the
backend has no "create thread" endpoint; a thread is implicitly created the
first time you `POST /chat/stream` with a new `thread_id`.

### SSE event vocabulary (both `/chat/stream` and `/chat/resume`)

```
event: plan       data: {"plan": [ {id, description, tool_name, tool_arguments,
                                    requires_approval, status, result}, ... ]}
event: token      data: {"token": "..."}
event: interrupt  data: {"message", "step_id", "tool_name", "tool_arguments"}
event: done       data: {"message": "Execution finished"}
event: error      data: {"error": "..."}
```

Native `EventSource` can't do POST bodies or custom `Authorization` headers,
so `src/lib/sse.js` reads the raw `fetch()` `ReadableStream` and parses
`sse_starlette`'s wire format by hand (blank-line-delimited `event:`/`data:`
blocks). `src/lib/api.js` wraps this into `streamChat()` / `resumeChat()`,
each taking a small `handlers` object (`onPlan`, `onToken`, `onInterrupt`,
`onDone`, `onError`).

### How a turn actually flows (`useChatRuntime.js`)

1. User sends a message → appended to local state, `POST /chat/stream` fires.
2. `plan` events update the `PlanPanel`. `token` events append to a single
   growing assistant message bubble.
3. If the graph pauses on a gated step, an `interrupt` event sets
   `pendingApproval` and the stream ends for now — `ApprovalPanel` renders.
4. The user approves (optionally editing the tool's JSON arguments), denies,
   via `POST /chat/resume`. This can happen more than once per turn if
   multiple steps require approval; tokens from every resume append to the
   *same* assistant bubble the turn started with.
5. A `done` event ends the turn; `error` events surface inline.

## 5a. Gmail connect flow (`GmailConnectButton.jsx`)

The backend's Gmail OAuth is per-user and JWT-gated (see its `CLAUDE.md`,
Gmail integration section). Google's consent screen has to be reached by a
**real browser navigation** (`window.location.href = ...`), but that kind
of navigation can't carry an `Authorization` header — so the button does
this in two steps:

1. `api.getGmailAuthorizeUrl()` — a `fetch()` to `GET /auth/gmail/authorize`
   with the Bearer header attached like any other API call — returns
   `{"auth_url": "https://accounts.google.com/..."}`. This URL embeds a
   short-lived signed `state` identifying the current user; it's not
   something the client builds itself.
2. `window.location.href = auth_url` — a full-page navigation (not a
   popup) away to Google's consent screen. The whole tab leaves the SPA.

After the user approves, Google redirects to the **backend's**
`/auth/gmail/callback`, which stores the credentials and then itself
redirects back to `FRONTEND_URL` (a backend env var — see its
`.env.example`) with `?gmail=connected` or `?gmail=error&reason=...`.
**`FRONTEND_URL` on the backend must match wherever this app is actually
served from**, or the user lands on the wrong origin after connecting.

`GmailConnectButton.jsx` reads that `?gmail=...` param once (via
`useSearchParams`) to show a one-time inline success/error notice, then
strips it from the URL so a refresh doesn't re-show it. Connection status
itself always comes from `GET /auth/gmail/status` on mount, independent of
that param — the redirect notice is just a courtesy message, not the
source of truth. Since this is a full-page redirect, the whole app
remounts on return; no local React state survives the trip (nothing needs
to — thread list and auth session both come back from the backend/
`localStorage` as normal on load).

## 6. Why `useExternalStoreRuntime`, not `useLocalRuntime`

`@assistant-ui/react` ships two ways to drive its `<ThreadPrimitive>` /
`<ComposerPrimitive>` components: `useLocalRuntime(adapter)` (assistant-ui
owns the message list; you implement a `ChatModelAdapter.run()`) or
`useExternalStoreRuntime(store)` (you own the message list; assistant-ui just
renders it). This app needs to inject content mid-turn from an *external*
trigger — the user clicking Approve/Deny in `ApprovalPanel`, not typing in the
composer — which doesn't fit `ChatModelAdapter`'s "one `run()` per composer
submit" shape. Owning the array ourselves (`useChatRuntime`) and handing it to
`useExternalStoreRuntime` made the HITL resume flow straightforward instead of
fighting the adapter contract.

The message list itself is rendered with plain React (mapping over that same
array — see `Thread.jsx`) rather than `<ThreadPrimitive.Messages>`, so
streaming/loading states stay simple to reason about. The composer
(`<ComposerPrimitive.Root/Input/Send>`) is genuine assistant-ui, reading
`isRunning` from the runtime to disable itself mid-stream.

## 7. Known limitations / please verify

- `@assistant-ui/react` is pinned to `^0.7.11` in `package.json`. If `npm
  install` resolves a materially newer major version and `ThreadPrimitive` /
  `ComposerPrimitive` / `useExternalStoreRuntime` have changed shape, check
  https://www.assistant-ui.com/docs — the integration is isolated to
  `Thread.jsx` and `useChatRuntime.js`, so fixes should stay localized there.
- `GET /chat/threads` only returns `{thread_id}` (no title, timestamp, or
  preview) — the sidebar currently just lists raw thread IDs. If you'd like,
  I can add a friendlier label (e.g. first user message) by extending the
  backend's `list_threads_for_user` to include one, or by deriving it
  client-side from `GET /chat/threads/{id}/messages`.
- `system` and `tool` role messages from `GET /chat/threads/{id}/messages`
  are currently filtered out of the chat view (only `human`/`ai` render) since
  tool activity is already shown in the `PlanPanel`. Say the word if you'd
  rather see them inline as well.
- No thread renaming/deleting, no message editing/regeneration, no
  streaming-cancel button — none of these have backend endpoints yet.
- Password reset / email verification aren't implemented (backend doesn't
  expose them either).
- The Gmail connect flow is a full-page redirect, not a popup (see §5a) —
  simpler and popup-blocker-free, at the cost of a full app remount on
  return. Revisit if that ever feels heavy-handed.

## 8. Open questions

1. Should the sidebar show a nicer thread title than the raw UUID? If so,
   where should that title come from — I'd need a small backend change or a
   client-side heuristic.
2. Any preference on how tool `result` values render in `PlanPanel` when
   they're large objects rather than short strings (currently just
   `String(result)`, truncated to 3 lines)?
