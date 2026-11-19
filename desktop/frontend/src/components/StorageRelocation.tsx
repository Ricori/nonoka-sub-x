import { useState } from "react";
import type { RelocationProgress, StorageDestination, StorageTarget } from "../bridge/storage.ts";
import "./StorageRelocation.css";

/** 两个入口共用的一套迁移交互：设置页的「磁盘占用与位置」，和运行环境页安装前的位置提示。 */

export const storageTargetTitles: Record<StorageTarget, string> = {
  runtime: "运行环境安装目录",
  video: "视频缓存目录",
};

export function formatStorageBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(0)} MB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

/** 迁移是否已经结束到「不该再显示进度条」的状态。 */
export function relocationSettled(progress: RelocationProgress | null): boolean {
  if (!progress) return true;
  return !progress.active;
}

/** 记住「已经选好、等用户确认」的目标目录。两个入口的选择流程完全一样。 */
export function useDestinationChoice(onChoose: (target: StorageTarget) => Promise<StorageDestination | null>) {
  const [pending, setPending] = useState<StorageDestination | null>(null);
  const [choosing, setChoosing] = useState<StorageTarget | "">("");

  const choose = async (target: StorageTarget) => {
    setChoosing(target);
    try {
      const choice = await onChoose(target);
      setPending(choice && !choice.cancelled ? choice : null);
    } finally {
      setChoosing("");
    }
  };

  return { pending, choosing, choose, clear: () => setPending(null) };
}

interface RelocationConfirmProps {
  pending: StorageDestination;
  busy: boolean;
  /** 目录里还没有东西时，这不是「迁移」而是「决定装到哪」，话术要跟着换。 */
  empty?: boolean;
  onConfirm: () => void;
  onDismiss: () => void;
}

export function RelocationConfirm({ pending, busy, empty = false, onConfirm, onDismiss }: RelocationConfirmProps) {
  const title = storageTargetTitles[pending.target as StorageTarget] ?? pending.target;
  const remaining = Math.max(0, pending.bytes - pending.resumedBytes);
  return (
    <div className="storage-confirm" role="dialog" aria-label={empty ? "确认安装位置" : "确认迁移"}>
      <div>
        <strong>{empty ? `将${title}设为` : `将${title}迁移到`}</strong>
        <code>{pending.directory}</code>
        {pending.unavailable ? (
          <p className="storage-warn">{pending.unavailable}</p>
        ) : empty ? (
          <p>目标磁盘剩余 {formatStorageBytes(pending.freeBytes)}</p>
        ) : (
          <p>
            {pending.resumable
              ? `上次未完成的迁移已复制 ${formatStorageBytes(pending.resumedBytes)}，这次从中断处继续，还需 ${formatStorageBytes(remaining)}，`
              : `需要移动 ${formatStorageBytes(pending.bytes)}，`}
            目标磁盘剩余 {formatStorageBytes(pending.freeBytes)}。
            {pending.sameVolume
              ? "同一磁盘内移动，瞬间完成。"
              : "跨磁盘复制期间请不要关闭 Nonoka Sub X；本地服务会自动停止并在完成后重启。中途取消或失败都不会删掉已复制的部分，也不会动原目录。"}
          </p>
        )}
      </div>
      <div className="storage-confirm-actions">
        <button className="primary" disabled={!!pending.unavailable || busy} onClick={onConfirm} type="button">
          {empty ? "用这个位置" : "开始迁移"}
        </button>
        <button className="ghost" onClick={onDismiss} type="button">
          取消
        </button>
      </div>
    </div>
  );
}

export function RelocationProgressView({ progress, onCancel }: { progress: RelocationProgress | null; onCancel: () => void }) {
  if (!progress || progress.stage === "idle") return null;
  const active = progress.active;
  const finished = progress.stage === "failed" || progress.stage === "completed" || progress.stage === "cancelled";
  if (!active && !finished) return null;
  const percentage = progress.total ? Math.min(100, Math.max(0, (progress.completed / progress.total) * 100)) : 0;
  return (
    <div className="storage-progress" role="status" aria-live="polite">
      <div className="storage-progress-heading">
        <strong>{progress.message}</strong>
        {progress.total > 0 && (
          <small>
            {formatStorageBytes(progress.completed)} / {formatStorageBytes(progress.total)}
          </small>
        )}
      </div>
      {active && (
        <div className="storage-meter" aria-label={`迁移进度 ${percentage.toFixed(0)}%`}>
          <span style={{ width: `${percentage}%` }} />
        </div>
      )}
      {progress.destination && <code className="storage-progress-path">{progress.destination}</code>}
      {(progress.stage === "failed" || progress.stage === "cancelled") && (
        <p className="storage-resume-hint">再次选择同一位置即可从中断处继续，不会重新复制已完成的部分。</p>
      )}
      {progress.error && <p className="storage-warn">{progress.error}</p>}
      {active && progress.stage === "moving" && (
        <button className="ghost" onClick={onCancel} type="button">
          取消迁移
        </button>
      )}
    </div>
  );
}
