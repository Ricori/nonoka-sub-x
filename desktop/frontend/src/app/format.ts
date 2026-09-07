import type { TaskSnapshot } from "../providers/types.ts";
import type { TaskHistoryEntry } from "./types.ts";
import { activeStates, taskHistoryLimit } from "./types.ts";

export function parseTaskHistory(value: unknown): TaskHistoryEntry[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is TaskHistoryEntry => {
    if (!item || typeof item !== "object") return false;
    const record = item as Partial<TaskHistoryEntry>;
    return typeof record.taskId === "string"
      && (record.provider === "local" || record.provider === "cloud")
      && typeof record.mediaId === "string"
      && typeof record.title === "string"
      && typeof record.snapshot === "object";
  }).slice(0, taskHistoryLimit);
}

export function formatDuration(seconds: number): string {
  const total = Math.max(0, Math.round(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const remainder = total % 60;
  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`
    : `${minutes}:${String(remainder).padStart(2, "0")}`;
}

export function formatSize(bytes: number): string {
  return `${(bytes / (1024 * 1024)).toFixed(bytes >= 100 * 1024 * 1024 ? 0 : 1)} MB`;
}

export function formatDate(value: number | string): string {
  const date = typeof value === "number" ? new Date(value) : new Date(Date.parse(value));
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleDateString("zh-CN", { month: "short", day: "numeric" });
}

export function formatTaskDate(value: number | string): string {
  const date = typeof value === "number" ? new Date(value) : new Date(Date.parse(value));
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString("zh-CN", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

export function taskStateLabel(state: TaskSnapshot["state"]): string {
  return {
    queued: "排队中",
    running: "处理中",
    completed: "已完成",
    failed: "失败",
    cancelled: "已取消",
    interrupted: "已中断",
  }[state];
}

/** Names the stage a task is in. The state matters because a task can end
    before any stage event arrives -- a worker that dies during startup leaves
    the stage empty, and the fallback must not tell the user "处理中" right
    beside a 失败 badge. */
export function taskStageLabel(stage: string, state?: TaskSnapshot["state"]): string {
  if (state === "completed") return "处理完成";
  const label = {
    queued: "等待调度",
    starting: "准备运行环境",
    "gpu-queued": "等待语音处理",
    pipeline: "准备处理",
    vocal: "处理音频",
    aligned: "语音识别",
    "postprocess-queued": "等待字幕处理",
    stable: "整理识别结果",
    "raw-srt": "生成字幕",
    "llm-queued": "等待纠错与翻译",
    "translated-srt": "纠错与翻译",
    "final-srt": "整理最终字幕",
    completed: "处理完成",
    failed: "处理失败",
    cancelled: "已取消",
    interrupted: "已中断",
  }[stage];
  if (label) return label;
  if (state === undefined || activeStates.has(state)) return stage ? "处理中" : "准备中";
  return stage ? taskStateLabel(state) : "未进入处理环节";
}

const INTERNAL_BLOCK_TIMECODE = /^\d{1,2}:\d{2}(?::\d{2})?$/;

const errorCodeLabels: Record<string, string> = {
  missing_llm_key: "缺少 API Key",
  missing_gpu: "未检测到可用 GPU",
  missing_model: "缺少所需模型",
  insufficient_disk: "磁盘空间不足",
  missing_dependency: "缺少系统依赖",
  quota_exceeded: "模型用量超限或额度不足",
  auth_failed: "本地 CLI 认证失效或未登录",
  agent_failed: "本地 CLI 运行异常",
  engine_failed: "引擎处理异常",
};

export function taskActivityText(snapshot: TaskSnapshot, nowMs = Date.now()): string {
  if (snapshot.error) {
    const label = errorCodeLabels[snapshot.error.code];
    return label ? `${label}: ${snapshot.error.message}` : `${snapshot.error.code}: ${snapshot.error.message}`;
  }
  if (activeStates.has(snapshot.state)) {
    const started = Date.parse(snapshot.started_at || snapshot.created_at);
    const elapsed = Number.isFinite(started)
      ? formatDuration(Math.max(0, (nowMs - started) / 1000))
      : "0:00";
    const detail = String(snapshot.progress?.message ?? "").trim();
    const usefulDetail = detail && !INTERNAL_BLOCK_TIMECODE.test(detail) ? detail : "";
    return usefulDetail ? `已用时 ${elapsed} · ${usefulDetail}` : `已用时 ${elapsed}`;
  }
  if (snapshot.state === "completed") {
    const detail = String(snapshot.progress?.message ?? "").trim();
    if (detail && detail !== "正在处理" && detail !== "处理中" && !detail.startsWith("正在")) {
      return detail;
    }
    return "字幕已完成";
  }
  return snapshot.progress?.message || taskStateLabel(snapshot.state);
}

interface ProgressBand {
  start: number;
  end: number;
  timeConstantSeconds: number;
}

const FINAL_STAGE_PROGRESS: Record<string, ProgressBand> = {
  queued: { start: 2, end: 4, timeConstantSeconds: 15 },
  starting: { start: 4, end: 8, timeConstantSeconds: 20 },
  "gpu-queued": { start: 6, end: 8, timeConstantSeconds: 20 },
  pipeline: { start: 7, end: 9, timeConstantSeconds: 8 },
  vocal: { start: 9, end: 36, timeConstantSeconds: 75 },
  aligned: { start: 36, end: 64, timeConstantSeconds: 120 },
  "postprocess-queued": { start: 64, end: 66, timeConstantSeconds: 15 },
  stable: { start: 66, end: 70, timeConstantSeconds: 15 },
  "raw-srt": { start: 70, end: 73, timeConstantSeconds: 10 },
  "llm-queued": { start: 73, end: 75, timeConstantSeconds: 20 },
  "translated-srt": { start: 75, end: 94, timeConstantSeconds: 210 },
  "final-srt": { start: 94, end: 99, timeConstantSeconds: 60 },
};

const RAW_STAGE_PROGRESS: Record<string, ProgressBand> = {
  queued: { start: 2, end: 4, timeConstantSeconds: 15 },
  starting: { start: 4, end: 8, timeConstantSeconds: 20 },
  "gpu-queued": { start: 6, end: 8, timeConstantSeconds: 20 },
  pipeline: { start: 7, end: 9, timeConstantSeconds: 8 },
  vocal: { start: 9, end: 48, timeConstantSeconds: 75 },
  aligned: { start: 48, end: 88, timeConstantSeconds: 120 },
  "postprocess-queued": { start: 88, end: 90, timeConstantSeconds: 15 },
  stable: { start: 90, end: 95, timeConstantSeconds: 15 },
  "raw-srt": { start: 95, end: 99, timeConstantSeconds: 10 },
};

function progressBand(snapshot: TaskSnapshot): ProgressBand | undefined {
  const target = snapshot.requested_capabilities?.target;
  const stages = target === "raw-srt" ? RAW_STAGE_PROGRESS : FINAL_STAGE_PROGRESS;
  return stages[snapshot.stage] ?? stages.starting;
}

/**
 * Hybrid task progress: real stage counters win when available; otherwise an
 * elapsed-time estimate approaches (but never reaches) the current stage cap.
 */
export function taskProgress(snapshot: TaskSnapshot, stageElapsedMs = 0): number {
  if (snapshot.state === "completed") return 100;
  const band = progressBand(snapshot);
  if (!band) return activeStates.has(snapshot.state) ? 2 : 0;
  const total = Number(snapshot.progress?.total ?? 0);
  const completed = Number(snapshot.progress?.completed ?? 0);
  const realFraction = total > 0 ? Math.min(1, Math.max(0, completed / total)) : 0;
  const elapsedSeconds = Math.max(0, stageElapsedMs / 1000);
  const estimatedFraction = Math.min(
    0.92,
    1 - Math.exp(-elapsedSeconds / band.timeConstantSeconds),
  );
  const fraction = Math.max(realFraction, estimatedFraction);
  return Math.min(99, Math.max(0, band.start + (band.end - band.start) * fraction));
}
