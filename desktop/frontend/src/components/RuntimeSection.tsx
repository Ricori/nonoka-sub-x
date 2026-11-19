import type { Snapshot as SidecarSnapshot } from "../../bindings/github.com/Ricori/nonoka-x/desktop/internal/sidecar/models.js";
import { useEffect, useState } from "react";
import type { PythonBootstrapState, RuntimeCleanupItem, RuntimeCleanupPlan, RuntimeInstallTarget, RuntimeItem, RuntimeLayerOverview, RuntimeProvisionState } from "../bridge/runtime.ts";
import type { RelocationProgress, StorageDestination, StorageStatus, StorageTarget } from "../bridge/storage.ts";
import type { RuntimeCacheState } from "./runtimeCache.ts";
import { cleanupView, layerLabel } from "./engineLayers.ts";
import { InfoTip } from "./InfoTip.tsx";
import { Mark } from "./Mark.tsx";
import "./RuntimeSection.css";
import "./InstallLocationCard.css";
import "./StorageLocationsCard.css";
import { Notice } from "./Notice.tsx";
import {
  RelocationConfirm,
  RelocationProgressView,
  formatStorageBytes,
  useDestinationChoice,
} from "./StorageRelocation.tsx";

/** The settings page's 「运行环境」 section. */
interface RuntimeSectionProps {
  provisionMessage: string;
  onDismissProvisionMessage: () => void;
  provision: RuntimeProvisionState | null;
  pythonBootstrap: PythonBootstrapState | null;
  sidecar: SidecarSnapshot | null;
  migrationActive?: boolean;
  onInstall: (target: RuntimeInstallTarget) => Promise<void>;
  onInstallPython: () => Promise<void>;
  onCancelInstall: () => Promise<void>;
  onRemoveModels: () => Promise<void>;
  onRemoveUnused: (layers: string[]) => Promise<void>;
  cleanup: RuntimeCleanupPlan | null;
  requestedInstall: { target: RuntimeInstallTarget; seq: number } | null;
  storage: StorageStatus | null;
  storageProgress: RelocationProgress | null;
  storageBusy: boolean;
  storageMessage: string;
  onDismissStorageMessage: () => void;
  onChooseStorage: (target: StorageTarget) => Promise<StorageDestination | null>;
  onRelocateStorage: (target: StorageTarget, destination: string) => Promise<void>;
  onResetStorage: (target: StorageTarget) => Promise<void>;
  onCancelStorage: () => Promise<void>;
  onOpenStorage?: (target: StorageTarget) => Promise<void>;
  runtimeCache?: RuntimeCacheState;
}

const installPhases = [
  { id: "downloading", label: "下载" },
  { id: "verifying", label: "校验" },
  { id: "extracting", label: "解压" },
  { id: "activating", label: "安装" },
] as const;

const layerStateLabels: Record<RuntimeLayerOverview["state"], string> = {
  ready: "已就绪",
  missing: "未安装",
  outdated: "需更新",
  damaged: "需修复",
};

const stageOrder: Record<string, number> = {
  preparing: 0,
  resource: 0,
  downloading: 0,
  verifying: 1,
  checking: 1,
  extracting: 2,
  unpacking: 2,
  activating: 3,
  runtime: 3,
  model: 3,
  installing_python: 3,
  installing_dependencies: 3,
  completed: 4,
};

function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
  const amount = value / 1024 ** index;
  return `${amount >= 100 || index === 0 ? amount.toFixed(0) : amount.toFixed(1)} ${units[index]}`;
}

interface ProgressJob {
  resource: string;
  stage: string;
  message: string;
  progress: { completed: number; total: number; unit: string; bytes_per_second?: number } | null;
  proxy?: string;
}

