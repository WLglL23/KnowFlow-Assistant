import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertCircle,
  Check,
  CheckCircle2,
  Clock3,
  Download,
  FileText,
  History,
  Library,
  LoaderCircle,
  RefreshCw,
  RotateCcw,
  Trash2,
  X,
  XCircle,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import {
  approveSubmission,
  deleteKnowledgeSource,
  downloadSourceDocument,
  downloadSubmissionDocument,
  listAdminSubmissions,
  listCleanupFailures,
  listKnowledgeSources,
  rejectSubmission,
  setKnowledgeSourceEnabled,
  retryCleanup,
  type CleanupFailure,
  type KnowledgeSource,
} from "../../../api/adminKnowledge";
import { ApiError } from "../../../api/auth";
import type { Submission, SubmissionStatus } from "../../../api/submissions";
import { ConfirmDialog } from "../../shared/ConfirmDialog";
import { formatBytes, formatDate } from "../../shared/format";

type AdminTab = "review" | "sources" | "history" | "cleanup";
const submissionsKey = ["admin", "knowledge", "submissions"] as const;
const sourcesKey = ["admin", "knowledge", "sources"] as const;
const cleanupKey = ["admin", "cleanup-failures"] as const;

const statusCopy: Record<SubmissionStatus, { label: string; detail: string }> = {
  PENDING_REVIEW: { label: "待审核", detail: "等待管理员处理" },
  PUBLISHING: { label: "发布中", detail: "正在构建知识索引" },
  PUBLISHED: { label: "已发布", detail: "知识源已建立" },
  PUBLISH_FAILED: { label: "发布失败", detail: "发布未完成，系统不会自动重试" },
  REJECTED: { label: "已拒绝", detail: "审核已结束" },
};

function adminError(error: unknown): string {
  if (!(error instanceof ApiError)) return "请求未完成，请稍后重试";
  if (error.code === "PUBLICATION_CAPACITY_EXCEEDED") return "当前有资料正在发布，请稍后重试";
  if (error.code === "STATE_CONFLICT") return "记录状态已变化，请刷新后重试";
  if (error.status === 401) return "登录已失效，请重新登录";
  if (error.status === 403) return "当前身份无权访问 Knowledge 管理";
  if (error.status === 404) return "记录已不存在";
  return error.message || "请求未完成，请稍后重试";
}

function StatusIcon({ status }: { status: SubmissionStatus }) {
  if (status === "PUBLISHED") return <CheckCircle2 size={16} />;
  if (status === "PUBLISHING") return <LoaderCircle className="spin" size={16} />;
  if (status === "PENDING_REVIEW") return <Clock3 size={16} />;
  return <XCircle size={16} />;
}

function StatusCell({ submission }: { submission: Submission }) {
  const copy = statusCopy[submission.status];
  return (
    <div className={`admin-status ${submission.status.toLowerCase()}`}>
      <span><StatusIcon status={submission.status} />{copy.label}</span>
      <small>
        {submission.status === "REJECTED" && submission.rejection_reason
          ? submission.rejection_reason
          : copy.detail}
      </small>
    </div>
  );
}

function DocumentCell({ submission }: { submission: Submission }) {
  return (
    <div className="admin-document-cell">
      <span className="admin-document-icon" aria-hidden="true"><FileText size={18} /></span>
      <span>
        <strong title={submission.filename}>{submission.filename}</strong>
        <small>{submission.page_count} 页 · {formatBytes(submission.size_bytes)}</small>
      </span>
    </div>
  );
}

