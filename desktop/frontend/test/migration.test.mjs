import assert from "node:assert/strict";
import test from "node:test";

import { isMigratingError, migrationView, reportSections } from "../src/components/migration.ts";

const steps = [
  { id: "runtime-root", title: "Rename the big-file root to runtime", status: "done", download: false },
  { id: "settings", title: "Read settings and keys", status: "running", download: false },
  { id: "layer-download", title: "Download the Python environment and tool layers", status: "pending", download: true },
];

function status(overrides = {}) {
  return { schema: 1, state: "migrating", message: "正在迁移", dataOpen: false, steps, step: "settings", ...overrides };
}

test("the 503 migrating answer is told apart from a sidecar that failed", () => {
  assert.equal(isMigratingError(new Error("sidecar migrating (HTTP 503): 正在迁移旧版数据")), true);
  assert.equal(isMigratingError({ message: "sidecar migrating (HTTP 503): busy" }), true);
  assert.equal(isMigratingError("sidecar migrating (HTTP 503): busy"), true);
  assert.equal(isMigratingError(new Error("sidecar runtime_unavailable (HTTP 503): no runtime")), false);
  assert.equal(isMigratingError(new Error("sidecar is not running")), false);
  assert.equal(isMigratingError(null), false);
});

test("nothing to migrate shows nothing", () => {
  assert.equal(migrationView(null).visible, false);
  assert.equal(migrationView(status({ state: "not-needed", dataOpen: true, report: { settings: { droppedKeys: ["x"] } } })).visible, false);
});

test("the local steps hold the whole workspace and are followed", () => {
  const view = migrationView(status());
  assert.equal(view.visible, true);
  assert.equal(view.blocking, true);
  assert.equal(view.polling, true);
  assert.equal(view.canRetry, false);
  assert.equal(view.dismissible, false);
  assert.equal(view.stepLine, "迁移设置与密钥（2/3）");
});

test("a block, a rollback and an interruption keep the data closed and offer a retry", () => {
  for (const state of ["blocked", "rolled-back", "failed"]) {
    const view = migrationView(status({ state, error: "boom", blockers: ["D:\\ is not plugged in"] }), { dismissed: true });
    assert.equal(view.visible, true, `${state} cannot be dismissed`);
    assert.equal(view.blocking, true);
    assert.equal(view.canRetry, true);
    assert.equal(view.polling, false);
    assert.equal(view.error, "boom");
    assert.deepEqual(view.blockers, ["D:\\ is not plugged in"]);
  }
});

test("the downloads run with the data open and show concise progress", () => {
  const view = migrationView(status({
    state: "downloading",
    dataOpen: true,
    step: "layer-download",
    message: "正在下载新的运行环境；下载完成之前 FineSub 转写不可用",
    progress: { layer: "python", message: "正在下载 Python 环境", completed: 512 * 1024 * 1024, total: 2048 * 1024 * 1024, bytesPerSecond: 4 * 1024 * 1024 },
  }), { dismissed: true });
  assert.equal(view.visible, true, "the download card stays while it runs");
  assert.equal(view.blocking, false);
  assert.equal(view.polling, true);
  assert.equal(view.dismissible, false);
  assert.equal(view.percent, 25);
  assert.match(view.message, /暂不可用本机转写/);
  assert.equal(view.progressLine, "正在下载 Python 环境 · 512.0 MB / 2.0 GB · 4.0 MB/s");
  assert.equal(view.stepLine, "");

  const failed = migrationView(status({ state: "download-failed", dataOpen: true, error: "network" }));
  assert.equal(failed.canRetry, true);
  assert.equal(failed.blocking, false);
  assert.equal(failed.percent, null);
});

test("the result can be dismissed once the migration is complete", () => {
  const complete = status({ state: "completed", dataOpen: true });
  assert.equal(migrationView(complete).visible, true);
  assert.equal(migrationView(complete).dismissible, true);
  assert.equal(migrationView(complete).message, "");
  assert.equal(migrationView(complete, { dismissed: true }).visible, false);
});

test("the report only shows actionable items, hiding dropped settings and removed files", () => {
  const sections = reportSections({
    settings: {
      carried: ["GEMINI_FREE"],
      routes: ["correction"],
      renamed: { FINESUB_KEY_AZURE_SPEECH: "AZURE_SPEECH_KEY" },
      droppedVariables: ["FINESUB_DEBUG"],
      droppedTables: ["ui"],
      droppedKeys: ["llm.temperature"],
      unreadable: ["OPENAI_POOL"],
      notes: [],
    },
    tasks: { tasks: 2, not_converted: { aaaa: { correction_json: "segments do not align" } } },
    cleanup: { removed: ["finesub.env.pre-migration", "finesub-data\\"], unreadableKeysRemoved: ["OPENAI_POOL"] },
  }, { aaaa: "第一集" });
  const byTitle = Object.fromEntries(sections.map((section) => [section.title, section.items]));
  assert.deepEqual(Object.keys(byTitle), ["需要重新填写的密钥", "未转换的历史任务"]);
  assert.deepEqual(byTitle["需要重新填写的密钥"], ["OPENAI_POOL"]);
  assert.deepEqual(byTitle["未转换的历史任务"], ["第一集：纠错结果未转换（segments do not align）"]);
  assert.equal(reportSections({
    settings: { droppedVariables: ["A"], droppedTables: ["B"], droppedKeys: ["C"], unreadable: [], renamed: {} },
    cleanup: { removed: ["finesub.toml"] },
  }).length, 0, "dropped settings and removed files are omitted");
  assert.deepEqual(reportSections(undefined), []);
});
