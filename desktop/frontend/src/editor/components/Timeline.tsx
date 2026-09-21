import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { shallowEqual } from '../../home/lib/createStore';
import { PAD_Y, RULER_H0 } from '../constants';
import { addSegmentAt } from '../lib/edits';
import { laneTiAt, startMarquee } from '../lib/laneDrag';
import { buildRows, rowDisplayH } from '../lib/rows';
import { splitHandler } from '../lib/split';
import { deleteSelectedSegs, keepSelectedSegs } from '../lib/videoEdit';
import { docStore } from '../store/docStore';
import { dragStore } from '../store/dragStore';
import { layoutStore, saveLayout, tlCap } from '../store/layoutStore';
import { playStore } from '../store/playStore';
import { selStore, setActiveTrack } from '../store/selectionStore';
import { setTlEls, syncTlMetrics, tlScroll, tlStore } from '../store/tlStore';
import { showCtx } from '../store/uiStore';
import { clearVsel } from '../store/vselStore';
import {
  ensureBlkWin, isFocused, setZoom, syncZoomRange, tAtClientX, viewDur, viewStore, xOf,
} from '../store/viewStore';
import { Lane } from './Lane';
import { Ruler } from './Ruler';
import { TimelineToolbar } from './TimelineToolbar';
import { TrackLabels } from './TrackLabels';
import { CutShade, VideoRow } from './VideoRow';
import { VScrollbar } from './VScrollbar';
import { WaveRow } from './WaveRow';
import type { CtxItem } from '../types';

function Playhead() {
  const t = playStore.use(s => s.t);
  // xOf() 吃 pps、t0，聚焦成片时还吃片段，都得订阅（相加当快照会漏掉「一增一减」那种组合）
  viewStore.use(s => ({ pps: s.pps, t0: s.t0, pieces: s.pieces, focus: s.focus }), shallowEqual);
  return <div className="playhead" id="playhead" style={{ left: xOf(t) + "px" }} />;
}

function Marquee() {
  const m = dragStore.use(s => s.marquee);
  if (!m) return null;
  return <div className="marquee"
    style={{ left: m.left + "px", top: m.top + "px", width: m.w + "px", height: m.h + "px" }} />;
}

const focusText = (lang: "ja" | "zh") =>
  document.getElementById(lang === "zh" ? "insp-zh" : "insp-ja")?.focus();

