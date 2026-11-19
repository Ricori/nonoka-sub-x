import { useEffect, useState } from "react";
import type { CacheStatus } from "../bridge/library.ts";
import type { RelocationProgress, StorageDestination, StorageLocation, StorageStatus, StorageTarget } from "../bridge/storage.ts";
import type { RuntimeCacheState } from "./runtimeCache.ts";
import "./StorageLocationsCard.css";
import { Notice } from "./Notice.tsx";
import {
  RelocationConfirm,
  RelocationProgressView,
  formatStorageBytes,
  storageTargetTitles,
  useDestinationChoice,
} from "./StorageRelocation.tsx";

interface StorageLocationsCardProps {
  status: StorageStatus | null;
  progress: RelocationProgress | null;
  message: string;
  busy: boolean;
  cache?: CacheStatus | null;
  cacheBusy?: boolean;
  cacheMessage?: string;
  onChoose: (target: StorageTarget) => Promise<StorageDestination | null>;
  onRelocate: (target: StorageTarget, destination: string) => Promise<void>;
  onReset: (target: StorageTarget) => Promise<void>;
  onCancel: () => Promise<void>;
  onOpen?: (target: StorageTarget) => Promise<void>;
  onSaveCacheLimit?: (limit: number) => Promise<void>;
  onClearCache?: () => Promise<void>;
  /** 只展示指定目标；不传则展示全部。 */
  targets?: StorageTarget[];
  /** 嵌入合并卡片时去掉外层 panel 容器。 */
  embedded?: boolean;
  runtimeCache?: RuntimeCacheState;
  onDismissMessage: () => void;
  onDismissCacheMessage?: () => void;
}

const targetDetails: Record<StorageTarget, string> = {
  runtime: "运行环境层（Python、模型、工具）与缓存所在位置。",
  video: "处理过的视频副本，超出上限按最近使用自动回收。",
};

