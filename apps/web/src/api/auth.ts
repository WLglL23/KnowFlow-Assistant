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

interface ErrorEnvelope {
  error?: {
    code?: string;
    message?: string;
  };
  request_id?: string;
}

export const UNAUTHORIZED_EVENT = "knowflow:unauthorized";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly requestId?: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      Accept: "application/json",
      ...(init.body ? { "Content-Type": "application/json" } : {}),
      ...init.headers,
    },
  });

  const body = (await response.json().catch(() => null)) as ErrorEnvelope | T | null;
  if (!response.ok) {
    const envelope = body as ErrorEnvelope | null;
    if (response.status === 401) window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
    throw new ApiError(
      response.status,
      envelope?.error?.code ?? "REQUEST_FAILED",
      envelope?.error?.message ?? "请求未完成",
      envelope?.request_id,
    );
  }
  return body as T;
}

export function login(username: string, password: string): Promise<LoginResponse> {
  return request<LoginResponse>("/api/auth/login", {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });
}

export function guestLogin(): Promise<LoginResponse> {
  return request<LoginResponse>("/api/auth/guest", { method: "POST" });
}

export function getCurrentAccount(token: string): Promise<MeResponse> {
  return request<MeResponse>("/api/auth/me", {
    headers: { Authorization: `Bearer ${token}` },
  });
}
