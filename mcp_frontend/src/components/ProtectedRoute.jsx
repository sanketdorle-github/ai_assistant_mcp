import { Navigate, useLocation } from "react-router-dom";
import { Loader2 } from "lucide-react";
import { useAuth } from "../lib/auth";

export default function ProtectedRoute({ children }) {
  const { status } = useAuth();
  const location = useLocation();

  if (status === "loading") {
    return (
      <div className="flex min-h-screen items-center justify-center text-slate-400">
        <Loader2 className="animate-spin" size={20} />
      </div>
    );
  }

  if (status === "anonymous") {
    return <Navigate to="/login" state={{ from: location }} replace />;
  }

  return children;
}
