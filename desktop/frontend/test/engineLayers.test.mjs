import assert from "node:assert/strict";
import test from "node:test";

import { engineChoices } from "../src/components/engineChoices.ts";
import { cleanupBytes, cleanupView, engineInstallView, engineLayerStatus, installTargetFor, layerUsage } from "../src/components/engineLayers.ts";

const GB = 1024 ** 3;
const MB = 1024 ** 2;

function layer(id, state, { bytes = 0, archive = 0, current = state === "missing" ? null : "v1" } = {}) {
  return { id, label: id, state, version: "v1", current, path: null, problems: [], extra: [], checked: "quick", bytes, archive_bytes: archive };
}

function provision(layers, overrides = {}) {
  return {
    schema: 1, platform: "windows-x64", supported: true, runtime_supported: true, media_supported: true, media_ready: true,
    bootstrap_error: "", root: "D:\\Nonoka\\runtime",
    runtime: { id: "python", state: "missing" }, resources: [], models: [], optional_models: [],
    layers, unused_layers: [], unused_bytes: 0,
    job: { state: "idle", target: "", resource: "", stage: "", message: "", progress: null, error: null },
    ...overrides,
  };
}

const FINESUB = { required: ["tools", "python", "models"], optional: [] };
const MAI2 = { required: ["tools"], optional: [] };

// Only the tool layer is in: what a cloud API engine needs, a third of FineSub.
const toolsOnly = provision([
  layer("python", "missing", { archive: 3 * GB }),
  layer("models", "missing", { archive: 6 * GB }),
  layer("tools", "ready", { bytes: 300 * MB, archive: 100 * MB }),
]);

test("an engine lists the layers it lacks and what they download", () => {
  assert.deepEqual(engineLayerStatus(FINESUB, toolsOnly), {
    known: true, required: ["tools", "python", "models"], missing: ["python", "models"], downloadBytes: 9 * GB, installedBytes: 300 * MB,
  });
  assert.deepEqual(engineLayerStatus(MAI2, toolsOnly).missing, []);
  // An outdated or damaged layer is one to install again.
  const outdated = provision([layer("tools", "outdated", { bytes: 1, archive: 5 })]);
  assert.deepEqual(engineLayerStatus(MAI2, outdated).missing, ["tools"]);
  assert.equal(engineLayerStatus(MAI2, outdated).downloadBytes, 5);
  // Nothing to say without the layers: an older sidecar, or no sidecar.
  assert.equal(engineLayerStatus(undefined, toolsOnly).known, false);
  assert.equal(engineLayerStatus(MAI2, null).known, false);
  assert.equal(engineLayerStatus(MAI2, provision(undefined)).known, false);
});

test("the install target covers only the missing layers", () => {
  assert.equal(installTargetFor([]), null);
  assert.equal(installTargetFor(["tools"]), "media");
  assert.equal(installTargetFor(["python"]), "runtime");
  assert.equal(installTargetFor(["tools", "python"]), "runtime");
  assert.equal(installTargetFor(["models"]), "models");
  assert.equal(installTargetFor(["models-moss"]), "models-moss");
  // The job installs only the layers not ready (provision._run), so "all" never re-downloads the tools.
  assert.equal(installTargetFor(["python", "models"]), "all");
});

test("choosing an engine that lacks layers offers to download only those, and where", () => {
  const view = engineInstallView({ kind: "local-gpu", layers: FINESUB }, toolsOnly);
  assert.equal(view.target, "all");
  assert.deepEqual(view.layers.map((item) => [item.id, item.bytes]), [["python", 3 * GB], ["models", 6 * GB]]);
  assert.equal(view.total, 9 * GB);
  assert.equal(view.root, "D:\\Nonoka\\runtime");
  assert.equal(view.blocked, "");
  assert.equal(engineInstallView({ kind: "cloud-api", layers: MAI2 }, toolsOnly), null);
  // The cloud's FineSub has nothing here to install.
  assert.equal(engineInstallView({ kind: "cloud-service", layers: { required: [], optional: [] } }, toolsOnly), null);
  // A job running is shown, a failed one is retried, a platform without the layers says so.
  assert.equal(engineInstallView({ kind: "local-gpu", layers: FINESUB }, { ...toolsOnly, job: { ...toolsOnly.job, state: "running" } }).running, true);
  assert.equal(engineInstallView({ kind: "local-gpu", layers: FINESUB }, { ...toolsOnly, job: { ...toolsOnly.job, state: "failed", target: "all" } }).failed, true);
  assert.match(engineInstallView({ kind: "local-gpu", layers: FINESUB }, { ...toolsOnly, runtime_supported: false, platform: "macos-arm64" }).blocked, /macos-arm64/);
});

