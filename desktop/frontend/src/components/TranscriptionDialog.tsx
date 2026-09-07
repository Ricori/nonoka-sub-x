import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { MediaEntry } from "../bridge/library.ts";
import { cloudTaskRequest, localTaskRequest } from "../home/defaultRequest.ts";
import { GPU_TIERS } from "../providers/types.ts";
import type { Capabilities, GpuTier, TaskAxis, TaskRequest } from "../providers/types.ts";
import type { FineSubSettingsState } from "../bridge/settings.ts";
import type { ExecutionMode } from "../app/types.ts";
import { CustomSelect } from "./CustomSelect.tsx";
import {
  AXIS_KIND_HINT, AXIS_KIND_LABEL, AXIS_KIND_SHORT, parseAxisFile, recolumn,
  type AxisKind, type AxisParse,
} from "../subtitles/assAxis.ts";
import { routeForTaskGroup, routeServesMedia, type MediaCapability } from "./llmRouting.ts";
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

const LLM_KEY_HINT = "尚未配置模型提供商：请到设置里选择提供商与全局模型并保存，否则无法进行 LLM 纠错、翻译与知识处理。";
const RETRIEVAL_KEY_HINT = "需要 Exa、Tavily 或 Gemini Key 才能进行本地联网检索。";
const NATIVE_SEARCH_HINT = "需要 Gemini Key：模型原生检索依赖 Gemini 内置搜索。";

type Step = "axis" | "mode" | "settings";

