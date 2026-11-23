import assert from "node:assert/strict";
import test from "node:test";

import {
  buildAssFrom, buildPlatesAss, cuesOf, fontSizeForFs, outputLinesOf, placedPosOf, plateBox, plateDialogue,
  positionedAssEvents, positionedAssText, positionedCueOf,
} from "../src/subtitles/build.ts";
import { inkMetricsOf } from "../src/subtitles/metrics.ts";
import { layoutPositioned, POS_MIN_FONT_SCALE } from "../src/subtitles/layout.ts";
import { documentAss } from "../src/subtitles/document.ts";
import {
  normalizeSegCover, normalizeSegPos, posFromCover, trimLaneOverlaps, unionCover,
} from "../src/subtitles/position.ts";
import { composeSheet } from "../src/subtitles/styles.ts";
import { getStyleSheet, setStyleSheet } from "../src/editor/ass.ts";
import { buildAss, buildPiecesAss, buildPlates } from "../src/editor/lib/assBuild.ts";
import { mapSegs } from "../src/editor/lib/boot.ts";
import { mergeNext, splitAtPlayhead } from "../src/editor/lib/edits.ts";
import { cloneSegs, resetHistory, undo } from "../src/editor/lib/history.ts";
import { plateOn, plateSegsOfStyle, resetSegPos, setPlates, setSegFill, setSegPos } from "../src/editor/lib/segPos.ts";
import { beginDrag, nudgeSelection } from "../src/editor/lib/stageDrag.ts";
import { boxForLane, hitTest, laneOfBox, subtitleBoxesAt } from "../src/editor/lib/stageHit.ts";
import { docStore } from "../src/editor/store/docStore.ts";
import { playStore } from "../src/editor/store/playStore.ts";
import { packSeg } from "../src/editor/store/saveStore.ts";
import { selStore } from "../src/editor/store/selectionStore.ts";
import { selectLane, stageStore } from "../src/editor/store/stageStore.ts";

// Phase 2 of on-screen text: per-segment \pos placement and FlowOCR-compatible plates, in the
// one ASS builder that preview, export, burn and plugins share. Metrics fall back to fixed
// glyph ratios under node (no canvas), so layout numbers are deterministic.

const SHEET_TEXT = "";   // builtin origin/cn only, PlayRes 1920×1080
const sheet = composeSheet(SHEET_TEXT);
const X = 1920, Y = 1080;
const lane = (style, hidden = false) => ({ hidden, style });

/** One on-screen text: box [0.1,0.2]–[0.3,0.25], translation centred on it */
const ocrSeg = (over = {}) => ({
  t0: 1, t1: 3, ja: "", zh: "你好",
  pos: { x: 0.2, y: 0.225, an: 5, w: 0.2, fs: 0.05 },
  cover: { box: [0.1, 0.2, 0.3, 0.25], fill: "#112233" },
  ...over,
});

const source = (segs, over = {}) => ({
  segs: [],
  tracks: [{ id: "ocr1", name: "文字识别", ja: lane("origin", true), zh: lane("cn"), segs }],
  trackMeta: { name: "默认轨", ja: lane("origin"), zh: lane("cn") },
  effects: [],
  ...over,
});

// The box [192,216]–[576,270] grown by PLATE_PAD × glyph height (0.2 × 54 = 10.8 px) on every side
const PLATE = "Dialogue: 0,0:00:01.00,0:00:03.00,cn,文字识别,0,0,0,fo-fx|plate,"
  + "{\\an7\\pos(181.20,205.20)\\p1\\bord0\\shad0\\1c&H332211&\\1a&H00&}"
  + "m 0 0 l 405.60 0 405.60 75.60 0 75.60{\\p0}";

// No canvas under node: every font gets the bundled 方正准圆's fixed ink metrics
const INK = inkMetricsOf("方正准圆");
/** Fontsize whose ink is fs = 0.05 (54 px) tall */
const SIZE = 0.05 * Y / INK.ink;
const n2 = (value) => String(Math.round(value * 100) / 100);
/** \pos y of an an4-6 cue anchored at y: the ink's middle, not the line box's, sits there */
const midY = (y) => y + (INK.bottom - INK.top) / 2 * SIZE;

const events = (ass) => ass.split("\n").filter(line => line.startsWith("Dialogue:"));

// ── builder ────────────────────────────────────────────────────

