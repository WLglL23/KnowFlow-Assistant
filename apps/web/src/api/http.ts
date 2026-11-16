export const TOKEN_KEY = "knowflow.access_token";
export const UNAUTHORIZED_EVENT = "knowflow:unauthorized";

type ErrorEnvelope = {
  error?: { code?: string; message?: string; details?: { reason?: string } };
  request_id?: string;
};

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly requestId?: string,
    readonly details?: { reason?: string },
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export async function request(
  path: string,
  init: RequestInit = {},
  authenticated = true,
): Promise<Response> {
  const headers = new Headers(init.headers);
  if (!headers.has("Accept")) headers.set("Accept", "application/json");
  if (authenticated && !headers.has("Authorization")) {
    headers.set("Authorization", `Bearer ${window.sessionStorage.getItem(TOKEN_KEY) ?? ""}`);
  }
  if (init.body && !(init.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  const response = await fetch(path, { ...init, headers });
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as ErrorEnvelope | null;
    if (response.status === 401) window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
    throw new ApiError(
      response.status,
      body?.error?.code ?? "REQUEST_FAILED",
      body?.error?.message ?? "请求未完成",
      body?.request_id,
      body?.error?.details,
    );
  }
  return response;
}

export async function requestJson<T>(
  path: string,
  init: RequestInit = {},
  authenticated = true,
): Promise<T> {
  const response = await request(path, init, authenticated);
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}
