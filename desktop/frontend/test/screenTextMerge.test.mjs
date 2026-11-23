import assert from "node:assert/strict";
import test from "node:test";

import { setStyleSheet } from "../src/editor/ass.ts";
import { buildAss } from "../src/editor/lib/assBuild.ts";
import { buildRows } from "../src/editor/lib/rows.ts";
import { resetHistory, undo } from "../src/editor/lib/history.ts";
import {
  hasEditedOcrTracks,
  isOcrTrack,
  mapDocumentTracks,
  nonOcrHash,
  segmentsHash,
} from "../src/editor/lib/screenTextDoc.ts";
import {
  applyTrialResult, handleDocumentChanged, mergeServerScreenText, mergeTrialTracks, undoTrial,
} from "../src/editor/lib/screenTextMerge.ts";
import { configureEditorSession, setLoadedDocument } from "../src/editor/session.ts";
import { docStore } from "../src/editor/store/docStore.ts";
import { doSave, markDirty, saveStore, setSaveBaseline } from "../src/editor/store/saveStore.ts";
import { resetScreenTextStore, screenTextStore } from "../src/editor/store/screenTextStore.ts";
import { selStore } from "../src/editor/store/selectionStore.ts";
import { SCREEN_TEXT_STYLE, parseSheet } from "../src/subtitles/styles.ts";
import { ASS_STYLE_FORMAT } from "../src/subtitles/constants.ts";
import { buildSrtFrom } from "../src/subtitles/build.ts";

const lane = (style, hidden = false) => ({ hidden, style });

function makeServerDoc(over = {}) {
  return {
    schema: 1,
    video_id: "vid-1",
    title: "Demo",
    source: "local",
    fp: null,
    rev: 1,
    subtitles: [{ t0: 1, t1: 3, ja: "こんにちは", zh: "你好" }],
    tracks: [
      {
        id: "user-1",
        name: "注释",
        ja: lane("origin", true),
        zh: lane("cn", false),
        hja: 44,
        hzh: 44,
        segs: [{ t0: 2, t1: 4, ja: "", zh: "用户注释" }],
      },
    ],
    track_meta: { name: "默认轨", ja: lane("origin"), zh: lane("cn") },
    effects: [],
    styles: "",
    projection: { schema: 1, mode: "final" },
    ...over,
  };
}

function makeOcrTrack(id = "ocr-r1", zh = "标题译文", hash = "h-initial") {
  return {
    id,
    name: "标题区",
    ja: lane(SCREEN_TEXT_STYLE, true),
    zh: lane(SCREEN_TEXT_STYLE, false),
    hja: 44,
    hzh: 44,
    source: { kind: "screen-text", task: "task-1", region: "r1", label: "title", hash },
    segs: [
      {
        t0: 5,
        t1: 8,
        ja: "タイトル",
        zh,
        pos: { x: 0.5, y: 0.1, an: 8, w: 0.4, fs: 0.05 },
        cover: { box: [0.3, 0.08, 0.7, 0.14], fill: "#101010" },
      },
    ],
  };
}

function bootEditor(doc = makeServerDoc()) {
  resetHistory();
  resetScreenTextStore();
  configureEditorSession({
    media: { id: doc.video_id, title: doc.title, sourcePath: "D:/v.mp4", available: true },
    theme: "dark",
    toggleTheme() {},
    onClose() {},
    onSaved() {},
  });
  setLoadedDocument(doc);
  setSaveBaseline(doc);
  setStyleSheet(doc.styles || "");
  docStore.set({
    rev: doc.rev,
    title: doc.title,
    segs: structuredClone(doc.subtitles),
    tracks: mapDocumentTracks(doc.tracks),
    trackMeta: structuredClone(doc.track_meta),
    effects: [],
    styles: doc.styles || "",
  });
  saveStore.set({ dirty: false, saving: false, conflicted: false, stateText: "已加载", stateCls: "" });
  selStore.set({ curTrack: -1, sel: 0, selSet: new Set(), preview: null });
}

