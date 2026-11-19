import type { Snapshot as SidecarSnapshot } from "../../bindings/github.com/Ricori/nonoka-x/desktop/internal/sidecar/models.js";
import { useEffect, useState, type ReactNode } from "react";
import type { PythonBootstrapState, RuntimeCleanupItem, RuntimeCleanupPlan, RuntimeInstallTarget, RuntimeItem, RuntimeProvisionState } from "../bridge/runtime.ts";
import type { RelocationProgress, StorageDestination, StorageStatus, StorageTarget } from "../bridge/storage.ts";
import { InstallLocationCard } from "./InstallLocationCard.tsx";
import { installLocationView } from "./installLocation.ts";
import { cleanupBytes, cleanupView, layerLabel, layerUsage, type UsageOwner } from "./engineLayers.ts";
import { Mark } from "./Mark.tsx";
import type { Capabilities } from "../providers/types.ts";
import "./RuntimeSection.css";
import { Notice } from "./Notice.tsx";
import { formatStorageBytes } from "./StorageRelocation.tsx";

/** The settings page's 「运行环境」 section: what the runtime page was before WP6 folded it in. */
interface RuntimeSectionProps {
  capabilities: Capabilities | null;
  message: string;
  /** Failure of the last install/cancel/remove action, shown next to the
      controls that triggered it. */
  provisionMessage: string;
  onDismissProvisionMessage: () => void;
  provision: RuntimeProvisionState | null;
  pythonBootstrap: PythonBootstrapState | null;
  ready: boolean;
  sidecar: SidecarSnapshot | null;
  onInstall: (target: RuntimeInstallTarget) => Promise<void>;
  onInstallPython: () => Promise<void>;
  onCancelInstall: () => Promise<void>;
  onRemoveAll: () => Promise<void>;
  /** 「清理未使用的」：删掉没人用的旧版本，以及用户勾选的、当前引擎和纠错包都用不到的整层。 */
  onRemoveUnused: (layers: string[]) => Promise<void>;
  /** 现在清理会删什么（以当前转写引擎为准）；旧版 sidecar 读不到时为 null，只按旧版本列。 */
  cleanup: RuntimeCleanupPlan | null;
  /** 引擎列表里点了「下载所缺的层」：在这里走同一套安装流程（含安装位置确认）；`seq` 变了就再装一次。 */
  requestedInstall: { target: RuntimeInstallTarget; seq: number } | null;
  storage: StorageStatus | null;
  storageProgress: RelocationProgress | null;
  storageBusy: boolean;
  onChooseStorage: (target: StorageTarget) => Promise<StorageDestination | null>;
  onRelocateStorage: (target: StorageTarget, destination: string) => Promise<void>;
  onCancelStorage: () => Promise<void>;
}

// 会把十几 GB 拉下来的安装目标。装 yt-dlp 等小工具不值得为它弹位置提示。
const largeInstallTargets = new Set<RuntimeInstallTarget>(["all", "runtime", "models", "optional-models"]);

const installPhases = [
  { id: "downloading", label: "下载" },
  { id: "verifying", label: "校验" },
  { id: "extracting", label: "解压" },
  { id: "activating", label: "安装" },
] as const;

const assetStateLabels: Record<RuntimeItem["state"], string> = {
  missing: "尚未安装",
  downloading: "下载中",
  outdated: "需要更新",
  ready: "已就绪",
  failed: "安装失败",
};

const optionalToolIds = new Set(["yt-dlp", "aria2c", "node", "pot-provider"]);

// 这几样单独装没有意义：yt-dlp 下载，aria2c 提速，node 与 pot-provider 一起
// 提供 YouTube 要的 PO token。所以它们作为一件事呈现，也作为一件事安装。
const videoToolIds = new Set(["yt-dlp", "aria2c", "node", "pot-provider"]);

const diarizationIds: ReadonlySet<string> = new Set(["diarization"]);

