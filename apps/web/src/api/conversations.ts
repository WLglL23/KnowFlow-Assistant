import { ApiError, UNAUTHORIZED_EVENT } from "./auth";

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
  citations: unknown[];
  created_at: string;
};

export type ConversationDetail = Conversation & { messages: Message[] };
export type ConversationList = { items: Conversation[] };

type ErrorEnvelope = { error?: { code?: string; message?: string }; request_id?: string };
const tokenKey = "knowflow.access_token";

function token(): string {
  return window.sessionStorage.getItem(tokenKey) ?? "";
}

async function parseError(response: Response): Promise<never> {
  const body = (await response.json().catch(() => null)) as ErrorEnvelope | null;
  if (response.status === 401) window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
  throw new ApiError(
    response.status,
    body?.error?.code ?? "REQUEST_FAILED",
    body?.error?.message ?? "请求未完成",
    body?.request_id,
  );
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      Accept: "application/json",
      Authorization: `Bearer ${token()}`,
      ...(init.body ? { "Content-Type": "application/json" } : {}),
      ...init.headers,
    },
  });
  if (!response.ok) return parseError(response);
  return (await response.json()) as T;
}

export const listConversations = () => request<ConversationList>("/api/conversations");
export const createConversation = () => request<Conversation>("/api/conversations", { method: "POST" });
export const getConversation = (id: string) => request<ConversationDetail>(`/api/conversations/${id}`);
export const renameConversation = (id: string, title: string) =>
  request<Conversation>(`/api/conversations/${id}`, { method: "PATCH", body: JSON.stringify({ title }) });

export type RunEvent = {
  type: "stage.started" | "stage.completed" | "answer.delta" | "run.completed" | "run.failed";
  request_id: string;
  run_id: string;
  stage?: "UNDERSTAND" | "PLAN" | "RETRIEVE_1" | "VERIFY" | "RETRIEVE_2" | "GENERATE";
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
  const response = await fetch(`/api/conversations/${id}/runs`, {
    method: "POST",
    headers: { Accept: "text/event-stream", "Content-Type": "application/json", Authorization: `Bearer ${token()}` },
    body: JSON.stringify({ content, client_request_id: clientRequestId }),
  });
  if (!response.ok) return parseError(response);
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
