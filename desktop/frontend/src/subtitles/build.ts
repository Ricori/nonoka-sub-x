import { ASS_EVENTS_HEAD } from './constants.ts';
import { DEFAULT_EFFECT_TRACK_ID, resolveLaneEffects } from './effects.ts';
import { assColorFromHex, assNm, assSec, assTs, assTx, srtTs } from './format.ts';
import { srcToOut } from './pieces.ts';
import type { Piece } from './pieces.ts';
import { karaokeTimeline } from './karaoke.ts';
import { layoutBlock, layoutPositioned } from './layout.ts';
import { inkMetricsOf } from './metrics.ts';
import { anchorOnBox } from './position.ts';
import type { BlockRect, PlacedPos } from './layout.ts';
import { assHeadOf, resolveStyleIn } from './styles.ts';
import type { StyleSheet } from './styles.ts';
import type {
  Align, AssStyle, CoverBox, Lang, LaneMeta, SubtitleEffectBinding, SubtitleSegment, SubtitleSource,
} from './types.ts';

/**
 * 文档 → ASS/SRT 的拼装管线。无特效时继续与旧服务端导出口径一致；特效绑定则只在
 * 这一条公共管线里展开。编辑器拿 docStore 的就地可变文档喂进来，
 * 插件宿主拿服务端读回的 EditDocument 喂进来，于是插件拿到的字幕与编辑器导出的
 * 那份、以及 libass 正在预览的那份，是同一条代码路径出的结果。
 */
export interface OutputLine {
  arr: SubtitleSegment[];
  lang: Lang;
  style: string;
  name: string;
  meta: LaneMeta;
  trackId: string;
  effects: SubtitleEffectBinding[];
}

/** ti：-1 = 默认轨，否则自定义轨下标 */
export function trackNameOf(source: SubtitleSource, ti: number): string {
  return ti < 0
    ? (source.trackMeta?.name || "默认轨")
    : (source.tracks[ti]?.name || ("轨道 " + (ti + 1)));
}

/**
 * 输出线 = 堆叠优先级：默认轨 译文→原文 最贴边，再各自定义轨 译文→原文 依次向外。
 * 出不出只看 hidden；没绑样式或绑了本机没有的样式都回退 origin/cn。
 */
export function outputLinesOf(source: SubtitleSource, sheet: StyleSheet): OutputLine[] {
  const out: OutputLine[] = [];
  const add = (arr: SubtitleSegment[], lang: Lang, meta: LaneMeta, name: string, trackId: string) => {
    out.push({
      arr, lang, meta, trackId,
      style: resolveStyleIn(sheet, meta.style || "", lang), name,
      effects: resolveLaneEffects(source.effects, trackId, lang),
    });
  };
  const meta = source.trackMeta;
  if (meta) {
    if (!meta.zh.hidden) add(source.segs, "zh", meta.zh, trackNameOf(source, -1), DEFAULT_EFFECT_TRACK_ID);
    if (!meta.ja.hidden) add(source.segs, "ja", meta.ja, trackNameOf(source, -1), DEFAULT_EFFECT_TRACK_ID);
  }
  source.tracks.forEach((tr, ti) => {
    const trackId = tr.id || `track-${ti + 1}`;
    if (!tr.zh.hidden) add(tr.segs, "zh", tr.zh, trackNameOf(source, ti), trackId);
    if (!tr.ja.hidden) add(tr.segs, "ja", tr.ja, trackNameOf(source, ti), trackId);
  });
  return out;
}

const param = (binding: SubtitleEffectBinding, key: string, fallback: number): number => {
  const value = Number(binding.params[key]);
  return Number.isFinite(value) ? value : fallback;
};

/** 颜色参数 → `\1c&HBBGGRR&` 这样的覆写；留空（跟随样式）时返回空串，不写标签 */
const colorParam = (binding: SubtitleEffectBinding, key: string, tag: string): string => {
  const raw = binding.params[key];
  const color = assColorFromHex(typeof raw === "string" ? raw : "");
  return color ? `\\${tag}${color}` : "";
};

function transformTag(line: OutputLine): string {
  const fade = line.effects.find(effect => effect.templateId === "fade");
  if (!fade) return "";
  const fadeIn = Math.max(0, Math.round(param(fade, "inMs", 200)));
  const fadeOut = Math.max(0, Math.round(param(fade, "outMs", 200)));
  return fadeIn || fadeOut ? `{\\fad(${fadeIn},${fadeOut})}` : "";
}

interface BuiltEvent { layer: number; t0: number; t1: number; text: string }

const rint = (value: number) => Math.round(value);

function hashSeed(value: string): number {
  let hash = 2166136261;
  for (let index = 0; index < value.length; index++) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return hash >>> 0;
}

function seededRandom(seed: number): () => number {
  let state = seed || 0x6d2b79f5;
  return () => {
    state += 0x6d2b79f5;
    let value = state;
    value = Math.imul(value ^ value >>> 15, value | 1);
    value ^= value + Math.imul(value ^ value >>> 7, value | 61);
    return ((value ^ value >>> 14) >>> 0) / 4294967296;
  };
}