test("a positioned cue is written with \\an, \\pos and \\fs in PlayRes units", () => {
  const ass = buildAssFrom(source([ocrSeg()]), sheet);
  const text = events(ass).find(line => !line.includes("fo-fx|plate"));
  assert.equal(text, "Dialogue: 0,0:00:01.00,0:00:03.00,cn,文字识别,0,0,0,,"
    + `{\\an5\\pos(384,${n2(midY(243))})\\fs${n2(SIZE)}}你好`);
});

test("fs is the glyph height: the Fontsize is scaled by the font's ink share", () => {
  const style = sheet.styleMap.cn;
  assert.ok(Math.abs(fontSizeForFs(0.05, style, sheet) - SIZE) < 1e-9);
  assert.ok(SIZE > 54, "a line box is taller than the ink it holds");
});

test("the style's scale sizes positioned text as a whole, room to fit included", () => {
  const seg = ocrSeg({ pos: { x: 0.1, y: 0.2, an: 7, w: 0.2, fs: 0.05, lh: 0.07 } });
  const plain = placedPosOf(seg, sheet.styleMap.cn, sheet);
  const big = placedPosOf(seg, { ...sheet.styleMap.cn, scx: 150, scy: 150 }, sheet);
  assert.equal(big.size, plain.size, "Fontsize stays; libass applies ScaleX/ScaleY on top");
  assert.ok(Math.abs(big.width - plain.width * 1.5) < 1e-9);
  assert.ok(Math.abs(big.step - plain.step * 1.5) < 1e-9);
  assert.ok(Math.abs(big.maxHeight - plain.maxHeight * 1.5) < 1e-9);
  assert.ok(big.y < plain.y, "the ink's top stays on the box top: the bigger line box starts higher");
});

test("lines at the original's own pitch go out one event each, top-anchored on that pitch", () => {
  const seg = ocrSeg({
    zh: "第一行\n第二行",
    pos: { x: 0.1, y: 0.2, an: 7, w: 0.5, fs: 0.05, lh: 0.07 },
    cover: { box: [0.1, 0.2, 0.6, 0.32], fill: "#112233" },
  });
  const lines = outputLinesOf(source([seg]), sheet);
  const cue = positionedCueOf(seg, lines[2], lines, sheet);
  const top = 0.2 * Y - INK.top * SIZE;
  assert.deepEqual(positionedAssEvents(cue), [
    `{\\an7\\pos(192,${n2(top)})\\fs${n2(SIZE)}}第一行`,
    `{\\an7\\pos(192,${n2(top + 0.07 * Y)})\\fs${n2(SIZE)}}第二行`,
  ]);
  const ass = events(buildAssFrom(source([seg]), sheet)).filter(line => !line.includes("fo-fx"));
  assert.equal(ass.length, 2);
  // without a pitch of its own it stays one event, lines joined by \N
  delete seg.pos.lh;
  const one = positionedCueOf(seg, lines[2], lines, sheet);
  assert.equal(positionedAssEvents(one).length, 1);
});

test("plates reach past the OCR box so outlines and shadows stay hidden", () => {
  const box = plateBox(ocrSeg(), sheet);
  assert.deepEqual(box.map(v => Math.round(v * 1e6) / 1e6), [0.094375, 0.19, 0.305625, 0.26]);
  const big = plateBox(ocrSeg({ pos: { x: 0, y: 0, an: 7, fs: 0.2 }, cover: { box: [0, 0, 1, 1] } }), sheet);
  assert.deepEqual(big, [0, 0, 1, 1], "clamped to the frame");
});

test("positioned text wraps to w × PlayResX and shrinks before it overflows the box", () => {
  const long = "这是一段很长很长的译文，原来的框根本放不下它";
  const seg = ocrSeg({ zh: long });
  const style = sheet.styleMap.cn;
  const place = { x: 384, y: 243, an: 5, width: 384, size: 54, maxHeight: 54 };
  const laid = layoutPositioned(long, style, sheet, place);
  assert.ok(laid.size < 54, "font shrinks toward the floor first");
  assert.ok(laid.size >= 54 * POS_MIN_FONT_SCALE - 1e-9, "never below the floor");
  assert.ok(laid.block.lines.every(line => line.width <= 384 + 0.5), "every line fits w");
  assert.ok(laid.overflow, "this much text still overflows at the floor");

  const cue = positionedCueOf(seg, outputLinesOf(source([seg]), sheet)[2], outputLinesOf(source([seg]), sheet), sheet);
  const text = positionedAssText(cue);
  assert.ok(text.startsWith(`{\\an5\\pos(384,${n2(midY(243))})\\fs`), text);
  assert.equal(text.split("\\N").length, laid.block.lines.length, "breaks are explicit \\N");
  // the block is centred on the anchor (an5)
  assert.ok(Math.abs(cue.block.left + cue.block.width / 2 - 384) < 1e-6);
  assert.ok(Math.abs(cue.block.top + cue.block.height / 2 - midY(243)) < 1e-6);
});

