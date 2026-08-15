import { Navigate, useLocation, useNavigate } from "react-router-dom";

import { useAuth } from "../features/auth/AuthContext";
import { ConversationWorkspace } from "../features/conversation/ConversationWorkspace";

export function AppShell() {
  const { account, logout } = useAuth();
  const location = useLocation();
  const navigate = useNavigate();

  if (account === null) return null;
  if (location.pathname === "/admin/knowledge" && account.role !== "ADMIN") {
    return <Navigate to="/" replace />;
  }

  const activeView = location.pathname === "/admin/knowledge"
    ? "adminKnowledge"
    : location.pathname === "/knowledge"
      ? "knowledge"
      : "conversations";

  return (
    <ConversationWorkspace
      account={account}
      activeView={activeView}
      onNavigate={(view) => navigate(
        view === "adminKnowledge" ? "/admin/knowledge" : view === "knowledge" ? "/knowledge" : "/",
      )}
      onLogout={logout}
    />
  );
}
