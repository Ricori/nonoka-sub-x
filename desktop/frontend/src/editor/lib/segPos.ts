import { bumpDoc } from '../store/docStore';
import { markDirty } from '../store/saveStore';
import { pushHistory } from './history';
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
  seg.cover.fill = fill;
  refreshSegPos();
}

export const plateOn = (seg: Seg) => typeof seg.cover?.fill === "string";

export function togglePlate(seg: Seg, on: boolean) {
  setSegFill(seg, on ? DEFAULT_PLATE_FILL : null);
}

/** "Back to the original spot": anchor on the cover box again, at its width */
export function resetSegPos(seg: Seg) {
  if (!seg.pos || !seg.cover) return;
  pushHistory();
  lastEdit = { seg: null, at: 0 };
  Object.assign(seg.pos, posFromCover(seg.cover, seg.pos));
  refreshSegPos();
}
