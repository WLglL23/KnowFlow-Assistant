import { AlertCircle, Check, MessageSquare, Pencil, Trash2, X } from "lucide-react";
import Markdown from "react-markdown";

import type { Account } from "../../api/auth";
import type { Citation } from "../../api/conversations";
import { stageLabel } from "../../api/runStages";
import type { CitationSelection } from "../citations/CitationPreviewPanel";
import { ConfirmDialog } from "../shared/ConfirmDialog";
import { ConversationComposer } from "./ConversationComposer";
import { KnowledgeContextSidebar } from "./KnowledgeContextSidebar";
import { ResizableContextLayout } from "./ResizableContextLayout";
import { errorMessage, formatTime } from "./presentation";
import type { ConversationWorkspaceState } from "./useConversationWorkspace";

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


export function ConversationWorkspace({ account, workspace }: {
  account: Account;
  workspace: ConversationWorkspaceState;
}) {
  const { selected, detail, citationSelection, setCitationSelection, editing, titleDraft,
    setTitleDraft, saveTitle, renameMutation, setEditing, setError, setDeleteConversationOpen,
    streaming, streamText, stage, error, selectedId, draft, setDraft, send } = workspace;
  const messages = detail.data?.messages ?? [];
  return (
      <ResizableContextLayout revealSignal={citationSelection} sidebar={
        <KnowledgeContextSidebar account={account} conversation={detail.data ?? selected} citationSelection={citationSelection} onCitationClose={() => setCitationSelection(null)} onConversationUpdated={async () => { await detail.refetch(); }} />
      }>
      <section className="conversation-panel">
        {selected && <header className="conversation-header"><div>{editing ? <div className="title-edit"><input value={titleDraft} onChange={(event) => setTitleDraft(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") saveTitle(); if (event.key === "Escape") setEditing(false); }} autoFocus /><button className="icon-button" type="button" onClick={saveTitle} disabled={renameMutation.isPending} aria-label="保存标题"><Check size={17} /></button><button className="icon-button" type="button" onClick={() => setEditing(false)} disabled={renameMutation.isPending} aria-label="取消重命名"><X size={17} /></button></div> : <div className="title-row"><h2>{detail.data?.title ?? selected.title}</h2><button className="icon-button" type="button" onClick={() => { setTitleDraft(detail.data?.title ?? selected.title); setError(null); setEditing(true); }} aria-label="重命名会话" title="重命名"><Pencil size={16} /></button><button className="icon-button danger-icon" type="button" onClick={() => setDeleteConversationOpen(true)} aria-label="删除会话" title="删除会话"><Trash2 size={16} /></button></div>}</div> </header>}
        <div className="message-flow" aria-live="polite">
          {!selected && <div className="empty-detail"><MessageSquare size={30} /><h3>选择一个会话</h3><p>从左侧选择会话，或创建一个新的会话。</p></div>}
          {selected && detail.isPending && <div className="empty-detail"><p>正在加载会话详情…</p></div>}
          {selected && detail.isError && <div className="empty-detail query-error" role="alert"><AlertCircle size={24} /><h3>{errorMessage(detail.error)}</h3><button type="button" onClick={() => void detail.refetch()}>重新加载</button></div>}
          {selected && !detail.isPending && !detail.isError && messages.length === 0 && !streaming && <div className="empty-detail"><MessageSquare size={30} /><h3>开始一段对话</h3><p>上传 PDF，或直接输入问题。</p></div>}
          {selected && messages.map((message) => <article key={message.id} className={`message ${message.role === "USER" ? "user" : "assistant"}`}><div className="message-role">{message.role === "USER" ? "你" : "知办助手"}<time>{formatTime(message.created_at)}</time></div><div className="message-content">{message.role === "ASSISTANT" ? <>{message.content ? <MessageContent messageId={message.id} content={message.content} citations={message.citations} onSelect={setCitationSelection} /> : message.status === "PARTIAL" ? <span className="message-empty">回答未生成。</span> : null}{message.status === "PARTIAL" && <div className="message-error">错误：回答未完成</div>}</> : message.content}</div></article>)}
          {streaming && <article className="message assistant"><div className="message-role">知办助手<span className="streaming-label">{stage ? stageLabel(stage) : "等待接受"}</span></div><div className="message-content">{streamText ? <Markdown>{streamText}</Markdown> : <span className="typing-indicator">{stage ? `${stageLabel(stage)}…` : "请求已提交，等待服务响应…"}</span>}</div></article>}
        </div>
        {error && <div className="conversation-error" role="alert"><AlertCircle size={17} />{error}</div>}
        {selected && <ConversationComposer accountId={account.id} conversationId={selectedId} knowledgeMode={detail.data?.knowledge_mode ?? selected.knowledge_mode ?? "DISABLED"} selectedSourceCount={detail.data?.knowledge_mode === "SELECTED" ? detail.data.selected_source_ids.length : 0} draft={draft} streaming={streaming} onDraftChange={setDraft} onSend={() => void send()} />}
      </section>
      </ResizableContextLayout>

  );
}

export function ConversationDeleteDialog({ workspace }: { workspace: ConversationWorkspaceState }) {
  const { deleteConversationOpen, selected, detail, deleteMutation, setDeleteConversationOpen } = workspace;
  return (
    <>
      {deleteConversationOpen && selected && <ConfirmDialog title="删除这个会话？" description={`“${detail.data?.title ?? selected.title}”及其中的附件会立即隐藏且不可恢复。正在进行的回答会继续完成，历史引用会保留为“来源已删除”。`} busy={deleteMutation.isPending} onCancel={() => setDeleteConversationOpen(false)} onConfirm={() => deleteMutation.mutate(selected.id)} />}
    </>
  );
}
