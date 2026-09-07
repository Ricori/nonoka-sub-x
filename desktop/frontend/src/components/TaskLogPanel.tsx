import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ProviderID, TaskEvent, TaskEventPage } from "../providers/types.ts";
import { visibleTaskEvents } from "./taskLog.ts";
import "./TaskLogPanel.css";

/** Reading a task's events is a provider capability. The page passes in the
    matching provider's reader rather than coupling the panel to either one. */
export type TaskLogSource = (taskId: string, afterCursor: number) => Promise<TaskEventPage>;

const logPollIntervalMs = 6_000;
const logLineLimit = 1000;
const followThresholdPx = 24;

const typeLabels: Record<string, string> = {
  started: "启动",
  stage: "阶段",
  progress: "进度",
  warning: "警告",
  handoff: "交接",
  log: "日志",
  completed: "完成",
  failed: "失败",
  cancelled: "取消",
};

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

// A structured field (the stabilizer's `tags`, for one) is worth reading as
// JSON; `String()` renders every one of them as `[object Object]`.
function fieldText(value: unknown): string {
  if (value !== null && typeof value === "object") {
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  }
  return String(value);
}

function eventText(event: TaskEvent): string {
  const payload = event.payload ?? {};
  const stage = text(payload.stage);
  const message = text(payload.message) || text(payload.detail);
  if (event.type === "progress") {
    const completed = Number(payload.completed ?? 0);
    const total = Number(payload.total ?? 0);
    const unit = text(payload.unit);
    const counter = total > 0 ? `${completed}/${total} ${unit}`.trim() : `${completed} ${unit}`.trim();
    return [stage, counter, message].filter(Boolean).join(" · ");
  }
  const fields = payload.fields && typeof payload.fields === "object" && !Array.isArray(payload.fields)
    ? Object.entries(payload.fields as Record<string, unknown>).map(([key, value]) => `${key}=${fieldText(value)}`).join(" ")
    : "";
  const detail = [message, text(payload.impact), text(payload.action), fields].filter(Boolean).join(" · ");
  return [stage, text(payload.code), detail].filter(Boolean).join(" · ") || typeLabels[event.type] || event.type;
}

function eventTime(value: string): string {
  const parsed = Date.parse(value);
  if (Number.isNaN(parsed)) return "--:--:--";
  return new Date(parsed).toLocaleTimeString("zh-CN", { hour12: false });
}

export function TaskLogPanel({ taskId, active, provider, source }: { taskId: string; active: boolean; provider: ProviderID; source: TaskLogSource }) {
  const [events, setEvents] = useState<TaskEvent[]>([]);
  const [error, setError] = useState("");
  const [copied, setCopied] = useState(false);
  const [follow, setFollow] = useState(true);
  const bodyRef = useRef<HTMLDivElement | null>(null);
  const cursorRef = useRef(0);

  // Events are append-only and cursor addressed, so a running task is polled
  // from the last cursor seen rather than re-read from the beginning.
  useEffect(() => {
    let cancelled = false;
    cursorRef.current = 0;
    setEvents([]);
    setError("");
    const pull = async () => {
      try {
        const page = await source(taskId, cursorRef.current);
        if (cancelled) return;
        setError("");
        if (page.events.length === 0) return;
        cursorRef.current = page.events[page.events.length - 1].cursor;
        setEvents((current) => [...current, ...page.events].slice(-logLineLimit));
      } catch (value) {
        if (!cancelled) setError(value instanceof Error ? value.message : String(value));
      }
    };
    void pull();
    if (!active) return () => { cancelled = true; };
    const timer = window.setInterval(() => void pull(), logPollIntervalMs);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [active, source, taskId]);

  useEffect(() => {
    const body = bodyRef.current;
    if (body && follow) body.scrollTop = body.scrollHeight;
  }, [events, follow]);

  const visibleEvents = useMemo(() => visibleTaskEvents(events, provider), [events, provider]);
  const lines = useMemo(
    () => visibleEvents.map((event) => `${eventTime(event.timestamp)} [${typeLabels[event.type] ?? event.type}] ${eventText(event)}`),
    [visibleEvents],
  );

  const copy = useCallback(() => {
    navigator.clipboard.writeText(lines.join("\n")).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    }).catch(() => setCopied(false));
  }, [lines]);

  return (
    <section className="task-log-panel">
      <header className="task-log-heading">
        <strong>{provider === "cloud" ? "关键日志" : "详细日志"}</strong>
        <small>{active ? provider === "cloud" ? "云端任务关键事件" : "本机引擎实时输出" : `共 ${visibleEvents.length} 条记录`}</small>
        <button disabled={lines.length === 0} onClick={copy} type="button">{copied ? "已复制" : "复制全部"}</button>
      </header>
      <div
        className="task-log-body"
        onScroll={(event) => {
          const body = event.currentTarget;
          setFollow(body.scrollHeight - body.scrollTop - body.clientHeight <= followThresholdPx);
        }}
        ref={bodyRef}
      >
        {visibleEvents.length === 0
          ? <p className="task-log-empty">{error || (active ? "等待引擎输出…" : "该任务没有留下日志记录。")}</p>
          : visibleEvents.map((event) => (
            <p className={`task-log-line task-log-${event.type}`} key={event.cursor}>
              <time>{eventTime(event.timestamp)}</time>
              <b>{typeLabels[event.type] ?? event.type}</b>
              <span>{eventText(event)}</span>
            </p>
          ))}
      </div>
      {error && visibleEvents.length > 0 && <p className="task-log-error">{error}</p>}
      {!follow && active && (
        <button className="task-log-follow" onClick={() => setFollow(true)} type="button">回到最新 ↓</button>
      )}
    </section>
  );
}