interface PositionedGlyph { glyph: string; x: number; y: number; order: number; total: number }

/**
 * libass 不暴露排版坐标，逐字特效只能自己复算一遍。整块的定位数学在
 * src/subtitles/layout.ts —— 预览里的直接操控要用同一份，两边不能各算各的。
 *
 * 返回顺序与 karaokeGlyphs 完全一致（都只数可见字形），于是 order 可以直接索引 K 轴。
 */
function positionedGlyphs(text: string, style: AssStyle, sheet: StyleSheet): PositionedGlyph[] {
  const block = layoutBlock(text, style, sheet);
  const visible = (glyph: string) => !/^\s$/u.test(glyph);
  const visibleTotal = block.lines.reduce((count, line) =>
    count + line.glyphs.filter(visible).length, 0);
  const result: PositionedGlyph[] = [];
  let order = 0;
  for (const line of block.lines) {
    let cursor = line.left;
    line.glyphs.forEach((glyph, index) => {
      const width = line.advances[index];
      if (visible(glyph)) {
        result.push({
          glyph,
          x: cursor + width / 2,
          y: line.top + block.lineHeight / 2,
          order: order++,
          total: visibleTotal,
        });
      }
      cursor += width;
    });
  }
  return result;
}

/** 逐字特效共用的准备：字形坐标 + 每个字自己的起止时间 */
function glyphTimeline(line: OutputLine, segment: SubtitleSegment, style: AssStyle, sheet: StyleSheet) {
  const sourceText = (segment[line.lang] || "").trim();
  const glyphs = positionedGlyphs(sourceText, style, sheet);
  if (!glyphs.length) return null;
  // K 轴只描述原文：译文轴（以及原文改过、K 轴对不上了）一律退回按字数均分
  const stored = line.lang === "ja" ? segment.k : undefined;
  return { sourceText, glyphs, timeline: karaokeTimeline(sourceText, segment.t0, segment.t1, stored) };
}

const PARTICLE_SHAPE = "m 0 0 l 7 8 l 0 16 l 8 9 l 16 16 l 9 8 l 16 0 l 8 7";

/** 每字每粒子一条稳定随机序列：同一份文档反复导出得到同一份画面 */
const particleRandom = (line: OutputLine, segment: SubtitleSegment, text: string,
  order: number, index: number) =>
  seededRandom(hashSeed(`${line.trackId}|${line.lang}|${segment.t0}|${text}|${order}|${index}`));

function particleEvents(line: OutputLine, segment: SubtitleSegment, style: AssStyle,
  sheet: StyleSheet, binding: SubtitleEffectBinding): BuiltEvent[] {
  const prepared = glyphTimeline(line, segment, style, sheet);
  if (!prepared) return [];
  const { sourceText, glyphs } = prepared;
  const cueDuration = Math.max(.01, segment.t1 - segment.t0);
  const staggerMs = Math.max(0, param(binding, "staggerMs", 80));
  const enterMs = Math.max(50, param(binding, "enterMs", 300));
  const scaleFrom = Math.max(10, param(binding, "scaleFrom", 70));
  const count = Math.min(25, Math.max(0, Math.round(param(binding, "particleCount", 10))));
  const particleDurationMs = Math.max(100, param(binding, "particleDurationMs", 900));
  const scatter = Math.max(0, param(binding, "scatterPx", 180));
  const events: BuiltEvent[] = [];

  for (const glyph of glyphs) {
    const requestedOffset = glyph.order * staggerMs / 1000;
    const distributedOffset = cueDuration * glyph.order / Math.max(1, glyph.total);
    const start = segment.t0 + Math.min(requestedOffset, distributedOffset);
    const remainingMs = Math.max(10, (segment.t1 - start) * 1000);
    const actualEnter = Math.min(enterMs, remainingMs);
    const startScx = rint(style.scx * scaleFrom / 100);
    const startScy = rint(style.scy * scaleFrom / 100);
    const mainTag = `{\\an5\\pos(${rint(glyph.x)},${rint(glyph.y)})`
      + `\\fscx${startScx}\\fscy${startScy}\\blur4\\alpha&H80&`
      + `\\t(0,${rint(actualEnter)},\\fscx${rint(style.scx)}\\fscy${rint(style.scy)}\\blur0\\alpha&H00&)}`;
    events.push({ layer: 2, t0: start, t1: segment.t1, text: transformTag(line) + mainTag + assTx(glyph.glyph) });

    const particleEnd = Math.min(segment.t1, start + particleDurationMs / 1000);
    const actualParticleMs = Math.max(10, (particleEnd - start) * 1000);
    for (let index = 0; index < count; index++) {
      const random = particleRandom(line, segment, sourceText, glyph.order, index);
      const angle = random() * Math.PI * 2;
      const distance = scatter * (.35 + random() * .65);
      const dx = Math.cos(angle) * distance;
      const dy = Math.sin(angle) * distance * .55;
      const rotation = rint(random() * 720 - 360);
      const targetRotation = rotation + rint(random() * 1440 - 720);
      const particleScale = rint(35 + random() * 45);
      const particleTag = `{\\an5\\move(${rint(glyph.x)},${rint(glyph.y)},${rint(glyph.x + dx)},${rint(glyph.y + dy)},0,${rint(actualParticleMs)})`
        + `\\bord1\\shad0\\blur2\\fscx${particleScale}\\fscy${particleScale}\\frz${rotation}`
        + `\\t(0,${rint(actualParticleMs)},\\fscx10\\fscy10\\frz${targetRotation})`
        + `\\fad(0,${rint(Math.min(250, actualParticleMs))})\\p1}m 0 0 l 4 5 l 0 10 l 5 5 l 10 10 l 6 5 l 10 0 l 5 4`;
      events.push({ layer: 1, t0: start, t1: particleEnd, text: particleTag });
    }
  }
  return events;
}

