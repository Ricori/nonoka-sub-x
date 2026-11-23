// 字形度量：给「按字拆事件」的特效算排版坐标用。
//
// libass 不暴露排版结果，只能自己复算一遍。关键是它怎么理解 ASS 的 Fontsize：
// 用的是 FreeType 的 FT_SIZE_REQUEST_TYPE_REAL_DIM，把字体 hhea 的
// ascender-descender **整体**缩到 Fontsize，而不是把 em 缩到 Fontsize。
// 于是 Fontsize 70 的方正准圆（upem 256、asc 230、desc -63）实际 em 只有
// 70 × 256/293 ≈ 61.2px，全角字步进也是 61.2 而不是 70。早先按「1 em = Fontsize」
// 估的那版每个字宽出 14%，整行被撑开，就是这么来的。
//
// canvas 正好能一次问出这两个量：measureText(字).width 是 em 口径的步进，
// fontBoundingBoxAscent+Descent 是同一口径的 asc+desc。两者相除就是
// 「步进 ÷ Fontsize」，与探针字号无关，也天然跟着字形实际落到的那个字体走
// （回退字体的度量是回退字体自己的，和 libass 逐 face 定尺寸的口径一致）。

/** libass 的行高：REAL_DIM 保证 asc+desc == Fontsize，所以行距就是字号本身 */
export const lineHeightPx = (size: number, scaleY: number): number => size * scaleY / 100;

/** 量不到字体时的兜底比例（步进 ÷ Fontsize），取自随包的方正准圆 */
const FALLBACK_RATIO = { space: .218, narrow: .533, wide: .874 };

const fallbackRatio = (glyph: string): number =>
  /^\s$/u.test(glyph) ? FALLBACK_RATIO.space
    : /^[\x00-\xff]$/u.test(glyph) ? FALLBACK_RATIO.narrow
      : FALLBACK_RATIO.wide;

const cssFamily = (name: string) => `"${name.replace(/["\\]/g, "\\$&")}"`;
const familyKey = (name: string) => name.trim().toLowerCase();

/** 探针字号取大一点：measureText 的返回值有取整，字号小了比例会抖 */
const PROBE_SIZE = 400;

const ratios = new Map<string, number>();
const measured = new Set<string>();
const listeners = new Set<() => void>();

let context: CanvasRenderingContext2D | null | undefined;

function probeContext(): CanvasRenderingContext2D | null {
  if (context === undefined) {
    context = typeof document === "undefined" ? null
      : document.createElement("canvas").getContext("2d");
  }
  return context;
}

/**
 * 量一个字形的「步进 ÷ Fontsize」。字体还没确认可用时一律走兜底比例——
 * 半路量到回退字体会让同一份文档在字体加载前后排出两种版本。
 */
function ratioOf(font: string, glyph: string): number {
  const key = familyKey(font);
  if (!measured.has(key)) return fallbackRatio(glyph);
  const cacheKey = `${key}\u0000${glyph}`;
  const cached = ratios.get(cacheKey);
  if (cached !== undefined) return cached;
  const value = measureRatio(font, glyph) ?? fallbackRatio(glyph);
  ratios.set(cacheKey, value);
  return value;
}

function measureRatio(font: string, glyph: string): number | null {
  const ctx = probeContext();
  if (!ctx) return null;
  ctx.font = `${PROBE_SIZE}px ${cssFamily(font)}`;
  const metrics = ctx.measureText(glyph);
  const height = metrics.fontBoundingBoxAscent + metrics.fontBoundingBoxDescent;
  if (!(height > 0) || !Number.isFinite(metrics.width)) return null;
  return metrics.width / height;
}

/**
 * 一个字形在该样式下的步进宽度（px），口径与 libass 一致：
 * 字体步进 + Spacing，整体再乘 ScaleX。
 */
export function glyphAdvancePx(font: string, size: number, spacing: number,
  scaleX: number, glyph: string): number {
  return Math.max(1, (ratioOf(font, glyph) * size + spacing) * scaleX / 100);
}

