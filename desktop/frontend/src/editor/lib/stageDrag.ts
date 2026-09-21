import { hGroupOf, ignoresMarginV, layoutBlock, marginsForBlock, vGroupOf } from '../../subtitles/layout';
import type { BlockRect, Rect } from '../../subtitles/layout';
import type { StylePatch } from '../../subtitles/styleEdit';
import { getPlayRes, getStyleSheet } from '../ass';
import { layoutStore } from '../store/layoutStore';
import { playStore } from '../store/playStore';
import { selectLane, stageStore } from '../store/stageStore';
import type { Handle } from '../store/stageStore';
import { pushHistory } from './history';
import { boxForLane, hitTest, sameLane, subtitleBoxesAt } from './stageHit';
import type { SubtitleBox } from './stageHit';
import { patchStyle, styleFields } from './styleEdit';
import type { AssStyle } from '../types';

/**
 * 预览画面里的拖动：移动改三个 Margin、拖角改 Fontsize、拖边改 ScaleX/ScaleY、
 * 拖顶上的柄改 Angle。写回的都是**样式**，所以同一个样式绑在几条轨上就一起动
 * （调用方给「复制一份专属样式」的出路）。
 *
 * 几条从 laneDrag.ts 沿用的约定：pointer capture 挂在稳定祖先（.stage）上，因为框每帧
 * 重渲染；pushHistory 在第一次 pointermove 才懒触发，于是纯点击不进撤销栈；
 * pointercancel 与 pointerup 一起处理。
 */

/** 吸附容差（屏幕像素）。换算成 PlayRes 单位后随舞台大小变，屏幕上看着恒定 */
const SNAP_PX = 6;
const MIN_FONT = 8, MAX_FONT = 400;
const MIN_SCALE = 10, MAX_SCALE = 400;

interface Session {
  kind: "move" | "resize" | "rotate";
  handle?: Handle;
  styleName: string;
  text: string;
  /** 起拖那一刻的样式：居中反解要用它的 ml+mr，用实时值会一帧一帧漂 */
  style0: AssStyle;
  block0: BlockRect;
  /** resize 的对角锚点 / rotate 的旋转中心，PlayRes 坐标 */
  pivot: { x: number; y: number };
  angle0: number;
  screenAngle0: number;
  origin: { x: number; y: number };
  others: SubtitleBox[];
  sx: number;
  sy: number;
  rect: DOMRect;
  pushed: boolean;
}

let session: Session | null = null;
/** 命中字幕时吞掉紧随其后的那次 click —— pointerdown 和 click 是两个事件，stopPropagation 拦不住 */
let swallowClick = false;
let frame = 0;
let pending: PointerEvent | null = null;

export const shouldSwallowStageClick = () => {
  if (!swallowClick) return false;
  swallowClick = false;
  return true;
};

const toPlayRes = (rect: DOMRect, sx: number, sy: number, clientX: number, clientY: number) =>
  ({ x: (clientX - rect.left) / sx, y: (clientY - rect.top) / sy });

/** 样式的对齐锚点——libass 的 \frz 默认绕它转，选中框也按它做旋转原点 */
export function anchorOf(block: BlockRect, align: number) {
  const h = hGroupOf(align), v = vGroupOf(align);
  return {
    x: block.left + (h === 1 ? 0 : h === 2 ? block.width / 2 : block.width),
    y: block.top + (v === 3 ? 0 : v === 2 ? block.height / 2 : block.height),
  };
}

const screenAngleAt = (pivot: { x: number; y: number }, x: number, y: number) =>
  Math.atan2(y - pivot.y, x - pivot.x) * 180 / Math.PI;

/** 归一到 (-180, 180]，免得连着转几圈后数值变得没法读 */
function normaliseAngle(deg: number): number {
  let value = ((deg + 180) % 360 + 360) % 360 - 180;
  if (value === -180) value = 180;
  return Math.round(value * 10) / 10;
}

const clamp = (value: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, value));

// ── 吸附 ─────────────────────────────────────────────────────
// 候选：画面中线、标题安全区、样式自己当前的边距（吸回原位），以及别的字幕块的边。

function snapTargets(session: Session) {
  const { x: X, y: Y } = getPlayRes();
  const xs = [X / 2, X * 0.1, X * 0.9, session.style0.ml, X - session.style0.mr];
  const ys = [Y / 2, Y * 0.1, Y * 0.9, session.block0.top, session.block0.top + session.block0.height];
  for (const box of session.others) {
    xs.push(box.block.left, box.block.left + box.block.width / 2, box.block.left + box.block.width);
    ys.push(box.block.top, box.block.top + box.block.height / 2, box.block.top + box.block.height);
  }
  return { xs, ys };
}

