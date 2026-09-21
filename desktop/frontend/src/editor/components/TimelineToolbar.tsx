import { shallowEqual } from '../../home/lib/createStore';
import { enterClip, exitClip, newClipFromSelection } from '../lib/clips';
import { addSegmentAt, foldJa, newTrack, splitAtPlayhead, toggleFoldJa } from '../lib/edits';
import { endScrub } from '../lib/playback';
import { docStore } from '../store/docStore';
import { layoutStore, saveLayout } from '../store/layoutStore';
import { playStore } from '../store/playStore';
import { modalStore, showCtx } from '../store/uiStore';
import { ppsToSlider, setZoom, sliderToPps, viewStore } from '../store/viewStore';
import type { CtxItem } from '../types';

export function TimelineToolbar() {
  docStore.use(s => s.version);
  const { snap, scrubAudio } = layoutStore.use(s => ({ snap: s.snap, scrubAudio: s.scrubAudio }), shallowEqual);
  const { curClip, clips, pps } = viewStore.use(
    s => ({ curClip: s.curClip, clips: s.clips, pps: s.pps }), shallowEqual);

  /** 切片面包屑下拉：已经在完整片上就不列「完整片」；当前切片的名字写在按钮上，也不再列一遍 */
  function crumbMenu(e: React.MouseEvent) {
    const items: CtxItem[] = [];
    if (curClip) items.push({ label: "完整片", onClick: exitClip });
    for (const c of clips) {
      if (c !== curClip) items.push({ label: c.name, onClick: () => enterClip(c) });
    }
    if (items.length) items.push("-");
    items.push({ label: "以选中字幕块新建切片", onClick: () => void newClipFromSelection() });
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
      <button className="tool" id="btn-split" title="在当前位置把当前句拆成两句 (D)" onClick={splitAtPlayhead}>
        <svg width="13" height="13" viewBox="0 0 13 13" fill="none" stroke="currentColor" strokeWidth="1.3">
          <path d="M9.5 1.5 3 8l-1 3.5 3.5-1L12 4z" strokeLinejoin="round" />
          <path d="M7.3 3.7l2 2" />
        </svg>
        在当前位置拆分
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

      {/* 切片面包屑（参考剪映）：完整片时是个切换入口，进了切片就多一颗返回键 */}
      <div className="crumb" id="crumb">
        <button className="back" id="crumb-back" type="button" title="返回完整片" hidden={!curClip}
          onClick={exitClip}>
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.6"
            strokeLinecap="round" strokeLinejoin="round">
            <path d="M7.5 2 3.5 6l4 4" />
          </svg>
        </button>
        <button className={"cur" + (curClip ? " on-clip" : "")} id="crumb-cur" type="button"
          title="切换编辑对象：完整片或某个切片" onClick={crumbMenu}>
          {(curClip ? curClip.name : "完整片") + " ▾"}
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
