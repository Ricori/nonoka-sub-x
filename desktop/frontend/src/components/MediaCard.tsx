import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { KeyboardEvent as ReactKeyboardEvent, MouseEvent as ReactMouseEvent } from "react";
import { createPortal } from "react-dom";
import { hasCloudSubtitles } from "../bridge/cloud.ts";
import type { CloudEntry } from "../bridge/cloud.ts";
import { mediaLibrary } from "../bridge/library.ts";
import type { MediaEntry } from "../bridge/library.ts";
import { formatDate, formatDuration, formatSize } from "../app/format.ts";
import type { ViewMode } from "../app/types.ts";

interface LocalMediaCardProps {
  entry: MediaEntry;
  view: ViewMode;
  thumbnail?: string;
  running: boolean;
  /** The cloud already holds subtitles for this media and they are being
      pulled down right now. Offering 开始任务 over that spends a cloud task on
      text the app is seconds away from having. */
  adopting: boolean;
  runningProgress: number;
  cloudEntry?: CloudEntry;
  canStart: boolean;
  /** A cloud key is stored, so 同步到云端 means something for this card. */
  cloudLinked: boolean;
  syncEnabled: boolean;
  onOpen: (entry: MediaEntry) => void;
  onStart: (entry: MediaEntry) => Promise<void>;
  onCancel: (entry: MediaEntry) => Promise<void>;
  onRename: (entry: MediaEntry) => void;
  /** One entry point for both copies: the dialog asks which one goes. */
  onDeleteSubtitles: (entry: MediaEntry, cloudEntry?: CloudEntry) => void;
  onToggleSync: (entry: MediaEntry) => Promise<void>;
  onRemove: (entry: MediaEntry) => void;
  onAdoptCloud: (entry: MediaEntry, cloudEntry: CloudEntry) => Promise<void>;
  onRelink: (entry: MediaEntry) => Promise<void>;
}

interface MenuAction {
  label: string;
  danger?: boolean;
  disabled?: boolean;
  /** Set for a switch: rendered as a checkable item with its state. */
  checked?: boolean;
  onSelect: () => void;
}

// Groups are separated by "separator"; groups that end up empty collapse, so
// callers can list every group unconditionally.
type MenuItem = MenuAction | "separator";

function tidySeparators(items: MenuItem[]): MenuItem[] {
  const result: MenuItem[] = [];
  for (const item of items) {
    if (item === "separator" && (result.length === 0 || result[result.length - 1] === "separator")) continue;
    result.push(item);
  }
  if (result[result.length - 1] === "separator") result.pop();
  return result;
}

interface MenuAnchor {
  x: number;
  y: number;
  /** Right-align the menu edge to `x` (used when triggered from the bottom-right `...` button). */
  alignRight?: boolean;
}

function CardContextMenu({ anchor, actions, onClose }: { anchor: MenuAnchor | null; actions: MenuItem[]; onClose: () => void }) {
  const menuRef = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null);

  useLayoutEffect(() => {
    if (!anchor) {
      setPos(null);
      return;
    }
    const el = menuRef.current;
    const width = el?.offsetWidth ?? 180;
    const height = el?.offsetHeight ?? 200;
    const pad = 10;
    let left = anchor.alignRight ? anchor.x - width : anchor.x;
    let top = anchor.y;
    if (left + width > window.innerWidth - pad) left = window.innerWidth - width - pad;
    if (left < pad) left = pad;
    if (top + height > window.innerHeight - pad) top = Math.max(pad, anchor.y - height - 8);
    setPos({ left, top });
  }, [anchor]);

  useEffect(() => {
    if (!anchor) return;
    const closeOnOutside = (event: PointerEvent) => {
      if (!menuRef.current?.contains(event.target as Node)) onClose();
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    const closeOnViewportChange = () => onClose();
    document.addEventListener("pointerdown", closeOnOutside, true);
    document.addEventListener("keydown", closeOnEscape);
    window.addEventListener("scroll", closeOnViewportChange, true);
    window.addEventListener("resize", closeOnViewportChange);
    return () => {
      document.removeEventListener("pointerdown", closeOnOutside, true);
      document.removeEventListener("keydown", closeOnEscape);
      window.removeEventListener("scroll", closeOnViewportChange, true);
      window.removeEventListener("resize", closeOnViewportChange);
    };
  }, [anchor, onClose]);

  if (!anchor) return null;

  return createPortal(
    <div
      ref={menuRef}
      className="card-menu-popover"
      role="menu"
      style={{
        left: pos ? `${pos.left}px` : `${anchor.x}px`,
        top: pos ? `${pos.top}px` : `${anchor.y}px`,
        visibility: pos ? "visible" : "hidden",
      }}
      onClick={(event) => event.stopPropagation()}
      onContextMenu={(event) => event.preventDefault()}
    >
      {tidySeparators(actions).map((action, index) => action === "separator"
        ? <span key={`separator-${index}`} className="card-menu-separator" role="separator" />
        : action.checked !== undefined
          ? <button key={action.label} type="button" role="menuitemcheckbox" aria-checked={action.checked} className="checkable" disabled={action.disabled} onClick={() => { onClose(); action.onSelect(); }}>{action.label}<span className="card-menu-check" aria-hidden="true">{action.checked ? "✓" : ""}</span></button>
          : <button key={action.label} type="button" role="menuitem" className={action.danger ? "danger" : ""} disabled={action.disabled} onClick={() => { onClose(); action.onSelect(); }}>{action.label}</button>)}
    </div>,
    document.body,
  );
}

