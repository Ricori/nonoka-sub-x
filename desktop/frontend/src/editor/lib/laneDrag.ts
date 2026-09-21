import { MIN_DUR } from '../constants';
import { bumpDoc, locateSeg, orderedTis, segsOf, tiPos, trackName } from '../store/docStore';
import { dragStore } from '../store/dragStore';
import { layoutStore } from '../store/layoutStore';
import { playStore } from '../store/playStore';
import { markDirty } from '../store/saveStore';
import {
  curSegs, deselect, primaryInTrack, select, selectRange, selStore,
  setActiveTrack, setSelectionSegs, toggleSel,
} from '../store/selectionStore';
import { innerLeft, tlInner } from '../store/tlStore';
import { toast } from '../store/uiStore';
import { dtAt, freezeBlocks, tAtClientX, tOf, viewStore } from '../store/viewStore';
import { refreshAll } from './edits';
import { pushHistory } from './history';
import { endScrub, isScrubbing, resetScrubPlayed, scrubBlip, scrubSound, seek } from './playback';
import type { Seg, Ti } from '../types';

// 时间轴上的块拖动 / 拉伸 / 框选。拖动期间直接改句对象的 t0/t1 并 bumpDoc 重渲染：
// 块是按视口虚拟化的（一屏一两百个），每帧重渲染这点量比以前手搬 DOM 还省心，
// 而且跨轨时不用把 DOM 在 lane 之间搬家——数据一挪，块自然就画到新轨上了。

/** 鼠标纵向落在哪条轨（-1=默认轨）；折成 0 高的行不算，不在任何 lane 上返回 null */
export function laneTiAt(clientY: number): Ti | null {
  const inner = tlInner();
  if (!inner) return null;
  for (const el of Array.from(inner.querySelectorAll<HTMLElement>(".lane"))) {
    const r = el.getBoundingClientRect();
    if (r.height > 0 && clientY >= r.top && clientY < r.bottom) return +(el.dataset.ti ?? "-1");
  }
  return null;
}

/** 把一句从一条轨挪到另一条（数据层面），目标轨重新按 t0 排好 */
function moveSegToTrack(s: Seg, from: Ti, to: Ti) {
  if (from === to) return;
  const src = segsOf(from);
  const k = src.indexOf(s);
  if (k >= 0) src.splice(k, 1);
  const dst = segsOf(to);
  dst.push(s);
  dst.sort((a, b) => a.t0 - b.t0);
}

const setDropTi = (ti: Ti | null) => dragStore.set({ dropTi: ti });

/**
 * 整组拖动（仿 Pr）：横向所有句按同一 dt 平移，dt 被每句到目标轨上非选中相邻句的空隙
 * 共同框死；纵向整组一起换轨，只有每一句都放得下才真正换过去，否则维持上一帧的落位。
 */