test("short text that fits keeps the requested size", () => {
  const laid = layoutPositioned("你好", sheet.styleMap.cn, sheet,
    { x: 0, y: 0, an: 7, width: 384, size: 54, maxHeight: 54 });
  assert.equal(laid.size, 54);
  assert.equal(laid.overflow, false);
  assert.equal(laid.block.left, 0);
  assert.equal(laid.block.top, 0);
});

test("plates come first, on layer 0, in the exact FlowOCR form", () => {
  const ass = buildAssFrom(source([ocrSeg()]), sheet);
  const lines = events(ass);
  assert.equal(lines[0], PLATE);
  assert.equal(lines.length, 2);
  assert.equal(plateDialogue(1, 3, "cn", "文字识别", plateBox(ocrSeg(), sheet), "&H332211&", sheet), PLATE);
});

test("plates: 'omit' leaves them out, and fill null draws none but still masks for inpainting", () => {
  assert.ok(!buildAssFrom(source([ocrSeg()]), sheet, { plates: "omit" }).includes("fo-fx|plate"));
  const noFill = ocrSeg({ cover: { box: [0.1, 0.2, 0.3, 0.25], fill: null } });
  assert.ok(!buildAssFrom(source([noFill]), sheet).includes("fo-fx|plate"));
  const mask = events(buildPlatesAss(source([noFill]), sheet));
  assert.deepEqual(mask, [PLATE.replace("&H332211&", "&H000000&")]);
});

test("a segment shared by two showing lanes gets one plate, and the source text stacks above", () => {
  const seg = ocrSeg({ ja: "こんにちは" });
  const both = source([seg]);
  both.tracks[0].ja.hidden = false;
  const lines = events(buildAssFrom(both, sheet));
  assert.equal(lines.filter(line => line.includes("fo-fx|plate")).length, 1);
  const zh = lines.find(line => line.includes("你好"));
  const ja = lines.find(line => line.includes("こんにちは"));
  assert.ok(zh.includes(`\\pos(384,${n2(midY(243))})`), zh);
  // the translation block is SIZE high, centred on midY(243); the source sits right above it
  assert.ok(ja.includes(`\\an5\\pos(384,${n2(midY(243) - SIZE)})`), ja);
});

test("buildPlatesAss contains no other \\p1 drawing, even with particle effects bound", () => {
  const plain = { t0: 4, t1: 6, ja: "パーティクル", zh: "粒子" };
  const src = source([ocrSeg()], {
    segs: [plain],
    effects: [{ id: "e1", templateId: "karaoke-particle", enabled: true, target: { scope: "all" }, params: {} }],
  });
  const full = buildAssFrom(src, sheet);
  assert.ok(events(full).filter(line => line.includes("\\p1")).length > 1, "particles do draw in the full ASS");
  const positioned = events(full).filter(line => line.includes("你好"));
  assert.equal(positioned.length, 1, "positioned cues bypass the particle generator");
  assert.ok(positioned[0].includes(`\\pos(384,${n2(midY(243))})`));
  const plates = events(buildPlatesAss(src, sheet));
  assert.deepEqual(plates, [PLATE]);
});

test("documentAss and the editor builders pass the plate option through, default = draw", () => {
  const document = {
    schema: 1, video_id: "v", title: "v", source: "task", fp: null, rev: 0, subtitles: [],
    tracks: [{ id: "ocr1", name: "文字识别", ja: lane("origin", true), zh: lane("cn"), hja: 44, hzh: 44, segs: [ocrSeg()] }],
    track_meta: { name: "默认轨", ja: lane("origin"), zh: lane("cn") },
    projection: { schema: 1, mode: "final" },
  };
  assert.ok(documentAss(document).includes(PLATE));
  assert.ok(!documentAss(document, undefined, undefined, { plates: "omit" }).includes("fo-fx"));

  load([], [ocrSeg()]);
  assert.ok(buildAss().includes(PLATE));
  assert.ok(!buildAss({ plates: "omit" }).includes("fo-fx"));
  assert.ok(buildPiecesAss([{ t0: 0, t1: 10 }]).includes(PLATE));
  assert.deepEqual(events(buildPlates()), [PLATE]);
});

