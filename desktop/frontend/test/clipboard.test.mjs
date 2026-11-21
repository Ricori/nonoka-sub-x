import assert from "node:assert/strict";
import test from "node:test";

import {
  clearClipboard, clipboardCount, copySegments, cutSegments, hasClipboard, pasteSegments,
} from "../src/editor/lib/clipboard.ts";
import { redo, resetHistory, undo } from "../src/editor/lib/history.ts";
import { docStore } from "../src/editor/store/docStore.ts";
import { playStore } from "../src/editor/store/playStore.ts";
import { select, selStore, setActiveTrack, setSelectionSegs } from "../src/editor/store/selectionStore.ts";
import { setDuration, viewStore } from "../src/editor/store/viewStore.ts";

const lane = (style) => ({ hidden: false, style });
const track = (id, name, segs = []) => ({
  id, name, ja: lane("origin"), zh: lane("cn"), hja: 44, hzh: 44, segs,
});

function setup(over = {}) {
  resetHistory();
  clearClipboard();
  docStore.set({
    segs: over.segs ?? [
      {
        t0: 2, t1: 4, ja: "おはよう", zh: "早上好",
        k: [
          { t0: 2, t1: 3, text: "おは" },
          { t0: 3, t1: 4, text: "よう" },
        ],
        words: [
          { word: "おは", start: 2, end: 3 },
          { word: "よう", start: 3, end: 4 },
        ],
        low_conf: true,
        style: { zh: "Title" },
      },
      { t0: 6, t1: 8, ja: "こんにちは", zh: "你好" },
    ],
    tracks: over.tracks ?? [track("t1", "说话人 A", [])],
    trackMeta: { name: "默认轨", ja: lane("origin"), zh: lane("cn") },
    effects: [],
    styles: "",
    rev: 0,
  });
  viewStore.set({ duration: 60, t0: 0, t1: 60, pieces: over.pieces ?? null, focus: over.focus ?? false });
  setDuration(over.duration ?? 60);
  selStore.set({ curTrack: -1, sel: -1, selSet: new Set(), preview: null });
  playStore.set({ t: 0, playing: false, rate: 1 });
}

const near = (a, b, msg) => assert.ok(Math.abs(a - b) < 1e-6, `${msg ?? ""} expected ${b}, got ${a}`);

test("copy and paste single segment shifts timings, karaoke, words, and deep-clones metadata", () => {
  setup();
  select(0);
  assert.equal(copySegments(), 1);
  assert.equal(hasClipboard(), true);
  assert.equal(clipboardCount(), 1);

  playStore.set({ t: 10 });
  assert.equal(pasteSegments(), 1);

  const segs = docStore.get().segs;
  assert.equal(segs.length, 3);
  const pasted = segs[2];
  near(pasted.t0, 10);
  near(pasted.t1, 12);
  assert.equal(pasted.ja, "おはよう");
  assert.equal(pasted.zh, "早上好");
  assert.equal(pasted.low_conf, true);
  assert.deepEqual(pasted.style, { zh: "Title" });
  assert.notEqual(pasted.style, segs[0].style);
  assert.deepEqual(pasted.k, [
    { t0: 10, t1: 11, text: "おは" },
    { t0: 11, t1: 12, text: "よう" },
  ]);
  assert.deepEqual(pasted.words, [
    { word: "おは", start: 10, end: 11 },
    { word: "よう", start: 11, end: 12 },
  ]);
  // 原句与剪贴板均未被篡改
  near(segs[0].k[0].t0, 2);
  assert.equal(selStore.get().selSet.has(pasted), true);
  assert.equal(selStore.get().sel, 2);
});

test("pasting onto an occupied spot auto-shifts forward to the earliest free slot", () => {
  setup();
  select(0);
  copySegments();
  // 播放头停在刚选中的句子内部，连续粘贴两次应自动顺延排在后面
  playStore.set({ t: 2.01 });
  assert.equal(pasteSegments(), 1);
  let segs = docStore.get().segs;
  assert.equal(segs.length, 3);
  near(segs[1].t0, 4);
  near(segs[1].t1, 6);

  // 4..6 和 6..8 都被占了，再次粘贴应顺延到 8..10
  assert.equal(pasteSegments(), 1);
  segs = docStore.get().segs;
  assert.equal(segs.length, 4);
  near(segs[3].t0, 8);
  near(segs[3].t1, 10);
});

test("pasting near the right edge of an empty gap slides left within the gap to fit", () => {
  setup({
    segs: [
      { t0: 0, t1: 2, ja: "a", zh: "甲" },
      { t0: 10, t1: 12, ja: "b", zh: "乙" },
    ],
  });
  select(0);
  copySegments();
  // 在空隙 [2, 10] 内偏右的 9.2 处粘贴 2s 句，应左滑落进 [8, 10] 而非跳过 10..12
  assert.equal(pasteSegments({ ti: -1, at: 9.2 }), 1);
  const segs = docStore.get().segs;
  assert.equal(segs.length, 3);
  near(segs[1].t0, 8);
  near(segs[1].t1, 10);

  // 片尾附近 59.5 粘贴 2s 句，应落在 [58, 60]
  assert.equal(pasteSegments({ ti: -1, at: 59.5 }), 1);
  const segs2 = docStore.get().segs;
  assert.equal(segs2.length, 4);
  near(segs2[3].t0, 58);
  near(segs2[3].t1, 60);
});