function groupMove(startEv: React.PointerEvent, originTi: Ti, grabbedSeg: Seg) {
  const { selSet } = selStore.get();
  // 起手快照：每句的原始时间与原始所在轨
  const items = [...selSet].map(s => {
    const loc = locateSeg(s);
    if (!loc) return null;
    return { s, ti0: loc.ti, t0: s.t0, t1: s.t1, curTi: loc.ti };
  }).filter(Boolean) as { s: Seg; ti0: Ti; t0: number; t1: number; curTi: Ti }[];
  if (items.length < 2) return;
  const gMin = Math.min(...items.map(it => it.t0));
  const gMax = Math.max(...items.map(it => it.t1));

  /**
   * 某个纵向偏移 dPos 下，整组允许的 dt 区间；任一句目标轨不存在、或本句原始时间
   * 跨度在目标轨里压到了未选中句，则该 dPos 不可行，返回 null
   */
  const dtRange = (dPos: number) => {
    let maxLeft = Infinity, maxRight = Infinity;
    const order = orderedTis();
    const v = viewStore.get();
    for (const it of items) {
      const tp = tiPos(it.ti0) + dPos;
      if (tp < 0 || tp >= order.length) return null;
      const tiT = order[tp];
      let lo = v.t0, hi = v.t1;
      for (const o of segsOf(tiT)) {
        if (selSet.has(o)) continue;
        if (o.t1 <= it.t0) lo = Math.max(lo, o.t1);
        else if (o.t0 >= it.t1) hi = Math.min(hi, o.t0);
        else return null;   // 原始跨度就压在未选中句上 → 换不到这条轨
      }
      maxLeft = Math.min(maxLeft, it.t0 - lo);
      maxRight = Math.min(maxRight, hi - it.t1);
    }
    return { lo: -maxLeft, hi: maxRight };
  };

  // 吸附候选：播放头 + 各源轨里未选中句的入/出点
  const snapDt = (dtWanted: number) => {
    if (!layoutStore.get().snap) return dtWanted;
    const pps = viewStore.get().pps;
    const tol = 8 / pps, cands = [playStore.get().t];
    new Set(items.map(it => it.ti0)).forEach(ti2 =>
      segsOf(ti2).forEach(o => { if (!selSet.has(o)) cands.push(o.t0, o.t1); }));
    let bestD = tol, best = dtWanted;
    for (const c of cands) for (const edge of [gMin + dtWanted, gMax + dtWanted]) {
      const d = Math.abs(edge - c);
      if (d < bestD) { bestD = d; best = dtWanted + (c - edge); }
    }
    return best;
  };

  const place = (dt: number, dPos: number) => {
    const order = orderedTis();
    const touched = new Set<Ti>();
    items.forEach(it => {
      const tiT = order[tiPos(it.ti0) + dPos];
      if (tiT !== it.curTi) {
        moveSegToTrack(it.s, it.curTi, tiT);
        touched.add(it.curTi); touched.add(tiT);
        it.curTi = tiT;
      }
      it.s.t0 = it.t0 + dt; it.s.t1 = it.t1 + dt;
    });
    if (touched.size) touched.forEach(ti2 => segsOf(ti2).sort((a, b) => a.t0 - b.t0));
    setDropTi(dPos !== 0 ? items[0].curTi : null);
    bumpDoc();
  };

  const inner = tlInner();
  const startX = startEv.clientX;
  inner?.setPointerCapture(startEv.pointerId);
  freezeBlocks(true);   // 拖动中窗口不再随滚动重算
  let moved = false, curDPos = 0;
  const onMove = (ev: PointerEvent) => {
    if (!moved) pushHistory();
    moved = true;
    const dtWanted = snapDt(dtAt(gMin, ev.clientX - startX));
    const hoverTi = laneTiAt(ev.clientY);
    let desired = curDPos;
    if (hoverTi != null) { const p = tiPos(hoverTi); if (p >= 0) desired = p - tiPos(originTi); }
    // 先试鼠标想去的纵向档位，不行就退回上一帧合法档位（横向始终跟手，clamp 到合法区间）
    for (const cand of (desired === curDPos ? [curDPos] : [desired, curDPos])) {
      const rg = dtRange(cand);
      if (!rg) continue;
      place(Math.max(rg.lo, Math.min(rg.hi, dtWanted)), cand);
      curDPos = cand;
      break;
    }
  };
  const onUp = () => {
    inner?.removeEventListener("pointermove", onMove);
    inner?.removeEventListener("pointerup", onUp);
    inner?.removeEventListener("pointercancel", onUp);
    freezeBlocks(false);
    setDropTi(null);
    if (!moved) {   // 只点没拖：收拢成只选被点那句（仿 Pr）
      const loc = locateSeg(grabbedSeg);
      if (loc) { setActiveTrack(loc.ti, { silent: true }); select(loc.i); }
      return;
    }
    // 列表跟到被抓那句的最终落轨
    const gLoc = locateSeg(grabbedSeg);
    if (gLoc) selStore.set({ curTrack: gLoc.ti });
    refreshAll();
    selStore.set({ sel: primaryInTrack() });
    markDirty();
    if (curDPos !== 0) toast("已跨轨移动 " + items.length + " 句");
  };
  inner?.addEventListener("pointermove", onMove);
  inner?.addEventListener("pointerup", onUp);
  inner?.addEventListener("pointercancel", onUp);
}

/**
 * lane 上按下：命中块就选中/拖动/拉伸；空白交给 inner 上的框选（见 startMarquee）。
 * ti：-1=默认轨；lang：本 lane 是原文还是译文。
 */
