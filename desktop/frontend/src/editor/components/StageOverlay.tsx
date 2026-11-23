import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { shallowEqual } from '../../home/lib/createStore';
import { getPlayRes, getStyleSheet } from '../ass';
import { anchorOf, beginDrag, nudgeSelection, openInlineEditForBox, styleAngle } from '../lib/stageDrag';
import { boxForLane } from '../lib/stageHit';
import type { SubtitleBox } from '../lib/stageHit';
import { setSegText } from '../lib/edits';
import { armPending, commitPending, disarmPending, pushHistory } from '../lib/history';
import { patchStyle } from '../lib/styleEdit';
import { docStore, laneNames } from '../store/docStore';
import { playStore } from '../store/playStore';
import { patchScreenTextOptions, screenTextStore } from '../store/screenTextStore';
import { clearStageSelection, stageStore } from '../store/stageStore';
import { modalStore } from '../store/uiStore';
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

const HANDLES: Handle[] = ["w", "e"];

const pct = (value: number, span: number) => (value / span) * 100 + "%";

export function StageOverlay({ stageRef }: { stageRef: React.RefObject<HTMLDivElement | null> }) {
  const { sel, guides, hint, dragging, edit } = stageStore.use(
    s => ({ sel: s.sel, guides: s.guides, hint: s.hint, dragging: !!s.drag, edit: s.edit }), shallowEqual);
  const { drawingRoi, roi } = screenTextStore.use(
    s => ({ drawingRoi: s.drawingRoi, roi: s.options.roi }), shallowEqual);
  const sideTab = modalStore.use(s => s.sideTab);
  const playing = playStore.use(s => s.playing);
  const t = playStore.use(s => s.t);   // 换了句子框要跟着换
  docStore.use(s => s.version);     // 改了样式/文本也要重算

  // 切离「文字识别」页签时退出框选模式
  useEffect(() => {
    if (sideTab !== "screen-text" && drawingRoi) screenTextStore.set({ drawingRoi: false });
  }, [sideTab, drawingRoi]);

  useEffect(() => {
    if (!drawingRoi) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        screenTextStore.set({ drawingRoi: false });
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [drawingRoi]);

  // 点画面以外的地方就取消选中。舞台自己的点击由 onStagePointerDown 处理（点空白也会取消）；
  // 样式页和它弹出的下拉/取色器/弹窗算「还在编辑这条」，不取消
  useEffect(() => {
    const keep = ".stage, .side-style-page, .custom-select-menu, .cp-pop, .modal, .ctxmenu";
    const onDown = (event: PointerEvent) => {
      if (!stageStore.get().sel) return;
      const target = event.target instanceof Element ? event.target : null;
      if (target?.closest(keep)) return;
      clearStageSelection();
    };
    document.addEventListener("pointerdown", onDown, true);
    return () => document.removeEventListener("pointerdown", onDown, true);
  }, []);

  // 方向键在 useShortcuts 里被全局占着（←→ 跳播放头、↑↓ 切句）。只有右栏在「样式」页时
  // 才挂捕获阶段抢过来挪字幕；其他页（尤其「字幕」页）方向键照旧切句、跳播放头。
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (!stageStore.get().sel) return;
      const active = document.activeElement;
      const tag = active?.tagName;
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
      if (modalStore.get().sideTab !== "style") return;
      // 焦点在下拉框上时方向键是给它选项用的
      if (active?.closest(".custom-select") || document.querySelector(".custom-select-menu")) return;
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

  // 文字识别 ROI 框选层：优先接管舞台拖拽
  if (drawingRoi) {
    return <RoiDrawLayer stageRef={stageRef} existing={roi} />;
  }

  // 播放中不摆位置：框藏起来，也省掉每帧重算命中盒。
  // 播放头停在这条 lane 没字的空隙里也不画框——选中保留，走回有字的地方框再出来
  const box = sel && !playing ? boxForLane(sel) : null;
  if (!box?.seg) return <div className="stage-edit" aria-hidden="true" />;

  const { x: X, y: Y } = getPlayRes();
  const style = getStyleSheet().styleMap[box.styleName];
  const angle = styleAngle(box.styleName);
  const anchor = anchorOf(box.block, box.align);
  const grab = (kind: "move" | "resize" | "rotate", handle?: Handle) =>
    (event: React.PointerEvent) => {
      event.stopPropagation();
      event.preventDefault();
      if (stageRef.current) beginDrag(event, stageRef.current, box, kind, handle);
    };

  // Positioned text moves by \pos, so MarginV and the margin handles do not apply
  const lockedVertically = !box.positioned && !!style && ignoresMarginV(style.align);

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
        onClick={event => event.stopPropagation()}
        onDoubleClick={event => {
          event.stopPropagation();
          if (openInlineEditForBox(box)) event.preventDefault();
        }}>

        <span className="sel-tag">
          {box.trackName} · {laneNames()[box.lang]} · {box.styleName}
        </span>

        {!box.positioned && <span className="sel-rotate" title="拖动旋转（按住 Shift 吸附 15°）"
          onPointerDown={grab("rotate")} onClick={event => event.stopPropagation()}
          onDoubleClick={event => event.stopPropagation()} />}

        {!box.positioned && HANDLES.map(handle => (
          <span key={handle} className={"sel-handle h-" + handle}
            style={{ cursor: "ew-resize" }}
            title="拖动调整文本框宽度（按住 Alt 取消吸附）"
            onPointerDown={grab("resize", handle)}
            onClick={event => event.stopPropagation()}
            onDoubleClick={event => event.stopPropagation()} />
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
  const size = box.positioned ? box.block.lineHeight : (style?.size ?? 60);
  const fontPx = Math.max(12, Math.min(40, size * scale * 0.8));
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

  const width = Math.min(100, Math.max(12, (box.block.width / X) * 100));
  const left = Math.min(100 - width, Math.max(0, (box.block.left / X) * 100));
  const hGroup = hGroupOf(box.align);
  const textAlign = hGroup === 1 ? "left" : hGroup === 3 ? "right" : "center";
  // 下半屏的字幕：框贴着字的下沿往上长、提示放上面，否则提示会被舞台的 overflow 裁掉
  const lower = box.block.top + box.block.height / 2 > Y / 2;
  const place = lower
    ? { bottom: Math.max(1, ((Y - box.block.top - box.block.height) / Y) * 100) + "%" }
    : { top: Math.max(1, (box.block.top / Y) * 100) + "%" };

  return (
    <div className={"stage-inline-edit" + (lower ? " up" : "")}
      style={{ left: left + "%", width: width + "%", ...place }}
      onPointerDown={event => event.stopPropagation()}
      onClick={event => event.stopPropagation()}
      onDoubleClick={event => event.stopPropagation()}>
      <textarea ref={ref} rows={1} spellCheck={false} value={value}
        className={box.lang === "ja" ? "ja" : undefined}
        style={{
          fontSize: fontPx + "px",
          fontFamily: style?.font ? `"${style.font}", var(--font-ui)` : undefined,
          textAlign,
        }}
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

function RoiBox({ rect }: { rect: [number, number, number, number] }) {
  const [x0, y0, x1, y1] = rect;
  return (
    <div className="roi-box" style={{
      left: `${x0 * 100}%`, top: `${y0 * 100}%`,
      width: `${(x1 - x0) * 100}%`, height: `${(y1 - y0) * 100}%`,
    }}>
      <span className="roi-tag">识别区域</span>
    </div>
  );
}

function RoiDrawLayer({ stageRef, existing }: {
  stageRef: React.RefObject<HTMLDivElement | null>;
  existing: [number, number, number, number] | null;
}) {
  const [draft, setDraft] = useState<[number, number, number, number] | null>(null);
  const originRef = useRef<[number, number] | null>(null);

  const norm = (clientX: number, clientY: number): [number, number] | null => {
    const el = stageRef.current;
    if (!el) return null;
    const rect = el.getBoundingClientRect();
    if (!rect.width || !rect.height) return null;
    const x = Math.min(1, Math.max(0, (clientX - rect.left) / rect.width));
    const y = Math.min(1, Math.max(0, (clientY - rect.top) / rect.height));
    return [Math.round(x * 1000) / 1000, Math.round(y * 1000) / 1000];
  };

  const rectOf = (a: [number, number], b: [number, number]): [number, number, number, number] => [
    Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.max(a[0], b[0]), Math.max(a[1], b[1]),
  ];

  return (
    <div className="stage-edit roi-draw-layer"
      onPointerDown={(event) => {
        event.stopPropagation();
        event.preventDefault();
        const pt = norm(event.clientX, event.clientY);
        if (!pt) return;
        originRef.current = pt;
        setDraft([pt[0], pt[1], pt[0], pt[1]]);
        (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
      }}
      onPointerMove={(event) => {
        if (!originRef.current) return;
        event.stopPropagation();
        const pt = norm(event.clientX, event.clientY);
        if (pt) setDraft(rectOf(originRef.current, pt));
      }}
      onPointerUp={(event) => {
        if (!originRef.current) return;
        event.stopPropagation();
        const pt = norm(event.clientX, event.clientY);
        const finalRect = pt ? rectOf(originRef.current, pt) : draft;
        originRef.current = null;
        if (finalRect && (finalRect[2] - finalRect[0]) >= 0.02 && (finalRect[3] - finalRect[1]) >= 0.02) {
          patchScreenTextOptions({ roi: finalRect });
        }
        screenTextStore.set({ drawingRoi: false });
      }}
      onClick={(event) => event.stopPropagation()}>
      <div className="roi-banner">拖动框选识别区域 · 按 Esc 取消</div>
      {(draft ?? existing) && <RoiBox rect={(draft ?? existing)!} />}
    </div>
  );
}
