import { glyphAdvancePx, lineHeightPx } from './metrics.ts';
import type { StyleSheet } from './styles.ts';
import type { AssStyle } from './types.ts';

/**
 * 文本框排版与自动折行。三个调用方共用同一份数学：
 * 1. 普通字幕拼 ASS 时插入 \N（src/subtitles/build.ts），绕开 JASSUB 缺 libunibreak 不折中日文的问题；
 * 2. 逐字特效取每个字的坐标（src/subtitles/build.ts）；
 * 3. 预览里的直接操控取容器框与文字命中区（src/editor/lib/stageHit.ts）。
 * 于是选中框、画面折行、逐字特效与导出压片四处严格一致。
 *
 * 坐标一律是 PlayRes 单位。字宽和行高的口径见 src/subtitles/metrics.ts：Fontsize 描述的是
 * 「asc+desc」而不是 em，早先按 em 估的那版每个字宽出 14%、行距宽出 18%。
 *
 * 已知的近似（覆盖层要按同一口径，别单独「修」）：libass 的位置按 PlayResX→帧宽、
 * PlayResY→帧高各自缩放，而字号只按高度缩。这里把按字号算出的步进当成 PlayRes-X 单位，
 * 只在「帧宽高比 == PlayRes 宽高比」时精确。
 */

export interface Rect { left: number; top: number; width: number; height: number }

export interface LineRect {
  text: string;
  glyphs: string[];
  /** 每个字形的步进宽度，与 glyphs 一一对应 */
  advances: number[];
  left: number;
  top: number;
  width: number;
  height: number;
}

export interface BlockRect {
  /** 文本框容器：[MarginL, PlayResX - MarginR]，高度随折行后总行数自动伸缩 */
  left: number;
  top: number;
  width: number;
  height: number;
  /** 各行文字本身的紧致外接矩形（未选中时的点击命中区） */
  content: Rect;
  lineHeight: number;
  lines: LineRect[];
}

/** 1 = 左 / 中 / 右（Alignment 的横向分组） */
export const hGroupOf = (align: number): 1 | 2 | 3 => ((((align - 1) % 3) + 1) as 1 | 2 | 3);
/** 1 = 下（1-3）、2 = 中（4-6）、3 = 上（7-9） */
export const vGroupOf = (align: number): 1 | 2 | 3 => (Math.ceil(align / 3) as 1 | 2 | 3);

/** 垂直居中时 libass 根本不看 MarginV——拖动要据此锁死 Y 轴 */
export const ignoresMarginV = (align: number): boolean => vGroupOf(align) === 2;

