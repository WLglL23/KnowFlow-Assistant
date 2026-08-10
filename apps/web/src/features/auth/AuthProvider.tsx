import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  type PropsWithChildren,
  useCallback,
  useEffect,
  useMemo,
  useState,
} from "react";

import {
  ApiError,
  getCurrentAccount,
  guestLogin,
  login,
  type LoginResponse,
  UNAUTHORIZED_EVENT,
} from "../../api/auth";
import { AuthContext, type AuthContextValue, type AuthStatus } from "./AuthContext";

const TOKEN_KEY = "knowflow.access_token";

function storedToken(): string | null {
  return window.sessionStorage.getItem(TOKEN_KEY);
}

export function AuthProvider({ children }: PropsWithChildren) {
  const queryClient = useQueryClient();
  const [token, setToken] = useState<string | null>(storedToken);

  const clearSession = useCallback(() => {
    window.sessionStorage.removeItem(TOKEN_KEY);
    setToken(null);
    queryClient.removeQueries({ queryKey: ["auth", "me"] });
    queryClient.removeQueries({ queryKey: ["conversations"] });
    queryClient.removeQueries({ queryKey: ["conversation"] });
  }, [queryClient]);

  useEffect(() => {
    window.addEventListener(UNAUTHORIZED_EVENT, clearSession);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, clearSession);
  }, [clearSession]);

  const meQuery = useQuery({
    queryKey: ["auth", "me"],
    queryFn: () => getCurrentAccount(token as string),
    enabled: token !== null,
  });

  const acceptLogin = useCallback(
    (response: LoginResponse) => {
      window.sessionStorage.setItem(TOKEN_KEY, response.access_token);
      queryClient.setQueryData(["auth", "me"], {
        account: response.account,
      });
      setToken(response.access_token);
    },
    [queryClient],
  );

  const loginMutation = useMutation({
    mutationFn: ({ username, password }: { username: string; password: string }) =>
      login(username, password),
    onSuccess: acceptLogin,
  });

  const guestMutation = useMutation({ mutationFn: guestLogin, onSuccess: acceptLogin });

  const loginWithPassword = useCallback(
    async (username: string, password: string) => {
      await loginMutation.mutateAsync({ username, password });
    },
    [loginMutation],
  );

  const loginAsGuest = useCallback(async () => {
    await guestMutation.mutateAsync();
  }, [guestMutation]);

  let status: AuthStatus;
  if (token === null) status = "unauthenticated";
  else if (meQuery.isPending) status = "restoring";
  else if (meQuery.isError) status = meQuery.error instanceof ApiError && meQuery.error.status === 401
    ? "unauthenticated"
    : "error";
  else status = "authenticated";

  const value = useMemo<AuthContextValue>(
    () => ({
      account: meQuery.data?.account ?? null,
      status,
      isAuthenticating: loginMutation.isPending || guestMutation.isPending,
      loginWithPassword,
      loginAsGuest,
      logout: clearSession,
      retryRestore: () => void meQuery.refetch(),
    }),
    [
      clearSession,
      guestMutation.isPending,
      loginAsGuest,
      loginMutation.isPending,
      loginWithPassword,
      meQuery,
      status,
    ],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