/**
 * K 轴逐字粒子。照着 Aegisub 的 Lullamoon 模板那套做法来：
 *
 * - 整行在句首**之前**逐字淡入（`50*(syl.i-$syln)`），到自己的音节才脉冲放大，
 *   音节结束后再停留一小会儿淡出。所以整行一开始就读得到，不是一个个蹦出来。
 * - 碎片是**保持大小**的星形，不是缩成一点的尘：起始 ≈90%、终点 ≈粒子大小，
 *   慢出（accel 0.2）地飘散。缩到十几个百分点会让它们直接消失，这是先前那版
 *   看着「没有星星」的原因。
 * - 同一个字的碎片错峰生成、但**一起**在「音节结束 + 粒子余生」处散尽，
 *   于是长音节的碎片自然飘得更久。
 */
function karaokeParticleEvents(line: OutputLine, segment: SubtitleSegment, style: AssStyle,
  sheet: StyleSheet, binding: SubtitleEffectBinding): BuiltEvent[] {
  const prepared = glyphTimeline(line, segment, style, sheet);
  if (!prepared) return [];
  const { sourceText, glyphs, timeline } = prepared;
  const entryStagger = Math.max(0, param(binding, "entryStaggerMs", 50)) / 1000;
  const fadeInMs = Math.max(0, param(binding, "fadeInMs", 300));
  const popScale = Math.max(100, param(binding, "popScale", 120));
  // 单程涨/落的上限。0 = 跟着音节长短走（Aegisub 模板就是 line.duration/2），但那份
  // 模板配的是手打 K 轴；编辑器这边的 K 轴来自词级时间戳或整句均分，明显更粗，
  // 照搬会让每个字都以同一个拖沓的速度涨落，所以默认压在 280ms
  const popCap = Math.max(0, param(binding, "popMs", 280));
  const hold = Math.max(0, param(binding, "holdMs", 400)) / 1000;
  const count = Math.min(25, Math.max(0, Math.round(param(binding, "particleCount", 20))));
  const spawnMs = Math.max(0, param(binding, "spawnMs", 400));
  const lifeMs = Math.max(100, param(binding, "particleLifeMs", 1500));
  const particleScale = Math.max(20, param(binding, "particleScale", 110));
  const spread = Math.max(0, param(binding, "spreadPx", 300));
  const drift = Math.max(0, param(binding, "driftPx", 60));
  // 落回原大小的同时换色（模板的 \1c&HDCC8AE&\3c&HFFFFFF&）；留空则不写标签，保持样式色
  const swap = colorParam(binding, "swapPrimary", "1c") + colorParam(binding, "swapOutline", "3c");
  const particleColor = colorParam(binding, "particleOutline", "3c");
  const events: BuiltEvent[] = [];

  for (const glyph of glyphs) {
    const unit = timeline.units[glyph.order];
    if (!unit) continue;
    // 靠前的字先入场；最后一个字正好在句首出现
    const appear = Math.max(0, segment.t0 - entryStagger * (glyph.total - 1 - glyph.order));
    const end = Math.max(unit.t1 + hold, appear + .05);
    const popAt = rint((unit.t0 - appear) * 1000);
    const popFull = Math.max(2, rint((end - unit.t0) * 1000));
    const popHalf = popCap ? Math.min(rint(popCap), rint(popFull / 2)) : rint(popFull / 2);
    const enter = Math.min(rint(fadeInMs), Math.max(0, popAt));
    // 入场/退场用 \alpha 而不是 \fad：\fad 会盖掉「渐入渐出」绑定加在整行上的那个
    const mainTag = `{\\an5\\pos(${rint(glyph.x)},${rint(glyph.y)})\\shad0\\blur4`
      + (enter > 0 ? `\\alpha&HFF&\\t(0,${enter},\\alpha&H00&)` : "")
      + `\\t(${popAt},${popAt + popHalf},\\fscx${rint(style.scx * popScale / 100)}`
      + `\\fscy${rint(style.scy * popScale / 100)}\\blur6)`
      + `\\t(${popAt + popHalf},${popAt + popHalf * 2},\\fscx${rint(style.scx)}\\fscy${rint(style.scy)}\\blur4${swap})}`;
    events.push({ layer: 2, t0: appear, t1: end, text: transformTag(line) + mainTag + assTx(glyph.glyph) });

    const burnOut = unit.t1 + lifeMs / 1000;
    for (let index = 0; index < count; index++) {
      const random = particleRandom(line, segment, sourceText, glyph.order, index);
      const spawn = unit.t0 + spawnMs * index / Math.max(1, count - 1) / 1000;
      const life = Math.max(100, (burnOut - spawn) * 1000);
      // 单向：模板是 $center - math.random(300)，碎片只往一侧甩，像被风带走
      const dx = -random() * spread;
      const dy = (random() * 2 - 1) * drift;
      const from = Math.max(10, rint(particleScale * .9));
      const to = Math.max(10, rint(particleScale - index * 2));
      const spin = () => rint(random() * 360);
      const particleTag = `{\\an5\\bord1\\shad0\\blur4${particleColor}\\3a&HAA&`
        + `\\frz${spin()}\\frx${spin()}\\fry${spin()}`
        + `\\move(${rint(glyph.x)},${rint(glyph.y + 5)},${rint(glyph.x + dx)},${rint(glyph.y + dy)},0,${rint(life)})`
        + `\\fscx${from}\\fscy${from}`
        + `\\t(0,${rint(life)},0.2,\\fscx${to}\\fscy${to}`
        + `\\frx${rint(random() * 9000 - 4500)}\\fry${rint(random() * 12000 - 6000)}\\frz${rint(random() * 9000 - 4500)})`
        + `\\fad(0,200)\\p1}${PARTICLE_SHAPE}`;
      events.push({ layer: 1, t0: spawn, t1: burnOut, text: particleTag });
    }
  }
  return events;
}

