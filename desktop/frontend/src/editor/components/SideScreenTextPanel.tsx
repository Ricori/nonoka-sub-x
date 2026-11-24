import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { screenTextBridge } from "../../bridge/screenText.ts";
import { desktopWindows } from "../../bridge/windows.ts";
import { CustomSelect } from "../../components/CustomSelect.tsx";
import type { TaskEvent, TaskProgress } from "../../providers/types.ts";
import { isOcrTrack } from "../lib/screenTextDoc.ts";
import { canErase, cancelErase, restoreOriginalVideo, revealErasedVideo, startErase } from "../lib/screenTextErase.ts";
import {
  cancelScreenText,
  importGameTextBundle,
  refreshScreenTextEnvironment,
  requestStartScreenText,
  restoreScreenTextTask,
  undoTrial,
} from "../lib/screenTextMerge.ts";
import { docStore } from "../store/docStore.ts";
import { playStore } from "../store/playStore.ts";
import { patchScreenTextOptions, screenTextStore } from "../store/screenTextStore.ts";
import { modalStore } from "../store/uiStore.ts";
import { videoStore } from "../store/videoStore.ts";
import { viewStore } from "../store/viewStore.ts";
import { fmt } from "../utils.ts";
import { NumField, PropRow, Segmented, SideSection, Switch } from "./ui/fields.tsx";

const ACTIVE_STATES = new Set(["queued", "running"]);
const INSTALL_ISSUE_CODES = new Set([
  "missing_screen_text",
  "screen_text_layer_missing",
  "screen_text_layer_invalid",
  "screen_text_layer_outdated",
  "screen_text_not_installed",
  "ocr_package_missing",
]);

const LOG_POLL_MS = 1500;
const LOG_LINE_LIMIT = 400;
const LOG_TYPE_LABELS: Record<string, string> = {
  started: "启动",
  stage: "阶段",
  progress: "进度",
  warning: "警告",
  log: "日志",
  completed: "完成",
  failed: "失败",
  cancelled: "取消",
};

function stageLabel(stage: string, state: string): string {
  if (state === "queued") return "排队中…";
  switch (stage) {
    case "screen-read":
    case "extract":
    case "screen-extract":
    case "recognize":
      return "正在识别画面文字…";
    case "screen-tracks":
    case "tracks":
    case "build-tracks":
      return "正在整理文字区域…";
    case "match":
      return "正在匹配游戏剧本…";
    case "screen-translate":
    case "translate":
      return "正在翻译文字…";
    case "erase":
      return "正在抹除画面原字…";
    default:
      return stage ? `处理中（${stage}）…` : "正在处理文字识别…";
  }
}

const ERASE_PHASE_LABELS = {
  erase: "正在抹除原字…",
  mux: "正在合回音轨…",
  apply: "正在换上新视频…",
} as const;

function formatSince(startMs: number, nowMs: number): string {
  return startMs ? formatElapsed(new Date(startMs).toISOString(), nowMs) : "";
}

function progressNumbers(progress: TaskProgress | null | undefined): { pct: number | null; text: string } {
  if (!progress || !progress.total || progress.total <= 0) {
    return { pct: null, text: "" };
  }
  const completed = Math.max(0, Number(progress.completed) || 0);
  const total = Number(progress.total);
  const ratio = Math.max(0, Math.min(1, completed / total));
  const pct = Math.round(ratio * 100);
  if (progress.unit === "permille" && total === 1000) {
    return { pct, text: `${(completed / 10).toFixed(1)}%` };
  }
  const unitLabel = progress.unit === "items"
    ? "条"
    : progress.unit === "steps"
      ? "步"
      : progress.unit === "seconds"
        ? "秒"
        : progress.unit;
  const counter = unitLabel ? `${completed}/${total} ${unitLabel}` : `${completed}/${total}`;
  return { pct, text: `${pct}% · ${counter}` };
}

