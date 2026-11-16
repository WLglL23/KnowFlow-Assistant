import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertCircle, Check, X } from "lucide-react";
import { lazy, Suspense, useEffect, useMemo, useState } from "react";

import { ApiError, type Account } from "../../api/auth";
import { listAvailableKnowledgeSources, type KnowledgeSource } from "../../api/knowledgeSources";
import { listAttachments, updateKnowledgeScope, type Conversation } from "../../api/conversations";
import type { CitationSelection } from "../citations/CitationPreviewPanel";
import { KnowledgeSubmissionsWorkspace } from "../knowledge/submissions/KnowledgeSubmissionsWorkspace";

const CitationPreviewPanel = lazy(async () => ({
  default: (await import("../citations/CitationPreviewPanel")).CitationPreviewPanel,
}));

type ContextTab = "sources" | "submissions" | "citation";

function scopeError(error: unknown): string {
  if (!(error instanceof ApiError)) return "知识库设置未保存，请稍后重试";
  if (error.code === "SOURCE_LIMIT_EXCEEDED") return "来源名额已满";
  if (error.code === "SOURCE_NOT_AVAILABLE") return "该知识源当前不可用，已刷新列表";
  if (error.status === 403) return "当前身份无权使用知识库";
  return "知识库设置未保存，请稍后重试";
}

export function KnowledgeContextSidebar({
  account,
  conversation,
  citationSelection,
  onCitationClose,
  onConversationUpdated,
}: {
  account: Account;
  conversation: Conversation | undefined;
  citationSelection: CitationSelection | null;
  onCitationClose: () => void;
  onConversationUpdated: () => Promise<void>;
}) {
  const client = useQueryClient();
  const [tab, setTab] = useState<ContextTab>("sources");
  const [scopeFeedback, setScopeFeedback] = useState<string | null>(null);
  const sources = useQuery({
    queryKey: ["knowledge", "available-sources"],
    queryFn: listAvailableKnowledgeSources,
    enabled: !account.is_guest && tab === "sources",
  });
  const attachments = useQuery({
    queryKey: ["attachments", account.id, conversation?.id],
    queryFn: () => listAttachments(conversation?.id ?? ""),
    enabled: conversation !== undefined && tab === "sources",
  });
  const update = useMutation({
    mutationFn: ({ mode, sourceIds }: { mode: Conversation["knowledge_mode"]; sourceIds: string[] }) =>
      updateKnowledgeScope(conversation?.id ?? "", mode, sourceIds),
    onSuccess: async () => {
      setScopeFeedback(null);
      await Promise.all([
        onConversationUpdated(),
        client.invalidateQueries({ queryKey: ["conversations", account.id] }),
      ]);
    },
    onError: async (error) => {
      setScopeFeedback(scopeError(error));
      await Promise.all([
        onConversationUpdated(),
        client.invalidateQueries({ queryKey: ["knowledge", "available-sources"] }),
      ]);
    },
  });

  useEffect(() => {
    if (citationSelection) setTab("citation");
  }, [citationSelection]);

  const selectedIds = conversation?.knowledge_mode === "SELECTED" ? conversation.selected_source_ids : [];
  const sourceById = useMemo(() => new Map(sources.data?.items.map((source) => [source.id, source])), [sources.data]);
  const selectedSources = selectedIds.flatMap((id) => {
    const source = sourceById.get(id);
    return source ? [source] : [];
  });
  const availableSources = (sources.data?.items ?? []).filter((source) => !selectedIds.includes(source.id));
  const occupyingAttachments = attachments.data?.items.filter((item) => item.status !== "FAILED").length ?? 0;
  const enabled = conversation?.knowledge_mode !== "DISABLED";
  const atLimit = occupyingAttachments + selectedIds.length >= 5;
  const disabled = !conversation || update.isPending;

  function setScope(mode: Conversation["knowledge_mode"], sourceIds: string[]) {
    if (!conversation) return;
    setScopeFeedback(null);
    update.mutate({ mode, sourceIds });
  }

  function removeSource(source: KnowledgeSource) {
    const next = selectedIds.filter((id) => id !== source.id);
    setScope(next.length ? "SELECTED" : "ALL", next);
  }

  function addSource(source: KnowledgeSource) {
    setScope("SELECTED", [...selectedIds, source.id]);
  }

  function closeCitation() {
    onCitationClose();
    setTab("sources");
  }

  return (
    <aside className="knowledge-context-sidebar" aria-label="知识库与投稿">
      {tab !== "citation" && (
        <nav className="knowledge-context-tabs" aria-label="知识上下文">
          <button className={tab === "sources" ? "active" : ""} type="button" onClick={() => setTab("sources")}>知识库</button>
          <button className={tab === "submissions" ? "active" : ""} type="button" onClick={() => setTab("submissions")}>投稿</button>
        </nav>
      )}
      {tab === "citation" && citationSelection && (
        <Suspense fallback={<div className="citation-state">正在打开引用预览…</div>}>
          <CitationPreviewPanel selection={citationSelection} onClose={closeCitation} />
        </Suspense>
      )}
      {tab === "sources" && (
        <div className="knowledge-context-content">
          <div className="knowledge-toggle-row">
            <button className={`knowledge-toggle ${enabled ? "enabled" : ""}`} type="button" onClick={() => setScope(enabled ? "DISABLED" : "ALL", [])} disabled={disabled} aria-pressed={enabled} aria-label="启用知识库">
              <span />
            </button>
            <span>启用知识库</span>
          </div>
          {!account.is_guest && enabled && (
            <>
              {scopeFeedback && <div className="knowledge-context-feedback" role="alert"><AlertCircle size={15} />{scopeFeedback}</div>}
              {sources.isError && <div className="knowledge-context-feedback" role="alert"><AlertCircle size={15} />知识库暂时无法加载</div>}
              {selectedSources.length > 0 && <section className="knowledge-source-group"><h3>已选择</h3><ul>{selectedSources.map((source) => <li className="knowledge-source-row" key={source.id}><span className="knowledge-source-name" title={source.filename}>{source.filename}</span><button className="knowledge-source-action remove" type="button" disabled={disabled} aria-label={`取消选择 ${source.filename}`} title={`取消选择 ${source.filename}`} onClick={() => removeSource(source)}><X size={16} /></button></li>)}</ul></section>}
              <section className="knowledge-source-group"><h3>可选</h3>{sources.isPending ? <p className="knowledge-context-state">正在加载…</p> : <ul>{availableSources.map((source) => <li className="knowledge-source-row" key={source.id}><span className="knowledge-source-name" title={source.filename}>{source.filename}</span><button className="knowledge-source-action add" type="button" disabled={disabled || atLimit} aria-label={`选择 ${source.filename}`} title={`选择 ${source.filename}`} onClick={() => addSource(source)}><Check size={16} /></button></li>)}</ul>}</section>
            </>
          )}
          {account.is_guest && <p className="knowledge-context-state">登录员工账号后可选择知识库。</p>}
        </div>
      )}
      {tab === "submissions" && <KnowledgeSubmissionsWorkspace account={account} compact />}
    </aside>
  );
}
