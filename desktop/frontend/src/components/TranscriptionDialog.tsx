import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Service } from "../../bindings/github.com/Ricori/nonoka-x/desktop/internal/provider/index.js";
import type { MediaEntry } from "../bridge/library.ts";
import { defaultTaskRequest, requestForEngine } from "../home/defaultRequest.ts";
import { cloudAcceptsDuration } from "../home/executionAvailability.ts";
import { GPU_TIERS } from "../providers/types.ts";
import type { Capabilities, DiarizationLevel, GpuTier, KnowledgeSubject, TaskAxis, TaskRequest } from "../providers/types.ts";
import type { FineSubSettingsState } from "../bridge/settings.ts";
import type { ExecutionMode } from "../app/types.ts";
import { CustomSelect } from "./CustomSelect.tsx";
import {
  AXIS_KIND_HINT, AXIS_KIND_LABEL, AXIS_KIND_SHORT, parseAxisFile, recolumn,
  type AxisKind, type AxisParse,
} from "../subtitles/assAxis.ts";
import { routeForTaskGroup, routeServesMedia, routeServesNativeSearch, type MediaCapability } from "./llmRouting.ts";
import "./TranscriptionDialog.css";

interface TranscriptionDialogProps {
  entry: MediaEntry;
  localReady: boolean;
  localCapabilities: Capabilities | null;
  cloudCapabilities: Capabilities | null;
  settings: FineSubSettingsState | null;
  localIssue?: string;
  cloudAuthenticated: boolean;
  cloudRemaining?: number;
  busy: boolean;
  error: string;
  onClose: () => void;
  onOpenRuntime: () => void;
  onOpenAccount: () => void;
  onOpenKeys: () => void;
  onImport: (axis: TaskAxis) => Promise<void>;
  onStart: (engine: ExecutionMode, request: TaskRequest, axis: TaskAxis | null) => Promise<void>;
}

const languageOptions = [
  ["ja", "日语"],
  ["zh", "中文"],
  ["en", "英语"],
  ["auto", "自动检测"],
] as const;

const knowledgeKindOptions: Array<{ value: KnowledgeSubject["kind"]; label: string }> = [
  { value: "streamer", label: "主播 / 频道" },
  { value: "work", label: "作品 / 节目" },
  { value: "topic", label: "其他知识主题" },
];

const emptyKnowledgeSubject = (): KnowledgeSubject => ({
  kind: "streamer",
  subject: "",
  aliases: "",
  description: "",
});

interface KnowledgeEntry {
  id: string;
  name: string;
  category: string;
  intro?: string;
}

/** 已选主体；`key` 只给 React 用：已有主体取 id，新建的草稿取本地序号。 */
interface PickedSubject {
  key: string;
  subject: KnowledgeSubject;
}

const pickEntry = (entry: KnowledgeEntry): PickedSubject => ({
  key: entry.id,
  subject: {
    id: entry.id,
    kind: entry.category === "streamer" ? "streamer" : "topic",
    subject: entry.name,
    aliases: "",
    description: entry.intro ?? "",
  },
});

const entryLabel = (entry: Pick<KnowledgeEntry, "category" | "name">) => `${entry.category === "streamer" ? "主播" : "通用"} · ${entry.name}`;

const ADD_KNOWLEDGE_SUBJECT = "";
const NEW_KNOWLEDGE_SUBJECT = "__new_knowledge_subject__";
// 与引擎 `MAX_KNOWLEDGE_SUBJECTS` 一致：每个纠错窗口都带着全部已选主体，所以这也是提示词体积的上限。
const MAX_KNOWLEDGE_SUBJECTS = 8;

const LLM_KEY_HINT = "尚未配置模型提供商：请到设置里选择提供商与全局模型并保存，否则无法进行 LLM 纠错、翻译与知识处理。";
const RETRIEVAL_KEY_HINT = "需要 Exa、Tavily 或 Gemini 免费池 Key 才能进行本地联网检索。";
const NATIVE_SEARCH_HINT = "请在设置里将「纠错与翻译」和「背景准备与资料研究」设为支持原生检索的模型，并配置对应 Key 或本地 CLI；可使用本地 Codex、CodeBuddy、Antigravity 的 Gemini 模型或支持搜索的 Gemini API 模型。";

// 说话人分离。每次打开对话框都从关闭开始，不沿用上次的选择。
// 与 `nonoka_x.model_install.DIARIZATION_MODEL_IDS` 一致：两个模型都就绪才能分离。
const DIARIZATION_MODEL_IDS = ["nemotron3", "speaker-embedding"];
// 复核不用单独的模型，用的就是「纠错与翻译」那一格：每个纠错窗多一次调用。
const DIARIZATION_JUDGE_HINT = "实验功能：由 LLM 模型复核说话人，每个窗口多一次调用；只在生成最终字幕时可用";


type Step = "axis" | "settings";

interface KeyLimits {
  llm: boolean;
  nativeSearch: boolean;
  retrieval: boolean;
  audio: boolean;
  video: boolean;
}

/** 一条轴动辄两三个小时，只有分秒会把「覆盖到 2:41:00」显示成「161:00」。 */
const clock = (seconds: number) => {
  const whole = Math.max(0, Math.floor(seconds));
  const minutes = Math.floor(whole / 60);
  const tail = `${String(minutes % 60).padStart(2, "0")}:${String(whole % 60).padStart(2, "0")}`;
  return minutes < 60 ? `${minutes}:${String(whole % 60).padStart(2, "0")}` : `${Math.floor(minutes / 60)}:${tail}`;
};

// 没有 N 卡的机器上 sidecar 只报一个 `cpu`，所以这里不必单独判断「有没有显卡」
function initialRequest(entry: MediaEntry, capabilities: Capabilities | null): TaskRequest {
  const request = defaultTaskRequest(entry);
  const device = capabilities?.devices[0]?.id;
  if (device) {
    request.device = device;
    // `auto` 会去探测一张不存在的卡，然后按显卡档位算资源预算——跑出来的结果没错
    // （设备意图优先），但预算是假的。选定 CPU 就把档位一起说清楚。
    if (device === "cpu") request.gpu_tier = "cpu";
  }
  return request;
}

