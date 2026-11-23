import type { MigrationState, MigrationStatus } from "../bridge/migration.ts";
import type { RuntimeInstallTarget, RuntimeLayerId, RuntimeLayerOverview, RuntimeProvisionState } from "../bridge/runtime.ts";
import type { Capabilities } from "../providers/types.ts";
import { CLOUD_TRANSCRIPTION_ENGINE, DEFAULT_TRANSCRIPTION_ENGINE } from "../providers/types.ts";

export type OnboardingStep = 1 | 2 | 3 | 4;
export type MigrationWizardStep = 1 | 2 | 3;
export type OnboardingMode = "local" | "cloud" | "cloud-api";
export type LocalEngineId = "finesub" | "moss-local";

export interface PlannedLayerItem {
  id: RuntimeLayerId;
  label: string;
  description: string;
  state: RuntimeLayerOverview["state"];
  archiveBytes: number;
  installedBytes: number;
  requiredForBase: boolean;
}

export interface OnboardingInstallPlan {
  layers: PlannedLayerItem[];
  missingLayers: RuntimeLayerId[];
  missingDownloadBytes: number;
  totalEstimatedBytes: number;
  allReady: boolean;
  primaryTarget: RuntimeInstallTarget | null;
  chainedTarget: RuntimeInstallTarget | null;
}

export interface HardwareSummary {
  runtimeSupported: boolean;
  gpuName: string;
  gpuMemoryMb: number;
  hasGpu: boolean;
  mossGpuReady: boolean;
  recommendedMode: OnboardingMode;
}

export interface FriendlyError {
  title: string;
  hint: string;
  detail: string;
  suggestProxy: boolean;
}

const LAYER_META: Record<RuntimeLayerId, { label: string; description: string }> = {
  tools: {
    label: "工具层",
    description: "FFmpeg、FFprobe 与视频处理工具（基础必需）",
  },
  python: {
    label: "Python 环境层",
    description: "隔离的 Python 3.12 / PyTorch / CUDA 推理环境（本机必需）",
  },
  models: {
    label: "FineSub AI 模型层",
    description: "人声分离、Whisper 转写、Qwen 复核与说话人分离模型",
  },
  "models-moss": {
    label: "MOSS 0.9B 模型层",
    description: "MOSS-Transcribe-Diarize 语音识别与说话人声纹模型",
  },
  "screen-text": {
    label: "文字识别层",
    description: "识别画面上的字、抹掉原字用的运行环境与模型（按需安装）",
  },
};

export function layerOverviewOf(provision: RuntimeProvisionState | null, id: RuntimeLayerId): RuntimeLayerOverview | undefined {
  return provision?.layers?.find((layer) => layer.id === id);
}

export function layerStateOf(provision: RuntimeProvisionState | null, id: RuntimeLayerId): RuntimeLayerOverview["state"] {
  const overview = layerOverviewOf(provision, id);
  if (overview) return overview.state;
  if (id === "tools") return provision?.media_ready ? "ready" : "missing";
  if (id === "python") {
    const state = provision?.runtime?.state;
    if (state === "ready") return "ready";
    if (state === "outdated") return "outdated";
    if (state === "failed") return "damaged";
    return "missing";
  }
  if (id === "models") {
    const models = provision?.models ?? [];
    if (models.length > 0 && models.every((item) => item.state === "ready")) return "ready";
    if (models.some((item) => item.state === "outdated")) return "outdated";
    if (models.some((item) => item.state === "failed")) return "damaged";
    return "missing";
  }
  const moss = provision?.moss_models ?? [];
  if (moss.length > 0 && moss.every((item) => item.state === "ready")) return "ready";
  if (moss.some((item) => item.state === "outdated")) return "outdated";
  if (moss.some((item) => item.state === "failed")) return "damaged";
  return "missing";
}

/**
 * 旧版迁移在引导页里接管展示：除 `not-needed` 外均进入迁移流程，
 * `completed` 需等待用户点击「进入应用」后关闭。
 */
