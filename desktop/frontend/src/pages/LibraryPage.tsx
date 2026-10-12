import type { Dispatch, SetStateAction } from "react";
import type { CloudEntry } from "../bridge/cloud.ts";
import type { MediaEntry } from "../bridge/library.ts";
import { CloudMediaCard, LocalMediaCard } from "../components/MediaCard.tsx";
import { CustomSelect } from "../components/CustomSelect.tsx";
import { Mascot } from "../components/Mascot.tsx";
import type { LibraryFilter, LibraryItem, SortMode, ViewMode } from "../app/types.ts";
import "./LibraryPage.css";
import { Notice } from "../components/Notice.tsx";
import type { NoticeTone } from "../components/Notice.tsx";

interface LibraryPageProps {
  items: LibraryItem[];
  visibleItems: LibraryItem[];
  thumbnails: Record<string, string>;
  cloudThumbnails: Record<string, string>;
  remoteByFingerprint: Map<string, CloudEntry>;
  filter: LibraryFilter;
  filterCounts: Record<LibraryFilter, number>;
  sort: SortMode;
  view: ViewMode;
  busy: boolean;
  message: string;
  messageTone: NoticeTone;
  localRunningID?: string;
  adoptingMedia: ReadonlySet<string>;
  adoptingCloud: ReadonlySet<string>;
  runningProgress: number;
  taskActive: boolean;
  syncing: boolean;
  setFilter: Dispatch<SetStateAction<LibraryFilter>>;
  setSort: Dispatch<SetStateAction<SortMode>>;
  setView: Dispatch<SetStateAction<ViewMode>>;
  onClearQuery: () => void;
  onImport: () => Promise<void>;
  onOpen: (entry: MediaEntry) => void;
  onStart: (entry: MediaEntry) => Promise<void>;
  onCancel: () => Promise<void>;
  onRename: (entry: MediaEntry) => void;
  onDeleteSubtitles: (entry: MediaEntry) => void;
  onRemove: (entry: MediaEntry) => void;
  onAdoptCloud: (entry: MediaEntry, cloudEntry: CloudEntry) => Promise<void>;
  onEditCloud: (entry: CloudEntry) => Promise<void>;
  onAssociateCloud: (entry: CloudEntry) => Promise<void>;
  onDeleteCloud: (entry: CloudEntry) => void;
  onRelink: (entry: MediaEntry) => Promise<void>;
  onDismissMessage: () => void;
}

export function LibraryPage(props: LibraryPageProps) {
  const { items, visibleItems, thumbnails, cloudThumbnails, remoteByFingerprint, filter, filterCounts, sort, view, busy, message, messageTone, localRunningID, adoptingMedia, adoptingCloud, runningProgress, taskActive, syncing, setFilter, setSort, setView, onClearQuery, onImport, onOpen, onStart, onCancel, onRename, onDeleteSubtitles, onRemove, onAdoptCloud, onEditCloud, onAssociateCloud, onDeleteCloud, onRelink, onDismissMessage } = props;
  // A local card's cover comes from the library's own thumbnail directory, which
  // adoption now fills for media this machine has no file of. Entries adopted
  // before it did -- and media whose frame ffmpeg never produced -- fall back to
  // the cloud entry's cover, which is already in hand: it is fetched for every
  // cloud entry, merged into a local card or not.
  const coverOf = (entry: MediaEntry) =>
    thumbnails[entry.id] ?? cloudThumbnails[remoteByFingerprint.get(entry.fingerprint)?.id ?? ""];
  return (
    <section className="library-view">
      <div className="library-toolbar">
        {([["all", "全部"], ["ready", "可编辑"], ["running", "处理中"], ["cloud", "仅云端"], ["missing", "文件丢失"]] as const).map(([value, label]) => (
          <button className={filter === value ? "active" : ""} key={value} onClick={() => setFilter(value)}>{label} <span>{filterCounts[value]}</span></button>
        ))}
        <span className="toolbar-spacer" />
        <CustomSelect<SortMode>
          ariaLabel="媒体排序"
          compact
          value={sort}
          options={[{ value: "recent", label: "最近使用" }, { value: "name", label: "按标题" }, { value: "duration", label: "按时长" }]}
          onChange={setSort}
        />
        <div className="view-switch" aria-label="视图切换"><button className={view === "grid" ? "active" : ""} aria-label="网格视图" onClick={() => setView("grid")}>▦</button><button className={view === "list" ? "active" : ""} aria-label="列表视图" onClick={() => setView("list")}>☷</button></div>
      </div>
      <Notice className="library-message" message={message} tone={messageTone} autoDismissMs={messageTone === "warn" ? 0 : undefined} onDismiss={onDismissMessage} />
      {syncing && items.length === 0 ? (
        <div className="library-sync-state" role="status" aria-live="polite" aria-busy="true">
          <span className="library-sync-spinner" aria-hidden="true" />
          <strong>正在同步媒体库…</strong>
        </div>
      ) : items.length === 0 ? (
        <button className="empty-state library-empty" onClick={() => void onImport()} disabled={busy}><Mascot pose="welcome" /><h3>把本地视频或音频拖进来</h3><p>支持 MP4 / MOV / MKV / WebM 视频，以及 MP3 / M4A / FLAC / WAV / Opus 等纯音频。登录云端后，字幕记录会按媒体指纹自动合并到同一个媒体库。</p></button>
      ) : visibleItems.length === 0 ? (
        <div className="filtered-empty"><Mascot pose="idle" /><strong>没有匹配的媒体</strong><span>尝试清除搜索词或切换筛选条件。</span><button onClick={() => { onClearQuery(); setFilter("all"); }}>查看全部媒体</button></div>
      ) : (
        <div className={`media-grid ${view === "list" ? "list-view" : ""}`}>
          {visibleItems.map((item) => item.kind === "local" ? (
            <LocalMediaCard key={`local:${item.entry.id}`} entry={item.entry} thumbnail={coverOf(item.entry)} running={item.entry.id === localRunningID} adopting={adoptingMedia.has(item.entry.id)} runningProgress={runningProgress} cloudEntry={remoteByFingerprint.get(item.entry.fingerprint)} canStart={item.entry.available && !taskActive} onOpen={onOpen} onStart={onStart} onCancel={onCancel} onRename={onRename} onDeleteSubtitles={onDeleteSubtitles} onRemove={onRemove} onAdoptCloud={onAdoptCloud} onDeleteCloud={onDeleteCloud} onRelink={onRelink} />
          ) : <CloudMediaCard key={`cloud:${item.entry.id}`} entry={item.entry} thumbnail={cloudThumbnails[item.entry.id]} adopting={adoptingCloud.has(item.entry.id)} onEdit={onEditCloud} onAssociate={onAssociateCloud} onDelete={onDeleteCloud} />)}
        </div>
      )}
    </section>
  );
}