function formatElapsed(startedIso: string | undefined, nowMs: number): string {
  if (!startedIso) return "";
  const startMs = Date.parse(startedIso);
  if (Number.isNaN(startMs)) return "";
  const secs = Math.max(0, Math.floor((nowMs - startMs) / 1000));
  const mm = String(Math.floor(secs / 60)).padStart(2, "0");
  const ss = String(secs % 60).padStart(2, "0");
  return `${mm}:${ss}`;
}

function logEventTime(value: string): string {
  const parsed = Date.parse(value);
  if (Number.isNaN(parsed)) return "--:--:--";
  return new Date(parsed).toLocaleTimeString("zh-CN", { hour12: false });
}

function logEventText(event: TaskEvent): string {
  const payload = event.payload ?? {};
  const str = (v: unknown) => (typeof v === "string" ? v : "");
  const stage = str(payload.stage);
  const message = str(payload.message) || str(payload.detail);
  if (event.type === "progress") {
    const completed = Number(payload.completed ?? 0);
    const total = Number(payload.total ?? 0);
    const unit = str(payload.unit);
    let counter = "";
    if (unit === "permille" && total === 1000) {
      counter = `${(completed / 10).toFixed(1)}%`;
    } else if (total > 0) {
      const u = unit === "items" ? "条" : unit === "steps" ? "步" : unit === "seconds" ? "秒" : unit;
      counter = `${completed}/${total} ${u}`.trim();
    }
    return [stage, counter, message].filter(Boolean).join(" · ");
  }
  return [stage, str(payload.code), message].filter(Boolean).join(" · ") || LOG_TYPE_LABELS[event.type] || event.type;
}

function taskStateMeta(state: string): { label: string; cls: string } {
  switch (state) {
    case "queued":
      return { label: "排队中", cls: "active" };
    case "running":
      return { label: "进行中", cls: "active" };
    case "completed":
      return { label: "已完成", cls: "ok" };
    case "failed":
      return { label: "失败", cls: "bad" };
    case "cancelled":
      return { label: "已取消", cls: "muted" };
    case "interrupted":
      return { label: "已中断", cls: "bad" };
    default:
      return { label: state || "未知", cls: "muted" };
  }
}