export function isMigrationWizardActive(migration: MigrationStatus | null, dismissed: boolean): boolean {
  if (!migration || migration.state === "not-needed") return false;
  if (migration.state === "completed") return !dismissed;
  return true;
}

const MIGRATION_DATA_STATES = new Set<MigrationState>(["checking", "migrating", "blocked", "rolled-back", "failed"]);
const MIGRATION_DOWNLOAD_STATES = new Set<MigrationState>(["downloading", "download-failed"]);

export function migrationWizardStep(migration: MigrationStatus | null): MigrationWizardStep {
  if (!migration || MIGRATION_DATA_STATES.has(migration.state)) return 1;
  if (MIGRATION_DOWNLOAD_STATES.has(migration.state)) return 2;
  return 3;
}

/**
 * 判断老用户：已完成引导、媒体库已有条目、已登录云端，或基础环境已就绪时，
 * 静默放行，不打扰老用户（旧版数据迁移进行中除外）。
 */
export function shouldAutoCompleteExistingUser(input: {
  onboardingCompleted: boolean;
  mediaCount: number;
  cloudAuthenticated: boolean;
  provision: RuntimeProvisionState | null;
  migration: MigrationStatus | null;
}): boolean {
  if (input.migration && input.migration.state !== "not-needed") return false;
  if (input.onboardingCompleted) return true;
  if (input.mediaCount > 0 || input.cloudAuthenticated) return true;
  if (!input.provision || input.provision.bootstrap_error) return false;
  const toolsReady = !input.provision.media_supported || layerStateOf(input.provision, "tools") === "ready";
  const pythonReady = !input.provision.runtime_supported || layerStateOf(input.provision, "python") === "ready";
  return toolsReady && pythonReady;
}

/**
 * 基础环境是否已就绪（用于控制是否允许跳过引导或提前进入主界面）：
 * - 本机模式（支持本机 GPU 平台）：工具层 + Python 环境层都必须就绪
 * - 云端 / 云 API 模式（或不支持本机 GPU 的平台）：工具层必须就绪
 */
export function isBaseEnvironmentReady(
  mode: OnboardingMode,
  provision: RuntimeProvisionState | null,
  sidecarRunning: boolean,
): boolean {
  if (!sidecarRunning || !provision || provision.bootstrap_error) return false;
  const toolsReady = !provision.media_supported || layerStateOf(provision, "tools") === "ready";
  if (!toolsReady) return false;
  if (mode === "local" && provision.runtime_supported) {
    return layerStateOf(provision, "python") === "ready";
  }
  return true;
}

export function planOnboardingInstall(input: {
  mode: OnboardingMode;
  localEngine: LocalEngineId;
  includeModels: boolean;
  provision: RuntimeProvisionState | null;
}): OnboardingInstallPlan {
  const { mode, localEngine, includeModels, provision } = input;
  const runtimeSupported = provision?.runtime_supported ?? true;
  const mediaSupported = provision?.media_supported ?? true;

  const ids: RuntimeLayerId[] = [];
  if (mediaSupported) ids.push("tools");
  if (mode === "local" && runtimeSupported) {
    ids.push("python");
    if (includeModels) {
      const modelLayer: RuntimeLayerId = localEngine === "moss-local" ? "models-moss" : "models";
      const indexed = modelLayer === "models" || Boolean(layerOverviewOf(provision, "models-moss"));
      if (indexed) ids.push(modelLayer);
    }
  }

  const layers: PlannedLayerItem[] = ids.map((id) => {
    const overview = layerOverviewOf(provision, id);
    const state = layerStateOf(provision, id);
    const meta = LAYER_META[id];
    return {
      id,
      label: meta.label,
      description: meta.description,
      state,
      archiveBytes: overview?.archive_bytes ?? 0,
      installedBytes: overview?.bytes ?? 0,
      requiredForBase: id === "tools" || (id === "python" && mode === "local" && runtimeSupported),
    };
  });

  const missingLayers = layers.filter((layer) => layer.state !== "ready").map((layer) => layer.id);
  const missingDownloadBytes = layers
    .filter((layer) => layer.state !== "ready")
    .reduce((sum, layer) => sum + layer.archiveBytes, 0);
  const totalEstimatedBytes = layers.reduce((sum, layer) => sum + (layer.archiveBytes || layer.installedBytes), 0);

  const hasToolsMissing = missingLayers.includes("tools");
  const hasPythonMissing = missingLayers.includes("python");
  const hasModelsMissing = missingLayers.includes("models");
  const hasMossMissing = missingLayers.includes("models-moss");

  let primaryTarget: RuntimeInstallTarget | null = null;
  let chainedTarget: RuntimeInstallTarget | null = null;

  if (missingLayers.length > 0) {
    if (mode === "local" && runtimeSupported) {
      if (hasToolsMissing || hasPythonMissing) {
        if (hasModelsMissing) {
          primaryTarget = "all";
        } else {
          primaryTarget = "runtime";
          if (hasMossMissing) chainedTarget = "models-moss";
        }
      } else if (hasModelsMissing) {
        primaryTarget = "models";
      } else if (hasMossMissing) {
        primaryTarget = "models-moss";
      }
    } else if (hasToolsMissing) {
      primaryTarget = "media";
    }
  }

  return {
    layers,
    missingLayers,
    missingDownloadBytes,
    totalEstimatedBytes,
    allReady: missingLayers.length === 0,
    primaryTarget,
    chainedTarget,
  };
}

