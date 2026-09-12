import { useCallback, useMemo, useRef, useState } from "react";
import { useExternalStoreRuntime } from "@assistant-ui/react";
import * as api from "./api";

let idCounter = 0;
function nextId(prefix) {
  idCounter += 1;
  return `${prefix}_${Date.now()}_${idCounter}`;
}

function newThreadId() {
  if (typeof crypto !== "undefined" && crypto.randomUUID) {
    return crypto.randomUUID();
  }
  return nextId("thread");
}

// Backend message roles (from langchain's BaseMessage.type) -> our roles.
function mapBackendRole(role) {
  if (role === "human") return "user";
  if (role === "ai") return "assistant";
  // "system" and "tool" messages are execution plumbing, not conversational
  // turns - the PlanPanel already shows tool activity, so we skip them here.
  return null;
}

/**
 * Owns all chat state for one thread: the message list, the live plan
 * (from `plan` SSE events), and any pending human-in-the-loop approval
 * (from `interrupt` SSE events). Also adapts that state into an
 * assistant-ui runtime via `useExternalStoreRuntime`, so <Thread /> can
 * render it with assistant-ui's primitives.
 */
export function useChatRuntime() {
  const [threadId, setThreadIdState] = useState(() => newThreadId());
  const [messages, setMessages] = useState([]);
  const [planSteps, setPlanSteps] = useState([]);
  const [pendingApproval, setPendingApproval] = useState(null);
  const [isRunning, setIsRunning] = useState(false);
  const [streamError, setStreamError] = useState(null);

  // Tracks which assistant message the *next* batch of `token` events
  // should append to. A single logical turn can span an initial
  // /chat/stream call plus one or more /chat/resume calls (one per
  // approval gate), and they should all land in the same bubble.
  const activeAssistantIdRef = useRef(null);

  // The AbortController for whichever /chat/stream or /chat/resume request
  // is currently in flight, so stopChat() can close the connection
  // immediately client-side (in addition to telling the backend to halt
  // via api.stopChat - see stopChat below).
  const abortControllerRef = useRef(null);

  const appendToken = useCallback((token) => {
    const id = activeAssistantIdRef.current;
    if (!id) return;
    setMessages((prev) =>
      prev.map((m) => (m.id === id ? { ...m, content: m.content + token } : m)),
    );
  }, []);

  const ensureActiveAssistantMessage = useCallback(() => {
    if (activeAssistantIdRef.current) return activeAssistantIdRef.current;
    const id = nextId("asst");
    activeAssistantIdRef.current = id;
    setMessages((prev) => [...prev, { id, role: "assistant", content: "" }]);
    return id;
  }, []);

  // Called whenever a turn ends (stopped/done/error) without ever having
  // streamed any content into the active assistant bubble. Without this,
  // an empty bubble is left behind rendering the "Thinking..." shimmer
  // forever, since MessageBubble decides what to show purely from
  // `content` being non-empty - it has no idea the run is actually over.
  const finalizeActiveAssistantMessage = useCallback((fallbackContent) => {
    const id = activeAssistantIdRef.current;
    if (!id) return;
    setMessages((prev) => {
      const msg = prev.find((m) => m.id === id);
      if (!msg || msg.content) return prev; // already has real content, leave it
      if (fallbackContent === undefined) {
        // No fallback text provided (e.g. plain stop) - just drop the
        // empty bubble entirely rather than showing a placeholder.
        return prev.filter((m) => m.id !== id);
      }
      return prev.map((m) =>
        m.id === id ? { ...m, content: fallbackContent } : m,
      );
    });
    activeAssistantIdRef.current = null;
  }, []);

  const handlers = useMemo(
    () => ({
      onPlan: (plan) => setPlanSteps(plan),
      onToken: (token) => {
        ensureActiveAssistantMessage();
        appendToken(token);
      },
      onInterrupt: (data) => {
        setPendingApproval(data);
        setIsRunning(false);
      },
      onStopped: () => {
        setIsRunning(false);
        setPendingApproval(null);
        finalizeActiveAssistantMessage();
      },
      onDone: () => {
        setIsRunning(false);
        setPendingApproval(null);
        finalizeActiveAssistantMessage();
      },
      onError: (err) => {
        setStreamError(err);
        setIsRunning(false);
        finalizeActiveAssistantMessage(
          "_Something went wrong generating this response._",
        );
      },
    }),
    [appendToken, ensureActiveAssistantMessage, finalizeActiveAssistantMessage],
  );

  const sendMessage = useCallback(
    async (text) => {
      // Guard on pendingApproval too: onInterrupt flips isRunning back to
      // false (so the composer looks idle) while the backend graph is
      // actually still paused waiting on /chat/resume for this thread.
      // Without this check, typing a follow-up here silently discards the
      // pending approval (no resume/deny call ever reaches the backend)
      // and kicks off a brand-new stream, leaving the old checkpoint
      // stuck mid-interrupt forever.
      if (!text.trim() || isRunning || pendingApproval) return;
      setStreamError(null);
      setPendingApproval(null);
      setPlanSteps([]);
      activeAssistantIdRef.current = null;

      setMessages((prev) => [
        ...prev,
        { id: nextId("user"), role: "user", content: text },
      ]);
      setIsRunning(true);
      ensureActiveAssistantMessage();

      const controller = new AbortController();
      abortControllerRef.current = controller;

      try {
        await api.streamChat(
          { message: text, thread_id: threadId },
          handlers,
          controller.signal,
        );
      } catch (err) {
        // A user-initiated stop() aborts the fetch on purpose - that's not
        // a real error, so don't surface it as one. stopChat() has already
        // reset isRunning/messages state by the time this rejects.
        if (err.name !== "AbortError") {
          setStreamError(err.message || String(err));
          finalizeActiveAssistantMessage(
            "_Something went wrong generating this response._",
          );
        }
      } finally {
        setIsRunning(false);
        abortControllerRef.current = null;
      }
    },
    [
      threadId,
      isRunning,
      pendingApproval,
      handlers,
      ensureActiveAssistantMessage,
      finalizeActiveAssistantMessage,
    ],
  );

  const respondToApproval = useCallback(
    async (approve, modifiedArguments) => {
      if (!pendingApproval) return;
      setStreamError(null);
      setPendingApproval(null);
      setIsRunning(true);
      // Keep appending to the same assistant bubble that was started (or
      // will be started) for this turn.
      ensureActiveAssistantMessage();

      const controller = new AbortController();
      abortControllerRef.current = controller;

      try {
        await api.resumeChat(
          {
            thread_id: threadId,
            approve,
            modified_arguments: modifiedArguments,
          },
          handlers,
          controller.signal,
        );
      } catch (err) {
        if (err.name !== "AbortError") {
          setStreamError(err.message || String(err));
          finalizeActiveAssistantMessage(
            "_Something went wrong generating this response._",
          );
        }
      } finally {
        setIsRunning(false);
        abortControllerRef.current = null;
      }
    },
    [
      threadId,
      pendingApproval,
      handlers,
      ensureActiveAssistantMessage,
      finalizeActiveAssistantMessage,
    ],
  );

  const approve = useCallback(
    (modifiedArguments) => respondToApproval(true, modifiedArguments),
    [respondToApproval],
  );
  const deny = useCallback(
    () => respondToApproval(false, undefined),
    [respondToApproval],
  );

  // Stop the current run: tell the backend to halt as soon as it can
  // (checked between graph steps in run_agent_stream) *and* close the
  // client's connection immediately, so the UI doesn't sit waiting for a
  // stream that may take a little longer to actually stop server-side.
  const stopChat = useCallback(async () => {
    if (!isRunning) return;
    try {
      await api.stopChat({ thread_id: threadId });
    } catch (err) {
      // Best-effort - still abort the local stream below even if telling
      // the backend failed (e.g. a network hiccup), so the UI never stays
      // stuck on "running" just because this one request failed.
      console.error("Failed to notify backend of stop:", err);
    }
    abortControllerRef.current?.abort();
    setIsRunning(false);
    setPendingApproval(null);
    finalizeActiveAssistantMessage();
  }, [isRunning, threadId, finalizeActiveAssistantMessage]);

  const startNewThread = useCallback(async () => {
    // Notify the backend so an in-flight/paused run for the old thread
    // doesn't keep executing (or sit stuck on an interrupt) after we've
    // navigated away from it client-side.
    if (isRunning || pendingApproval) {
      try {
        await api.stopChat({ thread_id: threadId });
      } catch (err) {
        console.error("Failed to notify backend of stop:", err);
      }
    }
    abortControllerRef.current?.abort();
    abortControllerRef.current = null;

    setThreadIdState(newThreadId());
    setMessages([]);
    setPlanSteps([]);
    setPendingApproval(null);
    setStreamError(null);
    setIsRunning(false);
    activeAssistantIdRef.current = null;
  }, [isRunning, pendingApproval, threadId]);

  const loadThread = useCallback(
    async (id) => {
      if (isRunning || pendingApproval) {
        try {
          await api.stopChat({ thread_id: threadId });
        } catch (err) {
          console.error("Failed to notify backend of stop:", err);
        }
      }
      abortControllerRef.current?.abort();
      abortControllerRef.current = null;

      setThreadIdState(id);
      setPlanSteps([]);
      setPendingApproval(null);
      setStreamError(null);
      setIsRunning(false);
      activeAssistantIdRef.current = null;
      const history = await api.getThreadMessages(id);
      const mapped = history
        .map((m) => {
          const role = mapBackendRole(m.role);
          if (!role) return null;
          return { id: nextId("hist"), role, content: m.content };
        })
        .filter(Boolean);
      setMessages(mapped);
    },
    [isRunning, pendingApproval, threadId],
  );

  // --- assistant-ui integration ---------------------------------------
  //
  // `useExternalStoreRuntime` lets assistant-ui's <ThreadPrimitive /> /
  // <ComposerPrimitive /> components render and drive state that we
  // fully own (our `messages` array above), instead of assistant-ui
  // owning its own internal store. `onNew` fires when the user submits
  // the composer; everything else (streaming tokens in, HITL pausing)
  // is pushed in from outside via setMessages, which is exactly what
  // external-store runtimes are for.
  const runtime = useExternalStoreRuntime({
    messages,
    isRunning,
    convertMessage: (m) => ({
      role: m.role,
      id: m.id,
      content: [{ type: "text", text: m.content }],
    }),
    onNew: async (message) => {
      const textPart = message.content.find((c) => c.type === "text");
      await sendMessage(textPart?.text ?? "");
    },
  });

  return {
    runtime,
    threadId,
    messages,
    planSteps,
    pendingApproval,
    isRunning,
    streamError,
    sendMessage,
    approve,
    deny,
    stopChat,
    startNewThread,
    loadThread,
  };
}
