import assert from "node:assert/strict";
import test from "node:test";

import {
  detectHardware,
  friendlyBootstrapError,
  isBaseEnvironmentReady,
  isMigrationWizardActive,
  migrationWizardStep,
  planOnboardingInstall,
  shouldAutoCompleteExistingUser,
} from "../src/components/onboarding.ts";

function makeProvision(layerStates = {}, overrides = {}) {
  const defaults = {
    tools: { state: "missing", archive_bytes: 160 * 1024 * 1024, bytes: 0 },
    python: { state: "missing", archive_bytes: 2600 * 1024 * 1024, bytes: 0 },
    models: { state: "missing", archive_bytes: 3000 * 1024 * 1024, bytes: 0 },
    "models-moss": { state: "missing", archive_bytes: 1380 * 1024 * 1024, bytes: 0 },
  };
  const layers = Object.entries(defaults).map(([id, base]) => {
    const state = layerStates[id] ?? base.state;
    return {
      id,
      label: id,
      state,
      version: "1",
      current: state === "ready" ? "1" : null,
      path: null,
      problems: [],
      extra: [],
      checked: "quick",
      bytes: state === "ready" ? base.archive_bytes : 0,
      archive_bytes: base.archive_bytes,
    };
  });
  return {
    schema: 1,
    platform: "windows-x64",
    supported: true,
    runtime_supported: true,
    media_supported: true,
    media_ready: layerStates.tools === "ready",
    root: "C:\\runtime",
    runtime: { id: "python", state: layerStates.python ?? "missing" },
    resources: [],
    models: [],
    layers,
    job: { state: "idle", target: "", resource: "", stage: "", message: "", progress: null, error: null },
    ...overrides,
  };
}

test("base environment readiness strictly gates skipping per mode", () => {
  const empty = makeProvision();
  const toolsOnly = makeProvision({ tools: "ready" });
  const baseDone = makeProvision({ tools: "ready", python: "ready" });

  assert.equal(isBaseEnvironmentReady("local", empty, true), false);
  assert.equal(isBaseEnvironmentReady("local", toolsOnly, true), false);
  assert.equal(isBaseEnvironmentReady("local", baseDone, true), true);
  assert.equal(isBaseEnvironmentReady("local", baseDone, false), false);

  assert.equal(isBaseEnvironmentReady("cloud", empty, true), false);
  assert.equal(isBaseEnvironmentReady("cloud", toolsOnly, true), true);
  assert.equal(isBaseEnvironmentReady("cloud-api", toolsOnly, true), true);

  const macToolsOnly = makeProvision({ tools: "ready" }, { platform: "darwin-arm64", runtime_supported: false });
  assert.equal(isBaseEnvironmentReady("local", macToolsOnly, true), true);
});

test("default local plan bundles base environment and AI models together", () => {
  const empty = makeProvision();
  const fineSubFull = planOnboardingInstall({
    mode: "local",
    localEngine: "finesub",
    includeModels: true,
    provision: empty,
  });
  assert.deepEqual(fineSubFull.missingLayers, ["tools", "python", "models"]);
  assert.equal(fineSubFull.primaryTarget, "all");
  assert.equal(fineSubFull.chainedTarget, null);
  assert.equal(fineSubFull.allReady, false);

  const mossFull = planOnboardingInstall({
    mode: "local",
    localEngine: "moss-local",
    includeModels: true,
    provision: empty,
  });
  assert.deepEqual(mossFull.missingLayers, ["tools", "python", "models-moss"]);
  assert.equal(mossFull.primaryTarget, "runtime");
  assert.equal(mossFull.chainedTarget, "models-moss");

  const baseOnly = planOnboardingInstall({
    mode: "local",
    localEngine: "finesub",
    includeModels: false,
    provision: empty,
  });
  assert.deepEqual(baseOnly.missingLayers, ["tools", "python"]);
  assert.equal(baseOnly.primaryTarget, "runtime");
  assert.equal(baseOnly.chainedTarget, null);

  const cloudPlan = planOnboardingInstall({
    mode: "cloud",
    localEngine: "finesub",
    includeModels: true,
    provision: empty,
  });
  assert.deepEqual(cloudPlan.missingLayers, ["tools"]);
  assert.equal(cloudPlan.primaryTarget, "media");
  assert.equal(cloudPlan.chainedTarget, null);
});

