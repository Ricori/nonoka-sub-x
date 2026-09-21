import { useEffect, useState } from 'react';
import { DEFAULT_EFFECT_TRACK_ID } from '../../subtitles/effects';
import { CustomSelect } from '../../components/CustomSelect';
import { toggleFind } from '../lib/find';
import { docStore, trackName } from '../store/docStore';
import { findStore } from '../store/findStore';
import { curSegs, selStore, setActiveTrack } from '../store/selectionStore';
import { selectLane, stageStore } from '../store/stageStore';
import { modalStore } from '../store/uiStore';
import { viewRange, viewStore } from '../store/viewStore';
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
  viewStore.use(state => state.curClip);
  selStore.use(state => state.curTrack);
  const findOpen = findStore.use(state => state.open);
  const arr = curSegs();
  const [vA, vB] = viewRange(arr);
  return <div className="side-head">
    <TrackSelect />
    <span className="side-head-meta">{vB - vA} 句</span>
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

  useEffect(() => { selectLane(laneRef(curTrack, lang)); }, [curTrack, lang]);
  useEffect(() => {
    if (!stageSelection) return;
    const ti = stageSelection.trackId === DEFAULT_EFFECT_TRACK_ID
      ? -1
      : docStore.get().tracks.findIndex((track, index) =>
        (track.id || `track-${index + 1}`) === stageSelection.trackId);
    if (ti >= -1 && ti !== curTrack) setActiveTrack(ti, { silent: true });
    setLang(stageSelection.lang);
  }, [curTrack, stageSelection]);

  return <div className="side-page">
    <div className="side-head">
      <TrackSelect />
      <div className="side-segmented" role="group" aria-label="样式语言轴">
        <button type="button" className={lang === "ja" ? "on" : ""} onClick={() => setLang("ja")}>原文</button>
        <button type="button" className={lang === "zh" ? "on" : ""} onClick={() => setLang("zh")}>译文</button>
      </div>
    </div>
    <div className="side-panel"><StyleBar /></div>
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
