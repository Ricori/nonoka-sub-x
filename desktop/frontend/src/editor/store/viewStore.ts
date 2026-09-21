import { createStore } from '../../home/lib/createStore';
import { BLK_MARGIN, MIN_DUR, ZOOM_FLOOR, ZOOM_MAX } from '../constants';
import { fmt } from '../utils';
import { outToSrc, piecesDuration, srcToOut } from '../../subtitles/pieces.ts';
import type { Piece } from '../../subtitles/pieces.ts';
import { curVp, innerLeft, syncTlMetrics, tlInner, tlScroll, tlStore } from './tlStore';
import type { Seg } from '../types';

/**
 * 视图窗口：完整片 = 0..duration，聚焦成片时 = 首段起点..末段终点。
 * 时间数据（句子 t0/t1、播放头、peaks）一律是「原片绝对秒」，只有时间↔像素（xOf/tOf）和
 * 显示用的时间码知道剪辑：聚焦时两者都走成片时间，删掉的部分宽度为 0，时间轴直接合拢。
 * 存盘、ASS 生成、撤销栈都不用知道剪辑的存在。
 */
interface ViewState {
  duration: number;
  /** 初值必须和 duration 对齐：给 0 的话 setDuration() 之前的 fitPps() 会算出天文数字 */
  t0: number;
  t1: number;
  /** 视频轨片段（原片时间，有序）；null = 没剪过，整片就是一段 */
  pieces: Piece[] | null;
  /** 聚焦成片：时间轴合拢掉删掉的部分，时间码显示成片时间，播放时跳过删掉的部分 */
  focus: boolean;
  pps: number;                       // 像素/秒
  blkWin: [number, number] | null;   // 已渲染的字幕块时间窗
}

export const viewStore = createStore<ViewState>({
  duration: 60, t0: 0, t1: 60, pieces: null, focus: false, pps: 46, blkWin: null,
});

/** 当前的片段；没剪过就是整片一段 */
export const curPieces = (): Piece[] => {
  const v = viewStore.get();
  return v.pieces ?? [{ t0: 0, t1: v.duration }];
};
/** 聚焦成片生效中（剪过才有意义） */
export const isFocused = () => { const v = viewStore.get(); return v.focus && !!v.pieces?.length; };

/** 时间轴代表的时长（秒）：聚焦时是成片时长，决定 inner 宽度和全览缩放 */
export const viewDur = () => {
  const v = viewStore.get();
  return Math.max(0.001, isFocused() ? piecesDuration(v.pieces!) : v.t1 - v.t0);
};
/** 绝对秒 → 时间轴像素。聚焦时按成片时间摆：删掉的部分里的点都压在拼接点上 */
export const xOf = (t: number) => {
  const v = viewStore.get();
  return (isFocused() ? srcToOut(v.pieces!, t) : t - v.t0) * v.pps;
};
/** 时间轴像素 → 绝对秒。聚焦时落在拼接点上取后一段的起点 */
export const tOf = (x: number) => {
  const v = viewStore.get();
  return isFocused() ? outToSrc(v.pieces!, x / v.pps) : x / v.pps + v.t0;
};
/** 原片 [a, b) 在时间轴上占多宽（跨过删掉的部分只算留下的） */
export const wOf = (a: number, b: number) => xOf(b) - xOf(a);
/**
 * 拖动：把 anchor 这一点在时间轴上挪 dx 像素后，原片时间变了多少。完整片里就是 dx / pps；
 * 聚焦成片时跨过拼接点会连删掉的部分一起跳过，拖过去的块不会掉进看不见的地方。
 */
export const dtAt = (anchor: number, dx: number) => tOf(xOf(anchor) + dx) - anchor;
/** 原片时间 → 面向用户的时间：聚焦时是成片时间（跨过删掉的部分也连续），否则就是原片时间 */
export const viewTime = (t: number) => isFocused() ? srcToOut(viewStore.get().pieces!, t) : t;
/** 面向用户的时间码 */
export const fmtView = (t: number) => fmt(viewTime(t));
/** 面向用户的总时长：聚焦时是成片时长 */
export const viewOutDur = viewDur;
/** 鼠标视口坐标 → 绝对秒 */
export const tAtClientX = (clientX: number) => tOf(clientX - innerLeft());

/**
 * 视图内的句子在数组里必然是连续一段（同轨按 t0 有序、无重叠），所以聚焦时不过滤数组、
 * 只收窄下标区间——sel/selSet/撤销栈那套「下标即真实位置」的语义一个字都不用改。
 */
export function viewRange(arr: Seg[]): [number, number] {
  const v = viewStore.get();
  if (v.t0 <= 0 && v.t1 >= v.duration) return [0, arr.length];
  let a = 0, b = arr.length;
  while (a < arr.length && arr[a].t1 <= v.t0) a++;
  while (b > a && arr[b - 1].t0 >= v.t1) b--;
  return [a, b];
}

/**
 * 聚焦成片时整句都落在删掉的部分里：成片里没有它，列表、句数、上下句、查找都跳过。
 * 数据照留（切回完整片、恢复片段就又出来了），所以只是「不显示」，不改下标。
 */
