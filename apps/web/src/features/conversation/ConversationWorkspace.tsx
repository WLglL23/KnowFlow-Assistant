import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertCircle, Check, ChevronLeft, FileCheck2, LogOut, MessageSquare, PanelLeft, Pencil, Plus, Trash2, X } from "lucide-react";
import Markdown from "react-markdown";
import { useEffect, useMemo, useState } from "react";

import { ApiError, type Account } from "../../api/auth";
import { createConversation, deleteConversation, getConversation, listConversations, renameConversation, streamRun, type Citation, type Conversation, type RunEvent } from "../../api/conversations";
import { KnowledgeAdminWorkspace } from "../admin/knowledge/KnowledgeAdminWorkspace";
import type { CitationSelection } from "../citations/CitationPreviewPanel";
import { ConversationComposer } from "./ConversationComposer";
import { KnowledgeContextSidebar } from "./KnowledgeContextSidebar";
import { ConfirmDialog } from "../shared/ConfirmDialog";

function errorMessage(error: unknown): string {
  if (!(error instanceof ApiError)) return "请求未完成，请稍后重试";
  const messages: Record<string, string> = {
    REQUEST_ALREADY_ACCEPTED: "这条消息已经接受，请刷新会话查看结果",
    RUN_ALREADY_ACTIVE: "当前会话正在处理，请等待本轮完成",
    VALIDATION_FAILED: "请输入有效的问题内容",
    AUTH_REQUIRED: "登录已失效，请重新登录",
    AUTH_INVALID: "当前身份无效，请重新登录",
    NOT_FOUND: "会话不可见或已不存在",
    FORBIDDEN: "当前身份无权执行此操作",
  };
  if (messages[error.code]) return messages[error.code];
  if (error.status === 401) return "登录已失效，请重新登录";
  if (error.status === 403) return "当前身份无权执行此操作";
  if (error.status === 404) return "会话不可见或已不存在";
  return "请求未完成，请稍后重试";
}

function formatTime(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit" }).format(new Date(value));
}

const stageLabels: Record<string, string> = {
  UNDERSTAND: "理解问题",
  PLAN: "规划检索",
  RETRIEVE_1: "检索资料",
  VERIFY: "核验证据",
  RETRIEVE_2: "补充检索",
  GENERATE: "生成回答",
};

function roleLabel(role: Account["role"], isGuest: boolean): string {
  if (isGuest) return "游客";
  return role === "ADMIN" ? "管理员" : "员工";
}

function MessageContent({ messageId, content, citations, onSelect }: { messageId: string; content: string; citations: Citation[]; onSelect: (selection: CitationSelection) => void }) {
  const parts = content.split(/(\[\[citation:\d+\]\])/g);
  return <>{parts.map((part, index) => {
    const match = part.match(/^\[\[citation:(\d+)\]\]$/);
    if (!match) return <Markdown key={`${messageId}-text-${index}`}>{part}</Markdown>;
    const citationIndex = Number(match[1]);
    const citation = citations[citationIndex];
    if (!citation) return null;
    const glyph = citationIndex < 9 ? String.fromCharCode(9312 + citationIndex) : `[${citationIndex + 1}]`;
    return <button key={`${messageId}-citation-${citationIndex}-${index}`} className="citation-marker" type="button" title={`打开 ${citation.filename} 第 ${citation.pages.join("、")} 页`} onClick={() => onSelect({ messageId, citationIndex, citation })}>{glyph}</button>;
  })}</>;
}

type WorkspaceView = "conversations" | "adminKnowledge";

