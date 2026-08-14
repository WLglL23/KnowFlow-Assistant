import { ApiError, UNAUTHORIZED_EVENT } from "./auth";

export type SubmissionStatus =
  | "PENDING_REVIEW"
  | "REJECTED"
  | "PUBLISHING"
  | "PUBLISHED"
  | "PUBLISH_FAILED";

export type Submission = {
  id: string;
  filename: string;
  size_bytes: number;
  page_count: number;
  status: SubmissionStatus;
  rejection_reason: string | null;
  processing_stage: string | null;
  processing_progress: number | null;
  processing_error_code: string | null;
  created_at: string;
  reviewed_at: string | null;
  published_at: string | null;
};

export type SubmissionList = { items: Submission[] };
export type SubmissionUploadResponse =
  | { result: "CREATED"; submission: Submission }
  | { result: "ALREADY_ACTIVE" };

type ErrorEnvelope = {
  error?: { code?: string; message?: string; details?: { reason?: string } };
  request_id?: string;
};

const tokenKey = "knowflow.access_token";

async function parseError(response: Response): Promise<never> {
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

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      Accept: "application/json",
      Authorization: `Bearer ${window.sessionStorage.getItem(tokenKey) ?? ""}`,
      ...init.headers,
    },
  });
  if (!response.ok) return parseError(response);
  return (await response.json()) as T;
}

export function submitKnowledge(file: File): Promise<SubmissionUploadResponse> {
  const body = new FormData();
  body.append("file", file);
  return request<SubmissionUploadResponse>("/api/knowledge/submissions", {
    method: "POST",
    body,
  });
}

export const listMySubmissions = () =>
  request<SubmissionList>("/api/knowledge/submissions/mine");
