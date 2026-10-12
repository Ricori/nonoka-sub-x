import { useEffect, useRef, useState } from "react";
import { hasCloudSubtitles } from "../bridge/cloud.ts";
import type { CloudEntry } from "../bridge/cloud.ts";
import { mediaLibrary } from "../bridge/library.ts";
import type { MediaEntry } from "../bridge/library.ts";
import { formatDate, formatDuration, formatSize } from "../app/format.ts";

interface LocalMediaCardProps {
  entry: MediaEntry;
  thumbnail?: string;
  running: boolean;
  /** The cloud already holds subtitles for this media and they are being
      pulled down right now. Offering 开始任务 over that spends a cloud task on
      text the app is seconds away from having. */
  adopting: boolean;
  runningProgress: number;
  cloudEntry?: CloudEntry;
  canStart: boolean;
  onOpen: (entry: MediaEntry) => void;
  onStart: (entry: MediaEntry) => Promise<void>;
  onCancel: () => Promise<void>;
  onRename: (entry: MediaEntry) => void;
  onDeleteSubtitles: (entry: MediaEntry) => void;
  onRemove: (entry: MediaEntry) => void;
  onAdoptCloud: (entry: MediaEntry, cloudEntry: CloudEntry) => Promise<void>;
  onDeleteCloud: (entry: CloudEntry) => void;
  onRelink: (entry: MediaEntry) => Promise<void>;
}

interface MenuAction {
  label: string;
  danger?: boolean;
  disabled?: boolean;
  onSelect: () => void;
}

function CardMenu({ actions }: { actions: MenuAction[] }) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const closeOnOutside = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", closeOnOutside);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOnOutside);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [open]);

  return (
    <div className={`card-menu ${open ? "open" : ""}`} ref={root}>
      <button className="card-menu-trigger" type="button" aria-label="更多操作" aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen((value) => !value)}>
        <span /><span /><span />
      </button>
      {open && <div className="card-menu-popover" role="menu">
        {actions.map((action) => <button key={action.label} type="button" role="menuitem" className={action.danger ? "danger" : ""} disabled={action.disabled} onClick={() => { setOpen(false); action.onSelect(); }}>{action.label}</button>)}
      </div>}
    </div>
  );
}

