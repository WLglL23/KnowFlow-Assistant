import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Send, Pencil, Check, X, MessageSquare, AlertCircle } from "lucide-react";
import Markdown from "react-markdown";
import { useEffect, useMemo, useState } from "react";

import { ApiError, type Account } from "../../api/auth";
import { createConversation, getConversation, listConversations, renameConversation, streamRun, type Conversation, type RunEvent } from "../../api/conversations";

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

export function ConversationWorkspace({ account }: { account: Account }) {
  const client = useQueryClient();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [editing, setEditing] = useState(false);
  const [titleDraft, setTitleDraft] = useState("");
  const [stage, setStage] = useState<string | null>(null);
  const [terminalStatus, setTerminalStatus] = useState<string | null>(null);
  const [streaming, setStreaming] = useState(false);
  const [streamText, setStreamText] = useState("");
  const [error, setError] = useState<string | null>(null);

  const conversationListKey = ["conversations", account.id] as const;
  const conversationDetailKey = ["conversation", account.id, selectedId] as const;
  const conversations = useQuery({ queryKey: conversationListKey, queryFn: listConversations });
  const detail = useQuery({ queryKey: conversationDetailKey, queryFn: () => getConversation(selectedId as string), enabled: selectedId !== null });

  useEffect(() => {
    setSelectedId(null);
    setEditing(false);
    setError(null);
  }, [account.id]);

  useEffect(() => {
    if (selectedId === null && conversations.data?.items[0]) setSelectedId(conversations.data.items[0].id);
  }, [conversations.data, selectedId]);

  const selected: Conversation | undefined = useMemo(() => conversations.data?.items.find((item) => item.id === selectedId), [conversations.data, selectedId]);
  const createMutation = useMutation({ mutationFn: createConversation, onSuccess: async (item) => { setError(null); await client.invalidateQueries({ queryKey: conversationListKey }); setSelectedId(item.id); }, onError: (cause) => setError(errorMessage(cause)) });
  const renameMutation = useMutation({ mutationFn: ({ id, title }: { id: string; title: string }) => renameConversation(id, title), onSuccess: async () => { setError(null); setEditing(false); await client.invalidateQueries({ queryKey: conversationListKey }); await detail.refetch(); }, onError: (cause) => setError(errorMessage(cause)) });

  async function send() {
    const content = draft.trim();
    if (!selectedId || !content || streaming) return;
    setError(null); setStreaming(true); setStage(null); setTerminalStatus(null); setStreamText(""); setDraft("");
    let generateStarted = false;
    let requestId: string | null = null;
    let runId: string | null = null;
    let terminal = false;
    try {
      await streamRun(selectedId, content, crypto.randomUUID(), (event: RunEvent) => {
        if (!event.request_id || !event.run_id || terminal) return;
        requestId ??= event.request_id;
        runId ??= event.run_id;
        if (event.request_id !== requestId || event.run_id !== runId) return;
        if (event.type === "stage.started") { generateStarted = event.stage === "GENERATE"; setStage(event.stage ?? null); }
        if (event.type === "answer.delta" && generateStarted && event.delta) setStreamText((value) => value + event.delta);
        if (event.type === "run.completed" || event.type === "run.failed") {
          terminal = true;
          setTerminalStatus(event.type === "run.completed" ? "已完成" : "处理失败");
          if (event.type === "run.failed") setError(event.error?.message ?? "回答处理失败");
        }
      });
      if (!terminal) setError("连接已中断，已刷新会话以查看持久化结果");
      await detail.refetch(); await client.invalidateQueries({ queryKey: conversationListKey });
    } catch (cause) {
      setError(errorMessage(cause)); await detail.refetch(); await client.invalidateQueries({ queryKey: conversationListKey });
    } finally { setStreaming(false); }
  }

  const messages = detail.data?.messages ?? [];
  return (
    <div className="conversation-workspace">
      <aside className="conversation-list" aria-label="会话列表">
        <div className="conversation-list-header"><div><span className="workspace-kicker">CONVERSATIONS</span><h2>会话</h2></div><button className="icon-button" type="button" onClick={() => { setError(null); createMutation.mutate(); }} disabled={streaming || createMutation.isPending} aria-label="创建会话" title="创建会话"><Plus size={19} /></button></div>
        {conversations.isPending && <p className="empty-state">正在加载会话…</p>}
        {conversations.isError && <div className="query-error" role="alert"><AlertCircle size={17} />{errorMessage(conversations.error)}<button type="button" onClick={() => void conversations.refetch()}>重试</button></div>}
        {!conversations.isPending && !conversations.isError && conversations.data?.items.length === 0 && <p className="empty-state">还没有会话，创建一个开始对话</p>}
        <div className="conversation-items">{conversations.data?.items.map((item) => <button key={item.id} className={`conversation-item ${item.id === selectedId ? "selected" : ""}`} type="button" onClick={() => setSelectedId(item.id)} disabled={streaming}><MessageSquare size={16} /><span><strong>{item.title}</strong><small>{formatTime(item.updated_at)}</small></span></button>)}</div>
      </aside>
      <section className="conversation-panel">
        {selected && <header className="conversation-header"><div>{editing ? <div className="title-edit"><input value={titleDraft} onChange={(event) => setTitleDraft(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") { setError(null); renameMutation.mutate({ id: selected.id, title: titleDraft }); } if (event.key === "Escape") setEditing(false); }} autoFocus /><button className="icon-button" type="button" onClick={() => { setError(null); renameMutation.mutate({ id: selected.id, title: titleDraft }); }} disabled={renameMutation.isPending} aria-label="保存标题"><Check size={17} /></button><button className="icon-button" type="button" onClick={() => setEditing(false)} disabled={renameMutation.isPending} aria-label="取消重命名"><X size={17} /></button></div> : <div className="title-row"><h2>{detail.data?.title ?? selected.title}</h2><button className="icon-button" type="button" onClick={() => { setTitleDraft(detail.data?.title ?? selected.title); setError(null); setEditing(true); }} aria-label="重命名会话" title="重命名"><Pencil size={16} /></button></div>}<span>{account.display_name} · 当前会话</span></div><span className={`run-status ${streaming ? "active" : ""}`}>{streaming ? terminalStatus ?? (stage ? stageLabels[stage] ?? stage : "正在提交") : "已就绪"}</span></header>}
        <div className="message-flow" aria-live="polite">
          {!selected && <div className="empty-detail"><MessageSquare size={30} /><h3>选择一个会话</h3><p>从左侧选择会话，或创建一个新的会话。</p></div>}
          {selected && detail.isPending && <div className="empty-detail"><p>正在加载会话详情…</p></div>}
          {selected && detail.isError && <div className="empty-detail query-error" role="alert"><AlertCircle size={24} /><h3>{errorMessage(detail.error)}</h3><button type="button" onClick={() => void detail.refetch()}>重新加载</button></div>}
          {selected && !detail.isPending && !detail.isError && messages.length === 0 && !streaming && <div className="empty-detail"><MessageSquare size={30} /><h3>开始一段对话</h3><p>提出一个普通问题，助手会在当前会话中连续回答。</p></div>}
          {messages.map((message) => <article key={message.id} className={`message ${message.role === "USER" ? "user" : "assistant"}`}><div className="message-role">{message.role === "USER" ? "你" : "知办助手"}<time>{formatTime(message.created_at)}</time>{message.role === "ASSISTANT" && message.status !== "COMPLETE" && <span className={`message-status ${message.status.toLowerCase()}`}>{message.status === "PARTIAL" ? "回答未完成" : "处理中"}</span>}</div><div className="message-content">{message.role === "ASSISTANT" ? message.content ? <Markdown>{message.content}</Markdown> : message.status === "PARTIAL" ? <span className="message-empty">本次未生成可保存的回答正文。</span> : null : message.content}</div></article>)}
          {streaming && <article className="message assistant"><div className="message-role">知办助手<span className="streaming-label">{terminalStatus ?? (stage ? stageLabels[stage] ?? stage : "等待接受")}</span></div><div className="message-content">{streamText ? <Markdown>{streamText}</Markdown> : <span className="typing-indicator">{stage ? `${stageLabels[stage] ?? stage}…` : "请求已提交，等待服务响应…"}</span>}</div></article>}
        </div>
        {error && <div className="conversation-error" role="alert"><AlertCircle size={17} />{error}</div>}
        <div className="composer"><textarea value={draft} onChange={(event) => setDraft(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void send(); } }} placeholder="输入你的问题…" aria-label="问题输入" disabled={streaming || !selectedId} /><button className="send-button" type="button" onClick={() => void send()} disabled={streaming || !selectedId || !draft.trim()} aria-label="发送问题" title="发送"><Send size={18} /></button><small>Enter 发送 · Shift + Enter 换行</small></div>
      </section>
    </div>
  );
}
