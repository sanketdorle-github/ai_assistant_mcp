import { consumeSSE } from "./sse";

export const API_BASE_URL =
  import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000";

const TOKEN_KEY = "mcp_orchestration_token";

export function getToken() {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token) {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

async function request(path, { method = "GET", body, auth = true } = {}) {
  const headers = { "Content-Type": "application/json" };
  if (auth) {
    const token = getToken();
    if (token) headers["Authorization"] = `Bearer ${token}`;
  }

  let res;
  try {
    res = await fetch(`${API_BASE_URL}${path}`, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch (err) {
    // Network-level failure - most commonly a missing CORS setup on the
    // backend, or the backend simply not running. See FRONTEND.md.
    throw new ApiError(
      `Could not reach the backend at ${API_BASE_URL}. Is it running, and does it allow CORS from this origin? (${err.message})`,
      0,
    );
  }

  const text = await res.text();
  const data = text ? safeJson(text) : null;

  if (!res.ok) {
    const detail =
      (data && (data.detail || data.message)) ||
      res.statusText ||
      "Request failed";
    throw new ApiError(
      typeof detail === "string" ? detail : JSON.stringify(detail),
      res.status,
    );
  }

  return data;
}

function safeJson(text) {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

// --- Auth -------------------------------------------------------------

/** POST /auth/register -> UserResponse */
export function register({ name, email, password }) {
  return request("/auth/register", {
    method: "POST",
    body: { name, email, password },
    auth: false,
  });
}

/** POST /auth/login -> { access_token, token_type } */
export function login({ email, password }) {
  return request("/auth/login", {
    method: "POST",
    body: { email, password },
    auth: false,
  });
}

/** GET /auth/me -> UserResponse (requires Bearer token) */
export function getMe() {
  return request("/auth/me");
}

// --- Gmail connection ---------------------------------------------------

/** GET /auth/gmail/status -> { authenticated, has_refresh_token?, ... } */
export function getGmailStatus() {
  return request("/auth/gmail/status");
}

/** GET /auth/gmail/authorize -> { auth_url }. Fetch this (it needs the
 * Bearer header) then `window.location.href = auth_url` to actually start
 * the Google consent flow - a plain navigation can't carry the header. */
export function getGmailAuthorizeUrl() {
  return request("/auth/gmail/authorize");
}

/** DELETE /auth/gmail/disconnect -> { disconnected: true } */
export function disconnectGmail() {
  return request("/auth/gmail/disconnect", { method: "DELETE" });
}

// --- Chat: threads & history -------------------------------------------

/** GET /chat/threads -> [{ thread_id }] */
export function listThreads() {
  return request("/chat/threads");
}

/** GET /chat/threads/{id}/messages -> [{ role, content }] */
export function getThreadMessages(threadId) {
  return request(`/chat/threads/${encodeURIComponent(threadId)}/messages`);
}

// --- Chat: streaming -----------------------------------------------------
//
// Both /chat/stream and /chat/resume return the same SSE event vocabulary:
//   plan      -> { plan: TaskStep[] }
//   token     -> { token: string }
//   interrupt -> { message, step_id, tool_name, tool_arguments }
//   stopped   -> { message: "Execution stopped by user." }
//   done      -> { message: "Execution finished" }
//   error     -> { error: string }
//
// `handlers` may define any of: onPlan, onToken, onInterrupt, onStopped, onDone, onError

async function streamRequest(path, body, handlers, signal) {
  const token = getToken();
  const headers = { "Content-Type": "application/json" };
  if (token) headers["Authorization"] = `Bearer ${token}`;

  let res;
  try {
    res = await fetch(`${API_BASE_URL}${path}`, {
      method: "POST",
      headers,
      body: JSON.stringify(body),
      signal,
    });
  } catch (err) {
    if (err.name === "AbortError") throw err;
    throw new ApiError(
      `Could not reach the backend at ${API_BASE_URL} (${err.message})`,
      0,
    );
  }

  await consumeSSE(res, (eventName, dataStr) => {
    let data;
    try {
      data = JSON.parse(dataStr);
    } catch {
      data = null;
    }

    switch (eventName) {
      case "plan":
        handlers.onPlan?.(data?.plan ?? []);
        break;
      case "token":
        handlers.onToken?.(data?.token ?? "");
        break;
      case "interrupt":
        handlers.onInterrupt?.(data);
        break;
      case "stopped":
        handlers.onStopped?.(data);
        break;
      case "done":
        handlers.onDone?.(data);
        break;
      case "error":
        handlers.onError?.(data?.error ?? "Unknown error");
        break;
      default:
        break;
    }
  });
}

/** POST /chat/stream — start (or continue) a thread with a new user message. */
export function streamChat({ message, thread_id }, handlers, signal) {
  return streamRequest(
    "/chat/stream",
    { message, thread_id },
    handlers,
    signal,
  );
}

/** POST /chat/resume — approve/deny/modify a paused (HITL) tool step. */
export function resumeChat(
  { thread_id, approve, modified_arguments },
  handlers,
  signal,
) {
  return streamRequest(
    "/chat/resume",
    { thread_id, approve, modified_arguments: modified_arguments ?? null },
    handlers,
    signal,
  );
}

/**
 * POST /chat/stop — ask the backend to halt the in-flight run for this
 * thread as soon as possible (checked between graph steps server-side).
 * Fire-and-forget from the caller's perspective: pair this with aborting
 * the local fetch/stream too, since this only stops the *backend* from
 * continuing - it doesn't by itself close the client's connection.
 */
export function stopChat({ thread_id }) {
  return request("/chat/stop", { method: "POST", body: { thread_id } });
}

export { ApiError };
