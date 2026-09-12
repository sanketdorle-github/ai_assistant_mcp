import { Routes, Route, Navigate } from "react-router-dom";
import { AuthProvider } from "./lib/auth";
import LoginPage from "./components/LoginPage";
import RegisterPage from "./components/RegisterPage";
import ChatPage from "./components/ChatPage";
import ProtectedRoute from "./components/ProtectedRoute";

export default function App() {
  return (
    <AuthProvider>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/register" element={<RegisterPage />} />
        <Route
          path="/"
          element={
            <ProtectedRoute>
              <ChatPage />
            </ProtectedRoute>
          }
        />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </AuthProvider>
  );
}
