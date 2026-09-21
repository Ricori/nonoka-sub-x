import { MIN_DUR } from '../constants';
import { bumpDoc, docStore, locateSeg, segsOf } from '../store/docStore';
import { playStore } from '../store/playStore';
import { markDirty } from '../store/saveStore';
import {
  curSegs, deselect, primaryInTrack, select, selStore, setActiveTrack,
} from '../store/selectionStore';
import { askModal, toast } from '../store/uiStore';
import { viewRange, viewStore } from '../store/viewStore';
import { pushHistory } from './history';
import { seek } from './playback';
import { syncSubs, syncSubsSoon } from './subtitles';
import { removeEffectsForTrack } from './effects';
import type { Lang, Seg } from '../types';
import { CN_STYLE, ORIGIN_STYLE } from '../../subtitles/styles';

/**
 * 改了句子就必须带上 syncSubs：预览是 libass 按整份 ASS 渲染的，只重画当前帧
 * 画的还是旧 track，时间/文本的改动一个都看不见。
 */
export function refreshAll() {
  bumpDoc();
  syncSubs();
}

/** 原文 lane 是否整体折叠（默认轨 + 所有自定义轨都隐藏） */
export function foldJa(): boolean {
  const { trackMeta, tracks } = docStore.get();
  return !!trackMeta?.ja.hidden && tracks.every(tr => tr.ja.hidden);
}

// ── 新建字幕（落在激活轨）───────────────
export function addSegmentAt(tc: number) {
  const arr = curSegs();
  if (arr.some(s => tc >= s.t0 && tc < s.t1)) { toast("此处已有字幕，请在空白处新建"); return; }
  // 新字幕只能落在本轨前后字幕之间的空隙里
  const v = viewStore.get();
  let lo = v.t0, hi = v.t1;
  arr.forEach(s => {
    if (s.t1 <= tc) lo = Math.max(lo, s.t1);
    if (s.t0 >= tc) hi = Math.min(hi, s.t0);
  });
  if (hi - lo < MIN_DUR) { toast("空隙太小，放不下新字幕"); return; }
  const t0 = Math.min(Math.max(tc, lo), hi - MIN_DUR);
  const t1 = Math.min(t0 + 2.0, hi);
  pushHistory();
  const seg: Seg = { t0, t1, ja: "", zh: "" };
  arr.push(seg);
  arr.sort((a, b) => a.t0 - b.t0);
  refreshAll();
  select(arr.indexOf(seg));
  seek(t0 + 0.01);
  markDirty();
  toast("已新建字幕，双击块可编辑文本");
}

// ── 删除字幕（批量、可跨轨）───────────────────────────
export function deleteSegment() {
  const { selSet, sel } = selStore.get();
  if (!selSet.size) return;
  // 每句各自定位所在轨再摘除（用句引用，删一句不影响其余定位）
  const victims = [...selSet];
  const oldSel = sel;
  pushHistory();
  let n = 0;
  victims.forEach(s => {
    const loc = locateSeg(s);
    if (loc) { segsOf(loc.ti).splice(loc.i, 1); n++; }
  });
  if (!n) return;
  // 删完把主选中落回 curTrack 原位附近（列表往上顶）
  const arr = curSegs();
  const next = arr.length ? Math.min(Math.max(oldSel, 0), arr.length - 1) : -1;
  refreshAll();
  if (next >= 0) select(next);
  else deselect();
  markDirty();
  toast(n > 1 ? ("已删除 " + n + " 句") : "已删除 1 句");
}

// ── 拆分：只按时间把一个轴切成两个，两侧都保留同一句原文与译文 ──
export function splitAtPlayhead() {
  const arr = curSegs();
  const { sel } = selStore.get();
  const s = arr[sel];
  if (!s) return;
  const t = playStore.get().t;
  if (t <= s.t0 + MIN_DUR || t >= s.t1 - MIN_DUR) { toast("请把当前位置移到本句中间再拆分"); return; }
  pushHistory();
  const b: Seg = { t0: t, t1: s.t1, ja: s.ja, zh: s.zh };
  if (s.words) b.words = s.words.map(w => ({ ...(w as object) }));
  // 两侧都留同一句原文，K 轴也跟着整份留下：字数没变，逐字特效照旧对得上
  if (s.k) b.k = s.k.map(u => ({ ...u }));
  if (s.low_conf) b.low_conf = true;
  s.t1 = t;
  arr.splice(sel + 1, 0, b);
  refreshAll(); markDirty();
  toast("已在当前位置拆分（两侧保留同一句）");
}