/** 生成型模板 → 事件展开器 */
const GENERATORS: Record<string, (line: OutputLine, segment: SubtitleSegment, style: AssStyle,
  sheet: StyleSheet, binding: SubtitleEffectBinding) => BuiltEvent[]> = {
  "character-particle": particleEvents,
  "karaoke-particle": karaokeParticleEvents,
};

/** 命中的第一个生成型绑定（模板表里的顺序），没有就照常出整句 */
export const generatorOf = (line: OutputLine): SubtitleEffectBinding | undefined =>
  line.effects.find(effect => GENERATORS[effect.templateId]);

/**
 * 有绑定、但样式表里查不到的样式名。轨道绑定的会回退到 origin/cn，句子自己绑的
 * 会回退到轨道样式——两种都照常出图，只是换了副样子。
 */
export function unknownStylesOf(source: SubtitleSource, sheet: StyleSheet): string[] {
  const miss = new Set<string>();
  const chk = (lane: LaneMeta | undefined) => {
    if (lane && lane.style && !sheet.styleMap[lane.style]) miss.add(lane.style);
  };
  const chkSegs = (segs: SubtitleSegment[]) => {
    for (const s of segs) for (const name of Object.values(s.style ?? {}))
      if (name && !sheet.styleMap[name]) miss.add(name);
  };
  if (source.trackMeta) { chk(source.trackMeta.zh); chk(source.trackMeta.ja); }
  chkSegs(source.segs);
  for (const tr of source.tracks) { chk(tr.zh); chk(tr.ja); chkSegs(tr.segs); }
  return [...miss];
}

/** 一句在某条线上实际用的样式：句子自己绑了、且样式表里有，就用它；否则跟随轨道 */
export const cueStyleIn = (sheet: StyleSheet, segment: SubtitleSegment, line: OutputLine): string => {
  const own = segment.style?.[line.lang];
  return own && sheet.styleMap[own] ? own : line.style;
};

/** 会真的出现在画面上的样式名（轨道的 + 句子自己绑的），查缺字、量度量用 */
export function usedStylesOf(source: SubtitleSource, sheet: StyleSheet): Set<string> {
  const names = new Set<string>();
  for (const line of outputLinesOf(source, sheet)) {
    names.add(line.style);
    for (const s of line.arr) if ((s[line.lang] || "").trim()) names.add(cueStyleIn(sheet, s, line));
  }
  return names;
}

export interface BuiltCue {
  segment: SubtitleSegment;
  t0: number;
  t1: number;
  text: string;
  /** 这一句实际用的样式（见 cueStyleIn） */
  style: string;
}

/**
 * 一条输出线上真正会出图的那些句，按时间排好、重叠钳好。
 *
 * 预览里的命中测试必须复用它：钳重叠这一步会改出点，自己再算一遍的话，边界处点到的
 * 会是另一句。
 */
