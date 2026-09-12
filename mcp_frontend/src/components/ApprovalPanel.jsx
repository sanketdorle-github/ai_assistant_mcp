import { useState } from "react";
import { ShieldAlert, Check, X, PencilLine } from "lucide-react";

export default function ApprovalPanel({ pendingApproval, onApprove, onDeny, isRunning }) {
  const [editing, setEditing] = useState(false);
  const [argsText, setArgsText] = useState(() =>
    JSON.stringify(pendingApproval.tool_arguments ?? {}, null, 2)
  );
  const [parseError, setParseError] = useState(null);

  const handleApprove = () => {
    if (!editing) {
      onApprove(undefined);
      return;
    }
    try {
      const parsed = JSON.parse(argsText);
      setParseError(null);
      onApprove(parsed);
    } catch (err) {
      setParseError("Arguments must be valid JSON: " + err.message);
    }
  };

  return (
    <div className="mx-auto mb-3 max-w-3xl rounded-xl border border-amber-300 bg-amber-50 p-4">
      <div className="flex items-start gap-2">
        <ShieldAlert size={18} className="mt-0.5 shrink-0 text-amber-600" />
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium text-amber-900">
            Approval needed: {pendingApproval.message}
          </p>
          <p className="mt-1 text-xs text-amber-700">
            Tool: <code>{pendingApproval.tool_name}</code>
          </p>

          {editing ? (
            <div className="mt-2">
              <textarea
                value={argsText}
                onChange={(e) => setArgsText(e.target.value)}
                rows={5}
                className="w-full rounded-lg border border-amber-300 bg-white p-2 font-mono text-xs outline-none focus:border-amber-500"
              />
              {parseError && (
                <p className="mt-1 text-xs text-red-600">{parseError}</p>
              )}
            </div>
          ) : (
            <pre className="mt-2 max-h-32 overflow-auto rounded-lg bg-white/70 p-2 text-xs text-amber-900">
              {JSON.stringify(pendingApproval.tool_arguments ?? {}, null, 2)}
            </pre>
          )}

          <div className="mt-3 flex flex-wrap gap-2">
            <button
              type="button"
              onClick={handleApprove}
              disabled={isRunning}
              className="inline-flex items-center gap-1 rounded-lg bg-green-600 px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50"
            >
              <Check size={14} /> {editing ? "Approve with edits" : "Approve"}
            </button>
            <button
              type="button"
              onClick={onDeny}
              disabled={isRunning}
              className="inline-flex items-center gap-1 rounded-lg bg-red-100 px-3 py-1.5 text-xs font-medium text-red-700 disabled:opacity-50"
            >
              <X size={14} /> Deny
            </button>
            <button
              type="button"
              onClick={() => setEditing((v) => !v)}
              disabled={isRunning}
              className="inline-flex items-center gap-1 rounded-lg border border-amber-300 px-3 py-1.5 text-xs font-medium text-amber-800 disabled:opacity-50"
            >
              <PencilLine size={14} /> {editing ? "Cancel edit" : "Edit arguments"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
