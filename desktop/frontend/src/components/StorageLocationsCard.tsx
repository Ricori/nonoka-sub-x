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
  onOpen?: (target: StorageTarget) => Promise<void>;
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
    onOpen,
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

  // 上限在失焦或回车时直接保存，不再单独放一个平时总是灰着的「保存」按钮。
  // 填了无效值就退回已保存的上限，而不是悄悄保留一个不会生效的数字。
  const commitLimit = () => {
    const value = Number(limit);
    const valid = Number.isInteger(value) && value >= 1 && value <= 1024;
    if (!valid || !cache || !onSaveCacheLimit) {
      if (cache) setLimit(String(cache.limitGB));
      return;
    }
    if (value !== cache.limitGB) void onSaveCacheLimit(value);
  };
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
      {/* 卡片自己不再带标题，没有数据时至少说一句，免得只剩一个空框。 */}
      {!status && <p className="storage-empty">正在读取磁盘占用…</p>}
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
          // 目录还没建出来（或磁盘拔掉了）时没有东西可打开。
          const nothingToOpen = location.missing || (location.empty && location.bytes === 0 && location.files === 0);

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

              {hasLimit && (
                <div className="storage-meter" role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(usage)} aria-label={`缓存使用率 ${usage.toFixed(0)}%`}>
                  <span style={{ width: `${usage}%` }} />
                </div>
              )}

              <div className="storage-path">
                <code title={location.directory}>{location.directory}</code>
                {onOpen && (
                  <button
                    type="button"
                    disabled={nothingToOpen}
                    title={nothingToOpen ? "目录还不存在" : "在文件管理器中打开"}
                    aria-label={`在文件管理器中打开${title}`}
                    onClick={() => void onOpen(target)}
                  >
                    <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2 4.5A1.5 1.5 0 0 1 3.5 3h2.8l1.4 1.5h4.8A1.5 1.5 0 0 1 14 6v5.5a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 2 11.5z" /></svg>
                    打开
                  </button>
                )}
              </div>

              <div className="storage-row-footer">
                <div className="storage-row-stats">
                  <span>
                    {location.volume} 剩余 <b>{formatStorageBytes(location.freeBytes)}</b>
                  </span>
                  {isVideo && !location.missing && fileCount > 0 && (
                    <span>
                      <b>{fileCount}</b> 个视频副本
                    </span>
                  )}
                  {isVideo && cache && onSaveCacheLimit && (
                    <label className="storage-cache-limit">
                      上限
                      <input
                        type="number"
                        min="1"
                        max="1024"
                        step="1"
                        value={limit}
                        disabled={cacheBusy || busy}
                        title="失焦或回车后保存"
                        onChange={(event) => setLimit(event.target.value)}
                        onBlur={commitLimit}
                        onKeyDown={(event) => {
                          if (event.key === "Enter") event.currentTarget.blur();
                          if (event.key === "Escape") setLimit(String(cache.limitGB));
                        }}
                      />
                      GB
                    </label>
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