/** 三个参考点里最先落进容差的那个吸附量；没有就返回 0 */
function bestSnap(edges: number[], targets: number[], tolerance: number) {
  let best = 0, bestDistance = tolerance, hit: number | null = null;
  for (const target of targets) {
    for (const edge of edges) {
      const distance = Math.abs(target - edge);
      if (distance < bestDistance) { bestDistance = distance; best = target - edge; hit = target; }
    }
  }
  return { delta: best, guide: hit };
}

function applySnap(session: Session, want: Rect, off: boolean): Rect {
  if (off || !layoutStore.get().snap) {
    stageStore.set({ guides: { x: [], y: [] } });
    return want;
  }
  const { xs, ys } = snapTargets(session);
  const tolerance = SNAP_PX / session.sx;
  const x = bestSnap([want.left, want.left + want.width / 2, want.left + want.width], xs, tolerance);
  const y = bestSnap([want.top, want.top + want.height / 2, want.top + want.height], ys, SNAP_PX / session.sy);
  stageStore.set({
    guides: { x: x.guide === null ? [] : [x.guide], y: y.guide === null ? [] : [y.guide] },
  });
  return { ...want, left: want.left + x.delta, top: want.top + y.delta };
}

// ── 各种拖动 ──────────────────────────────────────────────────

function moveTo(session: Session, point: { x: number; y: number }, alt: boolean) {
  const dx = point.x - session.origin.x;
  // 垂直居中时 libass 忽略 MarginV：锁死 Y 轴，并在框上挂个一键切换的提示
  const locked = ignoresMarginV(session.style0.align);
  const dy = locked ? 0 : point.y - session.origin.y;
  const want = applySnap(session, {
    left: session.block0.left + dx, top: session.block0.top + dy,
    width: session.block0.width, height: session.block0.height,
  }, alt);
  patchStyle(session.styleName, marginsForBlock(want, session.style0, getStyleSheet()));
  if (locked) stageStore.set({ hint: "middle-v" });
}

function resizeTo(session: Session, point: { x: number; y: number }) {
  const sheet = getStyleSheet();
  const handle = session.handle ?? "se";
  const corner = handle.length === 2;
  const patch: StylePatch = {};
  let style = session.style0;

  if (corner) {
    // 以纵向定比例：行高严格正比于字号，块宽却是仿射的（Spacing 与字号无关），
    // 用宽度算的话 Spacing ≠ 0 时比例会漂
    const ratio = Math.abs(point.y - session.pivot.y) / Math.max(1, session.block0.height);
    const size = clamp(Math.round(session.style0.size * ratio), MIN_FONT, MAX_FONT);
    patch.fontsize = size;
    style = { ...session.style0, size };
  } else if (handle === "e" || handle === "w") {
    const ratio = Math.abs(point.x - session.pivot.x) / Math.max(1, session.block0.width);
    const scx = clamp(Math.round(session.style0.scx * ratio), MIN_SCALE, MAX_SCALE);
    patch.scalex = scx;
    style = { ...session.style0, scx };
  } else {
    const ratio = Math.abs(point.y - session.pivot.y) / Math.max(1, session.block0.height);
    const scy = clamp(Math.round(session.style0.scy * ratio), MIN_SCALE, MAX_SCALE);
    patch.scaley = scy;
    style = { ...session.style0, scy };
  }

  // 新尺寸下重排一次再反解边距，锚点才钉得住——这就是缩放手感的全部
  const grown = session.text
    ? layoutBlock(session.text, style, sheet)
    : { ...session.block0, height: style.size * style.scy / 100 };
  const want: Rect = {
    left: handle.includes("w") ? session.pivot.x - grown.width : session.pivot.x,
    top: handle.includes("n") ? session.pivot.y - grown.height : session.pivot.y,
    width: grown.width, height: grown.height,
  };
  patchStyle(session.styleName, { ...patch, ...marginsForBlock(want, style, sheet) });
}

function rotateTo(session: Session, point: { x: number; y: number }, shift: boolean) {
  // ASS 的 \frz 逆时针为正，而屏幕 y 轴朝下让 atan2 顺时针为正，所以取负号
  const delta = screenAngleAt(session.pivot, point.x, point.y) - session.screenAngle0;
  let angle = normaliseAngle(session.angle0 - delta);
  if (shift) angle = Math.round(angle / 15) * 15;
  patchStyle(session.styleName, { angle });
}

// ── 会话 ──────────────────────────────────────────────────────

function apply(event: PointerEvent) {
  if (!session) return;
  const point = toPlayRes(session.rect, session.sx, session.sy, event.clientX, event.clientY);
  if (!session.pushed) {
    // 懒入栈：光点一下选中不该占一步撤销
    pushHistory();
    session.pushed = true;
  }
  if (session.kind === "move") moveTo(session, point, event.altKey);
  else if (session.kind === "resize") resizeTo(session, point);
  else rotateTo(session, point, event.shiftKey);
}