test("cut removes selected segments in one undoable step and keeps clipboard intact", () => {
  setup();
  select(0);
  assert.equal(cutSegments(), 1);
  assert.equal(docStore.get().segs.length, 1);
  assert.equal(docStore.get().segs[0].ja, "こんにちは");
  assert.equal(hasClipboard(), true);

  // 撤销还原被剪切的句，剪贴板依然可粘贴
  undo();
  assert.equal(docStore.get().segs.length, 2);
  assert.equal(docStore.get().segs[0].ja, "おはよう");
  assert.equal(pasteSegments({ ti: 0, at: 15 }), 1);
  assert.equal(docStore.get().tracks[0].segs.length, 1);
  near(docStore.get().tracks[0].segs[0].t0, 15);
  near(docStore.get().tracks[0].segs[0].t1, 17);

  // 撤销/重做粘贴
  undo();
  assert.equal(docStore.get().tracks[0].segs.length, 0);
  redo();
  assert.equal(docStore.get().tracks[0].segs.length, 1);
});

test("multi-segment cross-track copy and paste preserves relative timing and track offsets", () => {
  setup({
    segs: [
      { t0: 2, t1: 4, ja: "A1", zh: "甲1" },
      { t0: 6, t1: 8, ja: "A2", zh: "甲2" },
    ],
    tracks: [
      track("t1", "轨1", [{ t0: 3, t1: 5, ja: "B1", zh: "乙1" }]),
      track("t2", "轨2", []),
    ],
  });
  const sA1 = docStore.get().segs[0];
  const sA2 = docStore.get().segs[1];
  const sB1 = docStore.get().tracks[0].segs[0];
  setSelectionSegs([sA1, sA2, sB1]);

  assert.equal(copySegments(), 3);
  // 粘贴到轨 1（pos=1）：原默认轨(offset 0) → 轨 1，原轨 1(offset 1) → 轨 2
  assert.equal(pasteSegments({ ti: 0, at: 20 }), 3);

  const tr1 = docStore.get().tracks[0].segs;
  const tr2 = docStore.get().tracks[1].segs;
  assert.equal(tr1.length, 3);
  near(tr1[1].t0, 20);
  near(tr1[1].t1, 22);
  assert.equal(tr1[1].ja, "A1");
  near(tr1[2].t0, 24);
  near(tr1[2].t1, 26);
  assert.equal(tr1[2].ja, "A2");

  assert.equal(tr2.length, 1);
  near(tr2[0].t0, 21);
  near(tr2[0].t1, 23);
  assert.equal(tr2[0].ja, "B1");

  // 选区落在刚粘贴出来的 3 句上
  assert.equal(selStore.get().selSet.size, 3);
  assert.equal(selStore.get().curTrack, 0);
});

test("cross-track paste fails cleanly when tracks are fewer than copied span", () => {
  setup({
    segs: [{ t0: 2, t1: 4, ja: "A1", zh: "甲1" }],
    tracks: [
      track("t1", "轨1", []),
      track("t2", "轨2", [{ t0: 2, t1: 4, ja: "C1", zh: "丙1" }]),
    ],
  });
  setSelectionSegs([docStore.get().segs[0], docStore.get().tracks[1].segs[0]]);
  assert.equal(copySegments(), 2);

  // 删掉两条自定义轨后只剩默认轨，跨距为 2 的剪贴板不能强行压到同一轨
  docStore.set({ tracks: [] });
  assert.equal(pasteSegments({ ti: -1, at: 10 }), 0);
  assert.equal(docStore.get().segs.length, 1);
});

test("pasting in focused mode skips deleted video gaps so pasted subtitles remain visible", () => {
  setup({
    segs: [{ t0: 8, t1: 10, ja: "尾句", zh: "尾句" }],
    pieces: [{ t0: 0, t1: 10 }, { t0: 20, t1: 30 }],
    focus: true,
    duration: 30,
  });
  select(0);
  copySegments();
  playStore.set({ t: 8.01 });

  // 8..10 被占用，10..20 是成片里删掉的空洞，应直接落到下一段可见片段 20..22
  assert.equal(pasteSegments(), 1);
  const segs = docStore.get().segs;
  assert.equal(segs.length, 2);
  near(segs[1].t0, 20);
  near(segs[1].t1, 22);
});

test("paste refuses cleanly without pushing history when there is no room left", () => {
  setup({
    segs: [{ t0: 0, t1: 59, ja: "很长", zh: "很长" }],
    duration: 60,
  });
  select(0);
  copySegments();
  assert.equal(pasteSegments({ ti: -1, at: 50 }), 0);
  assert.equal(docStore.get().segs.length, 1);
});

test("copying from default track and pasting via active track switch lands on custom track", () => {
  setup();
  select(0);
  copySegments();
  setActiveTrack(0, { silent: true });
  playStore.set({ t: 25 });
  assert.equal(pasteSegments(), 1);
  assert.equal(docStore.get().tracks[0].segs.length, 1);
  near(docStore.get().tracks[0].segs[0].t0, 25);
  near(docStore.get().tracks[0].segs[0].t1, 27);
});
