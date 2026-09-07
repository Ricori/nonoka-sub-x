import assert from "node:assert/strict";
import test from "node:test";

import { visibleTaskEvents } from "../src/components/taskLog.ts";

function event(cursor, type, payload = {}) {
  return { cursor, task_id: "vid_test", type, timestamp: `2026-09-03T00:00:${String(cursor).padStart(2, "0")}Z`, payload };
}

test("cloud task log keeps key events and hides operator diagnostics", () => {
  const input = [
    event(1, "started", { stage: "queued" }),
    event(2, "log", { stage: "starting", message: "正在准备云端运行环境" }),
    event(3, "handoff", { stage: "vocal", message: "正在启动 Vocal GPU 阶段" }),
    event(4, "warning", { code: "routing-preset", message: "unused audio binding" }),
    event(7, "warning", { code: "routing-profile", message: "provider calibration" }),
    event(8, "warning", { code: "srt-line-budget", message: "/__modal/volumes/vo-x/tasks/vid_test/subtitle.srt: Segment 1 line has 25.5 weighted characters; limit is 25." }),
    event(5, "warning", { code: "resource-budget", message: "内存接近上限" }),
    event(6, "completed"),
  ];

  assert.deepEqual(visibleTaskEvents(input, "cloud").map(({ cursor }) => cursor), [1, 3, 5, 6]);
});

test("unchanged consecutive cloud progress heartbeats occupy one line", () => {
  const input = [
    event(1, "progress", { stage: "translated-srt", completed: 0, total: 1, unit: "window", message: "first" }),
    event(2, "progress", { stage: "translated-srt", completed: 0, total: 1, unit: "window", message: "latest" }),
    event(3, "progress", { stage: "translated-srt", completed: 1, total: 1, unit: "window" }),
  ];

  const visible = visibleTaskEvents(input, "cloud");
  assert.deepEqual(visible.map(({ cursor }) => cursor), [2, 3]);
  assert.equal(visible[0].payload.message, "latest");
});

test("local task logs remain complete", () => {
  const input = [event(1, "log"), event(2, "warning", { code: "routing-profile" })];
  assert.equal(visibleTaskEvents(input, "local"), input);
});
