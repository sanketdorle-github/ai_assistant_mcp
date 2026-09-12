import { ThreadPrimitive, ComposerPrimitive } from "@assistant-ui/react";
import { ArrowUp, Bot, User, Square } from "lucide-react";
import MarkdownText from "./MarkdownText";

/**
 * Renders one message bubble. We map over our own `messages` state
 * directly (see useChatRuntime) rather than through
 * <ThreadPrimitive.Messages>, so streaming updates, role handling, and
 * empty-state placeholders all stay simple and predictable - the
 * runtime wiring below is still real assistant-ui (composer + running
 * state), it's just that message *rendering* is plain React.
 */
function MessageBubble({ role, content }) {
  const isUser = role === "user";
  return (
    <div className={`flex gap-3 ${isUser ? "flex-row-reverse" : ""}`}>
      <div
        className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-full ${
          isUser ? "bg-brand-600 text-white" : "bg-slate-200 text-slate-700"
        }`}
      >
        {isUser ? <User size={16} /> : <Bot size={16} />}
      </div>
      <div
        className={`max-w-[75%] rounded-2xl px-4 py-2.5 ${
          isUser
            ? "bg-brand-600 text-white"
            : "bg-white border border-slate-200 text-slate-900"
        }`}
      >
        {isUser ? (
          <p className="text-sm leading-relaxed whitespace-pre-wrap break-words">
            {content}
          </p>
        ) : content ? (
          <MarkdownText text={content} />
        ) : (
          <ThinkingIndicator />
        )}
      </div>
    </div>
  );
}

// Add this small component above MessageBubble in Thread.jsx
function ThinkingIndicator() {
  return (
    <div className="inline-flex items-center gap-2 py-0.5">
      <span
        className="bg-clip-text text-transparent bg-[length:200%_100%] animate-shimmer text-sm font-medium
                   bg-gradient-to-r from-slate-300 via-slate-900 to-slate-300"
      >
        Thinking
      </span>
      <span className="flex items-center gap-1 ">
        <span className="h-1.5 w-1.5 rounded-full bg-slate-400 animate-bounce [animation-delay:-0.3s]" />
        <span className="h-1.5 w-1.5 rounded-full bg-slate-400 animate-bounce [animation-delay:-0.15s]" />
        <span className="h-1.5 w-1.5 rounded-full bg-slate-400 animate-bounce" />
      </span>
    </div>
  );
}

export default function Thread({ messages, isRunning, onStop }) {
  return (
    <ThreadPrimitive.Root className="flex h-full flex-col">
      <ThreadPrimitive.Viewport className="flex-1 overflow-y-auto px-4 py-6">
        <div className="mx-auto flex max-w-3xl flex-col gap-5">
          {messages.length === 0 && (
            <div className="mt-16 text-center text-slate-400">
              <Bot size={32} className="mx-auto mb-3 opacity-50" />
              <p className="text-sm">
                Ask the assistant something to get started.
              </p>
            </div>
          )}
          {messages.map((m) => (
            <MessageBubble key={m.id} role={m.role} content={m.content} />
          ))}
        </div>
      </ThreadPrimitive.Viewport>

      <div className="border-t border-slate-200 bg-white px-4 py-3">
        <ComposerPrimitive.Root className="mx-auto flex max-w-3xl items-end gap-2 rounded-xl border border-slate-300 bg-white p-2 focus-within:border-brand-500">
          <ComposerPrimitive.Input
            rows={1}
            autoFocus
            placeholder={
              isRunning
                ? "Waiting for the assistant..."
                : "Message the assistant..."
            }
            disabled={isRunning}
            className="max-h-40 flex-1 resize-none bg-transparent px-2 py-1.5 text-sm outline-none placeholder:text-slate-400 disabled:cursor-not-allowed"
          />
          {isRunning ? (
            <button
              type="button"
              onClick={onStop}
              aria-label="Stop generating"
              title="Stop"
              className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-slate-800 text-white hover:bg-slate-700"
            >
              <Square size={13} fill="currentColor" />
            </button>
          ) : (
            <ComposerPrimitive.Send
              disabled={isRunning}
              className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-brand-600 text-white disabled:cursor-not-allowed disabled:opacity-40"
            >
              <ArrowUp size={16} />
            </ComposerPrimitive.Send>
          )}
        </ComposerPrimitive.Root>
      </div>
    </ThreadPrimitive.Root>
  );
}
