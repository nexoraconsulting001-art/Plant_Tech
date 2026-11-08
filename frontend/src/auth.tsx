// Login state: the token (kept in secure storage) and the current user with their permissions.
import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";

import { apiGet, apiPost, setApiToken, setOnUnauthorized, waitForLogin } from "@/src/api";
import { unregisterPush } from "@/src/push";
import { queryClient } from "@/src/query-client";
import { storage } from "@/src/utils/storage";

const TOKEN_KEY = "nurserypos_token_v1";

// Read the saved login as soon as the app loads, before any screen asks the server (api.ts waits for it). The
// provider below reads it again to load the user.
waitForLogin(storage.secureGet<string | null>(TOKEN_KEY, null).then((saved) => {
  if (saved) setApiToken(saved);
}));

export type Permission = "sell" | "stock" | "customers" | "reports";

export type User = {
  id: string;
  name: string;
  phone: string | null;
  email: string | null;
  role: "owner" | "worker";
  permissions: Record<Permission, boolean>;
  is_admin: boolean;
  has_password: boolean;
  business: {
    id: string; name: string; onboarding_done: boolean; gst_registered: boolean; logo_path: string | null; profit_percent: number;
  } | null;
};

type Session = { token: string; user: User };

type AuthValue = {
  ready: boolean;
  user: User | null;
  isOwner: boolean;
  can: (perm: Permission) => boolean;
  login: (phone: string, password: string) => Promise<User>;
  register: (name: string, phone: string, password: string) => Promise<User>;
  googleLogin: (idToken: string) => Promise<User>;
  setSession: (s: Session) => Promise<void>;
  refresh: () => Promise<User | null>;
  logout: () => Promise<void>;
};

const AuthContext = createContext<AuthValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [ready, setReady] = useState(false);
  const [user, setUser] = useState<User | null>(null);

  const logout = useCallback(async () => {
    await unregisterPush(); // this phone stops getting the owner's notifications (sent while still logged in)
    setApiToken(null);
    setUser(null);
    queryClient.clear();
    await storage.secureRemove(TOKEN_KEY);
  }, []);

  const setSession = useCallback(async ({ token, user: u }: Session) => {
    setApiToken(token);
    await storage.secureSet(TOKEN_KEY, token);
    queryClient.clear();
    setUser(u);
  }, []);

  const refresh = useCallback(async () => {
    try {
      const u: User = await apiGet("/auth/me");
      setUser(u);
      return u;
    } catch {
      return null;
    }
  }, []);

  useEffect(() => {
    setOnUnauthorized(() => {
      logout();
    });
    (async () => {
      const saved = await storage.secureGet<string | null>(TOKEN_KEY, null);
      if (saved) {
        setApiToken(saved);
        try {
          setUser(await apiGet("/auth/me"));
        } catch {
          // offline or expired: an expired token was already cleared by the 401 handler
        }
      }
      setReady(true);
    })();
    return () => setOnUnauthorized(null);
  }, [logout]);

  const value = useMemo<AuthValue>(() => {
    const finish = async (s: Session) => {
      await setSession(s);
      return s.user;
    };
    return {
      ready,
      user,
      isOwner: user?.role === "owner",
      can: (perm) => !!user?.permissions?.[perm],
      login: async (phone, password) => finish(await apiPost("/auth/login", { phone, password })),
      register: async (name, phone, password) => finish(await apiPost("/auth/register", { name, phone, password })),
      googleLogin: async (idToken) => finish(await apiPost("/auth/google", { id_token: idToken })),
      setSession,
      refresh,
      logout,
    };
  }, [ready, user, setSession, refresh, logout]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside AuthProvider");
  return ctx;
}
