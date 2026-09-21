import { shallowEqual } from '../../home/lib/createStore';
import { addSegmentAt, foldJa, newTrack, toggleFoldJa } from '../lib/edits';
import { endScrub } from '../lib/playback';
import {
  cutAtPlayhead, hasContiguous, keepSelectedSegs, mergeContiguous, resetVideoEdit, splitHere, toggleFocus,
} from '../lib/videoEdit';
import { docStore } from '../store/docStore';
import { layoutStore, saveLayout } from '../store/layoutStore';
import { playStore } from '../store/playStore';
import { modalStore, showCtx } from '../store/uiStore';
import { ppsToSlider, setZoom, sliderToPps, viewStore } from '../store/viewStore';
import { selStore } from '../store/selectionStore';
import { vselStore } from '../store/vselStore';
import { joinPieces } from '../../subtitles/pieces.ts';
import type { CtxItem } from '../types';

export function TimelineToolbar() {
  docStore.use(s => s.version);
  const { snap, scrubAudio } = layoutStore.use(s => ({ snap: s.snap, scrubAudio: s.scrubAudio }), shallowEqual);
  const { pieces, focus, pps } = viewStore.use(
    s => ({ pieces: s.pieces, focus: s.focus, pps: s.pps }), shallowEqual);
  const focused = focus && !!pieces;
  const videoActive = vselStore.use(s => s.videoActive);
  const joined = pieces ? joinPieces(pieces) : null;

  /** 成片面包屑下拉：在完整片和成片之间切换，外加几个整体操作 */
  function crumbMenu(e: React.MouseEvent) {
    const items: CtxItem[] = [];
    if (pieces) items.push({ label: focused ? "查看完整片" : "查看成片", onClick: toggleFocus });
    // 成片只供预览：剪辑类的一概不给
    if (focused) { showCtx(e, items); return; }
    if (selStore.get().selSet.size) items.push({ label: "只保留选中字幕的范围", onClick: keepSelectedSegs });
    items.push({ label: "在播放头处切分视频 (Ctrl+B)", onClick: cutAtPlayhead });
    if (pieces) {
      items.push("-");
      if (hasContiguous()) items.push({ label: "合并连续片段", onClick: mergeContiguous });
      items.push({ label: "恢复整片（撤销全部剪辑）", danger: true, onClick: resetVideoEdit });
    }
    showCtx(e, items);
  }

  return (
    <div className="tl-toolbar">
      <button className="tool" id="tool-add" title="在当前位置新建字幕 (N)"
        onClick={() => addSegmentAt(playStore.get().t)}>
        <svg width="13" height="13" viewBox="0 0 13 13" fill="none" stroke="currentColor" strokeWidth="1.6">
          <path d="M6.5 2v9M2 6.5h9" />
        </svg>
        新建字幕
      </button>
      <div className="sep"></div>
      {/* 切分：点过视频轨就切视频，否则切当前字幕句（默认） */}
      <button className="tool" id="btn-split" onClick={splitHere}
        title={videoActive && !focused ? "在播放头处把视频轨切成两段 (Ctrl+B)" : "在当前位置把当前句拆成两句 (D)"}>
        <svg width="13" height="13" viewBox="0 0 13 13" fill="none" stroke="currentColor" strokeWidth="1.3">
          <circle cx="3.2" cy="10" r="1.8" />
          <circle cx="9.8" cy="10" r="1.8" />
          <path d="M4.5 8.7 10.5 1.5M8.5 8.7 2.5 1.5" />
        </svg>
        切分
      </button>
      <div className="sep"></div>
      <button className="tool" id="btn-new-track"
        title="新建轨道，字幕与样式可独立" onClick={() => void newTrack()}>
        <svg width="13" height="13" viewBox="0 0 13 13" fill="none" stroke="currentColor" strokeWidth="1.4">
          <path d="M1.5 3h10M1.5 6.5h6M6.5 8.5v4M4.5 10.5h4" />
        </svg>
        新建轨道
      </button>
      <div className="sep"></div>
      <button className={"tool" + (foldJa() ? " on" : "")} id="btn-fold-ja"
        title="一键隐藏所有原文轨，导出同步不出原文" onClick={toggleFoldJa}>
        <svg width="13" height="13" viewBox="0 0 13 13" fill="none" stroke="currentColor" strokeWidth="1.4">
          <path d="M1 6.5S3 2.5 6.5 2.5 12 6.5 12 6.5 10 10.5 6.5 10.5 1 6.5 1 6.5Z" />
          <path d="M2 11 11 2" />
        </svg>
        <span id="fold-ja-txt">隐藏原文轨</span>
      </button>
      <div className="sep"></div>
      {/* 右对齐区：仅图标，靠 #tool-snap 的 margin-left:auto 吃掉前面的剩余空间 */}
      <button className={"tool icon-only" + (snap ? " on" : "")} id="tool-snap" title="吸附到相邻字幕/当前位置"
        onClick={() => layoutStore.set({ snap: !snap })}>
        <svg width="13" height="13" viewBox="0 0 13 13" fill="none" stroke="currentColor" strokeWidth="1.4">
          <path d="M3 1v6a3.5 3.5 0 0 0 7 0V1" />
          <path d="M1.5 11.5h10" />
        </svg>
      </button>
      <div className="sep"></div>
      <button className={"tool icon-only" + (scrubAudio ? " on" : "")} id="tool-scrub"
        title="擦洗音：拖动时间轴或用 ←/→ 步进时，播一小段声音（不影响正常播放）"
        onClick={() => {
          const next = !scrubAudio;
          layoutStore.set({ scrubAudio: next });
          if (!next) endScrub();   // 正响着就立刻收声
          saveLayout();
        }}>
        <svg width="13" height="13" viewBox="0 0 13 13" fill="none" stroke="currentColor" strokeWidth="1.4">
          <path d="M1.5 5h2l3-2.5v8L3.5 8h-2z" strokeLinejoin="round" />
          <path d="M8.5 4.8a2.4 2.4 0 0 1 0 3.4" />
          <path d="M10 3.3a4.6 4.6 0 0 1 0 6.4" />
        </svg>
      </button>
      <div className="sep"></div>
      <button className="tool icon-only" id="btn-ass-style"
        title="编辑样式模板"
        onClick={() => modalStore.set({ tplOpen: true })}>
        <svg width="13" height="13" viewBox="0 0 13 13" fill="none" stroke="currentColor" strokeWidth="1.4">
          <path d="M3.1 9.2 6.5 2.2l3.4 7" strokeLinejoin="round" />
          <path d="M4.6 7.2h3.8" />
          <path d="M2.4 11.4h8.2" strokeWidth="1.9" strokeLinecap="round" />
        </svg>
      </button>
      <div className="sep"></div>

      {/* 成片面包屑（参考剪映）：完整片时是个切换入口，聚焦成片时多一颗返回键 */}
      <div className="crumb" id="crumb">
        <button className="back" id="crumb-back" type="button" title="返回完整片" hidden={!focused}
          onClick={toggleFocus}>
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.6"
            strokeLinecap="round" strokeLinejoin="round">
            <path d="M7.5 2 3.5 6l4 4" />
          </svg>
        </button>
        <button className={"cur" + (focused ? " on-edit" : "")} id="crumb-cur" type="button"
          title="在完整片与剪好的成片之间切换" onClick={crumbMenu}>
          {(joined
            ? `${focused ? "成片" : "完整片"}`
            : "完整片") + " ▾"}
        </button>
      </div>

      <div className="zoom">
        <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.4">
          <circle cx="5" cy="5" r="3.6" />
          <path d="M8 8l2.6 2.6" />
        </svg>
        <input type="range" id="zoom" min="0" max="1000" aria-label="时间轴缩放"
          title="时间轴缩放（Ctrl+滚轮以光标为锚点缩放）"
          value={String(ppsToSlider(pps))}
          onChange={e => { setZoom(sliderToPps(+e.target.value)); saveLayout(); }} />
      </div>
    </div>
  );
}
