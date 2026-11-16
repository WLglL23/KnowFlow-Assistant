import { PanelRightClose, PanelRightOpen } from "lucide-react";
import { useEffect, useId, useRef, useState, type CSSProperties, type KeyboardEvent, type PointerEvent, type ReactNode } from "react";

const DEFAULT_WIDTH = 340;
const MIN_WIDTH = 280;
const MAX_WIDTH = 900;
const STORAGE_KEY = "knowflow.context-panel";

function readPreference(): { width: number; collapsed: boolean } {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "{}");
    return {
      width: typeof saved.width === "number" && Number.isFinite(saved.width)
        ? Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, saved.width)) : DEFAULT_WIDTH,
      collapsed: saved.collapsed === true,
    };
  } catch { return { width: DEFAULT_WIDTH, collapsed: false }; }
}

export function ResizableContextLayout({ children, sidebar, revealSignal }: {
  children: ReactNode;
  sidebar: ReactNode;
  revealSignal: object | null;
}) {
  const [preference] = useState(readPreference);
  const [width, setWidth] = useState(preference.width);
  const [collapsed, setCollapsed] = useState(preference.collapsed);
  const [availableWidth, setAvailableWidth] = useState(0);
  const [resizing, setResizing] = useState(false);
  const container = useRef<HTMLElement>(null);
  const drag = useRef<{ pointerId: number; x: number; width: number } | null>(null);
  const contentId = useId();
  const overlay = availableWidth > 0 && availableWidth < 640;
  const maximum = availableWidth > 0
    ? Math.min(MAX_WIDTH, Math.max(120, availableWidth - (overlay ? 48 : 368))) : MAX_WIDTH;
  const minimum = Math.min(MIN_WIDTH, maximum);
  const panelWidth = Math.round(Math.min(maximum, Math.max(minimum, width)));

  useEffect(() => {
    const element = container.current;
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => setAvailableWidth(entry.contentRect.width));
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (revealSignal) setCollapsed(false);
  }, [revealSignal]);

  useEffect(() => {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify({ width, collapsed })); }
    catch { /* Layout controls remain usable when browser storage is unavailable. */ }
  }, [width, collapsed]);

  function startResize(event: PointerEvent<HTMLDivElement>) {
    if (event.button !== 0 || !event.isPrimary) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    drag.current = { pointerId: event.pointerId, x: event.clientX, width: panelWidth };
    setResizing(true);
  }

  function resize(event: PointerEvent<HTMLDivElement>) {
    const origin = drag.current;
    if (!origin || origin.pointerId !== event.pointerId) return;
    setWidth(Math.min(maximum, Math.max(minimum, origin.width + origin.x - event.clientX)));
  }

  function finishResize(event: PointerEvent<HTMLDivElement>) {
    drag.current = null;
    setResizing(false);
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
  }

  function resizeWithKeyboard(event: KeyboardEvent<HTMLDivElement>) {
    const next = { ArrowLeft: panelWidth + 24, ArrowRight: panelWidth - 24,
      Home: minimum, End: maximum }[event.key];
    if (next === undefined) return;
    event.preventDefault();
    setWidth(Math.min(maximum, Math.max(minimum, next)));
  }

  return (
    <section ref={container}
      className={`workspace-main conversation-workbench context-layout ${collapsed ? "context-collapsed" : ""} ${overlay ? "context-overlay" : ""} ${resizing ? "context-resizing" : ""}`}
      style={{ "--context-panel-width": `${panelWidth}px` } as CSSProperties}>
      {children}
      <div className="context-resizer" hidden={collapsed} role="separator" tabIndex={collapsed ? -1 : 0}
        aria-label="调整右侧栏宽度" aria-orientation="vertical" aria-controls={contentId}
        aria-valuemin={minimum} aria-valuemax={maximum} aria-valuenow={panelWidth}
        aria-valuetext={`${panelWidth} 像素`} title="拖动调整宽度，方向键微调，双击恢复默认"
        onPointerDown={startResize} onPointerMove={resize} onPointerUp={finishResize}
        onPointerCancel={finishResize} onLostPointerCapture={() => { drag.current = null; setResizing(false); }}
        onKeyDown={resizeWithKeyboard} onDoubleClick={() => setWidth(DEFAULT_WIDTH)} />
      <aside className={`context-panel-shell ${collapsed ? "is-collapsed" : ""}`} aria-label="资料侧栏">
        <header className="context-panel-toolbar">
          {!collapsed && <span>资料与引用</span>}
          <button className="icon-button" type="button" onClick={() => setCollapsed((value) => !value)}
            aria-label={collapsed ? "展开右侧栏" : "折叠右侧栏"} aria-expanded={!collapsed}
            aria-controls={contentId} title={collapsed ? "展开右侧栏" : "折叠右侧栏"}>
            {collapsed ? <PanelRightOpen size={18} /> : <PanelRightClose size={18} />}
          </button>
        </header>
        <div id={contentId} className="context-panel-content" hidden={collapsed}>{sidebar}</div>
      </aside>
    </section>
  );
}
