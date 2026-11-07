import assert from "node:assert/strict";
import test from "node:test";

import { HARDWARE_FACTOR, estimateExportBytes } from "../src/export/estimate.ts";

const source = {
  width: 1920, height: 1080, fps: 24, videoKbps: 8000, audioKbps: 2304, hasAudio: true,
  audioSampleRate: 48000, audioChannels: 2, audioBits: 24,
};
const base = { source, pieces: [{ t0: 0, t1: 60 }], crf: 21, preset: "medium", scaleH: 0, joined: false };
// Isolate the audio share: same video settings, audio-only difference.
const audioBytes = (input) => estimateExportBytes({ ...base, ...input })
  - estimateExportBytes({ ...base, source: { ...source, hasAudio: false }, abr: "192k" });
const kbps = (bytes) => bytes * 8 / 1000 / 60 / 1.01;

test("copy sizes from the source audio bitrate", () => {
  assert.equal(Math.round(kbps(audioBytes({ abr: "copy", container: "mkv" }))), 2304);
});

test("FLAC in MKV sizes from the PCM layout", () => {
  // 48 kHz × 2 ch × 24 bit = 2304 kbps PCM, about 60% of it as FLAC.
  assert.equal(Math.round(kbps(audioBytes({ abr: "flac", container: "mkv" }))), Math.round(2304 * 0.6));
});

test("FLAC is not offered for MP4 and falls back to AAC 192k", () => {
  assert.equal(Math.round(kbps(audioBytes({ abr: "flac", container: "mp4" }))), 192);
});

test("joined copy falls back to FLAC in MKV and 192k in MP4", () => {
  const pieces = [{ t0: 0, t1: 30 }, { t0: 40, t1: 70 }];
  const joined = (container) => audioBytes({ abr: "copy", container, joined: true, pieces });
  assert.equal(Math.round(kbps(joined("mkv"))), Math.round(2304 * 0.6));
  assert.equal(Math.round(kbps(joined("mp4"))), 192);
});

test("GPU encoding scales only the video share", () => {
  const silent = { ...base, source: { ...source, hasAudio: false }, abr: "192k" };
  const cpu = estimateExportBytes(silent);
  const gpu = estimateExportBytes({ ...silent, gpu: true });
  assert.equal(Math.round(gpu / cpu * 100), Math.round(HARDWARE_FACTOR * 100));
});