interface KeyLimits {
  llm: boolean;
  gemini: boolean;
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

function requestFor(mode: ExecutionMode, entry: MediaEntry, capabilities: Capabilities | null): TaskRequest {
  const request = mode === "local" ? localTaskRequest(entry) : cloudTaskRequest(entry);
  if (mode === "local" && capabilities?.devices[0]?.id) request.device = capabilities.devices[0].id;
  return request;
}

/** 把缺少 Key 的选项拉回可用值；无需改动时返回原对象。 */
function withinLimits(request: TaskRequest, limits: KeyLimits): TaskRequest {
  const target = request.target === "final-srt" && !limits.llm ? "raw-srt" : request.target;
  const media = (request.correction.media === "audio" && !limits.audio) || (request.correction.media === "video" && !limits.video)
    ? "text"
    : request.correction.media;
  const retrieval = (request.correction.retrieval === "local" && !limits.retrieval) || (request.correction.retrieval === "native" && !limits.gemini)
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

  // 只用来定第 2 步一开始选中哪张卡（轴要到第 1 步选完才存在，所以这里无须看它）
  const preferredMode = initialMode === "local"
    ? localReady ? "local" : cloudAuthenticated ? "cloud" : "local"
    : cloudAuthenticated ? "cloud" : localReady ? "local" : "cloud";
  const [mode, setMode] = useState<ExecutionMode>(preferredMode);
  const [request, setRequest] = useState<TaskRequest>(() => requestFor(preferredMode, entry, localCapabilities));
  const selectedReady = mode === "local" ? localReady : cloudAuthenticated;
  const selectedCapabilities = mode === "local" ? localCapabilities : cloudCapabilities;
  const supportsVideo = selectedCapabilities?.features.video_multimodal === true;
  const supportsKnowledge = selectedCapabilities?.features.knowledge === true;
  const finalOutput = request.target === "final-srt";
  const devices = localCapabilities?.devices ?? [];
  const speakers = axisParse && axisParse.speakers.length >= 2 ? axisParse.speakers : [];

  const limitsFor = useCallback((target: ExecutionMode, retrieval: TaskRequest["correction"]["retrieval"]): KeyLimits => {
    // 云端运行的凭据由 Nonoka Cloud 管理；读不到本地设置时也不做限制，避免误锁死。
    const keys = target === "local" ? settings : null;
    const configured = (name: string) => keys?.keys.some((key) => key.configured && key.name === name) === true;
    const gemini = !keys || configured("GEMINI_FREE") || configured("GEMINI_PAID");
    const video = (target === "local" ? localCapabilities : cloudCapabilities)?.features.video_multimodal === true;
    // 音视频窗看的是「谁来答」，而自引擎 0.5.0 起答的人只有一个：钉住的模型组
    // 替换整条链，后面不再挂任何兜底。所以这里问的是选中的那个模型自己收不收，
    // 与有没有 Gemini Key 无关。开着本地检索时每窗还有一个查询轮，它跟着纠错
    // 参考走却用「窗口规划」那一格的模型，所以两格都得能收——否则引擎会在开跑前
    // 的能力校验里直接拒掉整个任务。
    const serves = (capability: MediaCapability) => {
      if (!keys) return true;
      if (!routeServesMedia(keys.modelRouting, "correction", capability)) return false;
      return retrieval !== "local" || routeServesMedia(keys.modelRouting, "planning", capability);
    };
    return {
      // 只认已保存的全局模型：装了 codex CLI、或在设置里选了却没保存，都不算配置好。
      llm: !keys || keys.llmReady,
      gemini,
      retrieval: !keys || keys.retrievalKeyConfigured,
      audio: target === "local" && serves("supportsAudio"),
      video: video && serves("supportsVideo"),
    };
  }, [cloudCapabilities, localCapabilities, settings]);
  const limits = useMemo(
    () => limitsFor(mode, request.correction.retrieval),
    [limitsFor, mode, request.correction.retrieval],
  );

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
    if (request.correction.retrieval === "local" && freePool("planning")) {
      return `Gemini 免费池不承担${label}窗：开启资料检索后每窗的查询轮使用「窗口规划」模型，请把这一格改指到付费池的 Gemini 模型或自带${label}能力的本地 CLI 模型，或把资料检索设为「不检索」。`;
    }
    return `开启资料检索后每窗还有一个查询轮，它使用「窗口规划」的模型，而该模型不支持${label}：请把这一格也指到支持的模型，或把资料检索设为「不检索」。`;
  }, [request.correction.retrieval, settings]);

  const summary = useMemo(() => {
    if (axis && importOnly) return `直接导入 ${axis.rows.length} 条字幕，不做任何处理`;
    if (axis && translateOnly) return `按你的轴补中文译文，不做识别（${axis.rows.length} 条）`;
    const output = finalOutput ? "AI 纠错与翻译后的最终字幕" : "识别与断句后的原始字幕";
    const source = request.language === "auto" ? "自动检测语言" : languageOptions.find(([id]) => id === request.language)?.[1] ?? request.language;
    return `${source} · ${output}${axis ? " · 按导入的轴重排" : ""}`;
  }, [axis, finalOutput, importOnly, request.language, translateOnly]);

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

  /** 换运行环境就换一整份默认请求：两边的 source 形态、目标和显存项都不一样。 */
  const applyMode = useCallback((nextMode: ExecutionMode) => {
    setMode(nextMode);
    const next = requestFor(nextMode, entry, localCapabilities);
    setRequest(withinLimits(next, limitsFor(nextMode, next.correction.retrieval)));
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
        : current.knowledge === "none" ? "collect" : current.knowledge,
    }));
  };

  // 日文轴不跑识别，target 已经被钉死；本地缺 LLM 时它整条路都走不通（云端的模型
  // 由 Nonoka Cloud 提供，limits.llm 在那一侧恒为 true）。
  const startBlocked = busy || !selectedReady || (translateOnly && !limits.llm);
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
              <span><strong>我没有轴</strong><small>从头识别与断句，再按继续纠错和翻译</small></span>
              <em>默认</em>
            </button>

            <button className={`execution-card ${axisOn ? "chosen" : ""}`} onClick={chooseAxis}>
              <span className="execution-icon axis">≡</span>
              <span><strong>我已有轴</strong><small>空轴 / 单语轴 / 双语轴，成片保持你打好的时间</small></span>
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
              <span><strong>本地运行</strong><small>原视频不离开电脑，使用本机转写引擎 与 GPU</small></span>
              <em>{localReady ? "可用" : "未就绪"}</em>
            </button>
            {!localReady && <p className="execution-unavailable">{localIssue || "本地运行环境尚未就绪"}<button onClick={onOpenRuntime}>检查运行环境</button></p>}

            <button className={`execution-card ${mode === "cloud" && cloudAuthenticated ? "chosen" : ""}`} disabled={!cloudAuthenticated} onClick={() => chooseMode("cloud")}>
              <span className="execution-icon cloud">☁</span>
              <span><strong>云端运行</strong><small>{translateOnly ? "不上传音轨，只把这条轴交给 Nonoka Cloud 补译文" : "提取音轨后交给 Nonoka Cloud 处理"}</small></span>
              <em>{cloudAuthenticated ? cloudRemaining === undefined ? "已登录" : `剩余 ${cloudRemaining} 次` : "未登录"}</em>
            </button>
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
                  <button className={request.target === "raw-srt" ? "selected" : ""} onClick={() => setTarget("raw-srt")}><strong>原始字幕</strong><small>完成识别与断句，不调用 LLM</small></button>
                  <button className={`${request.target === "final-srt" ? "selected" : ""} ${limits.llm ? "" : "unavailable"}`.trim()} aria-disabled={!limits.llm || undefined} title={limits.llm ? undefined : LLM_KEY_HINT} onClick={() => setTarget("final-srt")}><strong>最终字幕</strong><small>继续进行纠错、翻译与知识处理</small></button>
                </div>
                {!limits.llm && <p className="option-unavailable">{LLM_KEY_HINT}<button onClick={onOpenKeys}>前往配置</button></p>}
                {/* 空轴只保证「行落在你的时间上」。纠错阶段会合并被切碎的句子，合并后的一行
                    只能落在它重叠最多的那一行——要求逐行严格对应时得选原始字幕。 */}
                {/* {axis && finalOutput && <p className="transcription-note">AI 纠错会合并被切碎的句子，合并后的整句落在与它重叠最多的那一行上；要每一行都严格对应你的轴，请选「原始字幕」。</p>} */}
              </section>
            )}

            {!translateOnly && <section className="transcription-section form-grid">
              <label>原始语言<CustomSelect value={request.language} options={languageOptions.map(([value, label]) => ({ value, label }))} onChange={(language) => setRequest((current) => ({ ...current, language }))} /></label>
              {mode === "local" && <label>计算设备<CustomSelect value={request.device} options={devices.length > 0 ? devices.map((device) => ({ value: device.id, label: `${device.name} · ${Math.round(device.memory_mb / 1024)} GB` })) : [{ value: "cuda", label: "自动选择 NVIDIA GPU" }]} onChange={(device) => setRequest((current) => ({ ...current, device }))} /></label>}
              {mode === "local" && <label>显卡档位<CustomSelect value={request.gpu_tier} options={GPU_TIER_OPTIONS} onChange={(gpu_tier) => setRequest((current) => ({ ...current, gpu_tier }))} /></label>}
              {mode === "local" && <label>人声分离<CustomSelect value={request.separate ? "on" : "off"} options={[{ value: "on", label: "开启" }, { value: "off", label: "关闭（输入已是纯人声）", hint: SKIP_SEPARATION_HINT }]} onChange={(value) => setRequest((current) => ({ ...current, separate: value === "on" }))} /></label>}
            </section>}

            {finalOutput && <>
              <section className="transcription-section form-grid">
                <label>纠错参考<CustomSelect value={request.correction.media} options={[{ value: "text", label: "仅识别文本" }, ...(mode === "local" ? [{ value: "audio" as const, label: "音频", disabled: !limits.audio, hint: limits.audio ? undefined : mediaHint("supportsAudio") }] : []), ...(supportsVideo ? [{ value: "video" as const, label: "视频多模态", disabled: !limits.video, hint: limits.video ? undefined : mediaHint("supportsVideo") }] : [])]} onChange={(media) => setRequest((current) => ({ ...current, correction: { ...current.correction, media } }))} /></label>
                <label>处理质量<CustomSelect value={request.correction.difficulty} options={[{ value: "efficiency", label: "效率优先" }, { value: "intermediate", label: "均衡" }, { value: "quality", label: "质量优先" }]} onChange={(difficulty) => setRequest((current) => ({ ...current, correction: { ...current.correction, difficulty } }))} /></label>
                <label>快速模式<CustomSelect value={request.correction.fast} options={[{ value: "auto", label: "自动" }, { value: "on", label: "开启" }, { value: "off", label: "关闭" }]} onChange={(fast) => setRequest((current) => ({ ...current, correction: { ...current.correction, fast } }))} /></label>
                {mode === "local" && <label>资料检索<CustomSelect value={request.correction.retrieval} options={[{ value: "none", label: "不检索" }, { value: "local", label: "本地检索", disabled: !limits.retrieval, hint: limits.retrieval ? undefined : RETRIEVAL_KEY_HINT }, { value: "native", label: "模型原生检索", disabled: !limits.gemini, hint: limits.gemini ? undefined : NATIVE_SEARCH_HINT }]} onChange={(retrieval) => setRequest((current) => ({ ...current, correction: { ...current.correction, retrieval } }))} /></label>}
                {supportsKnowledge && <label>知识库<CustomSelect value={request.knowledge} options={[{ value: "none", label: "不使用" }, { value: "collect", label: "读取知识库" }, { value: "update", label: "读取知识库并自动更新", disabled: mode === "cloud" }]} onChange={(knowledge) => setRequest((current) => ({ ...current, knowledge }))} /></label>}
              </section>

              <section className="transcription-section prompt-grid">
                <label>背景信息<textarea rows={3} maxLength={4000} placeholder="人物、节目、专有名词或上下文（可选）" value={request.correction.extra_info} onChange={(event) => setRequest((current) => ({ ...current, correction: { ...current.correction, extra_info: event.target.value } }))} /></label>
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
          {step === "settings" && <button ref={primaryAction} className="primary-button" disabled={startBlocked} onClick={() => void onStart(mode, translateOnly && axis ? { ...request, axis } : request, axis)}>{busy ? uploadingCloudAudio ? "上传中…" : "正在启动…" : "开始任务"}</button>}
        </footer>
      </section>
    </div>
  );
}