export function cuesOf(line: OutputLine, sheet: StyleSheet): BuiltCue[] {
  // 组内按时间排：全片一起排会让晚开口的高优先级线被挤走
  const cues = line.arr.filter(s => (s[line.lang] || "").trim())
    .map(s => ({
      segment: s, t0: s.t0, t1: s.t1, text: transformTag(line) + assTx(s[line.lang]),
      style: cueStyleIn(sheet, s, line),
    }))
    .sort((a, b) => a.t0 - b.t0 || a.t1 - b.t1);
  // 同一条线内相邻句的毫秒级重叠（ASR 数据自带）会被当成碰撞，
  // 把后一句整句顶离贴边位——前句出点一律钳到后句入点。
  // Positioned cues are left alone: on-screen text overlaps on purpose and \pos never collides
  let previous: BuiltCue | null = null;
  for (const cue of cues) {
    if (cue.segment.pos) continue;
    if (previous && previous.t1 > cue.t0) previous.t1 = cue.t0;
    previous = cue;
  }
  return cues;
}

// ── Per-segment placement ──────────────────────────────────────

/** A positioned cue, laid out in PlayRes units: what `\pos` will draw and where */
export interface PositionedCue {
  block: BlockRect;
  /** font size actually written as `\fs` (may be shrunk to fit) */
  size: number;
  overflow: boolean;
  /** `\pos` point, PlayRes units */
  x: number;
  y: number;
  an: Align;
}

/**
 * ASS Fontsize whose CJK ink is `fs` (fraction of frame height) tall in this style's font, at
 * the style's own 100% scale: ScaleX/ScaleY then size positioned text up or down as a whole.
 */
export function fontSizeForFs(fs: number, style: AssStyle, sheet: StyleSheet): number {
  return fs * sheet.playRes.y / inkMetricsOf(style.font).ink;
}

/**
 * `pos` (frame fractions) → PlayRes placement request for this style. `pos` describes the
 * original text's ink (anchor point, glyph height, line pitch) while libass places line boxes,
 * so the anchor moves by the gap between line box and ink: the translation's glyphs land where
 * the original's were, at the same height. The style's ScaleX/ScaleY (the style panel's 大小)
 * grow or shrink that as a whole, room to wrap and fit included.
 */
export function placedPosOf(segment: SubtitleSegment, style: AssStyle, sheet: StyleSheet): PlacedPos | null {
  const pos = segment.pos;
  if (!pos) return null;
  const { x: X, y: Y } = sheet.playRes;
  const box = segment.cover?.box;
  const sx = (style.scx || 100) / 100, sy = (style.scy || 100) / 100;
  const size = pos.fs ? fontSizeForFs(pos.fs, style, sheet) : style.size;
  const line = size * sy;
  const ink = inkMetricsOf(style.font);
  const v = Math.ceil(pos.an / 3);
  const shift = v === 3 ? -ink.top : v === 1 ? ink.bottom : (ink.bottom - ink.top) / 2;
  const step = pos.lh ? pos.lh * Y * sy : 0;
  return {
    x: pos.x * X,
    y: pos.y * Y + shift * line,
    an: pos.an,
    width: pos.w ? pos.w * X * sx : Math.max(1, X - style.ml - style.mr),
    size,
    // the box bounds ink; the line boxes around it stick out by the font's top/bottom gaps
    maxHeight: box ? (box[3] - box[1]) * Y * sy + (ink.top + ink.bottom) * line : 0,
    ...(step ? { step } : {}),
  };
}

function layoutCue(segment: SubtitleSegment, lang: Lang, styleName: string, sheet: StyleSheet) {
  const style = sheet.styleMap[styleName];
  const text = (segment[lang] || "").trim();
  const place = style ? placedPosOf(segment, style, sheet) : null;
  if (!style || !place || !text) return null;
  return { place, laid: layoutPositioned(text, style, sheet, place) };
}

/**
 * Where a positioned cue lands. `pos` belongs to the segment, so both lanes share it; when the
 * translation lane of the same track is also showing, the source text sits right above it.
 * `lines` is the full outputLinesOf result (used to find that sibling lane).
 */
export function positionedCueOf(segment: SubtitleSegment, line: OutputLine,
  lines: readonly OutputLine[], sheet: StyleSheet): PositionedCue | null {
  const own = layoutCue(segment, line.lang, cueStyleIn(sheet, segment, line), sheet);
  if (!own) return null;
  const { place, laid } = own;
  let dy = 0;
  if (line.lang === "ja") {
    const sibling = lines.find(other => other.trackId === line.trackId && other.lang === "zh");
    const below = sibling ? layoutCue(segment, "zh", cueStyleIn(sheet, segment, sibling), sheet) : null;
    if (below) dy = below.laid.block.top - (laid.block.top + laid.block.height);
  }
  const block = dy ? shiftBlock(laid.block, dy) : laid.block;
  return { block, size: laid.size, overflow: laid.overflow, x: place.x, y: place.y + dy, an: place.an as Align };
}

function shiftBlock(block: BlockRect, dy: number): BlockRect {
  return {
    ...block,
    top: block.top + dy,
    content: { ...block.content, top: block.content.top + dy },
    lines: block.lines.map(item => ({ ...item, top: item.top + dy })),
  };
}