// ── 合并：words 双方都有才拼接，否则丢掉 ──
export function mergeNext() {
  const arr = curSegs();
  const { sel } = selStore.get();
  if (sel < 0 || sel >= arr.length - 1) { toast("已是最后一句"); return; }
  pushHistory();
  const a = arr[sel], b = arr[sel + 1];
  a.t1 = b.t1; a.ja += b.ja; a.zh += b.zh;
  if (a.words && b.words) a.words = a.words.concat(b.words);
  else delete a.words;
  // K 轴同理：只有两句都有才拼得出覆盖整句的逐字时间
  if (a.k && b.k) a.k = a.k.concat(b.k);
  else delete a.k;
  if (b.low_conf) a.low_conf = true;
  arr.splice(sel + 1, 1);
  refreshAll(); markDirty();
  toast("已与下一句合并");
}

/** 检查器上的 ±0.1s */
export function nudge(which: "in" | "out", d: number) {
  const arr = curSegs();
  const { sel } = selStore.get();
  const s = arr[sel];
  if (!s) return;
  const v = viewStore.get();
  pushHistory();
  const lo = sel > 0 ? arr[sel - 1].t1 : v.t0;
  const hi = sel < arr.length - 1 ? arr[sel + 1].t0 : v.t1;
  if (which === "in") s.t0 = Math.min(Math.max(s.t0 + d, lo), s.t1 - MIN_DUR);
  else s.t1 = Math.max(Math.min(s.t1 + d, hi), s.t0 + MIN_DUR);
  refreshAll(); markDirty();
}

/** I / O：把入点或出点设到当前位置 */
export function nudgeToPlayhead(which: "in" | "out") {
  const arr = curSegs();
  const { sel } = selStore.get();
  const s = arr[sel];
  if (!s) return;
  const v = viewStore.get();
  const t = playStore.get().t;
  const lo = sel > 0 ? arr[sel - 1].t1 : v.t0;
  const hi = sel < arr.length - 1 ? arr[sel + 1].t0 : v.t1;
  const canIn = which === "in" && t < s.t1 - MIN_DUR && t >= lo;
  const canOut = which === "out" && t > s.t0 + MIN_DUR && t <= hi;
  if (!canIn && !canOut) return;   // 无实际改动就不入撤销栈
  pushHistory();
  if (canIn) s.t0 = t;
  if (canOut) s.t1 = t;
  refreshAll(); markDirty();
}

/** V：当前句出点延长 300ms，最多到下一句入点（末句到视频结尾） */
export function extendCurrent() {
  const arr = curSegs();
  const { sel } = selStore.get();
  const s = arr[sel];
  if (!s) return;
  const hi = sel < arr.length - 1 ? arr[sel + 1].t0 : viewStore.get().t1;
  const nt1 = Math.min(s.t1 + 0.3, hi);
  if (nt1 <= s.t1 + 1e-9) { toast("已顶到下一句，无法再延长"); return; }
  pushHistory();
  s.t1 = nt1;
  refreshAll(); markDirty();
}

/**
 * 编辑原文即视为人已审过：低置信标记清除；文本与 words 不再对齐时
 * 拆分逻辑会自动退回按时间比例切，无需在这里丢 words
 */
export function setSegText(lang: Lang, value: string) {
  const { sel } = selStore.get();
  const s = curSegs()[sel];
  if (!s) return;
  s[lang] = value;
  if (lang === "ja") delete s.low_conf;
  bumpDoc();
  select(sel, { scroll: false });
  syncSubsSoon();
  markDirty();
}

// 上一句/下一句在切片里只在区间内走（sel 未定时从区间头尾起步）
export function gotoPrev() {
  const a = curSegs(), [vA, vB] = viewRange(a);
  if (vB <= vA) return;
  const { sel } = selStore.get();
  const i = Math.max(sel < 0 ? vB - 1 : sel - 1, vA);
  select(i); seek(a[i].t0 + 0.01);
}

