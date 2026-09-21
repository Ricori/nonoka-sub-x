import { HISTORY_MAX } from '../constants';
import { setStyleSheet } from '../ass';
import { bumpDoc, docStore } from '../store/docStore';
import { playStore } from '../store/playStore';
import { markDirty } from '../store/saveStore';
import { curSegs, restoreSelection, select, selStore } from '../store/selectionStore';
import { toast } from '../store/uiStore';
import { seek } from './playback';
import { syncSubs } from './subtitles';
import type { Seg, Snapshot, Track } from '../types';

/**
 * 撤销/重做历史。改动前存一份深拷贝快照，撤销即弹栈还原；文本编辑按「聚焦→首次输入」
 * 合成一步，拖动按「首次移动」入栈。有新改动就清空重做栈（分支了就没得重做）。
 */
const history: Snapshot[] = [];
const redoStack: Snapshot[] = [];

/** 文本编辑：聚焦时暂存的编辑前状态，首次 input 才真正入栈 */
let pendingSnap: Snapshot | null = null;

export function resetHistory() {
  history.length = 0;
  redoStack.length = 0;
  pendingSnap = null;
}

function cloneSegs(arr: Seg[]): Seg[] {
  return arr.map(s => {
    const o: Seg = { t0: s.t0, t1: s.t1, ja: s.ja, zh: s.zh };
    if (s.k) o.k = s.k;               // K 轴同样只被整体替换，浅引用即可
    if (s.words) o.words = s.words;   // words 只被整体替换、不原地改，浅引用即可
    if (s.low_conf) o.low_conf = true;
    return o;
  });
}

export function snapshot(): Snapshot {
  const d = docStore.get();
  const sel = selStore.get();
  return {
    segs: cloneSegs(d.segs),
    tracks: d.tracks.map(tr => ({
      ...tr,
      ja: { ...tr.ja }, zh: { ...tr.zh },   // 隐藏/绑样式也随快照还原
      segs: cloneSegs(tr.segs),
    })) as Track[],
    trackMeta: d.trackMeta ? {
      ...d.trackMeta,
      ja: { ...d.trackMeta.ja }, zh: { ...d.trackMeta.zh },
    } : null,
    effects: d.effects.map(effect => ({
      ...effect,
      target: { ...effect.target },
      params: { ...effect.params },
    })),
    styles: d.styles,   // 不可变字符串，快照只多一个引用
    curTrack: sel.curTrack, sel: sel.sel, t: playStore.get().t,
  };
}

function pushSnap(snap?: Snapshot) {
  history.push(snap || snapshot());
  if (history.length > HISTORY_MAX) history.shift();
  redoStack.length = 0;   // 有了新改动，原来的重做分支作废
}

/** 改动前调用：记录当前状态供撤销 */
export const pushHistory = () => pushSnap();

/** 文本框聚焦时暂存编辑前状态（每个聚焦会话只落一次栈） */
export const armPending = () => { pendingSnap = snapshot(); };
export const disarmPending = () => { pendingSnap = null; };

/** 文本编辑：首次 input 时把暂存的编辑前状态落栈 */
export function commitPending() {
  if (pendingSnap) { pushSnap(pendingSnap); pendingSnap = null; }
}

// 撤销与重做只差「从哪个栈弹、把当前状态压进哪个栈」，共用一个还原过程
function applySnap(snap: Snapshot) {
  docStore.set({
    segs: snap.segs,
    tracks: snap.tracks || [],
    trackMeta: snap.trackMeta ?? null,
    effects: snap.effects ?? [],
    styles: snap.styles,
  });
  // 渲染读的是 ass.ts 的单例：必须赶在下面 syncSubs 之前换回去，否则重画用的还是新样式
  setStyleSheet(snap.styles);
  const curTrack = Math.min(snap.curTrack != null ? snap.curTrack : -1, snap.tracks.length - 1);
  restoreSelection(curTrack, -1);
  const sel = Math.min(snap.sel, curSegs().length - 1);
  bumpDoc();
  syncSubs();
  if (sel >= 0) select(sel, { scroll: true });
  seek(snap.t);
  markDirty();
}

export function undo() {
  const snap = history.pop();
  if (!snap) { toast("没有可撤销的操作"); return; }
  redoStack.push(snapshot());   // 先留住当前状态，重做时弹回来
  applySnap(snap);
  toast("已撤销");
}

export function redo() {
  const snap = redoStack.pop();
  if (!snap) { toast("没有可重做的操作"); return; }
  history.push(snapshot());     // 直接压栈：走 pushSnap 会把 redoStack 清空
  if (history.length > HISTORY_MAX) history.shift();
  applySnap(snap);
  toast("已重做");
}
