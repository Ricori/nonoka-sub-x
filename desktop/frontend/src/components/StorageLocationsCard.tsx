import { useEffect, useState } from "react";
import type { CacheStatus } from "../bridge/library.ts";
import type { RelocationProgress, StorageDestination, StorageLocation, StorageStatus, StorageTarget } from "../bridge/storage.ts";
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
  onSaveCacheLimit?: (limit: number) => Promise<void>;
  onClearCache?: () => Promise<void>;
  onDismissMessage: () => void;
  onDismissCacheMessage?: () => void;
}

const targetDetails: Record<StorageTarget, string> = {
  runtime: "Python 运行时、转写模型与下载缓存，占用最大。",
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
    onSaveCacheLimit,
    onClearCache,
    onDismissMessage,
    onDismissCacheMessage,
  } = props;
  const { pending, choosing, choose, clear } = useDestinationChoice(onChoose);
  const active = progress?.active === true;

  const [limit, setLimit] = useState("20");

  useEffect(() => {
    if (cache) setLimit(String(cache.limitGB));
  }, [cache]);

  const limitValue = Number(limit);
  const limitValid = Number.isFinite(limitValue) && limitValue >= 1 && limitValue <= 1024;
  const usage = cache?.limitBytes ? Math.min(100, (cache.bytes / cache.limitBytes) * 100) : 0;

  const confirm = () => {
    if (!pending) return;
    const { target, directory } = pending;
    clear();
    void onRelocate(target as StorageTarget, directory);
  };

  const pendingEmpty = (status?.locations ?? []).some((location) => location.target === pending?.target && location.empty);

  return (
    <article className="panel storage-locations-card">
      <div className="storage-heading">
        <span className="eyebrow">Storage locations</span>
        <h2>磁盘占用</h2>
        <p>默认在系统盘，可整体迁移到其他磁盘。</p>
      </div>

      <div className="storage-rows">
        {(status?.locations ?? []).map((location: StorageLocation) => {
          const target = location.target as StorageTarget;
          const title = storageTargetTitles[target];
          if (!title) return null;
          const movingThis = active && progress?.target === location.target;
          const isVideo = target === "video";
          const fileCount = isVideo && cache ? cache.files : location.files;
          const bytes = isVideo && cache ? cache.bytes : location.bytes;

          const hasLimit = isVideo && cache != null && cache.limitBytes > 0;

          return (
            <section className="storage-row" key={location.target}>
              <div className="storage-row-heading">
                <div>
                  <strong>
                    {title}
                    {location.custom && <span className="storage-badge">已迁移</span>}
                  </strong>
                  <p>{targetDetails[target]}</p>
                </div>
                <b>
                  {location.missing ? "—" : formatStorageBytes(bytes)}
                  {hasLimit && <small> / {formatStorageBytes(cache.limitBytes)}</small>}
                </b>
              </div>

              {isVideo && (
                <div className="storage-cache-limit">
                  {hasLimit && (
                    <div className="storage-meter" aria-label={`缓存使用率 ${usage.toFixed(0)}%`}>
                      <span style={{ width: `${usage}%` }} />
                    </div>
                  )}
                  <label>
                    上限
                    <input
                      type="number"
                      min="1"
                      max="1024"
                      step="1"
                      value={limit}
                      onChange={(event) => setLimit(event.target.value)}
                    />
                    GB
                  </label>
                  {onSaveCacheLimit && (
                    <button
                      disabled={cacheBusy || busy || !limitValid || limitValue === cache?.limitGB}
                      onClick={() => void onSaveCacheLimit(limitValue)}
                      type="button"
                    >
                      保存
                    </button>
                  )}
                </div>
              )}

              <code className="storage-path" title={location.directory}>{location.directory}</code>

              <div className="storage-row-footer">
                <div className="storage-row-stats">
                  <span>
                    {location.volume} 剩余 <b>{formatStorageBytes(location.freeBytes)}</b>
                  </span>
                  {!location.missing && fileCount > 0 && (
                    <span>
                      <b>{fileCount}</b> {isVideo ? "个视频副本" : "个文件"}
                    </span>
                  )}
                  {location.empty && !location.missing && <span>尚未安装</span>}
                  {location.missing && <span className="storage-warn">目录当前不可访问，请重新连接该磁盘或改回默认位置</span>}
                </div>
                <div className="storage-row-actions">
                  {movingThis && <span className="storage-inline-progress">正在迁移…</span>}
                  {isVideo && onClearCache && (
                    <button
                      className="danger-link"
                      disabled={cacheBusy || busy || !fileCount}
                      onClick={() => void onClearCache()}
                      type="button"
                    >
                      清理缓存
                    </button>
                  )}
                  {location.custom && (
                    <button className="ghost" disabled={busy || active} onClick={() => void onReset(target)} type="button">
                      移回默认
                    </button>
                  )}
                  <button disabled={busy || active || choosing !== ""} onClick={() => void choose(target)} type="button">
                    {choosing === target ? "正在选择…" : "更改位置…"}
                  </button>
                </div>
              </div>

              {isVideo && cacheMessage && onDismissCacheMessage && (
                <Notice className="storage-cache-message" message={cacheMessage} onDismiss={onDismissCacheMessage} />
              )}
            </section>
          );
        })}
      </div>

      {pending && <RelocationConfirm pending={pending} busy={busy} empty={pendingEmpty} onConfirm={confirm} onDismiss={clear} />}
      <RelocationProgressView progress={progress} onCancel={() => void onCancel()} />

      <Notice className="storage-message" message={message} onDismiss={onDismissMessage} />
    </article>
  );
}
