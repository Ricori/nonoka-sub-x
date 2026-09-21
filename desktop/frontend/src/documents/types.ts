import type { KaraokeUnit, SubtitleEffectBinding } from '../subtitles/types.ts';

export interface EditSegment {
  t0: number;
  t1: number;
  ja: string;
  zh: string;
  /** 逐字（K 轴）时间，只描述原文 */
  k?: KaraokeUnit[];
  words?: unknown[];
  low_conf?: boolean;
}

export interface LaneMeta {
  hidden: boolean;
  style: string | null;
}

export interface EditTrack {
  id: string;
  name: string;
  ja: LaneMeta;
  zh: LaneMeta;
  hja: number;
  hzh: number;
  segs: EditSegment[];
}

export interface EditDocument {
  schema: 1;
  video_id: string;
  title: string;
  source: string;
  fp: string | null;
  rev: number;
  subtitles: EditSegment[];
  tracks: EditTrack[];
  track_meta: { name: string; ja: LaneMeta; zh: LaneMeta };
  effects?: SubtitleEffectBinding[];
  /** 这个视频的 [V4+ Styles] 原文；老文档没有，打开时拿本机默认模板当种子 */
  styles?: string;
  projection: { schema: 1; mode: "stable" | "final" };
  created_at?: string;
  updated_at?: string;
}
