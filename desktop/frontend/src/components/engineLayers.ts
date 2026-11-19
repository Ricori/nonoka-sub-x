import type { RuntimeInstallTarget, RuntimeLayerOverview, RuntimeProvisionState } from "../bridge/runtime.ts";
import type { EngineLayers } from "../providers/types.ts";

/** 三个运行环境层的名字，按安装顺序（`nonoka_x.runtime_install.INSTALL_ORDER`）。 */
export const LAYER_ORDER = ["tools", "python", "models"] as const;

export const LAYER_LABELS: Record<string, string> = {
  python: "Python 环境层",
  models: "模型层",
  tools: "工具层",
};

export const layerLabel = (layer: string) => LAYER_LABELS[layer] ?? layer;

function ordered(layers: Iterable<string>): string[] {
  const wanted = new Set(layers);
  return [...LAYER_ORDER.filter((layer) => wanted.has(layer)), ...[...wanted].filter((layer) => !(LAYER_ORDER as readonly string[]).includes(layer))];
}

export interface EngineLayerStatus {
  /** 读得到引擎要的层和各层的状态；旧版 sidecar、服务未连接、平台没有预制层时为 false。 */
  known: boolean;
  required: string[];
  /** 要的层里还没装好（未装、要更新或损坏）的。 */
  missing: string[];
  /** 装上缺的层要下载的量（压缩包之和）。 */
  downloadBytes: number;
  /** 要的层里已装的部分占的空间。 */
  installedBytes: number;
}

/** 一个引擎要的层现在装得怎样：缺哪几层、要下多少、已占多少（WP-UI）。 */
export function engineLayerStatus(layers: EngineLayers | undefined, provision: RuntimeProvisionState | null): EngineLayerStatus {
  const overview = provision?.layers;
  if (!layers || !overview) {
    return { known: false, required: layers ? ordered(layers.required) : [], missing: [], downloadBytes: 0, installedBytes: 0 };
  }
  const byId = new Map<string, RuntimeLayerOverview>(overview.map((item) => [item.id, item]));
  const required = ordered(layers.required);
  // 这台机器上压根没有的层（平台不提供）也算缺，只是不知道要下多少。
  const missing = required.filter((layer) => byId.get(layer)?.state !== "ready");
  return {
    known: true,
    required,
    missing,
    downloadBytes: missing.reduce((sum, layer) => sum + (byId.get(layer)?.archive_bytes ?? 0), 0),
    installedBytes: required.reduce((sum, layer) => {
      const item = byId.get(layer);
      return sum + (item && item.current !== null ? item.bytes : 0);
    }, 0),
  };
}

/**
 * 只装这几层要用的安装目标：能盖住它们的最小一个。安装任务本身只下没装好的层
 * （`provision._run`），所以选「全部」也不会重下已经装好的工具层。
 */
export function installTargetFor(missing: string[]): RuntimeInstallTarget | null {
  if (!missing.length) return null;
  const within = (allowed: string[]) => missing.every((layer) => allowed.includes(layer));
  if (within(["tools"])) return "media";
  if (within(["tools", "python"])) return "runtime";
  if (within(["models"])) return "models";
  return "all";
}

export interface EngineInstallView {
  target: RuntimeInstallTarget;
  /** 要下载的层与各自的压缩包大小（不知道时为 0）。 */
  layers: Array<{ id: string; label: string; bytes: number }>;
  total: number;
  /** 层装在哪（大文件根）。 */
  root: string;
  /** 已有安装任务在跑（进度在「运行环境」里）。 */
  running: boolean;
  /** 上一次装这些层失败了：按钮就是重试。 */
  failed: boolean;
  /** 非空时装不了，写着原因。 */
  blocked: string;
}

/** 选中一个缺层的引擎时，「只装它缺的层」那一块要显示什么；不缺、读不到或是云端 FineSub 时为 null。 */
export function engineInstallView(choice: { kind: string; layers?: EngineLayers }, provision: RuntimeProvisionState | null): EngineInstallView | null {
  if (choice.kind === "cloud-service" || !provision) return null;
  const status = engineLayerStatus(choice.layers, provision);
  const target = installTargetFor(status.missing);
  if (!status.known || target === null) return null;
  const byId = new Map<string, RuntimeLayerOverview>((provision.layers ?? []).map((item) => [item.id, item]));
  const layers = status.missing.map((layer) => ({ id: layer, label: layerLabel(layer), bytes: byId.get(layer)?.archive_bytes ?? 0 }));
  const needsRuntime = status.missing.some((layer) => layer !== "tools");
  const blocked = provision.bootstrap_error
    ? `运行环境层不可用：${provision.bootstrap_error}`
    : needsRuntime && !provision.runtime_supported
      ? `${provision.platform} 上没有预制的 Python 环境层与模型层`
      : status.missing.includes("tools") && !provision.media_supported
        ? `${provision.platform} 上还没有工具层`
        : "";
  return {
    target,
    layers,
    total: status.downloadBytes,
    root: provision.root,
    running: provision.job.state === "running",
    failed: provision.job.state === "failed" && provision.job.target === target,
    blocked,
  };
}

export interface UsageOwner {
  id: string;
  label: string;
  layers?: EngineLayers;
}

export interface UsageLayer {
  id: string;
  label: string;
  bytes: number;
  /** 也用这一层的其他引擎（标签）。 */
  sharedWith: string[];
}

export interface UsageRow {
  id: string;
  label: string;
  /** 已装的、这个引擎要的层。 */
  layers: UsageLayer[];
  bytes: number;
}

export interface Usage {
  rows: UsageRow[];
  /** 所有已装的层，每层只算一次。 */
  total: number;
}

/** 按引擎列出各自占的空间：共用的层在每个用它的引擎下都列出、标明被谁共用，合计只算一次。 */
export function layerUsage(owners: UsageOwner[], provision: RuntimeProvisionState | null): Usage | null {
  const overview = provision?.layers;
  if (!overview) return null;
  const installed = new Map<string, RuntimeLayerOverview>(overview.filter((item) => item.current !== null && item.bytes > 0).map((item) => [item.id, item]));
  const users = new Map<string, string[]>();
  for (const owner of owners) {
    for (const layer of owner.layers?.required ?? []) {
      users.set(layer, [...(users.get(layer) ?? []), owner.label]);
    }
  }
  const rows = owners.filter((owner) => owner.layers).map((owner): UsageRow => {
    const layers = ordered(owner.layers?.required ?? []).filter((layer) => installed.has(layer)).map((layer) => ({
      id: layer,
      label: layerLabel(layer),
      bytes: installed.get(layer)?.bytes ?? 0,
      sharedWith: (users.get(layer) ?? []).filter((label) => label !== owner.label),
    }));
    return { id: owner.id, label: owner.label, layers, bytes: layers.reduce((sum, layer) => sum + layer.bytes, 0) };
  });
  return { rows, total: [...installed.values()].reduce((sum, item) => sum + item.bytes, 0) };
}
