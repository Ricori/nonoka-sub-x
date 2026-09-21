import { useEffect, useRef } from 'react';
import { shallowEqual } from '../../home/lib/createStore';
import { PAD_Y, ROW_MAX, ROW_MIN, SB, SPECTRUM_ROW_H0, WAVE_ROW_MAX } from '../constants';
import { toggleDefaultHidden, toggleTrackHidden } from '../lib/edits';
import { applyRowHeight, rowDisplayH } from '../lib/rows';
import { effGain, setWaveGain, stepWaveGain } from '../lib/wave';
import { splitHandler } from '../lib/split';
import { docStore, laneColor } from '../store/docStore';
import { layoutStore, saveLayout, setRowH } from '../store/layoutStore';
import { selStore, setActiveTrack } from '../store/selectionStore';
import { openTrackPop } from '../store/uiStore';
import { rulerH, viewStore } from '../store/viewStore';
import type { Lang, RowSpec, Ti } from '../types';

const EyeIcon = ({ off }: { off: boolean }) => (
  <svg width="12" height="12" viewBox="0 0 13 13" fill="none" stroke="currentColor" strokeWidth="1.3">
    <path d="M1 6.5S3 2.5 6.5 2.5 12 6.5 12 6.5 10 10.5 6.5 10.5 1 6.5 1 6.5Z" />
    {off ? <path d="M2 11 11 2" /> : <circle cx="6.5" cy="6.5" r="1.7" />}
  </svg>
);

const GearIcon = () => (
  <svg width="12" height="12" viewBox="0 0 13 13" fill="none" stroke="currentColor" strokeWidth="1.3">
    <circle cx="6.5" cy="6.5" r="2" />
    <path d="M6.5 1v1.8M6.5 10.2V12M1 6.5h1.8M10.2 6.5H12M2.6 2.6l1.3 1.3M9.1 9.1l1.3 1.3M10.4 2.6 9.1 3.9M3.9 9.1l-1.3 1.3" />
  </svg>
);

/**
 * 波形增益按钮：点击在预设倍率间循环（自动 → 1× → … → 24× → 自动）；
 * 滚轮上下一档；右键 / Alt+点击回自动。只影响显示，不改音频。
 */
function GainButton() {
  const waveGain = layoutStore.use(s => s.waveGain);
  docStore.use(s => s.peaks);   // 自动档的实际倍率要等 peaks 到手才算得出
  const ref = useRef<HTMLButtonElement>(null);
  const auto = waveGain === 0;
  const g = effGain();

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      e.stopPropagation();
      stepWaveGain(e.deltaY < 0 ? 1 : -1);
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, []);

  return (
    <button ref={ref} className={"lbtn gain" + (!auto || g > 1.05 ? " on" : "")} id="wave-gain"
      title={`波形显示增益 ${auto ? "自动（当前约 " + g.toFixed(1) + "×）" : g + "×"}`
        + "：点击切换倍率，滚轮微调，右键回自动（只影响显示，不改音频）"}
      onClick={e => { if (e.altKey) setWaveGain(0); else stepWaveGain(1); }}
      onContextMenu={e => { e.preventDefault(); setWaveGain(0); }}>
      {auto ? "自动" : (Number.isInteger(g) ? g + "×" : g.toFixed(1) + "×")}
    </button>
  );
}

function AudioViewButton() {
  const mode = layoutStore.use(s => s.audioView);
  const toggle = () => {
    const next = mode === "wave" ? "spectrum" : "wave";
    layoutStore.set({ audioView: next });
    if (next === "spectrum" && layoutStore.get().rowH.wave < SPECTRUM_ROW_H0) setRowH("wave", SPECTRUM_ROW_H0);
    saveLayout();
  };
  return <button className="lbtn audio-view" title="切换波形 / 本地频谱图" onClick={toggle}>
    {mode === "wave" ? "波形" : "频谱"}
  </button>;
}

