import { ASS_EVENTS_HEAD } from './constants.ts';
import { DEFAULT_EFFECT_TRACK_ID, resolveLaneEffects } from './effects.ts';
import { assColorFromHex, assNm, assSec, assTs, assTx, srtTs } from './format.ts';
import { karaokeTimeline } from './karaoke.ts';
import { layoutBlock } from './layout.ts';
import { assHeadOf, resolveStyleIn } from './styles.ts';
import type { StyleSheet } from './styles.ts';
import type {
  AssStyle, Lang, LaneMeta, SubtitleEffectBinding, SubtitleSegment, SubtitleSource,
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
 * 没绑样式的线不出现；绑了本机没有的样式则回退 JP/CN，不再整条线消失。
 */
export function outputLinesOf(source: SubtitleSource, sheet: StyleSheet): OutputLine[] {
  const out: OutputLine[] = [];
  const add = (arr: SubtitleSegment[], lang: Lang, meta: LaneMeta, name: string, trackId: string) => {
    if (meta.style) out.push({
      arr, lang, meta, trackId,
      style: resolveStyleIn(sheet, meta.style, lang), name,
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

/** 只让程序生成受控的 ASS 覆写标签，普通字幕文本仍由 assTx 转义花括号。 */
const legacyFadeTag = (meta: LaneMeta): string => {
  const ms = (value: unknown) => Math.min(60_000, Math.max(0, Math.round(Number(value) || 0)));
  const fadeIn = ms(meta.fadeInMs), fadeOut = ms(meta.fadeOutMs);
  return fadeIn || fadeOut ? `{\\fad(${fadeIn},${fadeOut})}` : "";
};

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
  if (!fade) return legacyFadeTag(line.meta);
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

/** 有绑定、但样式表里查不到的样式名（这些线会回退到 JP/CN 照常出图） */
export function unknownStylesOf(source: SubtitleSource, sheet: StyleSheet): string[] {
  const miss = new Set<string>();
  const chk = (lane: LaneMeta | undefined) => {
    if (lane && lane.style && !sheet.styleMap[lane.style]) miss.add(lane.style);
  };
  if (source.trackMeta) { chk(source.trackMeta.zh); chk(source.trackMeta.ja); }
  for (const tr of source.tracks) { chk(tr.zh); chk(tr.ja); }
  return [...miss];
}

export interface BuiltCue { segment: SubtitleSegment; t0: number; t1: number; text: string }

/**
 * 一条输出线上真正会出图的那些句，按时间排好、重叠钳好。
 *
 * 预览里的命中测试必须复用它：钳重叠这一步会改出点，自己再算一遍的话，边界处点到的
 * 会是另一句。
 */
export function cuesOf(line: OutputLine): BuiltCue[] {
  // 组内按时间排：全片一起排会让晚开口的高优先级线被挤走
  const cues = line.arr.filter(s => (s[line.lang] || "").trim())
    .map(s => ({ segment: s, t0: s.t0, t1: s.t1, text: transformTag(line) + assTx(s[line.lang]) }))
    .sort((a, b) => a.t0 - b.t0 || a.t1 - b.t1);
  // 同一条线内相邻句的毫秒级重叠（ASR 数据自带）会被当成碰撞，
  // 把后一句整句顶离贴边位——前句出点一律钳到后句入点
  for (let i = 0; i < cues.length - 1; i++)
    if (cues[i].t1 > cues[i + 1].t0) cues[i].t1 = cues[i + 1].t0;
  return cues;
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
    if (!meta?.style) return;
    if (meta.style === name) found.push({ trackId, trackName, lang, direct: true });
    else if (resolveStyleIn(sheet, meta.style, lang) === name) {
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

export function buildAssFrom(source: SubtitleSource, sheet: StyleSheet): string {
  const evs: string[] = [];
  for (const L of outputLinesOf(source, sheet)) {
    const generator = generatorOf(L);
    const style = sheet.styleMap[L.style];
    for (const e of cuesOf(L)) {
      const built = generator && style
        ? GENERATORS[generator.templateId](L, { ...e.segment, t0: e.t0, t1: e.t1 }, style, sheet, generator)
        : [{ layer: 0, t0: e.t0, t1: e.t1, text: e.text }];
      for (const event of built)
        evs.push(`Dialogue: ${event.layer},${assTs(event.t0)},${assTs(event.t1)},${L.style},${assNm(L.name)},0,0,0,,${event.text}`);
    }
  }
  return assHeadOf(sheet) + ASS_EVENTS_HEAD + evs.join("\n") + "\n";
}

/**
 * 区间 ASS：整片那份必须与服务端逐字一致，所以只在它外面包一层做区间变换——
 * 滤掉不相交的 Dialogue，其余把起止钳进区间后统一减去 T0。
 */
export function clipAss(full: string, T0: number, T1: number): string {
  const i = full.indexOf("Dialogue:");
  if (i < 0) return full;
  const head = full.slice(0, i);
  const out: string[] = [];
  for (const line of full.slice(i).split("\n")) {
    const m = /^Dialogue: (\d+),([^,]+),([^,]+),(.*)$/.exec(line);
    if (!m) continue;
    const a = assSec(m[2]), b = assSec(m[3]);
    if (b <= T0 || a >= T1) continue;
    out.push(`Dialogue: ${m[1]},${assTs(Math.max(a, T0) - T0)},${assTs(Math.min(b, T1) - T0)},${m[4]}`);
  }
  return head + out.join("\n") + "\n";
}

export type SrtLang = "both" | "zh" | "ja";

interface Cue { t0: number; t1: number; text: string }

const VISIBLE: LaneMeta = { hidden: false, style: null };

/**
 * lang: both=译文在上原文在下 / zh=只译文 / ja=只原文；被眼睛藏起来的 lane 不出
 * （与 ASS 导出同口径）。选中语言整条都空的句子跳过、序号按合并后的时间序重排——
 * 关掉翻译跑出来的产物译文全空，照原样出就是一份满是空块的坏 SRT。
 * T0/T1 给了就裁到那个区间并把时间轴平移到 0（切片导出）。
 */
export function buildSrtFrom(source: SubtitleSource, lang: SrtLang, T0?: number, T1?: number): string {
  const clip = T0 !== undefined && T1 !== undefined;
  const cues: Cue[] = [];

  const collect = (arr: SubtitleSegment[], ja: LaneMeta, zh: LaneMeta) => {
    for (const s of arr) {
      if (clip && (s.t1 <= T0! || s.t0 >= T1!)) continue;
      const lines: string[] = [];
      if (lang !== "ja" && !zh.hidden && (s.zh || "").trim()) lines.push(s.zh);
      if (lang !== "zh" && !ja.hidden && (s.ja || "").trim()) lines.push(s.ja);
      if (!lines.length) continue;
      cues.push({
        t0: clip ? Math.max(s.t0, T0!) - T0! : s.t0,
        t1: clip ? Math.min(s.t1, T1!) - T0! : s.t1,
        text: lines.join("\n"),
      });
    }
  };
  collect(source.segs, source.trackMeta?.ja || VISIBLE, source.trackMeta?.zh || VISIBLE);
  for (const tr of source.tracks) collect(tr.segs, tr.ja, tr.zh);

  // 摊平成一条时间流。sort 是稳定的，同一时刻的多轨保持「默认轨在前」的收集顺序
  cues.sort((a, b) => a.t0 - b.t0 || a.t1 - b.t1);
  return cues.map((c, i) =>
    `${i + 1}\n${srtTs(c.t0)} --> ${srtTs(c.t1)}\n${c.text}\n\n`).join("");
}
