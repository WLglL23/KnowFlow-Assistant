import { AlertCircle, MessageSquare, Plus } from "lucide-react";

import { errorMessage, formatTime } from "./presentation";
import type { ConversationWorkspaceState } from "./useConversationWorkspace";

export function ConversationList({ workspace }: { workspace: ConversationWorkspaceState }) {
  const { conversations, selectedId, setSelectedId, streaming, createMutation, setError } = workspace;
  return (
        <div className="sidebar-conversations">
          <div className="conversation-list-header">
            <h2>会话</h2>
            <button className="icon-button" type="button" onClick={() => { setError(null); createMutation.mutate(); }} disabled={streaming || createMutation.isPending} aria-label="创建会话" title="创建会话"><Plus size={18} /></button>
          </div>
        {conversations.isPending && <p className="empty-state">正在加载会话…</p>}
        {conversations.isError && <div className="query-error" role="alert"><AlertCircle size={17} />{errorMessage(conversations.error)}<button type="button" onClick={() => void conversations.refetch()}>重试</button></div>}
        {!conversations.isPending && !conversations.isError && conversations.data?.items.length === 0 && <p className="empty-state">还没有会话，创建一个开始对话</p>}
        <div className="conversation-items">{conversations.data?.items.map((item) => <button key={item.id} className={`conversation-item ${item.id === selectedId ? "selected" : ""}`} type="button" onClick={() => setSelectedId(item.id)} disabled={streaming}><MessageSquare size={16} /><span><strong>{item.title}</strong><small>{formatTime(item.updated_at)}</small></span></button>)}</div>
        </div>
  );
}
