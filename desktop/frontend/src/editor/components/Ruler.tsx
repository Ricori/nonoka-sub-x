import { Fragment } from 'react';
import { shallowEqual } from '../../home/lib/createStore';
import { CLIP_LANE_H } from '../constants';
import { commitClipEdge, dragClipEdgeTo, enterClip, removeClip, renameClip } from '../lib/clips';
import { bindScrub } from '../lib/laneDrag';
import { openExport } from '../lib/openExport';
import { innerLeft } from '../store/tlStore';
import { modalStore, showCtx } from '../store/uiStore';
import { clipLayout, tOf, viewDur, viewStore, xOf } from '../store/viewStore';
import { fmt } from '../utils';
import type { Clip } from '../types';

// 放大到 400px/s 时 0.5s 一格太疏，补上 0.1/0.2 两档（标签显示到秒的两位小数）
const STEPS = [0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800];

interface Tick { s: number; major: boolean; label?: string; endlab?: boolean }

/** 60 分钟 × 140px/s 的整条时间轴远超一次能铺的量，刻度只生成视口附近的一段 */
function ticksFor(left: number, vw: number, pps: number): Tick[] {
  const major = STEPS.find(s => s * pps >= 70) || 3600;
  const minor = major / 5;
  const dur = viewDur();
  // 刻度按「视图内相对秒」打，标签才会在切片里从 00:00 起
  const a = Math.max(0, Math.floor((left - vw * 0.5) / pps / minor) * minor);
  const b = Math.min(dur, (left + vw * 1.5) / pps);
  const out: Tick[] = [];
  for (let s = a; s <= b + 1e-6; s += minor) {
    const isMajor = Math.abs(s / major - Math.round(s / major)) < 1e-6;
    out.push(isMajor
      // 亚秒刻度要带小数位，否则相邻标签一模一样
      ? { s, major: true, label: major < 1 ? fmt(s) : fmt(s).slice(0, 5), endlab: (dur - s) * pps < 46 }
      : { s, major: false });
  }
  return out;
}

const clipMenu = (e: React.MouseEvent, c: Clip) => showCtx(e, [
  { label: "进入切片", onClick: () => enterClip(c) },
  { label: "导出这个切片", onClick: () => void openExport(c) },
  "-",
  { label: "重命名", onClick: () => void renameClip(c) },
  { label: "删除切片", danger: true, onClick: () => void removeClip(c) },
]);

const showTip = (e: React.PointerEvent, clip: Clip) =>
  modalStore.set({ clipTip: { clip, x: e.clientX, y: e.clientY } });
const hideTip = () => modalStore.set({ clipTip: null });

/** 把某个切片的「条 + 两面旗」摆到当前缩放下该在的位置（拖动中直接动这三个元素） */
function placeClipEls(c: Clip) {
  const q = (s: string) => document.querySelector<HTMLElement>(`.ruler [data-clip="${c.id}"].${s}`);
  const bar = q("clip-mark"), l = q("l"), r = q("r");
  const pps = viewStore.get().pps;
  if (bar) { bar.style.left = xOf(c.t0) + "px"; bar.style.width = Math.max((c.t1 - c.t0) * pps, 2) + "px"; }
  if (l) l.style.left = xOf(c.t0) + "px";
  if (r) r.style.left = (xOf(c.t1) - 7) + "px";
}

/**
 * 拖黄旗改起止。只挪这三个元素、不整体重排标尺——重排会把手上这面旗的 DOM 换掉，
 * 指针捕获和 move/up 监听跟着一起没，拖到一半就断了
 */
function dragClipEdge(ev: React.PointerEvent, c: Clip, which: "l" | "r") {
  ev.preventDefault();
  hideTip();
  const el = ev.currentTarget as HTMLElement;
  el.setPointerCapture(ev.pointerId);
  const move = (e: PointerEvent) => {
    dragClipEdgeTo(c, which, tOf(e.clientX - innerLeft()));
    placeClipEls(c);
  };
  const up = () => {
    el.removeEventListener("pointermove", move);
    el.removeEventListener("pointerup", up);
    el.removeEventListener("pointercancel", up);
    commitClipEdge(c);   // pointercancel 也走这里，否则拖到一半被打断就不落盘了
  };
  el.addEventListener("pointermove", move);
  el.addEventListener("pointerup", up);
  el.addEventListener("pointercancel", up);
}

/**
 * 切片标记：不另占一条轨——起止各一枚小黄旗（可拖），中间一条细长条。
 * 层号由 clipLayout() 按时间贪心分配，条与条永不相交。
 */
function ClipMarks() {
  const { items } = clipLayout();
  const pps = viewStore.get().pps;
  if (!items.length) return null;
  return (
    <>
      {items.map(({ c, lane }) => {
        const top = lane * CLIP_LANE_H;
        return (
          <Fragment key={c.id}>
            <div className="clip-mark" data-clip={c.id}
              style={{ top: (top + 1) + "px", left: xOf(c.t0) + "px", width: Math.max((c.t1 - c.t0) * pps, 2) + "px" }}
              onPointerMove={e => showTip(e, c)}
              onPointerLeave={hideTip}
              // 标尺本身的 pointerdown 是拖播放头，这里得拦下来，否则双击变成了擦洗
              onPointerDown={e => e.stopPropagation()}
              onDoubleClick={e => { e.stopPropagation(); hideTip(); enterClip(c); }}
              onContextMenu={e => { e.stopPropagation(); hideTip(); clipMenu(e, c); }}>
              <div className="nm">{c.name}</div>
            </div>
            {(["l", "r"] as const).map(side => (
              <div key={side} className={"clip-flag " + side} data-clip={c.id}
                style={{ top: top + "px", left: (side === "l" ? xOf(c.t0) : xOf(c.t1) - 7) + "px" }}
                onPointerMove={e => showTip(e, c)}
                onPointerLeave={hideTip}
                onPointerDown={e => { e.stopPropagation(); dragClipEdge(e, c, side); }}
                onContextMenu={e => { e.stopPropagation(); hideTip(); clipMenu(e, c); }} />
            ))}
          </Fragment>
        );
      })}
    </>
  );
}

export function Ruler({ left, w }: { left: number; w: number }) {
  const { pps } = viewStore.use(
    s => ({ pps: s.pps, t0: s.t0, t1: s.t1, clips: s.clips, curClip: s.curClip }), shallowEqual);
  const ticks = ticksFor(left, w, pps);

  return (
    <div className="ruler" id="ruler" onPointerDown={e => bindScrub(e, false)}>
      {ticks.map(t => (
        <div key={t.s} className={"tick" + (t.major ? " major" : "") + (t.endlab ? " endlab" : "")}
          style={{ left: (t.s * pps) + "px" }}>
          {t.major && <span>{t.label}</span>}
        </div>
      ))}
      <ClipMarks />
    </div>
  );
}
