import { docStore, locateSeg, orderedTis, segsOf, tiPos } from '../store/docStore';
import { playStore } from '../store/playStore';
import { markDirty } from '../store/saveStore';
import {
  curSegs, deselect, select, selStore, setActiveTrack, setSelectionSegs,
} from '../store/selectionStore';
import { toast } from '../store/uiStore';
import { isFocused, viewStore } from '../store/viewStore';
import { refreshAll } from './edits';
import { pushHistory } from './history';
import { seek } from './playback';
import type { Seg, Ti } from '../types';
import { cloneCover, clonePos, trimLaneOverlaps } from '../../subtitles/position';

const EPS = 1e-6;

interface ClipItem {
  /** 相对整组最早起点的偏移（秒） */
  relT0: number;
  relT1: number;
  /** 相对整组最靠上轨道的行距（orderedTis 下标差）；单轨选中时全为 0 */
  trackOffset: number;
  seg: Seg;
}

interface ClipPayload {
  anchorT0: number;
  items: ClipItem[];
}

interface Span {
  t0: number;
  t1: number;
}

let clipboard: ClipPayload | null = null;

export const hasClipboard = (): boolean => !!clipboard?.items.length;
export const clipboardCount = (): number => clipboard?.items.length ?? 0;
export const clearClipboard = (): void => { clipboard = null; };

function cloneWord(word: unknown, dt = 0): unknown {
  if (!word || typeof word !== "object") return word;
  const out = { ...(word as Record<string, unknown>) };
  if (dt) {
    if (typeof out.start === "number") out.start += dt;
    if (typeof out.end === "number") out.end += dt;
    if (typeof out.t0 === "number") out.t0 += dt;
    if (typeof out.t1 === "number") out.t1 += dt;
  }
  return out;
}

function cloneSeg(s: Seg, dt = 0, clampLo?: number, clampHi?: number): Seg {
  let t0 = s.t0 + dt;
  let t1 = s.t1 + dt;
  if (clampLo != null) t0 = Math.max(t0, clampLo);
  if (clampHi != null) t1 = Math.min(t1, clampHi);
  if (t1 < t0) t1 = t0;
  const out: Seg = { t0, t1, ja: s.ja, zh: s.zh };
  if (s.k) {
    out.k = s.k.map(u => {
      const u0 = Math.min(Math.max(u.t0 + dt, t0), t1);
      const u1 = Math.min(Math.max(u.t1 + dt, u0), t1);
      return { ...u, t0: u0, t1: u1 };
    });
  }
  if (s.words) out.words = s.words.map(w => cloneWord(w, dt));
  if (s.low_conf) out.low_conf = true;
  if (s.style) out.style = { ...s.style };
  if (s.pos) out.pos = clonePos(s.pos);
  if (s.cover) out.cover = cloneCover(s.cover);
  return out;
}

/** 收集当前选区里的有效句，按「轨序 → 句序」稳定排好 */
function collectSelected(): { seg: Seg; ti: Ti; i: number; pos: number }[] {
  const { selSet } = selStore.get();
  if (!selSet.size) return [];
  const found: { seg: Seg; ti: Ti; i: number; pos: number }[] = [];
  for (const seg of selSet) {
    const loc = locateSeg(seg);
    if (!loc) continue;
    const pos = tiPos(loc.ti);
    if (pos < 0) continue;
    found.push({ seg, ti: loc.ti, i: loc.i, pos });
  }
  found.sort((a, b) => (a.pos - b.pos) || (a.i - b.i));
  return found;
}

function writeClipboard(found: { seg: Seg; pos: number }[]) {
  const minPos = Math.min(...found.map(x => x.pos));
  const anchorT0 = Math.min(...found.map(x => x.seg.t0));
  clipboard = {
    anchorT0,
    items: found.map(x => ({
      relT0: x.seg.t0 - anchorT0,
      relT1: Math.max(x.seg.t0 - anchorT0, x.seg.t1 - anchorT0),
      trackOffset: x.pos - minPos,
      seg: cloneSeg(x.seg),
    })),
  };
}

/** 复制选中的字幕句（支持单句、多选及跨轨） */
export function copySegments(): number {
  const found = collectSelected();
  if (!found.length) return 0;
  writeClipboard(found);
  const n = found.length;
  toast(n > 1 ? `已复制 ${n} 句字幕` : "已复制 1 句字幕");
  return n;
}