export function KnowledgeAdminWorkspace() {
  const client = useQueryClient();
  const [tab, setTab] = useState<AdminTab>("review");
  const [feedback, setFeedback] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<KnowledgeSource | null>(null);
  const submissions = useQuery({
    queryKey: submissionsKey,
    queryFn: listAdminSubmissions,
    refetchInterval: (query) =>
      query.state.data?.items.some((item) => item.status === "PUBLISHING") ? 2000 : false,
  });
  const sources = useQuery({ queryKey: sourcesKey, queryFn: listKnowledgeSources });
  const cleanup = useQuery({ queryKey: cleanupKey, queryFn: listCleanupFailures, enabled: tab === "cleanup" });

  const pending = useMemo(
    () => submissions.data?.items.filter((item) => item.status === "PENDING_REVIEW") ?? [],
    [submissions.data],
  );
  const history = useMemo(
    () => submissions.data?.items.filter((item) => item.status !== "PENDING_REVIEW") ?? [],
    [submissions.data],
  );
  const publishedIds = useMemo(
    () => submissions.data?.items.filter((item) => item.status === "PUBLISHED").map((item) => item.id).join(",") ?? "",
    [submissions.data],
  );

  useEffect(() => {
    if (publishedIds) void client.invalidateQueries({ queryKey: sourcesKey });
  }, [client, publishedIds]);

  async function refreshAll() {
    setFeedback(null);
    await Promise.all([submissions.refetch(), sources.refetch()]);
  }

  const review = useMutation({
    mutationFn: ({ id, action }: { id: string; action: "approve" | "reject" }) =>
      action === "approve" ? approveSubmission(id) : rejectSubmission(id),
    onSuccess: async (result) => {
      setFeedback(result.status === "PUBLISHING" ? "资料已进入发布中" : "审核状态已更新");
      await Promise.all([
        client.invalidateQueries({ queryKey: submissionsKey }),
        client.invalidateQueries({ queryKey: sourcesKey }),
      ]);
    },
    onError: async (error) => {
      setFeedback(adminError(error));
      await client.invalidateQueries({ queryKey: submissionsKey });
    },
  });

  const toggle = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) =>
      setKnowledgeSourceEnabled(id, enabled),
    onSuccess: async () => {
      setFeedback("知识源状态已更新");
      await client.invalidateQueries({ queryKey: sourcesKey });
    },
    onError: async (error) => {
      setFeedback(adminError(error));
      await client.invalidateQueries({ queryKey: sourcesKey });
    },
  });

  const removeSource = useMutation({
    mutationFn: (source: KnowledgeSource) => deleteKnowledgeSource(source.id),
    onSuccess: async () => {
      setDeleteTarget(null);
      setFeedback("知识源已删除");
      await Promise.all([
        client.invalidateQueries({ queryKey: sourcesKey }),
        client.invalidateQueries({ queryKey: cleanupKey }),
      ]);
    },
    onError: (error) => setFeedback(adminError(error)),
  });

  const retry = useMutation({
    mutationFn: (item: CleanupFailure) => retryCleanup(item.resource_type, item.resource_id),
    onSuccess: async () => {
      setFeedback("清理已重新开始");
      await client.invalidateQueries({ queryKey: cleanupKey });
    },
    onError: async (error) => {
      setFeedback(adminError(error));
      await client.invalidateQueries({ queryKey: cleanupKey });
    },
  });

  const documentDownload = useMutation({
    mutationFn: ({ kind, id, filename }: { kind: "submission" | "source"; id: string; filename: string }) =>
      kind === "submission"
        ? downloadSubmissionDocument(id, filename)
        : downloadSourceDocument(id, filename),
    onError: (error) => setFeedback(adminError(error)),
  });

  const busyReviewId = review.isPending ? review.variables?.id : null;
  const busySourceId = toggle.isPending ? toggle.variables?.id : null;

  return (
    <section className="admin-knowledge-workspace" aria-labelledby="admin-knowledge-heading">
      <header className="admin-knowledge-header">
        <div>
          <span>KNOWLEDGE ADMIN</span>
          <h1 id="admin-knowledge-heading">Knowledge 管理</h1>
        </div>
        <button
          className="icon-button"
          type="button"
          onClick={() => void refreshAll()}
          disabled={submissions.isFetching || sources.isFetching || cleanup.isFetching}
          aria-label="刷新 Knowledge 管理数据"
          title="刷新"
        >
          <RefreshCw className={submissions.isFetching || sources.isFetching || cleanup.isFetching ? "spin" : ""} size={17} />
        </button>
      </header>

      <nav className="admin-tabs" aria-label="Knowledge 管理视图">
        <button type="button" className={tab === "review" ? "active" : ""} onClick={() => setTab("review")} aria-current={tab === "review" ? "page" : undefined}>
          <Clock3 size={16} />待审核<span>{pending.length}</span>
        </button>
        <button type="button" className={tab === "sources" ? "active" : ""} onClick={() => setTab("sources")} aria-current={tab === "sources" ? "page" : undefined}>
          <Library size={16} />知识库<span>{sources.data?.items.length ?? 0}</span>
        </button>
        <button type="button" className={tab === "history" ? "active" : ""} onClick={() => setTab("history")} aria-current={tab === "history" ? "page" : undefined}>
          <History size={16} />历史记录<span>{history.length}</span>
        </button>
        <button type="button" className={tab === "cleanup" ? "active" : ""} onClick={() => setTab("cleanup")} aria-current={tab === "cleanup" ? "page" : undefined}>
          <RotateCcw size={16} />清理失败<span>{cleanup.data?.items.length ?? 0}</span>
        </button>
      </nav>

      {feedback && <div className="admin-feedback" role="status"><AlertCircle size={16} />{feedback}</div>}

      <div className="admin-knowledge-content">
        {tab !== "sources" && submissions.isPending && <AdminState loading label="正在加载投稿记录…" />}
        {tab !== "sources" && submissions.isError && <AdminState error label={adminError(submissions.error)} onRetry={() => void submissions.refetch()} />}
        {tab === "sources" && sources.isPending && <AdminState loading label="正在加载知识源…" />}
        {tab === "sources" && sources.isError && <AdminState error label={adminError(sources.error)} onRetry={() => void sources.refetch()} />}
        {tab === "cleanup" && cleanup.isPending && <AdminState loading label="正在加载清理状态…" />}
        {tab === "cleanup" && cleanup.isError && <AdminState error label={adminError(cleanup.error)} onRetry={() => void cleanup.refetch()} />}

        {tab === "review" && submissions.data && (
          pending.length === 0 ? <AdminState label="当前没有待审核投稿" /> :
          <div className="admin-records" role="table" aria-label="待审核投稿">
            <div className="admin-record-header review" role="row"><span>文档</span><span>提交时间</span><span>状态</span><span>操作</span></div>
            {pending.map((item) => (
              <div className="admin-record review" role="row" key={item.id}>
                <DocumentCell submission={item} />
                <time dateTime={item.created_at}>{formatDate(item.created_at)}</time>
                <StatusCell submission={item} />
                <div className="admin-actions">
                  <button className="icon-button" type="button" onClick={() => documentDownload.mutate({ kind: "submission", id: item.id, filename: item.filename })} disabled={documentDownload.isPending} aria-label={`下载 ${item.filename}`} title="下载 PDF"><Download size={16} /></button>
                  <button className="admin-action reject" type="button" onClick={() => review.mutate({ id: item.id, action: "reject" })} disabled={busyReviewId !== null}><X size={15} />拒绝</button>
                  <button className="admin-action approve" type="button" onClick={() => review.mutate({ id: item.id, action: "approve" })} disabled={busyReviewId !== null}><Check size={15} />批准</button>
                </div>
              </div>
            ))}
          </div>
        )}

        {tab === "sources" && sources.data && (
          sources.data.items.length === 0 ? <AdminState label="知识库中还没有已发布资料" /> :
          <div className="admin-records" role="table" aria-label="已发布知识源">
            <div className="admin-record-header source" role="row"><span>文档</span><span>状态</span><span>启用</span><span>操作</span></div>
            {sources.data.items.map((source) => (
              <SourceRow key={source.id} source={source} busy={busySourceId !== null || removeSource.isPending} onToggle={(enabled) => toggle.mutate({ id: source.id, enabled })} onDownload={() => documentDownload.mutate({ kind: "source", id: source.id, filename: source.filename })} onDelete={() => setDeleteTarget(source)} />
            ))}
          </div>
        )}

        {tab === "history" && submissions.data && (
          history.length === 0 ? <AdminState label="当前没有历史记录" /> :
          <div className="admin-records" role="table" aria-label="投稿历史记录">
            <div className="admin-record-header history" role="row"><span>文档</span><span>提交时间</span><span>状态</span><span>完成时间</span></div>
            {history.map((item) => (
              <div className="admin-record history" role="row" key={item.id}>
                <DocumentCell submission={item} />
                <time dateTime={item.created_at}>{formatDate(item.created_at)}</time>
                <StatusCell submission={item} />
                <time dateTime={item.published_at ?? item.reviewed_at ?? item.created_at}>{formatDate(item.published_at ?? item.reviewed_at ?? item.created_at)}</time>
              </div>
            ))}
          </div>
        )}

        {tab === "cleanup" && cleanup.data && (
          cleanup.data.items.length === 0 ? <AdminState label="当前没有待处理的清理失败" /> :
          <div className="admin-records cleanup-records" role="table" aria-label="清理失败记录">
            <div className="admin-record-header cleanup" role="row"><span>资源</span><span>步骤</span><span>状态</span><span>操作</span></div>
            {cleanup.data.items.map((item) => <CleanupRow key={`${item.resource_type}-${item.resource_id}-${item.step}`} item={item} busy={retry.isPending} onRetry={() => retry.mutate(item)} />)}
          </div>
        )}
      </div>
      {deleteTarget && <ConfirmDialog title="删除这个知识源？" description={`“${deleteTarget.filename}”会立即从知识库隐藏。历史消息仍保留，但引用预览会显示“来源已删除”。`} busy={removeSource.isPending} onCancel={() => setDeleteTarget(null)} onConfirm={() => removeSource.mutate(deleteTarget)} />}
    </section>
  );
}