// ── no-trim rule ───────────────────────────────────────────────

test("overlap clamp skips positioned cues but still trims ordinary ones", () => {
  const a = ocrSeg({ t0: 1, t1: 5, zh: "甲" });
  const b = ocrSeg({ t0: 2, t1: 4, zh: "乙" });
  const lines = outputLinesOf(source([a, b]), sheet);
  const cues = cuesOf(lines[2], sheet);
  assert.deepEqual(cues.map(c => [c.t0, c.t1]), [[1, 5], [2, 4]]);

  const plain = trimLaneOverlaps([{ t0: 1, t1: 3 }, { t0: 2.5, t1: 4 }]);
  assert.deepEqual(plain.map(s => s.t1), [2.5, 4]);
  // an ordinary cue clamps to the next ordinary one, jumping over positioned ones in between
  const mixed = trimLaneOverlaps([{ t0: 1, t1: 6 }, { t0: 2, t1: 9, pos: {} }, { t0: 5, t1: 7 }]);
  assert.deepEqual(mixed.map(s => s.t1), [5, 9, 7]);
});

test("opening a document keeps pos/cover, drops malformed ones, and never trims positioned segs", () => {
  const segs = mapSegs([
    { t0: 1, t1: 5, ja: "", zh: "甲", pos: { x: 0.5, y: 0.5, an: 2 }, cover: { box: [0, 0, 0.5, 0.5], fill: null } },
    { t0: 2, t1: 4, ja: "", zh: "乙", pos: { x: 2, y: -1, an: 42, w: 0.3, fs: 0 } },
    { t0: 3, t1: 6, ja: "", zh: "丙", pos: { x: "a" }, cover: { box: [0.5, 0, 0.1, 1] } },
    { t0: 5.5, t1: 8, ja: "", zh: "丁" },
  ]);
  assert.deepEqual(segs.map(s => [s.t0, s.t1]), [[1, 5], [2, 4], [3, 5.5], [5.5, 8]]);
  assert.deepEqual(segs[0].pos, { x: 0.5, y: 0.5, an: 2 });
  assert.deepEqual(segs[0].cover, { box: [0, 0, 0.5, 0.5], fill: null });
  assert.deepEqual(segs[1].pos, { x: 1, y: 0, an: 5, w: 0.3 });
  assert.equal(segs[2].pos, undefined);
  assert.equal(segs[2].cover, undefined);
});

test("normalizers and cover helpers", () => {
  assert.equal(normalizeSegPos(null), undefined);
  assert.equal(normalizeSegCover({ box: [0, 0, 1] }), undefined);
  assert.deepEqual(normalizeSegCover({ box: [0.1, 0.1, 0.2, 0.2], fill: " #fff000 " }),
    { box: [0.1, 0.1, 0.2, 0.2], fill: "#fff000" });
  assert.deepEqual(unionCover({ box: [0.1, 0.2, 0.3, 0.4], fill: null }, { box: [0.2, 0.1, 0.5, 0.3], fill: "#000000" }),
    { box: [0.1, 0.1, 0.5, 0.4], fill: null });
  assert.deepEqual(posFromCover({ box: [0.1, 0.2, 0.3, 0.4] }, { x: 0, y: 0, an: 7, fs: 0.04 }),
    { x: 0.1, y: 0.2, an: 7, w: 0.3 - 0.1, fs: 0.04 });
});

// ── editor whitelists ──────────────────────────────────────────

const STYLE_SHEET = "";

function load(segs = [], ocr = [], t = 2) {
  resetHistory();
  setStyleSheet(STYLE_SHEET);
  docStore.set({
    segs,
    tracks: [{
      id: "ocr1", name: "文字识别", ja: lane("origin", true), zh: lane("cn"), hja: 44, hzh: 44, segs: ocr,
      source: { kind: "screen-text", task: "t1", region: "r1", label: "标题", hash: "h" },
    }],
    trackMeta: { name: "默认轨", ja: lane("origin"), zh: lane("cn") },
    effects: [],
    styles: STYLE_SHEET,
    rev: 0,
  });
  playStore.set({ t, playing: false });
  selStore.set({ curTrack: 0, sel: 0, selSet: new Set(), preview: null });
  selectLane(null);
}