/** 剪切选中的字幕句：写入剪贴板并一步入撤销栈摘除 */
export function cutSegments(): number {
  const found = collectSelected();
  if (!found.length) return 0;
  writeClipboard(found);
  const oldSel = selStore.get().sel;
  pushHistory();
  for (const { seg } of found) {
    const loc = locateSeg(seg);
    if (loc) segsOf(loc.ti).splice(loc.i, 1);
  }
  const arr = curSegs();
  const next = arr.length ? Math.min(Math.max(oldSel, 0), arr.length - 1) : -1;
  refreshAll();
  if (next >= 0) select(next);
  else deselect();
  markDirty();
  const n = found.length;
  toast(n > 1 ? `已剪切 ${n} 句字幕` : "已剪切 1 句字幕");
  return n;
}

/** 聚焦成片时，视频轨被删掉的区间对字幕也不可见，一并当作不可落位区避开 */
function cutGaps(): Span[] {
  if (!isFocused()) return [];
  const pieces = viewStore.get().pieces;
  if (!pieces?.length) return [];
  const gaps: Span[] = [];
  for (let i = 0; i < pieces.length - 1; i++) {
    const a = pieces[i].t1, b = pieces[i + 1].t0;
    if (b - a > EPS) gaps.push({ t0: a, t1: b });
  }
  return gaps;
}

function buildObstacles(ti: Ti, extra: Span[]): Span[] {
  const list: Span[] = segsOf(ti).map(s => ({ t0: s.t0, t1: s.t1 }));
  if (extra.length) list.push(...extra);
  if (list.length > 1) list.sort((a, b) => a.t0 - b.t0);
  return list;
}

interface MappedItem {
  relT0: number;
  relT1: number;
  ti: Ti;
  seg: Seg;
  obstacles: Span[];
}

function fitsAt(mapped: MappedItem[], start: number, v0: number, v1: number, maxRelT1: number): boolean {
  if (start < v0 - EPS || start + maxRelT1 > v1 + EPS) return false;
  for (const m of mapped) {
    const a = start + m.relT0;
    const b = start + m.relT1;
    for (const o of m.obstacles) {
      if (o.t1 <= a + EPS) continue;
      if (o.t0 >= b - EPS) break;
      return false;
    }
  }
  return true;
}

/** 在不越过目标时间点的前提下向左微调，适配右键点在空隙偏右或片尾附近的情形 */
function findBackwardCover(
  mapped: MappedItem[], wanted: number, v0: number, v1: number, maxRelT1: number,
): number | null {
  const maxStart = v1 - maxRelT1;
  let cur = Math.min(wanted, maxStart);
  const minStart = Math.max(v0, wanted - maxRelT1);
  if (cur < minStart - EPS) return null;
  for (let iter = 0; iter < 10000; iter++) {
    if (cur < minStart - EPS) return null;
    let nextCur = cur;
    for (const m of mapped) {
      const a = cur + m.relT0;
      const b = cur + m.relT1;
      for (const o of m.obstacles) {
        if (o.t1 <= a + EPS) continue;
        if (o.t0 >= b - EPS) break;
        const needed = o.t0 - m.relT1;
        if (needed < nextCur) nextCur = needed;
      }
    }
    if (nextCur >= cur - EPS) return Math.max(v0, cur);
    cur = nextCur;
  }
  return null;
}

/** 从 wanted 起往后扫，找第一个所有句都不与已有字幕重叠的落位起点 */
function findForwardStart(
  mapped: MappedItem[], wanted: number, v0: number, v1: number, maxRelT1: number,
): number | null {
  const maxStart = v1 - maxRelT1;
  let cur = Math.max(v0, wanted);
  if (cur > maxStart + EPS) return null;
  for (let iter = 0; iter < 10000; iter++) {
    if (cur > maxStart + EPS) return null;
    let nextCur = cur;
    for (const m of mapped) {
      const a = cur + m.relT0;
      const b = cur + m.relT1;
      for (const o of m.obstacles) {
        if (o.t1 <= a + EPS) continue;
        if (o.t0 >= b - EPS) break;
        const needed = o.t1 - m.relT0;
        if (needed > nextCur) nextCur = needed;
      }
    }
    if (nextCur <= cur + EPS) return Math.min(cur, maxStart);
    cur = nextCur;
  }
  return null;
}