export function StorageLocationsCard(props: StorageLocationsCardProps) {
  const {
    status,
    progress,
    message,
    busy,
    cache,
    cacheBusy = false,
    cacheMessage = "",
    onChoose,
    onRelocate,
    onReset,
    onCancel,
    onOpen,
    onSaveCacheLimit,
    onClearCache,
    targets,
    embedded = false,
    runtimeCache,
    onDismissMessage,
    onDismissCacheMessage,
  } = props;
  const { pending, choosing, choose, clear } = useDestinationChoice(onChoose);
  const active = progress?.active === true;
  const matchingProgress = progress && (!targets || targets.includes(progress.target as StorageTarget)) ? progress : null;

  const [limit, setLimit] = useState("20");

  useEffect(() => {
    if (cache) setLimit(String(cache.limitGB));
  }, [cache]);

  const commitLimit = () => {
    const value = Number(limit);
    const valid = Number.isInteger(value) && value >= 1 && value <= 1024;
    if (!valid || !cache || !onSaveCacheLimit) {
      if (cache) setLimit(String(cache.limitGB));
      return;
    }
    if (value !== cache.limitGB) void onSaveCacheLimit(value);
  };

  const confirm = () => {
    if (!pending) return;
    const { target, directory } = pending;
    clear();
    void onRelocate(target as StorageTarget, directory);
  };

  const pendingEmpty = (status?.locations ?? []).some((location) => location.target === pending?.target && location.empty);
  const visibleLocations = (status?.locations ?? []).filter((location) => !targets || targets.includes(location.target as StorageTarget));

  const body = (
    <>
      {!status && <p className="storage-empty">正在读取磁盘占用…</p>}
      <div className="storage-rows">
        {visibleLocations.map((location: StorageLocation) => {
          const target = location.target as StorageTarget;
          const title = storageTargetTitles[target];
          if (!title) return null;
          const movingThis = active && progress?.target === location.target;
          const isVideo = target === "video";
          const runtimeCacheStatus = target === "runtime" && !location.missing ? runtimeCache?.status : null;
          const fileCount = isVideo && cache ? cache.files : location.files;
          const bytes = isVideo && cache ? cache.bytes : location.bytes;
          const hasLimit = isVideo && cache != null && cache.limitBytes > 0;
          const nothingToOpen = location.missing || (location.empty && location.bytes === 0 && location.files === 0);

          const tip = [
            targetDetails[target],
            `${location.volume} 剩余 ${formatStorageBytes(location.freeBytes)}`,
            isVideo && !location.missing ? `当前 ${fileCount} 个视频副本` : "",
            location.missing ? "目录当前不可访问，请重新连接磁盘或移回默认位置" : "",
          ].filter(Boolean).join("\n");

          return (
            <section className="storage-compact-row" key={location.target}>
              <div className="dir-header-row">
                <div className="dir-header-left" title={tip}>
                  <strong>{title}</strong>
                  {location.custom && <span className="storage-badge">已迁移</span>}
                  <span className="dir-meta">
                    {location.missing
                      ? "目录不可访问"
                      : `${formatStorageBytes(bytes)}${hasLimit ? ` / ${formatStorageBytes(cache.limitBytes)}` : ""} · ${location.volume} 剩余 ${formatStorageBytes(location.freeBytes)}`}
                  </span>
                </div>
                <div className="dir-header-actions">
                  {isVideo && cache && onSaveCacheLimit && (
                    <label className="dir-limit-control" title="视频缓存容量上限（失焦或回车保存）">
                      <span>上限</span>
                      <input
                        type="number"
                        min="1"
                        max="1024"
                        step="1"
                        value={limit}
                        disabled={cacheBusy || busy}
                        onChange={(event) => setLimit(event.target.value)}
                        onBlur={commitLimit}
                        onKeyDown={(event) => {
                          if (event.key === "Enter") event.currentTarget.blur();
                          if (event.key === "Escape") setLimit(String(cache.limitGB));
                        }}
                      />
                      <span>GB</span>
                    </label>
                  )}
                  {movingThis && <span className="storage-inline-progress">迁移中…</span>}
                  {isVideo && onClearCache && (
                    <button
                      className="runtime-remove-button runtime-compact-btn"
                      disabled={cacheBusy || busy || !fileCount}
                      onClick={() => void onClearCache()}
                      type="button"
                    >
                      清理缓存
                    </button>
                  )}
                  {runtimeCache && target === "runtime" && runtimeCacheStatus != null && runtimeCacheStatus.bytes > 0 && (
                    <button
                      className="runtime-remove-button runtime-compact-btn"
                      disabled={runtimeCache.busy || busy || active || runtimeCacheStatus.reclaimable === 0}
                      onClick={() => void runtimeCache.clear()}
                      type="button"
                    >
                      {runtimeCache.busy ? "清理中…" : "清理缓存"}
                    </button>
                  )}
                  {location.custom && (
                    <button className="quiet-button runtime-compact-btn" disabled={busy || active} onClick={() => void onReset(target)} type="button">
                      移回默认
                    </button>
                  )}
                  <button className="quiet-button runtime-compact-btn" disabled={busy || active || choosing !== ""} onClick={() => void choose(target)} type="button">
                    {choosing === target ? "选择中…" : "更改位置…"}
                  </button>
                </div>
              </div>

              <div className="dir-path-bar">
                <code title={location.directory}>{location.directory}</code>
                {onOpen && (
                  <button
                    type="button"
                    className="dir-open-btn"
                    disabled={nothingToOpen}
                    title={nothingToOpen ? "目录还不存在" : "在文件管理器中打开"}
                    onClick={() => void onOpen(target)}
                  >
                    <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2 4.5A1.5 1.5 0 0 1 3.5 3h2.8l1.4 1.5h4.8A1.5 1.5 0 0 1 14 6v5.5a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 2 11.5z" /></svg>
                    打开
                  </button>
                )}
              </div>

              {target === "runtime" && runtimeCache?.message && (
                <Notice className="storage-cache-message" message={runtimeCache.message} tone={runtimeCache.tone} onDismiss={runtimeCache.dismiss} />
              )}
              {isVideo && cacheMessage && onDismissCacheMessage && (
                <Notice className="storage-cache-message" message={cacheMessage} onDismiss={onDismissCacheMessage} />
              )}
            </section>
          );
        })}
      </div>

      {pending && <RelocationConfirm pending={pending} busy={busy} empty={pendingEmpty} onConfirm={confirm} onDismiss={clear} />}
      <RelocationProgressView progress={matchingProgress} onCancel={() => void onCancel()} />
      {(!targets || !progress || targets.includes(progress.target as StorageTarget)) && (
        <Notice className="storage-message" message={message} onDismiss={onDismissMessage} />
      )}
    </>
  );

  if (embedded) return body;
  return <article className="panel storage-locations-card">{body}</article>;
}
