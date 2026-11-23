import { DEFAULT_EFFECT_TRACK_ID } from '../../subtitles/effects';
import { bumpDoc, docStore } from '../store/docStore';
import { markDirty } from '../store/saveStore';
import { pushHistory } from './history';
import { stylesInUse } from './styleEdit';
import { syncSubs } from './subtitles';
import { DEFAULT_PLATE_FILL, posFromCover } from '../../subtitles/position';
import type { Seg, SegmentPos } from '../types';

/**
 * Writes to a segment's own placement (`pos`) and plate (`cover.fill`). These fields live on
 * the seg, not in the style sheet, so dragging one piece of on-screen text never moves another.
 *
 * Panel edits that arrive in a burst (scrubbing a number, dragging a colour) count as one undo
 * step, the same way patchStyleFromPanel coalesces style edits.
 */

const COALESCE_MS = 800;
let lastEdit: { seg: Seg | null; at: number } = { seg: null, at: 0 };

function remember(seg: Seg) {
  const now = Date.now();
  if (lastEdit.seg !== seg || now - lastEdit.at > COALESCE_MS) pushHistory();
  lastEdit = { seg, at: now };
}

/** Repaint after an in-place seg change (no history, no coalescing) */
export function refreshSegPos() {
  bumpDoc();
  syncSubs();
  markDirty();
}

const clamp01 = (value: number) => Math.min(1, Math.max(0, value));

/** Patch `pos` in place; the caller has already pushed history (drag, nudge) */
export function writeSegPos(seg: Seg, patch: Partial<SegmentPos>) {
  if (!seg.pos) return;
  const next = { ...seg.pos, ...patch };
  next.x = clamp01(next.x);
  next.y = clamp01(next.y);
  Object.assign(seg.pos, next);
  if (patch.fs === undefined && "fs" in patch) delete seg.pos.fs;
  refreshSegPos();
}

/** Inspector entry: patch `pos` with a coalesced undo step */
export function setSegPos(seg: Seg, patch: Partial<SegmentPos>) {
  if (!seg.pos) return;
  remember(seg);
  writeSegPos(seg, patch);
}

/** Plate colour `#rrggbb`, or null for "no plate" (the box is still erased at burn time) */
export function setSegFill(seg: Seg, fill: string | null) {
  if (!seg.cover) return;
  remember(seg);
  writeFill(seg, fill);
  refreshSegPos();
}

/** Set the plate colour; switching one off keeps its colour as `bg` so switching it back on restores it */
function writeFill(seg: Seg, fill: string | null) {
  const cover = seg.cover!;
  if (fill === null && typeof cover.fill === "string" && !cover.bg) cover.bg = cover.fill;
  cover.fill = fill;
}

export const plateOn = (seg: Seg) => typeof seg.cover?.fill === "string";

/** The colour a plate gets when switched on: the sampled background, else whatever it had, else black */
const plateColourOf = (seg: Seg) =>
  seg.cover?.bg ?? (typeof seg.cover?.fill === "string" ? seg.cover.fill : DEFAULT_PLATE_FILL);

export function togglePlate(seg: Seg, on: boolean) {
  setSegFill(seg, on ? plateColourOf(seg) : null);
}

/**
 * What the style panel's 底板 switch acts on: the segments with a cover box drawn in style `name`,
 * i.e. on the lanes bound to it, minus those carrying an own style other than `name`. With
 * `only`, just that segment (the panel is editing its own style).
 */
export function plateSegsOfStyle(name: string, only?: Seg | null): Seg[] {
  if (only) return only.cover ? [only] : [];
  const { segs, tracks } = docStore.get();
  const out: Seg[] = [];
  for (const binding of stylesInUse(name)) {
    const arr = binding.trackId === DEFAULT_EFFECT_TRACK_ID
      ? segs
      : tracks.find((track, index) => (track.id || `track-${index + 1}`) === binding.trackId)?.segs ?? [];
    for (const seg of arr) {
      const own = seg.style?.[binding.lang];
      if (seg.cover && (!own || own === name) && !out.includes(seg)) out.push(seg);
    }
  }
  return out;
}

/** Switch the plates of these segments on or off together, one undo step */
export function setPlates(segs: readonly Seg[], on: boolean) {
  const changing = segs.filter(seg => seg.cover && plateOn(seg) !== on);
  if (!changing.length) return;
  pushHistory();
  lastEdit = { seg: null, at: 0 };
  for (const seg of changing) writeFill(seg, on ? plateColourOf(seg) : null);
  refreshSegPos();
}

/** "Back to the original spot": anchor on the cover box again, at its width */
export function resetSegPos(seg: Seg) {
  if (!seg.pos || !seg.cover) return;
  pushHistory();
  lastEdit = { seg: null, at: 0 };
  Object.assign(seg.pos, posFromCover(seg.cover, seg.pos));
  refreshSegPos();
}
