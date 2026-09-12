import { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Mail, CheckCircle2, AlertTriangle, Loader2 } from "lucide-react";
import * as api from "../lib/api";

/**
 * Sidebar control that starts/reflects the backend's Gmail OAuth flow
 * (`/auth/gmail/authorize` -> Google consent -> `/auth/gmail/callback` ->
 * redirected back here with `?gmail=connected` or `?gmail=error&reason=`).
 *
 * Full-page redirect, not a popup: clicking "Connect Gmail" fetches the
 * consent URL (needs the Bearer header, which only `fetch` can attach,
 * not a plain navigation) then navigates the whole tab there. No local
 * state survives that trip - status comes back from `/auth/gmail/status`
 * on mount, and the `?gmail=...` param left by the redirect is only used
 * for a one-time inline confirmation/error message.
 */
export default function GmailConnectButton() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [status, setStatus] = useState(null); // null = loading, else { authenticated }
  const [connecting, setConnecting] = useState(false);
  const [disconnecting, setDisconnecting] = useState(false);
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null); // { type: "success" | "error", text }

  const refreshStatus = useCallback(async () => {
    try {
      const data = await api.getGmailStatus();
      setStatus(data);
    } catch (err) {
      setError(err.message || "Failed to load Gmail status");
      setStatus({ authenticated: false });
    }
  }, []);

  useEffect(() => {
    refreshStatus();
  }, [refreshStatus]);

  // One-time handling of the ?gmail=connected / ?gmail=error redirect
  // params left by /auth/gmail/callback, then strip them so a refresh
  // doesn't re-show the message.
  useEffect(() => {
    const gmail = searchParams.get("gmail");
    if (!gmail) return;

    if (gmail === "connected") {
      setNotice({ type: "success", text: "Gmail connected." });
    } else if (gmail === "error") {
      setNotice({
        type: "error",
        text: searchParams.get("reason") || "Gmail connection failed.",
      });
    }

    const next = new URLSearchParams(searchParams);
    next.delete("gmail");
    next.delete("reason");
    setSearchParams(next, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleConnect = useCallback(async () => {
    setError(null);
    setConnecting(true);
    try {
      const { auth_url } = await api.getGmailAuthorizeUrl();
      window.location.href = auth_url; // full navigation - page unloads
    } catch (err) {
      setError(err.message || "Could not start Gmail connection");
      setConnecting(false);
    }
  }, []);

  const handleDisconnect = useCallback(async () => {
    setError(null);
    setDisconnecting(true);
    try {
      await api.disconnectGmail();
      await refreshStatus();
    } catch (err) {
      setError(err.message || "Could not disconnect Gmail");
    } finally {
      setDisconnecting(false);
    }
  }, [refreshStatus]);

  return (
    <div className="mb-2">
      {notice && (
        <div
          className={`mb-2 flex items-start gap-1.5 rounded-md border px-2 py-1.5 text-xs ${
            notice.type === "success"
              ? "border-emerald-800 bg-emerald-950/40 text-emerald-300"
              : "border-red-800 bg-red-950/40 text-red-300"
          }`}
        >
          {notice.type === "success" ? (
            <CheckCircle2 size={13} className="mt-0.5 shrink-0" />
          ) : (
            <AlertTriangle size={13} className="mt-0.5 shrink-0" />
          )}
          <span>{notice.text}</span>
        </div>
      )}

      {error && (
        <div className="mb-2 flex items-start gap-1.5 rounded-md border border-red-800 bg-red-950/40 px-2 py-1.5 text-xs text-red-300">
          <AlertTriangle size={13} className="mt-0.5 shrink-0" />
          <span>{error}</span>
        </div>
      )}

      {status === null ? (
        <p className="px-1 text-xs text-slate-500">Checking Gmail…</p>
      ) : status.authenticated ? (
        <div className="flex items-center justify-between rounded-lg px-3 py-2 text-sm text-slate-300">
          <span className="flex items-center gap-2">
            <CheckCircle2 size={16} className="text-emerald-400" />
            Gmail connected
          </span>
          <button
            type="button"
            onClick={handleDisconnect}
            disabled={disconnecting}
            className="text-xs text-slate-500 hover:text-slate-300 disabled:opacity-50"
          >
            {disconnecting ? "..." : "Disconnect"}
          </button>
        </div>
      ) : (
        <button
          type="button"
          onClick={handleConnect}
          disabled={connecting}
          className="flex w-full items-center gap-2 rounded-lg border border-slate-700 px-3 py-2 text-sm text-slate-100 hover:bg-slate-800 disabled:opacity-50"
        >
          {connecting ? (
            <Loader2 size={16} className="animate-spin" />
          ) : (
            <Mail size={16} />
          )}
          {connecting ? "Redirecting…" : "Connect Gmail"}
        </button>
      )}
    </div>
  );
}