const assetLabels: Record<string, string> = {
  python: "Python / CUDA",
  ffmpeg: "FFmpeg",
  ffprobe: "FFprobe",
  separator: "人声分离模型",
  whisper: "Whisper 转写模型",
  "qwen-referee": "Qwen 复核模型",
  nemotron3: "说话人活动模型",
  "speaker-embedding": "声纹模型",
  diarization: "说话人分离模型",
  "yt-dlp": "yt-dlp",
  tokcount: "tokcount",
  aria2c: "aria2c",
  node: "Node.js",
  "pot-provider": "PO Token Provider",
};

const compactAssetLabels: Record<string, string> = {
  diarization: "说话人分离",
};

const toolPurposes: Record<string, string> = {
  nemotron3: "说话人分离：判断谁在说话",
  "speaker-embedding": "说话人分离：按声音归人",
  "yt-dlp": "解析在线视频与字幕",
  aria2c: "多连接加速下载",
  node: "运行 JavaScript 挑战",
  "pot-provider": "生成 YouTube PO Token",
};

// 说话人分离的两个模型只能一起用、一起装，所以在「AI模型」里合成一项展示。
function combineDiarization(models: RuntimeItem[]): RuntimeItem | null {
  if (!models.length) return null;
  const states = models.map((item) => item.state);
  const state: RuntimeItem["state"] = states.includes("downloading") ? "downloading"
    : states.includes("failed") ? "failed"
      : states.includes("outdated") ? "outdated"
        : states.every((value) => value === "ready") ? "ready"
          : "missing";
  const detail = models.map((item) => `${assetLabels[item.id] ?? item.id}：${assetStateLabels[item.state] ?? item.state}`).join("；");
  return { id: "diarization", state, detail };
}

