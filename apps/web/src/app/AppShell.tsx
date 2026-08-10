import { BadgeCheck, ChevronLeft, LogOut, PanelLeft, ShieldCheck } from "lucide-react";
import { useState } from "react";

import { useAuth } from "../features/auth/AuthContext";
import { ConversationWorkspace } from "../features/conversation/ConversationWorkspace";

function roleLabel(role: "ADMIN" | "EMPLOYEE", isGuest: boolean): string {
  if (isGuest) return "游客";
  return role === "ADMIN" ? "管理员" : "员工";
}

export function AppShell() {
  const { account, logout } = useAuth();
  const [collapsed, setCollapsed] = useState(false);

  if (account === null) return null;
  const label = roleLabel(account.role, account.is_guest);
  const initial = account.display_name.trim().charAt(0).toUpperCase() || "知";

  return (
    <main className={`workspace ${collapsed ? "sidebar-collapsed" : ""}`}>
      <aside className="workspace-sidebar panel">
        <div className="workspace-brand">
          <button
            className="sidebar-toggle"
            type="button"
            onClick={() => setCollapsed((value) => !value)}
            aria-label={collapsed ? "展开侧栏" : "折叠侧栏"}
          >
            {collapsed ? <PanelLeft size={21} /> : <ChevronLeft size={21} />}
          </button>
          <div className="workspace-brand-copy">
            <strong>知办助手</strong>
            <span>KnowFlow Workspace</span>
          </div>
        </div>

        <nav className="workspace-nav" aria-label="主导航">
          <div className="nav-item active" aria-current="page">
            <span className="nav-indicator" aria-hidden="true" />
            <BadgeCheck size={19} aria-hidden="true" />
            <span className="nav-copy">工作台</span>
          </div>
        </nav>

        <div className="workspace-profile">
          <div className="avatar" aria-hidden="true">{initial}</div>
          <div className="profile-copy">
            <strong>{account.display_name}</strong>
            <span>{label}</span>
          </div>
          <button className="logout-button" type="button" onClick={logout} aria-label="退出登录">
            <LogOut size={18} />
          </button>
        </div>
      </aside>

      <section className="workspace-main">
        <header className="workspace-header">
          <div>
            <span className="workspace-kicker">安全工作空间</span>
            <h1>工作台</h1>
          </div>
          <div className="verified-pill">
            <ShieldCheck size={17} aria-hidden="true" />
            身份已验证
          </div>
        </header>

        <ConversationWorkspace account={account} />
      </section>
    </main>
  );
}
