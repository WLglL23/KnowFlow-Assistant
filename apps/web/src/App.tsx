import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";

import { AppShell } from "./app/AppShell";
import { useAuth } from "./features/auth/AuthContext";
import { AuthProvider } from "./features/auth/AuthProvider";
import { LoginPage } from "./features/auth/LoginPage";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: false, refetchOnWindowFocus: false },
    mutations: { retry: false },
  },
});

function LoadingScreen() {
  return (
    <main className="status-screen" aria-live="polite">
      <div className="status-mark" aria-hidden="true">
        <span />
        <span />
        <span />
      </div>
      <p>正在恢复当前标签页的身份…</p>
    </main>
  );
}

function RecoveryScreen() {
  const { retryRestore, logout } = useAuth();

  return (
    <main className="status-screen">
      <div className="recovery-card">
        <span className="recovery-kicker">连接未完成</span>
        <h1>暂时无法恢复身份</h1>
        <p>请检查 API 服务后重试，或清除当前标签页身份并重新登录。</p>
        <div className="recovery-actions">
          <button className="primary-button compact" type="button" onClick={retryRestore}>
            重新连接
          </button>
          <button className="secondary-button compact" type="button" onClick={logout}>
            返回登录
          </button>
        </div>
      </div>
    </main>
  );
}

function ProtectedWorkspace() {
  const { status } = useAuth();

  if (status === "restoring") return <LoadingScreen />;
  if (status === "error") return <RecoveryScreen />;
  if (status !== "authenticated") return <Navigate to="/login" replace />;
  return <AppShell />;
}

function LoginRoute() {
  const { status } = useAuth();

  if (status === "restoring") return <LoadingScreen />;
  if (status === "error") return <RecoveryScreen />;
  if (status === "authenticated") return <Navigate to="/" replace />;
  return <LoginPage />;
}

function AppRoutes() {
  const { status } = useAuth();

  return (
    <Routes>
      <Route path="/login" element={<LoginRoute />} />
      <Route path="/" element={<ProtectedWorkspace />} />
      <Route
        path="*"
        element={<Navigate to={status === "authenticated" ? "/" : "/login"} replace />}
      />
    </Routes>
  );
}

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <AuthProvider>
          <AppRoutes />
        </AuthProvider>
      </BrowserRouter>
    </QueryClientProvider>
  );
}
