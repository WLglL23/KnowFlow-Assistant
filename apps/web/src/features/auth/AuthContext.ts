import { createContext, useContext } from "react";

import type { Account } from "../../api/auth";

export type AuthStatus = "restoring" | "unauthenticated" | "authenticated" | "error";

export interface AuthContextValue {
  account: Account | null;
  status: AuthStatus;
  isAuthenticating: boolean;
  loginWithPassword: (username: string, password: string) => Promise<void>;
  loginAsGuest: () => Promise<void>;
  logout: () => void;
  retryRestore: () => void;
}

export const AuthContext = createContext<AuthContextValue | null>(null);

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (context === null) throw new Error("useAuth must be used within AuthProvider");
  return context;
}
