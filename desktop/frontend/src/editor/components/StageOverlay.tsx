import { useEffect, useLayoutEffect, useRef } from 'react';
import { shallowEqual } from '../../home/lib/createStore';
import { getPlayRes, getStyleSheet } from '../ass';
import { anchorOf, beginDrag, nudgeSelection, styleAngle } from '../lib/stageDrag';
import { boxForLane } from '../lib/stageHit';
import type { SubtitleBox } from '../lib/stageHit';
import { setSegText } from '../lib/edits';
import { armPending, commitPending, disarmPending, pushHistory } from '../lib/history';
import { patchStyle } from '../lib/styleEdit';
import { docStore } from '../store/docStore';
import { playStore } from '../store/playStore';
import { clearStageSelection, stageStore } from '../store/stageStore';
import type { Handle } from '../store/stageStore';
import type { Seg } from '../types';
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
  const { sel, guides, hint, dragging, edit } = stageStore.use(
    s => ({ sel: s.sel, guides: s.guides, hint: s.hint, dragging: !!s.drag, edit: s.edit }), shallowEqual);
  const playing = playStore.use(s => s.playing);
  const t = playStore.use(s => s.t);   // 换了句子框要跟着换
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
      // 播放头停在这条 lane 的空隙里：框没画出来，方向键原样交还给全局快捷键
      const lane = stageStore.get().sel;
      if (!lane || !boxForLane(lane)?.seg) return;
      event.preventDefault();
      event.stopPropagation();
      nudgeSelection(delta[0], delta[1]);
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, []);

  // 就地改字：只要那一句还在画面上就一直开着；起播、播放头离开这句就收起
  // 文字删空时画面上就没有这块了，框改用上一次量到的位置，别半路收起
  const lastEditBox = useRef<SubtitleBox | null>(null);
  const editing = !!edit && !playing && t >= edit.seg.t0 && t < edit.seg.t1;
  const liveEditBox = editing ? boxForLane(edit) : null;
  if (!edit) lastEditBox.current = null;
  else if (liveEditBox?.seg === edit.seg) lastEditBox.current = liveEditBox;
  useEffect(() => {
    if (edit && !editing) stageStore.set({ edit: null });
  }, [edit, editing]);
  if (editing && edit && lastEditBox.current) {
    return <div className="stage-edit">
      <InlineEditor box={lastEditBox.current} seg={edit.seg} stage={stageRef.current} />
    </div>;
  }

  // 播放中不摆位置：框藏起来，也省掉每帧重算命中盒。
  // 播放头停在这条 lane 没字的空隙里也不画框——选中保留，走回有字的地方框再出来
  const box = sel && !playing ? boxForLane(sel) : null;
  if (!box?.seg) return <div className="stage-edit" aria-hidden />;

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

      <div className={"sel-box" + (dragging ? " dragging" : "")}
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
 * 画面上的就地改字框：盖在那行字上，字号按舞台缩放近似原字号。
 * 撤销口径和右栏的文本框一致（聚焦时布一个撤销起点，连续输入并成一步）。
 * Enter 确认收起，Shift+Enter 换行，Esc 收起。
 */
function InlineEditor({ box, seg, stage }: { box: SubtitleBox; seg: Seg; stage: HTMLElement | null }) {
  const ref = useRef<HTMLTextAreaElement>(null);
  const { x: X, y: Y } = getPlayRes();
  const style = getStyleSheet().styleMap[box.styleName];
  const scale = stage ? stage.clientHeight / Y : 0.5;
  const fontPx = Math.max(12, Math.min(40, (style?.size ?? 60) * scale * 0.8));
  const value = seg[box.lang];
  const close = () => { disarmPending(); stageStore.set({ edit: null }); };

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.focus();
    el.select();
  }, []);
  // 高度跟着内容长
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = el.scrollHeight + "px";
  }, [value, fontPx]);

  const minW = 32;   // 百分比：短句也留出能打字的宽度
  const width = Math.min(96, Math.max(minW, (box.block.width / X) * 100 + 6));
  const centre = ((box.block.left + box.block.width / 2) / X) * 100;
  const left = Math.min(100 - width - 2, Math.max(2, centre - width / 2));

  return (
    <div className="stage-inline-edit"
      style={{ left: left + "%", width: width + "%", top: pct(box.block.top, Y) }}
      onPointerDown={event => event.stopPropagation()}
      onClick={event => event.stopPropagation()}
      onDoubleClick={event => event.stopPropagation()}>
      <textarea ref={ref} rows={1} spellCheck={false} value={value}
        className={box.lang === "ja" ? "ja" : undefined}
        style={{ fontSize: fontPx + "px", fontFamily: style?.font ? `"${style.font}", var(--font-ui)` : undefined }}
        onFocus={armPending}
        onBlur={close}
        onChange={event => { commitPending(); setSegText(box.lang, event.target.value); }}
        onKeyDown={event => {
          event.stopPropagation();
          if (event.key === "Escape" || (event.key === "Enter" && !event.shiftKey)) {
            event.preventDefault();
            close();
          }
        }} />
      <span className="stage-inline-tip">Enter 完成 · Shift+Enter 换行 · Esc 退出</span>
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
