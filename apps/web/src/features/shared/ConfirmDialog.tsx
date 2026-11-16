import { AlertTriangle, X } from "lucide-react";

export function ConfirmDialog({
  title,
  description,
  confirmLabel = "永久删除",
  busy = false,
  onConfirm,
  onCancel,
}: {
  title: string;
  description: string;
  confirmLabel?: string;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <div className="confirm-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onCancel(); }}>
      <section className="confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="confirm-dialog-title">
        <button className="icon-button confirm-close" type="button" onClick={onCancel} disabled={busy} aria-label="关闭确认对话框" title="关闭"><X size={17} /></button>
        <div className="confirm-icon" aria-hidden="true"><AlertTriangle size={20} /></div>
        <h2 id="confirm-dialog-title">{title}</h2>
        <p>{description}</p>
        <div className="confirm-actions">
          <button className="secondary-button compact" type="button" onClick={onCancel} disabled={busy}>取消</button>
          <button className="danger-button compact" type="button" onClick={onConfirm} disabled={busy}>{busy ? "正在删除…" : confirmLabel}</button>
        </div>
      </section>
    </div>
  );
}