function ScreenTextLogPopover({
  title,
  taskId,
  taskState,
  active,
  onClose,
}: {
  title: string;
  taskId: string;
  taskState: string;
  active: boolean;
  onClose: () => void;
}) {
  const [events, setEvents] = useState<TaskEvent[]>([]);
  const [error, setError] = useState("");
  const [copied, setCopied] = useState(false);
  const [follow, setFollow] = useState(true);
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null);
  const [dragging, setDragging] = useState(false);
  const boxRef = useRef<HTMLDivElement | null>(null);
  const dragRef = useRef<{ dx: number; dy: number; w: number; h: number } | null>(null);
  const bodyRef = useRef<HTMLDivElement | null>(null);
  const cursorRef = useRef(0);

  useEffect(() => {
    let cancelled = false;
    let inFlight = false;
    cursorRef.current = 0;
    setEvents([]);
    setError("");
    const pull = async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        const page = await screenTextBridge.taskEvents(taskId, cursorRef.current);
        if (cancelled) return;
        setError("");
        const fresh = (page?.events ?? []).filter((ev) => ev.cursor > cursorRef.current);
        if (fresh.length === 0) return;
        cursorRef.current = fresh[fresh.length - 1].cursor;
        setEvents((curr) => [...curr, ...fresh].slice(-LOG_LINE_LIMIT));
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      } finally {
        inFlight = false;
      }
    };
    void pull();
    if (!active) return () => { cancelled = true; };
    const timer = window.setInterval(() => void pull(), LOG_POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [active, taskId]);

  useEffect(() => {
    const body = bodyRef.current;
    if (body && follow) body.scrollTop = body.scrollHeight;
  }, [events, follow]);

  const lines = useMemo(
    () => events.map((ev) => `${logEventTime(ev.timestamp)} [${LOG_TYPE_LABELS[ev.type] ?? ev.type}] ${logEventText(ev)}`),
    [events],
  );

  const copy = useCallback(() => {
    navigator.clipboard.writeText(lines.join("\n")).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    }).catch(() => setCopied(false));
  }, [lines]);

  const onHeadPointerDown = useCallback((ev: React.PointerEvent<HTMLDivElement>) => {
    if (ev.button !== 0 || (ev.target as HTMLElement).closest("button")) return;
    const box = boxRef.current;
    if (!box) return;
    const rect = box.getBoundingClientRect();
    dragRef.current = { dx: ev.clientX - rect.left, dy: ev.clientY - rect.top, w: rect.width, h: rect.height };
    setPos({ left: rect.left, top: rect.top });
    setDragging(true);
    ev.currentTarget.setPointerCapture(ev.pointerId);
    ev.preventDefault();
  }, []);

  const onHeadPointerMove = useCallback((ev: React.PointerEvent<HTMLDivElement>) => {
    const d = dragRef.current;
    if (!d) return;
    const pad = 8;
    const maxLeft = Math.max(pad, window.innerWidth - d.w - pad);
    const maxTop = Math.max(pad, window.innerHeight - d.h - pad);
    setPos({
      left: Math.min(maxLeft, Math.max(pad, ev.clientX - d.dx)),
      top: Math.min(maxTop, Math.max(pad, ev.clientY - d.dy)),
    });
  }, []);

  const onHeadPointerUp = useCallback((ev: React.PointerEvent<HTMLDivElement>) => {
    if (!dragRef.current) return;
    dragRef.current = null;
    setDragging(false);
    if (ev.currentTarget.hasPointerCapture(ev.pointerId)) {
      ev.currentTarget.releasePointerCapture(ev.pointerId);
    }
  }, []);

  const status = taskStateMeta(taskState);
  const host = typeof document !== "undefined"
    ? (document.querySelector(".professional-editor") ?? document.body)
    : null;
  if (!host) return null;

  const floatStyle = pos ? { left: `${pos.left}px`, top: `${pos.top}px`, right: "auto", bottom: "auto" } : undefined;

  return createPortal(
    <div
      ref={boxRef}
      className={"st-log-float" + (dragging ? " dragging" : "")}
      style={floatStyle}
      role="dialog"
      aria-label={title}
    >
      <div
        className="st-log-head"
        onPointerDown={onHeadPointerDown}
        onPointerMove={onHeadPointerMove}
        onPointerUp={onHeadPointerUp}
        onPointerCancel={onHeadPointerUp}
      >
        <div className="st-log-title">
          <strong>{title}</strong>
          <span className={`st-log-badge is-${status.cls}`}>{status.label}</span>
          <span className="st-log-count">{events.length} 条</span>
        </div>
        <div className="st-log-head-actions">
          <button type="button" className="side-text-btn" disabled={lines.length === 0} onClick={copy}>
            {copied ? "已复制" : "复制"}
          </button>
          <button type="button" className="st-log-close" title="关闭日志" aria-label="关闭日志" onClick={onClose}>
            ×
          </button>
        </div>
      </div>
      <div
        className="st-log-body"
        ref={bodyRef}
        onScroll={(ev) => {
          const el = ev.currentTarget;
          setFollow(el.scrollHeight - el.scrollTop - el.clientHeight <= 24);
        }}
      >
        {events.length === 0 ? (
          <p className="st-log-empty">{error || (active ? "等待引擎输出…" : "该任务没有日志记录。")}</p>
        ) : (
          events.map((ev) => (
            <p key={ev.cursor} className={`st-log-line is-${ev.type}`}>
              <time>{logEventTime(ev.timestamp)}</time>
              <b>{LOG_TYPE_LABELS[ev.type] ?? ev.type}</b>
              <span>{logEventText(ev)}</span>
            </p>
          ))
        )}
      </div>
      {error && events.length > 0 && <div className="st-log-err">{error}</div>}
      {!follow && active && (
        <button type="button" className="st-log-follow" onClick={() => setFollow(true)}>
          回到底部 ↓
        </button>
      )}
    </div>,
    host,
  );
}

