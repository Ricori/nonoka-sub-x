import assert from "node:assert/strict";
import test from "node:test";

import { cloudTaskRequest, localTaskRequest } from "../src/home/defaultRequest.ts";
import { PipelineController } from "../src/home/pipelineController.ts";
import { CloudExecutionProvider } from "../src/providers/cloudProvider.ts";
import { LocalExecutionProvider } from "../src/providers/localProvider.ts";
import { ProviderRegistry } from "../src/providers/registry.ts";

const taskId = "0123456789abcdef0123456789abcdef";

const snapshot = (state = "running") => ({
  schema: 1,
  task_id: taskId,
  provider: "local",
  state,
  stage: "stable",
  progress: null,
  engine: { version: "0.4.1", commit: "fixture" },
  requested_capabilities: {},
  effective_capabilities: {},
  error: null,
  last_cursor: 0,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
});

const capabilities = (overrides = {}) => ({
  provider: "local",
  adapter_schema: 1,
  artifact_schema: 1,
  engine: { name: "finesub", version: "0.4.1", commit: "fixture" },
  features: {
    raw_srt: true,
    translation: true,
    video_multimodal: true,
    knowledge: true,
    resume: true,
    diarization: false,
  },
  devices: [],
  runtime: { ready: true, issues: [] },
  ...overrides,
});

const request = () => ({
  schema: 1,
  provider: "local",
  source: { kind: "local_file", path: "/video.mp4", title: "video" },
  target: "raw-srt",
  language: "ja",
  device: "cuda:0",
  gpu_budget_gb: 8,
  vocal_profile: "quality",
  correction: {
    enabled: false,
    media: "audio",
    retrieval: "local",
    difficulty: "quality",
    fast: "auto",
    extra_info: "",
    extra_style: "",
  },
  knowledge: "none",
  cleanup_intermediate: false,
});

test("LocalExecutionProvider accepts only compatible local responses", async () => {
  const calls = [];
  const bridge = {
    async Capabilities() { return capabilities(); },
    async StartTask(body) { calls.push(body); return snapshot(); },
    async TaskStatus() { return snapshot(); },
    async TaskEvents() { return { schema: 1, task_id: taskId, events: [], next_cursor: 0, state: "running" }; },
    async CancelTask() { return snapshot("cancelled"); },
    async RetryTask() { return snapshot(); },
    async ResumeTask() { return snapshot(); },
    async TaskArtifacts() { return { schema: 1, task_id: taskId, engine_commit: "fixture", artifacts: {} }; },
  };
  const provider = new LocalExecutionProvider(bridge);
  assert.equal((await provider.capabilities()).provider, "local");
  assert.equal((await provider.start(request())).task_id, taskId);
  assert.equal(calls.length, 1);

  const cloud = request();
  cloud.provider = "cloud";
  cloud.source = { kind: "uploaded_audio", object_id: "object", title: "video" };
  await assert.rejects(() => provider.start(cloud), /local_file/);
});

test("PipelineController resumes event cursor and fetches artifacts on completion", async () => {
  const after = [];
  let status = "running";
  const provider = {
    async capabilities() { return capabilities(); },
    async start() { return snapshot(); },
    async status() { return snapshot(status); },
    async events(_id, cursor) {
      after.push(cursor);
      return {
        schema: 1,
        task_id: taskId,
        events: cursor === 0 ? [{ cursor: 1, task_id: taskId, type: "stage", timestamp: "now", payload: {} }] : [],
        next_cursor: 1,
        state: status,
      };
    },
    async cancel() { return snapshot("cancelled"); },
    async retry() { return snapshot(); },
    async resume() { return snapshot(); },
    async artifacts() { return { schema: 1, task_id: taskId, engine_commit: "fixture", artifacts: {} }; },
  };
  const controller = new PipelineController(provider);
  await controller.start(request());
  await controller.refresh();
  status = "completed";
  await controller.refresh();
  assert.deepEqual(after, [0, 1]);
  assert.equal(controller.current().events.length, 1);
  assert.equal(controller.current().snapshot.state, "completed");
  assert.equal(controller.current().artifacts.task_id, taskId);
});

test("PipelineController coalesces overlapping refreshes and deduplicates cursors", async () => {
  let statusCalls = 0;
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const event = { cursor: 1, task_id: taskId, type: "stage", timestamp: "now", payload: {} };
  const provider = {
    async capabilities() { return capabilities(); },
    async start() { return snapshot(); },
    async status() { statusCalls += 1; await gate; return snapshot(); },
    async events() { await gate; return { schema: 1, task_id: taskId, events: [event, event], next_cursor: 1, state: "running" }; },
    async cancel() { return snapshot("cancelled"); },
    async retry() { return snapshot(); },
    async resume() { return snapshot(); },
    async artifacts() { return { schema: 1, task_id: taskId, engine_commit: "fixture", artifacts: {} }; },
  };
  const controller = new PipelineController(provider);
  await controller.start(request());
  const first = controller.refresh();
  const second = controller.refresh();
  release();
  await Promise.all([first, second]);
  assert.equal(statusCalls, 1);
  assert.deepEqual(controller.current().events.map((item) => item.cursor), [1]);
});