function useCardMenu() {
  const [anchor, setAnchor] = useState<MenuAnchor | null>(null);
  const close = useCallback(() => setAnchor(null), []);
  const openAtContext = useCallback((event: ReactMouseEvent) => {
    event.preventDefault();
    event.stopPropagation();
    setAnchor({ x: event.clientX, y: event.clientY });
  }, []);
  const toggleFromButton = useCallback((event: ReactMouseEvent<HTMLButtonElement>) => {
    event.preventDefault();
    event.stopPropagation();
    if (anchor) {
      setAnchor(null);
      return;
    }
    const rect = event.currentTarget.getBoundingClientRect();
    setAnchor({ x: rect.right, y: rect.bottom + 6, alignRight: true });
  }, [anchor]);
  return { anchor, isOpen: anchor !== null, close, openAtContext, toggleFromButton };
}

function localMetaLine(entry: MediaEntry, subtitleOnly: boolean) {
  const parts: string[] = [];
  if (entry.available) {
    if (entry.audioOnly) parts.push("纯音频");
    if (entry.size > 0) parts.push(formatSize(entry.size));
  } else {
    parts.push(subtitleOnly ? "仅字幕" : "文件丢失");
  }
  const date = formatDate(entry.lastAccess || entry.addedAt);
  if (date !== "—") parts.push(date);
  return parts.join(" · ");
}

function cloudMetaLine(entry: CloudEntry) {
  const parts: string[] = ["云端字幕"];
  const date = entry.updatedAt ? formatDate(entry.updatedAt) : "—";
  if (date !== "—") parts.push(date);
  return parts.join(" · ");
}

function CloudBadgeIcon() {
  return (
    <svg className="state-badge-icon" viewBox="0 0 16 16" aria-hidden="true">
      <path d="M4.6 12.2h6.9a2.7 2.7 0 0 0 .35-5.38A4 4 0 0 0 4.1 6a2.95 2.95 0 0 0 .5 6.2Z" />
    </svg>
  );
}

function MediaPlaceholderIcon({ kind }: { kind: "video" | "audio" | "cloud" }) {
  if (kind === "cloud") {
    return (
      <svg className="media-placeholder-svg" viewBox="0 0 24 24" aria-hidden="true">
        <path d="M6.8 18.2h10.6a4.1 4.1 0 0 0 .5-8.17A6 6 0 0 0 6.1 9a4.4 4.4 0 0 0 .7 9.2Z" />
      </svg>
    );
  }
  if (kind === "audio") {
    return (
      <svg className="media-placeholder-svg" viewBox="0 0 24 24" aria-hidden="true">
        <path d="M9 17.5V6.5l10-2v11M9 17.5a2.5 2.5 0 1 1-5 0 2.5 2.5 0 0 1 5 0Zm10-2a2.5 2.5 0 1 1-5 0 2.5 2.5 0 0 1 5 0Z" />
      </svg>
    );
  }
  return (
    <svg className="media-placeholder-svg" viewBox="0 0 24 24" aria-hidden="true">
      <path d="M8.5 6.5 18 12l-9.5 5.5v-11Z" />
    </svg>
  );
}

