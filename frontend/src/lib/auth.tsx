import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { api, setUnauthorizedHandler, tokenStore } from "./api";
import type { User } from "./types";

interface AuthState {
  user: User | null;
  loading: boolean;
  login: (email: string, password: string) => Promise<void>;
  signup: (name: string, email: string, password: string) => Promise<"approved" | "pending">;
  logout: () => void;
  setSession: (token: string, user: User) => void;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(!!tokenStore.get());

  const logout = useCallback(() => {
    tokenStore.set(null);
    setUser(null);
  }, []);

  const setSession = useCallback((token: string, u: User) => {
    tokenStore.set(token);
    setUser(u);
  }, []);

  useEffect(() => {
    setUnauthorizedHandler(logout);
    if (!tokenStore.get()) return;
    api<User>("/auth/me")
      .then(setUser)
      .catch(() => logout())
      .finally(() => setLoading(false));
  }, [logout]);

  const login = useCallback(async (email: string, password: string) => {
    const r = await api<{ token: string; user: User }>("/auth/login", { body: { email, password } });
    setSession(r.token, r.user);
  }, [setSession]);

  const signup = useCallback(async (name: string, email: string, password: string) => {
    const r = await api<{ status: "approved" | "pending"; token?: string; user?: User }>("/auth/signup", {
      body: { name, email, password },
    });
    if (r.status === "approved" && r.token && r.user) setSession(r.token, r.user);
    return r.status;
  }, [setSession]);

  const value = useMemo(() => ({ user, loading, login, signup, logout, setSession }), [user, loading, login, signup, logout, setSession]);
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth outside AuthProvider");
  return ctx;
}