const isSpace = (ch: string) => ch === " " || ch === "\t";
/** 拉丁字母/数字等连续成词字符：自动换行时尽量整词落到下一行 */
const isWordChar = (ch: string) => /[A-Za-z0-9_'\-/.:@#]/.test(ch);
/** 不宜落到行首的常见中英文标点（简易避头） */
const NO_LINE_START = new Set("，。、！？；：）》」』】〕〉”’,.!?;:)]}%…—～~");
/** 不宜留在行尾的开括号（简易避尾） */
const NO_LINE_END = new Set("（《「『【〔〈“‘([{");

const isMidWord = (glyphs: string[], cut: number) =>
  isWordChar(glyphs[cut - 1]) && isWordChar(glyphs[cut]);

const canBreakClean = (glyphs: string[], cut: number) =>
  !isMidWord(glyphs, cut) && !NO_LINE_START.has(glyphs[cut]) && !NO_LINE_END.has(glyphs[cut - 1]);

interface WrappedLine {
  text: string;
  glyphs: string[];
  advances: number[];
  width: number;
}

/** 按容器可用宽度贪心折行（逼近 libass WrapStyle: 1） */
function wrapParagraph(rawLine: string, style: AssStyle, maxWidth: number): WrappedLine[] {
  const glyphs = Array.from(rawLine);
  if (!glyphs.length) return [{ text: "", glyphs: [], advances: [], width: 0 }];
  const advances = glyphs.map(glyph =>
    glyphAdvancePx(style.font, style.size, style.sp, style.scx, glyph));
  const total = advances.reduce((sum, advance) => sum + advance, 0);
  if (total <= maxWidth) return [{ text: rawLine, glyphs, advances, width: total }];

  const out: WrappedLine[] = [];
  let start = 0;
  while (start < glyphs.length) {
    let end = start;
    let width = 0;
    while (end < glyphs.length) {
      const next = width + advances[end];
      if (next > maxWidth && end > start) break;
      width = next;
      end++;
    }
    if (end >= glyphs.length) {
      const lineGlyphs = glyphs.slice(start);
      const lineAdvances = advances.slice(start);
      out.push({ text: lineGlyphs.join(""), glyphs: lineGlyphs, advances: lineAdvances, width });
      break;
    }

    let cut = -1;
    for (let i = end; i > start; i--) {
      if (canBreakClean(glyphs, i)) { cut = i; break; }
    }
    if (cut < 0) {
      for (let i = end; i > start; i--) {
        if (!isMidWord(glyphs, i)) { cut = i; break; }
      }
    }
    if (cut < 0) cut = end;

    let trimEnd = cut;
    while (trimEnd > start && isSpace(glyphs[trimEnd - 1])) trimEnd--;
    const lineGlyphs = glyphs.slice(start, trimEnd);
    const lineAdvances = advances.slice(start, trimEnd);
    const lineWidth = lineAdvances.reduce((sum, advance) => sum + advance, 0);
    out.push({ text: lineGlyphs.join(""), glyphs: lineGlyphs, advances: lineAdvances, width: lineWidth });

    start = cut;
    while (start < glyphs.length && isSpace(glyphs[start])) start++;
  }
  return out;
}

export function layoutBlock(text: string, style: AssStyle, sheet: StyleSheet): BlockRect {
  const boxLeft = style.ml;
  const boxWidth = Math.max(1, sheet.playRes.x - style.ml - style.mr);
  const rawLines = text.replace(/\r/g, "").split("\n");
  const wrapped = rawLines.flatMap(raw => wrapParagraph(raw, style, boxWidth));
  const lineHeight = lineHeightPx(style.size, style.scy);
  const lineSpacing = Math.max(0, style.lineSpacing || 0);
  const lineStep = lineHeight + lineSpacing;
  const blockHeight = Math.max(lineHeight, wrapped.length * lineHeight + Math.max(0, wrapped.length - 1) * lineSpacing);
  const vGroup = vGroupOf(style.align);
  let blockTop = sheet.playRes.y - style.mv - blockHeight;
  if (vGroup === 2) blockTop = (sheet.playRes.y - blockHeight) / 2;
  else if (vGroup === 3) blockTop = style.mv;

  const hGroup = hGroupOf(style.align);
  const lines: LineRect[] = wrapped.map((line, lineIndex) => {
    // libass 是在 [MarginL, PlayResX-MarginR] 之间对齐，不是在整幅画面里对齐
    let left = boxLeft;
    if (hGroup === 2) left = boxLeft + (boxWidth - line.width) / 2;
    else if (hGroup === 3) left = boxLeft + boxWidth - line.width;
    return {
      text: line.text, glyphs: line.glyphs, advances: line.advances,
      left, top: blockTop + lineIndex * lineStep, width: line.width, height: lineHeight,
    };
  });

  const contentLeft = Math.min(...lines.map(l => l.left));
  const contentRight = Math.max(...lines.map(l => l.left + l.width));
  return {
    left: boxLeft,
    top: blockTop,
    width: boxWidth,
    height: blockHeight,
    content: { left: contentLeft, top: blockTop, width: contentRight - contentLeft, height: blockHeight },
    lineHeight,
    lines,
  };
}

/** 三个 Margin 的补丁。索引签名是为了能直接并进 StylePatch */
export interface BlockMargins {
  [field: string]: number;
  marginl: number;
  marginr: number;
  marginv: number;
}

const clamp = (value: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, value));

/**
 * layoutBlock 的反函数：要把文本框放到 want 的位置和宽度，三个 Margin 该写成多少。
 *
 * 在文本框模型下，容器横跨 [MarginL, PlayResX - MarginR]，无论左/中/右对齐口径都统一：
 * - 平移时 want.width 不变，先定整像素宽度再钳制 left，保证 ml + mr 严格守恒、不意外触发换行；
 * - 调宽时 want.left 或 want.width 变化，另一侧边界原地钉死；
 * - 垂直居中（纵向分组 2）时 MarginV 被 libass 忽略，原样返回——调用方应当锁死 Y 轴。
 */
export function marginsForBlock(want: Rect, style: AssStyle, sheet: StyleSheet): BlockMargins {
  const X = sheet.playRes.x, Y = sheet.playRes.y;
  const width = clamp(Math.round(want.width), 1, X);
  const ml = clamp(Math.round(want.left), 0, X - width);
  const mr = clamp(X - ml - width, 0, X - 1);

  const vGroup = vGroupOf(style.align);
  const mv = vGroup === 2 ? style.mv
    : vGroup === 3 ? want.top
      : Y - (want.top + want.height);

  return {
    marginl: ml,
    marginr: mr,
    marginv: clamp(Math.round(mv), 0, Y - 1),
  };
}