test("space is listed per engine, shared layers marked and counted once", () => {
  const all = provision([
    layer("python", "ready", { bytes: 4 * GB }),
    layer("models", "ready", { bytes: 5 * GB }),
    layer("tools", "ready", { bytes: 300 * MB }),
  ]);
  const usage = layerUsage([
    { id: "finesub", label: "FineSub", layers: FINESUB },
    { id: "mai2", label: "MAI-Transcribe-2", layers: MAI2 },
    { id: "correction", label: "finesub correction", layers: { required: [], optional: [] } },
  ], all);
  assert.equal(usage.total, 9 * GB + 300 * MB);
  assert.deepEqual(usage.rows.map((row) => [row.id, row.bytes]), [["finesub", 9 * GB + 300 * MB], ["mai2", 300 * MB], ["correction", 0]]);
  assert.deepEqual(usage.rows[0].layers.find((item) => item.id === "tools").sharedWith, ["MAI-Transcribe-2"]);
  assert.deepEqual(usage.rows[1].layers[0].sharedWith, ["FineSub"]);
  assert.deepEqual(usage.rows[0].layers.find((item) => item.id === "python").sharedWith, []);
  // A layer not installed takes no space.
  assert.deepEqual(layerUsage([{ id: "finesub", label: "FineSub", layers: FINESUB }], toolsOnly).rows[0].layers.map((item) => item.id), ["tools"]);
  assert.equal(layerUsage([], provision(undefined)), null);
});

test("clean-up deletes old versions by default; a whole layer only when ticked", () => {
  const item = (layer, name, bytes, reason, blocked = "") => ({ layer, name, path: `${layer}\\${name}`, bytes, reason, blocked });
  const installed = provision([
    layer("python", "ready", { bytes: 4 * GB, archive: 3 * GB }),
    layer("models", "ready", { bytes: 5 * GB, archive: 6 * GB }),
    layer("tools", "ready", { bytes: 300 * MB }),
  ]);
  const owners = [{ id: "finesub", label: "FineSub", layers: FINESUB }, { id: "mai2", label: "MAI-Transcribe-2", layers: MAI2 }];
  const view = cleanupView([
    item("tools", "old", 10, "old-version"),
    item("python", "older", 7, "old-version", "有未结束的任务"),
    item("python", "v1", 4 * GB, "unneeded"),
    item("models", "v1", 5 * GB, "unneeded", "有未结束的任务用到这一层"),
  ], installed, owners);
  assert.deepEqual(view.removable.map((entry) => entry.name), ["old"]);
  assert.deepEqual(view.held.map((entry) => entry.name), ["older"]);
  assert.equal(view.oldBytes, 10);
  assert.deepEqual(view.layers.map((entry) => [entry.layer, entry.bytes, entry.downloadBytes, entry.neededBy, entry.blocked]), [
    ["python", 4 * GB, 3 * GB, ["FineSub"], ""],
    ["models", 5 * GB, 6 * GB, ["FineSub"], "有未结束的任务用到这一层"],
  ]);
  // Nothing ticked frees the old versions alone; a ticked layer adds its size, a held one never does.
  assert.equal(cleanupBytes(view, []), 10);
  assert.equal(cleanupBytes(view, ["python"]), 10 + 4 * GB);
  assert.equal(cleanupBytes(view, ["python", "models"]), 10 + 4 * GB);
});

test("the engine list carries each engine's layers; the cloud's FineSub needs none here", () => {
  const listed = {
    provider: "local", adapter_schema: 1, artifact_schema: 1, engine: { name: "", version: "", commit: "", bundle_id: "" },
    features: {}, devices: [], runtime: { ready: true, issues: [] },
    transcription_engines: [
      { id: "mai2", label: "MAI-Transcribe-2", kind: "cloud-api", provider: "local", ready: true, issues: [], billing: "", keys: [],
        features: { speakers: true, word_timestamps: true, separate: false, diarization_levels: ["off"], languages: [] }, layers: MAI2 },
    ],
  };
  const choices = engineChoices(listed, { authenticated: true });
  assert.deepEqual(choices[0].layers, MAI2);
  assert.deepEqual(choices[1].layers, { required: [], optional: [] });
  // An older sidecar says nothing of layers.
  assert.equal(engineChoices({ ...listed, transcription_engines: undefined }, { authenticated: true })[0].layers, undefined);
});
