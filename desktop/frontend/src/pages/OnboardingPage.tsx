import { Browser } from "@wailsio/runtime";
import { useEffect, useMemo, useRef, useState } from "react";
import type { Dispatch, SetStateAction } from "react";
import type { Snapshot as SidecarSnapshot } from "../../bindings/github.com/Ricori/nonoka-x/desktop/internal/sidecar/models.js";
import type { CloudSession } from "../bridge/cloud.ts";
import type { MigrationStatus } from "../bridge/migration.ts";
import { desktopPreferences, type ProxyStatus } from "../bridge/preferences.ts";
import type {
  PythonBootstrapState,
  RuntimeInstallTarget,
  RuntimeLayerId,
  RuntimeProvisionState,
} from "../bridge/runtime.ts";
import type { FineSubKeyState, FineSubSettingsState } from "../bridge/settings.ts";
import type {
  RelocationProgress,
  StorageDestination,
  StorageStatus,
  StorageTarget,
} from "../bridge/storage.ts";
import type { Theme } from "../app/types.ts";
import { transcriptionEngineLabel } from "../app/format.ts";
import type { EngineChoice } from "../components/engineChoices.ts";
import { layerLabel } from "../components/engineLayers.ts";
import { LlmConfigurationCard } from "../components/LlmConfigurationCard.tsx";
import { Mark } from "../components/Mark.tsx";
import { Mascot, type MascotPose } from "../components/Mascot.tsx";
import type { MigrationView } from "../components/migration.ts";
import { Notice, type NoticeTone } from "../components/Notice.tsx";
import {
  detectHardware,
  friendlyBootstrapError,
  isBaseEnvironmentReady,
  isMigrationWizardActive,
  layerOverviewOf,
  layerStateOf,
  migrationWizardStep,
  planOnboardingInstall,
  targetEngineForMode,
  type LocalEngineId,
  type OnboardingMode,
  type OnboardingStep,
} from "../components/onboarding.ts";
import { ProxySettingsCard } from "../components/ProxySettingsCard.tsx";
import {
  RelocationConfirm,
  RelocationProgressView,
  formatStorageBytes,
  useDestinationChoice,
} from "../components/StorageRelocation.tsx";
import type { Capabilities } from "../providers/types.ts";
import "./OnboardingPage.css";

const HIDDEN_ADVANCED_KEYS = new Set(["HF_TOKEN"]);

const ENGINE_KEY_LINKS: Record<string, { label: string; href: string }> = {
  "moss-pro": { label: "获取免费额度 ↗", href: "https://platform.moss.cn/app/api-keys" },
  scribe: { label: "获取 key ↗", href: "https://elevenlabs.io/app/settings/api-keys" },
};

const INSTALL_PHASES = [
  { id: "downloading", label: "下载" },
  { id: "verifying", label: "校验" },
  { id: "extracting", label: "解压" },
  { id: "activating", label: "安装" },
] as const;

const STAGE_ORDER: Record<string, number> = {
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

const MIGRATION_STEP_LABELS: Record<string, string> = {
  "runtime-root": "切换运行环境目录",
  models: "移动已有模型",
  settings: "迁移设置与密钥",
  knowledge: "迁移知识库",
  "limit-state": "迁移限流状态",
  "task-products": "转换历史任务",
  "old-environment": "清理旧运行环境",
  caches: "清理旧缓存",
  rebuildable: "清理旧辅助文件",
  leftovers: "清理旧配置残留",
  "model-completion": "核对并补齐模型",
  "layer-download": "下载运行环境",
};

const KNOWN_LAYER_IDS = new Set<string>(["tools", "python", "models", "models-moss"]);
function isLayerId(value: string): value is RuntimeLayerId {
  return KNOWN_LAYER_IDS.has(value);
}

function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
  const amount = value / 1024 ** index;
  return `${amount >= 100 || index === 0 ? amount.toFixed(0) : amount.toFixed(1)} ${units[index]}`;
}

function formatVram(memoryMb: number): string {
  if (!memoryMb || memoryMb <= 0) return "";
  const gb = memoryMb / 1024;
  return `${gb >= 10 ? gb.toFixed(0) : gb.toFixed(1)} GB 显存`;
}

function keyStateOf(settings: FineSubSettingsState | null, name: string): FineSubKeyState | null {
  return settings?.engineKeys?.find((item) => item.name === name)
    ?? settings?.keys.find((item) => item.name === name)
    ?? null;
}

export interface OnboardingPageProps {
  theme: Theme;
  onToggleTheme: () => void;
  sidecar: SidecarSnapshot | null;
  pythonBootstrap: PythonBootstrapState | null;
  provision: RuntimeProvisionState | null;
  provisionMessage: string;
  onDismissProvisionMessage: () => void;
  capabilities: Capabilities | null;
  engines: EngineChoice[];
  transcriptionEngine: string;
  onSelectEngine: (engine: string) => void;
  onInstallPython: () => Promise<void>;
  onInstall: (target: RuntimeInstallTarget) => Promise<void>;
  onInstallOffline: (layer: RuntimeLayerId) => Promise<void>;
  onCancelInstall: () => Promise<void>;
  onRefresh: () => Promise<void>;
  storage: StorageStatus | null;
  storageProgress: RelocationProgress | null;
  storageBusy: boolean;
  storageMessage: string;
  onDismissStorageMessage: () => void;
  onChooseStorage: (target: StorageTarget) => Promise<StorageDestination | null>;
  onRelocateStorage: (target: StorageTarget, destination: string) => Promise<void>;
  onCancelStorage: () => Promise<void>;
  cloudSession: CloudSession | null;
  loginKey: string;
  accountBusy: boolean;
  accountMessage: string;
  onLoginKeyChange: (value: string) => void;
  onLogin: () => Promise<void>;
  onDismissAccountMessage: () => void;
  settings: FineSubSettingsState | null;
  drafts: Record<string, string>;
  keysBusy: boolean;
  keysMessage: string;
  setDrafts: Dispatch<SetStateAction<Record<string, string>>>;
  onSaveKey: (updates: Record<string, string | null>, name: string) => Promise<void>;
  onDismissKeysMessage: () => void;
  migration: MigrationStatus | null;
  migrationView: MigrationView;
  migrationRetrying: boolean;
  onRetryMigration: () => void;
  onComplete: () => void;
}