function ProvisionProgress({ job, onCancel, cancellable = true }: { job: ProgressJob; onCancel: () => Promise<void>; cancellable?: boolean }) {
  const progress = job.progress;
  const percentage = progress?.total ? Math.min(100, Math.max(0, progress.completed / progress.total * 100)) : 0;
  const activePhase = stageOrder[job.stage] ?? 0;
  const progressDescription = progress?.total
    ? `${formatBytes(progress.completed)} / ${formatBytes(progress.total)}`
    : job.stage === "installing_dependencies" || job.stage === "activating" || job.stage === "runtime"
      ? "正在安装已下载的依赖"
      : job.stage === "verifying" || job.stage === "extracting" || job.stage === "unpacking" || job.stage === "checking"
        ? job.message
        : "正在准备…";
  return (
    <section className="provision-progress-card" role="status" aria-live="polite">
      <div className="provision-progress-heading">
        <div className="download-pulse" aria-hidden="true"><span>↓</span></div>
        <div>
          <small>{job.resource ? `正在处理 ${layerLabel(job.resource)}` : "正在准备运行环境"}</small>
          <strong>{job.message || "正在准备下载"}</strong>
        </div>
        <b>{progress?.total ? `${Math.round(percentage)}%` : "处理中"}</b>
        {cancellable && <button className="cancel-button" onClick={() => void onCancel()} type="button">取消</button>}
      </div>
      {job.proxy && <p className="provision-proxy">通过 {job.proxy} 下载</p>}
      <div className={`provision-progress-track ${progress?.total ? "" : "indeterminate"}`}>
        <span style={progress?.total ? { width: `${percentage}%` } : undefined} />
      </div>
      <div className="provision-progress-meta">
        <span>{progressDescription}</span>
        <span>{progress?.bytes_per_second ? `${formatBytes(progress.bytes_per_second)}/s` : "校验后自动安装"}</span>
      </div>
      <div className="provision-phase-list">
        {installPhases.map((phase, index) => (
          <span className={index < activePhase ? "complete" : index === activePhase ? "active" : ""} key={phase.id}>
            <i>{index < activePhase ? "✓" : index + 1}</i>{phase.label}
          </span>
        ))}
      </div>
    </section>
  );
}

function modelTooltip(models: RuntimeItem[], optionalModels: RuntimeItem[]): string {
  const names = [
    "人声分离模型",
    "Whisper 转写模型",
    "Qwen 复核模型",
    ...(optionalModels.length ? ["说话人分离模型（活动检测 + 声纹）"] : []),
  ];
  const ready = [...models, ...optionalModels].filter((item) => item.state === "ready").length;
  const total = models.length + optionalModels.length;
  return `供本机 FineSub 转写使用，按需下载与删除。\n包含：${names.join("、")}${total > 0 ? `\n当前就绪：${ready}/${total}` : ""}`;
}

