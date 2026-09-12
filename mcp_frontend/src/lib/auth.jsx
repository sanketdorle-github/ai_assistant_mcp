import { createContext, useCallback, useContext, useEffect, useState } from "react";
import * as api from "./api";

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  // "loading" | "authenticated" | "anonymous"
  const [status, setStatus] = useState("loading");

  const restore = useCallback(async () => {
    const token = api.getToken();
    if (!token) {
      setStatus("anonymous");
      return;
    }
    try {
      const me = await api.getMe();
      setUser(me);
      setStatus("authenticated");
    } catch {
      api.setToken(null);
      setUser(null);
      setStatus("anonymous");
    }
  }, []);

  useEffect(() => {
    restore();
  }, [restore]);

  const login = useCallback(async (email, password) => {
    const { access_token } = await api.login({ email, password });
    api.setToken(access_token);
    const me = await api.getMe();
    setUser(me);
    setStatus("authenticated");
    return me;
  }, []);

  const register = useCallback(async (name, email, password) => {
    await api.register({ name, email, password });
    // Registration succeeds but doesn't return a token, so log in right after.
    return login(email, password);
  }, [login]);

  const logout = useCallback(() => {
    api.setToken(null);
    setUser(null);
    setStatus("anonymous");
  }, []);

  return (
    <AuthContext.Provider value={{ user, status, login, register, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within an AuthProvider");
  return ctx;
}
