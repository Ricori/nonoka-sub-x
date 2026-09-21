import { useEffect, useState } from 'react';
import { DEFAULT_EFFECT_TRACK_ID } from '../../subtitles/effects';
import { docStore, trackName } from '../store/docStore';
import { curSegs, selStore, setActiveTrack } from '../store/selectionStore';
import { selectLane, stageStore } from '../store/stageStore';
import { modalStore } from '../store/uiStore';
import type { Lang } from '../types';
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

function SideScope() {
  docStore.use(state => state.version);
  const curTrack = selStore.use(state => state.curTrack);
  const sel = selStore.use(state => state.sel);
  const seg = curSegs()[sel];
  return <div className="side-scope">
    <span>{trackName(curTrack)}</span>
    <small>{seg ? `当前句 #${String(sel + 1).padStart(2, "0")}` : "未选择字幕"}</small>
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

  return <div className="side-panel side-style-panel">
    <div className="side-style-target">
      <span>编辑对象</span>
      <div className="side-segmented" role="group" aria-label="样式语言轴">
        <button type="button" className={lang === "ja" ? "on" : ""} onClick={() => setLang("ja")}>日语原文</button>
        <button type="button" className={lang === "zh" ? "on" : ""} onClick={() => setLang("zh")}>中文译文</button>
      </div>
    </div>
    <StyleBar />
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
    <SideScope />
    <div className="side-content">
      {sideTab === "subtitle" && <div className="side-subtitle-panel"><Inspector /><SegList /></div>}
      {sideTab === "style" && <SideStylePanel />}
      {sideTab === "effects" && <SideEffectsPanel />}
      {sideTab === "track" && <SideTrackPanel />}
    </div>
  </aside>;
}
