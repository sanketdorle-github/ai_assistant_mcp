import { useEffect, useState, useCallback } from "react";
import {
  MessageSquarePlus,
  LogOut,
  MessageSquare,
  RefreshCcw,
} from "lucide-react";
import * as api from "../lib/api";
import { useAuth } from "../lib/auth";
import GmailConnectButton from "./GmailConnectButton";

export default function Sidebar({
  activeThreadId,
  onSelectThread,
  onNewThread,
}) {
  const { user, logout } = useAuth();
  const [threads, setThreads] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.listThreads();
      setThreads(data);
    } catch (err) {
      setError(err.message || "Failed to load threads");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // Re-check the thread list whenever a new thread is created locally, so
  // it shows up here once the backend has actually persisted a checkpoint
  // for it (i.e. after the first message completes).
  useEffect(() => {
    if (!activeThreadId) return;
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeThreadId]);

  return (
    <aside className="flex h-full w-64 shrink-0 flex-col border-r border-slate-200 bg-slate-900 text-slate-100">
      <div className="p-3">
        <button
          type="button"
          onClick={onNewThread}
          className="flex w-full items-center gap-2 rounded-lg border border-slate-700 px-3 py-2 text-sm hover:bg-slate-800"
        >
          <MessageSquarePlus size={16} /> New chat
        </button>
      </div>

      <div className="flex items-center justify-between px-3 pb-1 pt-2">
        <span className="text-xs font-semibold uppercase tracking-wide text-slate-400">
          Chats
        </span>
        <button
          type="button"
          onClick={refresh}
          title="Refresh"
          className="text-slate-400 hover:text-slate-200"
        >
          <RefreshCcw size={14} />
        </button>
      </div>

      <div className="flex-1 overflow-y-auto px-2 pb-2">
        {loading && (
          <p className="px-2 py-2 text-xs text-slate-500">Loading...</p>
        )}
        {error && <p className="px-2 py-2 text-xs text-red-400">{error}</p>}
        {!loading && !error && threads.length === 0 && (
          <p className="px-2 py-2 text-xs text-slate-500">No threads yet.</p>
        )}
        <ul className="flex flex-col gap-1">
          {threads.map((t) => (
            <li key={t.thread_id}>
              <button
                type="button"
                onClick={() => onSelectThread(t.thread_id)}
                className={`flex w-full items-center gap-2 truncate rounded-lg px-3 py-2 text-left text-sm hover:bg-slate-800 ${
                  t.thread_id === activeThreadId ? "bg-slate-800" : ""
                }`}
                title={t.thread_id}
              >
                <MessageSquare size={14} className="shrink-0 text-slate-400" />
                <span className="truncate">{t.thread_id}</span>
              </button>
            </li>
          ))}
        </ul>
      </div>

      <div className="border-t border-slate-800 p-3">
        <GmailConnectButton />
        <p className="truncate text-xs text-slate-400">{user?.email}</p>
        <button
          type="button"
          onClick={logout}
          className="mt-2 flex w-full items-center gap-2 rounded-lg px-3 py-2 text-sm text-slate-300 hover:bg-slate-800"
        >
          <LogOut size={16} /> Log out
        </button>
      </div>
    </aside>
  );
}
