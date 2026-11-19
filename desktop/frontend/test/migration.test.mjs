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
  assert.equal(view.stepLine, "第 2 / 3 步：Read settings and keys");
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

test("the downloads run with the data open and say FineSub waits for them", () => {
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
  assert.match(view.message, /FineSub 转写不可用/);
  assert.equal(view.progressLine, "正在下载 Python 环境 · 512.0 MB / 2.0 GB · 4.0 MB/s");
  assert.equal(view.stepLine, "第 3 / 3 步：Download the Python environment and tool layers");

  const failed = migrationView(status({ state: "download-failed", dataOpen: true, error: "network" }));
  assert.equal(failed.canRetry, true);
  assert.equal(failed.blocking, false);
  assert.equal(failed.percent, null);
});

test("the result can be dismissed once the migration is complete", () => {
  const complete = status({ state: "completed", dataOpen: true });
  assert.equal(migrationView(complete).visible, true);
  assert.equal(migrationView(complete).dismissible, true);
  assert.equal(migrationView(complete, { dismissed: true }).visible, false);
});

test("the report names what was dropped, what could not be converted and what was deleted", () => {
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
  const find = (fragment) => Object.entries(byTitle).find(([title]) => title.includes(fragment))?.[1];
  assert.deepEqual(find("没有带过来的旧设置"), ["FINESUB_DEBUG", "ui", "llm.temperature"]);
  assert.deepEqual(find("本机打不开"), ["OPENAI_POOL"]);
  assert.deepEqual(find("改了名字"), ["FINESUB_KEY_AZURE_SPEECH → AZURE_SPEECH_KEY"]);
  assert.deepEqual(find("没有转换成新格式"), ["第一集：纠错结果没有转换（segments do not align）"]);
  assert.deepEqual(find("已删除的旧文件"), ["finesub.env.pre-migration", "finesub-data\\"]);
  assert.deepEqual(find("随旧密钥文件的备份一起删除"), ["OPENAI_POOL"]);
  assert.equal(reportSections({ cleanup: { removed: [] } }).length, 0, "empty groups are not listed");
  assert.deepEqual(reportSections(undefined), []);
});
