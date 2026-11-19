import assert from "node:assert/strict";
import test from "node:test";

import {
  activeTasksByMedia,
  applyActiveTaskSnapshots,
  canStartMediaTask,
  countActiveTasks,
  reconcileTaskHistory,
} from "../src/app/taskHistory.ts";

function entry(taskId, state, updatedAt = "2026-08-31T00:00:00Z", { provider = "local", mediaId = `media-${taskId}`, startedAt } = {}) {
  return {
    taskId,
    provider,
    mediaId,
    title: taskId,
    snapshot: {
      schema: 1,
      task_id: taskId,
      provider,
      state,
      stage: "",
      progress: null,
      engine: { version: "test", commit: "test" },
      requested_capabilities: {},
      effective_capabilities: {},
      error: null,
      last_cursor: 0,
      created_at: updatedAt,
      ...(startedAt ? { started_at: startedAt } : {}),
      updated_at: updatedAt,
    },
  };
}

test("refresh does not restore a completed task removed while refresh was in flight", () => {
  const completed = entry("completed", "completed");
  assert.deepEqual(reconcileTaskHistory([], [completed]), []);
});

test("refresh updates tasks that remain in history", () => {
  const running = entry("kept", "running");
  const completed = entry("kept", "completed", "2026-08-31T00:01:00Z");
  assert.deepEqual(reconcileTaskHistory([running], [completed]), [completed]);
});

test("active tasks on one media do not block starting tasks on other media", () => {
  const tasks = [
    entry("t2", "queued", "2026-08-31T00:02:00Z", { provider: "cloud", mediaId: "media-b" }),
    entry("t1", "running", "2026-08-31T00:01:00Z", { provider: "local", mediaId: "media-a" }),
    entry("t0", "completed", "2026-08-31T00:00:00Z", { provider: "local", mediaId: "media-c" }),
  ];
  const byMedia = activeTasksByMedia(tasks);
  assert.deepEqual([...byMedia.keys()], ["media-b", "media-a"]);
  assert.equal(canStartMediaTask({ id: "media-a", available: true }, byMedia), false);
  assert.equal(canStartMediaTask({ id: "media-b", available: true }, byMedia), false);
  assert.equal(canStartMediaTask({ id: "media-c", available: true }, byMedia), true);
  assert.equal(canStartMediaTask({ id: "media-d", available: true }, byMedia), true);
  assert.equal(canStartMediaTask({ id: "media-d", available: false }, byMedia), false);
});

test("countActiveTasks sums local tasks and remote/local cloud tasks without double counting", () => {
  const tasks = [
    entry("l1", "running", "2026-08-31T00:01:00Z", { provider: "local", mediaId: "m1" }),
    entry("l2", "queued", "2026-08-31T00:02:00Z", { provider: "local", mediaId: "m2" }),
    entry("c1", "running", "2026-08-31T00:03:00Z", { provider: "cloud", mediaId: "m3" }),
    entry("done", "completed", "2026-08-31T00:00:00Z", { provider: "local", mediaId: "m4" }),
  ];
  assert.equal(countActiveTasks(tasks, 0), 3);
  assert.equal(countActiveTasks(tasks, 1), 3);
  assert.equal(countActiveTasks(tasks, 2), 4);
});

test("applyActiveTaskSnapshots updates multiple running tasks and never revives a cancelled task", () => {
  const t1 = entry("t1", "running", "2026-08-31T00:01:00Z", { startedAt: "2026-08-31T00:00:30Z" });
  const t2 = entry("t2", "cancelled", "2026-08-31T00:02:00Z");
  const t3 = entry("t3", "queued", "2026-08-31T00:02:00Z", { provider: "cloud" });
  const updates = new Map([
    ["t1", { ...t1.snapshot, state: "completed", started_at: undefined, updated_at: "2026-08-31T00:03:00Z" }],
    ["t2", { ...t2.snapshot, state: "running", updated_at: "2026-08-31T00:01:59Z" }],
    ["t3", { ...t3.snapshot, state: "running", stage: "vocal", updated_at: "2026-08-31T00:03:00Z" }],
  ]);
  const next = applyActiveTaskSnapshots([t1, t2, t3], updates);
  assert.equal(next[0].snapshot.state, "completed");
  assert.equal(next[0].snapshot.started_at, "2026-08-31T00:00:30Z");
  assert.equal(next[1].snapshot.state, "cancelled");
  assert.equal(next[2].snapshot.state, "running");
  assert.equal(next[2].snapshot.stage, "vocal");
});