test("clean merge adds ocr* tracks, ensures screen-text style, updates rev, and can be undone", () => {
  const initial = makeServerDoc({ rev: 1 });
  bootEditor(initial);

  const updated = makeServerDoc({
    rev: 2,
    tracks: [...initial.tracks, makeOcrTrack()],
  });
  const res = mergeServerScreenText(updated);
  assert.deepEqual(res, { ocrCount: 1, rebased: false });
  assert.equal(docStore.get().rev, 2);
  assert.equal(saveStore.get().dirty, false, "clean merge stays clean");
  assert.equal(docStore.get().tracks.length, 2);
  assert.equal(docStore.get().tracks[1].id, "ocr-r1");
  assert.ok(parseSheet(docStore.get().styles).fields[SCREEN_TEXT_STYLE], "adds 文字识别 style");

  // Undo reverts the incoming ocr* tracks and restores previous state
  undo();
  assert.equal(docStore.get().tracks.length, 1);
  assert.equal(docStore.get().tracks[0].id, "user-1");
});

test("dirty merge rebases: keeps unsaved non-OCR edits, splices server ocr* tracks, stays dirty", () => {
  const initial = makeServerDoc({ rev: 1 });
  bootEditor(initial);

  // User edits default track locally without saving
  docStore.get().segs[0].zh = "本地修改过的译文";
  markDirty();

  const updated = makeServerDoc({
    rev: 2,
    tracks: [...initial.tracks, makeOcrTrack()],
  });
  const res = mergeServerScreenText(updated);
  assert.deepEqual(res, { ocrCount: 1, rebased: true });
  assert.equal(docStore.get().rev, 2);
  assert.equal(saveStore.get().dirty, true, "keeps dirty for autosave");
  assert.equal(docStore.get().segs[0].zh, "本地修改过的译文", "local speech edit preserved");
  assert.equal(docStore.get().tracks.length, 2);
  assert.equal(docStore.get().tracks[1].id, "ocr-r1");
});

test("409 save conflict with identical non-OCR hash rebases onto server ocr* tracks and retries once", async () => {
  const initial = makeServerDoc({ rev: 1 });
  bootEditor(initial);

  // User edits local speech subtitle
  docStore.get().segs[0].zh = "改了一句语音字幕";
  markDirty();

  // Meanwhile server got ocr* tracks (rev 2), non-OCR part identical to initial
  const serverWithOcr = makeServerDoc({
    rev: 2,
    tracks: [...initial.tracks, makeOcrTrack()],
  });
  assert.equal(nonOcrHash(serverWithOcr), nonOcrHash(initial));

  let saveCalls = 0;
  let lastSavedPayload = null;
  const io = {
    async read() {
      return serverWithOcr;
    },
    async save(_vid, payload) {
      saveCalls += 1;
      lastSavedPayload = payload;
      if (saveCalls === 1) throw new Error("409 revision conflict");
      return { ...payload, rev: 3 };
    },
  };

  await doSave(io);
  assert.equal(saveCalls, 2, "retried save once after rebase");
  assert.equal(saveStore.get().conflicted, false, "not locked");
  assert.equal(saveStore.get().dirty, false);
  assert.equal(docStore.get().rev, 3);
  assert.equal(lastSavedPayload.rev, 2, "retry carried server rev");
  assert.equal(lastSavedPayload.subtitles[0].zh, "改了一句语音字幕", "kept local edit");
  assert.equal(lastSavedPayload.tracks.filter(isOcrTrack).length, 1, "included server ocr* track");
});

test("409 save conflict with changed non-OCR content locks the editor as conflicted", async () => {
  const initial = makeServerDoc({ rev: 1 });
  bootEditor(initial);

  docStore.get().segs[0].zh = "本地改动";
  markDirty();

  // Another window edited default speech track on server
  const serverOtherEdit = makeServerDoc({
    rev: 2,
    subtitles: [{ t0: 1, t1: 3, ja: "こんにちは", zh: "别的窗口改的" }],
  });
  assert.notEqual(nonOcrHash(serverOtherEdit), nonOcrHash(initial));

  let saveCalls = 0;
  const io = {
    async read() {
      return serverOtherEdit;
    },
    async save() {
      saveCalls += 1;
      throw new Error("409 revision conflict");
    },
  };

  await doSave(io);
  assert.equal(saveCalls, 1, "did not retry when non-OCR content diverged");
  assert.equal(saveStore.get().conflicted, true);
  assert.equal(saveStore.get().stateCls, "bad");
});