export function gotoNext() {
  const a = curSegs(), [vA, vB] = viewRange(a);
  if (vB <= vA) return;
  const { sel } = selStore.get();
  const i = Math.min(sel < 0 ? vA : sel + 1, vB - 1);
  select(i); seek(a[i].t0 + 0.01);
}

// ── 轨道显隐 ──────────────────────────────────────────────
export function toggleDefaultHidden(lang: Lang) {
  const tm = docStore.get().trackMeta;
  if (!tm) return;
  tm[lang].hidden = !tm[lang].hidden;
  refreshAll(); markDirty();
}

export function toggleTrackHidden(ti: number, lang: Lang) {
  const tr = docStore.get().tracks[ti];
  if (!tr) return;
  tr[lang].hidden = !tr[lang].hidden;
  refreshAll(); markDirty();
}

/**
 * 一键隐藏所有原文轨。轨道多了以后原文 lane 占掉一半高度。等同于逐条点眼睛：
 * hidden 置位存服务端，预览与导出同步不出原文
 */
export function toggleFoldJa() {
  const { trackMeta, tracks } = docStore.get();
  if (!trackMeta) return;
  const hide = !foldJa();
  trackMeta.ja.hidden = hide;
  tracks.forEach(tr => { tr.ja.hidden = hide; });
  refreshAll(); markDirty();
  toast(hide ? "已隐藏所有原文轨（预览与导出同步不出原文）" : "已显示所有原文轨");
}

// ── 轨道增删（换轨靠时间轴上下拖字幕块，见 useLaneDrag）──────
export async function newTrack() {
  const { tracks } = docStore.get();
  const name = await askModal({
    title: "新建轨道",
    hint: "轨道名称（说话人 / 注释等）",
    value: "轨道 " + (tracks.length + 1),
  });
  if (name == null || !String(name).trim()) return;
  pushHistory();
  // 新轨默认只出译文，原文 lane 先藏着；这样也不会破坏「所有原文 lane 都 hidden」
  // 推导出来的「隐藏原文轨」
  tracks.push({
    id: "t" + Date.now().toString(36) + Math.random().toString(36).slice(2, 6),
    name: String(name).trim(),
    ja: { hidden: true, style: ORIGIN_STYLE },
    zh: { hidden: false, style: CN_STYLE },
    hja: 44, hzh: 44, segs: [],
  });
  refreshAll();
  setActiveTrack(tracks.length - 1, { silent: true });
  markDirty();
  toast("已新建轨道「" + String(name).trim() + "」");
}

export async function deleteTrack(ti: number) {
  const tracks = docStore.get().tracks;
  const tr = tracks[ti];
  if (!tr) return;
  const okDel = await askModal({
    title: `删除轨道「${tr.name}」？`,
    hint: `连同它的 ${tr.segs.length} 句字幕一起删除，可用 Ctrl+Z 撤销。`,
    okLabel: "删除轨道", danger: true,
  });
  if (!okDel || docStore.get().tracks[ti] !== tr) return;   // 取消，或等待期间轨道已变动
  pushHistory();
  removeEffectsForTrack(tr.id);
  tracks.splice(ti, 1);
  const { curTrack } = selStore.get();
  if (curTrack === ti) selStore.set({ curTrack: -1, sel: -1 });
  else if (curTrack > ti) selStore.set({ curTrack: curTrack - 1 });
  refreshAll();
  selStore.set({ sel: primaryInTrack() });
  markDirty();
  toast("已删除轨道「" + tr.name + "」");
}

/** 轨道设置弹层里改名/换绑样式 */
export function renameTarget(name: string, target: { kind: "track"; ti: number } | { kind: "default" }) {
  const d = docStore.get();
  if (target.kind === "track") { if (d.tracks[target.ti]) d.tracks[target.ti].name = name; }
  else if (d.trackMeta) d.trackMeta.name = name;
  bumpDoc(); markDirty();
}

export function bindStyle(lang: Lang, value: string, target: { kind: "track"; ti: number } | { kind: "default" }) {
  const d = docStore.get();
  if (target.kind === "track") { if (d.tracks[target.ti]) d.tracks[target.ti][lang].style = value || null; }
  else if (d.trackMeta) d.trackMeta[lang].style = value;
  refreshAll(); markDirty();
}