/**
 * 一条 lane 在当前样式下会占据的「带」：当前时刻没有可见句时，预览里的选中框画它。
 * 宽度取容器宽度 [MarginL, PlayResX - MarginR]，高度为单行行高。
 */
export function bandBlock(style: AssStyle, sheet: StyleSheet): BlockRect {
  const height = lineHeightPx(style.size, style.scy);
  const left = style.ml;
  const width = Math.max(1, sheet.playRes.x - style.ml - style.mr);
  const vGroup = vGroupOf(style.align);
  const top = vGroup === 2 ? (sheet.playRes.y - height) / 2
    : vGroup === 3 ? style.mv
      : sheet.playRes.y - style.mv - height;
  return {
    left, top, width, height,
    content: { left, top, width, height },
    lineHeight: height,
    lines: [{ text: "", glyphs: [], advances: [], left, top, width, height }],
  };
}

// ── Per-segment placement (`\pos`) ─────────────────────────────

/** A positioned cue's text never shrinks below this share of its requested size */
export const POS_MIN_FONT_SCALE = 0.6;
/** Each shrink step multiplies the font size by this */
const POS_SHRINK_STEP = 0.94;

/** `pos` already converted to PlayRes units */
export interface PlacedPos {
  x: number;
  y: number;
  an: number;
  /** wrap width */
  width: number;
  /** requested font size (ASS Fontsize, before shrinking) */
  size: number;
  /** height the block should fit in; 0 = no limit */
  maxHeight: number;
}

export interface PositionedBlock {
  block: BlockRect;
  /** font size actually used (after shrinking) */
  size: number;
  /** true when even the minimum size did not fit; text overflows */
  overflow: boolean;
}

/** Lay out at one font size, anchored at (place.x, place.y) the way libass anchors `\pos` */
function placeAt(text: string, style: AssStyle, sheet: StyleSheet, place: PlacedPos, size: number): BlockRect {
  // Reuse layoutBlock's wrapping with a box of exactly the wrap width; spacer lines for
  // lineSpacing rely on {\r}, which would undo the \fs override, so positioned text has none
  const X = sheet.playRes.x;
  const width = Math.max(1, Math.min(X, place.width));
  const temp: AssStyle = { ...style, size, lineSpacing: 0, ml: 0, mr: X - width, align: 7, mv: 0 };
  const raw = layoutBlock(text, temp, sheet);
  const contentWidth = Math.max(...raw.lines.map(line => line.width));
  const h = hGroupOf(place.an), v = vGroupOf(place.an);
  const left = place.x - (h === 1 ? 0 : h === 2 ? contentWidth / 2 : contentWidth);
  const top = place.y - (v === 3 ? 0 : v === 2 ? raw.height / 2 : raw.height);
  const lines: LineRect[] = raw.lines.map((line, index) => ({
    ...line,
    left: left + (h === 1 ? 0 : h === 2 ? (contentWidth - line.width) / 2 : contentWidth - line.width),
    top: top + index * raw.lineHeight,
  }));
  const rect = { left, top, width: contentWidth, height: raw.height };
  return { ...rect, content: { ...rect }, lineHeight: raw.lineHeight, lines };
}

/**
 * Layout for a positioned cue: wrap to the requested width; if the block is wider than that
 * (one unbreakable word) or taller than `maxHeight`, shrink the font step by step down to
 * POS_MIN_FONT_SCALE of the requested size, and only then let it overflow.
 */
export function layoutPositioned(text: string, style: AssStyle, sheet: StyleSheet, place: PlacedPos): PositionedBlock {
  const oneLine = lineHeightPx(place.size, style.scy);
  const limit = place.maxHeight > 0 ? Math.max(place.maxHeight, oneLine) : 0;
  const fits = (block: BlockRect) =>
    block.width <= place.width + 0.5 && (!limit || block.height <= limit + 0.5);
  const floor = place.size * POS_MIN_FONT_SCALE;
  let size = place.size;
  for (;;) {
    const block = placeAt(text, style, sheet, place, size);
    if (fits(block)) return { block, size, overflow: false };
    if (size <= floor) return { block, size, overflow: true };
    size = Math.max(floor, size * POS_SHRINK_STEP);
  }
}
