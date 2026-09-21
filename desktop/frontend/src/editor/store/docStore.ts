import { createStore } from '../../home/lib/createStore';
import { TRACK_PALETTE } from '../constants';
import { trackNameOf } from '../../subtitles/build';
import { resolveStyle, styleRgb } from '../ass';
import type { Lang, Peaks, Seg, SubtitleEffectBinding, Ti, Track, TrackMeta } from '../types';

export interface KnowledgeLearningState {
  status: "idle" | "queued" | "running" | "done" | "error";
  knowledge?: string;
  rev?: number;
  error?: string;
  report?: { added?: number; updated?: number; activated?: number; skipped?: number; examined?: number };
}

/**
 * 字幕文档。句对象就地可变（拖动、文本编辑直接改字段），改完调 bumpDoc() 触发重渲染。
 * 不做不可变复制是有意的：selSet、撤销快照、拖动都靠对象引用认句，复制一次就全断了。
 */
interface DocState {
  segs: Seg[];              // 默认轨（AI 转写+翻译）
  tracks: Track[];          // 自定义轨（说话人/注释）
  trackMeta: TrackMeta | null;   // 默认轨展示元数据（存服务端）
  effects: SubtitleEffectBinding[];  // 统一特效模板绑定
  /** 这个视频的 [V4+ Styles] 原文。样式跟着视频走，不再是机器级全局的那份 */
  styles: string;
  knowledgeBase: string;    // 本视频转写时选择的知识库
  canLearnKnowledge: boolean;
  knowledgeLearning: KnowledgeLearningState;
  rev: number;              // 服务端乐观锁版本
  title: string;
  videoFp: string | null;   // 上传时算好的文件指纹，选文件兜底校验用
  peaks: Peaks | null;
  version: number;
}

export const docStore = createStore<DocState>({
  segs: [], tracks: [], trackMeta: null, effects: [], styles: "",
  knowledgeBase: "", canLearnKnowledge: false, knowledgeLearning: { status: "idle" },
  rev: 0, title: "", videoFp: null, peaks: null, version: 0,
});

/** 就地改完文档后调用，触发订阅者重渲染 */
export const bumpDoc = () => docStore.set(s => ({ version: s.version + 1 }));

/** ti：-1=默认轨，否则自定义轨下标 */
export function segsOf(ti: Ti): Seg[] {
  const d = docStore.get();
  return ti < 0 ? d.segs : (d.tracks[ti] ? d.tracks[ti].segs : d.segs);
}

export function trackName(ti: Ti): string {
  const d = docStore.get();
  return trackNameOf({ segs: d.segs, tracks: d.tracks, trackMeta: d.trackMeta }, ti);
}

/** 定位某句在哪条轨/第几个。选中集合存的是对象引用，位置得现查 */
export function locateSeg(s: Seg): { ti: Ti; i: number } | null {
  const d = docStore.get();
  let i = d.segs.indexOf(s);
  if (i >= 0) return { ti: -1, i };
  for (let ti = 0; ti < d.tracks.length; ti++) {
    i = d.tracks[ti].segs.indexOf(s);
    if (i >= 0) return { ti, i };
  }
  return null;
}

/** 可放段落的轨从上到下的顺序（默认轨 -1 在最前，然后各自定义轨） */
export const orderedTis = (): Ti[] => [-1, ...docStore.get().tracks.map((_, i) => i)];
export const tiPos = (ti: Ti) => orderedTis().indexOf(ti);

/** 轨道 lane 主题色：取该 lane 绑定样式的 PrimaryColour，用于时间轴块/标签 */
export function laneColor(ti: Ti, lang: Lang): string {
  const tr = docStore.get().tracks[ti];
  const fb = TRACK_PALETTE[ti % TRACK_PALETTE.length];
  if (!tr) return fb;
  // 没绑或绑了样式表里没有的样式时跟着预览一起回退到 origin/cn，块的颜色才不会和画面上的字对不上
  return styleRgb(resolveStyle(tr[lang].style || "", lang), fb);
}
