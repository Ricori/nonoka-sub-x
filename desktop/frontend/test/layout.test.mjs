import assert from "node:assert/strict";
import test from "node:test";

import { hGroupOf, ignoresMarginV, layoutBlock, marginsForBlock, vGroupOf } from "../src/subtitles/layout.ts";
import { composeSheet } from "../src/subtitles/styles.ts";
import { DEFAULT_STYLE_SHEET } from "../src/subtitles/constants.ts";

// 预览里拖字幕 = 反解 Margin 再写回样式。正向那套是 libass 排版的复算（逐字特效也用
// 它，口径由 test/subtitles.test.mjs 的 \pos 断言钉住），这里盯的是反向那套：
// 九种 Alignment 各自该动哪个 Margin，以及「正向→施加位移→反向→再正向」能不能闭合。

const sheet = composeSheet(DEFAULT_STYLE_SHEET);
const { x: X, y: Y } = sheet.playRes;

const styleAt = (align, over = {}) => ({
  name: "T", font: "方正准圆_GBK", size: 70,
  c1: "&H00FFFFFF", c3: "&H00000000", c4: "&H00000000",
  bold: 0, italic: 0, scx: 100, scy: 100, sp: 0,
  outline: 2, shadow: 2, align, ml: 40, mr: 60, mv: 30, ...over,
});

const ALIGNMENTS = [1, 2, 3, 4, 5, 6, 7, 8, 9];

// 排版走的是浮点累加，末位噪声是常态；断言位置时按亚像素比
const near = (actual, expected, why) =>
  assert.ok(Math.abs(actual - expected) < 1e-6, (why ?? "") + " 期望 " + expected + "，实际 " + actual);

test("alignment groups split the numpad the way libass does", () => {
  assert.deepEqual(ALIGNMENTS.map(hGroupOf), [1, 2, 3, 1, 2, 3, 1, 2, 3]);
  assert.deepEqual(ALIGNMENTS.map(vGroupOf), [1, 1, 1, 2, 2, 2, 3, 3, 3]);
  assert.deepEqual(ALIGNMENTS.map(ignoresMarginV), [false, false, false, true, true, true, false, false, false]);
});

test("centred text is centred between the margins, not in the frame", () => {
  const style = styleAt(2, { ml: 10, mr: 10 });
  const block = layoutBlock("你好世界", style, sheet);
  assert.equal(block.left + block.width / 2, X / 2);
  // 左右边距不等时块心跟着偏：偏移量正好是 (ml - mr) / 2
  const skewed = layoutBlock("你好世界", styleAt(2, { ml: 200, mr: 0 }), sheet);
  assert.equal(skewed.left + skewed.width / 2, X / 2 + 100);
});

test("each corner alignment pins its own corner to its own margins", () => {
  const text = "字";
  const left = layoutBlock(text, styleAt(1), sheet);      // 左下
  assert.equal(left.left, 40);
  assert.equal(left.top + left.height, Y - 30);
  const right = layoutBlock(text, styleAt(9), sheet);     // 右上
  assert.equal(right.left + right.width, X - 60);
  assert.equal(right.top, 30);
  const topLeft = layoutBlock(text, styleAt(7), sheet);
  assert.equal(topLeft.left, 40);
  assert.equal(topLeft.top, 30);
  const bottomRight = layoutBlock(text, styleAt(3), sheet);
  assert.equal(bottomRight.left + bottomRight.width, X - 60);
  assert.equal(bottomRight.top + bottomRight.height, Y - 30);
});

test("vertically centred alignments ignore MarginV entirely", () => {
  const a = layoutBlock("字", styleAt(5, { mv: 30 }), sheet);
  const b = layoutBlock("字", styleAt(5, { mv: 900 }), sheet);
  assert.equal(a.top, b.top);
  assert.equal(a.top, (Y - a.height) / 2);
});