export function ConversationWorkspace({
  account,
  activeView,
  onNavigate,
  onLogout,
}: {
  account: Account;
  activeView: WorkspaceView;
  onNavigate: (view: WorkspaceView) => void;
  onLogout: () => void;
}) {
  const client = useQueryClient();
  const [collapsed, setCollapsed] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [editing, setEditing] = useState(false);
  const [titleDraft, setTitleDraft] = useState("");
  const [stage, setStage] = useState<string | null>(null);
  const [streaming, setStreaming] = useState(false);
  const [streamText, setStreamText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [citationSelection, setCitationSelection] = useState<CitationSelection | null>(null);
  const [deleteConversationOpen, setDeleteConversationOpen] = useState(false);

  const conversationListKey = ["conversations", account.id] as const;
  const conversationDetailKey = ["conversation", account.id, selectedId] as const;
  const conversations = useQuery({ queryKey: conversationListKey, queryFn: listConversations, enabled: activeView === "conversations" });
  const detail = useQuery({ queryKey: conversationDetailKey, queryFn: () => getConversation(selectedId as string), enabled: activeView === "conversations" && selectedId !== null });

  useEffect(() => {
    setSelectedId(null);
    setEditing(false);
    setError(null);
    setCitationSelection(null);
  }, [account.id]);

  useEffect(() => {
    setCitationSelection(null);
  }, [selectedId]);

  useEffect(() => {
    if (activeView !== "conversations") setCitationSelection(null);
  }, [activeView]);

  useEffect(() => {
    if (selectedId === null && conversations.data?.items[0]) setSelectedId(conversations.data.items[0].id);
  }, [conversations.data, selectedId]);

  const selected: Conversation | undefined = useMemo(() => conversations.data?.items.find((item) => item.id === selectedId), [conversations.data, selectedId]);
  const createMutation = useMutation({ mutationFn: createConversation, onSuccess: async (item) => { setError(null); await client.invalidateQueries({ queryKey: conversationListKey }); setSelectedId(item.id); }, onError: (cause) => setError(errorMessage(cause)) });
  const renameMutation = useMutation({ mutationFn: ({ id, title }: { id: string; title: string }) => renameConversation(id, title), onSuccess: async () => { setError(null); setEditing(false); await client.invalidateQueries({ queryKey: conversationListKey }); await detail.refetch(); }, onError: (cause) => setError(errorMessage(cause)) });
  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteConversation(id),
    onSuccess: async () => {
      setDeleteConversationOpen(false);
      setCitationSelection(null);
      setSelectedId(null);
      setError(null);
      await client.invalidateQueries({ queryKey: conversationListKey });
    },
    onError: (cause) => setError(errorMessage(cause)),
  });

  function saveTitle() {
    if (!selected) return;
    const currentTitle = detail.data?.title ?? selected.title;
    const normalizedTitle = titleDraft.trim();

    if (!normalizedTitle || normalizedTitle === currentTitle) {
      setTitleDraft(currentTitle);
      setError(null);
      setEditing(false);
      return;
    }

    setError(null);
    renameMutation.mutate({ id: selected.id, title: normalizedTitle });
  }

  async function send() {
    const content = draft.trim();
    if (!selectedId || !content || streaming) return;
    setError(null); setStreaming(true); setStage(null); setStreamText(""); setDraft("");
    let generateStarted = false;
    let accepted = false;
    let requestId: string | null = null;
    let runId: string | null = null;
    let terminal = false;
    try {
      await streamRun(selectedId, content, crypto.randomUUID(), (event: RunEvent) => {
        if (!event.request_id || !event.run_id || terminal) return;
        requestId ??= event.request_id;
        runId ??= event.run_id;
        accepted = true;
        if (event.request_id !== requestId || event.run_id !== runId) return;
        if (event.type === "stage.started") { generateStarted = event.stage === "GENERATE"; setStage(event.stage ?? null); }
        if (event.type === "answer.delta" && generateStarted && event.delta) setStreamText((value) => value + event.delta);
        if (event.type === "run.completed" || event.type === "run.failed") {
          terminal = true;
        }
      });
      await detail.refetch(); await client.invalidateQueries({ queryKey: conversationListKey });
    } catch (cause) {
      if (!accepted) setError(errorMessage(cause));
      await detail.refetch(); await client.invalidateQueries({ queryKey: conversationListKey });
    } finally { setStreaming(false); }
  }

  const messages = detail.data?.messages ?? [];
  const label = roleLabel(account.role, account.is_guest);
  const initial = account.display_name.trim().charAt(0).toUpperCase() || "知";

  return (
    <main className={`workspace ${collapsed ? "sidebar-collapsed" : ""}`}>
      <aside className="workspace-sidebar" aria-label="应用导航和会话列表">
        <div className="workspace-brand">
          <button
            className="sidebar-toggle"
            type="button"
            onClick={() => setCollapsed((value) => !value)}
            aria-label={collapsed ? "展开侧栏" : "折叠侧栏"}
            title={collapsed ? "展开侧栏" : "折叠侧栏"}
          >
            {collapsed ? <PanelLeft size={20} /> : <ChevronLeft size={20} />}
          </button>
          <div className="workspace-brand-copy">
            <strong>知办助手</strong>
          </div>
        </div>

        <nav className="workspace-nav" aria-label="主导航">
          <button className={`nav-item ${activeView === "conversations" ? "active" : ""}`} type="button" onClick={() => onNavigate("conversations")} aria-current={activeView === "conversations" ? "page" : undefined}>
            {activeView === "conversations" && <span className="nav-indicator" aria-hidden="true" />}
            <MessageSquare size={18} aria-hidden="true" />
            <span className="nav-copy">对话</span>
          </button>
          {account.role === "ADMIN" && (
            <button className={`nav-item ${activeView === "adminKnowledge" ? "active" : ""}`} type="button" onClick={() => onNavigate("adminKnowledge")} aria-current={activeView === "adminKnowledge" ? "page" : undefined} aria-label="Knowledge 管理">
              {activeView === "adminKnowledge" && <span className="nav-indicator" aria-hidden="true" />}
              <FileCheck2 size={18} aria-hidden="true" />
              <span className="nav-copy">知识管理</span>
            </button>
          )}
        </nav>

        {activeView === "conversations" && <div className="sidebar-conversations">
          <div className="conversation-list-header">
            <h2>会话</h2>
            <button className="icon-button" type="button" onClick={() => { setError(null); createMutation.mutate(); }} disabled={streaming || createMutation.isPending} aria-label="创建会话" title="创建会话"><Plus size={18} /></button>
          </div>
        {conversations.isPending && <p className="empty-state">正在加载会话…</p>}
        {conversations.isError && <div className="query-error" role="alert"><AlertCircle size={17} />{errorMessage(conversations.error)}<button type="button" onClick={() => void conversations.refetch()}>重试</button></div>}
        {!conversations.isPending && !conversations.isError && conversations.data?.items.length === 0 && <p className="empty-state">还没有会话，创建一个开始对话</p>}
        <div className="conversation-items">{conversations.data?.items.map((item) => <button key={item.id} className={`conversation-item ${item.id === selectedId ? "selected" : ""}`} type="button" onClick={() => setSelectedId(item.id)} disabled={streaming}><MessageSquare size={16} /><span><strong>{item.title}</strong><small>{formatTime(item.updated_at)}</small></span></button>)}</div>
        </div>}
        {activeView !== "conversations" && <div className="sidebar-spacer" />}

        <div className="workspace-profile">
          <div className="avatar" aria-hidden="true">{initial}</div>
          <div className="profile-copy">
            <strong>{account.display_name}</strong>
            <span>{label}</span>
          </div>
          <button className="logout-button" type="button" onClick={onLogout} aria-label="退出登录" title="退出登录">
            <LogOut size={17} />
          </button>
        </div>
      </aside>

      {activeView === "adminKnowledge" ? (
        <section className="workspace-main">
          <KnowledgeAdminWorkspace />
        </section>
      ) : (
      <section className="workspace-main conversation-workbench context-open">
      <section className="conversation-panel">
        {selected && <header className="conversation-header"><div>{editing ? <div className="title-edit"><input value={titleDraft} onChange={(event) => setTitleDraft(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") saveTitle(); if (event.key === "Escape") setEditing(false); }} autoFocus /><button className="icon-button" type="button" onClick={saveTitle} disabled={renameMutation.isPending} aria-label="保存标题"><Check size={17} /></button><button className="icon-button" type="button" onClick={() => setEditing(false)} disabled={renameMutation.isPending} aria-label="取消重命名"><X size={17} /></button></div> : <div className="title-row"><h2>{detail.data?.title ?? selected.title}</h2><button className="icon-button" type="button" onClick={() => { setTitleDraft(detail.data?.title ?? selected.title); setError(null); setEditing(true); }} aria-label="重命名会话" title="重命名"><Pencil size={16} /></button><button className="icon-button danger-icon" type="button" onClick={() => setDeleteConversationOpen(true)} aria-label="删除会话" title="删除会话"><Trash2 size={16} /></button></div>}</div> </header>}
        <div className="message-flow" aria-live="polite">
          {!selected && <div className="empty-detail"><MessageSquare size={30} /><h3>选择一个会话</h3><p>从左侧选择会话，或创建一个新的会话。</p></div>}
          {selected && detail.isPending && <div className="empty-detail"><p>正在加载会话详情…</p></div>}
          {selected && detail.isError && <div className="empty-detail query-error" role="alert"><AlertCircle size={24} /><h3>{errorMessage(detail.error)}</h3><button type="button" onClick={() => void detail.refetch()}>重新加载</button></div>}
          {selected && !detail.isPending && !detail.isError && messages.length === 0 && !streaming && <div className="empty-detail"><MessageSquare size={30} /><h3>开始一段对话</h3><p>上传 PDF，或直接输入问题。</p></div>}
          {selected && messages.map((message) => <article key={message.id} className={`message ${message.role === "USER" ? "user" : "assistant"}`}><div className="message-role">{message.role === "USER" ? "你" : "知办助手"}<time>{formatTime(message.created_at)}</time></div><div className="message-content">{message.role === "ASSISTANT" ? <>{message.content ? <MessageContent messageId={message.id} content={message.content} citations={message.citations} onSelect={setCitationSelection} /> : message.status === "PARTIAL" ? <span className="message-empty">回答未生成。</span> : null}{message.status === "PARTIAL" && <div className="message-error">错误：回答未完成</div>}</> : message.content}</div></article>)}
          {streaming && <article className="message assistant"><div className="message-role">知办助手<span className="streaming-label">{stage ? stageLabels[stage] ?? stage : "等待接受"}</span></div><div className="message-content">{streamText ? <Markdown>{streamText}</Markdown> : <span className="typing-indicator">{stage ? `${stageLabels[stage] ?? stage}…` : "请求已提交，等待服务响应…"}</span>}</div></article>}
        </div>
        {error && <div className="conversation-error" role="alert"><AlertCircle size={17} />{error}</div>}
        {selected && <ConversationComposer accountId={account.id} conversationId={selectedId} knowledgeMode={detail.data?.knowledge_mode ?? selected.knowledge_mode ?? "DISABLED"} selectedSourceCount={detail.data?.knowledge_mode === "SELECTED" ? detail.data.selected_source_ids.length : 0} draft={draft} streaming={streaming} onDraftChange={setDraft} onSend={() => void send()} />}
      </section>
      <KnowledgeContextSidebar account={account} conversation={detail.data ?? selected} citationSelection={citationSelection} onCitationClose={() => setCitationSelection(null)} onConversationUpdated={async () => { await detail.refetch(); }} />
      </section>
      )}
      {deleteConversationOpen && selected && <ConfirmDialog title="删除这个会话？" description={`“${detail.data?.title ?? selected.title}”及其中的附件会立即隐藏且不可恢复。正在进行的回答会继续完成，历史引用会保留为“来源已删除”。`} busy={deleteMutation.isPending} onCancel={() => setDeleteConversationOpen(false)} onConfirm={() => deleteMutation.mutate(selected.id)} />}
    </main>
  );
}
