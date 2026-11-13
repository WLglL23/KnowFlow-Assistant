import { request, requestJson } from "./http";
import type { Submission, SubmissionList } from "./submissions";

export type KnowledgeSource = {
  id: string;
  filename: string;
  status: "PUBLISHED";
  enabled: boolean;
};

export type CleanupFailure = {
  resource_type: "ATTACHMENT" | "SUBMISSION";
  resource_id: string;
  step: "ES" | "MINIO";
  status: "PENDING" | "FAILED";
  error_code: string | null;
  failed_at: string | null;
};

export type CleanupFailureList = { items: CleanupFailure[] };

type KnowledgeSourceList = { items: KnowledgeSource[] };
async function download(path: string, filename: string): Promise<void> {
  const response = await request(path, {
    headers: { Accept: "application/pdf" },
  });

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
  requestJson<SubmissionList>("/api/admin/knowledge/submissions");

export const approveSubmission = (submissionId: string) =>
  requestJson<Submission>(`/api/admin/knowledge/submissions/${submissionId}/approve`, {
    method: "POST",
  });

export const rejectSubmission = (submissionId: string) =>
  requestJson<Submission>(`/api/admin/knowledge/submissions/${submissionId}/reject`, {
    method: "POST",
  });

export const listKnowledgeSources = () =>
  requestJson<KnowledgeSourceList>("/api/admin/knowledge/sources");

export const setKnowledgeSourceEnabled = (sourceId: string, enabled: boolean) =>
  requestJson<KnowledgeSource>(`/api/admin/knowledge/sources/${sourceId}`, {
    method: "PATCH",
    body: JSON.stringify({ enabled }),
  });

export const downloadSubmissionDocument = (submissionId: string, filename: string) =>
  download(`/api/admin/knowledge/submissions/${submissionId}/document`, filename);

export const downloadSourceDocument = (sourceId: string, filename: string) =>
  download(`/api/admin/knowledge/sources/${sourceId}/document`, filename);

export const deleteKnowledgeSource = (sourceId: string) =>
  requestJson<void>(`/api/admin/knowledge/sources/${sourceId}`, { method: "DELETE" });

export const listCleanupFailures = () =>
  requestJson<CleanupFailureList>("/api/admin/cleanup-failures");

export const retryCleanup = (resourceType: CleanupFailure["resource_type"], resourceId: string) =>
  requestJson<Record<string, unknown>>(`/api/admin/cleanup-failures/${resourceType}/${resourceId}/retry`, { method: "POST" });
