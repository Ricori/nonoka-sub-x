import {
  MIN_PIECE, addRange, intersectRange, joinPieces, normalizePieces, pieceAt, piecesDuration, splitAt, subtractRange,
} from '../../subtitles/pieces.ts';
import type { Piece } from '../../subtitles/pieces.ts';
import { layoutStore } from '../store/layoutStore';
import { playStore } from '../store/playStore';
import { saveEdit } from '../store/saveStore';
import { curSegs, deselect, selStore } from '../store/selectionStore';
import { innerLeft } from '../store/tlStore';
import { toast } from '../store/uiStore';
import { clearVsel, vselStore } from '../store/vselStore';
import {
  applyInnerWidth, curPieces, ensureBlkWin, fitPps, isFocused, setDuration, syncZoomRange, tOf, viewStore,
} from '../store/viewStore';
import { fmt, fmtLen } from '../utils';
import { armPending, commitPending, disarmPending, pushHistory } from './history';
import { seek } from './playback';

// 视频轨剪辑：只改 viewStore.pieces（原片时间上的若干段），字幕数据一个字都不动。
// 每一步都进撤销栈、立刻落盘到本地 library.json。

/** 导出用的片段：首尾相接的并成一段；null = 没剪过，导出整片 */
export function exportPieces() {
  const pieces = viewStore.get().pieces;
  return pieces ? joinPieces(pieces) : null;
}

/** 剪过的成片导出时文件名带的后缀 */
export const EDIT_SUFFIX = " - 剪辑";

/** 给导出窗口和提示用的一句话：几段、多长、删掉了多少 */
export function editSummary(): string {
  const { duration } = viewStore.get();
  const pieces = exportPieces();
  if (!pieces) return `完整片　共 ${fmt(duration)}`;
  const kept = piecesDuration(pieces);
  return `剪辑成片　${pieces.length} 段　共 ${fmt(kept)}（删去 ${fmt(Math.max(0, duration - kept))}）`;
}

/** 只剩一段且铺满整片，就等于没剪过（切分过但没删的仍算剪辑，切分点要留着） */
const isWhole = (pieces: Piece[], duration: number) =>
  pieces.length === 1 && pieces[0].t0 <= MIN_PIECE && pieces[0].t1 >= duration - MIN_PIECE;

/** 片段变了（或进出聚焦）之后重算视图窗口：宽度、缩放下限、块窗口，播放头钳回窗口里 */
function refreshView(fit = false) {
  setDuration(viewStore.get().duration);
  if (fit) viewStore.set({ pps: fitPps() });
  applyInnerWidth();
  syncZoomRange();
  ensureBlkWin(true);
  seek(playStore.get().t);
}

/** 成片视图只供预览：视频轨的剪辑一律回完整片做（字幕照常可改） */
export const editLocked = () => isFocused();
const LOCKED_HINT = "成片仅供预览，要剪辑视频轨请先回到完整片";

/**
 * 写入新片段。null 或铺满整片 = 恢复成没剪过。noHistory：调用方已经自己入过栈
 * （拖边缘在首次移动时入栈）。返回是否真的写了——全删光、或在成片视图里剪都不允许。
 * 所有剪辑都从这里过，成片只读的闸也就只设在这一处（撤销/重做走 restorePieces，不受限）。
 */
export function setPieces(next: Piece[] | null, opt: { noHistory?: boolean } = {}): boolean {
  if (editLocked()) { toast(LOCKED_HINT); return false; }
  const { duration } = viewStore.get();
  let pieces = next ? normalizePieces(next, duration) : null;
  if (pieces && !pieces.length) { toast("至少要保留一段视频"); return false; }
  if (pieces && isWhole(pieces, duration)) pieces = null;
  if (!opt.noHistory) pushHistory();
  // 剪辑只在完整片里发生（成片被上面的闸挡住了），剪完也留在完整片，不自动切去成片
  viewStore.set({ pieces });
  clearVsel();
  refreshView();
  saveEdit();
  return true;
}

/** 撤销/重做还原片段：不入栈（历史自己管），但要落盘和重算视图 */
export function restorePieces(pieces: Piece[] | null) {
  const cur = viewStore.get().pieces;
  if (JSON.stringify(cur) === JSON.stringify(pieces)) return;
  viewStore.set({ pieces: pieces && pieces.map(p => ({ ...p })), focus: pieces ? viewStore.get().focus : false });
  clearVsel();
  refreshView();
  saveEdit();
}

export function cutAtPlayhead() {
  if (editLocked()) { toast(LOCKED_HINT); return; }
  const t = playStore.get().t;
  const cur = curPieces();
  const next = splitAt(cur, t);
  if (next.length === cur.length) {
    toast(pieceAt(cur, t) < 0 ? "播放头在删掉的部分里，没有可切分的片段" : "离片段边缘太近，切不出新片段");
    return;
  }
  setPieces(next);
}

export function deletePiece(i: number) {
  const cur = curPieces();
  if (!cur[i]) return;
  if (setPieces(cur.filter((_, k) => k !== i))) toast(`已删除片段（${fmtLen(cur[i].t1 - cur[i].t0)}）· Ctrl+Z 撤销`);
}

export function deleteRange(a: number, b: number) {
  if (b - a < MIN_PIECE) return;
  setPieces(subtractRange(curPieces(), a, b));
}