test("document:changed arriving mid-save waits until save finishes before merging", async () => {
  const initial = makeServerDoc({ rev: 1 });
  bootEditor(initial);
  docStore.get().segs[0].zh = "正在保存的改动";
  markDirty();

  let releaseSave;
  const saveStarted = new Promise((r) => { releaseSave = r; });
  let finishSave;
  const saveGate = new Promise((r) => { finishSave = r; });

  const io = {
    async read() { return initial; },
    async save(_vid, payload) {
      releaseSave();
      await saveGate;
      return { ...payload, rev: 2 };
    },
  };

  const saving = doSave(io);
  await saveStarted;
  assert.equal(saveStore.get().saving, true);

  let readCalled = false;
  const serverAfter = makeServerDoc({
    rev: 3,
    subtitles: [{ t0: 1, t1: 3, ja: "こんにちは", zh: "正在保存的改动" }],
    tracks: [...initial.tracks, makeOcrTrack()],
  });
  const mergePromise = handleDocumentChanged(
    { videoId: "vid-1", rev: 3, reason: "screen-text" },
    async () => {
      readCalled = true;
      return serverAfter;
    },
  );
  assert.equal(readCalled, false, "deferred while saving");

  finishSave();
  await saving;
  const merged = await mergePromise;
  assert.equal(merged, true);
  assert.equal(readCalled, true);
  assert.equal(docStore.get().rev, 3);
  assert.equal(docStore.get().tracks.filter(isOcrTrack).length, 1);
});

test("a trial goes straight into the document and 撤销 puts the screen-text tracks back", () => {
  const seg = (t0, t1, zh) => ({ t0, t1, ja: "", zh, pos: { x: 0.5, y: 0.5, an: 5 } });
  const existing = { ...makeOcrTrack("ocr", "整片"), segs: [seg(1, 3, "前"), seg(10, 12, "段内"), seg(80, 82, "后")] };
  bootEditor(makeServerDoc({ tracks: [...makeServerDoc().tracks, existing] }));
  const before = structuredClone(docStore.get().tracks.filter(isOcrTrack));

  // the trial covers 5–65: two texts at once at 20 s, so its result has two tracks
  const trial = mapDocumentTracks([
    { ...makeOcrTrack("ocr", "x"), segs: [seg(20, 22, "名字")] },
    { ...makeOcrTrack("ocr2", "x"), segs: [seg(20, 25, "台词")] },
  ]);
  assert.equal(applyTrialResult(trial, "", 5, 65), 2);
  const ocr = docStore.get().tracks.filter(isOcrTrack);
  assert.deepEqual(ocr.map(track => track.id), ["ocr", "ocr2"]);
  assert.deepEqual(ocr.map(track => track.segs.map(s => s.zh)), [["前", "名字", "后"], ["台词"]],
    "what was inside the range is gone, the rest stays");
  assert.ok(buildAss().includes("名字"), "it is part of the document, not an overlay");
  assert.equal(saveStore.get().dirty, true);
  assert.equal(screenTextStore.get().trialUndo.cueCount, 2);

  undoTrial();
  assert.deepEqual(structuredClone(docStore.get().tracks.filter(isOcrTrack)), before);
  assert.equal(screenTextStore.get().trialUndo, null);
  assert.ok(!buildAss().includes("名字"));
});

test("a screen-text track has only its translation lane", () => {
  const shown = { ...makeOcrTrack("ocr", "译文"), ja: lane(SCREEN_TEXT_STYLE, false) };
  bootEditor(makeServerDoc({ tracks: [...makeServerDoc().tracks, shown] }));
  const keys = buildRows().map(row => row.key);
  assert.ok(keys.includes("ocr-zh") && !keys.includes("ocr-ja"), keys.join(","));
  assert.equal(docStore.get().tracks.find(isOcrTrack).ja.hidden, true, "an older run's source lane no longer draws");
  assert.ok(!buildAss().includes("タイトル"));
});