const stageOrder: Record<string, number> = {
  preparing: 0,
  resource: 0,
  downloading: 0,
  verifying: 1,
  extracting: 2,
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
      : job.stage === "verifying" || job.stage === "extracting"
        ? job.message
        : "正在准备下载";
  return (
    <section className="provision-progress-card" role="status" aria-live="polite">
      <div className="provision-progress-heading">
        <div className="download-pulse" aria-hidden="true"><span>↓</span></div>
        <div>
          <small>{job.resource ? `正在处理 ${job.resource.toUpperCase()}` : "正在准备运行环境"}</small>
          <strong>{job.message || "正在准备下载"}</strong>
        </div>
        <b>{progress?.total ? `${Math.round(percentage)}%` : "处理中"}</b>
        {cancellable && <button className="cancel-button" onClick={() => void onCancel()} type="button">取消</button>}
      </div>
      {job.proxy && <p className="provision-proxy">已检测到{job.proxy}，所有下载将通过该代理进行</p>}
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

function AssetTile({ item, optional = false, action }: {
  item: RuntimeItem;
  optional?: boolean;
  action?: ReactNode;
}) {
  const ready = item.state === "ready";
  const stateLabel = optional && !ready
    ? item.state === "downloading" ? "下载中" : item.state === "outdated" ? "可按需更新" : "按需安装"
    : ready && item.source === "system" ? "系统已安装" : assetStateLabels[item.state] ?? item.state;
  const purpose = toolPurposes[item.id];
  const label = assetLabels[item.id] ?? item.id;
  // 未装好的按需项自带按钮时，按钮已说明状态；只留短名，给按钮腾地方。
  const compact = optional && !ready && Boolean(action);
  const shownLabel = compact ? compactAssetLabels[item.id] ?? label : label;
  const title = [label, stateLabel, item.version, purpose, item.detail].filter(Boolean).join(" · ");
  return (
    <div className={[optional && "optional-asset-tile", action && "has-action"].filter(Boolean).join(" ") || undefined} title={title || undefined} aria-label={`${label}：${stateLabel}${purpose ? `，${purpose}` : ""}`}>
      <span className={ready ? "asset-ready" : optional ? "asset-optional" : "asset-missing"}>{ready ? "✓" : optional ? "○" : "↓"}</span>
      <div>
        <span className="asset-title-row">
          <strong>{shownLabel}</strong>
          {!compact && <small className="asset-state">{stateLabel}</small>}
        </span>
        {purpose && <small className="asset-purpose">{purpose}</small>}
      </div>
      {action}
    </div>
  );
}

function AssetGroup({ title, items, optional = false, optionalIds, itemActions, target, installing, blocked, onInstall }: {
  title: string;
  items: RuntimeItem[];
  optional?: boolean;
  /** 混排分组里按需安装的那几项。 */
  optionalIds?: ReadonlySet<string>;
  /** 自带安装按钮的项；分组按钮不管它们。 */
  itemActions?: Record<string, ReactNode>;
  target: RuntimeInstallTarget;
  installing: boolean;
  blocked: string;
  onInstall: (target: RuntimeInstallTarget) => Promise<void>;
}) {
  const readyCount = items.filter((item) => item.state === "ready").length;
  const groupItems = items.filter((item) => !itemActions?.[item.id]);
  const allReady = groupItems.length > 0 && groupItems.every((item) => item.state === "ready");
  // 这个版本要的是另一个版本的层：动作是「更新」（整层替换），不是「下载缺失项」。
  const needsUpdate = groupItems.some((item) => item.state === "outdated");
  // 层与清单不符（文件缺了或被改过）：重新安装这一层就是修复。
  const needsRepair = groupItems.some((item) => item.state === "failed");
  const disabledReason = blocked
    || (!items.length ? "当前平台没有可安装的项目。" : "")
    || (allReady ? "该分组已全部安装并通过校验。" : "");
  return (
    <section className={`asset-group categorized ${optional ? "optional" : "required"}`}>
      <div className="asset-group-heading">
        <div className="asset-group-title">
          <strong>{title}</strong>
          <small>{readyCount}/{items.length} 已就绪</small>
        </div>
        <div className="asset-group-actions">
          <button
            className="quiet-button asset-group-action"
            disabled={disabledReason !== ""}
            onClick={() => void onInstall(target)}
            title={disabledReason || (needsRepair ? `重新安装「${title}」所在的运行环境层` : needsUpdate ? `这个版本需要新的运行环境层，更新时整层重新下载` : `下载并安装「${title}」所在的运行环境层`)}
            type="button"
          >
            {installing ? needsRepair ? "修复中…" : needsUpdate ? "更新中…" : "安装中…" : allReady ? "已安装" : needsRepair ? "修复" : needsUpdate ? "更新" : "下载缺失项"}
          </button>
        </div>
      </div>
      <div className="asset-grid">
        {items.map((item) => <AssetTile item={item} key={`${item.id}:${item.version ?? ""}`} optional={optional || optionalIds?.has(item.id)} action={itemActions?.[item.id]} />)}
        {!items.length && <p className="asset-group-empty">当前平台没有可用项目</p>}
      </div>
    </section>
  );
}

function DiarizationAction({ item, installing, blocked, onInstall }: {
  item: RuntimeItem;
  installing: boolean;
  blocked: string;
  onInstall: () => void;
}) {
  // 已就绪时卡片上的「已就绪」标签就够了，不再放一个禁用的按钮。
  if (item.state === "ready" && !installing) return null;
  const needsUpdate = item.state === "outdated";
  return (
    <button
      className="quiet-button asset-tile-action"
      disabled={blocked !== ""}
      onClick={onInstall}
      title={blocked || (needsUpdate ? "更新说话人分离模型" : "下载说话人分离模型")}
      type="button"
    >
      {installing ? needsUpdate ? "更新中…" : "下载中…" : needsUpdate ? "更新" : "下载"}
    </button>
  );
}


/** 按引擎列出占用：本机能跑的转写引擎，加纠错引擎（云端 FineSub 不占本机的层）。 */
function usageOwners(capabilities: Capabilities | null): UsageOwner[] {
  const engines = (capabilities?.transcription_engines ?? [])
    .filter((item) => item.kind !== "cloud-service")
    .map((item) => ({ id: item.id, label: item.label, layers: item.layers }));
  const correction = capabilities?.correction_engines?.[0];
  return correction ? [...engines, { id: `correction:${correction.id}`, label: "finesub correction（纠错与翻译）", layers: correction.layers }] : engines;
}

export function RuntimeSection({ capabilities, message, provisionMessage, provision, pythonBootstrap, ready, sidecar, onCancelInstall, onDismissProvisionMessage, onInstall, onInstallPython, onRemoveAll, onRemoveUnused, cleanup, requestedInstall, storage, storageProgress, storageBusy, onChooseStorage, onRelocateStorage, onCancelStorage }: RuntimeSectionProps) {
  const [confirmRemove, setConfirmRemove] = useState(false);
  const [confirmUnused, setConfirmUnused] = useState(false);
  // 用户勾选、要整层删掉的层；默认一个都不勾。
  const [pickedCleanup, setPickedCleanup] = useState<string[]>([]);
  // 用户点了大安装但还没确认位置时，先停在这里等确认。
  const [awaitingLocation, setAwaitingLocation] = useState<RuntimeInstallTarget | null>(null);
  const runtimeLocation = (storage?.locations ?? []).find((item) => item.target === "runtime") ?? null;
  const locationView = installLocationView(runtimeLocation, awaitingLocation !== null);
  const requestInstall = async (target: RuntimeInstallTarget) => {
    if (locationView.prompt && largeInstallTargets.has(target)) {
      setAwaitingLocation(target);
      return;
    }
    await onInstall(target);
  };
  // 引擎列表发来的安装请求：同一套位置确认与进度。只认新的 seq，重渲染不会重复安装。
  const [handledInstall, setHandledInstall] = useState(0);
  useEffect(() => {
    if (!requestedInstall || requestedInstall.seq === handledInstall) return;
    setHandledInstall(requestedInstall.seq);
    void requestInstall(requestedInstall.target);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [requestedInstall]);
  const issues = capabilities?.runtime?.issues ?? [];
  // 不阻断运行、但会改变耗时的情况（现在只有「没 N 卡，走 CPU」）
  const warnings = capabilities?.runtime?.warnings ?? [];
  const stages = capabilities?.runtime?.stages ?? [];
  const job = provision?.job;
  const pythonInstalling = pythonBootstrap?.state === "running";
  const pythonNeedsRepair = Boolean(pythonBootstrap?.python) && (pythonInstalling || pythonBootstrap?.state === "failed");
  // A sidecar that cannot build its layer manager (no readable layers.json, or
  // an interpreter missing the launcher's dependencies) answers requests but
  // cannot install anything. The card is the way out of the interpreter half of
  // that, so a running sidecar must not hide it.
  const bootstrapBroken = (provision?.bootstrap_error ?? "") !== "";
  const showPythonCard = !sidecar?.running || pythonInstalling || bootstrapBroken || pythonBootstrap?.state === "failed";
  const resources = provision?.resources ?? [];
  const optionalTools = resources.filter((item) => optionalToolIds.has(item.id));
  const videoTools = optionalTools.filter((item) => videoToolIds.has(item.id));
  const requiredRuntimeAssets = [
    ...(provision?.runtime_supported && provision.runtime ? [provision.runtime] : []),
    ...resources.filter((item) => !optionalToolIds.has(item.id)),
  ];
  const requiredModels = provision?.models ?? [];
  const diarization = combineDiarization(provision?.optional_models ?? []);
  const aiModels = diarization ? [...requiredModels, diarization] : requiredModels;
  const runtimeTarget: RuntimeInstallTarget = provision?.runtime_supported ? "runtime" : "media";
  // Every control below is gated on provision state. Without a reason attached
  // the panel greys out silently — on a first launch, or whenever the sidecar
  // cannot build its provisioner, that reads as a dead page.
  const jobRunning = job?.state === "running";
  const connectionBlocked = !provision
    ? pythonInstalling
      ? "正在准备 Python 启动环境，完成后即可安装运行时与模型。"
      // 只在服务确实没起来时才怪 Python：服务在跑而 provision 读不到，是另一回事。
      : sidecar?.running || !pythonBootstrap || pythonBootstrap.state === "ready"
        ? "本地执行服务尚未连接，无法读取运行时状态。等待服务启动，或点击右上角「重新检查」。"
        : pythonBootstrap.supported
          ? "本地执行服务尚未连接：请先用下方「Python 3.12」卡片准备启动环境。"
          // 平台没有自动安装，按钮点不动，所以这里必须给出手动出路，否则整页无解。
          : `本地执行服务尚未连接：${pythonBootstrap.platform} 暂不支持自动安装 Python。可自行准备一个 Python 3.12 环境并安装 httpx、pydantic，用环境变量 NONOKA_PYTHON 指向它后重启应用。`
    : provision.bootstrap_error
      ? `运行环境层不可用，暂时无法安装运行时或模型：${provision.bootstrap_error}。如果本地服务运行在缺少依赖的 Python 上，可用下方「Python 3.12」卡片安装隔离的启动环境后重试。`
      : "";
  // 本地不支持 ≠ 用不了：云端不依赖这里的任何运行时与模型，所以这条提示要把出路说清楚，
  // 而不是只报告「不支持」。
  const platformNotice = provision && !provision.runtime_supported
    ? `本地 GPU 流水线仅面向 Windows x64/NVIDIA，${provision.platform} 上${provision.media_supported ? "只能安装工具层（FFmpeg 等）" : "暂时没有预制的运行环境层，本地转写与托管 FFmpeg 都不可用"}。在此平台请在首页把运行方式切到「云端」——云端转写不需要本地运行时与模型，只需登录账号。`
    : "";
  const jobBlocked = jobRunning ? "已有安装任务正在进行，请等待其结束。" : "";
  const runtimeUnavailable = provision && !provision.runtime_supported && !provision.media_supported
    ? `托管运行时暂不支持 ${provision.platform}。`
    : "";
  const runtimeBlocked = connectionBlocked || runtimeUnavailable || jobBlocked;
  const requiredRuntimeReady = requiredRuntimeAssets.length > 0 && requiredRuntimeAssets.every((item) => item.state === "ready");
  const allBlocked = connectionBlocked
    || (provision && !provision.runtime_supported ? `本地 GPU 环境暂不支持 ${provision.platform}。` : "")
    || jobBlocked
    || (requiredRuntimeReady ? "必备运行时已齐全。" : "");
  const modelsBlocked = connectionBlocked
    || (provision && !provision.runtime_supported ? `本地模型暂不支持 ${provision.platform}。` : "")
    || jobBlocked
    || (provision && provision.runtime.state !== "ready" ? "需要先完成「必备运行时」。" : "");
  const toolsBlocked = connectionBlocked
    || (provision && !provision.runtime_supported ? `托管可选工具暂不支持 ${provision.platform}。` : "")
    || jobBlocked;
  // 位置卡上的「开始安装」用它对应目标的门槛，包括改完位置后 sidecar 重启的那几秒。
  const awaitingBlocked = awaitingLocation === "models" ? modelsBlocked : awaitingLocation === "runtime" ? runtimeBlocked : allBlocked;
  // 「完整核对」逐文件比 sha256，只对已经装上的层有意义。
  const installedLayers = (provision?.layers ?? []).filter((layer) => layer.current !== null);
  const repairBlocked = connectionBlocked || jobBlocked
    || (provision && provision.layers && installedLayers.length === 0 ? "还没有安装任何运行环境层。" : "");
  // 读得到清理计划就按它；旧版 sidecar 只有旧版本这一种。默认只删旧版本，
  // 当前引擎用不到的整层要用户逐层勾选才删（换回要它的引擎得重新下载）。
  const cleanupItems: RuntimeCleanupItem[] = cleanup?.items
    ?? (provision?.unused_layers ?? []).map((item) => ({ ...item, reason: "old-version" as const, blocked: "" }));
  const owners = usageOwners(capabilities);
  const cleanupChoice = cleanupView(cleanupItems, provision, owners);
  const pickedLayers = cleanupChoice.layers.filter((item) => pickedCleanup.includes(item.layer) && !item.blocked).map((item) => item.layer);
  const cleanupTotal = cleanupBytes(cleanupChoice, pickedLayers);
  const unusedBlocked = connectionBlocked || jobBlocked
    || (cleanupChoice.removable.length === 0 && pickedLayers.length === 0
      ? cleanupChoice.held.length > 0 ? "旧版本都有未结束的任务在用，等任务结束后再清理。" : "没有旧版本可删；要删整层请先勾选下面的层。"
      : "");
  const cleanupNote = [
    cleanupChoice.removable.length > 0 ? `${cleanupChoice.removable.length} 个旧版本可删，腾出 ${formatStorageBytes(cleanupChoice.oldBytes)}` : "没有可删的旧版本",
    cleanupChoice.held.length > 0 ? `${cleanupChoice.held.length} 个旧版本有未结束的任务在用，暂不删` : "",
    cleanupChoice.layers.length > 0 ? "当前引擎用不到的整层默认不删，需要时在下面单独勾选" : "",
    cleanup && !cleanup.engine_available ? "当前转写引擎不可用，只清理旧版本" : "",
  ].filter(Boolean).join("；");
  const usage = layerUsage(owners, provision);
  const pythonBlocked = !pythonBootstrap
    ? ""
    : !pythonBootstrap.supported
      ? pythonBootstrap.message || `${pythonBootstrap.platform} 暂不支持自动安装 Python。`
      : pythonInstalling ? "Python 正在安装中。" : "";
  return (
    <section className="runtime-layout">
      <article className="panel runtime-diagnostics">
        <div className="panel-heading">
          <div><h2>环境诊断</h2></div>
          <Mark>{ready ? "环境已就绪" : sidecar?.running ? "需要处理" : "服务未连接"}</Mark>
        </div>
        {issues.length ? (
          <ul className="issues">
            {issues.map((issue) => <li key={`${issue.code}:${issue.message}`}><strong>{issue.code}</strong><span>{issue.message}</span></li>)}
          </ul>
        ) : !ready && <p className="success-copy">{message || "等待环境状态。"}</p>}
        {warnings.length > 0 && (
          <ul className="issues advisory">
            {warnings.map((warning) => <li key={`${warning.code}:${warning.message}`}><strong>{warning.code}</strong><span>{warning.message}</span></li>)}
          </ul>
        )}
        <div className="diagnostic-section-heading">
          <strong>运行环节</strong>
          <small>{stages.filter((stage) => stage.ready).length} / {stages.length} 可用</small>
        </div>
        <div className="stage-list">
          {stages.map((stage) => (
            <div className={stage.ready ? "stage-ready" : "stage-blocked"} key={stage.id} title={stage.ready ? undefined : stage.issues[0]?.message}>
              <span>{stage.ready ? "✓" : "!"}</span>
              <div><strong>{stage.label}</strong></div>
            </div>
          ))}
          {!stages.length && <p className="success-copy">连接桌面执行服务后，将分别检测媒体准备、ASR、LLM、知识库和视频多模态环节。</p>}
        </div>
      </article>
      <article className="panel provision-panel">
        <div className="panel-heading">
          <div><h2>运行时与模型</h2></div>
          <div className="provision-actions">
            <button
              className={confirmRemove ? "runtime-remove-button confirming" : "runtime-remove-button"}
              disabled={!provision?.root || jobRunning}
              onBlur={() => setConfirmRemove(false)}
              onClick={() => {
                if (!confirmRemove) {
                  setConfirmRemove(true);
                  return;
                }
                setConfirmRemove(false);
                void onRemoveAll();
              }}
              title={jobRunning ? "请先取消或等待当前安装任务结束。" : "删除全部运行环境层（Python、模型、工具）与下载缓存；不会删除任务、字幕和用户设置。"}
              type="button"
            >
              {confirmRemove ? "确认删除环境" : "删除环境"}
            </button>
            <button
              className="quiet-button"
              disabled={repairBlocked !== ""}
              onClick={() => void onInstall("repair")}
              title={repairBlocked || "逐个文件核对已安装的运行环境层；与清单不符的层整层重新安装"}
              type="button"
            >
              {jobRunning && job?.target === "repair" ? "核对中…" : "完整核对"}
            </button>
            <button className="primary-button" disabled={allBlocked !== ""} onClick={() => void requestInstall("all")} title={allBlocked || "安装必备运行时，并下载全部缺失模型"}>一键准备全部</button>
          </div>
        </div>
        <Notice className="provision-message failed" message={provisionMessage} autoDismissMs={0} onDismiss={onDismissProvisionMessage} />
        {(connectionBlocked || platformNotice) !== "" && (
          <p className={provision?.bootstrap_error ? "provision-blocked failed" : "provision-blocked"} role="status">{connectionBlocked || platformNotice}</p>
        )}
        {showPythonCard && <section className={`required-dependency-card ${pythonBootstrap?.state ?? "missing"}`}>
          <div className="required-dependency-main">
            <div className="required-dependency-icon" aria-hidden="true">PY</div>
            <div className="required-dependency-copy">
              <span>启动依赖 · AUTOMATIC</span>
              <h3>Python 3.12</h3>
              <p>{pythonBootstrap?.state === "failed" && !pythonInstalling
                ? pythonBootstrap.message
                : bootstrapBroken && !pythonInstalling
                  ? "当前本地服务运行在版本不符或缺少依赖的系统 Python 上。安装隔离的 Python 3.12 后会自动重启服务。"
                  : pythonBootstrap?.message || "Nonoka Sub X 将自动下载隔离的 Python，不会修改系统 Python。"}</p>
              {pythonBootstrap && !pythonBootstrap.supported && (
                <p className="required-dependency-hint">手动方案：准备一个装有 httpx、pydantic 的 Python 3.12，把环境变量 <code>NONOKA_PYTHON</code> 指向它的可执行文件后重启应用。</p>
              )}
            </div>
            <div className="required-dependency-action">
              <small>{pythonInstalling ? `↓ 正在${pythonNeedsRepair ? "修复" : "安装"}` : pythonBootstrap?.state === "failed" ? `! ${pythonNeedsRepair ? "环境损坏" : "安装失败"}` : pythonBootstrap?.state === "ready" ? "✓ 已安装" : "! 尚未安装"}</small>
              <button className="primary-button" disabled={pythonBlocked !== ""} title={pythonBlocked || undefined} onClick={() => void onInstallPython()}>
                {pythonInstalling ? `正在${pythonNeedsRepair ? "修复" : "启用"}…` : pythonBootstrap?.state === "failed" ? pythonNeedsRepair ? "修复环境" : "重试安装" : pythonBootstrap?.state === "ready" ? "重新启用" : "立即安装"}
              </button>
            </div>
          </div>
          {pythonInstalling && pythonBootstrap && <ProvisionProgress job={{
            resource: "Python 3.12",
            stage: pythonBootstrap.stage,
            message: pythonBootstrap.message,
            progress: pythonBootstrap.progress,
            proxy: pythonBootstrap.proxy,
          }} onCancel={async () => undefined} cancellable={false} />}
        </section>}
        {locationView.visible && runtimeLocation && (
          <InstallLocationCard
            location={runtimeLocation}
            view={locationView}
            progress={storageProgress}
            busy={storageBusy}
            awaitingInstall={awaitingLocation !== null}
            installBlocked={awaitingBlocked || ""}
            onChoose={onChooseStorage}
            onRelocate={onRelocateStorage}
            onCancelRelocation={onCancelStorage}
            onProceed={() => {
              const target = awaitingLocation;
              setAwaitingLocation(null);
              if (target) void onInstall(target);
            }}
            onDismiss={() => setAwaitingLocation(null)}
          />
        )}
        {job?.state === "running" && <ProvisionProgress job={job} onCancel={onCancelInstall} />}
        {job?.state !== "running" && <Notice className={`provision-message ${job?.state ?? ""}`} message={job?.message ?? ""} />}
        <div className="asset-groups">
          <AssetGroup
            title="必备运行时"
            items={requiredRuntimeAssets}
            target={runtimeTarget}
            installing={jobRunning && job?.target === runtimeTarget}
            blocked={runtimeBlocked}
            onInstall={requestInstall}
          />
          <AssetGroup
            title="AI模型"
            items={aiModels}
            optionalIds={diarizationIds}
            itemActions={diarization ? { diarization: (
              <DiarizationAction
                item={diarization}
                installing={jobRunning && job?.target === "optional-models"}
                blocked={modelsBlocked}
                onInstall={() => void requestInstall("optional-models")}
              />
            ) } : undefined}
            target="models"
            installing={jobRunning && job?.target === "models"}
            blocked={modelsBlocked}
            onInstall={requestInstall}
          />
          <AssetGroup
            title="视频下载工具"
            items={videoTools}
            optional
            target="video-tools"
            installing={jobRunning && job?.target === "video-tools"}
            blocked={toolsBlocked}
            onInstall={requestInstall}
          />
        </div>
        {usage && usage.total > 0 && <section className="runtime-usage" aria-labelledby="runtime-usage-title">
          <div className="runtime-unused-heading">
            <div>
              <strong id="runtime-usage-title">按引擎占用</strong>
              <small>{`已装的层共 ${formatStorageBytes(usage.total)}；共用的层只算一次`}</small>
            </div>
          </div>
          <ul>
            {usage.rows.map((row) => (
              <li key={row.id}>
                <span>{row.label}</span>
                <small>{row.layers.length
                  ? row.layers.map((layer) => layer.sharedWith.length ? `${layer.label}（与 ${layer.sharedWith.join("、")} 共用）` : layer.label).join("、")
                  : "不占运行环境层"}</small>
                <b>{formatStorageBytes(row.bytes)}</b>
              </li>
            ))}
          </ul>
        </section>}
        {cleanupItems.length > 0 && <section className="runtime-unused" aria-labelledby="runtime-unused-title">
          <div className="runtime-unused-heading">
            <div>
              <strong id="runtime-unused-title">清理未使用的</strong>
              <small>{cleanupNote}</small>
            </div>
            <button
              className={confirmUnused ? "runtime-remove-button confirming" : "quiet-button"}
              disabled={unusedBlocked !== ""}
              onBlur={() => setConfirmUnused(false)}
              onClick={() => {
                if (!confirmUnused) {
                  setConfirmUnused(true);
                  return;
                }
                setConfirmUnused(false);
                const layers = pickedLayers;
                setPickedCleanup([]);
                void onRemoveUnused(layers);
              }}
              title={unusedBlocked || (pickedLayers.length
                ? `删除旧版本，并整层删除 ${pickedLayers.map(layerLabel).join("、")}；换回要它的引擎时需要重新下载`
                : "删除没人用的旧版本；当前引擎用不到的整层不删，除非在下面勾选")}
              type="button"
            >
              {confirmUnused ? `确认删除，腾出 ${formatStorageBytes(cleanupTotal)}` : "清理未使用的"}
            </button>
          </div>
          {(cleanupChoice.removable.length > 0 || cleanupChoice.held.length > 0) && <ul>
            {[...cleanupChoice.removable, ...cleanupChoice.held].map((item) => (
              <li key={item.path} title={item.path} className={item.blocked ? "held" : undefined}>
                <span>{layerLabel(item.layer)}</span>
                <small>{item.blocked ? `旧版本 ${item.name} · ${item.blocked}，不删` : `旧版本 ${item.name}`}</small>
                <b>{formatStorageBytes(item.bytes)}</b>
              </li>
            ))}
          </ul>}
          {cleanupChoice.layers.length > 0 && <div className="runtime-unused-layers">
            <small>当前转写引擎与纠错都用不到的层，默认保留；勾选的才会整层删除：</small>
            {cleanupChoice.layers.map((item) => {
              const picked = pickedLayers.includes(item.layer);
              const backTo = item.neededBy.length ? item.neededBy.join("、") : "要它的引擎";
              const redownload = item.downloadBytes > 0 ? `约 ${formatStorageBytes(item.downloadBytes)}` : "这一层";
              return (
                <label key={item.layer} className={item.blocked ? "held" : undefined}>
                  <input
                    type="checkbox"
                    checked={picked}
                    disabled={item.blocked !== ""}
                    onChange={(event) => {
                      setConfirmUnused(false);
                      setPickedCleanup((current) => event.target.checked
                        ? [...current.filter((layer) => layer !== item.layer), item.layer]
                        : current.filter((layer) => layer !== item.layer));
                    }}
                  />
                  <span>
                    <strong>{`同时删除${item.label}（占用 ${formatStorageBytes(item.bytes)}）`}</strong>
                    <small>{item.blocked
                      ? `${item.blocked}，不能删`
                      : `换回${backTo}时要重新下载${redownload}`}</small>
                  </span>
                </label>
              );
            })}
          </div>}
        </section>}
        {provision?.root && <div className="runtime-storage-row">
          <small className="runtime-root">安装目录：{provision.root}</small>
        </div>}
      </article>
    </section>
  );
}
