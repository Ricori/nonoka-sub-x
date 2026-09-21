// 编辑器数据类型。字幕句（Seg）在整个编辑器里都是「就地可变」的对象：
// selSet、拖动、撤销快照都靠对象引用认句，换成不可变结构会牵一发动全身。

export type Lang = "ja" | "zh";

export type {
  KaraokeUnit, SubtitleEffectBinding, SubtitleEffectParam, SubtitleEffectTarget,
} from '../subtitles/types.ts';

export interface Seg {
  t0: number;
  t1: number;
  ja: string;
  zh: string;
  /** 逐字（K 轴）时间，只描述原文；见 src/subtitles/karaoke.ts */
  k?: import('../subtitles/types.ts').KaraokeUnit[];
  words?: unknown[];
  low_conf?: boolean;
}

/** 一条 lane（原文/译文）的展示元数据 */
export interface LaneMeta {
  hidden: boolean;
  style: string | null;
  /** @deprecated 旧版轨道渐变；载入后迁移到 effects。 */
  fadeInMs?: number;
  /** @deprecated 旧版轨道渐变；载入后迁移到 effects。 */
  fadeOutMs?: number;
}

/** 自定义轨（说话人/注释），与默认轨同构的双 lane */
export interface Track {
  id: string;
  name: string;
  ja: LaneMeta;
  zh: LaneMeta;
  hja: number;
  hzh: number;
  segs: Seg[];
}

/** 默认轨的展示元数据（存服务端） */
export interface TrackMeta {
  name: string;
  ja: LaneMeta;
  zh: LaneMeta;
}

/** 切片：原片上的一段命名区间，存本地 library.json */
export interface Clip {
  id: string;
  name: string;
  t0: number;
  t1: number;
  createdAt: number;
}

export interface Peaks {
  per_sec: number;
  duration: number;
  peaks: number[];
}

/** ASS 样式：与插件宿主共用 src/subtitles 的定义 */
export type { AssStyle } from '../subtitles/types.ts';

/** ti：-1 = 默认轨，>=0 = tracks 下标 */
export type Ti = number;

/** 轨道设置弹层的作用对象 */
export type TrackPopTarget =
  | { kind: "track"; ti: number }
  | { kind: "default"; lang: Lang };

export type CtxItem = "-" | { label: string; onClick: () => void; danger?: boolean };

/** 撤销/重做快照 */
export interface Snapshot {
  segs: Seg[];
  tracks: Track[];
  trackMeta: TrackMeta | null;
  effects: import('../subtitles/types.ts').SubtitleEffectBinding[];
  /** 样式表是文档字段，拖字幕改的就是它，所以撤销要一起回退 */
  styles: string;
  curTrack: number;
  sel: number;
  t: number;
}

/** 时间轴上一条渲染出来的字幕块（或密度块） */
export type LaneItem =
  | { kind: "blk"; i: number; seg: Seg }
  | { kind: "agg"; i0: number; i1: number; x: number; w: number };

/** 时间轴的一行（音频行 / 某轨的某条 lane） */
export interface RowSpec {
  key: string;
  kind: "wave" | "lane";
  ti: Ti;
  lang: Lang | null;
  /** 整行折叠（一键隐藏原文轨）：不占任何高度 */
  fold: boolean;
  /** 可见（未被眼睛按钮隐藏）；隐藏时压成 22px 标签行 */
  vis: boolean;
  /** 本机偏好（存 localStorage）还是随轨道存服务端 */
  local: boolean;
  height: number;
  setHeight(v: number): void;
}
