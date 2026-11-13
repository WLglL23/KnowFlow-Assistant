import { ApiError } from "../../api/auth";

export function errorMessage(error: unknown): string {
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

export function formatTime(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit" }).format(new Date(value));
}