/** 已经确认可用、正在按实测度量排版的字体族 */
export const fontMetricsReady = (font: string): boolean => measured.has(familyKey(font));

export function onFontMetricsChange(listener: () => void): () => void {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

/**
 * 确认这些字体可以拿来量：随包字体先注册成 FontFace（只喂给了 libass，
 * 文档里并没有 @font-face，不注册的话 canvas 量到的是系统回退字体），
 * 系统装的字体则先 load 一遍再量。有新字体就绪就返回 true，调用方据此重排。
 */
export async function loadFontMetrics(fonts: Iterable<string>,
  bundled?: (font: string) => string | null): Promise<boolean> {
  const ctx = probeContext();
  if (!ctx || typeof document === "undefined" || !document.fonts) return false;
  const pending = [...new Set([...fonts].map(name => name.trim()).filter(Boolean))]
    .filter(name => !measured.has(familyKey(name)));
  if (!pending.length) return false;
  await Promise.all(pending.map(async name => {
    const url = bundled?.(name);
    if (url) {
      try {
        const face = new FontFace(name, `url(${JSON.stringify(url)})`);
        document.fonts.add(await face.load());
      } catch { /* 注册失败就退回系统同名字体，量不到再退兜底比例 */ }
    }
    await document.fonts.load(`40px ${cssFamily(name)}`, "字A").catch(() => undefined);
  }));
  let changed = false;
  for (const name of pending) {
    if (measureRatio(name, "字") === null) continue;
    measured.add(familyKey(name));
    changed = true;
  }
  if (changed) for (const listener of listeners) listener();
  return changed;
}

/**
 * Where a CJK glyph's ink sits inside a libass line (all as fractions of Fontsize, i.e. of
 * asc+desc): `ink` is the ink height, `top` the gap from the line top to the ink top,
 * `bottom` the gap from the ink bottom to the line bottom. Screen text uses it to make the
 * translation's ink land on the original's: 思源黑体 Heavy inks only 67% of its line and
 * starts 20% below the line top, so "Fontsize = original glyph height" draws a third too
 * small and visibly low.
 */
export interface InkMetrics { ink: number; top: number; bottom: number }

/** Bundled 方正准圆 (upem 256, asc 230, desc -63), measured from the font file */
const FALLBACK_INK: InkMetrics = { ink: .843, top: .051, bottom: .106 };
const INK_PROBE = "国字語あ";

const inks = new Map<string, InkMetrics>();

export function inkMetricsOf(font: string): InkMetrics {
  const key = familyKey(font);
  if (!measured.has(key)) return FALLBACK_INK;
  const cached = inks.get(key);
  if (cached) return cached;
  const value = measureInk(font) ?? FALLBACK_INK;
  inks.set(key, value);
  return value;
}

function measureInk(font: string): InkMetrics | null {
  const ctx = probeContext();
  if (!ctx) return null;
  ctx.font = `${PROBE_SIZE}px ${cssFamily(font)}`;
  let ascent = 0, descent = -Infinity, lineAscent = 0, lineDescent = 0;
  for (const glyph of INK_PROBE) {
    const m = ctx.measureText(glyph);
    if (!Number.isFinite(m.actualBoundingBoxAscent) || !Number.isFinite(m.fontBoundingBoxAscent)) return null;
    ascent = Math.max(ascent, m.actualBoundingBoxAscent);
    descent = Math.max(descent, m.actualBoundingBoxDescent);
    lineAscent = m.fontBoundingBoxAscent;
    lineDescent = m.fontBoundingBoxDescent;
  }
  const line = lineAscent + lineDescent;
  const ink = ascent + descent;
  if (!(line > 0) || !(ink > 0) || ink > line * 1.2) return null;
  return { ink: ink / line, top: (lineAscent - ascent) / line, bottom: (lineDescent - descent) / line };
}

/** 测试用：清空实测缓存，回到兜底比例 */
export function resetFontMetrics() {
  ratios.clear();
  inks.clear();
  measured.clear();
}
