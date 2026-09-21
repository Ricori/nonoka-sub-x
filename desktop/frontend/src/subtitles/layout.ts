import { glyphAdvancePx, lineHeightPx } from './metrics.ts';
import type { StyleSheet } from './styles.ts';
import type { AssStyle } from './types.ts';

/**
 * 复算 libass 的整块排版。两个调用方：逐字特效要每个字的坐标（src/subtitles/build.ts），
 * 预览里的直接操控要整块的外接矩形（src/editor/lib/stageHit.ts）。两边必须是同一份数学，
 * 否则拖的框和画面上的字对不上。
 *
 * 坐标一律是 PlayRes 单位。字宽和行高的口径见 src/subtitles/metrics.ts：Fontsize 描述的是
 * 「asc+desc」而不是 em，早先按 em 估的那版每个字宽出 14%、行距宽出 18%。
 *
 * 已知的近似（覆盖层要按同一口径，别单独「修」）：libass 的位置按 PlayResX→帧宽、
 * PlayResY→帧高各自缩放，而字号只按高度缩。这里把按字号算出的步进当成 PlayRes-X 单位，
 * 只在「帧宽高比 == PlayRes 宽高比」时精确。
 */

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
  /** 各行的并集 */
  left: number;
  top: number;
  width: number;
  height: number;
  lineHeight: number;
  lines: LineRect[];
}

/** 1 = 左 / 中 / 右（Alignment 的横向分组） */
export const hGroupOf = (align: number): 1 | 2 | 3 => ((((align - 1) % 3) + 1) as 1 | 2 | 3);
/** 1 = 下（1-3）、2 = 中（4-6）、3 = 上（7-9） */
export const vGroupOf = (align: number): 1 | 2 | 3 => (Math.ceil(align / 3) as 1 | 2 | 3);

/** 垂直居中时 libass 根本不看 MarginV——拖动要据此锁死 Y 轴 */
export const ignoresMarginV = (align: number): boolean => vGroupOf(align) === 2;

export function layoutBlock(text: string, style: AssStyle, sheet: StyleSheet): BlockRect {
  const rawLines = text.replace(/\r/g, "").split("\n");
  const lineHeight = lineHeightPx(style.size, style.scy);
  const blockHeight = Math.max(lineHeight, rawLines.length * lineHeight);
  const vGroup = vGroupOf(style.align);
  let blockTop = sheet.playRes.y - style.mv - blockHeight;
  if (vGroup === 2) blockTop = (sheet.playRes.y - blockHeight) / 2;
  else if (vGroup === 3) blockTop = style.mv;

  const hGroup = hGroupOf(style.align);
  const lines: LineRect[] = rawLines.map((line, lineIndex) => {
    const glyphs = Array.from(line);
    const advances = glyphs.map(glyph =>
      glyphAdvancePx(style.font, style.size, style.sp, style.scx, glyph));
    const width = advances.reduce((sum, advance) => sum + advance, 0);
    // libass 是在 [MarginL, PlayResX-MarginR] 之间居中，不是在整幅画面里居中
    let left = style.ml;
    if (hGroup === 2) left = style.ml + (sheet.playRes.x - style.ml - style.mr - width) / 2;
    else if (hGroup === 3) left = sheet.playRes.x - style.mr - width;
    return { text: line, glyphs, advances, left, top: blockTop + lineIndex * lineHeight, width, height: lineHeight };
  });

  const left = Math.min(...lines.map(l => l.left));
  const right = Math.max(...lines.map(l => l.left + l.width));
  return { left, top: blockTop, width: right - left, height: blockHeight, lineHeight, lines };
}

export interface Rect { left: number; top: number; width: number; height: number }

/** 三个 Margin 的补丁。索引签名是为了能直接并进 StylePatch */
export interface BlockMargins {
  [field: string]: number;
  marginl: number;
  marginr: number;
  marginv: number;
}

const clamp = (value: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, value));

/**
 * layoutBlock 的反函数：要把整块放到 want 的位置，三个 Margin 该写成多少。
 *
 * 居中（横向分组 2）时 libass 在 [ml, X-mr] 之间居中，块心 = (ml + X - mr)/2，于是水平
 * 偏移只能靠 ml 与 mr 的**差值**表达。这里刻意保住 ml+mr 这个「留白总量」不变——可用宽度
 * 不变，手感才是「自由移动」而不是「被挤压」。之所以安全，是因为 assHeadOf 写的是
 * WrapStyle: 2（关闭自动换行）：改左右边距不会引发重排，块宽不会跟着变。
 *
 * 垂直居中（纵向分组 2）时 MarginV 被忽略，原样返回——调用方应当锁死 Y 轴。
 */
export function marginsForBlock(want: Rect, style: AssStyle, sheet: StyleSheet): BlockMargins {
  const X = sheet.playRes.x, Y = sheet.playRes.y;
  let ml = style.ml, mr = style.mr;

  const hGroup = hGroupOf(style.align);
  if (hGroup === 1) {
    ml = want.left;
  } else if (hGroup === 3) {
    mr = X - (want.left + want.width);
  } else {
    const wanted = 2 * (want.left + want.width / 2) - X;   // 必须满足 ml - mr = wanted
    const gap = style.ml + style.mr;                        // 留白总量保持不变
    ml = (gap + wanted) / 2;
    mr = (gap - wanted) / 2;
    if (ml < 0) { ml = 0; mr = -wanted; }                   // 贴左边界，靠 mr 单独维持差值
    if (mr < 0) { mr = 0; ml = wanted; }
  }

  const vGroup = vGroupOf(style.align);
  const mv = vGroup === 2 ? style.mv
    : vGroup === 3 ? want.top
      : Y - (want.top + want.height);

  return {
    marginl: clamp(Math.round(ml), 0, X - 1),
    marginr: clamp(Math.round(mr), 0, X - 1),
    marginv: clamp(Math.round(mv), 0, Y - 1),
  };
}

/**
 * 一条 lane 在当前样式下会占据的「带」：当前时刻没有可见句时，预览里的选中框画它。
 * 宽度取可用带宽而不是某句的实际宽度——没有句子就没有文字宽度可言，
 * 而这条带正是下一句会落进去的地方。
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
    left, top, width, height, lineHeight: height,
    lines: [{ text: "", glyphs: [], advances: [], left, top, width, height }],
  };
}