test("PipelineController retries a cancelled task instead of resuming it", async () => {
  const calls = [];
  let state = "running";
  const provider = {
    async capabilities() { return capabilities(); },
    async start() { return snapshot(); },
    async status() { return snapshot(state); },
    async events() { return { schema: 1, task_id: taskId, events: [], next_cursor: 0, state }; },
    async cancel() { calls.push("cancel"); state = "cancelled"; return snapshot(state); },
    async retry() { calls.push("retry"); state = "queued"; return snapshot(state); },
    async resume() { calls.push("resume"); return snapshot("running"); },
    async artifacts() { return { schema: 1, task_id: taskId, engine_commit: "fixture", artifacts: {} }; },
  };
  const controller = new PipelineController(provider);
  await controller.start(request());
  assert.equal((await controller.cancel()).state, "cancelled");
  assert.equal((await controller.retry()).state, "queued");
  assert.deepEqual(calls, ["cancel", "retry"]);
  assert.equal(controller.current().error, null);
});

test("PipelineController keeps a terminal snapshot when the event page fails", async () => {
  const provider = {
    async capabilities() { return capabilities(); },
    async start() { return snapshot(); },
    async status() { return snapshot("failed"); },
    async events() { throw new Error("event page unavailable"); },
    async cancel() { return snapshot("cancelled"); },
    async retry() { return snapshot(); },
    async resume() { return snapshot(); },
    async artifacts() { throw new Error("must not be read"); },
  };
  const controller = new PipelineController(provider);
  await controller.start(request());
  await controller.refresh();
  assert.equal(controller.current().snapshot.state, "failed");
  assert.match(controller.current().error.message, /event page unavailable/);
});

test("PipelineController keeps a completed snapshot when artifacts are not ready", async () => {
  const provider = {
    async capabilities() { return capabilities(); },
    async start() { return snapshot(); },
    async status() { return snapshot("completed"); },
    async events() { return { schema: 1, task_id: taskId, events: [], next_cursor: 0, state: "completed" }; },
    async cancel() { return snapshot("cancelled"); },
    async retry() { return snapshot(); },
    async resume() { return snapshot(); },
    async artifacts() { throw new Error("artifacts_not_ready"); },
  };
  const controller = new PipelineController(provider);
  await controller.start(request());
  await controller.refresh();
  assert.equal(controller.current().snapshot.state, "completed");
  assert.equal(controller.current().artifacts, null);
  assert.match(controller.current().error.message, /artifacts_not_ready/);
});

test("PipelineController enforces runtime state before start", async () => {
  const unavailable = {
    async capabilities() { return capabilities({ runtime: { ready: false, issues: [{ code: "missing_gpu", message: "GPU missing" }] } }); },
    async start() { throw new Error("must not start"); },
  };
  const controller = new PipelineController(unavailable);
  await assert.rejects(() => controller.start(request()), /GPU missing/);
});

test("ProviderRegistry does not silently replace a provider", () => {
  const registry = new ProviderRegistry();
  const provider = {};
  registry.register("local", provider);
  assert.equal(registry.get("local"), provider);
  assert.throws(() => registry.register("local", provider), /already registered/);
  assert.throws(() => registry.get("cloud"), /not available/);
});

test("CloudExecutionProvider delegates the local media id to the native upload bridge", async () => {
  const calls = [];
  const bridge = {
    async capabilities() { return capabilities({ provider: "cloud", features: { ...capabilities().features, video_multimodal: false } }); },
    async startTask(localID, options) { calls.push([localID, options]); return { ...snapshot(), provider: "cloud" }; },
    async taskStatus() { return { ...snapshot(), provider: "cloud" }; },
    async taskEvents() { return { schema: 1, task_id: taskId, events: [], next_cursor: 0, state: "running" }; },
    async cancelTask() { return { ...snapshot("cancelled"), provider: "cloud" }; },
    async retryTask() { return { ...snapshot(), provider: "cloud" }; },
    async resumeTask() { return { ...snapshot(), provider: "cloud" }; },
    async taskArtifacts() { return { schema: 1, task_id: taskId, engine_commit: "fixture", artifacts: {} }; },
  };
  const provider = new CloudExecutionProvider(bridge);
  const value = cloudTaskRequest({
    id: "loc_0123456789ab", sourcePath: "/video.mp4", title: "clip", size: 100,
    duration: 12, width: 1920, height: 1080, fingerprint: "fp", addedAt: 0,
    lastAccess: 0, thumbnailAvailable: false, available: true, documentAvailable: false,
  });
  assert.equal((await provider.capabilities()).features.video_multimodal, false);
  assert.equal(value.knowledge, "none");
  assert.equal((await provider.start(value)).provider, "cloud");
  assert.equal(calls[0][0], "loc_0123456789ab");
  assert.equal(calls[0][1].source.kind, "uploaded_audio");
});

test("default local request contains a trusted path and no upload transport", () => {
  const value = localTaskRequest({
    id: "loc_0123456789ab",
    sourcePath: "C:\\media\\clip.mp4",
    title: "clip",
    size: 100,
    duration: 12,
    width: 1920,
    height: 1080,
    fingerprint: "fp",
    addedAt: 0,
    lastAccess: 0,
    thumbnailAvailable: false,
    available: true,
    documentAvailable: false,
  });
  assert.equal(value.provider, "local");
  assert.deepEqual(value.source, {
    kind: "local_file",
    path: "C:\\media\\clip.mp4",
    title: "clip",
    fingerprint: "fp",
    video_id: "loc_0123456789ab",
    duration: 12,
  });
  assert.equal(value.correction.media, "text");
  assert.equal(JSON.stringify(value).includes("upload"), false);
  assert.equal(JSON.stringify(value).includes("backend"), false);
});
