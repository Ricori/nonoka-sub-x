import type { Align, CoverBox, SegmentCover, SegmentPos } from './types.ts';

/**
 * Per-segment placement (`pos`) and the original text box (`cover`), shared by the editor,
 * the plugin host and the ASS builder. Everything here is pure and works in frame fractions
 * (0..1); conversion to PlayRes pixels happens only where ASS is written or boxes are laid out.
 */

const finite = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);
const clamp01 = (value: number) => Math.min(1, Math.max(0, value));

/** Keep a valid `pos`, drop anything malformed (returns undefined) */
export function normalizeSegPos(raw: unknown): SegmentPos | undefined {
  if (!raw || typeof raw !== "object") return undefined;
  const value = raw as Record<string, unknown>;
  if (!finite(value.x) || !finite(value.y)) return undefined;
  const an = Math.round(Number(value.an));
  const out: SegmentPos = {
    x: clamp01(value.x),
    y: clamp01(value.y),
    an: (an >= 1 && an <= 9 ? an : 5) as Align,
  };
  if (finite(value.w) && value.w > 0) out.w = Math.min(1, value.w);
  if (finite(value.fs) && value.fs > 0) out.fs = Math.min(1, value.fs);
  if (finite(value.lh) && value.lh > 0) out.lh = Math.min(1, value.lh);
  return out;
}

/** Keep a valid `cover`, drop anything malformed (returns undefined) */
export function normalizeSegCover(raw: unknown): SegmentCover | undefined {
  if (!raw || typeof raw !== "object") return undefined;
  const value = raw as Record<string, unknown>;
  const box = value.box;
  if (!Array.isArray(box) || box.length !== 4 || !box.every(finite)) return undefined;
  const [x0, y0, x1, y1] = (box as number[]).map(clamp01);
  if (!(x1 > x0) || !(y1 > y0)) return undefined;
  const out: SegmentCover = { box: [x0, y0, x1, y1] };
  if (value.fill === null) out.fill = null;
  else if (typeof value.fill === "string" && value.fill.trim()) out.fill = value.fill.trim();
  return out;
}

export const clonePos = (pos: SegmentPos): SegmentPos => ({ ...pos });

export const cloneCover = (cover: SegmentCover): SegmentCover => {
  const out: SegmentCover = { box: [...cover.box] as CoverBox };
  if (cover.fill !== undefined) out.fill = cover.fill;
  return out;
};

/** Smallest box holding both (merging two lines of the same on-screen text) */
export function unionCover(a: SegmentCover, b: SegmentCover): SegmentCover {
  const out: SegmentCover = {
    box: [
      Math.min(a.box[0], b.box[0]), Math.min(a.box[1], b.box[1]),
      Math.max(a.box[2], b.box[2]), Math.max(a.box[3], b.box[3]),
    ],
  };
  // The first segment's plate choice wins; if it had none, take the other's
  const fill = a.fill !== undefined ? a.fill : b.fill;
  if (fill !== undefined) out.fill = fill;
  return out;
}

/** The point of a box that alignment `an` anchors to, in the box's own units */
export function anchorOnBox(box: readonly [number, number, number, number], an: number) {
  const h = ((an - 1) % 3) + 1, v = Math.ceil(an / 3);
  const [x0, y0, x1, y1] = box;
  return {
    x: h === 1 ? x0 : h === 2 ? (x0 + x1) / 2 : x1,
    y: v === 3 ? y0 : v === 2 ? (y0 + y1) / 2 : y1,
  };
}

/**
 * "Back to the original spot": anchor on the cover box with the current alignment and
 * wrap to the box width. Font size is kept as the person set it.
 */
export function posFromCover(cover: SegmentCover, current?: SegmentPos): SegmentPos {
  const an = current?.an ?? 5;
  const point = anchorOnBox(cover.box, an);
  const out: SegmentPos = { x: point.x, y: point.y, an, w: cover.box[2] - cover.box[0] };
  if (current?.fs) out.fs = current.fs;
  if (current?.lh) out.lh = current.lh;
  return out;
}

/** Default plate colour when the person turns plates on */
export const DEFAULT_PLATE_FILL = "#000000";

/**
 * Clamp each cue's end to the next cue's start (ASR data carries millisecond overlaps that
 * libass would treat as collisions). Positioned cues are skipped entirely: several pieces of
 * on-screen text legitimately share the same time, and `\pos` events never collide.
 * `arr` must already be sorted by t0; it is changed in place.
 */
export function trimLaneOverlaps<T extends { t0: number; t1: number; pos?: SegmentPos }>(arr: T[]): T[] {
  let previous: T | null = null;
  for (const item of arr) {
    if (item.pos) continue;
    if (previous && previous.t1 > item.t0) previous.t1 = item.t0;
    previous = item;
  }
  return arr;
}
