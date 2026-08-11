import { useAuth } from "../features/auth/AuthContext";
import { ConversationWorkspace } from "../features/conversation/ConversationWorkspace";

export function AppShell() {
  const { account, logout } = useAuth();

  if (account === null) return null;

  return <ConversationWorkspace account={account} onLogout={logout} />;
}
