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
}

/** 自定义轨：与默认轨同构的双 lane */
export interface SubtitleTrack {
  id?: string;
  name: string;
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
  outline: number;
  shadow: number;
  align: number;
  ml: number;
  mr: number;
  mv: number;
}
