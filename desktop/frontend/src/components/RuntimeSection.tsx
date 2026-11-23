import type { Snapshot as SidecarSnapshot } from "../../bindings/github.com/Ricori/nonoka-x/desktop/internal/sidecar/models.js";
import { useState } from "react";
import type { PythonBootstrapState, RuntimeCleanupItem, RuntimeCleanupPlan, RuntimeInstallTarget, RuntimeItem, RuntimeLayerId, RuntimeLayerOverview, RuntimeProvisionState } from "../bridge/runtime.ts";
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
  jobMessageDismissed?: boolean;
  onDismissJobMessage?: () => void;
  provision: RuntimeProvisionState | null;
  pythonBootstrap: PythonBootstrapState | null;
  sidecar: SidecarSnapshot | null;
  migrationActive?: boolean;
  onInstall: (target: RuntimeInstallTarget) => Promise<void>;
  onInstallOffline?: (layer: RuntimeLayerId) => Promise<void>;
  onInstallPython: () => Promise<void>;
  onCancelInstall: () => Promise<void>;
  onRemoveModels: () => Promise<void>;
  onRemoveModelsMoss?: () => Promise<void>;
  onRemoveScreenText?: () => Promise<void>;
  onRemoveUnused: (layers: string[]) => Promise<void>;
  cleanup: RuntimeCleanupPlan | null;
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
  onOpenOnboarding?: () => void;
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
  manifest: 0,
  resource: 0,
  downloading: 0,
  offline: 1,
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

const KNOWN_LAYER_IDS = new Set<string>(["tools", "python", "models", "models-moss", "screen-text"]);

/** 文字识别层的说明（设置页的小问号）。 */
const SCREEN_TEXT_TIP = "编辑器里「文字识别」要用它：识别画面上的字，翻译后盖在原处，也能抹掉原字再压制。\n只支持 Windows；有 NVIDIA 显卡（驱动 580 以上）会快很多。";

function isRuntimeLayerId(value: string): value is RuntimeLayerId {
  return KNOWN_LAYER_IDS.has(value);
}

function offlineButtonTitle(layer: RuntimeLayerOverview | undefined, blocked: string): string {
  if (blocked) return blocked;
  return `选择本地 .tar.zst 离线包安装（与 R2 压缩包一致${layer?.version ? `，所需版本：${layer.version}` : ""}）`;
}

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