export function LocalMediaCard(props: LocalMediaCardProps) {
  const { entry, view, thumbnail, running, adopting, runningProgress, cloudEntry, canStart, cloudLinked, syncEnabled, onOpen, onStart, onCancel, onRename, onDeleteSubtitles, onToggleSync, onRemove, onAdoptCloud, onRelink } = props;
  // Subtitles adopted from the cloud before the video reached this machine.
  // The entry is unavailable like a missing file, but nothing was lost, so it
  // reads as "no video yet" rather than "文件丢失".
  const subtitleOnly = !entry.available && !entry.sourcePath;
  const unprocessed = entry.available && !entry.documentAvailable && !running && !adopting;
  const [cancelling, setCancelling] = useState(false);
  const menu = useCardMenu();

  const cancel = async () => {
    setCancelling(true);
    try {
      await onCancel(entry);
    } finally {
      setCancelling(false);
    }
  };

  const handleCardClick = () => {
    if (menu.isOpen || running || adopting) return;
    if (!entry.available) {
      void onRelink(entry);
      return;
    }
    if (!entry.documentAvailable) {
      if (canStart) void onStart(entry);
      return;
    }
    onOpen(entry);
  };

  const handleKeyDown = (event: ReactKeyboardEvent<HTMLElement>) => {
    if (event.target !== event.currentTarget) return;
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      handleCardClick();
    }
  };

  const cornerBadge = running
    ? { tone: "running", label: "处理中", cloud: false }
    : adopting
      ? { tone: "running", label: "取回字幕中", cloud: false }
      : !entry.available
        ? { tone: subtitleOnly ? "cloud" : "missing", label: subtitleOnly ? "仅字幕" : "文件丢失", cloud: subtitleOnly }
        : null;

  const menuActions: MenuItem[] = [
    ...(running ? [{ label: cancelling ? "正在取消…" : "取消处理", danger: true, disabled: cancelling, onSelect: () => void cancel() }] : []),
    ...(!entry.available ? [{ label: entry.documentAvailable || subtitleOnly ? "关联本地视频" : "重新定位视频", onSelect: () => void onRelink(entry) }] : []),
    ...(!entry.available && entry.documentAvailable ? [{ label: "仅编辑字幕", onSelect: () => onOpen(entry) }] : []),
    ...(entry.available && !entry.documentAvailable && hasCloudSubtitles(cloudEntry) ? [{ label: adopting ? "取回云端字幕…" : "取回云端字幕", disabled: adopting, onSelect: () => void onAdoptCloud(entry, cloudEntry) }] : []),
    ...(entry.available && !entry.documentAvailable && !running ? [{ label: "开始任务", disabled: !canStart || adopting, onSelect: () => void onStart(entry) }] : []),
    "separator",
    { label: "重命名", onSelect: () => onRename(entry) },
    ...(entry.available ? [{ label: "在文件夹中显示", onSelect: () => void mediaLibrary.revealInFolder(entry.sourcePath) }] : []),
    ...(entry.documentAvailable && entry.available ? [{ label: "重跑任务", disabled: !canStart || adopting, onSelect: () => void onStart(entry) }] : []),
    "separator",
    ...(cloudLinked && entry.fingerprint ? [{ label: "同步到云端", checked: syncEnabled, onSelect: () => void onToggleSync(entry) }] : []),
    "separator",
    ...(entry.documentAvailable || cloudEntry ? [{ label: "删除字幕…", danger: true, onSelect: () => onDeleteSubtitles(entry, cloudEntry) }] : []),
    { label: "从媒体库移除…", danger: true, onSelect: () => onRemove(entry) },
  ];

  const cardHint = running
    ? "任务处理中，右键可取消"
    : !entry.available
      ? "点击关联本地视频"
      : unprocessed
        ? "点击开始字幕任务"
        : "点击编辑字幕";

  const listStateBadge = running
    ? { tone: "accent", label: `处理中 ${Math.round(runningProgress)}%` }
    : adopting
      ? { tone: "accent", label: "取回字幕中" }
      : subtitleOnly
        ? { tone: "accent", label: "仅字幕" }
        : !entry.available
          ? { tone: "danger", label: "文件丢失" }
          : unprocessed
            ? { tone: "neutral", label: "待处理" }
            : cloudEntry
              ? { tone: "ok", label: "已同步" }
              : null;

  const resolutionText = entry.width > 0
    ? `${entry.width}×${entry.height}`
    : entry.audioOnly
      ? "纯音频"
      : "—";

  if (view === "list") {
    return (
      <article
        className={`media-card media-row local-card${!entry.available ? " missing-media" : ""}${unprocessed ? " unprocessed-media" : ""}${menu.isOpen ? " menu-open" : ""}`}
        role="button"
        tabIndex={0}
        title={cardHint}
        onClick={handleCardClick}
        onKeyDown={handleKeyDown}
        onContextMenu={menu.openAtContext}
      >
        <div className="media-row-main">
          <div className="media-thumb">
            {thumbnail ? <img src={thumbnail} alt="" /> : <MediaPlaceholderIcon kind={entry.audioOnly ? "audio" : "video"} />}
            {unprocessed && <div className="media-unprocessed-mask" aria-hidden="true" />}
            {running && <span className="card-progress" style={{ width: `${runningProgress}%` }} />}
          </div>
          <div className="media-row-title">
            <strong title={entry.sourcePath || entry.title}>{entry.title}</strong>
            {listStateBadge && <span className={`badge tone-${listStateBadge.tone}`}>{listStateBadge.label}</span>}
          </div>
        </div>
        <span className="media-col media-col-res">{resolutionText}</span>
        <span className="media-col media-col-size">{entry.size > 0 ? formatSize(entry.size) : "—"}</span>
        <span className="media-col media-col-dur">{entry.duration > 0 ? formatDuration(entry.duration) : "—"}</span>
        <span className="media-col media-col-date">{formatDate(entry.lastAccess || entry.addedAt)}</span>
        <div className="media-col-actions">
          <button
            className="card-menu-trigger"
            type="button"
            aria-label="更多操作"
            aria-haspopup="menu"
            aria-expanded={menu.isOpen}
            onClick={menu.toggleFromButton}
          >
            <span /><span /><span />
          </button>
        </div>
        <CardContextMenu anchor={menu.anchor} actions={menuActions} onClose={menu.close} />
      </article>
    );
  }

  return (
    <article
      className={`media-card local-card${!entry.available ? " missing-media" : ""}${unprocessed ? " unprocessed-media" : ""}${menu.isOpen ? " menu-open" : ""}`}
      role="button"
      tabIndex={0}
      title={cardHint}
      onClick={handleCardClick}
      onKeyDown={handleKeyDown}
      onContextMenu={menu.openAtContext}
    >
      <div className="media-thumb">
        {thumbnail ? <img src={thumbnail} alt="" /> : <MediaPlaceholderIcon kind={entry.audioOnly ? "audio" : "video"} />}
        {unprocessed && (
          <div className="media-unprocessed-mask" aria-hidden="true">
            <span className="media-unprocessed-pill">开始任务</span>
          </div>
        )}
        {cornerBadge && (
          <strong className={`state-badge ${cornerBadge.tone}`}>
            {cornerBadge.cloud && <CloudBadgeIcon />}
            {cornerBadge.label}
          </strong>
        )}
        {entry.duration > 0 && <span className="media-duration">{formatDuration(entry.duration)}</span>}
        {running && <span className="card-progress" style={{ width: `${runningProgress}%` }} />}
        <button
          className="card-menu-trigger"
          type="button"
          aria-label="更多操作"
          aria-haspopup="menu"
          aria-expanded={menu.isOpen}
          onClick={menu.toggleFromButton}
        >
          <span /><span /><span />
        </button>
      </div>
      <div className="media-card-copy">
        <strong title={entry.sourcePath || entry.title}>{entry.title}</strong>
        <span className="media-meta">{localMetaLine(entry, subtitleOnly)}</span>
      </div>
      <CardContextMenu anchor={menu.anchor} actions={menuActions} onClose={menu.close} />
    </article>
  );
}