function SourceRow({ source, busy, onToggle, onDownload, onDelete }: { source: KnowledgeSource; busy: boolean; onToggle: (enabled: boolean) => void; onDownload: () => void; onDelete: () => void }) {
  return (
    <div className="admin-record source" role="row">
      <div className="admin-document-cell">
        <span className="admin-document-icon published" aria-hidden="true"><FileText size={18} /></span>
        <span><strong title={source.filename}>{source.filename}</strong><small>Knowledge Source</small></span>
      </div>
      <div className="admin-status published"><span><CheckCircle2 size={16} />已发布</span><small>{source.enabled ? "可用于未来来源" : "当前已停用"}</small></div>
      <button className={`source-switch ${source.enabled ? "enabled" : ""}`} type="button" role="switch" aria-checked={source.enabled} aria-label={`${source.enabled ? "停用" : "启用"} ${source.filename}`} title={source.enabled ? "停用" : "启用"} disabled={busy} onClick={() => onToggle(!source.enabled)}><span /></button>
      <div className="admin-actions"><button className="icon-button" type="button" onClick={onDownload} aria-label={`下载 ${source.filename}`} title="下载 PDF"><Download size={16} /></button><button className="icon-button danger-icon" type="button" onClick={onDelete} aria-label={`删除 ${source.filename}`} title="删除知识源"><Trash2 size={16} /></button></div>
    </div>
  );
}

