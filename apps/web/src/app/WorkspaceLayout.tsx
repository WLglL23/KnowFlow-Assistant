import { useState, type ReactNode } from "react";
import { ChevronLeft, FileCheck2, LogOut, MessageSquare, PanelLeft } from "lucide-react";

import type { Account } from "../api/auth";

export type WorkspaceView = "conversations" | "adminKnowledge";

function roleLabel(role: Account["role"], isGuest: boolean): string {
  if (isGuest) return "游客";
  return role === "ADMIN" ? "管理员" : "员工";
}


export function WorkspaceLayout({ account, activeView, onNavigate, onLogout, sidebar, children }: {
  account: Account;
  activeView: WorkspaceView;
  onNavigate: (view: WorkspaceView) => void;
  onLogout: () => void;
  sidebar: ReactNode;
  children: ReactNode;
}) {
  const [collapsed, setCollapsed] = useState(false);
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

        {sidebar ?? <div className="sidebar-spacer" />}
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

      {children}
    </main>
  );
}
