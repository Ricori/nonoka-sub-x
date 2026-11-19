import { useEffect, useMemo, useRef, useState } from "react";
import type { MediaEntry } from "../bridge/library.ts";
import { formatTaskDate, taskActivityText, taskProgress, taskStageLabel, taskStateLabel, taskTranscriptionEngine, transcriptionEngineLabel } from "../app/format.ts";
import type { TaskHistoryEntry } from "../app/types.ts";
import { activeStates, recoverableStates } from "../app/types.ts";
import { Mascot } from "../components/Mascot.tsx";
import "./TasksPage.css";
import { Notice } from "../components/Notice.tsx";
import type { NoticeTone } from "../components/Notice.tsx";
import { TaskLogPanel } from "../components/TaskLogPanel.tsx";
import type { TaskLogSource } from "../components/TaskLogPanel.tsx";

interface TasksPageProps {
  tasks: TaskHistoryEntry[];
  media: MediaEntry[];
  activeCount: number;
  message: string;
  messageTone: NoticeTone;
  pipelineError?: string;
  /** Each task must read events from the provider that owns it. */
  logSources?: Partial<Record<TaskHistoryEntry["provider"], TaskLogSource>>;
  onNavigateLibrary: () => void;
  onOpenEditor?: (entry: MediaEntry) => void;
  onClearTasks: () => Promise<void>;
  onTaskAction: (item: TaskHistoryEntry, action: "cancel" | "resume") => Promise<void>;
  onDismissMessage: () => void;
}

function AnimatedTaskProgress({ snapshot }: { snapshot: TaskHistoryEntry["snapshot"] }) {
  const stageStartedAt = useRef(Date.now());
  const [display, setDisplay] = useState(() => taskProgress(snapshot));
  const active = activeStates.has(snapshot.state);

  useEffect(() => {
    stageStartedAt.current = Date.now();
    setDisplay((current) => snapshot.state === "completed"
      ? 100
      : Math.max(current, taskProgress(snapshot)));
  }, [snapshot.stage, snapshot.state]);

  useEffect(() => {
    const next = taskProgress(snapshot, Date.now() - stageStartedAt.current);
    setDisplay((current) => snapshot.state === "completed"
      ? 100
      : active ? Math.max(current, next) : next);
  }, [active, snapshot]);

  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => {
      const next = taskProgress(snapshot, Date.now() - stageStartedAt.current);
      setDisplay((current) => Math.max(current, next));
    }, 750);
    return () => window.clearInterval(timer);
  }, [active, snapshot]);

  return (
    <div className="task-row-progress-wrap">
      <div
        className={`task-row-progress ${active ? "estimated" : ""}`}
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(display)}
      >
        <span style={{ width: `${display}%` }} />
      </div>
      <small>{Math.round(display)}%</small>
    </div>
  );
}

function TaskActivityText({ snapshot }: { snapshot: TaskHistoryEntry["snapshot"] }) {
  const [now, setNow] = useState(Date.now());
  const active = activeStates.has(snapshot.state);

  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active]);

  return <p className="task-activity">{taskActivityText(snapshot, now)}</p>;
}

