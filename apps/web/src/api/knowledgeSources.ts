import { ApiError, UNAUTHORIZED_EVENT } from "./auth";

export type KnowledgeSource = {
  id: string;
  filename: string;
  status: "PUBLISHED";
  enabled: true;
};

type KnowledgeSourceList = { items: KnowledgeSource[] };
type ErrorEnvelope = { error?: { code?: string; message?: string }; request_id?: string };

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

export async function listAvailableKnowledgeSources(): Promise<KnowledgeSourceList> {
  const response = await fetch("/api/knowledge/sources", {
    headers: {
      Accept: "application/json",
      Authorization: `Bearer ${window.sessionStorage.getItem("knowflow.access_token") ?? ""}`,
    },
  });
  if (!response.ok) return parseError(response);
  return response.json() as Promise<KnowledgeSourceList>;
}