test("trial lines take a free track, else a new one; emptied tracks go", () => {
  const seg = (t0, t1, zh) => ({ t0, t1, ja: "", zh });
  const local = mapDocumentTracks([
    { ...makeOcrTrack("ocr", "x"), segs: [seg(0, 2, "a"), seg(6, 8, "b")] },
    { ...makeOcrTrack("ocr2", "x"), segs: [seg(6, 7, "inside only")] },
  ]);
  const trial = mapDocumentTracks([{ ...makeOcrTrack("ocr", "x"), segs: [seg(5, 9, "t1"), seg(1, 3, "clash")] }]);
  const { tracks, cueCount } = mergeTrialTracks(local, trial, 5, 9);
  assert.equal(cueCount, 2);
  assert.deepEqual(tracks.map(track => [track.id, track.segs.map(s => s.zh)]),
    [["ocr", ["a", "t1"]], ["ocr2", ["clash"]]],
    "ocr2 lost its only line to the range, then took the line that clashes on ocr");
});

test("a track a trial adds gets a style of its own, copied from the user's 文字识别", () => {
  const seg = (t0, t1, zh) => ({ t0, t1, ja: "", zh, pos: { x: 0.5, y: 0.5, an: 5 } });
  const look = "Style: 文字识别,微软雅黑,88,&H0000FFFF,&H000000FF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,2,0,9,12,12,12,1";
  const styles = `[V4+ Styles]\n${ASS_STYLE_FORMAT}\n${look}\n`;
  bootEditor(makeServerDoc({ styles, tracks: [...makeServerDoc().tracks,
    { ...makeOcrTrack("ocr", "x"), segs: [seg(20, 30, "已有")] }] }));
  applyTrialResult(mapDocumentTracks([{ ...makeOcrTrack("ocr", "x"), segs: [seg(22, 24, "同时")] }]), "", 0, 10);
  const added = docStore.get().tracks.find(track => track.id === "ocr2");
  assert.equal(added.name, "文字识别 2");
  assert.equal(added.zh.style, "文字识别 2");
  const fields = parseSheet(docStore.get().styles).fields["文字识别 2"];
  assert.equal(fields.fontname, "微软雅黑", "starts with the look the user gave the screen text");
  assert.equal(fields.fontsize, "88");
});

test("segmentsHash detects whether an ocr* track was edited", async () => {
  const track = mapDocumentTracks([makeOcrTrack()])[0];
  const digest = await segmentsHash(track.segs);
  track.source.hash = digest;
  assert.equal(await hasEditedOcrTracks([track]), false);

  track.segs[0].zh = "人工润色过";
  assert.equal(await hasEditedOcrTracks([track]), true);
});

test("buildSrtFrom skips screen-text tracks by default and includes them when opted in", () => {
  const src = {
    segs: [{ t0: 1, t1: 3, ja: "音声", zh: "语音字幕" }],
    tracks: [
      {
        id: "user-1",
        name: "注释",
        ja: lane("origin", true),
        zh: lane("cn", false),
        segs: [{ t0: 2, t1: 4, ja: "", zh: "用户注释" }],
      },
      makeOcrTrack("ocr-r1", "文字识别译文"),
    ],
    trackMeta: { name: "默认轨", ja: lane("origin"), zh: lane("cn") },
    effects: [],
  };
  const defaultSrt = buildSrtFrom(src, "zh");
  assert.ok(defaultSrt.includes("语音字幕"));
  assert.ok(defaultSrt.includes("用户注释"));
  assert.ok(!defaultSrt.includes("文字识别译文"), "screen-text excluded from SRT by default");

  const withOcr = buildSrtFrom(src, "zh", undefined, { includeScreenText: true });
  assert.ok(withOcr.includes("文字识别译文"), "screen-text included when opted in");
});