export function TasksPage(props: TasksPageProps) {
  const { tasks, media, message, messageTone, pipelineError, logSources, onNavigateLibrary, onOpenEditor, onClearTasks, onTaskAction, onDismissMessage } = props;
  const [openLogs, setOpenLogs] = useState<ReadonlySet<string>>(() => new Set());
  const toggleLog = (taskId: string) => setOpenLogs((current) => {
    const next = new Set(current);
    if (!next.delete(taskId)) next.add(taskId);
    return next;
  });

  const mediaById = useMemo(() => new Map(media.map((entry) => [entry.id, entry])), [media]);
  const activeTasks = tasks.filter((item) => activeStates.has(item.snapshot.state));
  const historyTasks = tasks.filter((item) => !activeStates.has(item.snapshot.state));

  const renderRow = (item: TaskHistoryEntry, mode: "active" | "history") => {
    const snapshot = item.snapshot;
    const isCompleted = snapshot.state === "completed";
    const isActive = mode === "active";
    const isRecoverable = recoverableStates.has(snapshot.state);
    const logSource = logSources?.[item.provider];
    const logsAvailable = logSource !== undefined;
    const logsOpen = logsAvailable && openLogs.has(item.taskId);
    const engineLabel = `${item.provider === "cloud" ? "☁ 云端" : "本机"} · ${transcriptionEngineLabel(taskTranscriptionEngine(snapshot, item.provider))}`;
    const matchedMedia = mediaById.get(item.mediaId);
    const canEditDirect = isCompleted && Boolean(onOpenEditor && matchedMedia?.documentAvailable);

    return (
      <article className={`task-row task-${snapshot.state}${isActive ? " is-active" : " is-compact"}`} key={item.taskId}>
        <div className="task-state-icon" aria-hidden="true">
          {isCompleted ? "✓" : isActive ? "↻" : isRecoverable ? "!" : "·"}
        </div>
        <div className="task-row-copy">
          <div className="task-row-title">
            <strong title={item.title}>{item.title}</strong>
            {!isCompleted && (
              <span className={`badge task-state task-state-${snapshot.state}`}>
                {taskStateLabel(snapshot.state)}
              </span>
            )}
            <small className="badge">{engineLabel}</small>
          </div>
          {isActive ? (
            <>
              <div className="task-row-meta">
                <span>{taskStageLabel(snapshot.stage, snapshot.state)}</span>
                <code>{item.taskId.slice(0, 12)}</code>
                <time dateTime={snapshot.updated_at}>{formatTaskDate(snapshot.updated_at)}</time>
              </div>
              <AnimatedTaskProgress snapshot={snapshot} />
              <TaskActivityText snapshot={snapshot} />
            </>
          ) : (
            <div className="task-row-meta">
              <time dateTime={snapshot.updated_at}>{formatTaskDate(snapshot.updated_at)}</time>
              <span className="task-meta-sep" aria-hidden="true">·</span>
              <code>{item.taskId.slice(0, 12)}</code>
              {!isCompleted && (
                <>
                  <span className="task-meta-sep" aria-hidden="true">·</span>
                  <span className="task-row-reason">{taskActivityText(snapshot, Date.now())}</span>
                </>
              )}
            </div>
          )}
        </div>
        <div className="task-row-actions">
          {logsAvailable && (
            <button
              aria-expanded={logsOpen}
              className={logsOpen ? "quiet-button btn-sm task-log-toggle open" : "quiet-button btn-sm task-log-toggle"}
              onClick={() => toggleLog(item.taskId)}
              title={item.provider === "cloud" ? "查看云端任务事件" : "查看本机引擎的逐行输出"}
            >
              日志 {logsOpen ? "▴" : "▾"}
            </button>
          )}
          {isActive && (
            <button className="danger-button btn-sm" onClick={() => void onTaskAction(item, "cancel")}>取消</button>
          )}
          {isRecoverable && (
            <button className="primary-button btn-sm resume" onClick={() => void onTaskAction(item, "resume")}>继续</button>
          )}
          {isCompleted && (
            canEditDirect && matchedMedia && onOpenEditor ? (
              <button
                className="accent-button btn-sm edit-subtitles"
                onClick={() => onOpenEditor(matchedMedia)}
                title="直接打开字幕编辑器"
              >
                编辑字幕
              </button>
            ) : (
              <button
                className="accent-button btn-sm view-media"
                onClick={onNavigateLibrary}
                title="前往媒体库查看"
              >
                查看媒体
              </button>
            )
          )}
        </div>
        {logsOpen && logSource !== undefined && (
          <TaskLogPanel active={isActive} provider={item.provider} source={logSource} taskId={item.taskId} />
        )}
      </article>
    );
  };

  return (
    <section className="task-history">
      <Notice className="task-history-message" message={message} tone={messageTone} onDismiss={onDismissMessage} />
      <Notice className="task-history-message" message={pipelineError ?? ""} />
      {tasks.length === 0 ? (
        <div className="panel task-empty">
          <Mascot pose="wave" />
          <h2>从第一个视频开始</h2>
          <p>在媒体库选择视频并开始任务，任务进度、运行位置和历史结果都会集中显示在这里。</p>
          <button className="primary-button" onClick={onNavigateLibrary}>前往媒体库</button>
        </div>
      ) : (
        <>
          {activeTasks.length > 0 && (
            <div className="task-section">
              <header className="task-section-header">
                <h2 className="section-title">
                  进行中
                  <span className="running-summary"><i />{activeTasks.length}</span>
                </h2>
              </header>
              <div className="task-active-list">
                {activeTasks.map((item) => renderRow(item, "active"))}
              </div>
            </div>
          )}

          {historyTasks.length > 0 && (
            <div className="task-section">
              <header className="task-section-header">
                <h2 className="section-title">
                  处理历史
                  <small>{historyTasks.length} 条记录</small>
                </h2>
                <button
                  className="ghost-button btn-sm task-clear-button"
                  title={`清除 ${historyTasks.length} 条已结束任务记录`}
                  onClick={() => void onClearTasks()}
                >
                  清空历史
                </button>
              </header>
              <div className="panel task-history-panel">
                {historyTasks.map((item) => renderRow(item, "history"))}
              </div>
            </div>
          )}
        </>
      )}
    </section>
  );
}