export function LocalMediaCard(props: LocalMediaCardProps) {
  const { entry, thumbnail, running, adopting, runningProgress, cloudEntry, canStart, onOpen, onStart, onCancel, onRename, onDeleteSubtitles, onRemove, onAdoptCloud, onDeleteCloud, onRelink } = props;
  // Subtitles adopted from the cloud before the video reached this machine.
  // The entry is unavailable like a missing file, but nothing was lost, so it
  // reads as "no video yet" rather than "文件丢失".
  const subtitleOnly = !entry.available && !entry.sourcePath;
  const [cancelling, setCancelling] = useState(false);
  const cancel = async () => {
    setCancelling(true);
    try {
      await onCancel();
    } finally {
      setCancelling(false);
    }
  };
  const menuActions: MenuAction[] = [
    { label: "重命名", onSelect: () => onRename(entry) },
    ...(entry.documentAvailable && entry.available ? [{ label: "重跑任务", disabled: !canStart || adopting, onSelect: () => void onStart(entry) }] : []),
    ...(entry.available ? [{ label: "在文件夹中显示", onSelect: () => void mediaLibrary.revealInFolder(entry.sourcePath) }] : []),
    ...(entry.documentAvailable ? [{ label: "删除本地字幕", danger: true, onSelect: () => onDeleteSubtitles(entry) }] : []),
    ...(cloudEntry ? [{ label: "删除云端字幕", danger: true, disabled: cloudEntry.status === "queued" || cloudEntry.status === "running", onSelect: () => onDeleteCloud(cloudEntry) }] : []),
    { label: "从媒体库移除视频", danger: true, onSelect: () => onRemove(entry) },
  ];
  return (
    <article className={`media-card local-card ${!entry.available ? "missing-media" : ""}`}>
      <div className="media-thumb">
        {thumbnail ? <img src={thumbnail} alt="" /> : <span className="media-placeholder">{entry.audioOnly ? "♪" : "▶"}</span>}
        <small className="duration-badge">{formatDuration(entry.duration)}</small>
        <strong className={`state-badge ${running || adopting ? "running" : subtitleOnly ? "cloud" : !entry.available ? "missing" : entry.documentAvailable ? "ready" : "idle"}`}>{running ? "处理中" : adopting ? "取回字幕中" : subtitleOnly ? "仅字幕" : !entry.available ? "文件丢失" : entry.documentAvailable ? "字幕可编辑" : "等待处理"}</strong>
        {running && <span className="card-progress" style={{ width: `${runningProgress}%` }} />}
      </div>
      <div className="media-card-copy">
        <div className="media-title-row">
          <strong title={entry.sourcePath}>{entry.title}</strong>
          <CardMenu actions={menuActions} />
        </div>
        <span className="media-meta">{entry.width > 0 ? `${entry.width}×${entry.height} · ${formatSize(entry.size)} · ` : entry.audioOnly ? `纯音频 · ${formatSize(entry.size)} · ` : ""}{formatDate(entry.lastAccess || entry.addedAt)}</span>
        <div className="media-chips"><small>{entry.available ? "本机文件" : subtitleOnly ? "尚未关联视频" : "源文件离线"}</small>{cloudEntry && <small className="cloud-badge">☁ 字幕已同步</small>}</div>
        <div className="media-card-actions">
          {entry.documentAvailable && <button className="secondary-card-action" onClick={() => onOpen(entry)}>编辑字幕</button>}
          {!entry.available ? (
            <button className="secondary-card-action" onClick={() => void onRelink(entry)}>{entry.documentAvailable || subtitleOnly ? "关联视频" : "重新定位"}</button>
          ) : running ? (
            <button className="cancel-card-action" disabled={cancelling} onClick={() => void cancel()} title="停止本次处理；已完成的环节会保留，之后可以继续">{cancelling ? "正在取消…" : "取消处理"}</button>
          ) : !entry.documentAvailable && hasCloudSubtitles(cloudEntry) ? (
            <>
              <button className="primary-card-action" disabled={adopting} onClick={() => void onAdoptCloud(entry, cloudEntry)}>{adopting ? "取回云端字幕…" : "取回云端字幕"}</button>
              <button className="secondary-card-action" disabled={!canStart || adopting} onClick={() => void onStart(entry)}>重跑任务</button>
            </>
          ) : !entry.documentAvailable ? (
            <button className="primary-card-action" disabled={!canStart || adopting} onClick={() => void onStart(entry)}>开始任务</button>
          ) : null}
        </div>
      </div>
    </article>
  );
}

export function CloudMediaCard({ entry, thumbnail, adopting, onEdit, onAssociate, onDelete }: { entry: CloudEntry; thumbnail?: string; adopting: boolean; onEdit: (entry: CloudEntry) => Promise<void>; onAssociate: (entry: CloudEntry) => Promise<void>; onDelete: (entry: CloudEntry) => void }) {
  return (
    <article className="media-card cloud-card">
      <div className="media-thumb cloud-thumb">{thumbnail ? <img src={thumbnail} alt="" /> : <span>☁</span>}<small className="duration-badge">{formatDuration(entry.duration)}</small><strong className="state-badge cloud">仅云端字幕</strong></div>
      <div className="media-card-copy">
        <div className="media-title-row"><strong title={entry.title}>{entry.title}</strong><CardMenu actions={[{ label: "删除云端字幕", danger: true, disabled: entry.status === "queued" || entry.status === "running", onSelect: () => onDelete(entry) }]} /></div>
        <span className="media-meta">{entry.status} · {formatDate(entry.updatedAt)}</span>
        <div className="media-chips"><small className="cloud-badge">{entry.source === "local_sync" ? "由本机自动同步" : "云端容器任务"}</small>{entry.ownerName && <small className="cloud-badge">{entry.ownerName}</small>}</div>
        <div className="media-card-actions">
          {hasCloudSubtitles(entry) && <button className="secondary-card-action" disabled={adopting} onClick={() => void onEdit(entry)} title="不需要本地视频，字幕会取回本机后直接打开编辑器">{adopting ? "取回字幕…" : "编辑字幕"}</button>}
          <button className="secondary-card-action" disabled={adopting || !hasCloudSubtitles(entry)} onClick={() => void onAssociate(entry)}>关联本地视频</button>
        </div>
      </div>
    </article>
  );
}
