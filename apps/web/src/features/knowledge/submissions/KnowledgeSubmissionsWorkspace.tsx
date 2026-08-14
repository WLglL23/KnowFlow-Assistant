import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertCircle,
  CheckCircle2,
  Clock3,
  FileText,
  LoaderCircle,
  RefreshCw,
  Send,
  ShieldCheck,
  Upload,
  XCircle,
} from "lucide-react";
import { type ChangeEvent, type FormEvent, useEffect, useRef, useState } from "react";

import { ApiError, type Account } from "../../../api/auth";
import {
  listMySubmissions,
  type Submission,
  type SubmissionStatus,
  submitKnowledge,
} from "../../../api/submissions";

const statusCopy: Record<SubmissionStatus, { label: string; detail: string }> = {
  PENDING_REVIEW: { label: "待审核", detail: "已进入审核队列" },
  REJECTED: { label: "未通过", detail: "审核已完成" },
  PUBLISHING: { label: "发布中", detail: "正在准备知识内容" },
  PUBLISHED: { label: "已发布", detail: "知识内容已可用" },
  PUBLISH_FAILED: { label: "发布失败", detail: "发布未完成，请等待后续处理" },
};

function submissionError(error: unknown): string {
  if (!(error instanceof ApiError)) return "投稿未完成，请稍后重试";
  const reason = error.details?.reason;
  if (error.code === "INVALID_PDF") {
    const invalidMessages: Record<string, string> = {
      MIME_MISMATCH: "文件类型与 PDF 不一致",
      EXTENSION_INVALID: "请选择扩展名为 .pdf 的文件",
      SIGNATURE_INVALID: "文件不是有效的 PDF",
      PASSWORD_PROTECTED: "暂不支持受密码保护的 PDF",
      CORRUPT: "PDF 已损坏或无法读取",
      PAGE_COUNT_UNREADABLE: "无法读取 PDF 页数",
    };
    return invalidMessages[reason ?? ""] ?? "PDF 无法安全读取，请检查文件后重试";
  }
  if (error.code === "DOCUMENT_TOO_LARGE") {
    return reason === "PAGES_EXCEEDED" ? "PDF 超过 100 页" : "PDF 超过 20 MiB";
  }
  if (error.code === "VALIDATION_FAILED") return "请选择一个 PDF 文件";
  if (error.status === 401) return "登录已失效，请重新登录";
  if (error.status === 403) return "当前身份无权投稿";
  return "投稿未完成，请稍后重试";
}