export function isCutSeg(s: Seg): boolean {
  if (!isFocused()) return false;
  const p = viewStore.get().pieces!;
  return srcToOut(p, s.t1) - srcToOut(p, s.t0) <= 1e-6;
}

/** 视图内、且没被整句剪掉的句子下标，按先后排好 */
export function listedIdx(arr: Seg[]): number[] {
  const [a, b] = viewRange(arr);
  const out: number[] = [];
  for (let i = a; i < b; i++) if (!isCutSeg(arr[i])) out.push(i);
  return out;
}

/**
 * 时长会来两次（先按 peaks/末句估，视频 loadedmetadata 后才是真值），每次都要重新对齐
 * 视图窗口——聚焦时也得按新时长钳一遍，否则末段终点可能落在片尾之外。
 * 片段变了、进出聚焦也走这里重算窗口。
 */
export function setDuration(d: number) {
  viewStore.set({ duration: d });
  if (isFocused()) {
    const p = viewStore.get().pieces!;
    const t1 = Math.min(p[p.length - 1].t1, d);
    viewStore.set({ t1, t0: Math.max(0, Math.min(p[0].t0, t1 - MIN_DUR)) });
  } else {
    viewStore.set({ duration: d, t0: 0, t1: d });
  }
}

// ── 缩放 ──────────────────────────────────────────────────────────
/** 缩到最小时整条轴要能塞进视口：长视频下限就放宽到「全览」这一档 */
export const fitPps = () => {
  const w = tlStore.get().w;
  return (w > 0 && viewDur() > 0) ? w / viewDur() : ZOOM_FLOOR;
};
export const zoomMin = () => Math.min(ZOOM_FLOOR, fitPps());

// 滑块走对数刻度（0–1000 映射到 zoomMin()–ZOOM_MAX 像素/秒）：放大上限拉到 400px/s
// 后再用线性刻度的话，常用的几十 px/s 会全挤在最左边一小截里
export const sliderToPps = (v: number) => { const lo = zoomMin(); return lo * Math.pow(ZOOM_MAX / lo, v / 1000); };
export const ppsToSlider = (p: number) => {
  const lo = zoomMin();
  return Math.round(1000 * Math.log(Math.max(p, lo) / lo) / Math.log(ZOOM_MAX / lo));
};

/** 缩放到 np 像素/秒，锚点默认视口中心；传 anchorClientX 则以光标处时间为锚点 */
export function setZoom(np: number, anchorClientX?: number) {
  np = Math.min(Math.max(np, zoomMin()), ZOOM_MAX);
  if (np === viewStore.get().pps) return;
  // 视口先量齐，再一次性写回：中途读 DOM 会强制同步重排
  const vp = curVp();
  const scroll = tlScroll();
  const rect = scroll ? scroll.getBoundingClientRect() : ({ left: 0 } as DOMRect);
  const ax = anchorClientX != null
    ? Math.min(Math.max(anchorClientX - rect.left, 0), vp.w)
    : vp.w / 2;
  const at = tOf(vp.left + ax);   // 锚点对应的时间
  viewStore.set({ pps: np });
  // 先把 inner 宽度写到位再挪 scrollLeft，否则浏览器会按旧宽度把它钳回去
  // （React 稍后渲染出的宽度与这里一致，不会打架）
  applyInnerWidth();
  const left = Math.max(0, Math.min(xOf(at) - ax, viewDur() * np - vp.w));
  if (scroll) scroll.scrollLeft = left;
}

/** 视口宽或时长变了 → 全览这一档也跟着变：重新钳当前缩放 */
export function syncZoomRange() {
  const lo = zoomMin();
  if (viewStore.get().pps < lo - 1e-9) {
    viewStore.set({ pps: lo });
    applyInnerWidth();
    return true;
  }
  return false;
}

export function applyInnerWidth() {
  const inner = tlInner();
  if (inner) inner.style.width = (viewDur() * viewStore.get().pps) + "px";
}

/** 时长/缩放/可视尺寸变了之后的整体重排：宽度 → 指标 → 块窗口 */
export function relayout() {
  applyInnerWidth();
  syncTlMetrics();
  ensureBlkWin(true);
}

// ── 字幕块渲染窗 ──────────────────────────────────────────────────
// 块只画视口附近的一段：整轨几千句全建 DOM，缩放和滚动都会卡成幻灯片
let blkFrozen = false;
export const freezeBlocks = (v: boolean) => { blkFrozen = v; };

export function visWin(vp = curVp()): [number, number] {
  const vw = vp.w || 1;
  return [Math.max(viewStore.get().t0, tOf(vp.left - vw * BLK_MARGIN)), tOf(vp.left + vw * (1 + BLK_MARGIN))];
}

/** 视口滚出已渲染的窗口才补画（拖动中冻结） */
export function ensureBlkWin(force = false) {
  const vp = curVp();
  const win = viewStore.get().blkWin;
  if (!force) {
    if (blkFrozen || !win) return;
    if (tOf(vp.left) >= win[0] && tOf(vp.left + vp.w) <= win[1]) return;
  }
  viewStore.set({ blkWin: visWin(vp) });
}
