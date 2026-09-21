import assert from "node:assert/strict";
import test from "node:test";

import { getStyleSheet, setStyleSheet } from "../src/editor/ass.ts";
import { docStore } from "../src/editor/store/docStore.ts";
import { playStore } from "../src/editor/store/playStore.ts";
import { selectLane, stageStore } from "../src/editor/store/stageStore.ts";
import { boxForLane, hitTest, subtitleBoxesAt } from "../src/editor/lib/stageHit.ts";
import { nudgeSelection, styleAngle } from "../src/editor/lib/stageDrag.ts";
import { patchStyle } from "../src/editor/lib/styleEdit.ts";
import { resetHistory, undo } from "../src/editor/lib/history.ts";
import { parseSheet } from "../src/subtitles/styles.ts";

// 预览里点选和拖动字幕。这里直接驱动真实的 store，走的就是编辑器跑的那条路：
// docStore → outputLines → layoutBlock → 命中盒，以及反过来 patchStyle → 样式表原文。
// 只有 libass 那一步不在（jassub 没装载时 syncSubs 自己短路），别的都是真的。

const SHEET = [
  "[V4+ Styles]",
  "Style: JP,方正准圆_GBK,70,&H00FFF9FD,&HF0000000,&H00EF9320,&H30633306,"
  + "0,0,0,0,100,100,7,0,1,2,2,8,10,10,30,1",
  "Style: CN,方正准圆_GBK,70,&H00FFF9FD,&HF0000000,&H00EF9320,&H30633306,"
  + "0,0,0,0,100,100,7,0,1,2,2,2,10,10,30,1",
].join("\n");

const lane = (style) => ({ hidden: false, style });

function load(over = {}) {
  resetHistory();
  setStyleSheet(SHEET);
  docStore.set({
    segs: [{ t0: 1, t1: 3, ja: "こんにちは", zh: "你好" }],
    tracks: [],
    trackMeta: { name: "默认轨", ja: lane("JP"), zh: lane("CN") },
    effects: [],
    styles: SHEET,
    rev: 0,
    ...over,
  });
  playStore.set({ t: 2 });
  selectLane(null);
}

const field = (name, key) => parseSheet(docStore.get().styles).fields[name][key];
const near = (actual, expected, why) =>
  assert.ok(Math.abs(actual - expected) < 1e-6, (why ?? "") + " 期望 " + expected + "，实际 " + actual);

test("a paused frame reports one box per visible lane, in stacking order", () => {
  load();
  const boxes = subtitleBoxesAt(2);
  assert.deepEqual(boxes.map(b => b.lang), ["zh", "ja"]);
  assert.deepEqual(boxes.map(b => b.styleName), ["CN", "JP"]);
  assert.deepEqual(boxes.map(b => b.text), ["你好", "こんにちは"]);
});

test("each box sits where its own alignment and margins put it", () => {
  load();
  const [zh, ja] = subtitleBoxesAt(2);
  const { x: X, y: Y } = getStyleSheet().playRes;
  // CN 是下对齐（align 2），块底距画面底 = MarginV
  assert.equal(zh.block.top + zh.block.height, Y - 30);
  // JP 是上对齐（align 8），块顶距画面顶 = MarginV
  assert.equal(ja.block.top, 30);
  // 两条都居中
  for (const box of [zh, ja]) near(box.block.left + box.block.width / 2, X / 2, box.lang + " 应当水平居中");
});

test("nothing is reported outside the cue, and gaps fall back to a band", () => {
  load();
  assert.equal(subtitleBoxesAt(5).length, 0);
  const band = boxForLane({ trackId: "default", lang: "zh" }, 5);
  assert.ok(band, "空隙里也要能继续选中这条轨");
  assert.equal(band.seg, null);
  assert.equal(band.text, "");
  // 带宽就是可用带：左右各让出一个边距
  assert.equal(band.block.left, 10);
  assert.equal(band.block.width, getStyleSheet().playRes.x - 20);
});

