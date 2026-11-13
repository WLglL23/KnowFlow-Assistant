import { requestJson } from "./http";

export type KnowledgeSource = {
  id: string;
  filename: string;
  status: "PUBLISHED";
  enabled: true;
};

type KnowledgeSourceList = { items: KnowledgeSource[] };
export function listAvailableKnowledgeSources(): Promise<KnowledgeSourceList> {
  return requestJson<KnowledgeSourceList>("/api/knowledge/sources");
}
