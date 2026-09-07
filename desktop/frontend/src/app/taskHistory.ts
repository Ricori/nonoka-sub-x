import type { TaskHistoryEntry } from "./types.ts";
import { taskHistoryLimit } from "./types.ts";

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
