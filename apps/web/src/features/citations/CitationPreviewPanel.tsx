import { useQuery } from "@tanstack/react-query";
import { AlertCircle, FileText, X } from "lucide-react";
import { useEffect, useState } from "react";
import { Document, Page, pdfjs } from "react-pdf";
import "react-pdf/dist/Page/AnnotationLayer.css";
import "react-pdf/dist/Page/TextLayer.css";

import { ApiError } from "../../api/auth";
import { getCitationPreview, type Citation } from "../../api/conversations";

pdfjs.GlobalWorkerOptions.workerSrc = new URL("pdfjs-dist/build/pdf.worker.min.mjs", import.meta.url).toString();

export type CitationSelection = { messageId: string; citationIndex: number; citation: Citation };

function previewError(error: unknown): string {
  if (error instanceof ApiError && error.code === "CITATION_SOURCE_DELETED") return "来源已删除";
  if (error instanceof ApiError && error.status === 404) return "引用不可见或已不存在";
  return "引用页暂时无法预览";
}

export function CitationPreviewPanel({ selection, onClose }: { selection: CitationSelection; onClose: () => void }) {
  const [objectUrl, setObjectUrl] = useState<string | null>(null);
  const [pageCount, setPageCount] = useState(0);
  const preview = useQuery({
    queryKey: ["citation-preview", selection.messageId, selection.citationIndex],
    queryFn: () => getCitationPreview(selection.messageId, selection.citationIndex),
  });

  useEffect(() => {
    if (!preview.data) return;
    const url = URL.createObjectURL(preview.data);
    setObjectUrl(url);
    return () => { URL.revokeObjectURL(url); setObjectUrl(null); };
  }, [preview.data]);

  return (
    <aside className="citation-panel" aria-label="引用页预览">
      <header className="citation-header">
        <div><span>引用预览</span><h3 title={selection.citation.filename}>{selection.citation.filename}</h3></div>
        <button className="icon-button" type="button" onClick={onClose} aria-label="关闭引用预览" title="关闭"><X size={18} /></button>
      </header>
      <div className="citation-pages-label"><FileText size={15} /><span>原文第 {selection.citation.pages.join("、")} 页</span></div>
      <div className="citation-document">
        {preview.isPending && <div className="citation-state">正在加载授权页…</div>}
        {preview.isError && <div className="citation-state error" role="alert"><AlertCircle size={22} /><span>{previewError(preview.error)}</span><button type="button" onClick={() => void preview.refetch()}>重试</button></div>}
        {objectUrl && (
          <Document file={objectUrl} onLoadSuccess={({ numPages }) => setPageCount(numPages)} loading={<div className="citation-state">正在解析引用页…</div>} error={<div className="citation-state error">引用页无法显示</div>}>
            {Array.from({ length: pageCount }, (_, index) => <Page key={index} pageNumber={index + 1} width={306} renderAnnotationLayer={false} renderTextLayer />)}
          </Document>
        )}
      </div>
    </aside>
  );
}
