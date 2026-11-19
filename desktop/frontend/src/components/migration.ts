import type { MigrationReport, MigrationState, MigrationStatus } from "../bridge/migration.ts";

/**
 * 旧版数据迁移（WP-M2）在界面上的样子。sidecar 启动时先迁移：本地步骤做完之前
 * 数据不打开，除迁移接口外一律答 503 `migrating`——那不是 sidecar 坏了，而是它在忙；
 * 之后在后台下载新的运行环境，FineSub 转写要等下载完成。
 */

/** sidecar 答 503 `migrating` 时 Go 那边的错误文字（`sidecar.HTTPError.Error()`）。 */
const migratingError = /\bsidecar migrating \(HTTP 503\)/;

export function isMigratingError(value: unknown): boolean {
  const text = value instanceof Error ? value.message : typeof value === "object" && value !== null && "message" in value
    ? String((value as { message: unknown }).message)
    : String(value ?? "");
  return migratingError.test(text);
}

/** 这些状态下要一直问迁移进度。 */
const pollingStates = new Set<MigrationState>(["checking", "migrating", "downloading"]);
/** 这些状态下有「重试」。 */
const retryStates = new Set<MigrationState>(["blocked", "rolled-back", "failed", "download-failed"]);

export const migrationPollIntervalMs = 1_000;

export interface MigrationView {
  /** 要不要显示迁移面板。 */
  visible: boolean;
  /** 数据还没打开：整个应用除了这块面板都用不了。 */
  blocking: boolean;
  tone: "info" | "warning" | "error" | "success";
  heading: string;
  message: string;
  polling: boolean;
  canRetry: boolean;
  /** 完成后的结果卡片可以关掉；数据没打开或还在下载时不行。 */
  dismissible: boolean;
  /** 「第 3 / 9 步：…」；没有步骤时为空。 */
  stepLine: string;
  /** 下载进度，0–100；不在下载或总量未知时为 null。 */
  percent: number | null;
  progressLine: string;
  error: string;
  blockers: string[];
  /** 结果里要告诉用户的几组名字，空的组不列。 */
  sections: { title: string; items: string[] }[];
}

const headings: Record<MigrationState, string> = {
  checking: "正在检查旧版数据",
  "not-needed": "",
  migrating: "正在迁移旧版数据",
  blocked: "旧版数据暂时无法迁移",
  "rolled-back": "迁移失败，已恢复原样",
  failed: "迁移中断",
  downloading: "正在下载新的运行环境",
  "download-failed": "运行环境下载失败",
  completed: "旧版数据已迁移完成",
};

const tones: Record<MigrationState, MigrationView["tone"]> = {
  checking: "info",
  "not-needed": "info",
  migrating: "info",
  blocked: "warning",
  "rolled-back": "error",
  failed: "error",
  downloading: "info",
  "download-failed": "error",
  completed: "success",
};

const units = ["B", "KB", "MB", "GB", "TB"];

export function formatBytes(value: number): string {
  let size = Math.max(0, value);
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${unit === 0 ? size.toFixed(0) : size.toFixed(1)} ${units[unit]}`;
}

/** 转换出的文件名（`migration.tasks.CONVERTED_FILES`）在界面上的叫法。 */
const convertedNames: Record<string, string> = {
  correction_json: "纠错结果",
  transcript_json: "转写结果",
};

export function reportSections(report: MigrationReport | undefined, taskTitles: Record<string, string> = {}): MigrationView["sections"] {
  if (!report) return [];
  const sections: MigrationView["sections"] = [];
  const settings = report.settings;
  if (settings) {
    const dropped = [...settings.droppedVariables, ...settings.droppedTables, ...settings.droppedKeys];
    if (dropped.length) sections.push({ title: "没有带过来的旧设置（Nonoka 不认识，FineSub 今后用默认值）", items: dropped });
    if (settings.unreadable.length) sections.push({ title: "本机打不开、没能带过来的密钥（请在设置里重新填写）", items: settings.unreadable });
    const renamed = Object.entries(settings.renamed).map(([from, to]) => `${from} → ${to}`);
    if (renamed.length) sections.push({ title: "改了名字的密钥", items: renamed });
  }
  const refused = Object.entries(report.tasks?.not_converted ?? {}).flatMap(([taskId, files]) => Object.entries(files).map(
    ([name, reason]) => `${taskTitles[taskId] || taskId}：${convertedNames[name] ?? name}没有转换（${reason}）`,
  ));
  if (refused.length) sections.push({ title: "没有转换成新格式的任务（原有文件都在，可以照常打开）", items: refused });
  const cleanup = report.cleanup;
  if (cleanup?.unreadableKeysRemoved?.length) sections.push({ title: "本机打不开的密钥已随旧密钥文件的备份一起删除", items: cleanup.unreadableKeysRemoved });
  if (cleanup?.removed.length) sections.push({ title: "已删除的旧文件", items: cleanup.removed });
  return sections;
}

export function migrationView(
  status: MigrationStatus | null,
  { dismissed = false, taskTitles = {} }: { dismissed?: boolean; taskTitles?: Record<string, string> } = {},
): MigrationView {
  const hidden: MigrationView = {
    visible: false, blocking: false, tone: "info", heading: "", message: "", polling: false, canRetry: false,
    dismissible: false, stepLine: "", percent: null, progressLine: "", error: "", blockers: [], sections: [],
  };
  if (!status || status.state === "not-needed") return hidden;
  const state = status.state;
  const blocking = status.dataOpen === false;
  const downloading = state === "downloading" || state === "download-failed";
  const dismissible = !blocking && !downloading;
  if (dismissed && dismissible) return hidden;

  const steps = status.steps ?? [];
  const index = status.step ? steps.findIndex((step) => step.id === status.step) : -1;
  const stepLine = index >= 0 && (state === "migrating" || downloading)
    ? `第 ${index + 1} / ${steps.length} 步：${steps[index].title}`
    : "";

  let percent: number | null = null;
  let progressLine = "";
  const progress = status.progress;
  if (state === "downloading" && progress) {
    if (progress.total > 0) percent = Math.min(100, Math.max(0, progress.completed / progress.total * 100));
    const amount = progress.total > 0 ? `${formatBytes(progress.completed)} / ${formatBytes(progress.total)}` : "";
    const speed = progress.bytesPerSecond && progress.bytesPerSecond > 0 ? `${formatBytes(progress.bytesPerSecond)}/s` : "";
    progressLine = [progress.message ?? "", amount, speed].filter(Boolean).join(" · ");
  }

  return {
    visible: true,
    blocking,
    tone: tones[state] ?? "info",
    heading: headings[state] ?? state,
    message: status.message === headings[state] ? "" : status.message,
    polling: pollingStates.has(state),
    canRetry: retryStates.has(state),
    dismissible,
    stepLine,
    percent,
    progressLine,
    error: status.error ?? "",
    blockers: status.blockers ?? [],
    sections: reportSections(status.report, taskTitles),
  };
}