export function detectHardware(
  capabilities: Capabilities | null,
  provision: RuntimeProvisionState | null,
): HardwareSummary {
  const runtimeSupported = provision?.runtime_supported ?? true;
  const gpu = capabilities?.devices?.find((device) => device.id !== "cpu") ?? null;
  const hasGpu = gpu !== null;
  const gpuMemoryMb = gpu?.memory_mb ?? 0;
  return {
    runtimeSupported,
    gpuName: gpu?.name ?? "",
    gpuMemoryMb,
    hasGpu,
    mossGpuReady: hasGpu && gpuMemoryMb >= 7500,
    recommendedMode: runtimeSupported && hasGpu ? "local" : "cloud",
  };
}

export function targetEngineForMode(
  mode: OnboardingMode,
  localEngine: LocalEngineId,
  cloudApiEngine: string,
): string {
  if (mode === "cloud") return CLOUD_TRANSCRIPTION_ENGINE;
  if (mode === "cloud-api") return cloudApiEngine || "moss-pro";
  return localEngine || DEFAULT_TRANSCRIPTION_ENGINE;
}

export function friendlyBootstrapError(rawMessage: string): FriendlyError {
  const detail = rawMessage.trim();
  const lower = detail.toLowerCase();
  if (
    lower.includes("access is denied")
    || lower.includes("sharing violation")
    || lower.includes("being used by another process")
    || lower.includes("permission denied")
  ) {
    return {
      title: "文件被临时占用，无法完成启用",
      hint: "通常是 Windows Defender 或安全软件正在扫描刚解压的启动器，或旧进程尚未完全退出。稍等几秒后点击「重试初始化」即可恢复。",
      detail,
      suggestProxy: false,
    };
  }
  if (
    lower.includes("timeout")
    || lower.includes("timed out")
    || lower.includes("eof")
    || lower.includes("connection")
    || lower.includes("tls")
    || lower.includes("no such host")
    || lower.includes("proxy")
    || lower.includes("github")
    || lower.includes("下载")
    || lower.includes("线路")
  ) {
    return {
      title: "网络连接受阻，未能下载启动环境",
      hint: "无法稳定连接到下载源。请检查网络连接，或点击左下角「网络代理」配置代理后再重试。",
      detail,
      suggestProxy: true,
    };
  }
  if (lower.includes("no space") || lower.includes("disk full") || lower.includes("空间不足")) {
    return {
      title: "系统盘空间不足",
      hint: "启动环境需要约 150 MB 系统盘空间，请释放少量空间后再点击重试。",
      detail,
      suggestProxy: false,
    };
  }
  return {
    title: "准备本地服务时遇到问题",
    hint: "请检查网络连接或安全软件拦截提示，随后点击「重试初始化」。",
    detail,
    suggestProxy: true,
  };
}
