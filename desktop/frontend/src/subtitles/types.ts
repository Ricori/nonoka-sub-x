// 字幕拼装的公共数据形状。编辑器（就地可变的 Seg/Track）和插件宿主（从服务端读回的
// EditDocument）都能结构化地喂进来，两边于是共用同一条 ASS/SRT 拼装管线。

export type Lang = "ja" | "zh";

export type SubtitleEffectTarget =
  | { scope: "all" }
  | { scope: "track"; trackId: string }
  | { scope: "lane"; trackId: string; lang: Lang };

export type SubtitleEffectParam = string | number | boolean;

/** 文档里只保存模板引用、作用范围和覆盖参数；模板实现由宿主统一管理。 */
export interface SubtitleEffectBinding {
  id: string;
  templateId: string;
  enabled: boolean;
  target: SubtitleEffectTarget;
  params: Record<string, SubtitleEffectParam>;
}

/** 一条 lane（原文/译文）的展示元数据 */
export interface LaneMeta {
  hidden: boolean;
  style: string | null;
}

/** K 轴的一个单位（一个字），t0/t1 是绝对秒 */
export interface KaraokeUnit {
  t0: number;
  t1: number;
  text: string;
}

export interface SubtitleSegment {
  t0: number;
  t1: number;
  ja: string;
  zh: string;
  /** 逐字（K 轴）时间，只描述原文；来自中间产物的词级时间戳或手动均分 */
  k?: KaraokeUnit[];
  /** 转写中间产物里的词级时间戳，K 轴由它生成 */
  words?: unknown[];
  /** 这一句单独绑的样式，按 lane 分；没写（或样式表里没有）的 lane 跟随轨道样式 */
  style?: SegmentStyle;
  /** Per-segment placement (on-screen text). Present = this cue ignores style margins */
  pos?: SegmentPos;
  /** The original on-screen text box this cue covers; drives plates and inpainting */
  cover?: SegmentCover;
}

/** ASS numpad alignment */
export type Align = 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9;

/**
 * Per-segment placement. Coordinates are fractions (0..1) of the video frame, independent
 * of PlayRes, so a document survives a PlayRes change.
 */
export interface SegmentPos {
  /** anchor point, fraction of frame width */
  x: number;
  /** anchor point, fraction of frame height */
  y: number;
  /** which point of the text block sits on (x, y) */
  an: Align;
  /** wrap width, fraction of frame width; omitted = the style's text box width */
  w?: number;
  /**
   * Ink height of one (CJK) glyph, fraction of frame height; omitted = the style's font size.
   * The (x, y) anchor is likewise where the ink sits, not libass's line box (see InkMetrics).
   */
  fs?: number;
  /** line pitch (top of one line to the next), fraction of frame height; omitted = the font's own */
  lh?: number;
}

/** The original text's box, fractions of the frame: [x0, y0, x1, y1] */
export type CoverBox = [number, number, number, number];

export interface SegmentCover {
  box: CoverBox;
  /** plate colour `#rrggbb`; null/omitted = no plate drawn, but the box is still erased */
  fill?: string | null;
  /** the background sampled around the original text: the colour a plate gets when switched on */
  bg?: string;
}

/** Where an auto-generated track came from (e.g. a screen-text OCR run) */
export interface TrackSource {
  kind: "screen-text";
  task: string;
  region: string;
  label: string;
  hash: string;
}

/** 字幕句自己的样式覆盖：只存设了的那条 lane */
export type SegmentStyle = Partial<Record<Lang, string>>;

/** 自定义轨：与默认轨同构的双 lane */
export interface SubtitleTrack {
  id?: string;
  name: string;
  source?: TrackSource;
  ja: LaneMeta;
  zh: LaneMeta;
  segs: SubtitleSegment[];
}

/** 拼装一份字幕需要的全部文档内容 */
export interface SubtitleSource {
  segs: SubtitleSegment[];
  tracks: SubtitleTrack[];
  trackMeta: { name: string; ja: LaneMeta; zh: LaneMeta } | null;
  effects?: SubtitleEffectBinding[];
}

/** ASS 样式表里解析出来的一个 Style */
export interface AssStyle {
  name: string;
  font: string;
  size: number;
  c1: string;
  c3: string;
  c4: string;
  bold: number;
  italic: number;
  scx: number;
  scy: number;
  sp: number;
  /** 多行之间的额外行间距（PlayRes 像素，>= 0）；不在标准 ASS 23 字段内，存为样式表注释扩展 */
  lineSpacing?: number;
  outline: number;
  shadow: number;
  align: number;
  ml: number;
  mr: number;
  mv: number;
}