export function beginDrag(
  event: React.PointerEvent | PointerEvent,
  stage: HTMLElement,
  box: SubtitleBox,
  kind: "move" | "resize" | "rotate",
  handle?: Handle,
) {
  const sheet = getStyleSheet();
  const style0 = sheet.styleMap[box.styleName];
  if (!style0) return;
  const rect = stage.getBoundingClientRect();
  const sx = rect.width / sheet.playRes.x, sy = rect.height / sheet.playRes.y;
  const origin = toPlayRes(rect, sx, sy, event.clientX, event.clientY);
  // 碰撞避让推过的块，其 block.top 与 MarginV 对不上；反解要用未推移的那份
  const block0 = box.shifted
    ? { ...box.block, top: box.block.top - box.shifted }
    : box.block;

  const anchor = anchorOf(block0, style0.align);
  const pivot = kind === "resize" && handle
    ? {
      x: handle.includes("w") ? block0.left + block0.width : handle.includes("e") ? block0.left : anchor.x,
      y: handle.includes("n") ? block0.top + block0.height : handle.includes("s") ? block0.top : anchor.y,
    }
    : anchor;

  session = {
    kind, handle, styleName: box.styleName, text: box.text, style0, block0, pivot,
    angle0: styleAngle(box.styleName),
    screenAngle0: screenAngleAt(pivot, origin.x, origin.y),
    origin, sx, sy, rect, pushed: false,
    others: subtitleBoxesAt().filter(other => !sameLane(other, box)),
  };
  stageStore.set({ drag: { kind, handle } });
  swallowClick = true;
  stage.setPointerCapture(event.pointerId);

  const onMove = (moveEvent: PointerEvent) => {
    pending = moveEvent;
    if (frame) return;
    frame = requestAnimationFrame(() => {
      frame = 0;
      if (pending) apply(pending);
      pending = null;
    });
  };
  const done = () => {
    stage.removeEventListener("pointermove", onMove);
    stage.removeEventListener("pointerup", done);
    stage.removeEventListener("pointercancel", done);
    if (frame) { cancelAnimationFrame(frame); frame = 0; }
    if (pending) { apply(pending); pending = null; }
    session = null;
    stageStore.set({ drag: null, guides: { x: [], y: [] } });
  };
  stage.addEventListener("pointermove", onMove);
  stage.addEventListener("pointerup", done);
  stage.addEventListener("pointercancel", done);
}

/** Angle 不在 AssStyle 的建模范围内（它是有损投影），只能从 23 字段原文里读 */
export function styleAngle(name: string): number {
  const value = Number(styleFields(name).angle);
  return Number.isFinite(value) ? value : 0;
}

/**
 * 舞台上的 pointerdown：命中字幕就选中并起拖，没命中就什么都不做——
 * .stage 原来的 onClick 照常切播放。
 */
export function onStagePointerDown(event: React.PointerEvent, stage: HTMLElement): boolean {
  if (event.button !== 0 || playStore.get().playing) return false;
  const sheet = getStyleSheet();
  const rect = stage.getBoundingClientRect();
  const point = toPlayRes(rect, rect.width / sheet.playRes.x, rect.height / sheet.playRes.y,
    event.clientX, event.clientY);
  const boxes = subtitleBoxesAt();
  const current = stageStore.get().sel;
  const cycleFrom = event.altKey && current
    ? boxes.find(box => sameLane(box, current))?.index
    : undefined;
  const hit = hitTest(boxes, point.x, point.y, cycleFrom);
  if (!hit) return false;
  event.preventDefault();
  selectLane({ trackId: hit.trackId, lang: hit.lang });
  beginDrag(event, stage, hit, "move");
  return true;
}

// 连着按方向键算一步撤销：隔了这么久再按才重新落栈（换了选中同样重新落栈，
// 时刻记在 stageStore 里，selectLane 会把它清零）
const NUDGE_COALESCE_MS = 800;

/** 方向键微调 */
export function nudgeSelection(dx: number, dy: number) {
  const lane = stageStore.get().sel;
  if (!lane) return;
  const box = boxForLane(lane);
  const sheet = getStyleSheet();
  const style = box && sheet.styleMap[box.styleName];
  if (!box || !style) return;
  if (ignoresMarginV(style.align) && dy) {
    stageStore.set({ hint: "middle-v" });
    dy = 0;
  }
  if (!dx && !dy) return;
  const now = Date.now();
  const previous = stageStore.get().nudgeAt;
  if (!previous || now - previous > NUDGE_COALESCE_MS) pushHistory();
  stageStore.set({ nudgeAt: now });
  const block = box.shifted ? { ...box.block, top: box.block.top - box.shifted } : box.block;
  patchStyle(box.styleName, marginsForBlock({
    left: block.left + dx, top: block.top + dy, width: block.width, height: block.height,
  }, style, sheet));
}