/**
 * 粘贴剪贴板中的字幕句。
 * 默认落在当前激活轨、当前播放头位置；右键菜单可传入光标所在轨与时间。
 * 若目标位置与已有字幕冲突，先尝试在本空隙内向左微调覆盖落点，否则自动顺延到最近的空闲位置。
 */
export function pasteSegments(opts: { ti?: Ti; at?: number } = {}): number {
  if (!clipboard?.items.length) {
    toast("剪贴板为空");
    return 0;
  }
  const order = orderedTis();
  const maxOffset = Math.max(...clipboard.items.map(it => it.trackOffset));
  if (maxOffset >= order.length) {
    toast("轨道数量不足，无法粘贴跨轨字幕");
    return 0;
  }

  const rawTi = opts.ti ?? selStore.get().curTrack;
  const clampedTi = Math.min(Math.max(rawTi, -1), docStore.get().tracks.length - 1);
  const rawPos = tiPos(clampedTi);
  const basePos = Math.min(rawPos < 0 ? 0 : rawPos, order.length - 1 - maxOffset);

  const v = viewStore.get();
  const maxRelT1 = Math.max(...clipboard.items.map(it => it.relT1));
  if (v.t1 - v.t0 + EPS < maxRelT1) {
    toast("当前视图空间不足，无法粘贴");
    return 0;
  }

  const extraGaps = cutGaps();
  const obstacleCache = new Map<Ti, Span[]>();
  const obstaclesOf = (ti: Ti) => {
    let list = obstacleCache.get(ti);
    if (!list) { list = buildObstacles(ti, extraGaps); obstacleCache.set(ti, list); }
    return list;
  };

  const mapped: MappedItem[] = clipboard.items.map(it => {
    const ti = order[basePos + it.trackOffset];
    return {
      relT0: it.relT0,
      relT1: it.relT1,
      ti,
      seg: it.seg,
      obstacles: obstaclesOf(ti),
    };
  });

  const wanted = Math.max(v.t0, opts.at ?? playStore.get().t);
  const anchorObstacles = obstaclesOf(order[basePos]);
  const pointOccupied = anchorObstacles.some(o => wanted >= o.t0 + EPS && wanted < o.t1 - EPS);

  let placed: number | null = null;
  if (fitsAt(mapped, wanted, v.t0, v.t1, maxRelT1)) {
    placed = wanted;
  } else {
    if (!pointOccupied) {
      placed = findBackwardCover(mapped, wanted, v.t0, v.t1, maxRelT1);
    }
    if (placed == null) {
      placed = findForwardStart(mapped, wanted, v.t0, v.t1, maxRelT1);
    }
  }

  if (placed == null) {
    toast("此处空间不足，无法粘贴");
    return 0;
  }

  const dt = placed - clipboard.anchorT0;
  pushHistory();

  const pasted: Seg[] = [];
  const touched = new Set<Ti>();
  for (const m of mapped) {
    const seg = cloneSeg(m.seg, dt, v.t0, v.t1);
    const arr = segsOf(m.ti);
    arr.push(seg);
    touched.add(m.ti);
    pasted.push(seg);
  }

  // 按时间排序并消除浮点误差带来的极微小重叠（<= EPS）
  for (const ti of touched) {
    const arr = segsOf(ti);
    arr.sort((a, b) => a.t0 - b.t0);
    trimLaneOverlaps(arr);
  }

  setActiveTrack(order[basePos], { silent: true });
  refreshAll();
  setSelectionSegs(pasted);
  const earliest = pasted.reduce((a, b) => (a.t0 <= b.t0 ? a : b));
  seek(Math.min(earliest.t0 + 0.01, earliest.t1));
  markDirty();

  const shifted = Math.abs(placed - wanted) > 1e-3;
  const baseMsg = pasted.length > 1 ? `已粘贴 ${pasted.length} 句字幕` : "已粘贴 1 句字幕";
  toast(shifted ? baseMsg + "（已避开重叠）" : baseMsg);
  return pasted.length;
}
