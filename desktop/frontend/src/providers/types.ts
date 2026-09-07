export type ProviderID = "local" | "cloud";
export type TaskState = "queued" | "running" | "completed" | "failed" | "cancelled" | "interrupted";
export type TaskEventType =
  | "started"
  | "stage"
  | "progress"
  | "warning"
  | "handoff"
  | "log"
  | "completed"
  | "failed"
  | "cancelled";

export interface EngineIdentity {
  name?: "finesub";
  version: string;
  commit: string;
  bundle_id?: string;
}

export interface Capabilities {
  provider: ProviderID;
  adapter_schema: 1;
  artifact_schema: 1;
  engine: EngineIdentity;
  features: {
    raw_srt: boolean;
    translation: boolean;
    video_multimodal: boolean;
    knowledge: boolean;
    resume: boolean;
    diarization: boolean;
  };
  devices: Array<{ id: string; name: string; memory_mb: number }>;
  runtime?: {
    ready: boolean;
    issues: Array<{ code: string; message: string }>;
    stages?: Array<{
      id: "media" | "raw-srt" | "final-srt" | "knowledge" | "video-multimodal";
      label: string;
      ready: boolean;
      issues: Array<{ code: string; message: string }>;
    }>;
    localAgent?: string;
  };
  settings?: { llm_key_configured: boolean };
}

export interface LocalSource {
  kind: "local_file";
  path: string;
  title: string;
  fingerprint?: string;
  video_id?: string;
  duration?: number;
}

export interface CloudSource {
  kind: "uploaded_audio";
  object_id: string;
  title: string;
  fingerprint?: string;
  duration?: number;
}

/** 用户导入的已有产物；解析与判型见 subtitles/assAxis.ts。 */
export interface TaskAxis {
  kind: "empty" | "ja" | "zh" | "bi";
  filename: string;
  rows: Array<{ t0: number; t1: number; ja: string; zh: string; spk: string }>;
}

export const GPU_TIERS = ["auto", "cpu", "entry", "standard", "standard_large_vram", "high"] as const;
export type GpuTier = (typeof GPU_TIERS)[number];

export interface TaskRequest {
  schema: 1;
  provider: ProviderID;
  source: LocalSource | CloudSource;
  target: "raw-srt" | "final-srt";
  language: string;
  device: string;
  /**
   * 显卡档位。FineSub 0.5.0 用它取代了按 GB 计的显存预算：档位说的是机器是什么，
   * 而不是花多少显存。`auto` 由引擎探测本机显卡，是出厂值；`high` 需要显卡自报
   * 24GB 以上，`auto` 最高只会选到 `standard_large_vram`。
   */
  gpu_tier: GpuTier;
  /**
   * 是否跑人声分离。关掉只在输入本身已是纯人声时才对：产物位置与格式不变
   * （`<stem>-vocal.ogg`，16 kHz 单声道），只是由源音直接转出。仅本地模式，
   * 云端的分离在它自己的容器里。
   */
  separate: boolean;
  vocal_profile: "cost" | "quality";
  correction: {
    enabled: boolean;
    media: "text" | "audio" | "video";
    retrieval: "none" | "local" | "native";
    difficulty: "efficiency" | "intermediate" | "quality";
    fast: "auto" | "on" | "off";
    extra_info: string;
    extra_style: string;
  };
  knowledge: "none" | "collect" | "update";
  cleanup_intermediate: boolean;
  /** 只在日文轴上出现：worker 据它跳过识别，直接补译文。其余轴型不进任务请求 */
  axis?: TaskAxis;
}

export interface TaskProgress {
  completed: number;
  total: number | null;
  unit: string;
  message: string;
}

export interface TaskError {
  code: string;
  message: string;
}

export interface TaskSnapshot {
  schema: 1;
  task_id: string;
  provider: ProviderID;
  state: TaskState;
  stage: string;
  progress: TaskProgress | null;
  engine: Pick<EngineIdentity, "version" | "commit">;
  requested_capabilities: Record<string, unknown>;
  effective_capabilities: Record<string, unknown>;
  error: TaskError | null;
  last_cursor: number;
  created_at: string;
  started_at?: string;
  updated_at: string;
}

export interface TaskListItem {
  snapshot: TaskSnapshot;
  media_id: string;
  title: string;
}

export interface TaskEvent {
  cursor: number;
  task_id: string;
  type: TaskEventType;
  timestamp: string;
  payload: Record<string, unknown>;
}

export interface TaskEventPage {
  schema: 1;
  task_id: string;
  events: TaskEvent[];
  next_cursor: number;
  state: TaskState;
}

export interface ArtifactEntry {
  uri: string;
  sha256: string;
  bytes: number;
}

/** 一次运行落盘的产物名。本地 worker 报告 PipelinePaths 上所有存在的文件，所以
 *  跑到哪一步就只有到那一步的产物；云端只回投影需要的那四个。 */
export type ArtifactName =
  | "vocal_audio"
  | "vad_json"
  | "vad_energy_npz"
  | "aligned_json"
  | "stable_json"
  | "raw_srt"
  | "translated_srt"
  | "annotated_csv"
  | "final_srt"
  | "metadata_json";

export interface ArtifactManifest {
  schema: 1;
  task_id: string;
  engine_commit: string;
  artifacts: Partial<Record<ArtifactName, ArtifactEntry>>;
}

export interface ExecutionProvider {
  capabilities(): Promise<Capabilities>;
  listTasks(): Promise<TaskListItem[]>;
  start(request: TaskRequest): Promise<TaskSnapshot>;
  status(taskId: string): Promise<TaskSnapshot>;
  events(taskId: string, afterCursor: number): Promise<TaskEventPage>;
  cancel(taskId: string): Promise<TaskSnapshot>;
  retry(taskId: string): Promise<TaskSnapshot>;
  resume(taskId: string): Promise<TaskSnapshot>;
  artifacts(taskId: string): Promise<ArtifactManifest>;
}