test("existing users auto-bypass onboarding unless a legacy migration is active", () => {
  const empty = makeProvision();
  const baseDone = makeProvision({ tools: "ready", python: "ready" });

  assert.equal(shouldAutoCompleteExistingUser({
    onboardingCompleted: false,
    mediaCount: 0,
    cloudAuthenticated: false,
    provision: empty,
    migration: null,
  }), false);

  assert.equal(shouldAutoCompleteExistingUser({
    onboardingCompleted: false,
    mediaCount: 2,
    cloudAuthenticated: false,
    provision: empty,
    migration: null,
  }), true);

  assert.equal(shouldAutoCompleteExistingUser({
    onboardingCompleted: false,
    mediaCount: 0,
    cloudAuthenticated: false,
    provision: baseDone,
    migration: null,
  }), true);

  assert.equal(shouldAutoCompleteExistingUser({
    onboardingCompleted: true,
    mediaCount: 5,
    cloudAuthenticated: true,
    provision: baseDone,
    migration: { schema: 1, state: "migrating", message: "正在升级", dataOpen: false },
  }), false);
});

test("legacy migration wizard maps states cleanly across 3 steps", () => {
  assert.equal(isMigrationWizardActive(null, false), false);
  assert.equal(isMigrationWizardActive({ schema: 1, state: "not-needed", message: "", dataOpen: true }, false), false);

  const migrating = { schema: 1, state: "migrating", message: "", dataOpen: false };
  assert.equal(isMigrationWizardActive(migrating, false), true);
  assert.equal(migrationWizardStep(migrating), 1);

  const downloading = { schema: 1, state: "downloading", message: "", dataOpen: true };
  assert.equal(isMigrationWizardActive(downloading, true), true);
  assert.equal(migrationWizardStep(downloading), 2);

  const completed = { schema: 1, state: "completed", message: "", dataOpen: true };
  assert.equal(isMigrationWizardActive(completed, false), true);
  assert.equal(isMigrationWizardActive(completed, true), false);
  assert.equal(migrationWizardStep(completed), 3);
});

test("hardware detection recommends local GPU or cloud appropriately", () => {
  const provision = makeProvision();
  const withRtx = detectHardware({
    devices: [{ id: "cuda:0", name: "NVIDIA GeForce RTX 4070", memory_mb: 12288 }, { id: "cpu", name: "CPU", memory_mb: 0 }],
  }, provision);
  assert.equal(withRtx.hasGpu, true);
  assert.equal(withRtx.mossGpuReady, true);
  assert.equal(withRtx.recommendedMode, "local");

  const cpuOnly = detectHardware({
    devices: [{ id: "cpu", name: "CPU", memory_mb: 0 }],
  }, provision);
  assert.equal(cpuOnly.hasGpu, false);
  assert.equal(cpuOnly.recommendedMode, "cloud");
});

test("bootstrap error translator turns raw Windows access denied errors into clear Chinese guidance", () => {
  const err = friendlyBootstrapError("启用 Python 启动环境失败：rename C:\\Users\\Chika\\AppData\\Roaming\\Nonoka X\\bootstrap\\launcher.staging C:\\Users\\Chika\\AppData\\Roaming\\Nonoka X\\bootstrap\\launcher: Access is denied.");
  assert.match(err.title, /占用/);
  assert.equal(err.suggestProxy, false);
  assert.match(err.detail, /Access is denied/);

  const net = friendlyBootstrapError("Get https://github.com/...: i/o timeout");
  assert.match(net.title, /网络/);
  assert.equal(net.suggestProxy, true);
});