test("line height is the font size, and a second line doubles the block", () => {
  const one = layoutBlock("一行", styleAt(2), sheet);
  const two = layoutBlock("第二行\n继续", styleAt(2), sheet);
  assert.equal(one.lineHeight, 70);
  assert.equal(one.height, 70);
  assert.equal(two.lines.length, 2);
  assert.equal(two.height, 140);
  assert.equal(two.lines[1].top - two.lines[0].top, 70);
  // 块的外接矩形是各行的并集：宽的那行说了算
  near(two.width, Math.max(two.lines[0].width, two.lines[1].width), "块宽取最宽那行");
});

test("marginsForBlock is the exact inverse of layoutBlock", () => {
  for (const align of ALIGNMENTS) {
    const style = styleAt(align);
    const block = layoutBlock("这是一句字幕", style, sheet);
    const back = marginsForBlock(block, style, sheet);
    assert.equal(back.marginl, style.ml, `align ${align} 的 MarginL 没还原`);
    assert.equal(back.marginr, style.mr, `align ${align} 的 MarginR 没还原`);
    assert.equal(back.marginv, style.mv, `align ${align} 的 MarginV 没还原`);
  }
});

test("moving a block round-trips to a block in the new place", () => {
  // 位移要落在边距余量之内（ml 40 / mr 60 / mv 30）：超出去只能贴边，见下一条
  for (const align of ALIGNMENTS) {
    const style = styleAt(align);
    const block = layoutBlock("这是一句字幕", style, sheet);
    const want = { ...block, left: block.left + 15, top: block.top - 20 };
    const margins = marginsForBlock(want, style, sheet);
    const moved = layoutBlock("这是一句字幕", {
      ...style, ml: margins.marginl, mr: margins.marginr, mv: margins.marginv,
    }, sheet);
    near(moved.left, want.left, "align " + align + " 的水平位移");
    // 垂直居中那三个不吃 MarginV，纵向原地不动是预期行为（调用方要锁 Y 轴）
    near(moved.top, ignoresMarginV(align) ? block.top : want.top, "align " + align + " 的垂直位移");
  }
});

test("a block can never be dragged out of the frame", () => {
  // ASS 的 Margin 不能为负，所以「贴边」就是极限——拖动时光标越过边界，块停在边上
  const right = styleAt(3);
  const block = layoutBlock("靠右", right, sheet);
  const margins = marginsForBlock({ ...block, left: block.left + 500 }, right, sheet);
  assert.equal(margins.marginr, 0);
  const moved = layoutBlock("靠右", { ...right, mr: 0 }, sheet);
  near(moved.left + moved.width, X, "右沿最多到画面边缘");
});

test("a centred move is expressed as a margin difference, keeping the usable width", () => {
  const style = styleAt(2, { ml: 150, mr: 150 });
  const block = layoutBlock("居中的一句", style, sheet);
  const margins = marginsForBlock({ ...block, left: block.left + 100 }, style, sheet);
  assert.equal(margins.marginl - margins.marginr, 200, "块心移 100，左右差应当变 200");
  assert.equal(margins.marginl + margins.marginr, 300, "留白总量不变，可用宽度才不变");
});

test("a centred block pushed past the frame edge clamps without losing the offset", () => {
  const style = styleAt(2, { ml: 10, mr: 10 });
  const block = layoutBlock("靠边", style, sheet);
  const margins = marginsForBlock({ ...block, left: block.left - 900 }, style, sheet);
  assert.equal(margins.marginl, 0, "顶到左边界");
  assert.equal(margins.marginr, 1800, "差值改由 MarginR 独自维持");
  const moved = layoutBlock("靠边", { ...style, ml: 0, mr: 1800 }, sheet);
  near(moved.left + moved.width / 2, block.left + block.width / 2 - 900, "块心偏移仍然成立");
});

test("margins are rounded and clamped inside the frame", () => {
  const style = styleAt(2);
  const block = layoutBlock("越界", style, sheet);
  const below = marginsForBlock({ ...block, top: Y + 500 }, style, sheet);
  assert.equal(below.marginv, 0, "拖到画面外只能贴边");
  const above = marginsForBlock({ ...block, top: -9999 }, style, sheet);
  assert.equal(above.marginv, Y - 1);
  assert.ok(Number.isInteger(above.marginv) && Number.isInteger(above.marginl));
});