/** 设备下拉项；显存只有显卡才有意义，CPU 那一项不挂 GB。 */
function deviceOptions(devices: Capabilities["devices"]): Array<{ value: string; label: string; hint?: string }> {
  // 读不到能力时的兜底：旧版 sidecar 不报 CPU，此时唯一能说的仍是「交给引擎选卡」。
  if (devices.length === 0) return [{ value: "cuda", label: "自动选择 NVIDIA GPU" }];
  return devices.map((device) => device.id === "cpu"
    ? { value: device.id, label: "CPU（不使用显卡）", hint: CPU_DEVICE_HINT }
    : { value: device.id, label: `${device.name} · ${Math.round(device.memory_mb / 1024)} GB` });
}

/** 把用不了的选项拉回可用值；无需改动时返回原对象。`correction` 是第二个分区整体能不能开。 */
function withinLimits(request: TaskRequest, limits: KeyLimits, correction: boolean): TaskRequest {
  const target = request.target === "final-srt" && !correction ? "raw-srt" : request.target;
  const media = (request.correction.media === "audio" && !limits.audio) || (request.correction.media === "video" && !limits.video)
    ? "text"
    : request.correction.media;
  const retrieval = (request.correction.retrieval === "local" && !limits.retrieval) || (request.correction.retrieval === "native" && !limits.nativeSearch)
    ? "none"
    : request.correction.retrieval;
  if (target === request.target && media === request.correction.media && retrieval === request.correction.retrieval) return request;
  return {
    ...request,
    target,
    correction: { ...request.correction, enabled: target === "final-srt", media, retrieval },
  };
}

// 档位名是引擎的（`finesub.speech.runtime.resources`），说明按上游对它们的定义写：
// `entry` 是「基础 GPU 加速」而不是「弱卡专用」，`auto` 探测本机显卡但最高只选到
// `standard_large_vram`，`high` 需要显卡自报 24GB 以上。顺序与 GPU_TIERS 一致，
// 少一个档位就会在这里露出来。
const GPU_TIER_LABELS: Record<GpuTier, string> = {
  auto: "自动检测（推荐）",
  cpu: "不使用显卡",
  entry: "入门 · 基础 GPU 加速",
  standard: "标准",
  standard_large_vram: "标准 · 大显存（12GB 起）",
  high: "高 · 需 24GB 以上",
};
const GPU_TIER_OPTIONS = GPU_TIERS.map((value) => ({ value, label: GPU_TIER_LABELS[value] }));

// 纯 CPU 能跑完整条流水线，但人声分离和 Whisper 都是重活：按核心数不同，耗时大致
// 是音频时长的十几到几十倍。不说清楚，用户只会以为卡死了。
const CPU_DEVICE_HINT = "无显卡时的运行方式；整条流水线仍然完整，但耗时通常是音频时长的十几到几十倍";

// 关掉分离不会报错，只会让识别质量整体塌掉——所以这里说清它是给什么输入用的。
const SKIP_SEPARATION_HINT = "仅在输入本身就是纯人声（已分离的人声轨、录音棚干声）时关闭；该分离而没分离不会报错，只是识别质量整体下降";

// 云端只把识别和声学说话人区分做到 stable.json，复核在纠错阶段、要读说话人时间线，而云端不交付它。
const CLOUD_JUDGE_HINT = "云端转写只做按声音区分；需要 LLM 复核说话人时请选本机转写";
// 云端的分离按时长自动分档、总是开启，没有关闭这一档；显示出来但不让改，免得选了不生效。
const CLOUD_SEPARATION_HINT = "云端转写总是先做人声分离；输入已是纯人声时请选本机转写";

