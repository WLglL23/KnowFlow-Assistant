import { useLocation, useNavigate } from "react-router-dom";

import { useAuth } from "../features/auth/AuthContext";
import { ConversationWorkspace } from "../features/conversation/ConversationWorkspace";

export function AppShell() {
  const { account, logout } = useAuth();
  const location = useLocation();
  const navigate = useNavigate();

  if (account === null) return null;

  return (
    <ConversationWorkspace
      account={account}
      activeView={location.pathname === "/knowledge" ? "knowledge" : "conversations"}
      onNavigate={(view) => navigate(view === "knowledge" ? "/knowledge" : "/")}
      onLogout={logout}
    />
  );
}
