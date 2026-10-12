import type { RelocationProgress, StorageDestination, StorageLocation, StorageTarget } from "../bridge/storage.ts";
import type { InstallLocationView } from "./installLocation.ts";
import "./InstallLocationCard.css";
import {
  RelocationConfirm,
  RelocationProgressView,
  formatStorageBytes,
  useDestinationChoice,
} from "./StorageRelocation.tsx";

interface InstallLocationCardProps {
  location: StorageLocation;
  /** 显隐与语气的唯一判定来源，见 installLocation.ts。 */
  view: InstallLocationView;
  progress: RelocationProgress | null;
  busy: boolean;
  /** 非 null 表示用户已经点了安装、正等着确认位置。 */
  awaitingInstall: boolean;
  /** 与页面上其他安装按钮同一套门槛；改完位置后 sidecar 重启的那几秒也算在内。 */
  installBlocked: string;
  onChoose: (target: StorageTarget) => Promise<StorageDestination | null>;
  onRelocate: (target: StorageTarget, destination: string) => Promise<void>;
  onCancelRelocation: () => Promise<void>;
  onProceed: () => void;
  onDismiss: () => void;
}

/**
 * 下载开始前的位置提示。
 *
 * 这张卡只在运行环境还没装的时候出现，因为那是改位置唯一免费的时刻：目录里没有
 * 东西，改位置只是改一条记录。装完再想搬就得跨盘复制十几 GB —— 设置页里的存储
 * 卡片管那种情况。
 */
export function InstallLocationCard(props: InstallLocationCardProps) {
  const { location, view, progress, busy, awaitingInstall, installBlocked, onChoose, onRelocate, onCancelRelocation, onProceed, onDismiss } = props;
  const { pending, choosing, choose, clear } = useDestinationChoice(onChoose);
  const relocating = progress?.active === true && progress.target === "runtime";
  const { tight, settled } = view;

  const confirm = () => {
    if (!pending) return;
    const { directory } = pending;
    clear();
    void onRelocate("runtime", directory);
  };

  return (
    <section className={`install-location-card ${tight ? "tight" : ""} ${awaitingInstall ? "awaiting" : ""}`}>
      <div className="install-location-main">
        <div className="install-location-icon" aria-hidden="true">{tight ? "!" : settled ? "✓" : "HD"}</div>
        <div className="install-location-copy">
          <span>安装位置 · {formatStorageBytes(location.estimated)}</span>
          <h3>
            {tight
              ? "所在磁盘空间可能不够，建议换一个盘"
              : settled
                ? "安装位置已就绪"
                : "运行环境文件较大，建议先选好安装位置"}
          </h3>
          <p>
            完整的本地 AI 运行环境约需 {formatStorageBytes(location.estimated)}。
            {settled ? "之后仍可在「设置 → 磁盘占用与位置」里调整。" : "现在改位置不会复制任何文件；装完之后再搬，就得跨盘挪几 GB。"}
          </p>
        </div>
      </div>

      <div className="install-location-path" title={location.directory}>
        <span>将安装到</span>
        <code>{location.directory}</code>
      </div>
      <p className={tight ? "install-location-free tight" : "install-location-free"}>
        {location.volume} 剩余 <b>{formatStorageBytes(location.freeBytes)}</b>
        {tight && `，不足建议的 ${formatStorageBytes(location.estimated)}`}
        {location.custom && "（已自定义）"}
      </p>

      {pending && <RelocationConfirm pending={pending} busy={busy} empty onConfirm={confirm} onDismiss={clear} />}
      <RelocationProgressView progress={relocating ? progress : null} onCancel={() => void onCancelRelocation()} />

      <div className="install-location-actions">
        <button
          className="quiet-button"
          disabled={busy || relocating || choosing !== ""}
          onClick={() => void choose("runtime")}
          type="button"
        >
          {choosing === "runtime" ? "正在选择…" : "选择其他位置…"}
        </button>
        {awaitingInstall && (
          <>
            <button
              className="primary-button"
              disabled={busy || relocating || installBlocked !== ""}
              onClick={onProceed}
              title={installBlocked || "从这个位置开始下载并安装"}
              type="button"
            >
              {relocating ? "正在设置位置…" : "开始安装"}
            </button>
            <button className="quiet-button" onClick={onDismiss} type="button">
              取消
            </button>
          </>
        )}
      </div>
    </section>
  );
}
