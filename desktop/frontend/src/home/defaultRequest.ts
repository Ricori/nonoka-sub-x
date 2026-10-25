import type { MediaEntry } from "../bridge/library.ts";
import type { ExecutionMode } from "../app/types.ts";
import type { TaskRequest } from "../providers/types.ts";

/**
 * 一份转写设置，两种计算引擎共用。
 *
 * 云端只把媒体识别到 stable.json，纠错、翻译和知识库都回到本机、用本机的模型跑，
 * 所以除了「谁来识别」之外，两边要的设置完全一样。对话框只编辑这一份；开始任务时
 * 再由 `requestForEngine` 落成对应引擎的 TaskRequest。
 */
export function defaultTaskRequest(entry: MediaEntry): TaskRequest {
  return {
    schema: 1,
    provider: "local",
    source: {
      kind: "local_file",
      path: entry.sourcePath,
      title: entry.title,
      fingerprint: entry.fingerprint,
      video_id: entry.id,
      duration: entry.duration,
    },
    target: "final-srt",
    language: "ja",
    device: "cuda",
    gpu_tier: "auto",
    separate: true,
    vocal_profile: "quality",
    correction: {
      enabled: true,
      media: "text",
      retrieval: "none",
      difficulty: "quality",
      fast: "auto",
      extra_info: "",
      extra_style: "",
    },
    knowledge: "read",
    knowledge_subjects: [],
    cleanup_intermediate: false,
  };
}

/**
 * 把共用的设置落成某个引擎的请求。
 *
 * 本机：原样，source 是本地文件。云端：source 换成待上传的音频（`object_id` 先填
 * 媒体 id，Go 侧上传后换成真正的对象 id），人声分离按时长自动分档（`cost`）；纠错
 * 与知识库设置原样带上——它们交给本机那一段续跑用，云端那一段不读。
 */
export function requestForEngine(engine: ExecutionMode, request: TaskRequest, entry: MediaEntry): TaskRequest {
  if (engine === "local") {
    return { ...request, provider: "local", vocal_profile: "quality" };
  }
  return {
    ...request,
    provider: "cloud",
    source: {
      kind: "uploaded_audio",
      object_id: entry.id,
      title: entry.title,
      fingerprint: entry.fingerprint,
      duration: entry.duration,
    },
    device: "cuda",
    vocal_profile: "cost",
    cleanup_intermediate: false,
  };
}