function formatDate(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function formatBytes(value: number): string {
  if (value < 1024 * 1024) return `${Math.max(1, Math.round(value / 1024))} KiB`;
  return `${(value / 1024 / 1024).toFixed(1)} MiB`;
}

function StatusIcon({ status }: { status: SubmissionStatus }) {
  if (status === "PUBLISHED") return <CheckCircle2 size={17} />;
  if (status === "REJECTED" || status === "PUBLISH_FAILED") return <XCircle size={17} />;
  if (status === "PUBLISHING") return <LoaderCircle className="spin" size={17} />;
  return <Clock3 size={17} />;
}

function SubmissionRow({ submission }: { submission: Submission }) {
  const copy = statusCopy[submission.status];
  return (
    <li className="submission-row">
      <div className="submission-file-icon" aria-hidden="true"><FileText size={19} /></div>
      <div className="submission-file">
        <strong title={submission.filename}>{submission.filename}</strong>
        <span>{submission.page_count} 页 · {formatBytes(submission.size_bytes)}</span>
      </div>
      <time dateTime={submission.created_at}>{formatDate(submission.created_at)}</time>
      <div className={`submission-status ${submission.status.toLowerCase()}`}>
        <span><StatusIcon status={submission.status} />{copy.label}</span>
        <small>{submission.status === "REJECTED" && submission.rejection_reason
          ? submission.rejection_reason
          : copy.detail}</small>
      </div>
    </li>
  );
}

export function KnowledgeSubmissionsWorkspace({ account }: { account: Account }) {
  const client = useQueryClient();
  const inputRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [feedback, setFeedback] = useState<{ kind: "success" | "info" | "error"; text: string } | null>(null);
  const mineKey = ["knowledge", "submissions", "mine", account.id] as const;
  const mine = useQuery({ queryKey: mineKey, queryFn: listMySubmissions });
  const upload = useMutation({
    mutationFn: submitKnowledge,
    onSuccess: async (result) => {
      setFile(null);
      if (inputRef.current) inputRef.current.value = "";
      setFeedback(result.result === "CREATED"
        ? { kind: "success", text: "投稿已创建，当前状态为待审核" }
        : { kind: "info", text: "相同内容正在处理或已可用，本次未创建新投稿" });
      await client.invalidateQueries({ queryKey: mineKey });
    },
    onError: (error) => setFeedback({ kind: "error", text: submissionError(error) }),
  });

  useEffect(() => {
    setFile(null);
    setFeedback(null);
    if (inputRef.current) inputRef.current.value = "";
  }, [account.id]);

  function chooseFile(event: ChangeEvent<HTMLInputElement>) {
    setFile(event.target.files?.[0] ?? null);
    setFeedback(null);
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!file || upload.isPending) return;
    setFeedback(null);
    upload.mutate(file);
  }

  return (
    <section className="knowledge-workspace" aria-labelledby="knowledge-heading">
      <header className="knowledge-header">
        <div>
          <span>KNOWLEDGE</span>
          <h1 id="knowledge-heading">知识投稿</h1>
        </div>
        <div className="knowledge-owner"><ShieldCheck size={16} />仅显示当前身份的提交</div>
      </header>

      <div className="knowledge-content">
        <section className="submission-uploader" aria-labelledby="submission-form-heading">
          <div className="submission-section-heading">
            <div>
              <h2 id="submission-form-heading">提交 PDF</h2>
              <p>单个文件，最大 20 MiB、100 页。文件内容由服务端安全检查。</p>
            </div>
            <Upload size={20} aria-hidden="true" />
          </div>
          <form onSubmit={submit}>
            <label className={`submission-picker ${file ? "has-file" : ""}`}>
              <input ref={inputRef} type="file" accept="application/pdf,.pdf" onChange={chooseFile} disabled={upload.isPending} />
              <FileText size={22} aria-hidden="true" />
              <span>{file ? file.name : "选择 PDF 文件"}</span>
              <small>{file ? formatBytes(file.size) : "浏览本机文件"}</small>
            </label>
            <button className="submission-submit" type="submit" disabled={!file || upload.isPending}>
              {upload.isPending ? <LoaderCircle className="spin" size={17} /> : <Send size={17} />}
              {upload.isPending ? "正在提交" : "提交审核"}
            </button>
          </form>
          {feedback && (
            <div className={`submission-feedback ${feedback.kind}`} role={feedback.kind === "error" ? "alert" : "status"}>
              {feedback.kind === "error" ? <AlertCircle size={17} /> : <CheckCircle2 size={17} />}
              <span>{feedback.text}</span>
            </div>
          )}
        </section>

        <section className="submission-list-section" aria-labelledby="submission-list-heading">
          <div className="submission-section-heading">
            <div>
              <h2 id="submission-list-heading">我的提交</h2>
              <p>{mine.data ? `共 ${mine.data.items.length} 条，按提交时间排列` : "从服务端读取真实状态"}</p>
            </div>
            <button className="icon-button" type="button" onClick={() => void mine.refetch()} disabled={mine.isFetching} aria-label="刷新提交列表" title="刷新">
              <RefreshCw className={mine.isFetching ? "spin" : ""} size={17} />
            </button>
          </div>
          {mine.isPending && <div className="submission-list-state"><LoaderCircle className="spin" size={22} />正在加载提交…</div>}
          {mine.isError && <div className="submission-list-state error" role="alert"><AlertCircle size={22} />列表暂时无法加载<button type="button" onClick={() => void mine.refetch()}>重试</button></div>}
          {mine.data?.items.length === 0 && <div className="submission-list-state"><FileText size={24} />还没有提交记录</div>}
          {mine.data && mine.data.items.length > 0 && <ul className="submission-list">{mine.data.items.map((item) => <SubmissionRow key={item.id} submission={item} />)}</ul>}
        </section>
      </div>
    </section>
  );
}
