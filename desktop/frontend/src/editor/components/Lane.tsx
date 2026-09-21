import { memo } from 'react';
import { BLK_DETAIL_W } from '../constants';
import { addSegmentAt } from '../lib/edits';
import { onLanePointerDown } from '../lib/laneDrag';
import { laneItems } from '../lib/laneItems';
import { docStore, laneColor, segsOf } from '../store/docStore';
import { dragStore } from '../store/dragStore';
import { selStore, setActiveTrack, shownSel } from '../store/selectionStore';
import { tAtClientX, viewStore, wOf, xOf } from '../store/viewStore';
import type { Lang, Seg, Ti } from '../types';

interface BlockProps {
  ti: Ti; i: number; left: number; width: number; text: string; sel: boolean;
  bg?: string; bc?: string;
}

/** 窄于 BLK_DETAIL_W 就不画手柄和文字：看不清，还白白拖慢排版 */
const Block = memo(function Block({ ti, i, left, width, text, sel, bg, bc }: BlockProps) {
  return (
    <div className={"blk" + (sel ? " sel" : "")} data-i={i} data-ti={ti}
      style={{ left: left + "px", width: width + "px", background: bg, borderColor: bc }}>
      {width >= BLK_DETAIL_W && <>
        <div className="h l" data-h="l" />
        <div className="txt">{text}</div>
        <div className="h r" data-h="r" />
      </>}
    </div>
  );
});

/** 密度块：一串窄块并出来的纯视觉条（这个缩放下本来也点不准，索性不接事件） */
const AggBlock = memo(function AggBlock({ ti, i0, i1, left, width, sel, bg }: {
  ti: Ti; i0: number; i1: number; left: number; width: number; sel: boolean; bg?: string;
}) {
  return (
    <div className={"aggblk" + (sel ? " sel" : "")} data-ti={ti} data-i0={i0} data-i1={i1}
      style={{ left: left + "px", width: Math.max(width, 2) + "px", background: bg }} />
  );
});

interface LaneProps {
  ti: Ti; lang: Lang; height: number; fold: boolean; vis: boolean;
  /** 双击块时要聚焦哪个编辑框 */
  onFocusText(lang: Lang): void;
}

export function Lane({ ti, lang, height, fold, vis, onFocusText }: LaneProps) {
  docStore.use(s => s.version);
  const blkWin = viewStore.use(s => s.blkWin);
  viewStore.use(s => s.pps);
  // 聚焦成片时片段一变（拖片段边缘），后面的块都要跟着挪
  viewStore.use(s => s.pieces);
  selStore.use(s => s.selSet);
  selStore.use(s => s.preview);
  const dropping = dragStore.use(s => s.dropTi === ti);

  const cls = "lane " + (ti < 0 ? lang : "custom")
    + (fold ? " foldlane" : (!vis ? " hiddenlane" : ""))
    + (dropping ? " dropping" : "");

  // 藏起来/折叠掉的轨块都不显示，索性连建都不建
  const hideAll = fold || !vis;
  const arr = segsOf(ti);
  const items = (hideAll || !blkWin) ? [] : laneItems(arr, blkWin);
  const selSet = shownSel();
  const rgb = ti < 0 ? null : laneColor(ti, lang).slice(4, -1);

  return (
    <div className={cls} data-ti={ti} data-lang={lang} style={{ height: height + "px" }}
      onPointerDown={e => onLanePointerDown(e, ti)}
      onDoubleClick={e => {
        // 双击块 → 聚焦本 lane 对应的原文/译文编辑框；双击空白 → 在该轨该处新建字幕
        if ((e.target as HTMLElement).closest(".blk")) { onFocusText(lang); return; }
        setActiveTrack(ti, { silent: true });
        addSegmentAt(tAtClientX(e.clientX));
      }}>
      {items.map(it => it.kind === "blk"
        ? <Block key={it.i} ti={ti} i={it.i}
          left={xOf(it.seg.t0)} width={Math.max(wOf(it.seg.t0, it.seg.t1), 8)}
          text={(lang === "ja" ? it.seg.ja : it.seg.zh) || "（空）"}
          sel={selSet.has(it.seg)}
          bg={rgb ? `rgba(${rgb},.16)` : undefined}
          bc={rgb ? `rgba(${rgb},.55)` : undefined} />
        : <AggBlock key={"a" + it.i0} ti={ti} i0={it.i0} i1={it.i1} left={it.x} width={it.w}
          sel={aggSel(arr, it.i0, it.i1, selSet)}
          bg={rgb ? `rgba(${rgb},.45)` : undefined} />)}
    </div>
  );
}

/** 密度块没法用单个下标对应单句：按并进来的下标区间现查有没有落进选中集合的 */
function aggSel(arr: Seg[], i0: number, i1: number, selSet: Set<Seg>) {
  for (let k = i0; k <= i1; k++) if (selSet.has(arr[k])) return true;
  return false;
}
