import { requestJson } from "./http";

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

export function submitKnowledge(file: File): Promise<SubmissionUploadResponse> {
  const body = new FormData();
  body.append("file", file);
  return requestJson<SubmissionUploadResponse>("/api/knowledge/submissions", {
    method: "POST",
    body,
  });
}

export const listMySubmissions = () =>
  requestJson<SubmissionList>("/api/knowledge/submissions/mine");

export const deleteSubmission = (submissionId: string) =>
  requestJson<void>(`/api/knowledge/submissions/${submissionId}`, { method: "DELETE" });
