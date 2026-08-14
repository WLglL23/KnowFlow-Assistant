import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertCircle, LoaderCircle, Plus, Send } from "lucide-react";
import { useRef, useState } from "react";

import { ApiError } from "../../api/auth";
import {
  listAttachments,
  uploadAttachment,
  type Attachment,
  type AttachmentUploadResult,
} from "../../api/conversations";

const resultLabels: Partial<Record<AttachmentUploadResult, string>> = {
  EXISTING_QUEUED: "相同附件正在等待处理",
  EXISTING_PROCESSING: "相同附件正在处理",
  ALREADY_EXISTS: "相同附件已存在",
};

function attachmentError(error: unknown): string {
  if (!(error instanceof ApiError)) return "附件上传失败，请稍后重试";
  const messages: Record<string, string> = {
    DOCUMENT_TOO_LARGE: "PDF 超过 10 MB 或 100 页",
    INVALID_PDF: "PDF 无法读取，请检查文件是否损坏或受密码保护",
    SOURCE_LIMIT_EXCEEDED: "当前会话的附件名额已用完",
    VALIDATION_FAILED: "请选择一个有效的 PDF 文件",
    NOT_FOUND: "会话不可见或已不存在",
  };
  return messages[error.code] ?? "附件上传失败，请稍后重试";
}

function attachmentState(attachment: Attachment): { label: string; tone: string } {
  if (attachment.status === "QUEUED") return { label: "等待处理", tone: "queued" };
  if (attachment.status === "PROCESSING") {
    const progress = attachment.processing_progress;
    return { label: typeof progress === "number" ? `处理中 ${progress}%` : "处理中", tone: "processing" };
  }
  if (attachment.status === "READY") return { label: "已引用", tone: "ready" };
  return { label: "处理失败", tone: "failed" };
}

type ConversationComposerProps = {
  accountId: string;
  conversationId: string | null;
  draft: string;
  streaming: boolean;
  onDraftChange: (value: string) => void;
  onSend: () => void;
};

export function ConversationComposer({
  accountId,
  conversationId,
  draft,
  streaming,
  onDraftChange,
  onSend,
}: ConversationComposerProps) {
  const queryClient = useQueryClient();
  const fileInput = useRef<HTMLInputElement>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const queryKey = ["attachments", accountId, conversationId] as const;
  const attachments = useQuery({
    queryKey,
    queryFn: () => listAttachments(conversationId as string),
    enabled: conversationId !== null,
    refetchInterval: (query) => query.state.data?.items.some((item) => item.status === "QUEUED" || item.status === "PROCESSING") ? 1500 : false,
  });
  const occupied = attachments.data?.items.filter((item) => item.status !== "FAILED").length ?? 0;
  const upload = useMutation({
    mutationFn: (file: File) => uploadAttachment(conversationId as string, file),
    onSuccess: async (response) => {
      setError(null);
      setNotice(resultLabels[response.result] ?? null);
      await queryClient.invalidateQueries({ queryKey });
    },
    onError: async (cause) => {
      setNotice(null);
      setError(attachmentError(cause));
      await queryClient.invalidateQueries({ queryKey });
    },
  });

  function chooseFile(file: File | undefined) {
    if (!file || !conversationId) return;
    setError(null);
    setNotice(null);
    upload.mutate(file);
    if (fileInput.current) fileInput.current.value = "";
  }

  return (
    <div className="composer-shell">
      <div className="attachment-strip" aria-label="当前会话附件">
        <span className="attachment-quota">附件 {occupied}/5</span>
        {attachments.data?.items.map((attachment) => {
          const state = attachmentState(attachment);
          return (
            <div className={`attachment-inline ${state.tone}`} key={attachment.id} title={`${state.label} · ${attachment.filename}`}>
              {attachment.status === "PROCESSING" && <LoaderCircle className="spin" size={13} aria-hidden="true" />}
              <span className="attachment-inline-state">{state.label}</span>
              <span aria-hidden="true">·</span>
              <span className="attachment-inline-name">{attachment.filename}</span>
            </div>
          );
        })}
      </div>
      {(notice || error || attachments.isError) && (
        <div className={`composer-feedback ${error || attachments.isError ? "error" : "success"}`} role={error || attachments.isError ? "alert" : "status"}>
          {attachments.isError ? <><AlertCircle size={14} />附件状态加载失败<button type="button" onClick={() => void attachments.refetch()}>重试</button></> : error ?? notice}
        </div>
      )}
      <div className="composer">
        <input ref={fileInput} className="visually-hidden" type="file" accept="application/pdf,.pdf" onChange={(event) => chooseFile(event.target.files?.[0])} />
        <button
          className="composer-icon-button attachment-trigger"
          type="button"
          onClick={() => fileInput.current?.click()}
          disabled={!conversationId || occupied >= 5 || upload.isPending}
          aria-label={occupied >= 5 ? "附件名额已满（5/5）" : `上传 PDF（${occupied}/5）`}
          title={occupied >= 5 ? "附件名额已满（5/5）" : `上传 PDF（${occupied}/5）`}
        >
          {upload.isPending ? <LoaderCircle className="spin" size={18} /> : <Plus size={19} />}
        </button>
        <textarea
          rows={1}
          value={draft}
          onChange={(event) => onDraftChange(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              onSend();
            }
          }}
          placeholder="输入你的问题…"
          aria-label="问题输入"
          disabled={streaming || !conversationId}
        />
        <button className="send-button" type="button" onClick={onSend} disabled={streaming || !conversationId || !draft.trim()} aria-label="发送问题" title="发送"><Send size={18} /></button>
      </div>
    </div>
  );
}
