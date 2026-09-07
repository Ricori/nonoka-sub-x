import type { Snapshot as SidecarSnapshot } from "../../bindings/github.com/Ricori/nonoka-x/desktop/internal/sidecar/models.js";
import { useState } from "react";
import type { PythonBootstrapState, RuntimeInstallTarget, RuntimeItem, RuntimeProvisionState, RuntimeToolGroup } from "../bridge/runtime.ts";
import type { RelocationProgress, StorageDestination, StorageStatus, StorageTarget } from "../bridge/storage.ts";
import { InstallLocationCard } from "../components/InstallLocationCard.tsx";
import { installLocationView } from "../components/installLocation.ts";
import { Mark } from "../components/Mark.tsx";
import type { Capabilities } from "../providers/types.ts";
import "./RuntimePage.css";
import { Notice } from "../components/Notice.tsx";

interface RuntimePageProps {
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
  onRemoveGroup: (target: RuntimeToolGroup) => Promise<void>;
  storage: StorageStatus | null;
  storageProgress: RelocationProgress | null;
  storageBusy: boolean;
  onChooseStorage: (target: StorageTarget) => Promise<StorageDestination | null>;
  onRelocateStorage: (target: StorageTarget, destination: string) => Promise<void>;
  onCancelStorage: () => Promise<void>;
}

// 会把十几 GB 拉下来的安装目标。装小工具（git、yt-dlp）不值得为它弹位置提示。
const largeInstallTargets = new Set<RuntimeInstallTarget>(["all", "runtime", "models"]);

const installPhases = [
  { id: "downloading", label: "下载" },
  { id: "verifying", label: "校验" },
  { id: "extracting", label: "解压" },
  { id: "activating", label: "安装" },
] as const;

const assetStateLabels: Record<RuntimeItem["state"], string> = {
  missing: "尚未安装",
  downloading: "下载中",
  outdated: "版本过旧",
  ready: "已就绪",
  failed: "安装失败",
};

const optionalToolIds = new Set(["git", "yt-dlp", "tokcount", "aria2c", "node", "pot-provider"]);

// 这几样单独装没有意义：yt-dlp 下载，aria2c 提速，node 与 pot-provider 一起
// 提供 YouTube 要的 PO token。所以它们作为一件事呈现，也作为一件事安装。
const videoToolIds = new Set(["yt-dlp", "aria2c", "node", "pot-provider"]);

const assetLabels: Record<string, string> = {
  python: "Python / CUDA",
  uv: "uv",
  ffmpeg: "FFmpeg",
  ffprobe: "FFprobe",
  separator: "人声分离模型",
  whisper: "Whisper 转写模型",
  "qwen-referee": "Qwen 复核模型",
  git: "Git",
  "yt-dlp": "yt-dlp",
  tokcount: "tokcount",
  aria2c: "aria2c",
  node: "Node.js",
  "pot-provider": "PO Token Provider",
};

const toolPurposes: Record<string, string> = {
  git: "克隆与更新知识库",
  tokcount: "本地精确统计 Token",
  "yt-dlp": "解析在线视频与字幕",
  aria2c: "多连接加速下载",
  node: "运行 JavaScript 挑战",
  "pot-provider": "生成 YouTube PO Token",
};

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

function AssetTile({ item, optional = false }: {
  item: RuntimeItem;
  optional?: boolean;
}) {
  const ready = item.state === "ready";
  const stateLabel = optional && !ready
    ? item.state === "downloading" ? "下载中" : item.state === "outdated" ? "可按需更新" : "按需安装"
    : ready && item.source === "system" ? "系统已安装" : assetStateLabels[item.state] ?? item.state;
  const purpose = toolPurposes[item.id];
  const label = assetLabels[item.id] ?? item.id;
  const title = [label, stateLabel, item.version, purpose, item.detail].filter(Boolean).join(" · ");
  return (
    <div className={optional ? "optional-asset-tile" : undefined} title={title || undefined} aria-label={`${label}：${stateLabel}${purpose ? `，${purpose}` : ""}`}>
      <span className={ready ? "asset-ready" : optional ? "asset-optional" : "asset-missing"}>{ready ? "✓" : optional ? "○" : "↓"}</span>
      <div>
        <span className="asset-title-row">
          <strong>{label}</strong>
          <small className="asset-state">{stateLabel}</small>
        </span>
        {purpose && <small className="asset-purpose">{purpose}</small>}
      </div>
    </div>
  );
}

