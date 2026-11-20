import assert from "node:assert/strict";
import test from "node:test";

import { getStyleSheet, setStyleSheet } from "../src/editor/ass.ts";
import { buildAss } from "../src/editor/lib/assBuild.ts";
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
  "Style: origin,方正准圆_GBK,70,&H00FFF9FD,&HF0000000,&H00EF9320,&H30633306,"
  + "0,0,0,0,100,100,7,0,1,2,2,8,12,12,30,1",
  "Style: cn,方正准圆_GBK,70,&H00FFF9FD,&HF0000000,&H00EF9320,&H30633306,"
  + "0,0,0,0,100,100,7,0,1,2,2,2,12,12,30,1",
].join("\n");

const lane = (style) => ({ hidden: false, style });

function load(over = {}) {
  resetHistory();
  setStyleSheet(SHEET);
  docStore.set({
    segs: [{ t0: 1, t1: 3, ja: "こんにちは", zh: "你好" }],
    tracks: [],
    trackMeta: { name: "默认轨", ja: lane("origin"), zh: lane("cn") },
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
  assert.deepEqual(boxes.map(b => b.styleName), ["cn", "origin"]);
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
  // 带宽就是可用带：左右各让出 12px 安全边距
  assert.equal(band.block.left, 12);
  assert.equal(band.block.width, getStyleSheet().playRes.x - 24);
});

test("clicking inside a box hits the text area, leaving empty margins free for play/pause", () => {
  load();
  const boxes = subtitleBoxesAt(2);
  const [zh] = boxes;
  // 容器默认是视频宽减去左右 12px 安全边距
  assert.equal(zh.block.left, 12);
  assert.equal(zh.block.width, getStyleSheet().playRes.x - 24);
  const centre = { x: zh.block.left + zh.block.width / 2, y: zh.block.top + zh.block.height / 2 };
  assert.equal(hitTest(boxes, centre.x, centre.y).lang, "zh");
  // 点在容器左右空白处不命中字幕，留给舞台点击播放/暂停
  assert.equal(hitTest(boxes, 30, centre.y), null, "短句两侧空白不该被全宽容器吞掉");
  assert.equal(hitTest(boxes, 5, 5), null, "画面角落没有字幕");
  // 把 JP 钉到 CN 的位置上。用上对齐 + 大 MarginV 而不是下对齐——同为下对齐会被碰撞
  // 避让推开（见下一条），跨组才会真重叠
  patchStyle("origin", { alignment: 8, marginv: 980 });
  const stacked = subtitleBoxesAt(2);
  const hit = hitTest(stacked, centre.x, centre.y);
  assert.equal(hit.lang, "ja", "重叠时取最上层");
  // Alt 轮换：给上一次命中的下标，就换到下面那条
  assert.equal(hitTest(stacked, centre.x, centre.y, hit.index).lang, "zh");
});

test("narrowing a style's horizontal margins wraps the text, grows box height, and emits \\N in ASS", () => {
  const longText = "明明早就已经决定好了要说的话";
  load({ segs: [{ t0: 1, t1: 3, ja: "こんにちは", zh: longText }] });
  const wide = subtitleBoxesAt(2).find(b => b.lang === "zh");
  assert.equal(wide.block.lines.length, 1);
  assert.equal(wide.block.height, 70);
  assert.ok(buildAss().includes(",," + longText + "\n"), "足够宽时不插入多余换行");

  patchStyle("cn", { marginl: 600, marginr: 600 });   // 宽 = 720，放不下 14 个全角字
  const narrow = subtitleBoxesAt(2).find(b => b.lang === "zh");
  assert.equal(narrow.block.width, 720);
  assert.ok(narrow.block.lines.length >= 2, "收窄后应当自动换行");
  assert.equal(narrow.block.height, narrow.block.lines.length * 70, "框高随折行行数自动调整");
  const expectedWrapped = narrow.block.lines.map(l => l.text).join("\\N");
  assert.ok(buildAss().includes(",," + expectedWrapped + "\n"), "ASS 事件应输出与选中框一致的 \\N 折行");

  // 拉宽回去后立即恢复单行，原始句子文本未被破坏
  patchStyle("cn", { marginl: 12, marginr: 12 });
  assert.equal(docStore.get().segs[0].zh, longText);
  assert.ok(buildAss().includes(",," + longText + "\n"), "拉宽后应恢复单行");
});

test("linespacing expands multi-line box height and emits transparent ASS spacer lines", () => {
  load({ segs: [{ t0: 1, t1: 3, ja: "単行", zh: "第一行\n第二行" }] });
  patchStyle("cn", { linespacing: 22 });
  const zh = subtitleBoxesAt(2).find(b => b.lang === "zh");
  assert.equal(zh.block.height, 2 * 70 + 22);
  const ass = buildAss();
  assert.ok(
    ass.includes(",,第一行\\N{\\fs22\\fscy100\\bord0\\shad0\\alpha&HFF&}\\h\\N{\\r}第二行\n"),
    "多行字幕应插入全透明占位行撑开行距",
  );
  // 单行字幕（即使设了行间距）也不插入占位行
  patchStyle("origin", { linespacing: 22 });
  assert.ok(buildAss().includes(",,単行\n"), "单行字幕不该插入占位行");
});

test("two bottom-aligned lanes are stacked, not drawn on top of each other", () => {
  load();
  patchStyle("origin", { alignment: 2, marginv: 30 });
  const boxes = subtitleBoxesAt(2);
  const zh = boxes.find(b => b.lang === "zh");
  const ja = boxes.find(b => b.lang === "ja");
  assert.equal(zh.shifted, 0, "先画的那条停在自己的边距上");
  assert.ok(ja.shifted < 0, "后画的被顶上去了");
  assert.equal(ja.block.top + ja.block.height, zh.block.top, "顶到贴着前一条的上沿");
});

test("nudging writes the move back into this video's style sheet without changing box width", () => {
  load();
  patchStyle("cn", { marginl: 100, marginr: 100 });
  selectLane({ trackId: "default", lang: "zh" });
  nudgeSelection(12, -20);
  // 水平平移保持容器宽度不变（ml + mr = 200），垂直加到 MarginV 上
  assert.equal(field("cn", "marginl"), "112");
  assert.equal(field("cn", "marginr"), "88");
  assert.equal(field("cn", "marginv"), "50");
  // 别的样式一个字节都不该动
  assert.equal(field("origin", "marginv"), "30");
  assert.equal(field("cn", "secondarycolour"), "&HF0000000");
});

test("a nudge really moves the box, and Ctrl+Z puts it back", () => {
  load();
  const before = subtitleBoxesAt(2).find(b => b.lang === "zh").block;
  selectLane({ trackId: "default", lang: "zh" });
  nudgeSelection(0, -40);
  const after = subtitleBoxesAt(2).find(b => b.lang === "zh").block;
  assert.equal(after.top, before.top - 40);

  undo();
  assert.equal(field("cn", "marginv"), "30");
  assert.equal(subtitleBoxesAt(2).find(b => b.lang === "zh").block.top, before.top);
  // 撤销也要把渲染用的那份单例换回去，否则重画用的还是新样式
  assert.equal(getStyleSheet().styleMap["cn"].mv, 30);
});

test("a vertically centred style refuses the vertical half of a nudge", () => {
  load();
  patchStyle("cn", { alignment: 5, marginl: 100, marginr: 100 });
  selectLane({ trackId: "default", lang: "zh" });
  const before = subtitleBoxesAt(2).find(b => b.lang === "zh").block.top;
  nudgeSelection(0, 30);
  assert.equal(subtitleBoxesAt(2).find(b => b.lang === "zh").block.top, before);
  assert.equal(stageStore.get().hint, "middle-v", "得说一声为什么没动");
  // 水平方向照常，同时保持容器宽度不变
  nudgeSelection(25, 0);
  assert.equal(field("cn", "marginl"), "125");
  assert.equal(field("cn", "marginr"), "75");
});

test("Angle is read from the raw fields, not from the lossy AssStyle", () => {
  load();
  assert.equal(styleAngle("cn"), 0);
  patchStyle("cn", { angle: 15 });
  assert.equal(styleAngle("cn"), 15);
  // AssStyle 不建模 Angle，改它不能顺手把别的字段冲掉
  assert.equal(field("cn", "secondarycolour"), "&HF0000000");
  assert.equal(field("cn", "spacing"), "7");
});

test("a hidden lane has no box at all", () => {
  load({ trackMeta: { name: "默认轨", ja: { hidden: true, style: "origin" }, zh: lane("cn") } });
  assert.deepEqual(subtitleBoxesAt(2).map(b => b.lang), ["zh"]);
  assert.equal(boxForLane({ trackId: "default", lang: "ja" }, 2), null);
});

test("a visible lane bound to nothing falls back to origin/cn", () => {
  load({ trackMeta: { name: "默认轨", ja: { hidden: false, style: null }, zh: lane("cn") } });
  assert.deepEqual(subtitleBoxesAt(2).map(b => b.lang), ["zh", "ja"]);
  assert.equal(boxForLane({ trackId: "default", lang: "ja" }, 2).styleName, "origin");
});