export function SideScreenTextPanel() {
  const state = screenTextStore.use((s) => s);
  const audioOnly = videoStore.use((s) => s.audioOnly);
  const erasedFrom = videoStore.use((s) => s.erasedFrom);
  const duration = viewStore.use((s) => s.duration);
  docStore.use((s) => s.version);

  /** 日志框看的是哪个任务：文字识别，还是抹除原字的 LaMa 任务 */
  const [showLog, setShowLog] = useState<"ocr" | "erase" | null>(null);
  const toggleLog = (which: "ocr" | "erase") => setShowLog((v) => (v === which ? null : which));
  const [nowMs, setNowMs] = useState(() => Date.now());
  const running = Boolean(state.task && ACTIVE_STATES.has(state.task.state));
  const erasing = state.erase.running;

  const ocrTracks = docStore.get().tracks.filter(isOcrTrack);
  const ocrCueCount = ocrTracks.reduce((sum, track) => sum + track.segs.length, 0);

  useEffect(() => {
    void refreshScreenTextEnvironment();
    void restoreScreenTextTask();
  }, []);

  useEffect(() => {
    if (!running && !erasing) return;
    setNowMs(Date.now());
    const timer = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [running, erasing]);

  // 1. 音频文件没有视频画面
  if (audioOnly) {
    return (
      <div className="side-page">
        <div className="side-panel">
          <div className="st-empty">
            <div className="st-empty-title">纯音频项目</div>
            <p className="st-empty-desc">当前媒体没有视频画面，无法进行文字识别。</p>
          </div>
        </div>
      </div>
    );
  }

  // 2. macOS 不支持
  const unsupported = state.platform === "darwin"
    || state.check?.issues.some((issue) => issue.code === "unsupported_platform");
  if (unsupported) {
    return (
      <div className="side-page">
        <div className="side-panel">
          <div className="st-empty">
            <div className="st-empty-title">当前系统暂不支持</div>
            <p className="st-empty-desc">文字识别目前仅支持 Windows，macOS 暂不提供该功能。</p>
          </div>
        </div>
      </div>
    );
  }

  // 3. 未安装（只在主页面「设置 → 运行环境」安装，编辑器里不提供安装）
  const installMissing = (state.layerState && state.layerState !== "ready")
    || (!state.layerState && state.check?.issues.some((issue) => INSTALL_ISSUE_CODES.has(issue.code)));
  if (installMissing) {
    return (
      <div className="side-page">
        <div className="side-panel">
          <div className="st-empty">
            <div className="st-empty-title">未安装文字识别层</div>
            <p className="st-empty-desc">
              {state.layerAvailable
                ? "需要先在主页面「设置 → 运行环境」安装文字识别层，安装完成后这里会自动变为可用。"
                : "当前版本暂未提供文字识别层下载，可先在主页面「设置 → 运行环境」查看状态或离线安装。"}
            </p>
            <div className="st-actions">
              <button type="button" className="btn primary" onClick={() => void desktopWindows.openRuntimeSettings()}>
                打开设置
              </button>
              <button type="button" className="btn" disabled={state.checking}
                onClick={() => void refreshScreenTextEnvironment()}>
                {state.checking ? "检查中…" : "重新检查"}
              </button>
            </div>
          </div>
        </div>
      </div>
    );
  }

  const progress = state.task?.progress;
  const { pct, text: progressText } = progressNumbers(progress);
  const indeterminate = pct === null || pct === 0;
  const elapsed = running ? formatElapsed(state.task?.created_at, nowMs) : "";
  const checkIssues = state.check?.issues ?? [];
  const checkWarnings = state.check?.warnings ?? [];
  const blocked = Boolean(state.check && !state.check.ready);
  const engineReady = Boolean(state.check?.ready);
  const { options } = state;
  const erasable = canErase();
  const { erase } = state;
  const eraseElapsed = erasing ? formatSince(erase.startedAt, nowMs) : "";

  return (
    <div className="side-page side-screentext-page">
      <div className="side-panel">
        {/* 4. 运行中 */}
        {running && state.task && (
          <SideSection title={state.taskIsTrial ? "试跑 1 分钟" : "正在识别"}>
            <div className="st-progress-card">
              <div className="st-progress-head">
                <span>{stageLabel(state.task.stage, state.task.state)}</span>
                {progressText && <span className="st-mono">{progressText}</span>}
              </div>
              <div className={"st-bar" + (indeterminate ? " indeterminate" : "")}>
                <i style={{ width: indeterminate ? "40%" : `${pct}%` }} />
              </div>
              <div className="st-progress-meta">
                <span>{progress?.message || "正在处理…"}</span>
                {elapsed && <span className="st-mono">已用 {elapsed}</span>}
              </div>
              <div className="st-actions">
                <button type="button" className="btn" disabled={state.busy} onClick={() => void cancelScreenText()}>
                  取消任务
                </button>
                <button type="button" className="btn" onClick={() => toggleLog("ocr")}>
                  {showLog === "ocr" ? "关闭日志" : "查看日志"}
                </button>
              </div>
            </div>
          </SideSection>
        )}

        {/* 5. 试 1 分钟：结果已直接写进轨道，这里只给撤销 */}
        {!running && state.trialUndo && (
          <SideSection title="试跑结果">
            <div className="st-trial-card">
              <div className="st-trial-summary">
                <strong>已写入 {state.trialUndo.cueCount} 处文字</strong>
                <span className="st-mono">{fmt(state.trialUndo.start)} – {fmt(state.trialUndo.end)}</span>
              </div>
              <div className="st-actions">
                <button type="button" className="btn" onClick={undoTrial}>撤销</button>
              </div>
            </div>
          </SideSection>
        )}


        {/* 引擎就绪状态 */}
        <div className="st-hw-bar">
          <span className="st-engine-status">
            <i className={"st-dot" + (state.check ? (engineReady ? " ok" : " bad") : "")} />
            {state.checking && !state.check
              ? "正在检查引擎…"
              : engineReady
                ? "引擎已就绪"
                : state.check
                  ? "引擎未就绪"
                  : "引擎状态未知"}
          </span>
          <div className="st-hw-actions">
            {state.task?.task_id && (
              <button type="button" className="side-text-btn" onClick={() => toggleLog("ocr")}>
                {showLog === "ocr" ? "关闭日志" : "日志"}
              </button>
            )}
            <button type="button" className="side-text-btn" disabled={state.checking}
              onClick={() => void refreshScreenTextEnvironment()}>
              {state.checking ? "检查中…" : "刷新"}
            </button>
          </div>
        </div>

        {checkIssues.map((issue, idx) => (
          <div key={issue.code + idx} className="st-notice bad">{issue.message}</div>
        ))}
        {checkWarnings.map((warn, idx) => (
          <div key={warn.code + idx} className="st-notice warn">{warn.message}</div>
        ))}
        {state.error && <div className="st-notice bad">{state.error}</div>}

        {/* 完成摘要 / 已有文字识别轨 */}
        {!running && ocrTracks.length > 0 && (
          <div className="st-notice ok">
            <span>当前文档已有 {ocrTracks.length} 条文字识别轨（共 {ocrCueCount} 句）。</span>
            <button type="button" className="side-text-btn"
              onClick={() => modalStore.set({ sideTab: "track" })}>查看轨道</button>
          </div>
        )}

        {/* 设置区 */}
        <SideSection title="识别范围">
          <PropRow label="时间范围">
            <Segmented value={options.rangeMode}
              options={[{ value: "all", label: "整片" }, { value: "custom", label: "自定义" }]}
              onPick={(value) => patchScreenTextOptions({
                rangeMode: value,
                ...(value === "custom" && options.end <= options.start
                  ? { start: Math.floor(playStore.get().t || 0), end: Math.ceil(duration || 60) }
                  : {}),
              })} />
          </PropRow>
          {options.rangeMode === "custom" && (
            <div className="st-range-row">
              <NumField label="起" value={Math.round(options.start)} min={0} max={Math.max(1, Math.ceil(duration))}
                suffix="s" onInput={(value) => patchScreenTextOptions({ start: value })} />
              <NumField label="止" value={Math.round(options.end)} min={0} max={Math.max(1, Math.ceil(duration))}
                suffix="s" onInput={(value) => patchScreenTextOptions({ end: value })} />
              <button type="button" className="side-text-btn" title="把开始时间设为当前播放头"
                onClick={() => patchScreenTextOptions({ start: Math.floor(playStore.get().t || 0) })}>
                当前位置
              </button>
            </div>
          )}
          <PropRow label="跳过主字幕带" title="跳过画面底部常规语音字幕区，避免和默认语音轨重复">
            <Switch on={options.skipMain} onToggle={(skipMain) => patchScreenTextOptions({ skipMain })} />
          </PropRow>
          <PropRow label="识别区域" title="限制只识别画面里框出的区域，不框选则识别整帧">
            <div className="st-roi-ctl">
              <button type="button" className={"side-text-btn" + (state.drawingRoi ? " on" : "")}
                onClick={() => screenTextStore.set({ drawingRoi: !state.drawingRoi })}>
                {state.drawingRoi ? "取消框选" : "在画面上框选"}
              </button>
              {options.roi && (
                <button type="button" className="side-text-btn"
                  onClick={() => patchScreenTextOptions({ roi: null })}>
                  恢复整帧
                </button>
              )}
            </div>
          </PropRow>
          {state.drawingRoi && (
            <div className="st-hint">在左侧预览画面上按住左键拖出识别区域，按 Esc 取消。</div>
          )}
          {options.roi && !state.drawingRoi && (
            <div className="st-hint st-mono">
              区域：{Math.round(options.roi[0] * 100)}%,{Math.round(options.roi[1] * 100)}% – {Math.round(options.roi[2] * 100)}%,{Math.round(options.roi[3] * 100)}%
            </div>
          )}
        </SideSection>

        <SideSection title="译文与遮盖">
          <PropRow label="翻译">
            <Segmented value={options.translate}
              options={[{ value: "zh", label: "简体中文" }, { value: "none", label: "仅原文" }]}
              onPick={(translate) => patchScreenTextOptions({ translate })} />
          </PropRow>
          <PropRow label="遮盖方式" title="纯色底板盖住原字，译文按原字的位置和大小摆放；也可以在下方用 LaMa 直接抹掉原字">
            <Segmented value={options.cover}
              options={[{ value: "plate", label: "纯色底板" }, { value: "none", label: "不画底板" }]}
              onPick={(cover) => patchScreenTextOptions({ cover })} />
          </PropRow>
          <PropRow label="游戏剧本" title="导入 gtd-bundle/1 游戏剧本包后，识别到的游戏台词会直接匹配官方中文译文">
            <CustomSelect className="sd-select" ariaLabel="游戏剧本" value={options.game}
              options={[
                { value: "", label: "不使用" },
                ...state.games.map((item) => ({
                  value: item.game,
                  label: item.version ? `${item.game} (${item.version})` : item.game,
                })),
              ]}
              onChange={(game) => patchScreenTextOptions({ game })} />
          </PropRow>
          <PropRow label="">
            <button type="button" className="side-text-btn" disabled={state.busy}
              onClick={() => void importGameTextBundle()}>
              导入游戏剧本包 (.zip)…
            </button>
          </PropRow>
          <PropRow label="运算设备">
            <CustomSelect className="sd-select" ariaLabel="文字识别运算设备" value={options.device}
              options={[
                { value: "auto", label: "自动（优先 GPU）" },
                { value: "cuda", label: "GPU (CUDA)" },
                { value: "cpu", label: "CPU（较慢）" },
              ]}
              onChange={(device) => patchScreenTextOptions({ device: device as "auto" | "cuda" | "cpu" })} />
          </PropRow>
        </SideSection>

        {/* 操作按钮 */}
        {!running && (
          <div className="sd-foot st-foot">
            <button type="button" className="btn primary wide"
              disabled={state.busy || blocked || erasing}
              onClick={() => void requestStartScreenText(false)}>
              {ocrTracks.length > 0 ? "重新识别整片" : "开始文字识别"}
            </button>
            <button type="button" className="btn wide"
              disabled={state.busy || blocked || erasing}
              title="从当前播放头起识别 1 分钟，结果直接写进轨道，可撤销"
              onClick={() => void requestStartScreenText(true)}>
              从当前位置试 1 分钟
            </button>
          </div>
        )}

        {/* 抹除原字：LaMa 抹掉整片原字，换成新视频（原视频不动） */}
        <SideSection title="抹除原字">
          {erasing ? (
            <div className="st-progress-card">
              <div className="st-progress-head">
                <span>{erase.cancelling ? "正在取消…" : ERASE_PHASE_LABELS[erase.phase]}</span>
                {erase.pct !== null && <span className="st-mono">{erase.pct}%</span>}
              </div>
              <div className={"st-bar" + (erase.pct === null || erase.pct === 0 ? " indeterminate" : "")}>
                <i style={{ width: erase.pct === null || erase.pct === 0 ? "40%" : `${erase.pct}%` }} />
              </div>
              <div className="st-progress-meta">
                <span>可以继续编辑字幕</span>
                {eraseElapsed && <span className="st-mono">已用 {eraseElapsed}</span>}
              </div>
              <div className="st-actions">
                <button type="button" className="btn" disabled={erase.cancelling || erase.phase === "apply"}
                  onClick={cancelErase}>
                  取消
                </button>
                <button type="button" className="btn" disabled={!state.eraseLog} onClick={() => toggleLog("erase")}>
                  {showLog === "erase" ? "关闭日志" : "查看日志"}
                </button>
              </div>
            </div>
          ) : (
            <>
              {erasedFrom ? (
                <div className="st-notice ok">
                  <span>正在用抹字后的视频，底板已隐藏。</span>
                </div>
              ) : (
                <div className="st-hint">
                  {erasable
                    ? "抹掉画面里的原字。建议先确认好各处蒙版位置后再抹除。"
                    : "先做文字识别后才能抹除。"}
                </div>
              )}
              <div className="st-actions">
                <button type="button" className={"btn" + (erasedFrom ? "" : " primary")}
                  disabled={!erasable || running || state.busy || blocked}
                  title={erasedFrom ? "按当前的原字框，从原视频重新抹一遍" : undefined}
                  onClick={() => void startErase()}>
                  {erasedFrom ? "重新抹除" : "开始抹除"}
                </button>
                {erasedFrom && (
                  <>
                    <button type="button" className="btn" onClick={revealErasedVideo}>打开所在文件夹</button>
                    <button type="button" className="btn" onClick={() => void restoreOriginalVideo()}>换回原视频</button>
                  </>
                )}
                {state.eraseLog && (
                  <button type="button" className="btn" onClick={() => toggleLog("erase")}>
                    {showLog === "erase" ? "关闭日志" : "日志"}
                  </button>
                )}
              </div>
            </>
          )}
        </SideSection>
      </div>
      {showLog === "ocr" && state.task?.task_id && (
        <ScreenTextLogPopover
          title="文字识别日志"
          taskId={state.task.task_id}
          taskState={state.task.state}
          active={running}
          onClose={() => setShowLog(null)}
        />
      )}
      {showLog === "erase" && state.eraseLog && (
        <ScreenTextLogPopover
          title="抹除原字日志"
          taskId={state.eraseLog.taskId}
          taskState={state.eraseLog.state}
          active={state.eraseLog.state === "running"}
          onClose={() => setShowLog(null)}
        />
      )}
    </div>
  );
}
