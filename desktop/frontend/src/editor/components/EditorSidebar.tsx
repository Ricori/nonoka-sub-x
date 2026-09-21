import { useEffect, useRef, useState } from 'react';
import { shallowEqual } from '../../home/lib/createStore';
import { DEFAULT_EFFECT_TRACK_ID } from '../../subtitles/effects';
import { CustomSelect } from '../../components/CustomSelect';
import { toggleFind } from '../lib/find';
import { docStore, trackName } from '../store/docStore';
import { findStore } from '../store/findStore';
import { curSegs, selStore, setActiveTrack } from '../store/selectionStore';
import { selectLane, stageStore } from '../store/stageStore';
import { modalStore } from '../store/uiStore';
import { listedIdx, viewStore } from '../store/viewStore';
import type { Lang } from '../types';
import { FindBar } from './FindBar';
import { Inspector } from './Inspector';
import { SegList } from './SegList';
import { SideEffectsPanel } from './SideEffectsPanel';
import { SideTrackPanel } from './SideTrackPanel';
import { StyleBar } from './stage/StyleBar';

const tabs = [
  ["subtitle", "字幕"], ["style", "样式"], ["effects", "特效"], ["track", "轨道"],
] as const;

function laneRef(ti: number, lang: Lang) {
  if (ti < 0) return { trackId: DEFAULT_EFFECT_TRACK_ID, lang };
  const track = docStore.get().tracks[ti];
  return { trackId: track?.id || `track-${ti + 1}`, lang };
}

/** 面板顶部的轨道切换：字幕、样式两页都跟着「当前轨」走 */
export function TrackSelect() {
  docStore.use(state => state.version);
  const curTrack = selStore.use(state => state.curTrack);
  const { tracks } = docStore.get();
  const options = [-1, ...tracks.map((_, index) => index)].map(ti => ({ value: ti, label: trackName(ti) }));
  return <CustomSelect compact className="side-track-select" ariaLabel="当前轨道" value={curTrack}
    options={options} onChange={ti => setActiveTrack(ti, { silent: true })} />;
}

function SubtitleHead() {
  docStore.use(state => state.version);
  viewStore.use(state => ({ t0: state.t0, t1: state.t1, pieces: state.pieces, focus: state.focus }), shallowEqual);
  selStore.use(state => state.curTrack);
  const findOpen = findStore.use(state => state.open);
  // 聚焦成片时只数成片里还有的句
  const n = listedIdx(curSegs()).length;
  return <div className="side-head">
    <TrackSelect />
    <span className="side-head-meta">{n} 句</span>
    <button type="button" className={"side-icon-btn" + (findOpen ? " on" : "")} id="btn-find"
      title="查找 / 替换 (Ctrl+F)" aria-pressed={findOpen} onClick={toggleFind}>
      <svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="7" cy="7" r="4.5" /><path d="m10.5 10.5 3 3" /></svg>
    </button>
  </div>;
}

function SideStylePanel() {
  const curTrack = selStore.use(state => state.curTrack);
  const [lang, setLang] = useState<Lang>("zh");
  const stageSelection = stageStore.use(state => state.sel);

  // 手动切原文/译文算一次选中，画面上把那条框出来；切到这一页本身不选中任何东西
  const pickLang = (next: Lang) => { setLang(next); selectLane(laneRef(curTrack, next)); };
  // 两个方向各管各的：画面上选中了别的轨 → 当前轨跟过去；顶部切了轨 → 选中跟过来。
  // 合成一个 effect 的话，切轨时选中还指着旧轨，会当场把当前轨拽回去
  useEffect(() => {
    if (!stageSelection) return;
    const ti = stageSelection.trackId === DEFAULT_EFFECT_TRACK_ID
      ? -1
      : docStore.get().tracks.findIndex((track, index) =>
        (track.id || `track-${index + 1}`) === stageSelection.trackId);
    if (ti >= -1 && ti !== selStore.get().curTrack) setActiveTrack(ti, { silent: true });
    setLang(stageSelection.lang);
  }, [stageSelection]);
  // 首次挂载不算切轨：此时选中可能刚从别处设好（例如缺样式提示跳过来），当前轨还没跟上
  const mounted = useRef(false);
  useEffect(() => {
    if (!mounted.current) { mounted.current = true; return; }
    const sel = stageStore.get().sel;
    if (sel && sel.trackId !== laneRef(curTrack, sel.lang).trackId) selectLane(laneRef(curTrack, sel.lang));
  }, [curTrack]);

  return <div className="side-page side-style-page">
    <div className="side-head">
      <TrackSelect />
      <div className="side-segmented" role="group" aria-label="样式语言轴">
        <button type="button" className={lang === "ja" ? "on" : ""} onClick={() => pickLang("ja")}>原文</button>
        <button type="button" className={lang === "zh" ? "on" : ""} onClick={() => pickLang("zh")}>译文</button>
      </div>
    </div>
    <div className="side-panel"><StyleBar lane={laneRef(curTrack, lang)} /></div>
  </div>;
}

export function EditorSidebar() {
  const sideTab = modalStore.use(state => state.sideTab);
  return <aside className="side">
    <div className="side-tabs" role="tablist" aria-label="编辑功能">
      {tabs.map(([id, label]) => <button key={id} type="button" role="tab"
        aria-selected={sideTab === id} className={sideTab === id ? "on" : ""}
        onClick={() => modalStore.set({ sideTab: id })}>{label}</button>)}
    </div>
    <div className="side-content">
      {sideTab === "subtitle" && <div className="side-page side-subtitle-page">
        <SubtitleHead />
        <FindBar />
        <Inspector />
        <SegList />
      </div>}
      {sideTab === "style" && <SideStylePanel />}
      {sideTab === "effects" && <SideEffectsPanel />}
      {sideTab === "track" && <SideTrackPanel />}
    </div>
  </aside>;
}
