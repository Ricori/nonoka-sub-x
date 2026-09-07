import assert from "node:assert/strict";
import test from "node:test";

import {
  formatTaskDate,
  taskActivityText,
  taskProgress,
  taskStageLabel,
} from "../src/app/format.ts";

test("task dates include hours and minutes", () => {
  const formatted = formatTaskDate("2026-08-24T00:05:00Z");
  assert.match(formatted, /8月24日/);
  assert.match(formatted, /\d{2}:\d{2}/);
  assert.equal(formatTaskDate("not-a-date"), "—");
});

function snapshot(stage, options = {}) {
  return {
    schema: 1,
    task_id: "task-progress",
    provider: "cloud",
    state: options.state ?? "running",
    stage,
    progress: options.progress ?? null,
    engine: { version: "test", commit: "test" },
    requested_capabilities: { target: options.target ?? "final-srt" },
    effective_capabilities: {},
    error: null,
    last_cursor: 0,
    created_at: options.created_at ?? "2026-08-24T00:00:00Z",
    ...(options.started_at ? { started_at: options.started_at } : {}),
    updated_at: "2026-08-24T00:00:00Z",
  };
}

test("estimated progress moves inside a stage without reaching its cap", () => {
  const task = snapshot("vocal");
  const initial = taskProgress(task, 0);
  const later = taskProgress(task, 60_000);
  const muchLater = taskProgress(task, 60 * 60_000);
  assert.equal(initial, 9);
  assert.ok(later > initial);
  assert.ok(muchLater < 36);
});

test("real stage counters take precedence over the time estimate", () => {
  const task = snapshot("aligned", {
    progress: { completed: 3, total: 4, unit: "intervals", message: "" },
  });
  assert.equal(taskProgress(task, 0), 57);
});

test("raw tasks use the shorter stage map and active tasks never reach 100", () => {
  assert.equal(taskProgress(snapshot("aligned", { target: "raw-srt" }), 0), 48);
  assert.ok(taskProgress(snapshot("raw-srt", { target: "raw-srt" }), 60 * 60_000) < 100);
  assert.equal(taskProgress(snapshot("completed", { state: "completed" })), 100);
});

test("internal stage names are replaced with user-facing labels", () => {
  assert.equal(taskStageLabel("vocal"), "处理音频");
  assert.equal(taskStageLabel("aligned"), "语音识别");
  assert.equal(taskStageLabel("final-srt"), "整理最终字幕");
  assert.equal(taskStageLabel("final-srt", "running"), "整理最终字幕");
  assert.equal(taskStageLabel("final-srt", "completed"), "处理完成");
  assert.equal(taskStageLabel("raw-srt", "completed"), "处理完成");
  assert.equal(taskStageLabel("completed", "completed"), "处理完成");
  assert.equal(taskStageLabel("unknown-internal-stage"), "处理中");
  // A task that died before its first stage event has an empty stage, and the
  // row must not read "处理中" next to its own 失败 badge.
  assert.equal(taskStageLabel("", "failed"), "未进入处理环节");
  assert.equal(taskStageLabel("unknown-internal-stage", "cancelled"), "已取消");
  assert.equal(taskStageLabel("", "queued"), "准备中");
  assert.equal(taskStageLabel("vocal", "failed"), "处理音频");
});

test("activity text shows elapsed task time instead of a block timecode", () => {
  const task = snapshot("vocal", {
    progress: { completed: 0, total: 1, unit: "blocks", message: "00:00" },
  });
  assert.equal(taskActivityText(task, Date.parse(task.created_at) + 65_000), "已用时 1:05");
});

test("activity text keeps useful backend detail after elapsed time", () => {
  const task = snapshot("aligned", {
    progress: { completed: 2, total: 5, unit: "intervals", message: "正在识别第 3 段" },
  });
  assert.equal(
    taskActivityText(task, Date.parse(task.created_at) + 9_000),
    "已用时 0:09 · 正在识别第 3 段",
  );
});

test("activity text measures elapsed time from started_at when resuming or restarting", () => {
  const task = snapshot("aligned", {
    created_at: "2026-08-24T00:00:00Z",
    started_at: "2026-08-24T01:30:00Z",
    progress: { completed: 2, total: 5, unit: "intervals", message: "正在识别第 3 段" },
  });
  // 15 seconds after started_at (even though 5415 seconds after created_at)
  assert.equal(
    taskActivityText(task, Date.parse(task.started_at) + 15_000),
    "已用时 0:15 · 正在识别第 3 段",
  );
});

test("completed task activity text overrides in-progress messages", () => {
  const taskWithRunningMsg = snapshot("final-srt", {
    state: "completed",
    progress: { completed: 0, total: null, unit: "", message: "正在处理" },
  });
  assert.equal(taskActivityText(taskWithRunningMsg), "字幕已完成");

  const taskWithProcessingMsg = snapshot("final-srt", {
    state: "completed",
    progress: { completed: 0, total: null, unit: "", message: "处理中" },
  });
  assert.equal(taskActivityText(taskWithProcessingMsg), "字幕已完成");

  const taskWithDetail = snapshot("final-srt", {
    state: "completed",
    progress: { completed: 1, total: 1, unit: "", message: "已有结果，跳过" },
  });
  assert.equal(taskActivityText(taskWithDetail), "已有结果，跳过");
});

