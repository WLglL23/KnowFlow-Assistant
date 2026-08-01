import { Eye, EyeOff, KeyRound, UserRound, UserRoundPlus } from "lucide-react";
import { type FormEvent, useState } from "react";

import { ApiError } from "../../api/auth";
import { useAuth } from "./AuthContext";

function BrandMark() {
  return (
    <span className="brand-mark" aria-hidden="true">
      <span className="brand-orbit orbit-a" />
      <span className="brand-orbit orbit-b" />
      <span className="brand-core" />
    </span>
  );
}

export function LoginPage() {
  const { loginWithPassword, loginAsGuest, isAuthenticating } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [fieldError, setFieldError] = useState<{ username?: string; password?: string }>({});
  const [submitError, setSubmitError] = useState("");

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const nextErrors: { username?: string; password?: string } = {};
    if (!username.trim()) nextErrors.username = "请输入账号";
    if (!password) nextErrors.password = "请输入密码";
    setFieldError(nextErrors);
    setSubmitError("");
    if (Object.keys(nextErrors).length > 0) return;

    try {
      await loginWithPassword(username.trim(), password);
    } catch (error) {
      setSubmitError(
        error instanceof ApiError && error.status === 401
          ? "账号或密码不正确"
          : "暂时无法登录，请检查服务后重试",
      );
    }
  }

  async function handleGuestLogin() {
    setFieldError({});
    setSubmitError("");
    try {
      await loginAsGuest();
    } catch {
      setSubmitError("暂时无法创建游客身份，请稍后重试");
    }
  }

  return (
    <main className="login-shell">
      <span className="ambient ambient-one" aria-hidden="true" />
      <span className="ambient ambient-two" aria-hidden="true" />

      <section className="login-intro" aria-labelledby="login-hero-title">
        <div className="login-brand">
          <BrandMark />
          <span>知办助手</span>
        </div>

        <div className="intro-copy">
          <span className="eyebrow">企业知识，从这里开始流动</span>
          <h1 id="login-hero-title">
            让每一次查找，
            <br />
            <span>更接近答案。</span>
          </h1>
          <p>进入安全、清晰的知识工作空间，让资料与思考保持在同一条脉络里。</p>
          <div className="trust-line" aria-label="身份特性">
            <span>标签页隔离</span>
            <span>身份可验证</span>
            <span>访问有边界</span>
          </div>
        </div>

        <p className="copyright">© 2026 知办助手</p>
      </section>

      <section className="login-side" aria-label="登录">
        <div className="login-card">
          <div className="card-heading">
            <span className="card-kicker">KNOWFLOW ACCESS</span>
            <h2>欢迎回来</h2>
            <p>使用预置账号，或创建新的游客身份。</p>
          </div>

          <form onSubmit={handleSubmit} noValidate>
            <div className="field">
              <label htmlFor="username">账号</label>
              <div className={`input-wrap ${fieldError.username ? "has-error" : ""}`}>
                <UserRound size={19} aria-hidden="true" />
                <input
                  id="username"
                  name="username"
                  autoComplete="username"
                  value={username}
                  onChange={(event) => {
                    setUsername(event.target.value);
                    setFieldError((current) => ({ ...current, username: undefined }));
                  }}
                  placeholder="请输入用户名"
                  aria-invalid={Boolean(fieldError.username)}
                  aria-describedby={fieldError.username ? "username-error" : undefined}
                />
              </div>
              <p className="field-message" id="username-error">
                {fieldError.username ?? ""}
              </p>
            </div>

            <div className="field">
              <label htmlFor="password">密码</label>
              <div className={`input-wrap ${fieldError.password ? "has-error" : ""}`}>
                <KeyRound size={19} aria-hidden="true" />
                <input
                  id="password"
                  name="password"
                  type={showPassword ? "text" : "password"}
                  autoComplete="current-password"
                  value={password}
                  onChange={(event) => {
                    setPassword(event.target.value);
                    setFieldError((current) => ({ ...current, password: undefined }));
                  }}
                  placeholder="请输入登录密码"
                  aria-invalid={Boolean(fieldError.password)}
                  aria-describedby={fieldError.password ? "password-error" : undefined}
                />
                <button
                  className="visibility-button"
                  type="button"
                  onClick={() => setShowPassword((visible) => !visible)}
                  aria-label={showPassword ? "隐藏密码" : "显示密码"}
                >
                  {showPassword ? <EyeOff size={18} /> : <Eye size={18} />}
                </button>
              </div>
              <p className="field-message" id="password-error">
                {fieldError.password ?? ""}
              </p>
            </div>

            <p className="submit-message" aria-live="polite">
              {submitError}
            </p>
            <button className="primary-button" type="submit" disabled={isAuthenticating}>
              {isAuthenticating ? "正在验证…" : "登录"}
            </button>
          </form>

          <div className="divider"><span>或</span></div>
          <button
            className="guest-button"
            type="button"
            onClick={handleGuestLogin}
            disabled={isAuthenticating}
          >
            <UserRoundPlus size={19} aria-hidden="true" />
            创建游客身份
          </button>
          <p className="session-note">身份仅保存在当前浏览器标签页。</p>
        </div>
      </section>
    </main>
  );
}
