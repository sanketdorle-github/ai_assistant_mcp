import { AssistantRuntimeProvider } from "@assistant-ui/react";
import { AlertTriangle } from "lucide-react";
import Sidebar from "./Sidebar";
import Thread from "./Thread";
import PlanPanel from "./PlanPanel";
import ApprovalPanel from "./ApprovalPanel";
import { useChatRuntime } from "../lib/useChatRuntime";

export default function ChatPage() {
  const {
    runtime,
    threadId,
    messages,
    planSteps,
    pendingApproval,
    isRunning,
    streamError,
    approve,
    deny,
    stopChat,
    startNewThread,
    loadThread,
  } = useChatRuntime();

  return (
    <div className="flex h-screen w-screen overflow-hidden">
      <Sidebar
        activeThreadId={threadId}
        onNewThread={startNewThread}
        onSelectThread={loadThread}
      />

      <main className="flex min-w-0 flex-1 flex-col bg-slate-50">
        <header className="flex shrink-0 items-center justify-between border-b border-slate-200 bg-white px-4 py-3">
          <div>
            <h1 className="text-sm font-semibold text-slate-800">
              Oreo Personal Assistant
            </h1>
            <p className="truncate text-xs text-slate-400">Chat : {threadId}</p>
          </div>
        </header>

        {streamError && (
          <div className="mx-4 mt-3 flex items-start gap-2 rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700">
            <AlertTriangle size={16} className="mt-0.5 shrink-0" />
            <span>{streamError}</span>
          </div>
        )}

        <div className="min-h-0 flex-1">
          <AssistantRuntimeProvider runtime={runtime}>
            <Thread
              messages={messages}
              isRunning={isRunning}
              onStop={stopChat}
            />
          </AssistantRuntimeProvider>
        </div>

        {pendingApproval && (
          <div className="px-4 pb-3">
            <ApprovalPanel
              pendingApproval={pendingApproval}
              onApprove={approve}
              onDeny={deny}
              isRunning={isRunning}
            />
          </div>
        )}
      </main>

      <aside className="hidden w-72 shrink-0 overflow-y-auto border-l border-slate-200 bg-slate-50 lg:block">
        <PlanPanel planSteps={planSteps} />
      </aside>
    </div>
  );
}