function CleanupRow({ item, busy, onRetry }: { item: CleanupFailure; busy: boolean; onRetry: () => void }) {
  const resourceLabel = item.resource_type === "ATTACHMENT" ? "会话附件" : "知识投稿";
  return <div className="admin-record cleanup" role="row">
    <div className="admin-document-cell"><span className="admin-document-icon" aria-hidden="true"><RotateCcw size={18} /></span><span><strong>{resourceLabel}</strong><small>{item.resource_id}</small></span></div>
    <span className="cleanup-step">{item.step === "ES" ? "Elasticsearch" : "MinIO"}</span>
    <div className={`admin-status cleanup-${item.status.toLowerCase()}`}><span>{item.status === "FAILED" ? <XCircle size={16} /> : <Clock3 size={16} />}{item.status === "FAILED" ? "失败" : "待处理"}</span><small>{item.error_code ?? "等待清理"}</small></div>
    <div className="admin-actions"><button className="admin-action approve" type="button" onClick={onRetry} disabled={busy}><RotateCcw size={15} />重试</button></div>
  </div>;
}

function AdminState({ label, loading = false, error = false, onRetry }: { label: string; loading?: boolean; error?: boolean; onRetry?: () => void }) {
  return (
    <div className={`admin-state ${error ? "error" : ""}`} role={error ? "alert" : "status"}>
      {loading ? <LoaderCircle className="spin" size={22} /> : error ? <AlertCircle size={22} /> : <FileText size={22} />}
      <span>{label}</span>
      {onRetry && <button type="button" onClick={onRetry}>重试</button>}
    </div>
  );
}