export function onLanePointerDown(e: React.PointerEvent, ti: Ti) {
  const blk = (e.target as HTMLElement).closest<HTMLElement>(".blk");
  if (!blk) return;
  const i = +(blk.dataset.i ?? "-1");
  const gseg = segsOf(ti)[i];
  const { selSet, sel } = selStore.get();
  // 右键只为叫出菜单：命中的块已在选区里就原样保留（整组做切片全靠它），
  // 不在选区里才改选这一句。绝不能往下走到 select(i)——那会让多选一右键就塌成一句
  if (e.button !== 0) {
    if (gseg && !selSet.has(gseg)) { setActiveTrack(ti, { silent: true }); select(i); }
    return;
  }
  e.preventDefault();
  const mode = (e.target as HTMLElement).dataset.h || "move";
  // 拖动一个已在多选内的块 → 整组一起走（可跨轨）。用句引用判定，且必须先于
  // setActiveTrack——被抓的块可能在别的轨上，切轨会重算主选中但不该扰动这次拖动
  if (mode === "move" && selSet.size > 1 && gseg && selSet.has(gseg)) { groupMove(e, ti, gseg); return; }
  setActiveTrack(ti, { silent: true });
  // 修饰点选：Ctrl/⌘ 增减单句，Shift 从主选中连续扩选（都不触发拖动）
  if (mode === "move" && (e.ctrlKey || e.metaKey)) { toggleSel(i); return; }
  if (mode === "move" && e.shiftKey && sel >= 0 && sel < curSegs().length) { selectRange(sel, i); return; }
  select(i);

  const arr = segsOf(ti);
  const s = arr[i];
  if (!s) return;
  const start = { x: e.clientX, t0: s.t0, t1: s.t1 };
  const inner = tlInner();
  // 指针捕获挂在 inner 上而不是块上：跨轨时块要在 lane 之间换位置，
  // 捕获元素一旦离开原父节点就会丢捕获，inner 全程不动最稳
  inner?.setPointerCapture(e.pointerId);
  freezeBlocks(true);

  const snapTo = (v: number, arr2: Seg[]) => {
    if (!layoutStore.get().snap) return v;
    const cands = [playStore.get().t];
    arr2.forEach(o => { if (o !== s) cands.push(o.t0, o.t1); });
    const tol = 8 / viewStore.get().pps;
    for (const c of cands) if (Math.abs(v - c) < tol) return c;
    return v;
  };

  // 同轨相邻字幕不可重叠：活动范围被前一句出点 / 后一句入点框死
  const vw = viewStore.get();
  const srcGap = {
    lo: i > 0 ? arr[i - 1].t1 : vw.t0,
    hi: i < arr.length - 1 ? arr[i + 1].t0 : vw.t1,
  };

  // 目标轨里光标处那段空隙；光标压在别的字幕上、或空隙塞不下本句就返回 null
  const gapIn = (ti2: Ti, tc: number, dur: number) => {
    const v = viewStore.get();
    let lo = v.t0, hi = v.t1;
    for (const o of segsOf(ti2)) {
      if (o === s) continue;
      if (o.t1 <= tc) lo = Math.max(lo, o.t1);
      else if (o.t0 >= tc) hi = Math.min(hi, o.t0);
      else return null;
    }
    return hi - lo >= dur ? { lo, hi } : null;
  };

  let dstTi = ti, gap = srcGap;
  let moved = false;

  const onMove = (ev: PointerEvent) => {
    if (!moved) pushHistory();   // 首次移动前存快照（纯点击不入栈）
    moved = true;
    const dx = ev.clientX - start.x;
    // 以被拖的那条边为锚换算：聚焦成片时跨过拼接点要连删掉的部分一起跳过
    const dt = dtAt(mode === "r" ? start.t1 : start.t0, dx);
    if (mode === "move") {
      const dur = start.t1 - start.t0;
      // 跨轨：光标压到别的轨上，且那儿的空隙放得下，就把这句挪过去
      const hover = laneTiAt(ev.clientY);
      if (hover != null && hover !== dstTi) {
        const g = gapIn(hover, tAtClientX(ev.clientX), dur);
        if (g) {
          gap = g;
          moveSegToTrack(s, dstTi, hover);
          dstTi = hover;
          setDropTi(dstTi !== ti ? dstTi : null);
        }
      }
      const nt0 = Math.min(Math.max(snapTo(start.t0 + dt, segsOf(dstTi)), gap.lo), gap.hi - dur);
      s.t0 = nt0; s.t1 = nt0 + dur;
    } else if (mode === "l") {
      s.t0 = Math.min(Math.max(snapTo(start.t0 + dt, arr), srcGap.lo), s.t1 - MIN_DUR);
    } else {
      s.t1 = Math.max(Math.min(snapTo(start.t1 + dt, arr), srcGap.hi), s.t0 + MIN_DUR);
    }
    bumpDoc();
  };
  const onUp = () => {
    inner?.removeEventListener("pointermove", onMove);
    inner?.removeEventListener("pointerup", onUp);
    inner?.removeEventListener("pointercancel", onUp);
    freezeBlocks(false);
    setDropTi(null);
    if (!moved) return;
    if (dstTi !== ti) {
      // 落到别的轨：列表跟着切过去
      setActiveTrack(dstTi, { silent: true });
      const dst = segsOf(dstTi);
      dst.sort((a, b) => a.t0 - b.t0);
      refreshAll(); markDirty();
      select(dst.indexOf(s));
      toast("已移到「" + trackName(dstTi) + "」");
      return;
    }
    arr.sort((a, b) => a.t0 - b.t0);
    refreshAll(); markDirty();
    select(arr.indexOf(s), { scroll: false });
  };
  inner?.addEventListener("pointermove", onMove);
  inner?.addEventListener("pointerup", onUp);
  inner?.addEventListener("pointercancel", onUp);
}