/** Up to two decimals, no trailing zeros (`\pos` / `\fs` values) */
const num2 = (value: number) => String(Math.round(value * 100) / 100);

/** ASS text for a positioned cue: `{\anN\pos(X,Y)\fsF}` + the wrapped lines joined by \N */
export function positionedAssText(cue: PositionedCue): string {
  const text = assTx(cue.block.lines.map(item => item.text).join("\n"));
  return `{\\an${cue.an}\\pos(${num2(cue.x)},${num2(cue.y)})\\fs${num2(cue.size)}}${text}`;
}

/**
 * ASS texts for a positioned cue. Lines at the font's own pitch go out as one event; a pitch of
 * its own (the original text's, `pos.lh`) can't be told to libass, so then every line is an
 * event of its own, top-anchored at its line top on the same horizontal anchor.
 */
export function positionedAssEvents(cue: PositionedCue): string[] {
  const { lines, lineHeight } = cue.block;
  if (lines.length < 2 || Math.abs(lines[1].top - lines[0].top - lineHeight) < 0.01) {
    return [positionedAssText(cue)];
  }
  const an = 6 + (((cue.an - 1) % 3) + 1);
  return lines.map(line =>
    `{\\an${an}\\pos(${num2(cue.x)},${num2(line.top)})\\fs${num2(cue.size)}}${assTx(line.text)}`);
}

// ── Plates (FlowOCR-compatible `fo-fx|plate` events) ───────────

export type PlateMode = "draw" | "omit";

export interface BuildAssOptions {
  /** "draw" (default) puts opaque plates under positioned text; "omit" leaves them out */
  plates?: PlateMode;
}

const fixed2 = (value: number) => value.toFixed(2);

/** One plate, in the exact form FlowOCR's burn/plates.py reads (PlayRes px, two decimals) */
export function plateDialogue(t0: number, t1: number, style: string, name: string,
  box: readonly [number, number, number, number], color: string, sheet: StyleSheet): string {
  const { x: X, y: Y } = sheet.playRes;
  const W = fixed2((box[2] - box[0]) * X), H = fixed2((box[3] - box[1]) * Y);
  return `Dialogue: 0,${assTs(t0)},${assTs(t1)},${style},${assNm(name)},0,0,0,fo-fx|plate,`
    + `{\\an7\\pos(${fixed2(box[0] * X)},${fixed2(box[1] * Y)})\\p1\\bord0\\shad0\\1c${color}\\1a&H00&}`
    + `m 0 0 l ${W} 0 ${W} ${H} 0 ${H}{\\p0}`;
}

/** How far a plate reaches past the OCR box, as a share of the glyph height */
export const PLATE_PAD = 0.2;
/** …but never more than this share of the frame height */
const PLATE_PAD_MAX = 0.015;

/**
 * The cover box grown on every side: OCR boxes hug the glyphs, while the original's outline,
 * shadow and anti-aliasing reach a few pixels further and would peek out from an exact plate.
 * The same box is the inpainting mask, which wants that margin just as much.
 *
 * `follow`: a drawn plate is the translation's backing, so it moves with it — by however far the
 * translation was dragged from the spot it started on (its anchor on the cover box). The
 * inpainting mask does not follow: what is erased is the original text, which stays where it is.
 */
export function plateBox(segment: SubtitleSegment, sheet: StyleSheet, follow = false): CoverBox {
  const box = segment.cover!.box;
  const glyph = segment.pos?.fs ?? (box[3] - box[1]);
  const padY = Math.min(PLATE_PAD_MAX, glyph * PLATE_PAD);
  const padX = padY * sheet.playRes.y / sheet.playRes.x;
  const pos = segment.pos;
  const from = follow && pos ? anchorOnBox(box, pos.an) : null;
  const dx = from && pos ? pos.x - from.x : 0, dy = from && pos ? pos.y - from.y : 0;
  return [Math.max(0, box[0] + dx - padX), Math.max(0, box[1] + dy - padY),
    Math.min(1, box[2] + dx + padX), Math.min(1, box[3] + dy + padY)];
}

/**
 * Plate events for every segment on a showing lane that has a cover box, each segment once
 * (both lanes share it). `requireFill`: only covers with a valid fill colour (drawn plates);
 * otherwise every cover box (the inpainting mask), black where it has no fill.
 */
function plateEvents(lines: readonly OutputLine[], sheet: StyleSheet, requireFill: boolean): string[] {
  const seen = new Set<SubtitleSegment>();
  const out: string[] = [];
  for (const line of lines) {
    for (const segment of line.arr) {
      const cover = segment.cover;
      if (!cover || seen.has(segment)) continue;
      const color = assColorFromHex(cover.fill);
      if (requireFill && !color) continue;
      seen.add(segment);
      out.push(plateDialogue(segment.t0, segment.t1, cueStyleIn(sheet, segment, line), line.name,
        plateBox(segment, sheet, requireFill), color ?? "&H000000&", sheet));
    }
  }
  return out;
}