test("undo snapshots and the save payload deep-copy pos and cover", () => {
  const seg = ocrSeg();
  const [copy] = cloneSegs([seg]);
  assert.deepEqual(copy.pos, seg.pos);
  assert.deepEqual(copy.cover, seg.cover);
  assert.notEqual(copy.pos, seg.pos);
  assert.notEqual(copy.cover.box, seg.cover.box);
  const packed = packSeg(seg, 0);
  assert.deepEqual(packed.pos, seg.pos);
  assert.deepEqual(packed.cover, seg.cover);
  assert.notEqual(packed.pos, seg.pos);
});

test("split copies pos/cover to both halves; merge unions the cover boxes", () => {
  load([], [ocrSeg({ zh: "第一句，第二句" })], 2);
  splitAtPlayhead();
  const [a, b] = docStore.get().tracks[0].segs;
  assert.deepEqual(b.pos, a.pos);
  assert.deepEqual(b.cover, a.cover);
  assert.notEqual(b.pos, a.pos);
  b.cover.box = [0.15, 0.25, 0.4, 0.3];

  selStore.set({ curTrack: 0, sel: 0 });
  mergeNext();
  const [merged] = docStore.get().tracks[0].segs;
  assert.deepEqual(merged.cover.box, [0.1, 0.2, 0.4, 0.3]);
  assert.equal(merged.cover.fill, "#112233");
  assert.deepEqual(merged.pos, ocrSeg().pos);
});

// ── preview: hit boxes and dragging ────────────────────────────

test("positioned boxes sit at their \\pos and are not pushed by collision avoidance", () => {
  // a bottom-aligned default-track line plus a positioned box near the bottom edge
  const near = ocrSeg({ pos: { x: 0.5, y: 0.97, an: 2, w: 0.5, fs: 0.05 } });
  load([{ t0: 1, t1: 3, ja: "", zh: "普通字幕" }], [near, ocrSeg({ t0: 1.5, t1: 2.5, zh: "同时" })]);
  const boxes = subtitleBoxesAt(2);
  const positioned = boxes.filter(box => box.positioned);
  assert.equal(positioned.length, 2, "both overlapping positioned segs show on the same lane");
  const low = positioned.find(box => box.seg === near);
  assert.equal(low.shifted, 0);
  assert.ok(Math.abs(low.block.top + low.block.height - (0.97 * Y + INK.bottom * SIZE)) < 1e-6,
    "an2: the ink's bottom edge on y");
  assert.ok(Math.abs(low.block.left + low.block.width / 2 - X / 2) < 1e-6);
  assert.equal(low.align, 2);

  const centre = { x: low.block.left + low.block.width / 2, y: low.block.top + low.block.height / 2 };
  const hit = hitTest(boxes, centre.x, centre.y);
  assert.equal(hit.seg, near);
  assert.deepEqual(laneOfBox(hit), { trackId: "ocr1", lang: "zh", seg: near });
  selectLane(laneOfBox(hit));
  assert.equal(boxForLane(stageStore.get().sel).seg, near, "selection stays on the picked seg");
});

test("arrow-key nudge moves seg.pos, not the style, and undoes in one step", () => {
  const seg = ocrSeg();
  load([], [seg]);
  const before = docStore.get().styles;
  selectLane({ trackId: "ocr1", lang: "zh", seg });
  nudgeSelection(96, 0);
  nudgeSelection(0, 54);
  const moved = docStore.get().tracks[0].segs[0];
  assert.ok(Math.abs(moved.pos.x - 0.25) < 1e-9);
  assert.ok(Math.abs(moved.pos.y - 0.275) < 1e-9);
  assert.equal(docStore.get().styles, before, "style margins untouched");
  undo();
  assert.deepEqual(docStore.get().tracks[0].segs[0].pos, ocrSeg().pos);
});