export function Timeline() {
  docStore.use(s => s.version);
  // 聚焦成片时 inner 宽度是成片时长，片段一变就得跟着变
  const { pps } = viewStore.use(
    s => ({ pps: s.pps, t0: s.t0, t1: s.t1, pieces: s.pieces, focus: s.focus }), shallowEqual);
  // rowH 决定各行高度，tlViewH 决定轨道区可视高度（tlCap）——少订阅一个，
  // 拖 hsplit 就只改了 store 不重渲染，看起来像「拖不动」
  layoutStore.use(s => ({ rowH: s.rowH, tlViewH: s.tlViewH }), shallowEqual);
  const { left, w } = tlStore.use(s => ({ left: s.left, w: s.w }), shallowEqual);
  const scrollRef = useRef<HTMLDivElement>(null);
  const innerRef = useRef<HTMLDivElement>(null);
  const labelsRef = useRef<HTMLDivElement>(null);
  const rafRef = useRef<number | null>(null);
  const [, forceTick] = useState(0);

  useLayoutEffect(() => {
    setTlEls(scrollRef.current, innerRef.current);
    syncTlMetrics();
    return () => setTlEls(null, null);
  }, []);

  // 视口宽或时长变了 → 全览这一档也跟着变
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => { if (syncZoomRange()) saveLayout(); syncTlMetrics(); });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // 轨道区可视高度上限跟着窗口走
  useEffect(() => {
    const onResize = () => { forceTick(n => n + 1); syncTlMetrics(); };
    addEventListener("resize", onResize);
    return () => removeEventListener("resize", onResize);
  }, []);

  /**
   * 滚轮：Ctrl 以光标为锚点缩放，Alt 纵向滚轨道，其余横向滚动；deltaMode 归一化后触控板更顺。
   * 要 preventDefault，所以不能用 React 的 onWheel（合成事件是被动的）。
   */
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      const unit = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? el.clientHeight : 1;
      if (e.ctrlKey || e.metaKey) {
        e.preventDefault();
        setZoom(viewStore.get().pps * Math.exp(-e.deltaY * unit * 0.0012), e.clientX);
        saveLayout();
        return;
      }
      // 纵向滚轮也映射为横向滚动；已有横向分量（deltaX）时直接用
      const d = (Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY) * unit;
      if (!d) return;
      e.preventDefault();
      if (e.altKey) el.scrollTop += d; else el.scrollLeft += d;
      syncTlMetrics();
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, []);

  const rows = buildRows();

  // 行高/轨道数变了：滚动高度跟着变，自绘滚动条与块窗口要重算
  useLayoutEffect(() => { syncTlMetrics(); });

  function onScroll() {
    // 标签栏跟着纵向滚。纵向关了原生条，浏览器也就不派发 scroll 事件给它
    const sc = tlScroll();
    if (labelsRef.current && sc) labelsRef.current.scrollTop = sc.scrollTop;
    if (rafRef.current) return;
    rafRef.current = requestAnimationFrame(() => {
      rafRef.current = null;
      syncTlMetrics();
      ensureBlkWin();
    });
  }

  /**
   * 时间轴级框选：挂在 inner 上，连轨道下方那块不属于任何 lane 的空白也覆盖到。
   * 落在块上交给 lane 自己的选中/拖动，落在标尺/波形/视频行上交给它们自己。
   */
  function onInnerDown(e: React.PointerEvent) {
    if (e.button !== 0) return;
    const el = e.target as HTMLElement;
    // 视频轨之外的任何点击都放下视频轨上的选中，Delete 才不会删到看不见的东西
    if (!el.closest("#videorow")) clearVsel();
    if (el.closest(".blk")) return;
    if (el.closest("#ruler") || el.closest("#waverow") || el.closest("#videorow")) return;
    const ti = laneTiAt(e.clientY);
    startMarquee(e, ti == null ? selStore.get().curTrack : ti);
  }

  /** 时间轴空白处右键：新建字幕 / 按选中的字幕块剪视频轨（视频行有自己的菜单） */
  function onInnerCtx(e: React.MouseEvent) {
    const items: CtxItem[] = [];
    // 落在字幕轴（lane）内才给「新建字幕」，标尺/波形上不给
    const ti = laneTiAt(e.clientY);
    if (ti != null) {
      const tc = tAtClientX(e.clientX);
      items.push({
        label: "在当前位置新建字幕",
        onClick: () => { setActiveTrack(ti, { silent: true }); addSegmentAt(tc); },
      });
    }
    const n = selStore.get().selSet.size;
    // 按选中字幕剪视频轨只在完整片里给：成片只供预览
    if (n && !isFocused()) {
      if (items.length) items.push("-");
      items.push(
        { label: `只保留选中字幕的范围（${n} 句）`, onClick: keepSelectedSegs },
        { label: `从成片中删掉选中字幕（${n} 句）`, danger: true, onClick: deleteSelectedSegs },
      );
    }
    if (!items.length) return;
    showCtx(e, items);
  }

  return (
    <section className="timeline" style={{ "--ruler-h": RULER_H0 + "px" } as React.CSSProperties}>
      <TimelineToolbar />
      <div className="tl-body" style={{ height: tlCap() + "px" }}>
        <TrackLabels rows={rows} labelsRef={labelsRef} />
        <div className="lbl-split" id="lbl-split" title="拖动调整轨道名栏宽度"
          onPointerDown={splitHandler(() => layoutStore.get().lblW, (v0, dx) => {
            layoutStore.set({ lblW: Math.min(Math.max(v0 + dx, 90), 360) });
            syncTlMetrics();   // 轨道可见区宽度变了，标尺与波形要重绘
            saveLayout();
          })} />
        <div className="tl-scroll" id="tl-scroll" ref={scrollRef} onScroll={onScroll}>
          <div className="tl-inner" id="tl-inner" ref={innerRef}
            style={{ width: (viewDur() * pps) + "px", paddingBottom: PAD_Y + "px" }}
            onPointerDown={onInnerDown} onContextMenu={onInnerCtx}>
            <Ruler left={left} w={w} />
            <CutShade />
            {rows.map(r => r.kind === "video"
              ? <VideoRow key={r.key} height={rowDisplayH(r)} left={left} w={w} />
              : r.kind === "wave"
                ? <WaveRow key={r.key} height={rowDisplayH(r)} left={left} w={w} />
                : <Lane key={r.key} ti={r.ti} lang={r.lang!} height={rowDisplayH(r)}
                  fold={r.fold} vis={r.vis} onFocusText={focusText} />)}
            <Playhead />
            <Marquee />
          </div>
        </div>
        <VScrollbar />
      </div>
    </section>
  );
}