test("clicking inside a box finds it, and overlaps resolve to the top layer", () => {
  load();
  const boxes = subtitleBoxesAt(2);
  const [zh] = boxes;
  const centre = { x: zh.block.left + zh.block.width / 2, y: zh.block.top + zh.block.height / 2 };
  assert.equal(hitTest(boxes, centre.x, centre.y).lang, "zh");
  assert.equal(hitTest(boxes, 5, 5), null, "画面角落没有字幕");
  // 把 JP 钉到 CN 的位置上。用上对齐 + 大 MarginV 而不是下对齐——同为下对齐会被碰撞
  // 避让推开（见下一条），跨组才会真重叠
  patchStyle("JP", { alignment: 8, marginv: 980 });
  const stacked = subtitleBoxesAt(2);
  const hit = hitTest(stacked, centre.x, centre.y);
  assert.equal(hit.lang, "ja", "重叠时取最上层");
  // Alt 轮换：给上一次命中的下标，就换到下面那条
  assert.equal(hitTest(stacked, centre.x, centre.y, hit.index).lang, "zh");
});

test("two bottom-aligned lanes are stacked, not drawn on top of each other", () => {
  load();
  patchStyle("JP", { alignment: 2, marginv: 30 });
  const boxes = subtitleBoxesAt(2);
  const zh = boxes.find(b => b.lang === "zh");
  const ja = boxes.find(b => b.lang === "ja");
  assert.equal(zh.shifted, 0, "先画的那条停在自己的边距上");
  assert.ok(ja.shifted < 0, "后画的被顶上去了");
  assert.equal(ja.block.top + ja.block.height, zh.block.top, "顶到贴着前一条的上沿");
});

test("nudging writes the move back into this video's style sheet", () => {
  load();
  selectLane({ trackId: "default", lang: "zh" });
  nudgeSelection(12, -20);
  // CN 居中 + 下对齐：水平靠左右边距的差值表达，垂直加到 MarginV 上
  assert.equal(Number(field("CN", "marginl")) - Number(field("CN", "marginr")), 24);
  assert.equal(field("CN", "marginv"), "50");
  // 别的样式一个字节都不该动
  assert.equal(field("JP", "marginv"), "30");
  assert.equal(field("CN", "secondarycolour"), "&HF0000000");
});

test("a nudge really moves the box, and Ctrl+Z puts it back", () => {
  load();
  const before = subtitleBoxesAt(2).find(b => b.lang === "zh").block;
  selectLane({ trackId: "default", lang: "zh" });
  nudgeSelection(0, -40);
  const after = subtitleBoxesAt(2).find(b => b.lang === "zh").block;
  assert.equal(after.top, before.top - 40);

  undo();
  assert.equal(field("CN", "marginv"), "30");
  assert.equal(subtitleBoxesAt(2).find(b => b.lang === "zh").block.top, before.top);
  // 撤销也要把渲染用的那份单例换回去，否则重画用的还是新样式
  assert.equal(getStyleSheet().styleMap["CN"].mv, 30);
});

test("a vertically centred style refuses the vertical half of a nudge", () => {
  load();
  patchStyle("CN", { alignment: 5 });
  selectLane({ trackId: "default", lang: "zh" });
  const before = subtitleBoxesAt(2).find(b => b.lang === "zh").block.top;
  nudgeSelection(0, 30);
  assert.equal(subtitleBoxesAt(2).find(b => b.lang === "zh").block.top, before);
  assert.equal(stageStore.get().hint, "middle-v", "得说一声为什么没动");
  // 水平方向照常
  nudgeSelection(25, 0);
  assert.equal(Number(field("CN", "marginl")) - Number(field("CN", "marginr")), 50);
});

test("Angle is read from the raw fields, not from the lossy AssStyle", () => {
  load();
  assert.equal(styleAngle("CN"), 0);
  patchStyle("CN", { angle: 15 });
  assert.equal(styleAngle("CN"), 15);
  // AssStyle 不建模 Angle，改它不能顺手把别的字段冲掉
  assert.equal(field("CN", "secondarycolour"), "&HF0000000");
  assert.equal(field("CN", "spacing"), "7");
});

test("a lane bound to nothing has no box at all", () => {
  load({ trackMeta: { name: "默认轨", ja: { hidden: false, style: null }, zh: lane("CN") } });
  assert.deepEqual(subtitleBoxesAt(2).map(b => b.lang), ["zh"]);
  assert.equal(boxForLane({ trackId: "default", lang: "ja" }, 2), null);
});