/**
 * 框选：轨道空白处拖出矩形，多选扫到的字幕块（跨轨、跨 lane 都算）。命中按时间区间算而不
 * 遍历块 DOM——屏幕外的句被虚拟化掉了没有 DOM，但逻辑上照样该被框到。
 * 按住 Ctrl/⌘：追加模式——点空白不动选区（补选时误点不至于前功尽弃），拖框则并入原选区。
 */
export function startMarquee(startEv: React.PointerEvent, ti: Ti) {
  startEv.preventDefault();
  const additive = startEv.ctrlKey || startEv.metaKey;
  const base = additive ? [...selStore.get().selSet] : [];
  if (!additive) {
    setActiveTrack(ti, { silent: true });
    deselect();   // 框选替换原有选择
  }
  const inner = tlInner();
  if (!inner) return;
  const innerRect = inner.getBoundingClientRect();
  const x0 = startEv.clientX, y0 = startEv.clientY;
  const lanes = Array.from(inner.querySelectorAll<HTMLElement>(".lane"))
    .map(el => [el, +(el.dataset.ti ?? "-1")] as const);
  inner.setPointerCapture(startEv.pointerId);
  freezeBlocks(true);
  let moved = false, hitSegs: Seg[] = [];

  const onMove = (ev: PointerEvent) => {
    if (!moved && Math.abs(ev.clientX - x0) < 4 && Math.abs(ev.clientY - y0) < 4) return;
    moved = true;
    const L = Math.min(x0, ev.clientX), R = Math.max(x0, ev.clientX);
    const T = Math.min(y0, ev.clientY), B = Math.max(y0, ev.clientY);
    dragStore.set({
      marquee: { left: L - innerRect.left, top: T - innerRect.top, w: R - L, h: B - T },
    });
    const hit = new Set<Seg>(base);
    const tA = tOf(L - innerRect.left), tB = tOf(R - innerRect.left);
    for (const [el, ti2] of lanes) {
      if (el.classList.contains("hiddenlane") || el.classList.contains("foldlane")) continue;
      const r = el.getBoundingClientRect();
      if (!(r.height > 0 && r.bottom >= T && r.top <= B)) continue;
      for (const s of (segsOf(ti2) || [])) if (s.t1 >= tA && s.t0 <= tB) hit.add(s);
    }
    hitSegs = [...hit];
    selStore.set({ preview: hit });   // 实时预览命中态（不动 selSet，落点才提交）
  };
  const onUp = () => {
    inner.removeEventListener("pointermove", onMove);
    inner.removeEventListener("pointerup", onUp);
    inner.removeEventListener("pointercancel", onUp);
    freezeBlocks(false);
    dragStore.set({ marquee: null });
    selStore.set({ preview: null });
    // 没拖动 = 点空白：普通点击取消选中，Ctrl/⌘ 点击保留原选区
    if (!moved) { if (!additive) deselect(); return; }
    setSelectionSegs(hitSegs, additive ? { scroll: false } : {});
  };
  inner.addEventListener("pointermove", onMove);
  inner.addEventListener("pointerup", onUp);
  inner.addEventListener("pointercancel", onUp);
}

/** 标尺/波形上按住拖动 = 移动播放头（带擦洗音）。alsoDeselect：点音频行同时取消句子选定 */
export function bindScrub(e: React.PointerEvent, alsoDeselect: boolean) {
  if (alsoDeselect) deselect();
  const el = e.currentTarget as HTMLElement;
  el.setPointerCapture(e.pointerId);
  // 新一次按住拖动 = 新的意图，「这段听过了」只在一次按住期间有效。不清的话：
  // 上一次拖动的最后一片会播到播放头前面，紧接着再拖就正好落在听过的范围里，一路哑的
  resetScrubPlayed();
  const x0 = e.clientX;
  let dragged = false;
  const move = (ev: { clientX: number }) => {
    if (Math.abs(ev.clientX - x0) > 3) dragged = true;
    // 擦洗音接管后不再逐帧写 currentTime，否则一直 seek 就没声了
    seek(tOf(ev.clientX - innerLeft()), { noVideo: isScrubbing() });
    scrubSound();
  };
  move(e);
  const onMove = (ev: PointerEvent) => move(ev);
  const done = () => {
    el.removeEventListener("pointermove", onMove);
    el.removeEventListener("pointerup", done);
    el.removeEventListener("pointercancel", done);
    // 拖动：松手立刻收声（手停了就该停）。点击：让它响完一小段再停
    if (dragged) endScrub(); else scrubBlip();
  };
  el.addEventListener("pointermove", onMove);
  el.addEventListener("pointerup", done);
  el.addEventListener("pointercancel", done);
}