function AssetGroup({ title, items, optional = false, target, installing, blocked, removable = false, confirmingRemove = false, onInstall, onRemove, onCancelRemove }: {
  title: string;
  items: RuntimeItem[];
  optional?: boolean;
  target: RuntimeInstallTarget;
  installing: boolean;
  blocked: string;
  removable?: boolean;
  confirmingRemove?: boolean;
  onInstall: (target: RuntimeInstallTarget) => Promise<void>;
  onRemove?: () => void;
  onCancelRemove?: () => void;
}) {
  const readyCount = items.filter((item) => item.state === "ready").length;
  const allReady = items.length > 0 && readyCount === items.length;
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
          {(!allReady || !removable) && (
            <button
              className="quiet-button asset-group-action"
              disabled={disabledReason !== ""}
              onClick={() => void onInstall(target)}
              title={disabledReason || `下载并安装「${title}」中的缺失项`}
              type="button"
            >
              {installing ? "安装中…" : allReady ? "已安装" : "下载缺失项"}
            </button>
          )}
          {removable && onRemove && (
            <button
              className={`asset-group-remove${allReady ? " installed" : ""}${confirmingRemove ? " confirming" : ""}`}
              disabled={blocked !== ""}
              onBlur={onCancelRemove}
              onClick={onRemove}
              title={blocked || `卸载「${title}」中的所有托管工具`}
              aria-label={confirmingRemove ? `确认卸载「${title}」` : `卸载「${title}」`}
              type="button"
            >
              {confirmingRemove ? "确认卸载" : allReady ? (
                <>
                  <span className="remove-idle-label">已安装</span>
                  <span className="remove-hover-label">卸载整组</span>
                </>
              ) : "卸载整组"}
            </button>
          )}
        </div>
      </div>
      <div className="asset-grid">
        {items.map((item) => <AssetTile item={item} key={`${item.id}:${item.version ?? ""}`} optional={optional} />)}
        {!items.length && <p className="asset-group-empty">当前平台没有可用项目</p>}
      </div>
    </section>
  );
}

