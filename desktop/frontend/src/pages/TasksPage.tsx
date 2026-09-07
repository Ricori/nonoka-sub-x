import { useEffect, useRef, useState } from "react";
import type { MediaEntry } from "../bridge/library.ts";
import { formatTaskDate, taskActivityText, taskProgress, taskStageLabel, taskStateLabel } from "../app/format.ts";
import type { TaskHistoryEntry } from "../app/types.ts";
import { activeStates, recoverableStates } from "../app/types.ts";
import { Mark } from "../components/Mark.tsx";
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

  return <p>{taskActivityText(snapshot, now)}</p>;
}

export function TasksPage(props: TasksPageProps) {
  const { tasks, activeCount, message, messageTone, pipelineError, logSources, onNavigateLibrary, onClearTasks, onTaskAction, onDismissMessage } = props;
  const clearableCount = tasks.filter((item) => !activeStates.has(item.snapshot.state)).length;
  const [openLogs, setOpenLogs] = useState<ReadonlySet<string>>(() => new Set());
  const toggleLog = (taskId: string) => setOpenLogs((current) => {
    const next = new Set(current);
    if (!next.delete(taskId)) next.add(taskId);
    return next;
  });
  return (
    <section className="task-history">
      <header className="task-history-header">
        <div>{tasks.length > 0 && <Mark>{tasks.length} 个任务</Mark>}<h2>处理历史</h2></div>
        <div className="task-history-header-actions">
          {activeCount > 0 && <span className="running-summary"><i />{activeCount} 个进行中</span>}
          {tasks.length > 0 && (
            <button
              className="task-clear-button"
              disabled={clearableCount === 0}
              title={clearableCount === 0 ? "进行中的任务会保留在列表中" : `清除 ${clearableCount} 条已结束任务记录`}
              onClick={() => void onClearTasks()}
            >
              清空列表
            </button>
          )}
        </div>
      </header>
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
        <div className="task-list">
          {tasks.map((item) => {
            const snapshot = item.snapshot;
            const logSource = logSources?.[item.provider];
            const logsAvailable = logSource !== undefined;
            const logsOpen = logsAvailable && openLogs.has(item.taskId);
            return (
              <article className={`task-row task-${snapshot.state}`} key={item.taskId}>
                <div className="task-state-icon" aria-hidden="true">{snapshot.state === "completed" ? "✓" : activeStates.has(snapshot.state) ? "↻" : recoverableStates.has(snapshot.state) ? "!" : "·"}</div>
                <div className="task-row-copy">
                  <div className="task-row-title"><strong>{item.title}</strong><span className={`task-state task-state-${snapshot.state}`}>{taskStateLabel(snapshot.state)}</span><small>{item.provider === "cloud" ? "☁ 云端" : "本机"}</small></div>
                  <div className="task-row-meta"><span>{taskStageLabel(snapshot.stage, snapshot.state)}</span><code>{item.taskId.slice(0, 12)}</code><time dateTime={snapshot.updated_at}>{formatTaskDate(snapshot.updated_at)}</time></div>
                  <AnimatedTaskProgress snapshot={snapshot} />
                  <TaskActivityText snapshot={snapshot} />
                </div>
                <div className="task-row-actions">
                  {logsAvailable && (
                    <button
                      aria-expanded={logsOpen}
                      className={logsOpen ? "task-log-toggle open" : "task-log-toggle"}
                      onClick={() => toggleLog(item.taskId)}
                      title={item.provider === "cloud" ? "查看云端任务事件" : "查看本机引擎的逐行输出"}
                    >
                      日志 {logsOpen ? "▴" : "▾"}
                    </button>
                  )}
                  {activeStates.has(snapshot.state) && <button onClick={() => void onTaskAction(item, "cancel")}>取消</button>}
                  {recoverableStates.has(snapshot.state) && <button className="resume" onClick={() => void onTaskAction(item, "resume")}>继续</button>}
                  {snapshot.state === "completed" && (
                    <button
                      className="view-media"
                      onClick={onNavigateLibrary}
                      title="前往媒体库查看"
                    >
                      查看媒体
                    </button>
                  )}
                </div>
                {logsOpen && logSource !== undefined && (
                  <TaskLogPanel active={activeStates.has(snapshot.state)} provider={item.provider} source={logSource} taskId={item.taskId} />
                )}
              </article>
            );
          })}
        </div>
      )}
    </section>
  );
}
