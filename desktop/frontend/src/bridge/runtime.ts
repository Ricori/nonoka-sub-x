import * as ProviderService from "../../bindings/github.com/Ricori/nonoka-x/desktop/internal/provider/service.js";

export interface RuntimeItem {
  id: string;
  version?: string;
  state: "missing" | "downloading" | "outdated" | "ready" | "failed";
  detail?: string;
  source?: "managed" | "system";
}

/** 每个目标对应它要的运行环境层（`nonoka_x.runtime_install.TARGET_LAYERS`）：装任何一个工具都是装整个工具层。
    `repair` 对已装的层做完整核对（逐文件 sha256），有问题的整层重新安装。 */
export type RuntimeInstallTarget = "media" | "runtime" | "models" | "all" | "optional-models" | "repair" | "yt-dlp" | "tokcount" | "aria2c" | "node" | "pot-provider" | "video-tools";
/** 一个运行环境层的状态（`LayerManager.overview()`），给设置页的「清理未使用的」用。 */
export interface RuntimeLayerOverview {
  id: "python" | "models" | "tools";
  label: string;
  state: "ready" | "missing" | "outdated" | "damaged";
  /** 这个版本要的层版本。 */
  version: string;
  /** current.json 指向的版本。 */
  current: string | null;
  path: string | null;
  problems: string[];
  /** 清单以外多出来的文件（不影响就绪，修复时清掉）。 */
  extra: string[];
  checked: "quick" | "full";
  bytes: number;
  archive_bytes: number;
}

/** 不是当前、也不是这个版本要的层版本，或中断留下的目录。 */
export interface RuntimeUnusedLayer {
  layer: string;
  name: string;
  path: string;
  bytes: number;
}

/** 「清理未使用的」要删的一项：当前引擎和纠错包都用不到的层（`unneeded`，整层删），或没人用的旧版本。 */
export interface RuntimeCleanupItem extends RuntimeUnusedLayer {
  reason: "old-version" | "unneeded";
  /** 非空时这一项不删，写着原因（有未结束的任务用到它）。 */
  blocked: string;
}

/** 「清理未使用的」现在会删什么（`GET /v1/runtime/provision/unused?engine=`，WP-UI）。 */
export interface RuntimeCleanupPlan {
  schema: 1;
  engine: string | null;
  /** 当前引擎在这台 sidecar 上是否存在；不存在时只列旧版本，不整层删。 */
  engine_available: boolean;
  /** 为当前引擎、纠错包与媒体探测留下的层；为 null 时一层都不整层删。 */
  needed: string[] | null;
  /** 有未结束任务可能用到的层。 */
  busy: string[];
  items: RuntimeCleanupItem[];
  /** 能腾出的空间（不含被占用而留下的）。 */
  bytes: number;
  blocked_bytes: number;
}

/** 「清理未使用的」的结果：删掉的、因占用或任务未结束而留下的，以及腾出的字节数。 */
export interface RuntimeUnusedRemoval {
  schema: 1;
  removed: RuntimeUnusedLayer[];
  kept: Array<RuntimeUnusedLayer & { reason: string }>;
  freed: number;
}

export interface RuntimeProvisionState {
  schema: 1;
  platform: string;
  supported: boolean;
  runtime_supported: boolean;
  media_supported: boolean;
  media_ready: boolean;
  /** Non-empty when the sidecar could not construct its layer manager (for
      example layers.json is unreadable): every install target is unavailable
      until it is resolved. */
  bootstrap_error?: string;
  root: string;
  runtime: RuntimeItem;
  resources: RuntimeItem[];
  models: RuntimeItem[];
  /** 只有部分任务用得到的模型（目前是说话人分离的两个），可单独下载，从不阻断转写。 */
  optional_models?: RuntimeItem[];
  /** 三层各自的状态；旧版 sidecar 没有这几个字段。 */
  layers?: RuntimeLayerOverview[];
  unused_layers?: RuntimeUnusedLayer[];
  unused_bytes?: number;
  job: {
    state: "idle" | "running" | "completed" | "cancelled" | "failed";
    target: string;
    resource: string;
    stage: string;
    message: string;
    progress: { completed: number; total: number; unit: string; bytes_per_second?: number } | null;
    error: { code: string; message: string } | null;
    /** 本次安装下载所经过的代理，例如“本地代理 127.0.0.1:7890”；直连时为空。 */
    proxy?: string;
  };
}

/** 运行环境缓存的占用：还没装完的层压缩包（`<大文件根>\cache\downloads\`）与转写引擎的编译缓存（numba），都可以清掉。 */
export interface RuntimeCacheStatus {
  schema: 1;
  bytes: number;
  /** 删掉能腾出的空间（不算硬链接到别处的文件）。 */
  reclaimable: number;
  files: number;
}

export interface RuntimeCacheClearResult extends RuntimeCacheStatus {
  freed: number;
  removed: number;
  /** 未能删除的条目，通常是被占用的文件。 */
  failures: string[];
}

export interface PythonBootstrapState {
  schema: 1;
  platform: string;
  supported: boolean;
  state: "missing" | "running" | "ready" | "failed" | "unavailable";
  stage: string;
  message: string;
  python?: string;
  /** 本次安装下载所经过的代理；直连时为空。 */
  proxy?: string;
  progress: { completed: number; total: number; unit: string; bytes_per_second?: number } | null;
}

export const fineSubRuntime = {
  pythonStatus: () => ProviderService.PythonBootstrapStatus() as Promise<unknown> as Promise<PythonBootstrapState>,
  installPython: () => ProviderService.InstallPython() as Promise<unknown> as Promise<PythonBootstrapState>,
  status: () => ProviderService.RuntimeProvisionStatus() as Promise<unknown> as Promise<RuntimeProvisionState>,
  install: (target: RuntimeInstallTarget) => ProviderService.InstallRuntime(target) as Promise<unknown> as Promise<RuntimeProvisionState>,
  cancel: () => ProviderService.CancelRuntimeInstall() as Promise<unknown> as Promise<RuntimeProvisionState>,
  removeAll: () => ProviderService.RemoveRuntime() as Promise<unknown> as Promise<RuntimeProvisionState>,
  /** 以当前转写引擎为准；sidecar 不认识的引擎只列旧版本。 */
  cleanupPlan: (engine: string) => ProviderService.UnusedRuntime(engine) as Promise<unknown> as Promise<RuntimeCleanupPlan>,
  removeUnused: (engine: string) => ProviderService.RemoveUnusedRuntime(engine) as Promise<unknown> as Promise<RuntimeUnusedRemoval>,
  cacheStatus: () => ProviderService.RuntimeCacheStatus() as Promise<unknown> as Promise<RuntimeCacheStatus>,
  clearCache: () => ProviderService.ClearRuntimeCache() as Promise<unknown> as Promise<RuntimeCacheClearResult>,
};
