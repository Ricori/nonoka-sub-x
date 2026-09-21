import { useEffect } from 'react';
import { shallowEqual } from '../../home/lib/createStore';
import { getPlayRes, getStyleSheet } from '../ass';
import { anchorOf, beginDrag, nudgeSelection, styleAngle } from '../lib/stageDrag';
import { boxForLane } from '../lib/stageHit';
import type { SubtitleBox } from '../lib/stageHit';
import { pushHistory } from '../lib/history';
import { patchStyle } from '../lib/styleEdit';
import { docStore } from '../store/docStore';
import { playStore } from '../store/playStore';
import { clearStageSelection, stageStore } from '../store/stageStore';
import type { Handle } from '../store/stageStore';
import { hGroupOf, ignoresMarginV } from '../../subtitles/layout';

/**
 * 预览画面里的选中框。整层 pointer-events: none，只有框和各个柄吃事件——
 * 舞台空白处照常是「点一下播放/暂停」。命中测试本身委托给 .stage 的 onPointerDown
 * （见 VideoStage + lib/stageDrag），这里只负责画。
 *
 * 框一律用百分比定位：拉窗口、拖侧栏时它自动跟着舞台走，不用重新测量。
 */

const HANDLES: Handle[] = ["nw", "n", "ne", "e", "se", "s", "sw", "w"];
const CORNER: Record<string, string> = { nw: "nwse", ne: "nesw", se: "nwse", sw: "nesw", n: "ns", s: "ns", e: "ew", w: "ew" };

const pct = (value: number, span: number) => (value / span) * 100 + "%";

export function StageOverlay({ stageRef }: { stageRef: React.RefObject<HTMLDivElement | null> }) {
  const { sel, guides, hint, dragging } = stageStore.use(
    s => ({ sel: s.sel, guides: s.guides, hint: s.hint, dragging: !!s.drag }), shallowEqual);
  const playing = playStore.use(s => s.playing);
  playStore.use(s => s.t);          // 换了句子框要跟着换
  docStore.use(s => s.version);     // 改了样式/文本也要重算

  // 方向键在 useShortcuts 里被全局占着（←→ 跳播放头、↑↓ 切句）。挂捕获阶段抢在它前面，
  // 没选中字幕时原样放行，于是不选中就是原来的行为。
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (!stageStore.get().sel) return;
      const tag = document.activeElement?.tagName;
      if (tag === "TEXTAREA" || tag === "INPUT") return;
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        clearStageSelection();
        return;
      }
      const step = event.shiftKey ? 10 : 1;
      const move: Record<string, [number, number]> = {
        ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step],
      };
      const delta = move[event.key];
      if (!delta || event.ctrlKey || event.metaKey || event.altKey) return;
      event.preventDefault();
      event.stopPropagation();
      nudgeSelection(delta[0], delta[1]);
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, []);

  // 播放中不摆位置：框藏起来，也省掉每帧重算命中盒
  const box = sel && !playing ? boxForLane(sel) : null;
  if (!box) return <div className="stage-edit" aria-hidden />;

  const { x: X, y: Y } = getPlayRes();
  const style = getStyleSheet().styleMap[box.styleName];
  const angle = styleAngle(box.styleName);
  const anchor = anchorOf(box.block, style?.align ?? 2);
  const grab = (kind: "move" | "resize" | "rotate", handle?: Handle) =>
    (event: React.PointerEvent) => {
      event.stopPropagation();
      event.preventDefault();
      if (stageRef.current) beginDrag(event, stageRef.current, box, kind, handle);
    };

  const lockedVertically = !!style && ignoresMarginV(style.align);

  return (
    <div className="stage-edit">
      {guides.x.map(x => <i key={"x" + x} className="stage-guide v" style={{ left: pct(x, X) }} />)}
      {guides.y.map(y => <i key={"y" + y} className="stage-guide h" style={{ top: pct(y, Y) }} />)}

      <div className={"sel-box" + (dragging ? " dragging" : "") + (box.seg ? "" : " ghost")}
        style={{
          left: pct(box.block.left, X),
          top: pct(box.block.top, Y),
          width: pct(box.block.width, X),
          height: pct(box.block.height, Y),
          transform: angle ? `rotate(${-angle}deg)` : undefined,
          transformOrigin: box.block.width && box.block.height
            ? `${((anchor.x - box.block.left) / box.block.width) * 100}% `
              + `${((anchor.y - box.block.top) / box.block.height) * 100}%`
            : "center",
        }}
        onPointerDown={grab("move")}
        onClick={event => event.stopPropagation()}>

        <span className="sel-tag">
          {box.trackName} · {box.lang === "ja" ? "原文" : "译文"} · {box.styleName}
        </span>

        <span className="sel-rotate" title="拖动旋转（按住 Shift 吸附 15°）"
          onPointerDown={grab("rotate")} onClick={event => event.stopPropagation()} />

        {HANDLES.map(handle => (
          <span key={handle} className={"sel-handle h-" + handle}
            style={{ cursor: CORNER[handle] + "-resize" }}
            title={handle.length === 2 ? "拖动改字号" : handle === "e" || handle === "w" ? "拖动改横向缩放" : "拖动改纵向缩放"}
            onPointerDown={grab("resize", handle)}
            onClick={event => event.stopPropagation()} />
        ))}
      </div>

      {lockedVertically && hint === "middle-v" && (
        <MiddleHint box={box} align={style!.align} playResY={Y} />
      )}
      {!!box.shifted && !dragging && (
        <div className="stage-note" style={{ top: pct(box.block.top, Y) }}>
          这条被同一侧的别的字幕挤开了，所以没停在边距说的位置
        </div>
      )}
    </div>
  );
}

/**
 * 垂直居中（Alignment 4/5/6）时 libass 根本不看 MarginV，竖直拖动毫无反应。
 * 拖动不擅自改对齐方式（那很难预料），改成给一个一键出口。
 */
function MiddleHint({ box, align, playResY }: { box: SubtitleBox; align: number; playResY: number }) {
  const toBottom = () => {
    // 切到下对齐的同时把 MarginV 定在当前位置，视觉上不跳
    pushHistory();
    patchStyle(box.styleName, {
      alignment: hGroupOf(align),
      marginv: Math.max(0, Math.round(playResY - (box.block.top + box.block.height))),
    });
    stageStore.set({ hint: null });
  };
  return (
    <div className="stage-note warn" style={{ top: pct(box.block.top + box.block.height, playResY) }}>
      纵向被锁定：中间对齐时 libass 忽略 MarginV
      <button onPointerDown={event => event.stopPropagation()} onClick={toBottom}>切到底部对齐</button>
    </div>
  );
}
