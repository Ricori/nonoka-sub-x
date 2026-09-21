import { memo, useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { shallowEqual } from '../../home/lib/createStore';
import { LIST_MARGIN } from '../constants';
import { matchRanges } from '../lib/find';
import { seek } from '../lib/playback';
import { docStore } from '../store/docStore';
import { findStore } from '../store/findStore';
import {
  curSegs, registerRowScroller, select, selectRange, selStore, toggleSel,
} from '../store/selectionStore';
import { fmtView, viewRange, viewStore } from '../store/viewStore';
import type { Lang, Seg } from '../types';

// 和时间轴一样只渲染视口附近的行：几千行全铺出来，光它们的排版/绘制就能把每一次
// 缩放/编辑拖到十几帧。上下各垫一块占位 div 把滚动条撑到该有的长度。
const segRowHs = new WeakMap<Seg, number>();   // 句 → 实测行高：文本换行的行更高，一律按估值会撑歪
let rowEstH = 46;                              // 没渲染过的行按这个估高
const segRowH = (s: Seg) => segRowHs.get(s) || rowEstH;

/** 把查找命中染上底色；cur 是「当前这一处」的起点（没有则 -1） */
function Marked({ text, q, mc, cur }: { text: string; q: string; mc: boolean; cur: number }) {
  const ranges = matchRanges(text, q, mc);
  if (!ranges.length) return <>{text}</>;
  const out: React.ReactNode[] = [];
  let p = 0;
  ranges.forEach(([a, b], k) => {
    if (a > p) out.push(text.slice(p, a));
    out.push(<mark key={k} className={"hit" + (a === cur ? " cur" : "")}>{text.slice(a, b)}</mark>);
    p = b;
  });
  if (p < text.length) out.push(text.slice(p));
  return <>{out}</>;
}

interface RowProps {
  i: number; label: string; tin: string; tout: string;
  ja: string; zh: string; active: boolean;
  /** 查找词与大小写开关；hlJa/hlZh 是本行「当前那一处」命中的起点，没有则 -1 */
  q: string; mc: boolean; hlJa: number; hlZh: number;
  onPick(i: number, e: React.MouseEvent): void;
}

const Row = memo(function Row(
  { i, label, tin, tout, ja, zh, active, q, mc, hlJa, hlZh, onPick }: RowProps,
) {
  const cell = (text: string, cur: number) =>
    (text ? (q ? <Marked text={text} q={q} mc={mc} cur={cur} /> : text) : "（空）");
  return (
    <div className={"row" + (active ? " active" : "")} data-i={i} onClick={e => onPick(i, e)}>
      <span className="idx">{label}</span>
      <span className="body">
        <span className="ja">{cell(ja, hlJa)}</span>
        <span className="zh">{cell(zh, hlZh)}</span>
      </span>
      <span className="tc"><span className="in">{tin}</span><span className="out">{tout}</span></span>
    </div>
  );
});

export function SegList() {
  docStore.use(s => s.version);
  const tracksVer = docStore.use(s => s.tracks.length);
  viewStore.use(s => s.curClip);
  const { curTrack, selSet } = selStore.use(s => ({ curTrack: s.curTrack, selSet: s.selSet }), shallowEqual);
  // 查找条开着时行里要染命中；「当前那一处」另外描边，跟计数 1/N 对得上
  const find = findStore.use(s => {
    const hit = s.cursor >= 0 ? s.hits[s.cursor] : null;
    return {
      q: s.open ? s.query : "", mc: s.matchCase,
      curSeg: s.open ? (hit ? hit.seg : null) : null,
      curLang: (hit ? hit.lang : null) as Lang | null,
      curStart: hit ? hit.start : -1,
    };
  }, shallowEqual);
  const scRef = useRef<HTMLDivElement>(null);
  const rowsRef = useRef<HTMLDivElement>(null);
  const pending = useRef<number | null>(null);
  const rafRef = useRef<number | null>(null);
  const [win, setWin] = useState<[number, number]>([0, 0]);

  /** #rows 在滚动容器里的可见区间（换算成 #rows 内部坐标） */
  const listVp = useCallback(() => {
    const sc = scRef.current, rows = rowsRef.current;
    if (!sc || !rows) return { top: 0, h: 400 };
    const y0 = rows.getBoundingClientRect().top - sc.getBoundingClientRect().top + sc.scrollTop;
    return { top: sc.scrollTop - y0, h: sc.clientHeight || 400 };
  }, []);

  const computeWin = useCallback(() => {
    const arr = curSegs();
    const vp = listVp();
    const yA = vp.top - vp.h * LIST_MARGIN, yB = vp.top + vp.h * (1 + LIST_MARGIN);
    const [vA, vB] = viewRange(arr);
    let a = vA, y = 0;
    while (a < vB && y + segRowH(arr[a]) < yA) { y += segRowH(arr[a]); a++; }
    let b = a;
    while (b < vB && y < yB) { y += segRowH(arr[b]); b++; }
    setWin(prev => (prev[0] === a && prev[1] === b) ? prev : [a, b]);
  }, [listVp]);

  /** 滚出已渲染区间就补画 */
  const ensureRows = useCallback(() => {
    const arr = curSegs(), vp = listVp();
    const [vA, vB] = viewRange(arr);
    let i = vA, y = 0;
    while (i < vB && y + segRowH(arr[i]) < vp.top) { y += segRowH(arr[i]); i++; }
    const first = i;
    while (i < vB && y < vp.top + vp.h) { y += segRowH(arr[i]); i++; }
    if (first < win[0] || i > win[1]) computeWin();
  }, [computeWin, listVp, win]);

  // 句子增删/换轨/进出切片都会改行数，窗口跟着重算
  useEffect(() => { computeWin(); }, [computeWin, curTrack, tracksVer]);

  /** 主选中行滚到眼前：没渲染出来的行按行高估算位置先跳过去，再重画窗口 */
  useEffect(() => {
    registerRowScroller(i => {
      const rows = rowsRef.current, sc = scRef.current;
      if (!rows || !sc) return;
      const arr = curSegs();
      if (i < 0 || i >= arr.length) return;
      const el = rows.querySelector<HTMLElement>(`.row[data-i="${i}"]`);
      if (el) { el.scrollIntoView({ block: "nearest" }); return; }
      const [vA] = viewRange(arr);
      let y = 0;
      for (let k = vA; k < i; k++) y += segRowH(arr[k]);
      sc.scrollTop += y - listVp().top - sc.clientHeight / 2;   // 先按估高跳到大概位置
      pending.current = i;
      computeWin();
    });
    return () => registerRowScroller(null);
  }, [computeWin, listVp]);

  // 实测行高回填：估值不准的话，滚上去时占位块会把内容顶得一跳一跳
  useLayoutEffect(() => {
    const rows = rowsRef.current;
    if (!rows) return;
    const arr = curSegs();
    let sum = 0, n = 0;
    for (const el of Array.from(rows.children) as HTMLElement[]) {
      if (!el.classList.contains("row")) continue;
      const hh = el.offsetHeight;
      if (hh > 0) { const s = arr[+(el.dataset.i ?? -1)]; if (s) { segRowHs.set(s, hh); sum += hh; n++; } }
    }
    if (n) rowEstH = sum / n;
    // 估高有偏差，落地后再对一次位
    if (pending.current != null) {
      const el = rows.querySelector<HTMLElement>(`.row[data-i="${pending.current}"]`);
      pending.current = null;
      el?.scrollIntoView({ block: "nearest" });
    }
  });

  const onPick = useCallback((i: number, e: React.MouseEvent) => {
    if (e.ctrlKey || e.metaKey) { toggleSel(i); return; }
    const cur = selStore.get().sel;
    if (e.shiftKey && cur >= 0 && cur < curSegs().length) { selectRange(cur, i); return; }
    select(i);
    seek(curSegs()[i].t0 + 0.01);
  }, []);

  const arr = curSegs();
  const [vA, vB] = viewRange(arr);
  const a = Math.min(Math.max(win[0], vA), vB);
  const b = Math.min(Math.max(win[1], a), vB);
  let padTop = 0;
  for (let k = vA; k < a; k++) padTop += segRowH(arr[k]);
  let padBot = 0;
  for (let k = b; k < vB; k++) padBot += segRowH(arr[k]);

  const rows: React.ReactElement[] = [];
  for (let i = a; i < b; i++) {
    const s = arr[i];
    rows.push(
      <Row key={i} i={i} label={String(i - vA + 1).padStart(2, "0")}
        tin={fmtView(s.t0)} tout={fmtView(s.t1)}
        ja={s.ja} zh={s.zh}
        q={find.q} mc={find.mc}
        hlJa={find.curSeg === s && find.curLang === "ja" ? find.curStart : -1}
        hlZh={find.curSeg === s && find.curLang === "zh" ? find.curStart : -1}
        active={selSet.has(s)} onPick={onPick} />,
    );
  }

  return (
    <div className="seg-list" id="seg-list" ref={scRef}
      onScroll={() => {
        if (rafRef.current) return;
        rafRef.current = requestAnimationFrame(() => { rafRef.current = null; ensureRows(); });
      }}>
      <div id="rows" ref={rowsRef}>
        <div style={{ height: Math.max(0, padTop) + "px" }} />
        {rows}
        <div style={{ height: Math.max(0, padBot) + "px" }} />
      </div>
    </div>
  );
}
