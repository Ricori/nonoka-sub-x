import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Service } from "../../bindings/github.com/Ricori/nonoka-x/desktop/internal/provider/index.js";
import type { MediaEntry } from "../bridge/library.ts";
import { cloudTaskRequest, localTaskRequest } from "../home/defaultRequest.ts";
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
  initialMode: ExecutionMode;
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
  onStart: (mode: ExecutionMode, request: TaskRequest, axis: TaskAxis | null) => Promise<void>;
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

// 说话人分离（FineSub 0.6.0）。上次的选择记在本机，下次打开对话框沿用；读写失败就当没记过。
const DIARIZATION_STORAGE_KEY = "nonoka:diarization-level";
const DIARIZATION_LEVELS: DiarizationLevel[] = ["off", "acoustics", "judge-text", "judge-audio"];
const DIARIZATION_JUDGE_HINT = "实验功能：在 LLM 纠错阶段按窗口复核说话人，会额外调用模型；只在生成最终字幕时可用";

function rememberedDiarization(): DiarizationLevel {
  try {
    const value = localStorage.getItem(DIARIZATION_STORAGE_KEY) as DiarizationLevel | null;
    return value && DIARIZATION_LEVELS.includes(value) ? value : "off";
  } catch {
    return "off";
  }
}

function rememberDiarization(level: DiarizationLevel) {
  try {
    localStorage.setItem(DIARIZATION_STORAGE_KEY, level);
  } catch {
    // 只是个便利项，存不下不影响本次任务
  }
}