function ProvisionProgress({
  job,
  onCancel,
  cancellable = true,
  onSwitchToOffline,
}: {
  job: ProgressJob;
  onCancel: () => Promise<void>;
  cancellable?: boolean;
  onSwitchToOffline?: () => void;
}) {
  const progress = job.progress;
  const percentage = progress?.total ? Math.min(100, Math.max(0, progress.completed / progress.total * 100)) : 0;
  const activePhase = stageOrder[job.stage] ?? 0;
  const slowDownload = job.stage === "downloading"
    && progress?.bytes_per_second != null
    && progress.bytes_per_second > 0
    && progress.bytes_per_second < 512 * 1024;
  const progressDescription = progress?.total
    ? `${formatBytes(progress.completed)} / ${formatBytes(progress.total)}`
    : job.stage === "installing_dependencies" || job.stage === "activating" || job.stage === "runtime"
      ? "正在安装已下载的依赖"
      : job.stage === "offline" || job.stage === "verifying" || job.stage === "extracting" || job.stage === "unpacking" || job.stage === "checking"
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
        {(onSwitchToOffline || cancellable) && (
          <div className="provision-progress-actions">
            {onSwitchToOffline && (
              <button
                className="quiet-button runtime-compact-btn"
                title="下载慢或卡住时，可直接选择本地 .tar.zst 离线包安装（选定后自动中止在线下载）"
                onClick={onSwitchToOffline}
                type="button"
              >
                改用离线包…
              </button>
            )}
            {cancellable && <button className="cancel-button" onClick={() => void onCancel()} type="button">取消</button>}
          </div>
        )}
      </div>
      {job.proxy && <p className="provision-proxy">通过 {job.proxy} 下载</p>}
      <div className={`provision-progress-track ${progress?.total ? "" : "indeterminate"}`}>
        <span style={progress?.total ? { width: `${percentage}%` } : undefined} />
      </div>
      <div className="provision-progress-meta">
        <span>{progressDescription}</span>
        <span>{progress?.bytes_per_second ? `${formatBytes(progress.bytes_per_second)}/s` : job.stage === "offline" ? "离线包校验后自动安装" : "校验后自动安装"}</span>
      </div>
      {slowDownload && onSwitchToOffline && (
        <p className="provision-slow-hint">当前在线下载较慢，可随时点击「改用离线包…」直接导入本地 .tar.zst 压缩包。</p>
      )}
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

function mossModelTooltip(mossModels: RuntimeItem[]): string {
  const ready = mossModels.filter((item) => item.state === "ready").length;
  const total = mossModels.length;
  return `供本机 MOSS 0.9B 转写使用，按需下载与删除（需 NVIDIA RTX 30 系及以上显卡，约 8 GB 显存）。\n包含：MOSS-Transcribe-Diarize（0.9B 语音识别与说话人分离）、说话人声纹模型（WeSpeaker CPU + PLDA，约 26 MB）${total > 0 ? `\n当前就绪：${ready}/${total}` : ""}`;
}

export function RuntimeSection(props: RuntimeSectionProps) {
  const {
    provisionMessage,
    jobMessageDismissed = false,
    onDismissJobMessage,
    provision,
    pythonBootstrap,
    sidecar,
    migrationActive = false,
    onCancelInstall,
    onDismissProvisionMessage,
    onInstall,
    onInstallOffline,
    onInstallPython,
    onRemoveModels,
    onRemoveModelsMoss,
    onRemoveScreenText,
    onRemoveUnused,
    cleanup,
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
    onOpenOnboarding,
  } = props;

  const [confirmRemoveModels, setConfirmRemoveModels] = useState(false);
  const [confirmRemoveModelsMoss, setConfirmRemoveModelsMoss] = useState(false);
  const [confirmRemoveScreenText, setConfirmRemoveScreenText] = useState(false);
  const [confirmUnused, setConfirmUnused] = useState(false);
  const { pending, choosing, choose, clear } = useDestinationChoice(onChooseStorage);

  const runtimeLocation = (storage?.locations ?? []).find((item) => item.target === "runtime") ?? null;
  const relocatingRuntime = storageProgress?.active === true && storageProgress.target === "runtime";
  const storageActive = storageProgress?.active === true;

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
  const modelsMossLayer = layerById.get("models-moss");
  const screenTextLayer = layerById.get("screen-text");

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
  const mossModels = provision?.moss_models ?? [];
  const modelsReady = modelsLayer ? modelsLayer.state === "ready" : (requiredModels.length > 0 && requiredModels.every((item) => item.state === "ready"));
  const modelsInstalled = modelsLayer ? modelsLayer.current !== null : requiredModels.some((item) => item.state === "ready");
  const modelsNeedsUpdate = modelsLayer ? modelsLayer.state === "outdated" : requiredModels.some((item) => item.state === "outdated");
  const modelsNeedsRepair = modelsLayer ? modelsLayer.state === "damaged" : requiredModels.some((item) => item.state === "failed");

  const modelsMossIndexed = Boolean(modelsMossLayer);
  const modelsMossReady = modelsMossLayer ? modelsMossLayer.state === "ready" : (mossModels.length > 0 && mossModels.every((item) => item.state === "ready"));
  const modelsMossInstalled = modelsMossLayer ? modelsMossLayer.current !== null : mossModels.some((item) => item.state === "ready");
  const modelsMossNeedsUpdate = modelsMossLayer ? modelsMossLayer.state === "outdated" : mossModels.some((item) => item.state === "outdated");
  const modelsMossNeedsRepair = modelsMossLayer ? modelsMossLayer.state === "damaged" : mossModels.some((item) => item.state === "failed");

  const migrationBlocked = migrationActive ? "旧版升级正在自动更新运行环境，请等待完成。" : "";
  const connectionBlocked = !provision
    ? connecting ? "正在连接本地服务…" : "本地执行服务尚未连接。"
    : bootstrapBroken ? `运行环境层不可用：${provision.bootstrap_error}` : "";
  const platformNotice = provision && !provision.runtime_supported
    ? provision.media_supported
      ? `${provision.platform} 上仅提供工具层；本机 GPU 转写仅支持 Windows x64，可使用云端 FineSub 或云 API 引擎。`
      : `当前平台（${provision.platform}）暂不支持预制运行环境层；本机 GPU 转写仅支持 Windows x64，可使用云端 FineSub。`
    : "";
  const jobBlocked = jobRunning ? "已有安装任务正在进行。" : "";

  const toolsBlocked = migrationBlocked || connectionBlocked || (provision && !provision.media_supported ? "当前平台暂不支持工具层。" : "") || jobBlocked;
  const pythonLayerBlocked = migrationBlocked || connectionBlocked || (provision && !provision.runtime_supported ? `本地 GPU 环境暂不支持 ${provision.platform}。` : "") || jobBlocked;
  const modelsOfflineBlocked = migrationBlocked
    || connectionBlocked
    || (provision && !provision.runtime_supported ? `本地模型暂不支持 ${provision.platform}。` : "")
    || jobBlocked;
  const modelsBlocked = modelsOfflineBlocked
    || (provision && pythonState !== "ready" ? "请先完成基础环境初始化后再下载模型。" : "");
  const removeModelsBlocked = migrationBlocked || connectionBlocked || jobBlocked || (!modelsInstalled ? "模型层尚未安装。" : "");
  const modelsMossOfflineBlocked = modelsOfflineBlocked
    || (provision && !modelsMossIndexed ? "当前版本尚未在 layers.json 中登记 MOSS 模型层。" : "");
  const modelsMossBlocked = modelsMossOfflineBlocked
    || (provision && pythonState !== "ready" ? "请先完成基础环境初始化后再下载模型。" : "");
  const removeModelsMossBlocked = migrationBlocked || connectionBlocked || jobBlocked || (!modelsMossInstalled ? "MOSS 模型层尚未安装。" : "");

  const screenText = provision?.screen_text;
  const screenTextAvailable = Boolean(screenTextLayer) || screenText?.available === true;
  const screenTextReady = screenTextLayer ? screenTextLayer.state === "ready" : screenText?.state === "ready";
  const screenTextInstalled = screenTextLayer ? screenTextLayer.current !== null : screenText?.state === "ready";
  const screenTextNeedsUpdate = screenTextLayer?.state === "outdated";
  const screenTextNeedsRepair = screenTextLayer?.state === "damaged";
  const screenTextBlocked = migrationBlocked || connectionBlocked
    || (provision && !provision.runtime_supported ? "只支持 Windows。" : "")
    || (provision && !screenTextAvailable ? "这个版本还没有文字识别层。" : "")
    || jobBlocked;
  const removeScreenTextBlocked = migrationBlocked || connectionBlocked || jobBlocked || (!screenTextInstalled ? "还没安装。" : "");

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
  const toolsWorking = migrationActive || (jobRunning && (job?.resource === "tools" || job?.target === "offline:tools" || job?.target === "media" || job?.target === "runtime" || job?.target === "all"));
  const pythonWorking = migrationActive || (jobRunning && (job?.resource === "python" || job?.target === "offline:python" || job?.target === "runtime" || job?.target === "all"));
  const modelsWorking = migrationActive || (jobRunning && (job?.resource === "models" || job?.target === "offline:models" || job?.target === "models" || job?.target === "all"));
  const modelsMossWorking = migrationActive || (jobRunning && (job?.resource === "models-moss" || job?.target === "offline:models-moss" || job?.target === "models-moss"));
  const screenTextWorking = jobRunning && (job?.resource === "screen-text" || job?.target === "offline:screen-text" || job?.target === "screen-text");

  const activeDownloadLayer: RuntimeLayerId | null = (() => {
    if (!jobRunning || !job || !onInstallOffline || !pythonSupported) return null;
    if (job.stage === "offline" || job.stage === "unpacking" || job.stage === "checking" || job.stage === "completed") return null;
    if (isRuntimeLayerId(job.resource)) return job.resource;
    if (job.target === "media") return "tools";
    if (job.target === "models" || job.target === "optional-models") return "models";
    if (job.target === "models-moss") return "models-moss";
    if (job.target === "screen-text") return "screen-text";
    if (job.target === "runtime" || job.target === "all") return toolsState !== "ready" ? "tools" : pythonState !== "ready" ? "python" : null;
    return null;
  })();

  const toolsActionLabel = toolsState === "outdated" ? "更新" : toolsState === "damaged" ? "修复" : "安装";
  const pythonActionLabel = pythonState === "outdated" ? "更新" : pythonState === "damaged" ? "修复" : "安装";
  const modelsActionLabel = modelsNeedsRepair ? "修复模型" : modelsNeedsUpdate ? "更新模型" : "下载模型";
  const modelsMossActionLabel = modelsMossNeedsRepair ? "修复模型" : modelsMossNeedsUpdate ? "更新模型" : "下载模型";
  const modelsSizeText = modelsLayer && modelsLayer.bytes > 0
    ? formatStorageBytes(modelsLayer.bytes)
    : modelsLayer && modelsLayer.archive_bytes > 0
      ? `下载约 ${formatStorageBytes(modelsLayer.archive_bytes)}`
      : "";
  const screenTextActionLabel = screenTextNeedsRepair ? "修复" : screenTextNeedsUpdate ? "更新" : "下载";
  const screenTextSizeText = screenTextLayer && screenTextLayer.bytes > 0
    ? formatStorageBytes(screenTextLayer.bytes)
    : screenTextLayer && screenTextLayer.archive_bytes > 0
      ? `下载约 ${formatStorageBytes(screenTextLayer.archive_bytes)}`
      : "";
  const modelsMossSizeText = modelsMossLayer && modelsMossLayer.bytes > 0
    ? formatStorageBytes(modelsMossLayer.bytes)
    : modelsMossLayer && modelsMossLayer.archive_bytes > 0
      ? `下载约 ${formatStorageBytes(modelsMossLayer.archive_bytes)}`
      : "";

  return (
    <section className="runtime-layout">
      <article className="panel runtime-flat-panel">
        <Notice className="provision-message failed" message={provisionMessage} onDismiss={onDismissProvisionMessage} />

        {migrationActive && (
          <p className="provision-blocked" role="status">旧版升级正在后台更新运行环境，请查看页面顶部进度。</p>
        )}

        {!migrationActive && platformNotice !== "" && (
          <p className="provision-blocked" role="status">{platformNotice}</p>
        )}

        {!migrationActive && !showOnboardingCard && !connecting && connectionBlocked !== "" && (
          <p className={bootstrapBroken ? "provision-blocked failed" : "provision-blocked"} role="status">{connectionBlocked}</p>
        )}

        {/* 精简版环境初始化提示条（完整引导已移至独立配置向导页） */}
        {showOnboardingCard && (
          pythonInstalling && pythonBootstrap ? (
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
            <div className={`runtime-wizard-banner ${tightDisk ? "tight" : ""}`} role="status">
              <div className="runtime-wizard-copy">
                <strong>{baseInterrupted ? "基础运行环境安装未完成" : tightDisk ? "安装目录空间紧张，建议换盘后安装" : "基础运行环境尚未完整安装"}</strong>
                <span>
                  {baseInterrupted && job?.message
                    ? job.message
                    : pythonSupported
                      ? `工具层与 Python 环境层${baseDownloadBytes > 0 ? `约 ${formatStorageBytes(baseDownloadBytes)}` : ""}，可打开配置向导按使用方式一步步安装。`
                      : `工具层（FFmpeg、FFprobe 与 yt-dlp）${baseDownloadBytes > 0 ? `约 ${formatStorageBytes(baseDownloadBytes)}` : ""}，安装后即可导入视频、导出字幕与使用云端引擎。`}
                </span>
              </div>
              <div className="runtime-wizard-actions">
                {onOpenOnboarding && (
                  <button
                    type="button"
                    className="primary-button runtime-compact-btn"
                    onClick={onOpenOnboarding}
                  >
                    打开配置向导
                  </button>
                )}
                <button
                  type="button"
                  className="quiet-button runtime-compact-btn"
                  disabled={!runtimeLocation || storageBusy || relocatingRuntime || (baseTarget === "runtime" ? pythonLayerBlocked : toolsBlocked) !== ""}
                  onClick={() => void onInstall(baseTarget)}
                >
                  {baseInterrupted ? "继续安装" : "安装基础环境"}
                </button>
              </div>
            </div>
          )
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

        {!migrationActive && jobRunning && job && (
          <ProvisionProgress
            job={job}
            onCancel={onCancelInstall}
            onSwitchToOffline={activeDownloadLayer && onInstallOffline ? () => void onInstallOffline(activeDownloadLayer) : undefined}
          />
        )}
        {!migrationActive && !jobRunning && !showOnboardingCard && (
          <Notice
            className={`provision-message ${job?.state ?? ""}`}
            message={jobMessageDismissed ? "" : (job?.message ?? "")}
            onDismiss={onDismissJobMessage}
          />
        )}

        {/* 拍平的核心层列表：工具层、Python 环境层、AI 模型层 */}
        <div className="runtime-layer-list">
          <div className="runtime-layer-row">
            <div className="runtime-layer-info">
              <span className={toolsState === "ready" ? "layer-dot ready" : "layer-dot"} aria-hidden="true">{toolsState === "ready" ? "✓" : "↓"}</span>
              <strong>工具层</strong>
              <InfoTip
                text={pythonSupported
                  ? "默认安装，支持更新。包含 FFmpeg、FFprobe、tokcount 与在线视频下载工具（yt-dlp、aria2c、Node.js、PO Token 生成器）。"
                  : "包含 FFmpeg、FFprobe 与 yt-dlp，用于视频导入、转码导出、在线下载与云端转写。"}
                label="工具层"
              />
            </div>
            <div className="runtime-layer-actions">
              {toolsLayer && toolsLayer.bytes > 0 && <span className="runtime-layer-size">{formatStorageBytes(toolsLayer.bytes)}</span>}
              {!migrationActive && toolsSupported && pythonSupported && toolsState !== "ready" && onInstallOffline && (
                <button
                  type="button"
                  className="quiet-button runtime-compact-btn"
                  disabled={toolsBlocked !== ""}
                  title={offlineButtonTitle(toolsLayer, toolsBlocked)}
                  onClick={() => void onInstallOffline("tools")}
                >
                  离线安装…
                </button>
              )}
              {!migrationActive && toolsSupported && toolsState !== "ready" && (
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
              {!migrationActive && pythonSupported && pythonState !== "ready" && onInstallOffline && (
                <button
                  type="button"
                  className="quiet-button runtime-compact-btn"
                  disabled={pythonLayerBlocked !== ""}
                  title={offlineButtonTitle(pythonLayer, pythonLayerBlocked)}
                  onClick={() => void onInstallOffline("python")}
                >
                  离线安装…
                </button>
              )}
              {!migrationActive && pythonSupported && pythonState !== "ready" && (
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
              {!modelsReady && !migrationActive && pythonSupported && onInstallOffline && (
                <button
                  type="button"
                  className="quiet-button runtime-compact-btn"
                  disabled={modelsOfflineBlocked !== ""}
                  title={offlineButtonTitle(modelsLayer, modelsOfflineBlocked)}
                  onClick={() => void onInstallOffline("models")}
                >
                  离线安装…
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

          <div className="runtime-layer-row">
            <div className="runtime-layer-info">
              <span className={modelsMossReady ? "layer-dot ready" : "layer-dot"} aria-hidden="true">{modelsMossReady ? "✓" : "↓"}</span>
              <strong>MOSS 模型层</strong>
              <InfoTip text={mossModelTooltip(mossModels)} label="MOSS 模型层" />
            </div>
            <div className="runtime-layer-actions">
              {modelsMossSizeText && <span className="runtime-layer-size">{modelsMossSizeText}</span>}
              {modelsMossInstalled && !migrationActive && onRemoveModelsMoss && (
                <button
                  className={confirmRemoveModelsMoss ? "runtime-remove-button runtime-compact-btn confirming" : "runtime-remove-button runtime-compact-btn"}
                  disabled={removeModelsMossBlocked !== ""}
                  onBlur={() => setConfirmRemoveModelsMoss(false)}
                  onClick={() => {
                    if (!confirmRemoveModelsMoss) {
                      setConfirmRemoveModelsMoss(true);
                      return;
                    }
                    setConfirmRemoveModelsMoss(false);
                    void onRemoveModelsMoss();
                  }}
                  title={removeModelsMossBlocked || "仅删除 MOSS 模型层，基础环境与用户数据保持不变"}
                  type="button"
                >
                  {confirmRemoveModelsMoss ? "确认删除" : "删除模型"}
                </button>
              )}
              {!modelsMossReady && !migrationActive && pythonSupported && onInstallOffline && (
                <button
                  type="button"
                  className="quiet-button runtime-compact-btn"
                  disabled={modelsMossOfflineBlocked !== ""}
                  title={offlineButtonTitle(modelsMossLayer, modelsMossOfflineBlocked)}
                  onClick={() => void onInstallOffline("models-moss")}
                >
                  离线安装…
                </button>
              )}
              {!modelsMossReady && !migrationActive && (
                <button
                  className="primary-button runtime-compact-btn"
                  disabled={modelsMossBlocked !== ""}
                  onClick={() => void onInstall("models-moss")}
                  title={modelsMossBlocked || modelsMossActionLabel}
                  type="button"
                >
                  {modelsMossWorking ? "下载中…" : modelsMossActionLabel}
                </button>
              )}
              {(modelsMossWorking || !modelsMossReady) && (
                <Mark tone={modelsMossWorking ? "accent" : modelsMossNeedsUpdate || modelsMossNeedsRepair ? "caution" : "neutral"}>
                  {modelsMossWorking ? "处理中" : modelsMossNeedsUpdate ? "需更新" : modelsMossNeedsRepair ? "需修复" : "未安装"}
                </Mark>
              )}
            </div>
          </div>

          <div className="runtime-layer-row">
            <div className="runtime-layer-info">
              <span className={screenTextReady ? "layer-dot ready" : "layer-dot"} aria-hidden="true">{screenTextReady ? "✓" : "↓"}</span>
              <strong>文字识别层</strong>
              <InfoTip text={SCREEN_TEXT_TIP} label="文字识别层" />
            </div>
            <div className="runtime-layer-actions">
              {screenTextSizeText && <span className="runtime-layer-size">{screenTextSizeText}</span>}
              {screenTextInstalled && !migrationActive && onRemoveScreenText && (
                <button
                  className={confirmRemoveScreenText ? "runtime-remove-button runtime-compact-btn confirming" : "runtime-remove-button runtime-compact-btn"}
                  disabled={removeScreenTextBlocked !== ""}
                  onBlur={() => setConfirmRemoveScreenText(false)}
                  onClick={() => {
                    if (!confirmRemoveScreenText) {
                      setConfirmRemoveScreenText(true);
                      return;
                    }
                    setConfirmRemoveScreenText(false);
                    void onRemoveScreenText();
                  }}
                  title={removeScreenTextBlocked || "只删这个组件，字幕和设置不受影响"}
                  type="button"
                >
                  {confirmRemoveScreenText ? "确认删除" : "删除"}
                </button>
              )}
              {!screenTextReady && !migrationActive && screenTextAvailable && pythonSupported && onInstallOffline && (
                <button
                  type="button"
                  className="quiet-button runtime-compact-btn"
                  disabled={screenTextBlocked !== ""}
                  title={offlineButtonTitle(screenTextLayer, screenTextBlocked)}
                  onClick={() => void onInstallOffline("screen-text")}
                >
                  离线安装…
                </button>
              )}
              {!screenTextReady && !migrationActive && screenTextAvailable && (
                <button
                  className="primary-button runtime-compact-btn"
                  disabled={screenTextBlocked !== ""}
                  onClick={() => void onInstall("screen-text")}
                  title={screenTextBlocked || screenTextActionLabel}
                  type="button"
                >
                  {screenTextWorking ? "下载中…" : screenTextActionLabel}
                </button>
              )}
              {(screenTextWorking || !screenTextReady) && (
                <Mark tone={screenTextWorking ? "accent" : screenTextNeedsUpdate || screenTextNeedsRepair ? "caution" : "neutral"}>
                  {screenTextWorking ? "处理中" : !screenTextAvailable ? "暂未提供" : screenTextNeedsUpdate ? "需更新" : screenTextNeedsRepair ? "需修复" : "未安装"}
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
        {runtimeLocation && (
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
