import { Navigate, useLocation, useNavigate } from "react-router-dom";

import type { Account } from "../api/auth";
import { useAuth } from "../features/auth/AuthContext";
import { KnowledgeAdminWorkspace } from "../features/admin/knowledge/KnowledgeAdminWorkspace";
import { ConversationList } from "../features/conversation/ConversationList";
import { ConversationWorkspace, ConversationDeleteDialog } from "../features/conversation/ConversationWorkspace";
import { useConversationWorkspace } from "../features/conversation/useConversationWorkspace";
import { WorkspaceLayout, type WorkspaceView } from "./WorkspaceLayout";

export function AppShell() {
  const { account, logout } = useAuth();
  const location = useLocation();
  const navigate = useNavigate();

  if (account === null) return null;
  if (location.pathname === "/admin/knowledge" && account.role !== "ADMIN") {
    return <Navigate to="/" replace />;
  }
  const activeView = location.pathname === "/admin/knowledge" ? "adminKnowledge" : "conversations";
  return <AuthenticatedWorkspace account={account} activeView={activeView}
    onNavigate={(view) => navigate(view === "adminKnowledge" ? "/admin/knowledge" : "/")}
    onLogout={logout} />;
}

function AuthenticatedWorkspace({ account, activeView, onNavigate, onLogout }: {
  account: Account;
  activeView: WorkspaceView;
  onNavigate: (view: WorkspaceView) => void;
  onLogout: () => void;
}) {
  const workspace = useConversationWorkspace(account, activeView === "conversations");
  return (
    <WorkspaceLayout account={account} activeView={activeView} onNavigate={onNavigate}
      onLogout={onLogout} sidebar={activeView === "conversations" ? <ConversationList workspace={workspace} /> : null}>
      {activeView === "adminKnowledge" ? (
        <section className="workspace-main"><KnowledgeAdminWorkspace /></section>
      ) : <ConversationWorkspace account={account} workspace={workspace} />}
      <ConversationDeleteDialog workspace={workspace} />
    </WorkspaceLayout>
  );
}