type Step = "axis" | "mode" | "settings";

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
function requestFor(mode: ExecutionMode, entry: MediaEntry, capabilities: Capabilities | null): TaskRequest {
  const request = mode === "local" ? localTaskRequest(entry) : cloudTaskRequest(entry);
  const device = capabilities?.devices[0]?.id;
  if (mode === "local" && device) {
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

/** 把缺少 Key 的选项拉回可用值；无需改动时返回原对象。 */
function withinLimits(request: TaskRequest, limits: KeyLimits): TaskRequest {
  const target = request.target === "final-srt" && !limits.llm ? "raw-srt" : request.target;
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
    knowledge: target === "final-srt" ? request.knowledge : "none",
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

export function TranscriptionDialog(props: TranscriptionDialogProps) {
  const {
    entry, initialMode, localReady, localCapabilities, cloudCapabilities, settings, localIssue, cloudAuthenticated,
    cloudRemaining, busy, error, onClose, onOpenRuntime, onOpenKeys, onImport, onStart,
  } = props;
  // `onOpenAccount` 仍在 props 上、App 也仍在传，只是它唯一的用处——未登录时那条
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
  // 中文轴 / 双语轴什么都不必算，连运行环境都不用问；日文轴两种环境都跑得了，
  // 但两边都只做翻译那一段。
  const importOnly = axis !== null && (axis.kind === "zh" || axis.kind === "bi");
  const translateOnly = axis?.kind === "ja";
  const axisBlocked = axisOn && axisParse === null;
  const totalSteps = importOnly ? 1 : 3;
  const stepIndex = step === "axis" ? 1 : step === "mode" ? 2 : 3;
  const cloudDurationExceeded = !cloudAcceptsDuration(entry.duration);
  const cloudReady = cloudAuthenticated && !cloudDurationExceeded;

  // 只用来定第 2 步一开始选中哪张卡（轴要到第 1 步选完才存在，所以这里无须看它）
  const preferredMode = initialMode === "local"
    ? localReady ? "local" : cloudReady ? "cloud" : "local"
    : cloudReady ? "cloud" : localReady ? "local" : "cloud";
  const [mode, setMode] = useState<ExecutionMode>(preferredMode);
  const [request, setRequest] = useState<TaskRequest>(() => requestFor(preferredMode, entry, localCapabilities));
  const [knowledgeEntries, setKnowledgeEntries] = useState<KnowledgeEntry[]>([]);
  const [pickedSubjects, setPickedSubjects] = useState<PickedSubject[]>([]);
  const draftCounter = useRef(0);
  const [knowledgeLoading, setKnowledgeLoading] = useState(false);
  const [knowledgeLoaded, setKnowledgeLoaded] = useState(false);
  const [knowledgeLoadError, setKnowledgeLoadError] = useState("");
  const selectedReady = mode === "local" ? localReady : cloudReady;
  const selectedCapabilities = mode === "local" ? localCapabilities : cloudCapabilities;
  const supportsVideo = selectedCapabilities?.features.video_multimodal === true;
  const supportsKnowledge = selectedCapabilities?.features.knowledge === true;
  const finalOutput = request.target === "final-srt";
  // 主体只在本地读得到（列表来自本机知识库），云端不出现这一块。
  const knowledgeActive = mode === "local" && supportsKnowledge && finalOutput && request.knowledge !== "none";
  const knowledgeWrites = knowledgeActive && request.knowledge === "update";
  const devices = localCapabilities?.devices ?? [];
  const cpuOnly = (localCapabilities?.runtime?.warnings ?? []).some((warning) => warning.code === "missing_gpu");
  const speakers = axisParse && axisParse.speakers.length >= 2 ? axisParse.speakers : [];
  const [diarizationChoice, setDiarizationChoice] = useState<DiarizationLevel>(rememberedDiarization);

  const limitsFor = useCallback((target: ExecutionMode): KeyLimits => {
    // 云端运行的凭据由 Nonoka Cloud 管理；读不到本地设置时也不做限制，避免误锁死。
    const keys = target === "local" ? settings : null;
    const video = (target === "local" ? localCapabilities : cloudCapabilities)?.features.video_multimodal === true;
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
      audio: target === "local" && serves("supportsAudio"),
      video: video && serves("supportsVideo"),
    };
  }, [cloudCapabilities, localCapabilities, settings]);
  const limits = useMemo(
    () => limitsFor(mode),
    [limitsFor, mode],
  );

  // 说话人分离：轴里已经标了说话人时以轴为准，不再分离；日文轴不跑识别，也就无从分离。
  const diarizationAvailable = !translateOnly && speakers.length === 0 && selectedCapabilities?.features.diarization === true;
  // 裁判在 LLM 纠错阶段运行；音频裁判还要「纠错与翻译」那一格的模型能听音频，云端的纠错只走文本。
  const judgeTextAllowed = finalOutput;
  const judgeAudioAllowed = finalOutput && mode === "local" && limits.audio;
  const diarization: DiarizationLevel = !diarizationAvailable ? "off"
    : diarizationChoice === "judge-audio" && !judgeAudioAllowed ? (judgeTextAllowed ? "judge-text" : "acoustics")
      : diarizationChoice === "judge-text" && !judgeTextAllowed ? "acoustics"
        : diarizationChoice;
  const diarizationOnCpu = mode === "local" && (cpuOnly || request.device === "cpu");
  // 本地模型没下载也能开跑（引擎会在分离阶段自己下载），这里只是让人知道要等，并给出提前下载的入口。
  const diarizationModels = localCapabilities?.runtime?.managed?.optional_models ?? [];
  const diarizationModelsMissing = mode === "local" && diarizationModels.some((item) => item.state !== "ready");
  const diarizationNote = diarization === "off" ? ""
    : [
      "按说话人把字幕拆成不同轨道，不改变字幕文本与时间",
      diarizationModelsMissing ? "说话人分离模型尚未下载（约 450MB），开始后会在分离阶段自动下载，也可先到运行环境页的「可选模型」下载" : "",
      diarizationOnCpu ? "本次在 CPU 上运行，约为音频时长的五分之一" : "",
    ].filter(Boolean).join("；");
  const chooseDiarization = (level: DiarizationLevel) => {
    setDiarizationChoice(level);
    rememberDiarization(level);
  };

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
    if (axis && translateOnly) return `按你的轴补中文译文，不做识别（${axis.rows.length} 条）`;
    const output = finalOutput ? "AI 纠错与翻译后的最终字幕" : "识别与断句后的原始字幕";
    const source = request.language === "auto" ? "自动检测语言" : languageOptions.find(([id]) => id === request.language)?.[1] ?? request.language;
    return `${source} · ${output}${axis ? " · 按导入的轴重排" : ""}${diarization !== "off" ? " · 区分说话人" : ""}`;
  }, [axis, diarization, finalOutput, importOnly, request.language, translateOnly]);

  const back = useCallback(() => {
    if (step === "settings") setStep("mode");
    else if (step === "mode") setStep("axis");
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
    setRequest((current) => withinLimits(current, limits));
  }, [limits]);

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

  /** 换运行环境就换一整份默认请求：两边的 source 形态、目标和显存项都不一样。 */
  const applyMode = useCallback((nextMode: ExecutionMode) => {
    setMode(nextMode);
    const next = requestFor(nextMode, entry, localCapabilities);
    setRequest(withinLimits(next, limitsFor(nextMode)));
  }, [entry, limitsFor, localCapabilities]);

  // 日文轴只有翻译这一件事可做。钉在这里而不是选运行环境时，是因为轴型可以在第 1
  // 步改回来——退两步换成日文轴再走回来，请求里若还留着 raw-srt，任务会被两端一致地
  // 以 invalid_axis 拒掉。
  useEffect(() => {
    if (!translateOnly) return;
    setRequest((current) => current.target === "final-srt" ? current : {
      ...current,
      target: "final-srt",
      // 纠错参考默认退回纯文本：原文是人写的、已经校对过，再切一遍音频窗只是
      // 多花钱多花时间，需要时用户自己往上加（云端本就只有纯文本这一档）。
      correction: { ...current.correction, enabled: true, media: "text" },
    });
  }, [mode, translateOnly]);

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

  // 卡片只负责选中，翻页一律走「下一步」：这几张卡的差别（跑不跑识别、上不上云）
  // 大到值得让用户看清选中态再确认，点一下就跳走会翻错页。
  const chooseNoAxis = () => {
    // 解析结果留着：改主意再回来时不必重挑一次文件，`axis` 本来就只在选中时才成立。
    setAxisOn(false);
  };

  // 选了「我已有轴」
  const chooseAxis = () => {
    setAxisOn(true);
    // if (!axisParse && !axisReading) fileInput.current?.click();
  };

  const chooseMode = (nextMode: ExecutionMode) => {
    const ready = nextMode === "local" ? localReady : cloudAuthenticated;
    // 重选同一张卡不该把设置清回默认：从第 3 步退回来再确认是常见动作。
    if (!ready || nextMode === mode) return;
    applyMode(nextMode);
  };

  const setTarget = (target: TaskRequest["target"]) => {
    if (target === "final-srt" && !limits.llm) return;
    setRequest((current) => ({
      ...current,
      target,
      correction: { ...current.correction, enabled: target === "final-srt" },
      knowledge: target === "raw-srt" || mode === "cloud"
        ? "none"
        : current.knowledge === "none" ? "read" : current.knowledge,
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

  // 日文轴不跑识别，target 已经被钉死；本地缺 LLM 时它整条路都走不通（云端的模型
  // 由 Nonoka Cloud 提供，limits.llm 在那一侧恒为 true）。
  // 写回必须有目标：至少一个主体，且每条新建草稿都填了名称。
  const knowledgeSubjectMissing = knowledgeWrites && (
    !knowledgeLoaded || knowledgeSubjects.length === 0 || knowledgeSubjects.some((item) => !item.subject.trim())
  );
  const startBlocked = busy || !selectedReady || (translateOnly && !limits.llm) || knowledgeSubjectMissing;
  const startRequest: TaskRequest = {
    ...request,
    knowledge_subjects: knowledgeSubjects,
    ...(diarization !== "off" ? { diarization: { level: diarization } } : {}),
  };
  const uploadingCloudAudio = busy && mode === "cloud" && !translateOnly;
  const coverage = axisParse ? clock(Math.max(...axisParse.rows.map((row) => row.t1))) : "";
  const notes = axisParse && axisParse.skipped > 0 ? `跳过 ${axisParse.skipped} 条注释或无效行` : "";
  // 只有空轴会经过识别，所以只有它会踩到这件事：转写引擎不区分说话人，重叠区间里
  // 每个词落在哪一行只能按时间判，两人同时说话时必然有一边串台。其余轴型的文字是
  // 用户自己写好的，重叠再多也无害。
  const overlaps = axisParse && axisKind === "empty" ? axisParse.overlaps : 0;

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
              {/* 第 1 步选完就离开视野，可它决定了后面两步的形态，得一直看得见 */}
              {/* {step !== "axis" && axis && <span className="axis-chip" title={axis.filename}>{`${AXIS_KIND_SHORT[axis.kind]} · ${axis.filename}`}</span>} */}
            </div>
            <h2 id="transcription-title">{step === "axis" ? "选择已有产物" : step === "mode" ? "选择运行环境" : "转写设置"}</h2>
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
        ) : step === "mode" ? (
          <div className="execution-picker">
            <button className={`execution-card ${mode === "local" && localReady ? "chosen" : ""}`} disabled={!localReady} onClick={() => chooseMode("local")}>
              <span className="execution-icon local">⌁</span>
              <span><strong>本地运行</strong><small>{cpuOnly ? "原视频不离开电脑；本机没有可用显卡，将以 CPU 运行，耗时通常是音频时长的十几到几十倍" : "使用本机的 GPU 和模型"}</small></span>
              <em>{localReady ? cpuOnly ? "可用 · CPU" : "可用" : "未就绪"}</em>
            </button>
            {!localReady && <p className="execution-unavailable">{localIssue || "本地运行环境尚未就绪"}<button onClick={onOpenRuntime}>检查运行环境</button></p>}

            <button className={`execution-card ${mode === "cloud" && cloudReady ? "chosen" : ""}`} disabled={!cloudReady} onClick={() => chooseMode("cloud")}>
              <span className="execution-icon cloud">☁</span>
              <span><strong>云端运行</strong><small>{cloudDurationExceeded ? `视频时长 ${clock(entry.duration)}，超过云端 2 小时上限` : translateOnly ? "把这轴交给 Nonoka Cloud 补译文" : "使用 Nonoka Cloud 的资源"}</small></span>
              <em>{cloudDurationExceeded ? "时长超限" : cloudAuthenticated ? cloudRemaining === undefined ? "已登录" : `剩余 ${cloudRemaining} 次` : "未登录"}</em>
            </button>
            {cloudDurationExceeded && <p className="execution-unavailable">云端仅接受最长 2 小时的媒体，请改用本地运行。</p>}
            {/* {!cloudAuthenticated && <p className="execution-unavailable">需要先登录云端账户<button onClick={onOpenAccount}>前往登录</button></p>} */}
          </div>
        ) : (
          <div className="transcription-settings">
            {translateOnly ? (
              <section className="transcription-section">
                <div className="transcription-section-title"><strong>输出内容</strong><span>{mode === "local" ? "本地运行" : "云端运行"}</span></div>
                <p className="transcription-note">{mode === "local"
                  ? "这份轴的原文已经齐了：不做识别，只补中文译文，每一行的时间原样保留。"
                  : "这份轴的原文已经齐了：只补中文译文，每一行的时间原样保留。"}</p>
                {!limits.llm && <p className="option-unavailable">{LLM_KEY_HINT}<button onClick={onOpenKeys}>前往配置</button></p>}
              </section>
            ) : (
              <section className="transcription-section">
                <div className="transcription-section-title"><strong>输出内容</strong><span>{mode === "local" ? "本地运行" : "云端运行"}</span></div>
                <div className="option-cards two-columns">
                  <button className={request.target === "raw-srt" ? "selected" : ""} onClick={() => setTarget("raw-srt")}><strong>原始字幕</strong><small>只进行打轴与原文识别</small></button>
                  <button className={`${request.target === "final-srt" ? "selected" : ""} ${limits.llm ? "" : "unavailable"}`.trim()} aria-disabled={!limits.llm || undefined} title={limits.llm ? undefined : LLM_KEY_HINT} onClick={() => setTarget("final-srt")}><strong>最终字幕</strong><small>继续进行纠错、翻译</small></button>
                </div>
                {!limits.llm && <p className="option-unavailable">{LLM_KEY_HINT}<button onClick={onOpenKeys}>前往配置</button></p>}
                {/* 空轴只保证「行落在你的时间上」。纠错阶段会合并被切碎的句子，合并后的一行
                    只能落在它重叠最多的那一行——要求逐行严格对应时得选原始字幕。 */}
                {/* {axis && finalOutput && <p className="transcription-note">AI 纠错会合并被切碎的句子，合并后的整句落在与它重叠最多的那一行上；要每一行都严格对应你的轴，请选「原始字幕」。</p>} */}
              </section>
            )}

            {!translateOnly && <section className="transcription-section form-grid">
              <label>原始语言<CustomSelect value={request.language} options={languageOptions.map(([value, label]) => ({ value, label }))} onChange={(language) => setRequest((current) => ({ ...current, language }))} /></label>
              {mode === "local" && <label>计算设备<CustomSelect value={request.device} options={deviceOptions(devices)} onChange={(device) => setRequest((current) => ({ ...current, device, gpu_tier: device === "cpu" ? "cpu" : current.gpu_tier === "cpu" ? "auto" : current.gpu_tier }))} /></label>}
              {mode === "local" && <label>显卡档位<CustomSelect value={request.gpu_tier} options={GPU_TIER_OPTIONS} onChange={(gpu_tier) => setRequest((current) => ({ ...current, gpu_tier }))} /></label>}
              {mode === "local" && <label>人声分离<CustomSelect value={request.separate ? "on" : "off"} options={[{ value: "on", label: "开启" }, { value: "off", label: "关闭（输入已是纯人声）", hint: SKIP_SEPARATION_HINT }]} onChange={(value) => setRequest((current) => ({ ...current, separate: value === "on" }))} /></label>}
              {diarizationAvailable && <label>说话人分离<CustomSelect value={diarization} options={[
                { value: "off" as const, label: "关闭" },
                { value: "acoustics" as const, label: "按声音区分说话人" },
                { value: "judge-text" as const, label: "声音 + 文本复核（实验）", disabled: !judgeTextAllowed, hint: DIARIZATION_JUDGE_HINT },
                { value: "judge-audio" as const, label: "声音 + 音频复核（实验）", disabled: !judgeAudioAllowed, hint: mode === "cloud" ? "云端的纠错只读文本，无法边听边复核" : DIARIZATION_JUDGE_HINT },
              ]} onChange={chooseDiarization} /></label>}
            </section>}
            {diarizationNote && <p className="transcription-note">{diarizationNote}{diarizationModelsMissing && <button type="button" className="inline-link" onClick={onOpenRuntime}>前往下载</button>}</p>}

            {finalOutput && <>
              <section className="transcription-section form-grid">
                <label>纠错参考<CustomSelect value={request.correction.media} options={[{ value: "text", label: "仅识别文本" }, ...(mode === "local" ? [{ value: "audio" as const, label: "音频", disabled: !limits.audio, hint: limits.audio ? undefined : mediaHint("supportsAudio") }] : []), ...(supportsVideo ? [{ value: "video" as const, label: "视频多模态", disabled: !limits.video, hint: limits.video ? undefined : mediaHint("supportsVideo") }] : [])]} onChange={(media) => setRequest((current) => ({ ...current, correction: { ...current.correction, media } }))} /></label>
                <label>处理质量<CustomSelect value={request.correction.difficulty} options={[{ value: "efficiency", label: "效率优先" }, { value: "intermediate", label: "均衡" }, { value: "quality", label: "质量优先" }]} onChange={(difficulty) => setRequest((current) => ({ ...current, correction: { ...current.correction, difficulty } }))} /></label>
                <label>快速模式<CustomSelect value={request.correction.fast} options={[{ value: "auto", label: "自动", hint: "字幕放得进单个窗口时一次完成，放不下时自动改走正常流程" }, { value: "off", label: "关闭" }]} onChange={(fast) => setRequest((current) => ({ ...current, correction: { ...current.correction, fast } }))} /></label>
                {mode === "local" && <label>资料检索<CustomSelect value={request.correction.retrieval} options={[{ value: "none", label: "不检索" }, { value: "local", label: "本地检索", disabled: !limits.retrieval, hint: limits.retrieval ? undefined : RETRIEVAL_KEY_HINT }, { value: "native", label: "模型原生检索", disabled: !limits.nativeSearch, hint: limits.nativeSearch ? undefined : NATIVE_SEARCH_HINT }]} onChange={(retrieval) => setRequest((current) => ({ ...current, correction: { ...current.correction, retrieval } }))} /></label>}
                {supportsKnowledge && <label>知识库<CustomSelect value={request.knowledge} options={[{ value: "none", label: "不使用" }, { value: "read", label: "读取知识库" }, { value: "update", label: "读取知识库并自动更新", disabled: mode === "cloud" }]} onChange={setKnowledge} /></label>}
              </section>

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

              <section className="transcription-section prompt-grid">
                <label>背景信息<textarea rows={3} maxLength={4000} placeholder="人物、节目、专有名词等（可选）" value={request.correction.extra_info} onChange={(event) => setRequest((current) => ({ ...current, correction: { ...current.correction, extra_info: event.target.value } }))} /></label>
                <label>翻译风格<textarea rows={3} maxLength={4000} placeholder="例如：简洁自然，保留角色名英文（可选）" value={request.correction.extra_style} onChange={(event) => setRequest((current) => ({ ...current, correction: { ...current.correction, extra_style: event.target.value } }))} /></label>
              </section>
            </>}

            {mode === "local" && <label className="cleanup-check"><input type="checkbox" checked={request.cleanup_intermediate} onChange={(event) => setRequest((current) => ({ ...current, cleanup_intermediate: event.target.checked }))} /><span><strong>任务完成后清理中间文件</strong></span></label>}
            {busy && mode === "cloud" && <div className="cloud-submit-progress" role="status" aria-live="polite" aria-busy="true">
              <div>
                <span className="cloud-submit-icon" aria-hidden="true">☁</span>
                <span>
                  <strong>{uploadingCloudAudio ? "正在准备并上传音轨…" : "正在提交云端任务…"}</strong>
                  <small>{uploadingCloudAudio ? "视频越长，上传所需时间越久；完成后会自动进入任务页面。" : "正在发送字幕与任务设置，完成后会自动进入任务页面。"}</small>
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
            : <button ref={primaryAction} className="primary-button" disabled={busy || axisBlocked} onClick={() => setStep("mode")}>下一步</button>)}
          {step === "mode" && <button ref={primaryAction} className="primary-button" disabled={busy || !selectedReady} onClick={() => setStep("settings")}>下一步</button>}
          {step === "settings" && <button ref={primaryAction} className="primary-button" disabled={startBlocked} onClick={() => void onStart(mode, translateOnly && axis ? { ...startRequest, axis } : startRequest, axis)}>{busy ? uploadingCloudAudio ? "上传中…" : "正在启动…" : "开始任务"}</button>}
        </footer>
      </section>
    </div>
  );
}
