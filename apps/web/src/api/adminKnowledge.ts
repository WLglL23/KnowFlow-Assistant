import { ApiError, UNAUTHORIZED_EVENT } from "./auth";
import type { Submission, SubmissionList } from "./submissions";

export type KnowledgeSource = {
  id: string;
  filename: string;
  status: "PUBLISHED";
  enabled: boolean;
};

type KnowledgeSourceList = { items: KnowledgeSource[] };
type ErrorEnvelope = {
  error?: { code?: string; message?: string; details?: { reason?: string } };
  request_id?: string;
};

const tokenKey = "knowflow.access_token";

function authorization(): string {
  return `Bearer ${window.sessionStorage.getItem(tokenKey) ?? ""}`;
}

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
      Authorization: authorization(),
      ...init.headers,
    },
  });
  if (!response.ok) return parseError(response);
  return (await response.json()) as T;
}

async function download(path: string, filename: string): Promise<void> {
  const response = await fetch(path, {
    headers: { Accept: "application/pdf", Authorization: authorization() },
  });
  if (!response.ok) return parseError(response);

  const url = URL.createObjectURL(await response.blob());
  try {
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    anchor.click();
  } finally {
    URL.revokeObjectURL(url);
  }
}

export const listAdminSubmissions = () =>
  request<SubmissionList>("/api/admin/knowledge/submissions");

export const approveSubmission = (submissionId: string) =>
  request<Submission>(`/api/admin/knowledge/submissions/${submissionId}/approve`, {
    method: "POST",
  });

export const rejectSubmission = (submissionId: string) =>
  request<Submission>(`/api/admin/knowledge/submissions/${submissionId}/reject`, {
    method: "POST",
  });

export const listKnowledgeSources = () =>
  request<KnowledgeSourceList>("/api/admin/knowledge/sources");

export const setKnowledgeSourceEnabled = (sourceId: string, enabled: boolean) =>
  request<KnowledgeSource>(`/api/admin/knowledge/sources/${sourceId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });

export const downloadSubmissionDocument = (submissionId: string, filename: string) =>
  download(`/api/admin/knowledge/submissions/${submissionId}/document`, filename);

export const downloadSourceDocument = (sourceId: string, filename: string) =>
  download(`/api/admin/knowledge/sources/${sourceId}/document`, filename);
