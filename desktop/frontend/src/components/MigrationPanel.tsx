import type { MigrationView } from "./migration.ts";

interface MigrationPanelProps {
  view: MigrationView;
  retrying: boolean;
  onRetry: () => void;
  onDismiss: () => void;
}

/**
 * 旧版数据迁移的进度与结果（WP-M2）。数据还没打开时它占住整个工作区；之后在后台下载
 * 新的运行环境时、以及完成后的结果，它是页面顶上的一块卡片。
 */
export function MigrationPanel({ view, retrying, onRetry, onDismiss }: MigrationPanelProps) {
  if (!view.visible) return null;
  return (
    <section className={`migration-panel ${view.tone}${view.blocking ? " blocking" : ""}`} role="status" aria-live="polite">
      <header>
        <div className="migration-symbol" aria-hidden="true">{view.tone === "success" ? "✓" : view.tone === "info" ? "↻" : "!"}</div>
        <div className="migration-copy">
          <span className="eyebrow">旧版数据迁移</span>
          <h2>{view.heading}</h2>
          {view.message && <p>{view.message}</p>}
        </div>
        <div className="migration-actions">
          {view.canRetry && <button className="primary-button" disabled={retrying} onClick={onRetry}>{retrying ? "正在重试…" : "重试"}</button>}
          {view.dismissible && <button className="quiet-button" onClick={onDismiss}>知道了</button>}
        </div>
      </header>
      {view.stepLine && <p className="migration-step">{view.stepLine}</p>}
      {view.percent !== null && <div className="progress-track"><span style={{ width: `${view.percent}%` }} /></div>}
      {view.progressLine && <p className="migration-progress">{view.progressLine}</p>}
      {view.error && <p className="migration-error">{view.error}</p>}
      {view.blockers.length > 0 && (
        <ul className="migration-blockers">
          {view.blockers.map((blocker) => <li key={blocker}>{blocker}</li>)}
        </ul>
      )}
      {view.sections.map((section) => (
        <details key={section.title} className="migration-section" open={view.blocking}>
          <summary>{section.title}（{section.items.length}）</summary>
          <ul>{section.items.map((item) => <li key={item}>{item}</li>)}</ul>
        </details>
      ))}
    </section>
  );
}