export function RuntimeSection(props: RuntimeSectionProps) {
  const {
    provisionMessage,
    provision,
    pythonBootstrap,
    sidecar,
    migrationActive = false,
    onCancelInstall,
    onDismissProvisionMessage,
    onInstall,
    onInstallPython,
    onRemoveModels,
    onRemoveUnused,
    cleanup,
    requestedInstall,
    storage,
    storageProgress,
    storageBusy,
    storageMessage,
    onDismissStorageMessage,
    onChooseStorage,
    onRelocateStorage,
    onResetStorage,
    onCancelStorage,
    onOpenStorage,
    runtimeCache,
  } = props;

  const [confirmRemoveModels, setConfirmRemoveModels] = useState(false);
  const [confirmUnused, setConfirmUnused] = useState(false);
  const { pending, choosing, choose, clear } = useDestinationChoice(onChooseStorage);

  const runtimeLocation = (storage?.locations ?? []).find((item) => item.target === "runtime") ?? null;
  const relocatingRuntime = storageProgress?.active === true && storageProgress.target === "runtime";
  const storageActive = storageProgress?.active === true;

  const [handledInstall, setHandledInstall] = useState(0);
  useEffect(() => {
    if (!requestedInstall || requestedInstall.seq === handledInstall) return;
    setHandledInstall(requestedInstall.seq);
    void onInstall(requestedInstall.target);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [requestedInstall]);

  const job = provision?.job;
  const jobRunning = job?.state === "running";
  const pythonInstalling = pythonBootstrap?.state === "running";
  const pythonNeedsRepair = Boolean(pythonBootstrap?.python) && (pythonInstalling || pythonBootstrap?.state === "failed");
  const bootstrapBroken = (provision?.bootstrap_error ?? "") !== "";
  const connecting = !provision && !bootstrapBroken && (!pythonBootstrap || pythonBootstrap.state === "missing" || pythonBootstrap.state === "running" || pythonBootstrap.state === "ready");

  const layerById = new Map<string, RuntimeLayerOverview>((provision?.layers ?? []).map((layer) => [layer.id, layer]));
  const toolsLayer = layerById.get("tools");
  const pythonLayer = layerById.get("python");
  const modelsLayer = layerById.get("models");

  const toolsState: RuntimeLayerOverview["state"] = toolsLayer?.state
    ?? (provision?.media_ready ? "ready" : "missing");
  const pythonState: RuntimeLayerOverview["state"] = pythonLayer?.state
    ?? (provision?.runtime?.state === "ready" ? "ready" : provision?.runtime?.state === "outdated" ? "outdated" : provision?.runtime?.state === "failed" ? "damaged" : "missing");

  const toolsMissing = (provision?.media_supported ?? false) && toolsState === "missing";
  const pythonMissing = (provision?.runtime_supported ?? false) && pythonState === "missing";
  const baseMissing = Boolean(provision && !bootstrapBroken && (toolsMissing || pythonMissing));
  const baseTarget: RuntimeInstallTarget = provision?.runtime_supported ? "runtime" : "media";
  const baseDownloadBytes = (toolsMissing ? toolsLayer?.archive_bytes ?? 0 : 0) + (pythonMissing ? pythonLayer?.archive_bytes ?? 0 : 0);

  const showOnboardingCard = !migrationActive && (pythonInstalling || (baseMissing && !jobRunning));
  const showPythonRecoveryCard = !migrationActive && !pythonInstalling && (
    bootstrapBroken
    || pythonBootstrap?.state === "failed"
    || Boolean(pythonBootstrap && !pythonBootstrap.supported && !sidecar?.running)
  );

  const requiredModels = provision?.models ?? [];
  const optionalModels = provision?.optional_models ?? [];
  const modelsReady = modelsLayer ? modelsLayer.state === "ready" : (requiredModels.length > 0 && requiredModels.every((item) => item.state === "ready"));
  const modelsInstalled = modelsLayer ? modelsLayer.current !== null : requiredModels.some((item) => item.state === "ready");
  const modelsNeedsUpdate = modelsLayer ? modelsLayer.state === "outdated" : requiredModels.some((item) => item.state === "outdated");
  const modelsNeedsRepair = modelsLayer ? modelsLayer.state === "damaged" : requiredModels.some((item) => item.state === "failed");

  const migrationBlocked = migrationActive ? "旧版升级正在自动更新运行环境，请等待完成。" : "";
  const connectionBlocked = !provision
    ? connecting ? "正在连接本地服务…" : "本地执行服务尚未连接。"
    : bootstrapBroken ? `运行环境层不可用：${provision.bootstrap_error}` : "";
  const platformNotice = provision && !provision.runtime_supported
    ? `${provision.platform} 上仅提供工具层；本机 GPU 转写仅支持 Windows x64，可使用云端 FineSub 或云 API 引擎。`
    : "";
  const jobBlocked = jobRunning ? "已有安装任务正在进行。" : "";

  const toolsBlocked = migrationBlocked || connectionBlocked || (provision && !provision.media_supported ? "当前平台暂不支持工具层。" : "") || jobBlocked;
  const pythonLayerBlocked = migrationBlocked || connectionBlocked || (provision && !provision.runtime_supported ? `本地 GPU 环境暂不支持 ${provision.platform}。` : "") || jobBlocked;
  const modelsBlocked = migrationBlocked
    || connectionBlocked
    || (provision && !provision.runtime_supported ? `本地模型暂不支持 ${provision.platform}。` : "")
    || jobBlocked
    || (provision && pythonState !== "ready" ? "请先完成基础环境初始化后再下载模型。" : "");
  const removeModelsBlocked = migrationBlocked || connectionBlocked || jobBlocked || (!modelsInstalled ? "模型层尚未安装。" : "");

  const installedLayers = (provision?.layers ?? []).filter((layer) => layer.current !== null);
  const repairBlocked = migrationBlocked || connectionBlocked || jobBlocked
    || (provision && provision.layers && installedLayers.length === 0 ? "还没有安装任何运行环境层。" : "");

  const cleanupItems: RuntimeCleanupItem[] = cleanup?.items
    ?? (provision?.unused_layers ?? []).map((item) => ({ ...item, reason: "old-version" as const, blocked: "" }));
  const cleanupChoice = cleanupView(cleanupItems, provision, []);
  const hasOldVersions = cleanupChoice.removable.length > 0 || cleanupChoice.held.length > 0;
  const unusedBlocked = migrationBlocked || connectionBlocked || jobBlocked
    || (cleanupChoice.removable.length === 0 ? "旧版本有未结束的任务在用，请稍后清理。" : "");

  const pythonBlocked = !pythonBootstrap
    ? ""
    : !pythonBootstrap.supported
      ? pythonBootstrap.message || `${pythonBootstrap.platform} 暂不支持自动安装 Python。`
      : pythonInstalling ? "Python 正在安装中。" : "";

  const confirmRelocation = () => {
    if (!pending) return;
    const { directory } = pending;
    clear();
    void onRelocateStorage("runtime", directory);
  };

  const runtimeCacheStatus = runtimeLocation && !runtimeLocation.missing ? runtimeCache?.status : null;
  const sharedCacheOnly = runtimeCacheStatus != null && runtimeCacheStatus.bytes > 0 && runtimeCacheStatus.reclaimable === 0;
  const nothingToOpen = !runtimeLocation || runtimeLocation.missing || (runtimeLocation.empty && runtimeLocation.bytes === 0 && runtimeLocation.files === 0);
  const tightDisk = runtimeLocation ? !runtimeLocation.enoughSpace : false;
  const baseInterrupted = job?.state === "failed" || job?.state === "cancelled";

  const toolsSupported = provision?.media_supported ?? true;
  const pythonSupported = provision?.runtime_supported ?? true;
  const toolsWorking = migrationActive || (jobRunning && (job?.target === "media" || job?.target === "runtime" || job?.target === "all"));
  const pythonWorking = migrationActive || (jobRunning && (job?.target === "runtime" || job?.target === "all"));
  const modelsWorking = migrationActive || (jobRunning && (job?.target === "models" || job?.target === "all"));

  const toolsActionLabel = toolsState === "outdated" ? "更新" : toolsState === "damaged" ? "修复" : "安装";
  const pythonActionLabel = pythonState === "outdated" ? "更新" : pythonState === "damaged" ? "修复" : "安装";
  const modelsActionLabel = modelsNeedsRepair ? "修复模型" : modelsNeedsUpdate ? "更新模型" : "下载模型";
  const modelsSizeText = modelsLayer && modelsLayer.bytes > 0
    ? formatStorageBytes(modelsLayer.bytes)
    : modelsLayer && modelsLayer.archive_bytes > 0
      ? `下载约 ${formatStorageBytes(modelsLayer.archive_bytes)}`
      : "";

  return (
    <section className="runtime-layout">
      <article className="panel runtime-flat-panel">
        <Notice className="provision-message failed" message={provisionMessage} autoDismissMs={0} onDismiss={onDismissProvisionMessage} />

        {migrationActive && (
          <p className="provision-blocked" role="status">旧版升级正在后台更新运行环境，请查看页面顶部进度。</p>
        )}

        {!migrationActive && platformNotice !== "" && (
          <p className="provision-blocked" role="status">{platformNotice}</p>
        )}

        {!migrationActive && !showOnboardingCard && !connecting && connectionBlocked !== "" && (
          <p className={bootstrapBroken ? "provision-blocked failed" : "provision-blocked"} role="status">{connectionBlocked}</p>
        )}

        {/* A1 首次初始化卡片：启动器静默就绪 → 先确认安装目录 → 点击开始安装 */}
        {showOnboardingCard && (
          <section className={`install-location-card ${tightDisk ? "tight" : ""} awaiting`}>
            {pythonInstalling && pythonBootstrap ? (
              <ProvisionProgress
                job={{
                  resource: "本地服务",
                  stage: pythonBootstrap.stage,
                  message: "正在初始化本地服务，不会修改系统环境",
                  progress: pythonBootstrap.progress,
                  proxy: pythonBootstrap.proxy,
                }}
                onCancel={async () => undefined}
                cancellable={false}
              />
            ) : (
              <>
                <div className="install-location-main">
                  <div className="install-location-icon" aria-hidden="true">{tightDisk ? "!" : "HD"}</div>
                  <div className="install-location-copy">
                    <span>首次初始化 · 基础环境</span>
                    <h3>{baseInterrupted ? "基础环境安装未完成" : tightDisk ? "当前磁盘空间不足，建议更换安装位置" : "确认基础运行环境的安装位置"}</h3>
                    <p>
                      {baseInterrupted && job?.message
                        ? job.message
                        : `将安装工具层与 Python 环境层${baseDownloadBytes > 0 ? `（下载约 ${formatStorageBytes(baseDownloadBytes)}）` : ""}，磁盘紧张时建议选非系统盘。`}
                    </p>
                  </div>
                </div>
                {runtimeLocation ? (
                  <>
                    <div className="install-location-path" title={runtimeLocation.directory}>
                      <span>安装到</span>
                      <code>{runtimeLocation.directory}</code>
                    </div>
                    <p className={tightDisk ? "install-location-free tight" : "install-location-free"}>
                      {runtimeLocation.volume} 剩余 <b>{formatStorageBytes(runtimeLocation.freeBytes)}</b>
                      {runtimeLocation.custom && "（已自定义）"}
                    </p>
                  </>
                ) : (
                  <p className="install-location-free">正在读取默认安装位置…</p>
                )}
                {pending && <RelocationConfirm pending={pending} busy={storageBusy} empty={runtimeLocation?.empty ?? true} onConfirm={confirmRelocation} onDismiss={clear} />}
                <RelocationProgressView progress={relocatingRuntime ? storageProgress : null} onCancel={() => void onCancelStorage()} />
                <div className="install-location-actions">
                  {runtimeLocation && (
                    <button
                      className="quiet-button"
                      disabled={storageBusy || relocatingRuntime || choosing !== ""}
                      onClick={() => void choose("runtime")}
                      type="button"
                    >
                      {choosing === "runtime" ? "正在选择…" : "更改位置…"}
                    </button>
                  )}
                  <button
                    className="primary-button"
                    disabled={!runtimeLocation || storageBusy || relocatingRuntime || (baseTarget === "runtime" ? pythonLayerBlocked : toolsBlocked) !== ""}
                    onClick={() => void onInstall(baseTarget)}
                    type="button"
                  >
                    {relocatingRuntime ? "正在设置位置…" : baseInterrupted ? "继续安装" : baseDownloadBytes > 0 ? `开始安装（约 ${formatStorageBytes(baseDownloadBytes)}）` : "开始安装"}
                  </button>
                </div>
              </>
            )}
          </section>
        )}

        {showPythonRecoveryCard && (
          <section className={`required-dependency-card ${pythonBootstrap?.state ?? "failed"}`}>
            <div className="required-dependency-main">
              <div className="required-dependency-icon" aria-hidden="true">PY</div>
              <div className="required-dependency-copy">
                <span>本地服务</span>
                <h3>Python 3.12 启动环境</h3>
                <p>{pythonBootstrap?.state === "failed"
                  ? pythonBootstrap.message
                  : bootstrapBroken
                    ? "当前服务运行在缺少依赖的 Python 上，修复后会自动重启。"
                    : pythonBootstrap?.message || "需要准备隔离的 Python 3.12 启动环境。"}</p>
                {pythonBootstrap && !pythonBootstrap.supported && (
                  <p className="required-dependency-hint">手动方案：准备装有 httpx、pydantic 的 Python 3.12，通过环境变量 <code>NONOKA_PYTHON</code> 指向它后重启应用。</p>
                )}
              </div>
              <div className="required-dependency-action">
                <button className="primary-button" disabled={pythonBlocked !== ""} title={pythonBlocked || undefined} onClick={() => void onInstallPython()}>
                  {pythonNeedsRepair ? "修复环境" : "重试安装"}
                </button>
              </div>
            </div>
          </section>
        )}

        {!migrationActive && jobRunning && job && <ProvisionProgress job={job} onCancel={onCancelInstall} />}
        {!migrationActive && !jobRunning && !showOnboardingCard && <Notice className={`provision-message ${job?.state ?? ""}`} message={job?.message ?? ""} />}

        {/* 拍平的 3 行核心层列表：工具层、Python 环境层、AI 模型层 */}
        <div className="runtime-layer-list">
          <div className="runtime-layer-row">
            <div className="runtime-layer-info">
              <span className={toolsState === "ready" ? "layer-dot ready" : "layer-dot"} aria-hidden="true">{toolsState === "ready" ? "✓" : "↓"}</span>
              <strong>工具层</strong>
              <InfoTip text="默认安装，支持更新。包含 FFmpeg、FFprobe、tokcount 与在线视频下载工具（yt-dlp、aria2c、Node.js、PO Token 生成器）。" label="工具层" />
            </div>
            <div className="runtime-layer-actions">
              {toolsLayer && toolsLayer.bytes > 0 && <span className="runtime-layer-size">{formatStorageBytes(toolsLayer.bytes)}</span>}
              {!showOnboardingCard && !migrationActive && toolsSupported && toolsState !== "ready" && (
                <button
                  type="button"
                  className="quiet-button runtime-compact-btn"
                  disabled={toolsBlocked !== ""}
                  onClick={() => void onInstall("media")}
                >
                  {toolsWorking ? `${toolsActionLabel}中…` : toolsActionLabel}
                </button>
              )}
              {(!toolsSupported || toolsWorking || toolsState !== "ready") && (
                <Mark tone={!toolsSupported ? "neutral" : toolsWorking ? "accent" : toolsState === "missing" ? "neutral" : "caution"}>
                  {!toolsSupported ? "不支持" : toolsWorking ? "处理中" : layerStateLabels[toolsState]}
                </Mark>
              )}
            </div>
          </div>

          <div className="runtime-layer-row">
            <div className="runtime-layer-info">
              <span className={pythonState === "ready" ? "layer-dot ready" : "layer-dot"} aria-hidden="true">{pythonState === "ready" ? "✓" : pythonSupported ? "↓" : "—"}</span>
              <strong>Python 环境层</strong>
              <InfoTip text="默认安装，支持更新。提供本机转写与推理所需的 Python 3.12 / PyTorch / CUDA 运行环境。" label="Python 环境层" />
            </div>
            <div className="runtime-layer-actions">
              {pythonLayer && pythonLayer.bytes > 0 && <span className="runtime-layer-size">{formatStorageBytes(pythonLayer.bytes)}</span>}
              {!showOnboardingCard && !migrationActive && pythonSupported && pythonState !== "ready" && (
                <button
                  type="button"
                  className="quiet-button runtime-compact-btn"
                  disabled={pythonLayerBlocked !== ""}
                  onClick={() => void onInstall("runtime")}
                >
                  {pythonWorking ? `${pythonActionLabel}中…` : pythonActionLabel}
                </button>
              )}
              {(!pythonSupported || pythonWorking || pythonState !== "ready") && (
                <Mark tone={!pythonSupported ? "neutral" : pythonWorking ? "accent" : pythonState === "missing" ? "neutral" : "caution"}>
                  {!pythonSupported ? "不支持" : pythonWorking ? "处理中" : layerStateLabels[pythonState]}
                </Mark>
              )}
            </div>
          </div>

          <div className="runtime-layer-row">
            <div className="runtime-layer-info">
              <span className={modelsReady ? "layer-dot ready" : "layer-dot"} aria-hidden="true">{modelsReady ? "✓" : "↓"}</span>
              <strong>AI 模型层</strong>
              <InfoTip text={modelTooltip(requiredModels, optionalModels)} label="AI 模型层" />
            </div>
            <div className="runtime-layer-actions">
              {modelsSizeText && <span className="runtime-layer-size">{modelsSizeText}</span>}
              {modelsInstalled && !migrationActive && (
                <button
                  className={confirmRemoveModels ? "runtime-remove-button runtime-compact-btn confirming" : "runtime-remove-button runtime-compact-btn"}
                  disabled={removeModelsBlocked !== ""}
                  onBlur={() => setConfirmRemoveModels(false)}
                  onClick={() => {
                    if (!confirmRemoveModels) {
                      setConfirmRemoveModels(true);
                      return;
                    }
                    setConfirmRemoveModels(false);
                    void onRemoveModels();
                  }}
                  title={removeModelsBlocked || "仅删除 AI 模型层，基础环境与用户数据保持不变"}
                  type="button"
                >
                  {confirmRemoveModels ? "确认删除" : "删除模型"}
                </button>
              )}
              {!modelsReady && !migrationActive && (
                <button
                  className="primary-button runtime-compact-btn"
                  disabled={modelsBlocked !== ""}
                  onClick={() => void onInstall("models")}
                  title={modelsBlocked || modelsActionLabel}
                  type="button"
                >
                  {modelsWorking ? "下载中…" : modelsActionLabel}
                </button>
              )}
              {(modelsWorking || !modelsReady) && (
                <Mark tone={modelsWorking ? "accent" : modelsNeedsUpdate || modelsNeedsRepair ? "caution" : "neutral"}>
                  {modelsWorking ? "处理中" : modelsNeedsUpdate ? "需更新" : modelsNeedsRepair ? "需修复" : "未安装"}
                </Mark>
              )}
            </div>
          </div>
        </div>

        {/* 仅在存在旧版本残留时出现的一键清理 */}
        {hasOldVersions && (
          <div className="runtime-inline-bar runtime-old-bar">
            <div className="runtime-inline-meta">
              <strong>旧版本残留</strong>
              <InfoTip text={[...cleanupChoice.removable, ...cleanupChoice.held].map((item) => `${layerLabel(item.layer)} ${item.name}（${formatStorageBytes(item.bytes)}）`).join("\n")} label="旧版本残留" />
              <span>
                {cleanupChoice.removable.length > 0
                  ? `${cleanupChoice.removable.length} 项可清理 · ${formatStorageBytes(cleanupChoice.oldBytes)}`
                  : `${cleanupChoice.held.length} 项正在使用`}
              </span>
            </div>
            <button
              className={confirmUnused ? "runtime-remove-button runtime-compact-btn confirming" : "quiet-button runtime-compact-btn"}
              disabled={unusedBlocked !== ""}
              onBlur={() => setConfirmUnused(false)}
              onClick={() => {
                if (!confirmUnused) {
                  setConfirmUnused(true);
                  return;
                }
                setConfirmUnused(false);
                void onRemoveUnused([]);
              }}
              type="button"
            >
              {confirmUnused ? `确认清理 ${formatStorageBytes(cleanupChoice.oldBytes)}` : "一键清理"}
            </button>
          </div>
        )}

        {/* 合并的安装目录区（两行结构：顶栏标题/占用/操作 + 底部全宽路径条与打开） */}
        {runtimeLocation && !showOnboardingCard && (
          <div className="runtime-dir-section">
            <div className="dir-header-row">
              <div className="dir-header-left">
                <strong>安装目录</strong>
                {runtimeLocation.custom && <span className="storage-badge">已迁移</span>}
                <span className="dir-meta">
                  {runtimeLocation.missing
                    ? "目录不可访问"
                    : `${formatStorageBytes(runtimeLocation.bytes)} · ${runtimeLocation.volume} 剩余 ${formatStorageBytes(runtimeLocation.freeBytes)}`}
                </span>
              </div>
              <div className="dir-header-actions">
                {runtimeCache && runtimeCacheStatus != null && runtimeCacheStatus.bytes > 0 && (
                  <button
                    type="button"
                    className="runtime-remove-button runtime-compact-btn"
                    disabled={runtimeCache.busy || storageBusy || storageActive || jobRunning || runtimeCacheStatus.reclaimable === 0}
                    title={sharedCacheOnly ? "缓存与已安装环境共用，删除不会腾出空间" : `清理缓存（${formatStorageBytes(runtimeCacheStatus.reclaimable)}）`}
                    onClick={() => void runtimeCache.clear()}
                  >
                    {runtimeCache.busy ? "清理中…" : "清理缓存"}
                  </button>
                )}
                {runtimeLocation.custom && (
                  <button
                    type="button"
                    className="quiet-button runtime-compact-btn"
                    disabled={storageBusy || storageActive || jobRunning || migrationActive}
                    onClick={() => void onResetStorage("runtime")}
                  >
                    移回默认
                  </button>
                )}
                <button
                  type="button"
                  className="quiet-button runtime-compact-btn"
                  disabled={storageBusy || storageActive || jobRunning || migrationActive || choosing !== ""}
                  onClick={() => void choose("runtime")}
                >
                  {choosing === "runtime" ? "选择中…" : "更改位置…"}
                </button>
                <button
                  type="button"
                  className="quiet-button runtime-compact-btn"
                  disabled={repairBlocked !== ""}
                  onClick={() => void onInstall("repair")}
                  title={repairBlocked || "逐文件核对已安装的运行环境层，损坏自动重装"}
                >
                  {jobRunning && job?.target === "repair" ? "核对中…" : "完整核对"}
                </button>
              </div>
            </div>
            <div className="dir-path-bar">
              <code title={runtimeLocation.directory}>{runtimeLocation.directory}</code>
              {onOpenStorage && (
                <button
                  type="button"
                  className="dir-open-btn"
                  disabled={nothingToOpen}
                  title={nothingToOpen ? "目录还不存在" : "在文件管理器中打开"}
                  onClick={() => void onOpenStorage("runtime")}
                >
                  <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2 4.5A1.5 1.5 0 0 1 3.5 3h2.8l1.4 1.5h4.8A1.5 1.5 0 0 1 14 6v5.5a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 2 11.5z" /></svg>
                  打开
                </button>
              )}
            </div>
            {pending && <RelocationConfirm pending={pending} busy={storageBusy} empty={runtimeLocation.empty} onConfirm={confirmRelocation} onDismiss={clear} />}
            <RelocationProgressView progress={relocatingRuntime ? storageProgress : null} onCancel={() => void onCancelStorage()} />
            {runtimeCache?.message && (
              <Notice className="storage-cache-message" message={runtimeCache.message} tone={runtimeCache.tone} onDismiss={runtimeCache.dismiss} />
            )}
            <Notice className="storage-message" message={storageMessage} onDismiss={onDismissStorageMessage} />
          </div>
        )}
      </article>
    </section>
  );
}