test("dragging a positioned box writes seg.pos and snaps to the cover box", () => {
  const seg = ocrSeg({ pos: { x: 0.5, y: 0.5, an: 7, w: 0.2, fs: 0.05 } });
  load([], [seg]);
  const listeners = {};
  const stage = {
    getBoundingClientRect: () => ({ left: 0, top: 0, width: X, height: Y }),
    setPointerCapture() {},
    addEventListener: (type, fn) => { listeners[type] = fn; },
    removeEventListener: (type) => { delete listeners[type]; },
  };
  globalThis.requestAnimationFrame ??= () => 1;
  globalThis.cancelAnimationFrame ??= () => {};
  const box = subtitleBoxesAt(2).find(b => b.positioned);
  const at = { x: box.block.left + 5, y: box.block.top + 5 };
  beginDrag({ clientX: at.x, clientY: at.y, pointerId: 1 }, stage, box, "move");
  // drop the ink's top-left a few px off the cover box's top-left → snaps onto it
  const dx = 0.1 * X + 3 - box.block.left, dy = 0.2 * Y - INK.top * SIZE + 1 - box.block.top;
  listeners.pointermove({ clientX: at.x + dx, clientY: at.y + dy, altKey: false });
  listeners.pointerup({});
  const pos = docStore.get().tracks[0].segs[0].pos;
  assert.ok(Math.abs(pos.x - 0.1) < 1e-9, "x snapped to cover left, got " + pos.x);
  assert.ok(Math.abs(pos.y - 0.2) < 1e-9, "y snapped to cover top, got " + pos.y);
  assert.equal(stageStore.get().drag, null);
});

test("inspector writes: pos patch, plate colour / off, back to the original spot", () => {
  const seg = ocrSeg({ pos: { x: 0.9, y: 0.9, an: 5, w: 0.5, fs: 0.05 } });
  load([], [seg]);
  setSegPos(seg, { an: 7 });
  setSegFill(seg, null);
  assert.ok(!buildAss().includes("fo-fx"), "plate off");
  assert.ok(buildPlates().includes("fo-fx|plate"), "still erased");
  resetSegPos(seg);
  assert.deepEqual(seg.pos, { x: 0.1, y: 0.2, an: 7, w: 0.3 - 0.1, fs: 0.05 });
  assert.ok(buildAss().includes(`\\an7\\pos(192,${n2(216 - INK.top * SIZE)})\\fs${n2(SIZE)}}你好`));
  assert.equal(getStyleSheet().playRes.x, X);
});

test("the style panel's 底板 switch turns a style's plates on and off together, keeping their colours", () => {
  const sampled = ocrSeg({ cover: { box: [0.1, 0.2, 0.3, 0.25], fill: null, bg: "#445566" } });
  const old = ocrSeg({ t0: 4, t1: 5, cover: { box: [0.1, 0.2, 0.3, 0.25], fill: "#112233" } });
  const own = ocrSeg({ t0: 6, t1: 7, style: { zh: "origin" } });
  load([], [sampled, old, own]);
  const segs = plateSegsOfStyle("cn");
  assert.deepEqual(segs, [sampled, old], "a line on its own style is not this style's");
  assert.deepEqual(plateSegsOfStyle("cn", own), [own], "with an own style, only that line");

  setPlates(segs, true);
  assert.equal(sampled.cover.fill, "#445566", "switched on in the colour sampled around the original");
  setPlates(segs, false);
  assert.ok(segs.every(seg => !plateOn(seg)));
  assert.equal(events(buildAss()).filter(line => line.includes("fo-fx|plate")).length, 1,
    "only the line on its own style keeps its plate");
  setPlates(segs, true);
  assert.equal(old.cover.fill, "#112233", "an older line without a sample gets its own colour back, not black");
  undo();
  const [first, second] = docStore.get().tracks[0].segs;
  assert.ok(!plateOn(first) && !plateOn(second), "one switch, one undo step");
});

test("a drawn plate moves with its translation; the erase mask stays on the original text", () => {
  // dragged 0.1 right and 0.05 down from where it started (the middle of its cover box)
  const moved = ocrSeg({ pos: { x: 0.3, y: 0.275, an: 5, w: 0.2, fs: 0.05 } });
  const drawn = events(buildAssFrom(source([moved]), sheet)).find(line => line.includes("fo-fx|plate"));
  // the PLATE box [181.2,205.2] shifted by (192, 54) px
  assert.ok(drawn.includes("\pos(373.20,259.20)"), drawn);
  assert.ok(drawn.includes("m 0 0 l 405.60 0 405.60 75.60 0 75.60"), "same size, only moved");
  const mask = events(buildPlatesAss(source([moved]), sheet));
  assert.deepEqual(mask, [PLATE], "LaMa still erases where the original text is");
  assert.deepEqual(plateBox(ocrSeg(), sheet, true), plateBox(ocrSeg(), sheet), "not dragged: nothing moves");
});
