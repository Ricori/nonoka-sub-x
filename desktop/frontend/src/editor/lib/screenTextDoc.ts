import type { EditDocument, EditTrack } from "../../documents/types.ts";
import { normalizeKaraoke } from "../../subtitles/karaoke.ts";
import { normalizeSegCover, normalizeSegPos, trimLaneOverlaps } from "../../subtitles/position.ts";
import { SCREEN_TEXT_STYLE, parseSheet, styleLine } from "../../subtitles/styles.ts";
import { ROW_H0, ROW_MAX, ROW_MIN } from "../constants.ts";
import type { Lang, Seg, Track } from "../types.ts";
import { clampN } from "../utils.ts";

/** Whether a track belongs to the on-screen text add-on (`ocr*` id or `source.kind === "screen-text"`). */
export function isOcrTrack(track: { id?: string; source?: { kind?: string } | null } | null | undefined): boolean {
  if (!track) return false;
  return track.source?.kind === "screen-text" || (typeof track.id === "string" && track.id.startsWith("ocr"));
}

/** 句子自己的样式覆盖：只留非空的字符串，一条都没有就整个不要 */
function normalizeSegStyle(raw: unknown): Seg["style"] | undefined {
  if (!raw || typeof raw !== "object") return undefined;
  const out: NonNullable<Seg["style"]> = {};
  for (const lang of ["ja", "zh"] as Lang[]) {
    const name = (raw as Record<string, unknown>)[lang];
    if (typeof name === "string" && name) out[lang] = name;
  }
  return out.ja || out.zh ? out : undefined;
}

export function mapSegs(items: readonly unknown[] | null | undefined): Seg[] {
  const out = (items ?? []).map((item) => {
    const value = item as Partial<Seg>;
    const segment: Seg = {
      t0: Number(value.t0) || 0,
      t1: Number(value.t1) || 0,
      ja: value.ja || "",
      zh: value.zh || "",
    };
    if (Array.isArray(value.words) && value.words.length) segment.words = value.words;
    const karaoke = normalizeKaraoke(value.k);
    if (karaoke.length) segment.k = karaoke;
    if (value.low_conf) segment.low_conf = true;
    const style = normalizeSegStyle(value.style);
    if (style) segment.style = style;
    const pos = normalizeSegPos(value.pos);
    if (pos) segment.pos = pos;
    const cover = normalizeSegCover(value.cover);
    if (cover) segment.cover = cover;
    return segment;
  }).sort((left, right) => left.t0 - right.t0);
  // Positioned segs (on-screen text) may overlap on purpose; never trim them
  return trimLaneOverlaps(out);
}

/** Only the fields of a known source kind survive; anything else is dropped */
export function normalizeTrackSource(raw: unknown): Track["source"] | undefined {
  if (!raw || typeof raw !== "object") return undefined;
  const value = raw as Record<string, unknown>;
  if (value.kind !== "screen-text") return undefined;
  const text = (key: string) => (typeof value[key] === "string" ? (value[key] as string) : "");
  return { kind: "screen-text", task: text("task"), region: text("region"), label: text("label"), hash: text("hash") };
}

export function mapDocumentTracks(rawTracks: readonly EditTrack[] | null | undefined): Track[] {
  return (rawTracks ?? []).map((track, index) => {
    const out: Track = {
      id: track.id || `t${Date.now().toString(36)}${index}`,
      name: track.name || `轨道 ${index + 1}`,
      ja: { hidden: !!track.ja?.hidden, style: track.ja?.style || null },
      zh: { hidden: !!track.zh?.hidden, style: track.zh?.style || null },
      hja: clampN(Number(track.hja), ROW_MIN, ROW_MAX, ROW_H0),
      hzh: clampN(Number(track.hzh), ROW_MIN, ROW_MAX, ROW_H0),
      segs: mapSegs(track.segs),
    };
    const source = normalizeTrackSource(track.source);
    if (source) out.source = source;
    return out;
  });
}

/**
 * Replace local `ocr*` tracks with incoming server `ocr*` tracks while preserving every non-OCR
 * track in its original order.
 */
export function mergeOcrTracks(localTracks: readonly Track[], incomingTracks: readonly Track[]): {
  tracks: Track[];
  ocrCount: number;
} {
  const kept = localTracks.filter((track) => !isOcrTrack(track));
  const incomingOcr = incomingTracks.filter((track) => isOcrTrack(track));
  return {
    tracks: [...kept, ...incomingOcr],
    ocrCount: incomingOcr.length,
  };
}