export function TranscriptionDialog(props: TranscriptionDialogProps) {
  const {
    entry, localReady, localCapabilities, cloudCapabilities, settings, localIssue, cloudAuthenticated,
    cloudRemaining, busy, error, onClose, onOpenRuntime, onOpenAccount, onOpenKeys, onImport, onStart,
  } = props;
  const [step, setStep] = useState<Step>("axis");
  const [axisOn, setAxisOn] = useState(false);
  const [axisFile, setAxisFile] = useState("");
  const [axisParse, setAxisParse] = useState<AxisParse | null>(null);
  const [axisKind, setAxisKind] = useState<AxisKind>("empty");
  const [axisIssue, setAxisIssue] = useState("");
  const [axisReading, setAxisReading] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const primaryAction = useRef<HTMLButtonElement>(null);

  // 三种产物走三条完全不同的路（见 subtitles/assAxis.ts 顶部），所以后面每一屏
  // 显示什么、能选什么，都得先看这里判出来的是哪一种。
  const axis: TaskAxis | null = useMemo(
    () => axisOn && axisParse ? { kind: axisKind, filename: axisFile, rows: recolumn(axisParse, axisKind) } : null,
    [axisFile, axisKind, axisOn, axisParse],
  );
  // 中文轴 / 双语轴什么都不必算，连运行环境都不用问；日文轴不跑识别，只在本机补译文。
  const importOnly = axis !== null && (axis.kind === "zh" || axis.kind === "bi");
  const translateOnly = axis?.kind === "ja";
  const axisBlocked = axisOn && axisParse === null;
  const totalSteps = importOnly ? 1 : 2;
  const stepIndex = step === "axis" ? 1 : 2;

  // 各段能不能跑，都从本机的分档就绪状态读：纠错与翻译永远在本机，云端转写的结果
  // 也要回到本机续跑（至少要渲染成字幕），所以本机的「续跑」档是云端转写的前提。
  const localStages = localCapabilities?.runtime?.stages ?? [];
  const stageIssue = (id: string): string => {
    const stage = localStages.find((item) => item.id === id);
    return stage && !stage.ready ? stage.issues[0]?.message || "尚未就绪" : "";
  };
  const localUnavailable = !localCapabilities
    ? "本地服务尚未启动"
    : localReady ? "" : localIssue || "本地运行环境尚未就绪";
  // 不报分档的旧版 sidecar：只能拿整体就绪状态兜底。
  const harnessIssue = !localCapabilities
    ? "本地服务尚未启动"
    : localStages.some((item) => item.id === "harness") ? stageIssue("harness") : localUnavailable;
  const cloudDurationExceeded = !cloudAcceptsDuration(entry.duration);
  const cloudUnavailable = !cloudAuthenticated
    ? "需要先登录云端账户"
    : cloudDurationExceeded
      ? `视频时长 ${clock(entry.duration)}，超过云端 2 小时上限`
      : harnessIssue ? `云端识别结果要回到本机继续处理：${harnessIssue}` : "";
  const engineIssue = (engine: ExecutionMode) => engine === "local" ? localUnavailable : cloudUnavailable;

  // 默认本机；本机转写不了而云端可以时才改选云端。
  const [engine, setEngine] = useState<ExecutionMode>(() => !localUnavailable || cloudUnavailable ? "local" : "cloud");
  const [request, setRequest] = useState<TaskRequest>(() => initialRequest(entry, localCapabilities));
  const [knowledgeEntries, setKnowledgeEntries] = useState<KnowledgeEntry[]>([]);
  const [pickedSubjects, setPickedSubjects] = useState<PickedSubject[]>([]);
  const draftCounter = useRef(0);
  const [knowledgeLoading, setKnowledgeLoading] = useState(false);
  const [knowledgeLoaded, setKnowledgeLoaded] = useState(false);
  const [knowledgeLoadError, setKnowledgeLoadError] = useState("");
  const engineCapabilities = engine === "local" ? localCapabilities : cloudCapabilities;
  // 纠错相关的能力看本机：无论谁转写，纠错、翻译和知识库都在本机跑。
  const supportsVideo = localCapabilities?.features.video_multimodal === true;
  const supportsKnowledge = localCapabilities?.features.knowledge === true;
  const finalOutput = request.target === "final-srt";
  const knowledgeActive = supportsKnowledge && finalOutput && request.knowledge !== "none";
  const knowledgeWrites = knowledgeActive && request.knowledge === "update";
  const devices = localCapabilities?.devices ?? [];
  const cpuOnly = (localCapabilities?.runtime?.warnings ?? []).some((warning) => warning.code === "missing_gpu");
  const speakers = axisParse && axisParse.speakers.length >= 2 ? axisParse.speakers : [];
  const [diarizationChoice, setDiarizationChoice] = useState<DiarizationLevel>("off");

  const limits = useMemo((): KeyLimits => {
    // 读不到本地设置时不做限制，避免误锁死；真不能跑时任务会在启动那一刻被本机拒绝。
    const keys = settings;
    // 音视频窗看的是「谁来答」，而自引擎 0.5.0 起答的人只有一个：钉住的模型组
    // 替换整条链，后面不再挂任何兜底。所以这里问的是选中的那个模型自己收不收，
    // 与有没有 Gemini Key 无关。引擎 0.6.0 起只剩纠错窗读媒体：本地检索的请求轮
    // 与交付在同一条对话里、由同一个模型完成，所以只看「纠错与翻译」这一格。
    const serves = (capability: MediaCapability) => !keys || routeServesMedia(keys.modelRouting, "correction", capability);
    return {
      // 只认已保存的全局模型：装了 codex CLI、或在设置里选了却没保存，都不算配置好。
      llm: !keys || keys.llmReady,
      nativeSearch: !keys || ["correction", "research"].every((routeID) => routeServesNativeSearch(keys.modelRouting, routeID)),
      retrieval: !keys || keys.retrievalKeyConfigured,
      audio: serves("supportsAudio"),
      // 纯音频素材没有画面可看；引擎遇到也会自己降级成音频，这里提前拦住免得白选。
      video: supportsVideo && serves("supportsVideo") && !entry.audioOnly,
    };
  }, [entry.audioOnly, settings, supportsVideo]);

  // 第二个分区能不能打开：先看模型配了没有，再看本机的纠错档（含续跑依赖）是否就绪。
  const correctionIssue = !limits.llm
    ? LLM_KEY_HINT
    : localStages.some((item) => item.id === "final-srt") ? stageIssue("final-srt") : !localCapabilities ? "本地服务尚未启动" : "";
  const correctionAvailable = correctionIssue === "";
  // 缺的是模型配置就去设置页，其余（续跑依赖等）去运行环境页。
  const correctionNeedsKeys = !limits.llm
    || localStages.find((item) => item.id === "final-srt")?.issues.some((issue) => issue.code === "missing_llm_key") === true;

  // 说话人分离：轴里已经标了说话人时以轴为准，不再分离；日文轴不跑识别，也就无从分离。
  const diarizationAvailable = !translateOnly && speakers.length === 0 && engineCapabilities?.features.diarization === true;
  // 本地模型没下载时不让选：先到运行环境页的「可选模型」下载。云端的模型在云端。
  // 没报 optional_models 的旧版 sidecar 无从判断，按已安装处理，不在这里拦。
  const optionalModels = localCapabilities?.runtime?.managed?.optional_models ?? [];
  const diarizationModelsMissing = engine === "local" && optionalModels.length > 0
    && DIARIZATION_MODEL_IDS.some((id) => optionalModels.find((item) => item.id === id)?.state !== "ready");
  const diarizationSelectable = diarizationAvailable && !diarizationModelsMissing;
  // 裁判在 LLM 纠错阶段运行，要读说话人时间线；云端不交付它，所以只有本机转写能复核。
  const judgeTextAllowed = finalOutput && engine === "local";
  const judgeAudioAllowed = judgeTextAllowed && limits.audio;
  const diarization: DiarizationLevel = !diarizationSelectable ? "off"
    : diarizationChoice === "judge-audio" && !judgeAudioAllowed ? (judgeTextAllowed ? "judge-text" : "acoustics")
      : diarizationChoice === "judge-text" && !judgeTextAllowed ? "acoustics"
        : diarizationChoice;
  const diarizationOnCpu = engine === "local" && (cpuOnly || request.device === "cpu");
  const diarizationNote = diarizationAvailable && diarizationModelsMissing ? "说话人分离模型尚未安装（约 450MB），需要先在运行环境页的「可选模型」中下载"
    : diarization === "off" ? ""
      : diarizationOnCpu ? "本次在 CPU 上运行，约为音频时长的五分之一" : "";

  /** 音视频不可用时，说清是哪一格挡住的——两格用的可能不是同一个模型。 */
  const mediaHint = useCallback((capability: MediaCapability): string => {
    const label = capability === "supportsAudio" ? "音频" : "视频多模态";
    if (!settings) return `当前模型不支持${label}。`;
    const serves = (routeID: string) => routeServesMedia(settings.modelRouting, routeID, capability);
    const freePool = (routeID: string) => routeForTaskGroup(settings.modelRouting, routeID).provider === "gemini-free";
    // 挡住的是配额还是能力？免费池那条与能力位无关，而且现在改 Key 也救不回来：
    // 钉住的是免费池那个目标本身，换 Key 不会换掉它。所以这句指向的是改这一格，
    // 不再是「去设置里填付费 Key」。排在前面，否则会落到下面那句、把用户支去
    // 「改用支持的模型」——而他选的模型明明声明了支持，照着改不会有任何效果。
    if (freePool("correction")) {
      return `Gemini 免费池不承担${label}窗：整段媒体要先传上 Files API，免费额度下极易撞上 503 与超时，而钉住的模型后面已经没有兜底接手。请到设置里把「纠错与翻译」改指到付费池的 Gemini 模型，或指到自带${label}能力的本地 CLI 模型（例如本地 Antigravity 的 Gemini 3.7 Flash）。`;
    }
    if (!serves("correction")) {
      return `「纠错与翻译」当前使用的模型不支持${label}：引擎 0.5.0 起，钉住的模型做不了的调用会直接失败而不是改走别人，所以请到设置里把这一格改指到支持${label}的模型（例如本地 Antigravity 的 Gemini 3.7 Flash，或付费池的 Gemini）。`;
    }
    return `当前模型不支持${label}。`;
  }, [settings]);

  const summary = useMemo(() => {
    if (axis && importOnly) return `直接导入 ${axis.rows.length} 条字幕，不做任何处理`;
    if (axis && translateOnly) return `按你的轴在本机补中文译文，不做识别（${axis.rows.length} 条）`;
    const where = engine === "local" ? "本机转写" : "云端转写";
    const output = finalOutput ? "本机 AI 纠错与翻译" : "只输出原始字幕";
    const source = request.language === "auto" ? "自动检测语言" : languageOptions.find(([id]) => id === request.language)?.[1] ?? request.language;
    return `${source} · ${where} · ${output}${axis ? " · 按导入的轴重排" : ""}${diarization !== "off" ? " · 区分说话人" : ""}`;
  }, [axis, diarization, engine, finalOutput, importOnly, request.language, translateOnly]);

  const back = useCallback(() => {
    if (step === "settings") setStep("axis");
    else onClose();
  }, [onClose, step]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (busy) return;
      if (event.key === "Escape") {
        event.preventDefault();
        back();
        return;
      }
      if (event.key !== "Enter" || event.defaultPrevented) return;
      // 回车推进当前这一步。焦点在按钮上时浏览器自己会点它，在下拉或输入框里时
      // 回车另有含义（CustomSelect 已经 preventDefault 过），两种都不能再抢。
      const tag = event.target instanceof HTMLElement ? event.target.tagName : "";
      if (tag === "BUTTON" || tag === "TEXTAREA" || tag === "INPUT" || tag === "SELECT") return;
      const action = primaryAction.current;
      if (action && !action.disabled) {
        event.preventDefault();
        action.click();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [back, busy]);

  // 每一步的主操作接管焦点：回车即推进，读屏也从这一步真正的落点开始念。
  useEffect(() => {
    primaryAction.current?.focus({ preventScroll: true });
  }, [step]);

  useEffect(() => {
    setRequest((current) => withinLimits(current, limits, correctionAvailable));
  }, [correctionAvailable, limits]);

  // 读取与写回共用一份列表：在两者间切换不重新拉取，也就不会冲掉已选的主体和草稿。
  useEffect(() => {
    if (!knowledgeActive) return;
    let cancelled = false;
    setKnowledgeLoading(true);
    setKnowledgeLoaded(false);
    setKnowledgeLoadError("");
    void (Service.Knowledge({ action: "list" }) as unknown as Promise<{ entries: KnowledgeEntry[] }>).then((data) => {
      if (cancelled) return;
      const entries = data.entries.filter((item) => item.category === "streamer" || item.category === "common");
      setKnowledgeEntries(entries);
      // 已选的主体可能在这期间被删掉了：留着只会让任务一开跑就失败。
      setPickedSubjects((current) => current.filter((item) => !item.subject.id || entries.some((entry) => entry.id === item.subject.id)));
      setKnowledgeLoaded(true);
    }).catch((value) => {
      if (cancelled) return;
      setKnowledgeEntries([]);
      setKnowledgeLoadError(value instanceof Error ? value.message : String(value));
      setKnowledgeLoaded(true);
    }).finally(() => { if (!cancelled) setKnowledgeLoading(false); });
    return () => { cancelled = true; };
  }, [knowledgeActive]);

  // 知识库还是空的、又要写回：没有可选的，直接给出第一条主体的草稿。
  useEffect(() => {
    if (knowledgeWrites && knowledgeLoaded && knowledgeEntries.length === 0) {
      setPickedSubjects((current) => current.length > 0 ? current : [{ key: `draft-${++draftCounter.current}`, subject: emptyKnowledgeSubject() }]);
    }
  }, [knowledgeEntries.length, knowledgeLoaded, knowledgeWrites]);

  // 日文轴只有翻译这一件事可做。钉在这里而不是选轴时，是因为轴型可以在第 1 步改回来——
  // 退回去换成日文轴再走回来，请求里若还留着 raw-srt，任务会以 invalid_axis 被拒。
  useEffect(() => {
    if (!translateOnly) return;
    setRequest((current) => current.target === "final-srt" ? current : {
      ...current,
      target: "final-srt",
      // 纠错参考默认退回纯文本：原文是人写的、已经校对过，再切一遍音频窗只是
      // 多花钱多花时间，需要时用户自己往上加。
      correction: { ...current.correction, enabled: true, media: "text" },
    });
  }, [translateOnly]);

  const readAxisFile = async (file: File) => {
    setAxisFile(file.name);
    setAxisParse(null);
    setAxisIssue("");
    setAxisReading(true);
    try {
      const parsed = parseAxisFile(await file.text(), file.name);
      if (parsed.rows.length === 0) {
        setAxisIssue("这个文件里没有解析出任何时间轴，换一份试试");
        return;
      }
      setAxisParse(parsed);
      setAxisKind(parsed.kind);
    } catch (value) {
      setAxisIssue(`读取失败：${value instanceof Error ? value.message : String(value)}`);
    } finally {
      setAxisReading(false);
    }
  };

  // 卡片只负责选中，翻页一律走「下一步」：这两张卡的差别（跑不跑识别）大到值得让
  // 用户看清选中态再确认，点一下就跳走会翻错页。
  const chooseNoAxis = () => {
    // 解析结果留着：改主意再回来时不必重挑一次文件，`axis` 本来就只在选中时才成立。
    setAxisOn(false);
  };

  // 选了「我已有轴」
  const chooseAxis = () => {
    setAxisOn(true);
  };

  // 换引擎不动其余设置：两边共用一份，只是「谁来识别」不同。
  const chooseEngine = (next: ExecutionMode) => {
    if (engineIssue(next)) return;
    setEngine(next);
  };

  const setCorrection = (on: boolean) => {
    if (on && !correctionAvailable) return;
    setRequest((current) => ({
      ...current,
      target: on ? "final-srt" : "raw-srt",
      correction: { ...current.correction, enabled: on },
    }));
  };

  const setKnowledge = (knowledge: TaskRequest["knowledge"]) => {
    setRequest((current) => ({ ...current, knowledge }));
    // 只读时建不了新主体：草稿留着也发不出去，干脆收起。
    if (knowledge !== "update") setPickedSubjects((current) => current.filter((item) => item.subject.id));
  };

  const addKnowledgeSubject = (choice: string) => {
    if (choice === ADD_KNOWLEDGE_SUBJECT) return;
    if (choice === NEW_KNOWLEDGE_SUBJECT) {
      setPickedSubjects((current) => [...current, { key: `draft-${++draftCounter.current}`, subject: emptyKnowledgeSubject() }]);
      return;
    }
    const entry = knowledgeEntries.find((item) => item.id === choice);
    if (entry) setPickedSubjects((current) => current.some((item) => item.key === entry.id) ? current : [...current, pickEntry(entry)]);
  };

  const removeKnowledgeSubject = (key: string) => {
    setPickedSubjects((current) => current.filter((item) => item.key !== key));
  };

  const updateDraftSubject = (key: string, patch: Partial<KnowledgeSubject>) => {
    setPickedSubjects((current) => current.map((item) => item.key === key ? { ...item, subject: { ...item.subject, ...patch } } : item));
  };

  // 只有写回时才可能有草稿；只读模式下发出去的只有已有主体。
  const knowledgeSubjects = knowledgeActive
    ? pickedSubjects.filter((item) => knowledgeWrites || item.subject.id).map((item) => item.subject)
    : [];
  const pickedExisting = pickedSubjects.filter((item) => item.subject.id);
  const pickedDrafts = knowledgeWrites ? pickedSubjects.filter((item) => !item.subject.id) : [];
  const subjectsFull = knowledgeSubjects.length >= MAX_KNOWLEDGE_SUBJECTS;
  const subjectOptions = knowledgeEntries.filter((entry) => !pickedSubjects.some((item) => item.key === entry.id));

  // 日文轴不跑识别，引擎无从谈起，只看纠错能不能开；其余情况先要选中的引擎能转写。
  // 写回必须有目标：至少一个主体，且每条新建草稿都填了名称。
  const knowledgeSubjectMissing = knowledgeWrites && (
    !knowledgeLoaded || knowledgeSubjects.length === 0 || knowledgeSubjects.some((item) => !item.subject.trim())
  );
  const runsOn: ExecutionMode = translateOnly ? "local" : engine;
  const startBlocked = busy
    || (translateOnly ? !correctionAvailable : engineIssue(engine) !== "")
    || (finalOutput && !correctionAvailable)
    || knowledgeSubjectMissing;
  const startRequest: TaskRequest = requestForEngine(runsOn, {
    ...request,
    knowledge: finalOutput ? request.knowledge : "none",
    knowledge_subjects: knowledgeSubjects,
    ...(diarization !== "off" ? { diarization: { level: diarization } } : {}),
  }, entry);
  const uploadingCloudAudio = busy && runsOn === "cloud";
  const coverage = axisParse ? clock(Math.max(...axisParse.rows.map((row) => row.t1))) : "";
  const notes = axisParse && axisParse.skipped > 0 ? `跳过 ${axisParse.skipped} 条注释或无效行` : "";
  // 只有空轴会经过识别，所以只有它会踩到这件事：转写引擎不区分说话人，重叠区间里
  // 每个词落在哪一行只能按时间判，两人同时说话时必然有一边串台。其余轴型的文字是
  // 用户自己写好的，重叠再多也无害。
  const overlaps = axisParse && axisKind === "empty" ? axisParse.overlaps : 0;

  const localBadge = localUnavailable ? "未就绪" : cpuOnly ? "可用 · CPU" : "可用";
  const cloudBadge = !cloudAuthenticated ? "未登录" : cloudDurationExceeded ? "时长超限" : harnessIssue ? "需本机环境"
    : cloudRemaining === undefined ? "已登录" : `剩余 ${cloudRemaining} 次`;
  const chosenIssue = engineIssue(engine);

  return (
    <div className="modal-backdrop transcription-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget && !busy) onClose(); }}>
      <section className="transcription-dialog" role="dialog" aria-modal="true" aria-labelledby="transcription-title">
        <header className="transcription-head">
          <div>
            <div className="transcription-progress">
              <span className="step-dots" aria-hidden="true">
                {Array.from({ length: totalSteps }, (_, index) => (
                  <i key={index} className={index + 1 === stepIndex ? "on" : index + 1 < stepIndex ? "done" : ""} />
                ))}
              </span>
              <span className="eyebrow">{`第 ${stepIndex} 步，共 ${totalSteps} 步`}</span>
            </div>
            <h2 id="transcription-title">{step === "axis" ? "选择已有产物" : "任务设置"}</h2>
            <p title={entry.sourcePath}>{entry.title}</p>
          </div>
          <button type="button" className="transcription-close" aria-label="关闭" disabled={busy} onClick={onClose}>×</button>
        </header>

        <input
          ref={fileInput}
          type="file"
          accept=".ass,.ssa,.srt"
          hidden
          onChange={(event) => {
            const file = event.target.files?.[0];
            // 清掉 value：同一个文件改完再选一次也要触发 change
            event.target.value = "";
            if (file) void readAxisFile(file);
          }}
        />

        {step === "axis" ? (
          <div className="execution-picker">
            <button className={`execution-card ${axisOn ? "" : "chosen"}`} onClick={chooseNoAxis}>
              <span className="execution-icon local">✧</span>
              <span><strong>我没有轴</strong><small>完整的识别、纠错和翻译流程</small></span>
              <em>默认</em>
            </button>

            <button className={`execution-card ${axisOn ? "chosen" : ""}`} onClick={chooseAxis}>
              <span className="execution-icon axis">≡</span>
              <span><strong>我已有轴</strong><small>空轴 / 单语轴 / 双语轴，会自动检测并补充空缺内容，轴里有翻译时则会立即导入</small></span>
              <em>{axisParse ? AXIS_KIND_SHORT[axisKind] : "选择文件"}</em>
            </button>

            {axisOn && <div className="axis-panel">
              {axisReading ? <p className="axis-hint">{`正在解析「${axisFile}」…`}</p>
                : axisIssue ? <>
                  <p className="axis-hint bad">{axisIssue}</p>
                  <button type="button" className="axis-browse" onClick={() => fileInput.current?.click()}><strong>换一份文件</strong></button>
                </>
                  : axisParse ? <>
                    <div className="axis-file-line">
                      <span title={axisFile}>{axisFile}</span>
                      <button type="button" onClick={() => fileInput.current?.click()}>换一份</button>
                    </div>
                    {/* 用 div 而不是 dl/dt/dd：全局样式表里有一份给别处写的
                        无类名 dl/dd 规则（styles.css 的 65% 宽 + 省略号），
                        在这里会把数字截成「2…」 */}
                    <div className="axis-stats">
                      <div><span>行数</span><strong>{axisParse.rows.length}</strong></div>
                      <div><span>覆盖到</span><strong>{coverage}</strong></div>
                      <div><span>说话人</span><strong>{speakers.length > 0 ? `${speakers.length} 位` : "未标注"}</strong></div>
                    </div>
                    <label>产物类型<CustomSelect value={axisKind} options={(["empty", "ja", "zh", "bi"] as AxisKind[]).map((value) => ({ value, label: AXIS_KIND_LABEL[value] }))} onChange={(value) => setAxisKind(value)} /></label>
                    <p className="axis-hint">{AXIS_KIND_HINT[axisKind]}；判断错了就在上面改</p>
                    {speakers.length > 0 && <p className="axis-hint ok">{`按 ${speakers.join("、")} 自动分轨`}</p>}
                    {overlaps > 0 && <p className="axis-hint warn">{`${overlaps} 处两人同时说话：识别不区分说话人，重叠处的词可能落到相邻那一行，导入后需人工校对`}</p>}
                    {notes && <p className="axis-hint">{notes}</p>}
                  </>
                    : <button type="button" className="axis-browse" onClick={() => fileInput.current?.click()}>
                      <strong>选择字幕文件</strong>
                      <small>支持 .ass / .ssa / .srt</small>
                    </button>}
            </div>}

            {axis && importOnly && <div className="transcription-summary"><span>将要执行</span><strong>{summary}</strong></div>}
            {error && <p className="transcription-error" role="alert">{error}</p>}
          </div>
        ) : (
          <div className="transcription-settings">
            <section className="transcription-block" aria-labelledby="transcription-block-asr">
              <header className="transcription-block-head">
                <span className="transcription-block-index" aria-hidden="true">1</span>
                <strong id="transcription-block-asr">转写</strong>
              </header>
              {translateOnly ? (
                <p className="transcription-note">已导入原文，跳过转写。</p>
              ) : <>
                <div className="option-cards two-columns engine-cards" role="radiogroup" aria-label="转写引擎">
                  <button role="radio" aria-checked={engine === "local"} className={`${engine === "local" ? "selected" : ""} ${localUnavailable ? "unavailable" : ""}`.trim()} aria-disabled={Boolean(localUnavailable) || undefined} title={localUnavailable || undefined} onClick={() => chooseEngine("local")}>
                    <span className="engine-card-head"><span className="execution-icon local" aria-hidden="true">⌁</span><strong>本机</strong><em>{localBadge}</em></span>
                  </button>
                  <button role="radio" aria-checked={engine === "cloud"} className={`${engine === "cloud" ? "selected" : ""} ${cloudUnavailable ? "unavailable" : ""}`.trim()} aria-disabled={Boolean(cloudUnavailable) || undefined} title={cloudUnavailable || undefined} onClick={() => chooseEngine("cloud")}>
                    <span className="engine-card-head"><span className="execution-icon cloud" aria-hidden="true">☁</span><strong>云端</strong><em>{cloudBadge}</em></span>
                  </button>
                </div>
                {chosenIssue && <p className="option-unavailable">{chosenIssue}{engine === "cloud" && !cloudAuthenticated
                  ? <button onClick={onOpenAccount}>前往登录</button>
                  : <button onClick={onOpenRuntime}>检查运行环境</button>}</p>}

                <div className="form-grid transcription-block-fields">
                  {engine === "local" && <label>计算设备<CustomSelect value={request.device} options={deviceOptions(devices)} onChange={(device) => setRequest((current) => ({ ...current, device, gpu_tier: device === "cpu" ? "cpu" : current.gpu_tier === "cpu" ? "auto" : current.gpu_tier }))} /></label>}
                  {engine === "local" && <label>显卡档位<CustomSelect value={request.gpu_tier} options={GPU_TIER_OPTIONS} onChange={(gpu_tier) => setRequest((current) => ({ ...current, gpu_tier }))} /></label>}
                  <label>原始语言<CustomSelect value={request.language} options={languageOptions.map(([value, label]) => ({ value, label }))} onChange={(language) => setRequest((current) => ({ ...current, language }))} /></label>
                  <label title={engine === "cloud" ? CLOUD_SEPARATION_HINT : undefined}>人声分离<CustomSelect value={engine === "cloud" || request.separate ? "on" : "off"} disabled={engine === "cloud"} options={[{ value: "on", label: "开启" }, { value: "off", label: "关闭（输入已是纯人声）", hint: SKIP_SEPARATION_HINT }]} onChange={(value) => setRequest((current) => ({ ...current, separate: value === "on" }))} /></label>
                  <label>说话人分离<CustomSelect value={diarization} disabled={!diarizationSelectable} options={[
                    { value: "off" as const, label: "关闭" },
                    { value: "acoustics" as const, label: "按声音区分说话人" },
                    { value: "judge-text" as const, label: "声音 + 文本复核（实验）", disabled: !judgeTextAllowed, hint: engine === "cloud" ? CLOUD_JUDGE_HINT : DIARIZATION_JUDGE_HINT },
                    { value: "judge-audio" as const, label: "声音 + 音频复核（实验）", disabled: !judgeAudioAllowed, hint: engine === "cloud" ? CLOUD_JUDGE_HINT : finalOutput && !limits.audio ? mediaHint("supportsAudio") : DIARIZATION_JUDGE_HINT },
                  ]} onChange={setDiarizationChoice} /></label>
                </div>
                {diarizationNote && <p className="transcription-note">{diarizationNote}{diarizationModelsMissing && <button type="button" className="inline-link" onClick={onOpenRuntime}>前往下载</button>}</p>}
              </>}
            </section>

            <section className={`transcription-block ${finalOutput ? "" : "collapsed"}`.trim()} aria-labelledby="transcription-block-llm">
              <header className="transcription-block-head">
                <span className="transcription-block-index" aria-hidden="true">2</span>
                <strong id="transcription-block-llm">纠错与翻译</strong>
                <label className="block-switch" title={correctionIssue || undefined}>
                  <input type="checkbox" role="switch" aria-label="纠错与翻译" checked={finalOutput} disabled={translateOnly || !correctionAvailable} onChange={(event) => setCorrection(event.target.checked)} />
                  <i aria-hidden="true" />
                </label>
              </header>
              {!correctionAvailable && <p className="option-unavailable">{correctionIssue}{correctionNeedsKeys
                ? <button onClick={onOpenKeys}>前往配置</button>
                : <button onClick={onOpenRuntime}>检查运行环境</button>}</p>}

              {finalOutput && <>
                <div className="form-grid transcription-block-fields">
                  <label>纠错参考<CustomSelect value={request.correction.media} options={[{ value: "text", label: "仅识别文本" }, { value: "audio" as const, label: "音频", disabled: !limits.audio, hint: limits.audio ? undefined : mediaHint("supportsAudio") }, ...(supportsVideo ? [{ value: "video" as const, label: "视频多模态", disabled: !limits.video, hint: limits.video ? undefined : entry.audioOnly ? "纯音频素材没有画面" : mediaHint("supportsVideo") }] : [])]} onChange={(media) => setRequest((current) => ({ ...current, correction: { ...current.correction, media } }))} /></label>
                  <label>处理质量<CustomSelect value={request.correction.difficulty} options={[{ value: "efficiency", label: "效率优先" }, { value: "intermediate", label: "均衡" }, { value: "quality", label: "质量优先" }]} onChange={(difficulty) => setRequest((current) => ({ ...current, correction: { ...current.correction, difficulty } }))} /></label>
                  <label>快速模式<CustomSelect value={request.correction.fast} options={[{ value: "auto", label: "自动", hint: "字幕放得进单个窗口时一次完成，放不下时自动改走正常流程" }, { value: "off", label: "关闭" }]} onChange={(fast) => setRequest((current) => ({ ...current, correction: { ...current.correction, fast } }))} /></label>
                  <label>资料检索<CustomSelect value={request.correction.retrieval} options={[{ value: "none", label: "不检索" }, { value: "local", label: "本地检索", disabled: !limits.retrieval, hint: limits.retrieval ? undefined : RETRIEVAL_KEY_HINT }, { value: "native", label: "模型原生检索", disabled: !limits.nativeSearch, hint: limits.nativeSearch ? undefined : NATIVE_SEARCH_HINT }]} onChange={(retrieval) => setRequest((current) => ({ ...current, correction: { ...current.correction, retrieval } }))} /></label>
                  {supportsKnowledge && <label>知识库<CustomSelect value={request.knowledge} options={[{ value: "none", label: "不使用" }, { value: "read", label: "读取知识库" }, { value: "update", label: "读取知识库并自动更新" }]} onChange={setKnowledge} /></label>}
                </div>

                {knowledgeActive && <section className="transcription-section knowledge-subject-section">
                  <div className="knowledge-subject-picker">
                    <label>知识主体<CustomSelect
                      value={ADD_KNOWLEDGE_SUBJECT}
                      disabled={knowledgeLoading || subjectsFull}
                      options={[
                        { value: ADD_KNOWLEDGE_SUBJECT, label: knowledgeLoading ? "正在读取知识库…" : subjectsFull ? `最多选择 ${MAX_KNOWLEDGE_SUBJECTS} 个` : "添加知识主体…", disabled: true },
                        ...subjectOptions.map((entry) => ({ value: entry.id, label: entryLabel(entry), hint: entry.intro || undefined })),
                        ...(knowledgeWrites ? [{ value: NEW_KNOWLEDGE_SUBJECT, label: "＋ 新建知识主体" }] : []),
                      ]}
                      onChange={addKnowledgeSubject}
                    /></label>
                  </div>
                  {pickedExisting.length > 0 && <ul className="knowledge-subject-chips" aria-label="已选知识主体">
                    {pickedExisting.map((item) => (
                      <li key={item.key} title={item.subject.description || undefined}>
                        <span>{entryLabel({ category: item.subject.kind === "streamer" ? "streamer" : "common", name: item.subject.subject })}</span>
                        <button type="button" aria-label={`移除 ${item.subject.subject}`} onClick={() => removeKnowledgeSubject(item.key)}>×</button>
                      </li>
                    ))}
                  </ul>}
                  {knowledgeLoadError && <p className="knowledge-load-warning">{knowledgeWrites ? `知识库列表读取失败，仍可新建主体：${knowledgeLoadError}` : `知识库列表读取失败：${knowledgeLoadError}`}</p>}
                  {knowledgeWrites && knowledgeLoaded && knowledgeSubjects.length === 0 && <p className="knowledge-required-hint">请至少选择或新建一个知识主体，作为新知识的写回目标。</p>}
                </section>}

                {pickedDrafts.map((item) => {
                  const draft = item.subject;
                  return <section key={item.key} className="transcription-section knowledge-context-card">
                    <div className="knowledge-context-head">
                      <span className="knowledge-context-icon" aria-hidden="true">⌁</span>
                      <span>
                        <strong>新建知识主体</strong>
                        <small>任务开始前建好词条，引导模型把新发现归到正确主体</small>
                      </span>
                      <span className="knowledge-context-actions">
                        <em>名称必填</em>
                        <button type="button" className="knowledge-context-remove" aria-label="移除这个新建主体" onClick={() => removeKnowledgeSubject(item.key)}>×</button>
                      </span>
                    </div>
                    <div className="knowledge-context-grid">
                      <label>主体类型<CustomSelect value={draft.kind} options={knowledgeKindOptions} onChange={(kind) => updateDraftSubject(item.key, { kind })} /></label>
                      <label>知识主体名称<input required maxLength={120} aria-invalid={!draft.subject.trim() || undefined} placeholder={draft.kind === "streamer" ? "例如：主播或频道的官方名称" : draft.kind === "work" ? "例如：作品、系列或节目名" : "例如：组织、游戏或专题名称"} value={draft.subject} onChange={(event) => updateDraftSubject(item.key, { subject: event.target.value })} /></label>
                      <label>别名 / 常用译名<input maxLength={300} placeholder="多个名称可用顿号或逗号分隔（可选）" value={draft.aliases} onChange={(event) => updateDraftSubject(item.key, { aliases: event.target.value })} /></label>
                      <label className="knowledge-context-description">主体说明<textarea rows={2} maxLength={1200} placeholder="频道定位、所属团体、作品背景等（可选）" value={draft.description} onChange={(event) => updateDraftSubject(item.key, { description: event.target.value })} /></label>
                    </div>
                    {!draft.subject.trim() && <p className="knowledge-required-hint">填写知识主体名称后才能开始任务。</p>}
                  </section>;
                })}

                <div className="prompt-grid transcription-block-fields">
                  <label>背景信息<textarea rows={3} maxLength={4000} placeholder="人物、节目、专有名词等（可选）" value={request.correction.extra_info} onChange={(event) => setRequest((current) => ({ ...current, correction: { ...current.correction, extra_info: event.target.value } }))} /></label>
                  <label>翻译风格<textarea rows={3} maxLength={4000} placeholder="例如：简洁自然，保留角色名英文（可选）" value={request.correction.extra_style} onChange={(event) => setRequest((current) => ({ ...current, correction: { ...current.correction, extra_style: event.target.value } }))} /></label>
                </div>
              </>}
            </section>

            {uploadingCloudAudio && <div className="cloud-submit-progress" role="status" aria-live="polite" aria-busy="true">
              <div>
                <span className="cloud-submit-icon" aria-hidden="true">☁</span>
                <span>
                  <strong>正在准备并上传音轨…</strong>
                  <small>视频越长，上传所需时间越久；完成后会自动进入任务页面。</small>
                </span>
              </div>
              <span className="cloud-submit-track" aria-hidden="true"><i /></span>
            </div>}
            <div className="transcription-summary"><span>将要执行</span><strong>{summary}</strong></div>
            {error && <p className="transcription-error" role="alert">{error}</p>}
          </div>
        )}

        <footer className="transcription-actions">
          <button disabled={busy} onClick={back}>{step === "axis" ? "取消" : "上一步"}</button>
          {step === "axis" && (axis && importOnly
            ? <button ref={primaryAction} className="primary-button" disabled={busy} onClick={() => void onImport(axis)}>{busy ? "正在导入…" : "导入"}</button>
            : <button ref={primaryAction} className="primary-button" disabled={busy || axisBlocked} onClick={() => setStep("settings")}>下一步</button>)}
          {step === "settings" && <button ref={primaryAction} className="primary-button" disabled={startBlocked} onClick={() => void onStart(runsOn, translateOnly && axis ? { ...startRequest, axis } : startRequest, axis)}>{busy ? uploadingCloudAudio ? "上传中…" : "正在启动…" : "开始任务"}</button>}
        </footer>
      </section>
    </div>
  );
}