/**
 * Plates only, for every cover box regardless of fill: the input of text removal (LaMa) at
 * burn time. Contains no other `\p1` drawing — the particle generators never run here.
 */
export function buildPlatesAss(source: SubtitleSource, sheet: StyleSheet): string {
  const plates = plateEvents(outputLinesOf(source, sheet), sheet, false);
  return assHeadOf(sheet) + ASS_EVENTS_HEAD + plates.join("\n") + "\n";
}

export interface StyleBinding {
  trackId: string;
  trackName: string;
  lang: Lang;
  /** false = 这条轨绑的是别的名字，只是回退到了这个样式（见 resolveStyleIn） */
  direct: boolean;
}

/**
 * 哪些 lane 正用着这个样式。**被眼睛藏起来的也算**：它一样共用样式，一取消隐藏就看到改动。
 * 拖动前拿它判断「改这一下会不会牵动别的轨」。
 */
export function lanesUsingStyle(source: SubtitleSource, sheet: StyleSheet, name: string): StyleBinding[] {
  const found: StyleBinding[] = [];
  const check = (trackId: string, trackName: string, lang: Lang, meta: LaneMeta | undefined) => {
    if (!meta) return;
    if (meta.style === name) found.push({ trackId, trackName, lang, direct: true });
    else if (resolveStyleIn(sheet, meta.style || "", lang) === name) {
      found.push({ trackId, trackName, lang, direct: false });
    }
  };
  if (source.trackMeta) {
    const name0 = trackNameOf(source, -1);
    check(DEFAULT_EFFECT_TRACK_ID, name0, "zh", source.trackMeta.zh);
    check(DEFAULT_EFFECT_TRACK_ID, name0, "ja", source.trackMeta.ja);
  }
  source.tracks.forEach((track, index) => {
    const trackId = track.id || `track-${index + 1}`;
    const trackName = trackNameOf(source, index);
    check(trackId, trackName, "zh", track.zh);
    check(trackId, trackName, "ja", track.ja);
  });
  return found;
}

/** 有多少个字幕块（一句的一条 lane）单独绑了这个样式。藏起来的轨也算，口径同 lanesUsingStyle */
export function segsUsingStyle(source: SubtitleSource, name: string): number {
  let count = 0;
  const scan = (segs: SubtitleSegment[]) => {
    for (const s of segs) {
      if (s.style?.ja === name) count++;
      if (s.style?.zh === name) count++;
    }
  };
  scan(source.segs);
  for (const track of source.tracks) scan(track.segs);
  return count;
}

/**
 * 按样式文本框宽度（[MarginL, PlayResX - MarginR]）自动折行后转成 ASS 文本。
 *
 * 不能指望 libass 自己折行：JASSUB 的 WASM 构建未带 libunibreak，遇到没有空格的
 * 中日文整句根本不会折行；各平台 ffmpeg 的 libass 编法也不统一。统一由 layoutBlock
 * 在拼 ASS 时插入 \N，配合 WrapStyle: 2，保证预览画面、选中框、逐字特效与导出压片
 * 四处折行完全一致，且原始句子文本本身保持干净、拉宽框即刻还原。
 */
function wrappedAssText(raw: string, style: AssStyle | undefined, sheet: StyleSheet): string {
  const trimmed = (raw || "").trim();
  if (!trimmed || !style) return assTx(raw);
  const block = layoutBlock(trimmed, style, sheet);
  const gap = Math.max(0, Math.round(style.lineSpacing || 0));
  if (block.lines.length <= 1 || gap <= 0) {
    return assTx(block.lines.map(line => line.text).join("\n"));
  }
  // 用全透明、字号等于 gap 的占位行撑开行距；{\r} 放在换行后的下一行开头，
  // 避免 libass 在占位行上恢复原字号把占位行撑高。单条 Dialogue 保留了碰撞避让、旋转与淡入淡出。
  const sep = `\\N{\\fs${gap}\\fscy100\\bord0\\shad0\\alpha&HFF&}\\h\\N{\\r}`;
  return block.lines
    .map(line => (line.text || "").replace(/\{/g, "(").replace(/\}/g, ")"))
    .join(sep);
}

/**
 * Plates come first, all on layer 0: libass draws events of one layer in file order, so they
 * end up under every piece of text without renumbering any existing layer.
 */