export function RuntimePage({ capabilities, message, provisionMessage, provision, pythonBootstrap, ready, sidecar, onCancelInstall, onDismissProvisionMessage, onInstall, onInstallPython, onRemoveAll, onRemoveGroup, storage, storageProgress, storageBusy, onChooseStorage, onRelocateStorage, onCancelStorage }: RuntimePageProps) {
  const [confirmRemove, setConfirmRemove] = useState(false);
  const [confirmRemoveGroup, setConfirmRemoveGroup] = useState<RuntimeToolGroup | null>(null);
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
  const issues = capabilities?.runtime?.issues ?? [];
  const stages = capabilities?.runtime?.stages ?? [];
  const job = provision?.job;
  const pythonInstalling = pythonBootstrap?.state === "running";
  const pythonNeedsRepair = Boolean(pythonBootstrap?.python) && (pythonInstalling || pythonBootstrap?.state === "failed");
  // A sidecar started on an interpreter without FineSub's bootstrap dependencies
  // answers requests but cannot build its provisioner. The card is the way out
  // of that state, so a running sidecar must not hide it.
  const bootstrapBroken = (provision?.bootstrap_error ?? "") !== "";
  const showPythonCard = !sidecar?.running || pythonInstalling || bootstrapBroken || pythonBootstrap?.state === "failed";
  const resources = provision?.resources ?? [];
  const optionalTools = resources.filter((item) => optionalToolIds.has(item.id));
  const videoTools = optionalTools.filter((item) => videoToolIds.has(item.id));
  const otherTools = optionalTools.filter((item) => !videoToolIds.has(item.id));
  const requiredRuntimeAssets = [
    ...(provision?.runtime_supported && provision.runtime ? [provision.runtime] : []),
    ...resources.filter((item) => !optionalToolIds.has(item.id)),
  ];
  const requiredModels = provision?.models ?? [];
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
      ? `FineSub bootstrap 不可用，暂时无法安装运行时或模型：${provision.bootstrap_error}。可用下方「Python 3.12」卡片安装隔离的启动环境后重试。`
      : "";
  // 本地不支持 ≠ 用不了：云端不依赖这里的任何运行时与模型，所以这条提示要把出路说清楚，
  // 而不是只报告「不支持」。
  const platformNotice = provision && !provision.runtime_supported
    ? `本地 GPU 流水线仅面向 Windows x64/NVIDIA，${provision.platform} 上${provision.media_supported ? "只能安装 FFmpeg 媒体工具" : "无法安装托管运行时"}。在此平台请在首页把运行方式切到「云端」——云端转写不需要本地运行时与模型，只需登录账号。`
    : "";
  const jobBlocked = jobRunning ? "已有安装任务正在进行，请等待其结束。" : "";
  const runtimeUnavailable = provision && !provision.runtime_supported && !provision.media_supported
    ? `托管运行时暂不支持 ${provision.platform}。`
    : "";
  const runtimeBlocked = connectionBlocked || runtimeUnavailable || jobBlocked;
  const allBlocked = connectionBlocked
    || (provision && !provision.runtime_supported ? `本地 GPU 环境暂不支持 ${provision.platform}。` : "")
    || jobBlocked;
  const modelsBlocked = connectionBlocked
    || (provision && !provision.runtime_supported ? `本地模型暂不支持 ${provision.platform}。` : "")
    || jobBlocked
    || (provision && provision.runtime.state !== "ready" ? "需要先完成「必备运行时」。" : "");
  const toolsBlocked = connectionBlocked
    || (provision && !provision.runtime_supported ? `托管可选工具暂不支持 ${provision.platform}。` : "")
    || jobBlocked;
  // 位置卡上的「开始安装」用它对应目标的门槛，包括改完位置后 sidecar 重启的那几秒。
  const awaitingBlocked = awaitingLocation === "models" ? modelsBlocked : awaitingLocation === "runtime" ? runtimeBlocked : allBlocked;
  const pythonBlocked = !pythonBootstrap
    ? ""
    : !pythonBootstrap.supported
      ? pythonBootstrap.message || `${pythonBootstrap.platform} 暂不支持自动安装 Python。`
      : pythonInstalling ? "Python 正在安装中。" : "";
  return (
    <section className="runtime-layout">
      <article className="panel runtime-diagnostics">
        <div className="panel-heading">
          <div><span className="eyebrow">Runtime diagnostics</span><h2>环境诊断</h2></div>
          <Mark>{ready ? "环境已就绪" : sidecar?.running ? "需要处理" : "服务未连接"}</Mark>
        </div>
        {issues.length ? (
          <ul className="issues">
            {issues.map((issue) => <li key={`${issue.code}:${issue.message}`}><strong>{issue.code}</strong><span>{issue.message}</span></li>)}
          </ul>
        ) : !ready && <p className="success-copy">{message || "等待环境状态。"}</p>}
        <div className="diagnostic-section-heading">
          <strong>运行环节</strong>
          <small>{stages.filter((stage) => stage.ready).length} / {stages.length} 可用</small>
        </div>
        <div className="stage-list">
          {stages.map((stage) => (
            <div className={stage.ready ? "stage-ready" : "stage-blocked"} key={stage.id}>
              <span>{stage.ready ? "✓" : "!"}</span>
              <div><strong>{stage.label}</strong>{!stage.ready && <small>{stage.issues[0]?.message}</small>}</div>
            </div>
          ))}
          {!stages.length && <p className="success-copy">连接桌面执行服务后，将分别检测媒体准备、ASR、LLM、知识库和视频多模态环节。</p>}
        </div>
      </article>
      <article className="panel provision-panel">
        <div className="panel-heading">
          <div><span className="eyebrow">Managed assets</span><h2>运行时与模型</h2></div>
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
              title={jobRunning ? "请先取消或等待当前安装任务结束。" : "删除托管的运行时、模型与缓存；不会删除任务、字幕和用户设置。"}
              type="button"
            >
              {confirmRemove ? "确认删除环境" : "删除环境"}
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
            title="必备模型"
            items={requiredModels}
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
            removable={videoTools.some((item) => item.source !== "system" && item.state !== "missing")}
            confirmingRemove={confirmRemoveGroup === "video-tools"}
            onInstall={requestInstall}
            onCancelRemove={() => setConfirmRemoveGroup((current) => current === "video-tools" ? null : current)}
            onRemove={() => {
              if (confirmRemoveGroup !== "video-tools") {
                setConfirmRemoveGroup("video-tools");
                return;
              }
              setConfirmRemoveGroup(null);
              void onRemoveGroup("video-tools");
            }}
          />
          <AssetGroup
            title="可选工具"
            items={otherTools}
            optional
            target="optional-tools"
            installing={jobRunning && job?.target === "optional-tools"}
            blocked={toolsBlocked}
            removable={otherTools.some((item) => item.source !== "system" && item.state !== "missing")}
            confirmingRemove={confirmRemoveGroup === "optional-tools"}
            onInstall={requestInstall}
            onCancelRemove={() => setConfirmRemoveGroup((current) => current === "optional-tools" ? null : current)}
            onRemove={() => {
              if (confirmRemoveGroup !== "optional-tools") {
                setConfirmRemoveGroup("optional-tools");
                return;
              }
              setConfirmRemoveGroup(null);
              void onRemoveGroup("optional-tools");
            }}
          />
        </div>
        {provision?.root && <div className="runtime-storage-row">
          <small className="runtime-root">安装目录：{provision.root}</small>
        </div>}
      </article>
    </section>
  );
}
