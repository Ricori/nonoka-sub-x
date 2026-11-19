import type { MediaEntry } from "../bridge/library.ts";
import type { TaskSnapshot } from "../providers/types.ts";
import type { TaskHistoryEntry } from "./types.ts";
import { activeStates, taskHistoryLimit } from "./types.ts";

/**
 * Map each media item that currently has a queued/running task to its newest
 * active task entry. Multiple media items can run concurrently.
 */
export function activeTasksByMedia(
  tasks: readonly TaskHistoryEntry[],
): Map<string, TaskHistoryEntry> {
  const byMedia = new Map<string, TaskHistoryEntry>();
  for (const item of tasks) {
    if (!item.mediaId || !activeStates.has(item.snapshot.state)) continue;
    if (!byMedia.has(item.mediaId)) byMedia.set(item.mediaId, item);
  }
  return byMedia;
}

/**
 * Total active tasks across local and cloud providers. Cloud may report
 * additional running jobs started on another device that are not yet in local
 * task history.
 */
export function countActiveTasks(
  tasks: readonly TaskHistoryEntry[],
  cloudRunning = 0,
): number {
  let localActive = 0;
  let cloudActive = 0;
  for (const item of tasks) {
    if (!activeStates.has(item.snapshot.state)) continue;
    if (item.provider === "cloud") cloudActive += 1;
    else localActive += 1;
  }
  return localActive + Math.max(cloudActive, Math.max(0, cloudRunning));
}

/**
 * Whether a media item may start a new transcription task. Running tasks on
 * other media items never block starting a new task; only an already-running
 * task on this exact media item locks its own start button.
 */
export function canStartMediaTask(
  entry: Pick<MediaEntry, "id" | "available">,
  activeByMedia: ReadonlyMap<string, TaskHistoryEntry>,
): boolean {
  return entry.available && !activeByMedia.has(entry.id);
}

/**
 * Merge polled snapshots into active rows only, so a poll that started before
 * a local cancel cannot revive a row that has already reached a terminal state.
 */
export function applyActiveTaskSnapshots(
  current: readonly TaskHistoryEntry[],
  updates: ReadonlyMap<string, TaskSnapshot>,
): TaskHistoryEntry[] {
  return current.map((item) => {
    if (!activeStates.has(item.snapshot.state)) return item;
    const snapshot = updates.get(item.taskId);
    if (!snapshot) return item;
    const startedAt = snapshot.started_at || item.snapshot.started_at;
    return {
      ...item,
      snapshot: {
        ...snapshot,
        ...(startedAt ? { started_at: startedAt } : {}),
      },
    };
  });
}

/**
 * Apply fresh snapshots only to entries that still exist in the local history.
 * In particular, a refresh that started before "clear" must not restore rows
 * after the local file and UI have removed them.
 */
export function reconcileTaskHistory(
  current: TaskHistoryEntry[],
  refreshed: TaskHistoryEntry[],
): TaskHistoryEntry[] {
  const refreshedByTask = new Map(refreshed.map((item) => [item.taskId, item]));
  return current.map((item) => {
    const updated = refreshedByTask.get(item.taskId);
    if (!updated) return item;
    const startedAt = updated.snapshot.started_at || item.snapshot.started_at;
    return {
      ...updated,
      snapshot: {
        ...updated.snapshot,
        ...(startedAt ? { started_at: startedAt } : {}),
      },
    };
  })
    .sort((left, right) => Date.parse(right.snapshot.updated_at) - Date.parse(left.snapshot.updated_at))
    .slice(0, taskHistoryLimit);
}