function TrackLabel({ r, cur }: { r: RowSpec; cur: boolean }) {
  const { tracks, trackMeta } = docStore.get();
  const lang = r.lang as Lang;
  const ti: Ti = r.ti;
  const isDefault = ti < 0;
  const tr = tracks[ti];
  const full = (isDefault || !tr ? (trackMeta?.name || "默认轨") : (tr.name || ("轨道 " + (ti + 1))))
    + " · " + (lang === "ja" ? "原文" : "译文");
  const hidden = isDefault || !tr
    ? !!trackMeta?.[lang].hidden
    : !!tr[lang].hidden;
  const cls = "lbl track" + (isDefault ? (lang === "ja" ? " t-ja" : " t-zh") : "")
    + (cur ? " cur" : "") + (r.fold ? " foldlane" : (!r.vis ? " hiddenlane" : ""));

  return (
    <div className={cls} id={isDefault ? (lang === "ja" ? "lbl-ja" : "lbl-zh") : undefined}
      style={{ height: rowDisplayH(r) + "px" }}>
      <i style={isDefault ? undefined : { background: laneColor(ti, lang) }} />
      <span className="tname" title={isDefault ? "点击切换列表到默认轨" : full + "（点击切换列表到此轨道）"}
        onClick={() => setActiveTrack(ti)}>{full}</span>
      <button className={"lbtn eye" + (hidden ? " off" : "")}
        title={hidden ? "显示轨道" : "隐藏轨道"}
        onClick={() => isDefault ? toggleDefaultHidden(lang) : toggleTrackHidden(ti, lang)}>
        <EyeIcon off={hidden} />
      </button>
      <button className="lbtn gear" title="轨道设置"
        onClick={e => openTrackPop(
          isDefault ? { kind: "default", lang } : { kind: "track", ti, lang }, e.currentTarget)}>
        <GearIcon />
      </button>
    </div>
  );
}

/**
 * 轨道名栏。与轨道区共用同一份 rows，两列高度才对得上；纵向滚动由 Timeline 同步
 * scrollTop（原生纵向条关掉了，见 .tl-labels 的 overflow-y:hidden）。
 * 每条「可见」行的底边各放一个行间手柄，拖动只改这一行的高度。
 */
export function TrackLabels({ rows, labelsRef }: {
  rows: RowSpec[]; labelsRef: React.RefObject<HTMLDivElement | null>;
}) {
  const lblW = layoutStore.use(s => s.lblW);
  const audioView = layoutStore.use(s => s.audioView);
  const curTrack = selStore.use(s => s.curTrack);
  // 切片层数变了标尺就会变高，分隔线的落点跟着重算
  viewStore.use(s => ({ clips: s.clips, t0: s.t0, t1: s.t1 }), shallowEqual);

  // 分隔线贴在其上方那条可见行的底边（与下一条可见行之间可能夹着隐藏行）
  let acc = rulerH();
  const bottoms = rows.map(r => (acc += rowDisplayH(r)));

  return (
    <div className="tl-labels" id="tl-labels" ref={labelsRef} style={{ width: lblW + "px" }}>
      <div className="lbl ruler-spacer"></div>
      {rows.map(r => r.kind === "wave"
        ? (
          <div className="lbl wave" key={r.key} style={{ height: rowDisplayH(r) + "px" }}>
            <i></i><span className="tname">音频 A1</span>
            <AudioViewButton />
            {audioView === "wave" && <GainButton />}
          </div>
        )
        : <TrackLabel key={r.key} r={r} cur={r.ti === curTrack} />)}
      {/* 底部占位：与轨道区横向滚动条等高，保证标签与轨道对齐 */}
      <div className="sb-gutter" id="sb-gutter" style={{ height: (SB + PAD_Y) + "px" }}></div>
      {rows.map((r, i) => r.vis && (
        <div key={r.key + "-h"} className="row-resize" title="拖动改这条轨道的高度"
          style={{ top: (bottoms[i] - 3) + "px" }}
          onPointerDown={splitHandler(() => r.height, (v0, _dx, dy) =>
            applyRowHeight(r, Math.min(Math.max(v0 + dy, ROW_MIN), r.kind === "wave" ? WAVE_ROW_MAX : ROW_MAX)))} />
      ))}
    </div>
  );
}
