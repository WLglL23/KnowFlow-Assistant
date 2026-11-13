import { requestJson } from "./http";

export { ApiError, TOKEN_KEY, UNAUTHORIZED_EVENT } from "./http";

export type AccountRole = "ADMIN" | "EMPLOYEE";

export interface Account {
  id: string;
  display_name: string;
  username: string;
  role: AccountRole;
  is_guest: boolean;
}

export interface LoginResponse {
  access_token: string;
  token_type: "bearer";
  expires_in: 28800;
  account: Account;
}

export interface MeResponse {
  account: Account;
}

export function login(username: string, password: string): Promise<LoginResponse> {
  return requestJson<LoginResponse>(
    "/api/auth/login",
    { method: "POST", body: JSON.stringify({ username, password }) },
    false,
  );
}

export function guestLogin(): Promise<LoginResponse> {
  return requestJson<LoginResponse>("/api/auth/guest", { method: "POST" }, false);
}

export function getCurrentAccount(token: string): Promise<MeResponse> {
  return requestJson<MeResponse>("/api/auth/me", {
    headers: { Authorization: `Bearer ${token}` },
  });
}