/** Deterministic JSON with sorted object keys so comparing two server documents ignores key order. */
function canonicalJson(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return "[" + value.map(canonicalJson).join(",") + "]";
  const record = value as Record<string, unknown>;
  const keys = Object.keys(record).filter((k) => record[k] !== undefined).sort();
  return "{" + keys.map((k) => `${JSON.stringify(k)}:${canonicalJson(record[k])}`).join(",") + "}";
}

function nonOcrStylesFingerprint(styles: string | undefined): string {
  const sheet = parseSheet(styles ?? "");
  const lines: string[] = [];
  for (const name of sheet.order) {
    if (name === SCREEN_TEXT_STYLE || name.startsWith(`${SCREEN_TEXT_STYLE}-`)) continue;
    lines.push(styleLine(name, sheet.fields[name]));
  }
  return canonicalJson({ playRes: sheet.playRes, lines, ext: sheet.ext });
}

/**
 * Fingerprint of everything in a server document EXCEPT `ocr*` tracks, auto-generated `文字识别*`
 * styles (and `rev`). Used on 409 save conflict: if the server's non-OCR content matches our
 * baseline, only a background screen-text run moved the document and we can safely rebase and
 * retry once.
 */
export function nonOcrHash(doc: Partial<EditDocument> | null | undefined): string {
  if (!doc) return "";
  const nonOcrTracks = (doc.tracks ?? []).filter((track) => !isOcrTrack(track));
  return canonicalJson({
    title: doc.title ?? "",
    subtitles: doc.subtitles ?? [],
    tracks: nonOcrTracks,
    track_meta: doc.track_meta ?? null,
    effects: doc.effects ?? [],
    styles: nonOcrStylesFingerprint(doc.styles),
  });
}

// ── Python-compatible segments_hash (matches `nonoka_x.screen_text.segments_hash`) ──

function pyRound(raw: unknown, places: number): number {
  const num = Number(raw);
  if (!Number.isFinite(num)) return 0;
  const factor = 10 ** places;
  const scaled = num * factor;
  const floor = Math.floor(scaled);
  const diff = scaled - floor;
  let rounded: number;
  if (Math.abs(diff - 0.5) < 1e-12) {
    rounded = floor % 2 === 0 ? floor : floor + 1;
  } else {
    rounded = Math.round(scaled);
  }
  const out = rounded / factor;
  return Object.is(out, -0) ? 0 : out;
}

function pyFloatJson(value: number): string {
  const normalized = Object.is(value, -0) ? 0 : value;
  return Number.isInteger(normalized) ? `${normalized}.0` : String(normalized);
}

/** Canonical JSON matching `nonoka_x.screen_text.segments_hash` before SHA-256. */
export function segmentsHashCanonical(segs: readonly Seg[]): string {
  const rows = segs.map((seg) => {
    const pos = seg.pos;
    const box = seg.cover?.box ?? [];
    const boxStr = box.map((v) => pyFloatJson(pyRound(v, 3))).join(",");
    return `[${pyFloatJson(pyRound(seg.t0, 2))},${pyFloatJson(pyRound(seg.t1, 2))},`
      + `${JSON.stringify(String(seg.ja ?? ""))},${JSON.stringify(String(seg.zh ?? ""))},`
      + `${pyFloatJson(pyRound(pos?.x ?? 0, 3))},${pyFloatJson(pyRound(pos?.y ?? 0, 3))},[${boxStr}]]`;
  });
  return `[${rows.join(",")}]`;
}

/** SHA-256 hex digest matching `nonoka_x.screen_text.segments_hash`. */
export async function segmentsHash(segs: readonly Seg[]): Promise<string> {
  const bytes = new TextEncoder().encode(segmentsHashCanonical(segs));
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

/** Whether an `ocr*` track has been manually modified since it was generated. */
export async function isOcrTrackEdited(track: Track): Promise<boolean> {
  const originalHash = track.source?.hash;
  if (!originalHash) return false;
  return (await segmentsHash(track.segs)) !== originalHash;
}

/** Whether any `ocr*` track in the document has been manually modified since generation. */
export async function hasEditedOcrTracks(tracks: readonly Track[]): Promise<boolean> {
  for (const track of tracks) {
    if (isOcrTrack(track) && await isOcrTrackEdited(track)) return true;
  }
  return false;
}
