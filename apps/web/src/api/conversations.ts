import { request, requestJson } from "./http";
import type { RunStage } from "./runStages";

export type Conversation = {
  id: string;
  title: string;
  knowledge_mode: "DISABLED" | "ALL" | "SELECTED";
  selected_source_ids: string[];
  created_at: string;
  updated_at: string;
};

export type Message = {
  id: string;
  role: "USER" | "ASSISTANT";
  content: string;
  status: "PENDING" | "COMPLETE" | "PARTIAL";
  citations: Citation[];
  created_at: string;
};

export type Citation = {
  source_type: "ATTACHMENT" | "KNOWLEDGE_SOURCE";
  source_id: string;
  filename: string;
  pages: number[];
};

export type AttachmentStatus = "QUEUED" | "PROCESSING" | "READY" | "FAILED";
export type AttachmentStage = "PARSING" | "CHUNKING" | "EMBEDDING" | "INDEXING";

export type Attachment = {
  id: string;
  filename: string;
  size_bytes: number;
  page_count: number;
  status: AttachmentStatus;
  processing_stage: AttachmentStage | null;
  processing_progress: number | null;
  processing_error_code: string | null;
  created_at: string;
};

export type AttachmentList = { items: Attachment[] };
export type AttachmentUploadResult =
  | "CREATED"
  | "EXISTING_QUEUED"
  | "EXISTING_PROCESSING"
  | "ALREADY_EXISTS"
  | "RETRY_QUEUED";
export type AttachmentUploadResponse = { result: AttachmentUploadResult; attachment: Attachment };

export type ConversationDetail = Conversation & { messages: Message[] };
export type ConversationList = { items: Conversation[] };

export const listConversations = () => requestJson<ConversationList>("/api/conversations");
export const createConversation = () => requestJson<Conversation>("/api/conversations", { method: "POST" });
export const getConversation = (id: string) => requestJson<ConversationDetail>(`/api/conversations/${id}`);
export const deleteConversation = (id: string) => requestJson<void>(`/api/conversations/${id}`, { method: "DELETE" });
export const renameConversation = (id: string, title: string) =>
  requestJson<Conversation>(`/api/conversations/${id}`, { method: "PATCH", body: JSON.stringify({ title }) });
export const updateKnowledgeScope = (
  id: string,
  knowledgeMode: Conversation["knowledge_mode"],
  selectedSourceIds: string[],
) => requestJson<Conversation>(`/api/conversations/${id}`, {
  method: "PATCH",
  body: JSON.stringify({ knowledge_mode: knowledgeMode, selected_source_ids: selectedSourceIds }),
});
export const listAttachments = (conversationId: string) =>
  requestJson<AttachmentList>(`/api/conversations/${conversationId}/attachments`);
export const deleteAttachment = (conversationId: string, attachmentId: string) =>
  requestJson<void>(`/api/conversations/${conversationId}/attachments/${attachmentId}`, { method: "DELETE" });
export const uploadAttachment = (conversationId: string, file: File) => {
  const body = new FormData();
  body.append("file", file);
  return requestJson<AttachmentUploadResponse>(`/api/conversations/${conversationId}/attachments`, {
    method: "POST",
    body,
  });
};

export async function getCitationPreview(messageId: string, citationIndex: number): Promise<Blob> {
  const response = await request(`/api/messages/${messageId}/citations/${citationIndex}/preview`, {
    headers: { Accept: "application/pdf" },
  });
  return response.blob();
}

export type RunEvent = {
  type: "stage.started" | "stage.completed" | "answer.delta" | "run.completed" | "run.failed";
  request_id: string;
  run_id: string;
  stage?: RunStage;
  delta?: string;
  error?: { code: string; message: string };
};

const runEventTypes = new Set<RunEvent["type"]>([
  "stage.started",
  "stage.completed",
  "answer.delta",
  "run.completed",
  "run.failed",
]);

export async function streamRun(
  id: string,
  content: string,
  clientRequestId: string,
  onEvent: (event: RunEvent) => void,
): Promise<void> {
  const response = await request(`/api/conversations/${id}/runs`, {
    method: "POST",
    headers: { Accept: "text/event-stream" },
    body: JSON.stringify({ content, client_request_id: clientRequestId }),
  });
  if (!response.body) throw new Error("连接未返回流式内容");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    buffer = (buffer + decoder.decode(value, { stream: !done })).replace(/\r\n/g, "\n");
    const frames = buffer.split("\n\n");
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      const eventType = frame.match(/^event:\s*(.+)$/m)?.[1]?.trim();
      const data = frame.match(/^data:\s*(.+)$/m)?.[1];
      if (!eventType || !data) continue;
      try {
        const parsed = JSON.parse(data) as Omit<RunEvent, "type">;
        if (!runEventTypes.has(eventType as RunEvent["type"])) continue;
        if (typeof parsed.request_id !== "string" || typeof parsed.run_id !== "string") continue;
        onEvent({ ...parsed, type: eventType as RunEvent["type"] });
      } catch {
        // Ignore malformed frames; persisted detail remains the source of truth.
      }
    }
    if (done) break;
  }
}