export function buildAssFrom(source: SubtitleSource, sheet: StyleSheet, options: BuildAssOptions = {}): string {
  const lines = outputLinesOf(source, sheet);
  const evs: string[] = options.plates === "omit" ? [] : plateEvents(lines, sheet, true);
  for (const L of lines) {
    const generator = generatorOf(L);
    const prefix = transformTag(L);
    for (const e of cuesOf(L, sheet)) {
      const style = sheet.styleMap[e.style];
      // Positioned cues skip the per-glyph generators for now (those lay glyphs out from margins)
      const placed = style && e.segment.pos ? positionedCueOf(e.segment, L, lines, sheet) : null;
      const built = placed
        ? positionedAssEvents(placed).map(text => ({ layer: 0, t0: e.t0, t1: e.t1, text: prefix + text }))
        : generator && style && !e.segment.pos
          ? GENERATORS[generator.templateId](L, { ...e.segment, t0: e.t0, t1: e.t1 }, style, sheet, generator)
          : [{ layer: 0, t0: e.t0, t1: e.t1, text: prefix + wrappedAssText(e.segment[L.lang], style, sheet) }];
      for (const event of built)
        evs.push(`Dialogue: ${event.layer},${assTs(event.t0)},${assTs(event.t1)},${e.style},${assNm(L.name)},0,0,0,,${event.text}`);
    }
  }
  return assHeadOf(sheet) + ASS_EVENTS_HEAD + evs.join("\n") + "\n";
}

/**
 * 按片段拼接的 ASS：整片那份必须与服务端逐字一致，所以只在它外面包一层做时间变换——
 * 每条 Dialogue 的起止都经 srcToOut 映射到成片时间，映射后不足一个厘秒的（整条落在
 * 删掉的部分里）直接丢掉。特效展开出来的每条 Dialogue 也各自映射，所以句子中间被删
 * 一截时，动画按缩短后的时长走完。
 */
export function piecesAss(full: string, pieces: readonly Piece[]): string {
  const i = full.indexOf("Dialogue:");
  if (i < 0) return full;
  const head = full.slice(0, i);
  const out: string[] = [];
  for (const line of full.slice(i).split("\n")) {
    const m = /^Dialogue: (\d+),([^,]+),([^,]+),(.*)$/.exec(line);
    if (!m) continue;
    const a = srcToOut(pieces, assSec(m[2])), b = srcToOut(pieces, assSec(m[3]));
    if (b - a < 0.01) continue;
    out.push(`Dialogue: ${m[1]},${assTs(a)},${assTs(b)},${m[4]}`);
  }
  return head + out.join("\n") + "\n";
}

/** 区间 ASS：只有一段的 piecesAss——滤掉不相交的 Dialogue，其余钳进区间后减去 T0 */
export const clipAss = (full: string, T0: number, T1: number): string =>
  piecesAss(full, [{ t0: T0, t1: T1 }]);

export type SrtLang = "both" | "zh" | "ja";

interface Cue { t0: number; t1: number; text: string }

const VISIBLE: LaneMeta = { hidden: false, style: null };

export interface BuildSrtOptions {
  /** 是否包含「文字识别」轨（`source.kind === "screen-text"`）。默认 false，SRT 导出跳过它们。 */
  includeScreenText?: boolean;
}

/**
 * lang: both=译文在上原文在下 / zh=只译文 / ja=只原文；被眼睛藏起来的 lane 不出
 * （与 ASS 导出同口径）。选中语言整条都空的句子跳过、序号按合并后的时间序重排——
 * 关掉翻译跑出来的产物译文全空，照原样出就是一份满是空块的坏 SRT。
 * pieces 给了就按片段映射到成片时间（见 piecesAss），整句都被删掉的不出。
 * 文字识别轨（`source.kind === "screen-text"`）默认跳过，传入 `{ includeScreenText: true }` 才带上。
 */
export function buildSrtFrom(
  source: SubtitleSource,
  lang: SrtLang,
  pieces?: readonly Piece[],
  options: BuildSrtOptions = {},
): string {
  const cues: Cue[] = [];
  const includeScreenText = options.includeScreenText === true;

  const collect = (arr: SubtitleSegment[], ja: LaneMeta, zh: LaneMeta) => {
    for (const s of arr) {
      const t0 = pieces ? srcToOut(pieces, s.t0) : s.t0;
      const t1 = pieces ? srcToOut(pieces, s.t1) : s.t1;
      if (pieces && t1 - t0 < 0.001) continue;
      const lines: string[] = [];
      if (lang !== "ja" && !zh.hidden && (s.zh || "").trim()) lines.push(s.zh);
      if (lang !== "zh" && !ja.hidden && (s.ja || "").trim()) lines.push(s.ja);
      if (!lines.length) continue;
      cues.push({ t0, t1, text: lines.join("\n") });
    }
  };
  collect(source.segs, source.trackMeta?.ja || VISIBLE, source.trackMeta?.zh || VISIBLE);
  for (const tr of source.tracks) {
    if (!includeScreenText && tr.source?.kind === "screen-text") continue;
    collect(tr.segs, tr.ja, tr.zh);
  }

  // 摊平成一条时间流。sort 是稳定的，同一时刻的多轨保持「默认轨在前」的收集顺序
  cues.sort((a, b) => a.t0 - b.t0 || a.t1 - b.t1);
  return cues.map((c, i) =>
    `${i + 1}\n${srtTs(c.t0)} --> ${srtTs(c.t1)}\n${c.text}\n\n`).join("");
}
