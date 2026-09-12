import { CheckCircle2, CircleDashed, Loader2, XCircle, ShieldAlert } from "lucide-react";

const STATUS_ICON = {
  pending: <CircleDashed size={16} className="text-slate-300" />,
  active: <Loader2 size={16} className="animate-spin text-brand-600" />,
  completed: <CheckCircle2 size={16} className="text-green-600" />,
  failed: <XCircle size={16} className="text-red-500" />,
};

export default function PlanPanel({ planSteps }) {
  if (!planSteps || planSteps.length === 0) {
    return (
      <div className="p-4 text-sm text-slate-400">
        No active plan yet. The planner's steps will appear here once you send
        a message.
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-3 p-4">
      <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-400">
        Execution plan
      </h3>
      <ol className="flex flex-col gap-2">
        {planSteps.map((step) => (
          <li
            key={step.id}
            className="rounded-lg border border-slate-200 bg-white p-3 text-sm"
          >
            <div className="flex items-start gap-2">
              <span className="mt-0.5">
                {STATUS_ICON[step.status] || STATUS_ICON.pending}
              </span>
              <div className="min-w-0 flex-1">
                <p className="font-medium text-slate-800">{step.description}</p>
                {step.tool_name && (
                  <p className="mt-0.5 truncate text-xs text-slate-500">
                    tool: <code>{step.tool_name}</code>
                  </p>
                )}
                {step.requires_approval && (
                  <p className="mt-0.5 flex items-center gap-1 text-xs text-amber-600">
                    <ShieldAlert size={12} /> requires approval
                  </p>
                )}
                {step.result && step.status === "completed" && (
                  <p className="mt-1 line-clamp-3 text-xs text-slate-500">
                    {String(step.result)}
                  </p>
                )}
                {step.status === "failed" && step.result && (
                  <p className="mt-1 text-xs text-red-500">{String(step.result)}</p>
                )}
              </div>
            </div>
          </li>
        ))}
      </ol>
    </div>
  );
}