export function OnboardingPage(props: OnboardingPageProps) {
  const {
    theme,
    onToggleTheme,
    sidecar,
    pythonBootstrap,
    provision,
    provisionMessage,
    onDismissProvisionMessage,
    capabilities,
    engines,
    transcriptionEngine,
    onSelectEngine,
    onInstallPython,
    onInstall,
    onInstallOffline,
    onCancelInstall,
    onRefresh,
    storage,
    storageProgress,
    storageBusy,
    storageMessage,
    onDismissStorageMessage,
    onChooseStorage,
    onRelocateStorage,
    onCancelStorage,
    cloudSession,
    loginKey,
    accountBusy,
    accountMessage,
    onLoginKeyChange,
    onLogin,
    onDismissAccountMessage,
    settings,
    drafts,
    keysBusy,
    keysMessage,
    setDrafts,
    onSaveKey,
    onDismissKeysMessage,
    migration,
    migrationView,
    migrationRetrying,
    onRetryMigration,
    onComplete,
  } = props;

  const migrationMode = isMigrationWizardActive(migration, false);
  const migrationStep = migrationWizardStep(migration);

  const [step, setStep] = useState<OnboardingStep>(() => (provision?.job.state === "running" ? 3 : 1));
  const [mode, setMode] = useState<OnboardingMode>(() => {
    if (transcriptionEngine === "finesub-cloud") return "cloud";
    const matched = engines.find((item) => item.id === transcriptionEngine);
    if (matched?.kind === "cloud-api") return "cloud-api";
    return "local";
  });
  const [modeTouched, setModeTouched] = useState(false);
  const [localEngine, setLocalEngine] = useState<LocalEngineId>(
    transcriptionEngine === "moss-local" ? "moss-local" : "finesub",
  );
  const [cloudApiEngine, setCloudApiEngine] = useState<string>("moss-pro");
  // 默认一起下载基础环境 + AI 模型层
  const [includeModels, setIncludeModels] = useState(true);
  // Step 3 明确询问用户有无离线包：默认在线下载，可选本地 .tar.zst 离线包导入
  const [installSource, setInstallSource] = useState<"online" | "offline">("online");
  const [proxyModalOpen, setProxyModalOpen] = useState(false);
  const [proxyStatus, setProxyStatus] = useState<ProxyStatus | null>(null);
  const [chainedTarget, setChainedTarget] = useState<RuntimeInstallTarget | null>(null);
  const [showMigrationLlm, setShowMigrationLlm] = useState(false);
  const [rechecking, setRechecking] = useState(false);
  const [keysSource, setKeysSource] = useState<"engine" | "llm">("llm");

  const { pending, choosing, choose, clear } = useDestinationChoice(onChooseStorage);

  const hardware = useMemo(() => detectHardware(capabilities, provision), [capabilities, provision]);

  // 硬件信息首次到达时，若用户尚未手动选择模式，自动按本机显卡情况选中推荐模式
  useEffect(() => {
    if (modeTouched || !provision) return;
    if (!hardware.runtimeSupported) {
      setMode("cloud");
    } else if (capabilities) {
      setMode(hardware.recommendedMode);
    }
  }, [capabilities, hardware.recommendedMode, hardware.runtimeSupported, modeTouched, provision]);

  // 代理状态轮询（用于左下角小标签显示当前走直连还是代理）
  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const next = await desktopPreferences.proxyStatus();
        if (!cancelled) setProxyStatus(next);
      } catch {
        // ignore
      }
    };
    void load();
    const timer = window.setInterval(() => void load(), 8000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [proxyModalOpen]);

  // Esc 关闭代理弹窗
  useEffect(() => {
    if (!proxyModalOpen) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setProxyModalOpen(false);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [proxyModalOpen]);

  const bootstrapBroken = (provision?.bootstrap_error ?? "") !== "";
  const pythonInstalling = pythonBootstrap?.state === "running";
  const pythonFailed = Boolean(
    bootstrapBroken
    || pythonBootstrap?.state === "failed"
    || (pythonBootstrap && !pythonBootstrap.supported && !sidecar?.running),
  );
  const serviceReady = Boolean(sidecar?.running && provision && !bootstrapBroken);

  const baseReady = isBaseEnvironmentReady(mode, provision, Boolean(sidecar?.running));
  // 规则 3：必须有基础环境后才允许跳过（旧版迁移期间不允许跳过）
  const canSkip = !migrationMode && baseReady;

  const installPlan = useMemo(
    () => planOnboardingInstall({ mode, localEngine, includeModels, provision }),
    [includeModels, localEngine, mode, provision],
  );

  // 为模式卡片实时估算下载体积
  const toolsBytes = layerOverviewOf(provision, "tools")?.archive_bytes ?? 0;
  const pythonBytes = layerOverviewOf(provision, "python")?.archive_bytes ?? 0;
  const fineSubModelBytes = layerOverviewOf(provision, "models")?.archive_bytes ?? 0;
  const mossModelBytes = layerOverviewOf(provision, "models-moss")?.archive_bytes ?? 0;
  const mossIndexed = Boolean(layerOverviewOf(provision, "models-moss"));
  const localSelectedModelBytes = localEngine === "moss-local" && mossIndexed ? mossModelBytes : fineSubModelBytes;
  const localTotalEstimate = toolsBytes + pythonBytes + (includeModels ? localSelectedModelBytes : 0);

  const cloudApiChoices = useMemo(() => {
    const list = engines.filter((item) => item.kind === "cloud-api");
    return [
      ...list.filter((item) => item.id === "moss-pro"),
      ...list.filter((item) => item.id !== "moss-pro"),
    ];
  }, [engines]);
  const activeCloudApi = useMemo(
    () => cloudApiChoices.find((item) => item.id === cloudApiEngine)
      ?? cloudApiChoices.find((item) => item.id === "moss-pro")
      ?? cloudApiChoices[0]
      ?? null,
    [cloudApiChoices, cloudApiEngine],
  );

  const job = provision?.job ?? null;
  const jobRunning = job?.state === "running";

  // MOSS 0.9B 链式安装：runtime（tools + python）完成后自动衔接 models-moss
  const lastJobState = useRef(job?.state);
  useEffect(() => {
    const prev = lastJobState.current;
    lastJobState.current = job?.state;
    if (!chainedTarget) return;
    if (job?.state === "failed" || job?.state === "cancelled") {
      setChainedTarget(null);
      return;
    }
    if (prev === "running" && job?.state === "completed") {
      const next = chainedTarget;
      setChainedTarget(null);
      if (next === "models-moss" && layerStateOf(provision, "models-moss") !== "ready") {
        void onInstall("models-moss");
      }
    }
  }, [chainedTarget, job?.state, onInstall, provision]);

  // 迁移完成后，如果报告里提示有需要重填的密钥，自动展开纠错配置区域方便用户处理
  useEffect(() => {
    if (migrationMode && migration?.state === "completed" && (migration.report?.settings?.unreadable?.length ?? 0) > 0) {
      setShowMigrationLlm(true);
    }
  }, [migration?.report?.settings?.unreadable?.length, migration?.state, migrationMode]);

  const runtimeLocation = (storage?.locations ?? []).find((item) => item.target === "runtime") ?? null;
  const relocatingRuntime = storageProgress?.active === true && storageProgress.target === "runtime";
  const tightDisk = runtimeLocation
    ? (!runtimeLocation.enoughSpace || (installPlan.missingDownloadBytes > 0 && runtimeLocation.freeBytes < installPlan.missingDownloadBytes * 2.2))
    : false;

  const confirmRelocation = () => {
    if (!pending) return;
    const { directory } = pending;
    clear();
    void onRelocateStorage("runtime", directory);
  };

  const startInstallPlan = async () => {
    if (!installPlan.primaryTarget) return;
    setChainedTarget(installPlan.chainedTarget);
    await onInstall(installPlan.primaryTarget);
  };

  const handleModeNext = () => {
    const chosenEngine = targetEngineForMode(mode, localEngine, activeCloudApi?.id ?? cloudApiEngine);
    onSelectEngine(chosenEngine);
    setStep(3);
  };

  const saveFrom = (source: "engine" | "llm") => async (updates: Record<string, string | null>, name: string) => {
    setKeysSource(source);
    await onSaveKey(updates, name);
  };

  const keysTone: NoticeTone = keysMessage.endsWith("已保存。") ? "success" : "warn";

  // LLM 配置准备度判断
  const modelRouting = settings?.modelRouting;
  const keys = settings?.keys ?? [];
  const baseUrls = settings?.baseUrls ?? [];
  const modelKeyNames = new Set((modelRouting?.providers ?? []).map((provider) => provider.keyName).filter(Boolean));
  const searchKeys = keys.filter((key) => !modelKeyNames.has(key.name) && !HIDDEN_ADVANCED_KEYS.has(key.name));
  const corrector = capabilities?.correction_engines?.[0] ?? null;
  const correctorReady = Boolean(corrector?.ready || capabilities?.settings?.llm_key_configured);
  const correctorIssue = !capabilities ? "本地服务尚未启动" : corrector && !corrector.ready ? corrector.issues[0]?.message ?? "尚未就绪" : "";

  // 活跃下载层（供「改用离线包」使用）
  const activeDownloadLayer: RuntimeLayerId | null = (() => {
    if (!jobRunning || !job || !hardware.runtimeSupported) return null;
    if (job.stage === "offline" || job.stage === "unpacking" || job.stage === "checking" || job.stage === "completed") return null;
    if (isLayerId(job.resource)) return job.resource;
    if (job.target === "media") return "tools";
    if (job.target === "models") return "models";
    if (job.target === "models-moss") return "models-moss";
    if (job.target === "runtime" || job.target === "all") {
      if (layerStateOf(provision, "tools") !== "ready") return "tools";
      if (layerStateOf(provision, "python") !== "ready") return "python";
      if (job.target === "all" && layerStateOf(provision, "models") !== "ready") return "models";
    }
    return null;
  })();

  const jobProgress = job?.progress;
  const jobPercent = jobProgress?.total ? Math.min(100, Math.max(0, (jobProgress.completed / jobProgress.total) * 100)) : 0;
  const jobPhase = STAGE_ORDER[job?.stage ?? ""] ?? 0;
  const slowDownload = jobRunning
    && job?.stage === "downloading"
    && jobProgress?.bytes_per_second != null
    && jobProgress.bytes_per_second > 0
    && jobProgress.bytes_per_second < 512 * 1024;

  const bootstrapError = pythonFailed
    ? friendlyBootstrapError(
      pythonBootstrap?.state === "failed"
        ? pythonBootstrap.message
        : bootstrapBroken
          ? provision?.bootstrap_error ?? ""
          : pythonBootstrap?.message || sidecar?.error || "本地服务未能启动",
    )
    : null;

  const mascotPose: MascotPose = migrationMode
    ? (migrationStep === 3 ? "wave" : migrationStep === 2 ? "idle" : "welcome")
    : (step === 4 ? "wave" : step === 3 ? "idle" : "welcome");

  const proxySummary = !proxyStatus
    ? "检测中"
    : proxyStatus.configured
      ? (proxyStatus.source === "custom" ? "自定义代理" : "系统代理")
      : "直连";

  const wizardSteps = migrationMode
    ? [
      { id: 1, title: "升级旧版数据", desc: "迁移媒体、密钥与知识库" },
      { id: 2, title: "更新运行环境", desc: "拉取新版基础组件" },
      { id: 3, title: "确认并完成", desc: "查看升级报告" },
    ]
    : [
      { id: 1, title: "欢迎与准备", desc: "初始化本地服务" },
      { id: 2, title: "选择使用方式", desc: "本机、云端或云 API" },
      { id: 3, title: "安装运行环境", desc: "确认目录并下载组件" },
      { id: 4, title: "纠错与完成", desc: "配置大模型（可选）" },
    ];

  const currentStepNumber = migrationMode ? migrationStep : step;
  const totalSteps = wizardSteps.length;

  const stepMeta = migrationMode
    ? (migrationStep === 1
      ? { title: migrationView.heading || "正在升级旧版数据", subtitle: migrationView.message || "检测到旧版本数据，正在为你无损迁移至新结构，完成前请勿关闭应用。" }
      : migrationStep === 2
        ? { title: migrationView.heading || "正在更新运行环境", subtitle: migrationView.message || "旧版数据已迁移完成，正在自动补齐新版所需的运行环境。" }
        : { title: "旧版数据升级完成", subtitle: "你的历史任务、知识库与设置已迁移就绪，确认下方报告即可开始使用。" })
    : (step === 1
      ? { title: "欢迎使用 Nonoka Sub X", subtitle: "从语音转写、AI 翻译到精修时间轴，只需几步。" }
      : step === 2
        ? { title: "选择你的转写方式", subtitle: "决定默认如何识别视频语音。不同方式所需的下载体积不同，之后随时可在设置中切换。" }
        : step === 3
          ? { title: jobRunning ? "正在安装运行环境" : installPlan.allReady ? "运行环境已就绪" : "确认安装位置与组件", subtitle: jobRunning ? "组件将安装在独立目录中，不会改动你的系统环境。" : installPlan.allReady ? "所选转写方式需要的运行环境均已安装完毕。" : "选择安装目录并开始下载，磁盘紧张时建议放到非系统盘。" }
          : { title: "配置 AI 纠错与翻译", subtitle: "用于将语音初稿自动校对并翻译为自然字幕。此步可跳过后仍可转写与编辑字幕。" });

  // 云 API 卡片内待保存的 Key
  const activeApiKeyUpdates = Object.fromEntries(
    (activeCloudApi?.keys ?? []).flatMap((name) => {
      const value = (drafts[name] ?? "").trim();
      return value ? [[name, value]] : [];
    }),
  ) as Record<string, string>;
  const hasApiKeyDraft = Object.keys(activeApiKeyUpdates).length > 0;

  return (
    <div className="onboarding-shell">
      {/* 左侧品牌与步骤轨（直接贴边铺满独立窗口左侧） */}
      <aside className="onboarding-rail">
        <div className="onboarding-brand">
          <img className="onboarding-brand-icon" src="./assets/icon.png" alt="Nonoka Sub X" draggable={false} />
          <div>
            <strong>Nonoka Sub X</strong>
            <span className="onboarding-brand-pill">{migrationMode ? "版本升级" : "首次配置"}</span>
          </div>
        </div>

        <ol className="onboarding-stepper" aria-label="配置步骤">
          {wizardSteps.map((item) => {
            const done = item.id < currentStepNumber;
            const active = item.id === currentStepNumber;
            const clickable = !migrationMode && !jobRunning && done && (item.id === 1 || serviceReady);
            return (
              <li key={item.id} className={`onboarding-step-item ${done ? "done" : active ? "active" : ""}`}>
                <button
                  type="button"
                  className="onboarding-step-btn"
                  disabled={!clickable}
                  onClick={() => clickable && setStep(item.id as OnboardingStep)}
                >
                  <span className="onboarding-step-index" aria-hidden="true">
                    {done ? "✓" : item.id}
                  </span>
                  <span className="onboarding-step-copy">
                    <b>{item.title}</b>
                    <small>{item.desc}</small>
                  </span>
                </button>
              </li>
            );
          })}
        </ol>

        <div className="onboarding-mascot-wrap">
          <Mascot pose={mascotPose} />
        </div>

        <div className="onboarding-rail-footer">
          <button
            type="button"
            className="onboarding-proxy-trigger"
            onClick={() => setProxyModalOpen(true)}
            title="下载受阻或较慢时，可在此配置 HTTP / SOCKS5 代理"
          >
            <span className={`onboarding-proxy-dot ${proxyStatus?.configured ? "active" : ""}`} aria-hidden="true" />
            <span>网络代理</span>
            <small>{proxySummary}</small>
          </button>
        </div>
      </aside>

      {/* 右侧主内容区（顶部自带原生无边框拖拽栏与主题切换） */}
      <section className="onboarding-main">
        <div className="workspace-titlebar onboarding-titlebar">
          <div className="workspace-titlebar-drag" aria-hidden="true" />
          <button
            className="titlebar-theme-button"
            type="button"
            aria-label="切换主题"
            title={theme === "dark" ? "切换到浅色模式" : "切换到深色模式"}
            onClick={onToggleTheme}
          >
            {theme === "dark" ? (
              <svg viewBox="0 0 16 16" aria-hidden="true">
                <circle cx="8" cy="8" r="3" />
                <path d="M8 1.5v1.6M8 12.9v1.6M1.5 8h1.6M12.9 8h1.6M3.4 3.4l1.1 1.1M11.5 11.5l1.1 1.1M12.6 3.4l-1.1 1.1M4.5 11.5l-1.1 1.1" />
              </svg>
            ) : (
              <svg viewBox="0 0 16 16" aria-hidden="true">
                <path d="M13.2 9.9A5.4 5.4 0 0 1 6.1 2.8a5.5 5.5 0 1 0 7.1 7.1Z" />
              </svg>
            )}
          </button>
          <span className="titlebar-divider" aria-hidden="true" />
        </div>

        <header className="onboarding-header">
          <div className="onboarding-header-copy">
            <span className="onboarding-eyebrow">
              {migrationMode ? `升级向导 · 步骤 ${currentStepNumber} / ${totalSteps}` : `步骤 ${currentStepNumber} / ${totalSteps}`}
            </span>
            <h1>{stepMeta.title}</h1>
            <p>{stepMeta.subtitle}</p>
          </div>
          {canSkip && (
            <button
              type="button"
              className="ghost-button btn-sm onboarding-skip-btn"
              onClick={onComplete}
            >
              跳过剩余配置 →
            </button>
          )}
        </header>

        <div className="onboarding-body">
          {/* ================= 旧版数据迁移流程 ================= */}
          {migrationMode && migration && (
            <div className="onboarding-flow-stack">
              {migrationStep === 1 && (
                <>
                  <div className={`onboarding-status-card tone-${migrationView.tone}`}>
                    <div className="onboarding-status-head">
                      <span className={`onboarding-status-badge tone-${migrationView.tone}`}>
                        {migrationView.polling ? <span className="onboarding-spinner" aria-hidden="true" /> : "!"}
                      </span>
                      <div>
                        <strong>{migrationView.stepLine || migrationView.heading}</strong>
                        <p>{migrationView.error || migrationView.message || "正在检查并整理旧版本目录与任务文件…"}</p>
                      </div>
                    </div>
                    {migrationView.blockers.length > 0 && (
                      <ul className="onboarding-bullet-list">
                        {migrationView.blockers.map((item) => <li key={item}>{item}</li>)}
                      </ul>
                    )}
                  </div>

                  {(migration.steps?.filter((s) => !s.download).length ?? 0) > 0 && (
                    <div className="onboarding-checklist">
                      {migration.steps!.filter((s) => !s.download).map((item) => (
                        <div key={item.id} className={`onboarding-check-row state-${item.status}`}>
                          <span className="onboarding-check-icon" aria-hidden="true">
                            {item.status === "done" || item.status === "skipped"
                              ? "✓"
                              : item.status === "failed"
                                ? "!"
                                : item.status === "running"
                                  ? "…"
                                  : "○"}
                          </span>
                          <strong>{MIGRATION_STEP_LABELS[item.id] ?? item.title}</strong>
                          <small>
                            {item.status === "done"
                              ? "已完成"
                              : item.status === "skipped"
                                ? "无需处理"
                                : item.status === "running"
                                  ? "处理中…"
                                  : item.status === "failed"
                                    ? "失败"
                                    : "等待中"}
                          </small>
                        </div>
                      ))}
                    </div>
                  )}
                </>
              )}

              {migrationStep === 2 && (
                <>
                  <div className={`onboarding-progress-box ${migrationView.tone === "error" ? "is-error" : ""}`}>
                    <div className="onboarding-progress-top">
                      <div>
                        <small>自动更新运行环境</small>
                        <strong>{migrationView.stepLine || migrationView.heading}</strong>
                      </div>
                      <b>{migrationView.percent !== null ? `${Math.round(migrationView.percent)}%` : migrationView.canRetry ? "已中断" : "处理中"}</b>
                    </div>
                    {migrationView.tone !== "error" && (
                      <div className={`onboarding-progress-track ${migrationView.percent === null ? "indeterminate" : ""}`}>
                        <span style={migrationView.percent !== null ? { width: `${migrationView.percent}%` } : undefined} />
                      </div>
                    )}
                    <div className="onboarding-progress-meta">
                      <span>{migrationView.error || migrationView.progressLine || "正在拉取新版工具层与 Python 环境层…"}</span>
                    </div>
                    {migrationView.canRetry && (
                      <div className="onboarding-inline-actions">
                        <button type="button" className="quiet-button btn-sm" onClick={() => setProxyModalOpen(true)}>
                          配置网络代理
                        </button>
                      </div>
                    )}
                  </div>

                  {(migration.steps?.filter((s) => s.download).length ?? 0) > 0 && (
                    <div className="onboarding-checklist">
                      {migration.steps!.filter((s) => s.download).map((item) => (
                        <div key={item.id} className={`onboarding-check-row state-${item.status}`}>
                          <span className="onboarding-check-icon" aria-hidden="true">
                            {item.status === "done" || item.status === "skipped"
                              ? "✓"
                              : item.status === "failed"
                                ? "!"
                                : item.status === "running"
                                  ? "…"
                                  : "○"}
                          </span>
                          <strong>{MIGRATION_STEP_LABELS[item.id] ?? item.title}</strong>
                          <small>
                            {item.status === "done"
                              ? "已完成"
                              : item.status === "skipped"
                                ? "无需更新"
                                : item.status === "running"
                                  ? "下载中…"
                                  : item.status === "failed"
                                    ? "中断"
                                    : "等待中"}
                          </small>
                        </div>
                      ))}
                    </div>
                  )}
                </>
              )}

              {migrationStep === 3 && (
                <>
                  <div className="onboarding-status-card tone-success">
                    <div className="onboarding-status-head">
                      <span className="onboarding-status-badge tone-success">✓</span>
                      <div>
                        <strong>所有旧版数据与运行环境均已升级就绪</strong>
                        <p>
                          {migrationView.sections.length > 0
                            ? "大部分设置已自动迁移，以下少量条目需要你留意或重新填写："
                            : "媒体记录、知识库、历史任务与配置均已无损过渡到新版本。"}
                        </p>
                      </div>
                    </div>
                  </div>

                  {migrationView.sections.map((section) => (
                    <div className="onboarding-report-card" key={section.title}>
                      <strong>{section.title}（{section.items.length}）</strong>
                      <ul className="onboarding-bullet-list">
                        {section.items.map((item) => <li key={item}>{item}</li>)}
                      </ul>
                    </div>
                  ))}

                  {settings && modelRouting && (
                    <div className="onboarding-migration-llm">
                      <div className="onboarding-migration-llm-bar">
                        <span>纠错与翻译大模型配置{correctorReady ? "（已就绪）" : "（可在此补充）"}</span>
                        <button
                          type="button"
                          className="inline-link"
                          onClick={() => setShowMigrationLlm((v) => !v)}
                        >
                          {showMigrationLlm ? "收起" : "展开配置"}
                        </button>
                      </div>
                      {showMigrationLlm && (
                        <div className="onboarding-llm-embed">
                          <LlmConfigurationCard
                            keys={keys}
                            baseUrls={baseUrls}
                            modelRouting={modelRouting}
                            searchKeys={searchKeys}
                            correctorIssue={correctorIssue}
                            drafts={drafts}
                            busy={keysBusy}
                            message={keysSource === "llm" ? keysMessage : ""}
                            messageTone={keysTone}
                            onDismissMessage={onDismissKeysMessage}
                            setDrafts={setDrafts}
                            onSave={saveFrom("llm")}
                          />
                        </div>
                      )}
                    </div>
                  )}
                </>
              )}
            </div>
          )}

          {/* ================= 新用户首次配置：Step 1 欢迎与准备 ================= */}
          {!migrationMode && step === 1 && (
            <div className="onboarding-flow-stack">
              <div className="onboarding-feature-grid">
                <div className="onboarding-feature-item">
                  <strong>本机与云端自由选</strong>
                  <p>支持本机显卡离线转写，也可使用 API 轻量运行。</p>
                </div>
                <div className="onboarding-feature-item">
                  <strong>独立隔离环境</strong>
                  <p>所有工具与依赖在独立沙箱运行，不影响系统环境。</p>
                </div>
                <div className="onboarding-feature-item">
                  <strong>一站式字幕工作流</strong>
                  <p>语音识别、AI 纠错翻译与精修字幕、导出视频无缝衔接。</p>
                </div>
              </div>

              {pythonInstalling && pythonBootstrap ? (
                <div className="onboarding-progress-box">
                  <div className="onboarding-progress-top">
                    <div>
                      <small>少女祈祷中</small>
                      <strong>{pythonBootstrap.message || "正在准备本地服务…"}</strong>
                    </div>
                    <b>
                      {pythonBootstrap.progress?.total
                        ? `${Math.round(Math.min(100, (pythonBootstrap.progress.completed / pythonBootstrap.progress.total) * 100))}%`
                        : "准备中"}
                    </b>
                  </div>
                  <div className={`onboarding-progress-track ${pythonBootstrap.progress?.total ? "" : "indeterminate"}`}>
                    <span
                      style={
                        pythonBootstrap.progress?.total
                          ? { width: `${Math.min(100, (pythonBootstrap.progress.completed / pythonBootstrap.progress.total) * 100)}%` }
                          : undefined
                      }
                    />
                  </div>
                  <div className="onboarding-progress-meta">
                    <span>
                      {pythonBootstrap.progress?.total
                        ? `${formatBytes(pythonBootstrap.progress.completed)} / ${formatBytes(pythonBootstrap.progress.total)}`
                        : "首次启动约数秒，请稍候…"}
                    </span>
                    <span>
                      {pythonBootstrap.progress?.bytes_per_second
                        ? `${formatBytes(pythonBootstrap.progress.bytes_per_second)}/s`
                        : pythonBootstrap.proxy
                          ? `通过 ${pythonBootstrap.proxy}`
                          : "不修改系统环境"}
                    </span>
                  </div>
                </div>
              ) : bootstrapError ? (
                <div className="onboarding-status-card tone-warning">
                  <div className="onboarding-status-head">
                    <span className="onboarding-status-badge tone-warning">!</span>
                    <div>
                      <strong>{bootstrapError.title}</strong>
                      <p>{bootstrapError.hint}</p>
                    </div>
                  </div>
                  {pythonBootstrap && !pythonBootstrap.supported && (
                    <p className="onboarding-tech-note">
                      手动方案：准备装有 httpx、pydantic 的 Python 3.12，通过环境变量 <code>NONOKA_PYTHON</code> 指向它后重启应用。
                    </p>
                  )}
                  <div className="onboarding-inline-actions">
                    <button
                      type="button"
                      className="primary-button btn-sm"
                      disabled={pythonBootstrap ? !pythonBootstrap.supported : false}
                      onClick={() => void onInstallPython()}
                    >
                      重试初始化
                    </button>
                    <button
                      type="button"
                      className="quiet-button btn-sm"
                      onClick={() => setProxyModalOpen(true)}
                    >
                      配置网络代理
                    </button>
                  </div>
                  {bootstrapError.detail && (
                    <details className="onboarding-tech-details">
                      <summary>查看技术详情</summary>
                      <pre>{bootstrapError.detail}</pre>
                    </details>
                  )}
                </div>
              ) : serviceReady ? (
                <div className="onboarding-status-card tone-success">
                  <div className="onboarding-status-head">
                    <span className="onboarding-status-badge tone-success">✓</span>
                    <div>
                      <strong>本地服务已就绪</strong>
                      <p>
                        {hardware.hasGpu
                          ? `检测到你的牛逼 N 卡：${hardware.gpuName}${hardware.gpuMemoryMb ? `（${formatVram(hardware.gpuMemoryMb)}）` : ""}，畅享 AI 加速！`
                          : hardware.runtimeSupported
                            ? "未检测到 NVIDIA 显卡，下一步可灵活选择云端转写或本机运行。"
                            : `当前平台为 ${provision?.platform ?? "当前系统"}，支持轻量工具层搭配云端转写与云 API。`}
                      </p>
                    </div>
                  </div>
                </div>
              ) : (
                <div className="onboarding-status-card tone-info">
                  <div className="onboarding-status-head">
                    <span className="onboarding-status-badge tone-info">
                      <span className="onboarding-spinner" aria-hidden="true" />
                    </span>
                    <div>
                      <strong>正在连接本地服务…</strong>
                      <p>正在启动后台服务并检测硬件环境，通常只需几秒钟。</p>
                    </div>
                    <button
                      type="button"
                      className="quiet-button btn-sm"
                      disabled={rechecking}
                      onClick={() => {
                        setRechecking(true);
                        void onRefresh().finally(() => setRechecking(false));
                      }}
                    >
                      {rechecking ? "检查中…" : "重新检查"}
                    </button>
                  </div>
                </div>
              )}
            </div>
          )}

          {/* ================= Step 2 选择使用方式 ================= */}
          {!migrationMode && step === 2 && (
            <div className="onboarding-flow-stack">
              <div className="onboarding-hardware-bar">
                <span className={`onboarding-hw-dot ${hardware.hasGpu ? "gpu" : ""}`} aria-hidden="true" />
                <span>
                  {hardware.hasGpu && hardware.runtimeSupported
                    ? `本机硬件：${hardware.gpuName}${hardware.gpuMemoryMb ? ` · ${formatVram(hardware.gpuMemoryMb)}` : ""}`
                    : !hardware.runtimeSupported
                      ? `当前系统（${provision?.platform ?? ""}）不支持本机 GPU 转写，推荐使用云端或云 API`
                      : "未检测到 NVIDIA 独立显卡，推荐 Nonoka Cloud 或第三方云 API"}
                </span>
              </div>

              <div className="onboarding-mode-list" role="radiogroup" aria-label="转写方式">
                {/* 选项 1：本机 AI 转写 */}
                <div
                  className={`onboarding-mode-card ${mode === "local" ? "selected" : ""} ${!hardware.runtimeSupported ? "disabled" : ""}`}
                  onClick={() => {
                    if (!hardware.runtimeSupported) return;
                    setModeTouched(true);
                    setMode("local");
                  }}
                >
                  <div className="onboarding-mode-head">
                    <span className="onboarding-radio" aria-hidden="true" />
                    <div className="onboarding-mode-title">
                      <strong>本机 AI 转写</strong>
                      {hardware.recommendedMode === "local" && <Mark tone="accent">推荐</Mark>}
                      <Mark tone="ok">免费离线</Mark>
                    </div>
                    <span className="onboarding-mode-size">
                      {localTotalEstimate > 0 ? `约 ${formatStorageBytes(localTotalEstimate)}` : "含基础与模型"}
                    </span>
                  </div>
                  <p className="onboarding-mode-desc">
                    {hardware.runtimeSupported
                      ? "在你的显卡上离线完成语音识别，音视频数据完全保留在本机。"
                      : "本机 AI 转写目前仅支持 Windows x64 平台。"}
                  </p>

                  {mode === "local" && hardware.runtimeSupported && (
                    <div className="onboarding-mode-extra" onClick={(e) => e.stopPropagation()}>
                      <div className="onboarding-subengine-row">
                        <span className="onboarding-sub-label">默认本机引擎</span>
                        <div className="onboarding-segmented">
                          <button
                            type="button"
                            className={localEngine === "finesub" ? "active" : ""}
                            onClick={() => setLocalEngine("finesub")}
                          >
                            FineSub（综合推荐）
                          </button>
                          {mossIndexed && (
                            <button
                              type="button"
                              className={localEngine === "moss-local" ? "active" : ""}
                              onClick={() => setLocalEngine("moss-local")}
                            >
                              MOSS 0.9B（多说话人）
                            </button>
                          )}
                        </div>
                      </div>
                      {localEngine === "moss-local" && !hardware.mossGpuReady && (
                        <p className="onboarding-inline-hint caution">
                          MOSS 0.9B 需要 NVIDIA RTX 30 系及以上显卡与约 8 GB 显存；若显存不足建议改用 FineSub。
                        </p>
                      )}
                      <label className="onboarding-checkbox">
                        <input
                          type="checkbox"
                          checked={includeModels}
                          onChange={(e) => setIncludeModels(e.target.checked)}
                        />
                        <span>
                          同时下载对应 AI 模型层
                          {localSelectedModelBytes > 0 ? `（约 ${formatStorageBytes(localSelectedModelBytes)}）` : ""}
                          ，安装后即可直接转写
                        </span>
                      </label>
                    </div>
                  )}
                </div>

                {/* 选项 2：Nonoka Cloud 云端转写 */}
                <div
                  className={`onboarding-mode-card ${mode === "cloud" ? "selected" : ""}`}
                  onClick={() => {
                    setModeTouched(true);
                    setMode("cloud");
                  }}
                >
                  <div className="onboarding-mode-head">
                    <span className="onboarding-radio" aria-hidden="true" />
                    <div className="onboarding-mode-title">
                      <strong>Nonoka Cloud 云端转写</strong>
                      {hardware.recommendedMode === "cloud" && <Mark tone="accent">推荐</Mark>}
                      <Mark tone="neutral">免显卡</Mark>
                    </div>
                    <span className="onboarding-mode-size">
                      {toolsBytes > 0 ? `仅约 ${formatStorageBytes(toolsBytes)}` : "轻量工具层"}
                    </span>
                  </div>
                  <p className="onboarding-mode-desc">
                    交给 Nonoka 云端完成识别，无需下载大型环境与模型，并支持多设备同步字幕。
                  </p>

                  {mode === "cloud" && (
                    <div className="onboarding-mode-extra" onClick={(e) => e.stopPropagation()}>
                      {cloudSession?.authenticated ? (
                        <div className="onboarding-cloud-authed">
                          <Mark tone="ok">已登录</Mark>
                          <span>
                            {cloudSession.name || "Nonoka Key"}
                            {cloudSession.admin ? " · 管理员不限次" : cloudSession.remaining != null ? ` · 剩余 ${cloudSession.remaining} 次额度` : ""}
                          </span>
                        </div>
                      ) : (
                        <div className="onboarding-inline-form">
                          <div className="onboarding-inline-input-row">
                            <input
                              type="password"
                              autoComplete="off"
                              autoCapitalize="none"
                              autoCorrect="off"
                              spellCheck={false}
                              placeholder="输入你的 Nonoka Key（也可之后登录）"
                              value={loginKey}
                              onChange={(e) => onLoginKeyChange(e.target.value)}
                            />
                            <button
                              type="button"
                              className="primary-button btn-sm"
                              disabled={accountBusy || !loginKey.trim()}
                              onClick={() => void onLogin()}
                            >
                              {accountBusy ? "验证中…" : "登录"}
                            </button>
                          </div>
                          <Notice
                            className="onboarding-inline-notice"
                            message={accountMessage}
                            tone={accountMessage.includes("成功") ? "success" : "warn"}
                            onDismiss={onDismissAccountMessage}
                          />
                        </div>
                      )}
                    </div>
                  )}
                </div>

                {/* 选项 3：第三方云 API */}
                {cloudApiChoices.length > 0 && (
                  <div
                    className={`onboarding-mode-card ${mode === "cloud-api" ? "selected" : ""}`}
                    onClick={() => {
                      setModeTouched(true);
                      setMode("cloud-api");
                    }}
                  >
                    <div className="onboarding-mode-head">
                      <span className="onboarding-radio" aria-hidden="true" />
                      <div className="onboarding-mode-title">
                        <strong>第三方云 API</strong>
                        <Mark tone="neutral">轻量免显卡</Mark>
                      </div>
                      <span className="onboarding-mode-size">
                        {toolsBytes > 0 ? `仅约 ${formatStorageBytes(toolsBytes)}` : "轻量工具层"}
                      </span>
                    </div>
                    <p className="onboarding-mode-desc">
                      使用自己的 MOSS Pro、Gemini 等第三方语音接口完成识别，仅需下载轻量工具层。
                    </p>

                    {mode === "cloud-api" && activeCloudApi && (
                      <div className="onboarding-mode-extra" onClick={(e) => e.stopPropagation()}>
                        <div className="onboarding-subengine-row">
                          <span className="onboarding-sub-label">选择接口</span>
                          <div className="onboarding-segmented">
                            {cloudApiChoices.map((choice) => (
                              <button
                                key={choice.id}
                                type="button"
                                className={choice.id === activeCloudApi.id ? "active" : ""}
                                onClick={() => setCloudApiEngine(choice.id)}
                              >
                                {choice.label}
                              </button>
                            ))}
                          </div>
                        </div>

                        {activeCloudApi.keys.length > 0 && (
                          <div className="onboarding-inline-form">
                            {activeCloudApi.keys.map((keyName) => {
                              const state = keyStateOf(settings, keyName);
                              const isUrl = keyName.endsWith("_ENDPOINT");
                              return (
                                <div className="onboarding-api-key-field" key={keyName}>
                                  <div className="onboarding-api-key-label">
                                    <span>{state?.label ?? keyName}</span>
                                    {state?.configured && <Mark tone="ok">已配置</Mark>}
                                    {ENGINE_KEY_LINKS[activeCloudApi.id] && (
                                      <a
                                        className="inline-link"
                                        href={ENGINE_KEY_LINKS[activeCloudApi.id].href}
                                        onClick={(e) => {
                                          e.preventDefault();
                                          const href = ENGINE_KEY_LINKS[activeCloudApi.id].href;
                                          void Browser.OpenURL(href).catch(() => window.open(href, "_blank", "noopener,noreferrer"));
                                        }}
                                      >
                                        {ENGINE_KEY_LINKS[activeCloudApi.id].label}
                                      </a>
                                    )}
                                  </div>
                                  <div className="onboarding-inline-input-row">
                                    <input
                                      type={isUrl ? "url" : "password"}
                                      autoComplete="off"
                                      spellCheck={false}
                                      placeholder={state?.configured ? "已配置，输入新值可替换" : isUrl ? "https://…" : "粘贴 API Key（也可之后再配）"}
                                      value={drafts[keyName] ?? ""}
                                      onChange={(e) => setDrafts((cur) => ({ ...cur, [keyName]: e.target.value }))}
                                    />
                                    <button
                                      type="button"
                                      className="quiet-button btn-sm"
                                      disabled={keysBusy || !hasApiKeyDraft}
                                      onClick={() => void saveFrom("engine")(activeApiKeyUpdates, `${activeCloudApi.label} 凭据`)}
                                    >
                                      {keysBusy ? "保存中…" : "保存"}
                                    </button>
                                  </div>
                                </div>
                              );
                            })}
                            {keysSource === "engine" && (
                              <Notice
                                className="onboarding-inline-notice"
                                message={keysMessage}
                                tone={keysTone}
                                onDismiss={onDismissKeysMessage}
                              />
                            )}
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                )}
              </div>
            </div>
          )}

          {/* ================= Step 3 安装目录与下载 ================= */}
          {!migrationMode && step === 3 && (
            <div className="onboarding-flow-stack">
              <Notice
                className="onboarding-inline-notice"
                message={provisionMessage}
                tone="warn"
                autoDismissMs={0}
                onDismiss={onDismissProvisionMessage}
              />
              <Notice
                className="onboarding-inline-notice"
                message={storageMessage}
                tone="warn"
                onDismiss={onDismissStorageMessage}
              />

              {/* 正在下载安装中 */}
              {jobRunning && job ? (
                <>
                  <div className="onboarding-progress-box">
                    <div className="onboarding-progress-top">
                      <div>
                        <small>{job.resource ? layerLabel(job.resource) : "运行环境"}</small>
                        <strong>{job.message || "正在准备下载…"}</strong>
                      </div>
                      <b>
                        {jobProgress?.total
                          ? `${Math.round(jobPercent)}%`
                          : job.stage === "verifying" || job.stage === "checking" || job.stage === "offline"
                            ? "校验中"
                            : job.stage === "extracting" || job.stage === "unpacking"
                              ? "解压中"
                              : job.stage === "activating" || job.stage === "runtime" || job.stage === "model"
                                ? "启用中"
                                : "准备中"}
                      </b>
                    </div>

                    <div className={`onboarding-progress-track ${jobProgress?.total ? "" : "indeterminate"}`}>
                      <span style={jobProgress?.total ? { width: `${jobPercent}%` } : undefined} />
                    </div>

                    <div className="onboarding-progress-meta">
                      <span>
                        {jobProgress?.total
                          ? `${formatBytes(jobProgress.completed)} / ${formatBytes(jobProgress.total)}`
                          : job.stage === "verifying" || job.stage === "checking" || job.stage === "offline"
                            ? "正在核对文件完整性，大文件可能需要几十秒"
                            : job.stage === "extracting" || job.stage === "unpacking"
                              ? "正在写入安装目录，请稍候"
                              : job.stage === "activating" || job.stage === "runtime" || job.stage === "model"
                                ? "正在启用已安装的组件"
                                : "正在连接下载源…"}
                      </span>
                      <span>
                        {jobProgress?.bytes_per_second
                          ? `${formatBytes(jobProgress.bytes_per_second)}/s${job.proxy ? ` · ${job.proxy}` : ""}`
                          : job.proxy
                            ? `通过 ${job.proxy}`
                            : ""}
                      </span>
                    </div>

                    <div className="onboarding-phase-strip">
                      {INSTALL_PHASES.map((phase, idx) => (
                        <span
                          key={phase.id}
                          className={idx < jobPhase ? "complete" : idx === jobPhase ? "active" : ""}
                        >
                          <i>{idx < jobPhase ? "✓" : idx + 1}</i>
                          {phase.label}
                        </span>
                      ))}
                    </div>

                    {slowDownload && (
                      <div className="onboarding-slow-banner" role="status">
                        <span>
                          {hardware.runtimeSupported
                            ? "当前在线下载较慢，建议配置网络代理提速，或改用本地离线包（.tar.zst）安装。"
                            : "当前在线下载较慢，建议配置网络代理提速。"}
                        </span>
                        <div className="onboarding-slow-banner-actions">
                          <button type="button" className="quiet-button btn-sm" onClick={() => setProxyModalOpen(true)}>
                            配置网络代理
                          </button>
                          {activeDownloadLayer && (
                            <button
                              type="button"
                              className="accent-button btn-sm"
                              onClick={() => void onInstallOffline(activeDownloadLayer)}
                            >
                              改用离线包…
                            </button>
                          )}
                        </div>
                      </div>
                    )}

                    <div className="onboarding-progress-actions">
                      <div className="onboarding-progress-btns">
                        {!slowDownload && (
                          <button type="button" className="quiet-button btn-sm" onClick={() => setProxyModalOpen(true)}>
                            网络代理
                          </button>
                        )}
                        {!slowDownload && activeDownloadLayer && (
                          <button
                            type="button"
                            className="quiet-button btn-sm"
                            onClick={() => void onInstallOffline(activeDownloadLayer)}
                          >
                            改用离线包…
                          </button>
                        )}
                        <button
                          type="button"
                          className="ghost-button btn-sm"
                          onClick={() => {
                            setChainedTarget(null);
                            void onCancelInstall();
                          }}
                        >
                          取消
                        </button>
                      </div>
                    </div>
                  </div>

                  <div className="onboarding-checklist">
                    {installPlan.layers.map((layer) => {
                      const active = job.resource === layer.id;
                      return (
                        <div
                          key={layer.id}
                          className={`onboarding-check-row state-${layer.state === "ready" ? "done" : active ? "running" : "pending"}`}
                        >
                          <span className="onboarding-check-icon" aria-hidden="true">
                            {layer.state === "ready" ? "✓" : active ? "…" : "○"}
                          </span>
                          <div className="onboarding-check-text">
                            <strong>{layer.label}</strong>
                            <small>{layer.description}</small>
                          </div>
                          <span className="onboarding-check-meta">
                            {layer.state === "ready"
                              ? "已就绪"
                              : active
                                ? "安装中…"
                                : layer.archiveBytes > 0
                                  ? formatStorageBytes(layer.archiveBytes)
                                  : "等待中"}
                          </span>
                        </div>
                      );
                    })}
                  </div>
                </>
              ) : installPlan.allReady ? (
                <>
                  <div className="onboarding-status-card tone-success">
                    <div className="onboarding-status-head">
                      <span className="onboarding-status-badge tone-success">✓</span>
                      <div>
                        <strong>所需运行环境已全部安装就绪</strong>
                        <p>
                          已安装至 <code>{runtimeLocation?.directory ?? provision?.root ?? ""}</code>
                        </p>
                      </div>
                    </div>
                  </div>
                  <div className="onboarding-checklist">
                    {installPlan.layers.map((layer) => (
                      <div key={layer.id} className="onboarding-check-row state-done">
                        <span className="onboarding-check-icon" aria-hidden="true">✓</span>
                        <div className="onboarding-check-text">
                          <strong>{layer.label}</strong>
                          <small>{layer.description}</small>
                        </div>
                        <span className="onboarding-check-meta">
                          {layer.installedBytes > 0 ? formatStorageBytes(layer.installedBytes) : "已就绪"}
                        </span>
                      </div>
                    ))}
                  </div>
                </>
              ) : (
                <>
                  {/* 上次安装中断 / 失败提示 */}
                  {(job?.state === "failed" || job?.state === "cancelled") && (
                    <div className="onboarding-status-card tone-warning">
                      <div className="onboarding-status-head">
                        <span className="onboarding-status-badge tone-warning">!</span>
                        <div>
                          <strong>{job.state === "cancelled" ? "安装已取消" : "环境下载失败"}</strong>
                          <p>{job.message || "网络波动导致下载中断，可重试在线下载（支持断点续传），也可配置代理或改用本地离线包。"}</p>
                        </div>
                      </div>
                      <div className="onboarding-inline-actions">
                        <button
                          type="button"
                          className="primary-button btn-sm"
                          disabled={!runtimeLocation || storageBusy || relocatingRuntime || !installPlan.primaryTarget}
                          onClick={() => {
                            setInstallSource("online");
                            void startInstallPlan();
                          }}
                        >
                          重试下载
                        </button>
                        <button type="button" className="quiet-button btn-sm" onClick={() => setProxyModalOpen(true)}>
                          配置网络代理
                        </button>
                        {hardware.runtimeSupported && (
                          <button
                            type="button"
                            className="quiet-button btn-sm"
                            onClick={() => setInstallSource("offline")}
                          >
                            改用离线包导入
                          </button>
                        )}
                      </div>
                    </div>
                  )}

                  {/* 安装位置选择卡片 */}
                  <div className={`onboarding-dir-card ${tightDisk ? "tight" : ""}`}>
                    <div className="onboarding-dir-top">
                      <div>
                        <strong>安装位置</strong>
                        {runtimeLocation ? (
                          <span className={`onboarding-disk-free ${tightDisk ? "tight" : ""}`}>
                            {runtimeLocation.volume} 剩余 <b>{formatStorageBytes(runtimeLocation.freeBytes)}</b>
                            {tightDisk ? " · 空间紧张，建议换盘" : ""}
                            {runtimeLocation.custom ? "（已自定义）" : ""}
                          </span>
                        ) : (
                          <span className="onboarding-disk-free">正在读取默认位置…</span>
                        )}
                      </div>
                      {runtimeLocation && (
                        <button
                          type="button"
                          className="quiet-button btn-sm"
                          disabled={storageBusy || relocatingRuntime || choosing !== ""}
                          onClick={() => void choose("runtime")}
                        >
                          {choosing === "runtime" ? "选择中…" : "更改位置…"}
                        </button>
                      )}
                    </div>
                    {runtimeLocation && (
                      <div className="onboarding-dir-path" title={runtimeLocation.directory}>
                        <code>{runtimeLocation.directory}</code>
                      </div>
                    )}
                    {pending && (
                      <RelocationConfirm
                        pending={pending}
                        busy={storageBusy}
                        empty={runtimeLocation?.empty ?? true}
                        onConfirm={confirmRelocation}
                        onDismiss={clear}
                      />
                    )}
                    <RelocationProgressView
                      progress={relocatingRuntime ? storageProgress : null}
                      onCancel={() => void onCancelStorage()}
                    />
                  </div>

                  {/* 明确询问用户有无离线包（仅支持预制离线包的平台展示） */}
                  {hardware.runtimeSupported && (
                  <div className="onboarding-section-block">
                    <div className="onboarding-section-head">
                      <span>你是否已准备好本地离线包（.tar.zst）？</span>
                      {installSource === "online" && installPlan.missingDownloadBytes > 0 && (
                        <Mark tone="accent">在线预计下载 {formatStorageBytes(installPlan.missingDownloadBytes)}</Mark>
                      )}
                    </div>
                    <div className="onboarding-source-grid" role="radiogroup" aria-label="获取方式">
                      <button
                        type="button"
                        role="radio"
                        aria-checked={installSource === "online"}
                        className={`onboarding-source-card ${installSource === "online" ? "selected" : ""}`}
                        onClick={() => setInstallSource("online")}
                      >
                        <span className="onboarding-radio" aria-hidden="true" />
                        <div>
                          <strong>没有离线包，在线下载</strong>
                          <small>自动拉取并校验所需组件，支持断点续传</small>
                        </div>
                      </button>
                      <button
                        type="button"
                        role="radio"
                        aria-checked={installSource === "offline"}
                        className={`onboarding-source-card ${installSource === "offline" ? "selected" : ""}`}
                        onClick={() => setInstallSource("offline")}
                      >
                        <span className="onboarding-radio" aria-hidden="true" />
                        <div>
                          <strong>已有离线包，本地导入</strong>
                          <small>直接选择本地 .tar.zst 压缩包逐层安装</small>
                        </div>
                      </button>
                    </div>
                  </div>
                  )}

                  {/* 组件清单（在线模式展示体积；离线模式每层直接带导入按钮） */}
                  <div className="onboarding-checklist">
                    {installPlan.layers.map((layer) => {
                      const overview = layerOverviewOf(provision, layer.id);
                      return (
                        <div
                          key={layer.id}
                          className={`onboarding-check-row state-${layer.state === "ready" ? "done" : "pending"}`}
                        >
                          <span className="onboarding-check-icon" aria-hidden="true">
                            {layer.state === "ready" ? "✓" : "↓"}
                          </span>
                          <div className="onboarding-check-text">
                            <strong>{layer.label}</strong>
                            <small>
                              {installSource === "offline" && overview?.version
                                ? `所需版本：${overview.version} · ${layer.description}`
                                : layer.description}
                            </small>
                          </div>
                          {layer.state === "ready" ? (
                            <span className="onboarding-check-meta">已安装</span>
                          ) : installSource === "offline" ? (
                            <button
                              type="button"
                              className="accent-button btn-sm"
                              disabled={storageBusy || relocatingRuntime}
                              onClick={() => void onInstallOffline(layer.id)}
                            >
                              选择离线包…
                            </button>
                          ) : (
                            <span className="onboarding-check-meta">
                              {layer.archiveBytes > 0 ? `约 ${formatStorageBytes(layer.archiveBytes)}` : "待下载"}
                            </span>
                          )}
                        </div>
                      );
                    })}
                  </div>
                </>
              )}
            </div>
          )}

          {/* ================= Step 4 纠错与翻译配置 & 完成 ================= */}
          {!migrationMode && step === 4 && (
            <div className="onboarding-flow-stack">
              <div className="onboarding-summary-strip">
                <div className="onboarding-summary-pill">
                  <small>默认转写引擎</small>
                  <b>{transcriptionEngineLabel(transcriptionEngine)}</b>
                </div>
                <div className="onboarding-summary-pill">
                  <small>基础运行环境</small>
                  <b className="is-ok">已就绪 ✓</b>
                </div>
                <div className="onboarding-summary-pill">
                  <small>AI 纠错与翻译</small>
                  <b className={correctorReady ? "is-ok" : ""}>{correctorReady ? "已配置 ✓" : "可选 · 未配置"}</b>
                </div>
              </div>

              {cloudSession?.authenticated && (
                <div className="onboarding-cloud-tip">
                  <Mark tone="accent">Nonoka Cloud 已登录</Mark>
                  <span>可在下方提供商中直接选用 Nonoka Cloud 进行纠错翻译，也可填写自己的大模型 API Key。</span>
                </div>
              )}

              {settings && modelRouting ? (
                <div className="onboarding-llm-embed">
                  <LlmConfigurationCard
                    keys={keys}
                    baseUrls={baseUrls}
                    modelRouting={modelRouting}
                    searchKeys={searchKeys}
                    correctorIssue={correctorIssue}
                    drafts={drafts}
                    busy={keysBusy}
                    message={keysSource === "llm" ? keysMessage : ""}
                    messageTone={keysTone}
                    onDismissMessage={onDismissKeysMessage}
                    setDrafts={setDrafts}
                    onSave={saveFrom("llm")}
                  />
                </div>
              ) : (
                <div className="onboarding-status-card tone-info">
                  <div className="onboarding-status-head">
                    <span className="onboarding-status-badge tone-info">i</span>
                    <div>
                      <strong>模型配置服务正在同步</strong>
                      <p>你现在可以直接进入应用，之后随时在「设置 → 纠错与翻译」中配置。</p>
                    </div>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>

        {/* 底部操作栏 */}
        <footer className="onboarding-footer">
          {migrationMode ? (
            <>
              <div className="onboarding-footer-hint">
                {migrationStep === 1 && (migrationView.polling ? "请勿关闭窗口，迁移完成后会自动进入下一步" : "排除上方原因后点击重试")}
                {migrationStep === 2 && (migrationView.polling ? "正在自动补齐新版基础运行环境…" : "检查网络或代理设置后点击重试")}
                {migrationStep === 3 && "升级已完成，随时可在设置中调整偏好"}
              </div>
              <div className="onboarding-footer-actions">
                {migrationView.canRetry && (
                  <button
                    type="button"
                    className="primary-button"
                    disabled={migrationRetrying}
                    onClick={onRetryMigration}
                  >
                    {migrationRetrying ? "正在重试…" : "重试"}
                  </button>
                )}
                {migrationStep === 3 && (
                  <button type="button" className="primary-button" onClick={onComplete}>
                    开始使用 Nonoka Sub X
                  </button>
                )}
              </div>
            </>
          ) : step === 1 ? (
            <>
              <div className="onboarding-footer-hint">
                {serviceReady ? "一切就绪，约需 1～2 分钟完成配置" : pythonInstalling ? "正在自动准备启动环境…" : "等待本地服务就绪"}
              </div>
              <div className="onboarding-footer-actions">
                <button
                  type="button"
                  className="primary-button"
                  disabled={!serviceReady}
                  onClick={() => setStep(2)}
                >
                  开始配置 →
                </button>
              </div>
            </>
          ) : step === 2 ? (
            <>
              <button type="button" className="quiet-button" onClick={() => setStep(1)}>
                ← 上一步
              </button>
              <div className="onboarding-footer-actions">
                <button type="button" className="primary-button" onClick={handleModeNext}>
                  下一步：安装环境 →
                </button>
              </div>
            </>
          ) : step === 3 ? (
            <>
              {jobRunning ? (
                <span className="onboarding-footer-hint">
                  {baseReady ? "基础环境已就绪，AI 模型正在后台继续下载" : "基础环境安装完成后即可继续"}
                </span>
              ) : (
                <button type="button" className="quiet-button" onClick={() => setStep(2)}>
                  ← 上一步
                </button>
              )}
              <div className="onboarding-footer-actions">
                {jobRunning ? (
                  baseReady && (
                    <button type="button" className="primary-button" onClick={() => setStep(4)}>
                      后台继续下载，下一步 →
                    </button>
                  )
                ) : installPlan.allReady ? (
                  <button type="button" className="primary-button" onClick={() => setStep(4)}>
                    下一步：纠错与翻译 →
                  </button>
                ) : (
                  <>
                    {baseReady && (
                      <button type="button" className="quiet-button" onClick={() => setStep(4)}>
                        稍后再下模型，下一步 →
                      </button>
                    )}
                    {installSource === "online" ? (
                      <button
                        type="button"
                        className="primary-button"
                        disabled={!runtimeLocation || storageBusy || relocatingRuntime || !installPlan.primaryTarget}
                        onClick={() => void startInstallPlan()}
                      >
                        {relocatingRuntime
                          ? "正在更改目录…"
                          : job?.state === "failed" || job?.state === "cancelled"
                            ? "重试在线下载"
                            : installPlan.missingDownloadBytes > 0
                              ? `开始在线下载（约 ${formatStorageBytes(installPlan.missingDownloadBytes)}）`
                              : "开始在线下载"}
                      </button>
                    ) : (
                      <button
                        type="button"
                        className="quiet-button"
                        disabled={!runtimeLocation || storageBusy || relocatingRuntime || !installPlan.primaryTarget}
                        onClick={() => {
                          setInstallSource("online");
                          void startInstallPlan();
                        }}
                      >
                        在线补齐剩余项
                      </button>
                    )}
                  </>
                )}
              </div>
            </>
          ) : (
            <>
              <button type="button" className="quiet-button" onClick={() => setStep(3)}>
                ← 上一步
              </button>
              <div className="onboarding-footer-actions">
                <button type="button" className="primary-button" onClick={onComplete}>
                  {correctorReady ? "完成配置，进入 Nonoka Sub X" : "跳过并进入 Nonoka Sub X"}
                </button>
              </div>
            </>
          )}
        </footer>
      </section>

      {/* 内置轻量代理弹窗 */}
      {proxyModalOpen && (
        <div className="onboarding-modal-backdrop" role="presentation" onClick={() => setProxyModalOpen(false)}>
          <div
            className="onboarding-modal panel"
            role="dialog"
            aria-modal="true"
            aria-labelledby="onboarding-proxy-title"
            onClick={(e) => e.stopPropagation()}
          >
            <header className="onboarding-modal-header">
              <div>
                <h3 id="onboarding-proxy-title">网络代理设置</h3>
                <p>影响启动器、运行环境与模型下载的线路选择</p>
              </div>
              <button
                type="button"
                className="onboarding-modal-close"
                aria-label="关闭"
                onClick={() => setProxyModalOpen(false)}
              >
                ×
              </button>
            </header>
            <div className="onboarding-modal-body">
              <ProxySettingsCard embedded />
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