export function CloudMediaCard({ entry, view, thumbnail, adopting, onEdit, onAssociate, onDelete }: { entry: CloudEntry; view: ViewMode; thumbnail?: string; adopting: boolean; onEdit: (entry: CloudEntry) => Promise<void>; onAssociate: (entry: CloudEntry) => Promise<void>; onDelete: (entry: CloudEntry) => void }) {
  const menu = useCardMenu();
  const canOpen = !adopting && hasCloudSubtitles(entry);

  const handleCardClick = () => {
    if (menu.isOpen || !canOpen) return;
    void onAssociate(entry);
  };

  const handleKeyDown = (event: ReactKeyboardEvent<HTMLElement>) => {
    if (event.target !== event.currentTarget) return;
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      handleCardClick();
    }
  };

  const menuActions: MenuItem[] = [
    ...(hasCloudSubtitles(entry) ? [
      { label: "关联本地视频", disabled: adopting, onSelect: () => void onAssociate(entry) },
      { label: adopting ? "取回字幕…" : "仅编辑字幕", disabled: adopting, onSelect: () => void onEdit(entry) },
    ] : []),
    "separator",
    { label: "删除云端字幕…", danger: true, disabled: entry.status === "queued" || entry.status === "running", onSelect: () => onDelete(entry) },
  ];

  if (view === "list") {
    return (
      <article
        className={`media-card media-row cloud-card${menu.isOpen ? " menu-open" : ""}`}
        role="button"
        tabIndex={0}
        title={canOpen ? "点击关联本地视频" : undefined}
        onClick={handleCardClick}
        onKeyDown={handleKeyDown}
        onContextMenu={menu.openAtContext}
      >
        <div className="media-row-main">
          <div className="media-thumb cloud-thumb">
            {thumbnail ? <img src={thumbnail} alt="" /> : <MediaPlaceholderIcon kind="cloud" />}
          </div>
          <div className="media-row-title">
            <strong title={entry.title}>{entry.title}</strong>
            <span className="badge tone-accent">{adopting ? "取回字幕中" : "仅云端"}</span>
          </div>
        </div>
        <span className="media-col media-col-res">{entry.ownerName || "云端"}</span>
        <span className="media-col media-col-size">—</span>
        <span className="media-col media-col-dur">{entry.duration > 0 ? formatDuration(entry.duration) : "—"}</span>
        <span className="media-col media-col-date">{formatDate(entry.updatedAt)}</span>
        <div className="media-col-actions">
          <button
            className="card-menu-trigger"
            type="button"
            aria-label="更多操作"
            aria-haspopup="menu"
            aria-expanded={menu.isOpen}
            onClick={menu.toggleFromButton}
          >
            <span /><span /><span />
          </button>
        </div>
        <CardContextMenu anchor={menu.anchor} actions={menuActions} onClose={menu.close} />
      </article>
    );
  }

  return (
    <article
      className={`media-card cloud-card${menu.isOpen ? " menu-open" : ""}`}
      role="button"
      tabIndex={0}
      title={canOpen ? "点击关联本地视频" : undefined}
      onClick={handleCardClick}
      onKeyDown={handleKeyDown}
      onContextMenu={menu.openAtContext}
    >
      <div className="media-thumb cloud-thumb">
        {thumbnail ? <img src={thumbnail} alt="" /> : <MediaPlaceholderIcon kind="cloud" />}
        {adopting ? (
          <strong className="state-badge running">取回字幕中</strong>
        ) : (
          <span className="state-badge cloud">
            <CloudBadgeIcon />
            仅云端
          </span>
        )}
        {entry.duration > 0 && <span className="media-duration">{formatDuration(entry.duration)}</span>}
        <button
          className="card-menu-trigger"
          type="button"
          aria-label="更多操作"
          aria-haspopup="menu"
          aria-expanded={menu.isOpen}
          onClick={menu.toggleFromButton}
        >
          <span /><span /><span />
        </button>
      </div>
      <div className="media-card-copy">
        <strong title={entry.title}>{entry.title}</strong>
        <span className="media-meta">{cloudMetaLine(entry)}</span>
      </div>
      <CardContextMenu anchor={menu.anchor} actions={menuActions} onClose={menu.close} />
    </article>
  );
}
