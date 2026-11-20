import assert from "node:assert/strict";
import test from "node:test";

import { hGroupOf, ignoresMarginV, layoutBlock, marginsForBlock, vGroupOf } from "../src/subtitles/layout.ts";
import { assHeadOf, composeSheet } from "../src/subtitles/styles.ts";
import { DEFAULT_MARGIN_H, DEFAULT_STYLE_SHEET } from "../src/subtitles/constants.ts";

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

test("default styles use 12px horizontal safety margins, centred alignment, and WrapStyle 2", () => {
  assert.match(assHeadOf(sheet), /^WrapStyle: 2$/m);
  for (const name of ["origin", "cn", "speaker1"]) {
    const st = sheet.styleMap[name];
    assert.equal(st.ml, DEFAULT_MARGIN_H, name + " 默认左边距");
    assert.equal(st.mr, DEFAULT_MARGIN_H, name + " 默认右边距");
    assert.equal(hGroupOf(st.align), 2, name + " 默认水平居中");
  }
});

test("line height is the font size, container spans margins, and content fits lines", () => {
  const one = layoutBlock("一行", styleAt(2), sheet);
  const two = layoutBlock("第二行\n继续", styleAt(2), sheet);
  assert.equal(one.lineHeight, 70);
  assert.equal(one.height, 70);
  assert.equal(two.lines.length, 2);
  assert.equal(two.height, 140);
  assert.equal(two.lines[1].top - two.lines[0].top, 70);
  // 容器宽由左右边距决定，文字紧致区 content 取最宽那行
  assert.equal(two.left, 40);
  assert.equal(two.width, X - 40 - 60);
  near(two.content.width, Math.max(two.lines[0].width, two.lines[1].width), "文字区宽取最宽那行");
});

test("long text wraps automatically to container width and grows box height", () => {
  // 单个全角字在 Fontsize 70 下约 61.18px，宽度 200px 每行恰好放 3 个字
  const narrow = styleAt(2, { ml: 860, mr: 860 });   // 宽 = 200
  const wrapped = layoutBlock("一二三四五六七", narrow, sheet);
  assert.equal(wrapped.width, 200);
  assert.deepEqual(wrapped.lines.map(l => l.text), ["一二三", "四五六", "七"]);
  assert.equal(wrapped.height, 210, "折成 3 行后框高自动长到 3 行");
  assert.equal(wrapped.top + wrapped.height, Y - 30, "底对齐保持底沿贴 MarginV");
});

test("manual newlines combine cleanly with automatic wrapping", () => {
  const narrow = styleAt(2, { ml: 860, mr: 860 });   // 宽 = 200，每行约 3 个全角字
  const mixed = layoutBlock("一二三四\n五六", narrow, sheet);
  assert.deepEqual(mixed.lines.map(l => l.text), ["一二三", "四", "五六"]);
  assert.equal(mixed.height, 210);
});

test("English words wrap at spaces and overly long words force-break without looping", () => {
  // 英文窄字符在 Fontsize 70 下约 37.31px，空格约 15.26px
  const narrow = styleAt(2, { ml: 810, mr: 810 });   // 宽 = 300
  const words = layoutBlock("hello world test", narrow, sheet);
  assert.deepEqual(words.lines.map(l => l.text), ["hello", "world", "test"]);

  const tiny = styleAt(2, { ml: 910, mr: 910 });     // 宽 = 100，单个长词放不下
  const broken = layoutBlock("abcdefgh", tiny, sheet);
  assert.ok(broken.lines.length >= 3);
  assert.equal(broken.lines.map(l => l.text).join(""), "abcdefgh");
});

test("closing punctuation avoids starting a wrapped line when an earlier break exists", () => {
  const narrow = styleAt(2, { ml: 890, mr: 890 });   // 宽 = 140，可放 2 个全角字
  const wrapped = layoutBlock("你好。世界", narrow, sheet);
  assert.equal(wrapped.lines[0].text, "你");
  assert.ok(!wrapped.lines[1].text.startsWith("。"), "句号不该落在行首");
});

test("lineSpacing increases spacing between wrapped lines without affecting single lines", () => {
  const single = layoutBlock("单行", styleAt(2, { lineSpacing: 24 }), sheet);
  assert.equal(single.height, 70, "单行高度不受行间距影响");

  const multi = layoutBlock("第一行\n第二行\n第三行", styleAt(2, { lineSpacing: 20 }), sheet);
  assert.equal(multi.lines.length, 3);
  assert.equal(multi.height, 3 * 70 + 2 * 20);
  assert.equal(multi.lines[1].top - multi.lines[0].top, 90);
  assert.equal(multi.lines[2].top - multi.lines[1].top, 90);
  assert.equal(multi.top + multi.height, Y - 30, "底对齐仍紧贴 MarginV");
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

test("a box pushed past the frame edge clamps without shrinking its width", () => {
  const style = styleAt(2, { ml: 100, mr: 100 });
  const block = layoutBlock("靠边", style, sheet);
  const margins = marginsForBlock({ ...block, left: block.left - 900 }, style, sheet);
  assert.equal(margins.marginl, 0, "顶到左边界");
  assert.equal(margins.marginr, 200, "保持原容器宽度（ml+mr 不变）");
  const moved = layoutBlock("靠边", { ...style, ml: margins.marginl, mr: margins.marginr }, sheet);
  assert.equal(moved.width, block.width, "平移贴边后容器宽度不变");
  assert.equal(moved.left, 0);
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