/** 只保留 [a, b)：相当于原来的「新建切片」。做完仍停在完整片，要看成片自己切过去 */
export function keepRange(a: number, b: number) {
  if (b - a < MIN_PIECE) return;
  setPieces(intersectRange(curPieces(), a, b));
}

export const keepPiece = (i: number) => { const p = curPieces()[i]; if (p) keepRange(p.t0, p.t1); };

export function restoreRange(a: number, b: number) {
  setPieces(addRange(curPieces(), a, b));
}

/** 有没有首尾相接、能并成一段的片段（切分点） */
export const hasContiguous = () => joinPieces(curPieces()).length < curPieces().length;

/** 把首尾相接的片段并成一段：切分点全部去掉，删掉的部分不动 */
export function mergeContiguous() {
  const cur = curPieces();
  const next = joinPieces(cur);
  if (next.length === cur.length) { toast("没有首尾相接的片段可合并"); return; }
  if (setPieces(next)) toast(`已合并连续片段（${cur.length} 段 → ${next.length} 段）· Ctrl+Z 撤销`);
}

export function resetVideoEdit() {
  if (!viewStore.get().pieces) return;
  setPieces(null);
  toast("已恢复整片 · Ctrl+Z 撤销");
}

/** 选中字幕句覆盖的时间并集 */
function selectedSpans(): Piece[] {
  return normalizePieces([...selStore.get().selSet].map(s => ({ t0: s.t0, t1: s.t1 })));
}

export function deleteSelectedSegs() {
  if (editLocked()) { toast(LOCKED_HINT); return; }
  const spans = selectedSpans();
  if (!spans.length) { toast("先选中要删掉的字幕块"); return; }
  let next = curPieces();
  for (const s of spans) next = subtractRange(next, s.t0, s.t1);
  setPieces(next);
}

export function keepSelectedSegs() {
  if (editLocked()) { toast(LOCKED_HINT); return; }
  const spans = selectedSpans();
  if (!spans.length) { toast("先选中要保留的字幕块"); return; }
  keepRange(spans[0].t0, spans[spans.length - 1].t1);
}

/** Delete 键：视频轨上有选中就删它，返回 false 表示交给字幕删除 */
export function deleteVsel(): boolean {
  const v = vselStore.get().vsel;
  if (!v) return false;
  if (v.kind === "piece") deletePiece(v.i);
  else deleteRange(v.t0, v.t1);
  return true;
}

export function toggleFocus() {
  if (!viewStore.get().pieces) { toast("还没剪过视频轨：先在视频轨上切分、删掉不要的部分"); return; }
  viewStore.set({ focus: !viewStore.get().focus });
  // 成片里视频轨不可操作：带过去的片段选中会让 Delete 去删片段而不是字幕
  clearVsel();
  refreshView(true);
}

export function selectPiece(i: number) {
  deselect();
  vselStore.set({ vsel: { kind: "piece", i } });
}

// ── 拖片段边缘 ────────────────────────────────────────────────────
/** 吸附目标：播放头、当前轨字幕边界、其它片段的边（拖动开始时的位置，自己那段不算） */
function snapT(t: number, others: Piece[]): number {
  if (!layoutStore.get().snap) return t;
  const tol = 8 / viewStore.get().pps;
  let best = t, bestD = tol;
  const consider = (c: number) => { const d = Math.abs(c - t); if (d < bestD) { bestD = d; best = c; } };
  consider(playStore.get().t);
  for (const s of curSegs()) { consider(s.t0); consider(s.t1); }
  for (const p of others) { consider(p.t0); consider(p.t1); }
  return best;
}

/**
 * 拖片段边缘改范围（不挪别的片段，也就不会动字幕）。只能拖到相邻片段的边为止；
 * 首次移动才入栈，松手再统一落盘和重算视图——拖动途中重算的话聚焦窗口会跟着跳。
 */
export function dragPieceEdge(ev: React.PointerEvent, i: number, side: "l" | "r") {
  ev.preventDefault();
  ev.stopPropagation();
  if (editLocked()) return;
  const el = ev.currentTarget as HTMLElement;
  el.setPointerCapture(ev.pointerId);
  const start = curPieces().map(p => ({ ...p }));
  const self = start[i];
  if (!self) return;
  const others = start.filter((_, k) => k !== i);
  const lo = side === "l" ? (start[i - 1]?.t1 ?? 0) : self.t0 + MIN_PIECE;
  const hi = side === "l" ? self.t1 - MIN_PIECE : (start[i + 1]?.t0 ?? viewStore.get().duration);
  armPending();
  let moved = false;
  const move = (e: PointerEvent) => {
    if (!moved) { moved = true; commitPending(); }
    const t = Math.min(Math.max(snapT(tOf(e.clientX - innerLeft()), others), lo), hi);
    const next = start.map(p => ({ ...p }));
    if (side === "l") next[i].t0 = t; else next[i].t1 = t;
    viewStore.set({ pieces: next });
  };
  const up = () => {
    el.removeEventListener("pointermove", move);
    el.removeEventListener("pointerup", up);
    el.removeEventListener("pointercancel", up);
    if (!moved) { disarmPending(); return; }
    setPieces(viewStore.get().pieces, { noHistory: true });
  };
  el.addEventListener("pointermove", move);
  el.addEventListener("pointerup", up);
  el.addEventListener("pointercancel", up);
}
