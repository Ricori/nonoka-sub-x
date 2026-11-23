import { documents } from "../../bridge/documents.ts";
import { fineSubRuntime } from "../../bridge/runtime.ts";
import { screenTextBridge } from "../../bridge/screenText.ts";
import type { DocumentChangedEvent, ScreenTextOptions, ScreenTextProgressEvent } from "../../bridge/screenText.ts";
import type { EditDocument } from "../../documents/types.ts";
import { SCREEN_TEXT_STYLE, ensureScreenTextStyles } from "../../subtitles/styles.ts";
import { setStyleSheet } from "../ass.ts";
import { getVid, setLoadedDocument } from "../session.ts";
import { bumpDoc, docStore } from "../store/docStore.ts";
import { playStore } from "../store/playStore.ts";
import { markDirty, runAfterSave, saveStore, setSaveBaseline } from "../store/saveStore.ts";
import { screenTextStore } from "../store/screenTextStore.ts";
import { restoreSelection, selStore } from "../store/selectionStore.ts";
import { toast } from "../store/uiStore.ts";
import { viewStore } from "../store/viewStore.ts";
import { errText } from "../utils.ts";
import { isOcrTrack, mapDocumentTracks, mergeOcrTracks } from "./screenTextDoc.ts";
import { cloneSegs, pushHistory } from "./history.ts";
import type { Seg, Track } from "../types.ts";
import { syncSubs } from "./subtitles.ts";

/**
 * Apply incoming `ocr*` tracks from a freshly read server document into the live editor.
 *
 * - Clean document (`!dirty`): pushHistory (undoable), swap `ocr*` tracks, ensure「文字识别」style,
 *   advance rev & baseline, keep `dirty: false`.
 * - Dirty document (`dirty`): rebase — pushHistory (undoable), keep all local non-OCR edits,
 *   splice in server `ocr*` tracks, ensure「文字识别」style, advance rev & baseline, stay dirty
 *   so autosave / Ctrl+S writes the combined result.
 */
export function mergeServerScreenText(serverDoc: EditDocument): { ocrCount: number; rebased: boolean } | null {
  const saveState = saveStore.get();
  if (saveState.conflicted) return null;

  const d = docStore.get();
  const incomingTracks = mapDocumentTracks(serverDoc.tracks);
  const { tracks, ocrCount } = mergeOcrTracks(d.tracks, incomingTracks);
  const styles = ocrCount > 0 ? ensureScreenTextStyles(d.styles, serverDoc.styles, ocrStyleNames(tracks)) : d.styles;
  const rebased = saveState.dirty;

  pushHistory();
  setLoadedDocument(serverDoc);
  setSaveBaseline(serverDoc);
  docStore.set({ rev: Number(serverDoc.rev) || d.rev, tracks, styles });
  if (styles !== d.styles) setStyleSheet(styles);

  const curTrack = selStore.get().curTrack;
  if (curTrack >= tracks.length) restoreSelection(tracks.length - 1, -1);

  bumpDoc();
  syncSubs();
  if (rebased) markDirty();

  return { ocrCount, rebased };
}

/**
 * Handle a `document:changed` event from Go `screentext.Service`.
 * If a save is in flight, defers until the save finishes before pulling and merging.
 */
export function handleDocumentChanged(
  event: DocumentChangedEvent,
  readDoc: (videoId: string) => Promise<EditDocument> = (id) => documents.read(id),
): Promise<boolean> {
  const videoId = getVid();
  if (!videoId || event.videoId !== videoId) return Promise.resolve(false);
  if (saveStore.get().conflicted) return Promise.resolve(false);

  return new Promise((resolve) => {
    runAfterSave(() => {
      void (async () => {
        try {
          const latest = await readDoc(videoId);
          const merged = mergeServerScreenText(latest);
          if (!merged) {
            resolve(false);
            return;
          }
          screenTextStore.set({
            trialUndo: null,
            lastCompletedTracks: merged.ocrCount,
            error: "",
          });
          syncSubs();
          toast(
            merged.ocrCount > 0
              ? (merged.rebased
                ? `文字识别已加入（${merged.ocrCount} 条轨，已保留本地改动）`
                : `文字识别已加入（${merged.ocrCount} 条轨）`)
              : "文字识别完成，没有找到新的文字",
            false,
            null,
            3200,
            merged.ocrCount > 0,
          );
          resolve(true);
        } catch (error) {
          toast("合并文字识别失败：" + errText(error), true);
          resolve(false);
        }
      })();
    });
  });
}

// ── Runtime check & task orchestration ────────────────────────────

const ACTIVE_STATES = new Set(["queued", "running"]);

export async function refreshScreenTextEnvironment() {
  screenTextStore.set({ checking: true });
  try {
    const [provision, check, gameList] = await Promise.all([
      fineSubRuntime.status().catch(() => null),
      screenTextBridge.check("auto").catch(() => null),
      screenTextBridge.listGameTexts().catch(() => null),
    ]);
    const platform = provision?.platform ?? "";
    const layerState = provision?.screen_text?.state ?? "";
    const layerAvailable = provision?.screen_text?.available ?? true;
    screenTextStore.set({
      platform,
      layerState,
      layerAvailable,
      check,
      games: gameList?.games ?? screenTextStore.get().games,
      checking: false,
    });
  } catch {
    screenTextStore.set({ checking: false });
  }
}

export async function importGameTextBundle() {
  screenTextStore.set({ busy: true, error: "" });
  try {
    const res = await screenTextBridge.importGameText();
    if (res?.cancelled) {
      screenTextStore.set({ busy: false });
      return;
    }
    const games = res?.games ?? screenTextStore.get().games;
    const imported = res?.imported;
    screenTextStore.set((s) => ({
      busy: false,
      games,
      options: imported ? { ...s.options, game: imported.game } : s.options,
    }));
    if (imported) {
      toast(`已导入游戏剧本包：${imported.game}${imported.version ? ` (${imported.version})` : ""}`, false, null, 3000, true);
    }
  } catch (error) {
    screenTextStore.set({ busy: false, error: "导入剧本包失败：" + errText(error) });
  }
}

export async function restoreScreenTextTask() {
  const videoId = getVid();
  if (!videoId) return;
  try {
    const snapshot = await screenTextBridge.status(videoId);
    if (!snapshot) return;
    if (ACTIVE_STATES.has(snapshot.state)) {
      screenTextStore.set({ task: snapshot, error: "" });
    } else if (!screenTextStore.get().task) {
      screenTextStore.set({ task: snapshot });
    }
  } catch {
    // Sidecar may be down; tab check will show the exact issue.
  }
}

export async function handleScreenTextProgress(event: ScreenTextProgressEvent) {
  const videoId = getVid();
  if (!videoId || event.mediaId !== videoId) return;
  const snapshot = event.snapshot;
  const prev = screenTextStore.get();
  screenTextStore.set({ task: snapshot, error: snapshot.state === "failed" ? (snapshot.error?.message || "识别失败") : "" });

  if (snapshot.state === "completed" && prev.taskIsTrial) {
    try {
      const result = await screenTextBridge.result(event.taskId);
      const tracks = mapDocumentTracks(result.tracks);
      const start = Number(result.range?.start) || 0;
      const end = Number(result.range?.end) || start + 60;
      screenTextStore.set({ taskIsTrial: false, error: "" });
      const cueCount = tracks.some(track => track.segs.length)
        ? applyTrialResult(tracks, result.styles || "", start, end) : 0;
      toast(cueCount > 0 ? `试跑完成：已写入 ${cueCount} 处文字` : "试跑这段没有识别到文字",
        false, null, 3200, cueCount > 0);
    } catch (error) {
      screenTextStore.set({ taskIsTrial: false, error: "读取试跑结果失败：" + errText(error) });
    }
  } else if (snapshot.state === "failed" || snapshot.state === "cancelled" || snapshot.state === "interrupted") {
    screenTextStore.set({ taskIsTrial: false });
  }
}

export function buildScreenTextOptions(trial: boolean, onEdited: "replace" | "keep"): ScreenTextOptions {
  const { options } = screenTextStore.get();
  const duration = viewStore.get().duration || 0;
  let start = 0;
  let end = 0;
  if (trial) {
    const now = Math.max(0, playStore.get().t || 0);
    start = Math.round(now * 100) / 100;
    end = duration > 0 ? Math.min(duration, start + 60) : start + 60;
    if (end <= start) {
      start = Math.max(0, duration - 60);
      end = duration;
    }
  } else if (options.rangeMode === "custom") {
    start = Math.max(0, Number(options.start) || 0);
    end = Math.max(0, Number(options.end) || 0);
  }
  return {
    start,
    end,
    device: options.device === "auto" ? "cuda" : options.device,
    skipMain: options.skipMain,
    translate: options.translate,
    cover: options.cover,
    game: options.game,
    trial,
    onEdited,
    ...(options.roi ? { roi: options.roi } : {}),
  };
}

/** A full run always replaces the document's screen-text tracks; a trial writes into them. */
export async function requestStartScreenText(trial: boolean) {
  await startScreenText(trial, "replace");
}

export async function startScreenText(trial: boolean, onEdited: "replace" | "keep") {
  const videoId = getVid();
  if (!videoId) return;
  // A new run makes the last trial's undo meaningless: it would put back tracks the run replaces
  screenTextStore.set({ busy: true, error: "", trialUndo: null });
  try {
    const opts = buildScreenTextOptions(trial, onEdited);
    const snapshot = await screenTextBridge.start(videoId, opts);
    screenTextStore.set({
      task: snapshot,
      taskIsTrial: trial,
      busy: false,
      error: "",
      lastCompletedTracks: trial ? screenTextStore.get().lastCompletedTracks : null,
    });
  } catch (error) {
    screenTextStore.set({ busy: false, error: errText(error) });
  }
}

export async function cancelScreenText() {
  const videoId = getVid();
  if (!videoId) return;
  screenTextStore.set({ busy: true });
  try {
    const snapshot = await screenTextBridge.cancel(videoId);
    screenTextStore.set({ task: snapshot, taskIsTrial: false, busy: false });
  } catch (error) {
    screenTextStore.set({ busy: false, error: errText(error) });
  }
}

/** The styles the screen-text tracks are bound to: each must exist in the sheet */
const ocrStyleNames = (tracks: readonly Track[]): string[] =>
  tracks.filter(isOcrTrack).flatMap(track => [track.zh.style, track.ja.style])
    .filter((name): name is string => !!name);

const cloneTrack = (track: Track): Track => ({
  ...track,
  ja: { ...track.ja },
  zh: { ...track.zh },
  segs: cloneSegs(track.segs),
  ...(track.source ? { source: { ...track.source } } : {}),
});

const overlaps = (segs: readonly Seg[], seg: Seg) => segs.some(other => other.t0 < seg.t1 && seg.t0 < other.t1);

/** `ocr`, then `ocr2`, `ocr3`, ...: the first id no track has yet */
function freeOcrId(tracks: readonly Track[]): { id: string; name: string } {
  const taken = new Set(tracks.map(track => track.id));
  for (let n = 1; ; n++) {
    const id = n === 1 ? "ocr" : `ocr${n}`;
    if (!taken.has(id)) return { id, name: n === 1 ? SCREEN_TEXT_STYLE : `${SCREEN_TEXT_STYLE} ${n}` };
  }
}

/**
 * The document's tracks with a trial's result laid into its screen-text tracks: whatever they held
 * inside the trial's range goes, the trial's lines take its place. A line keeps to the track of the
 * same number when that one is free at its time, else the first free one, else a new track; tracks
 * left with nothing are dropped. Without screen-text tracks yet, the trial's become them.
 */
export function mergeTrialTracks(local: readonly Track[], trial: readonly Track[], start: number, end: number): {
  tracks: Track[];
  cueCount: number;
} {
  const kept = local.filter(track => !isOcrTrack(track));
  const ocr = local.filter(isOcrTrack)
    .map(track => ({ ...track, segs: track.segs.filter(seg => !(seg.t1 > start && seg.t0 < end)) }));
  let cueCount = 0;
  trial.forEach((incoming, index) => {
    for (const seg of incoming.segs) {
      cueCount++;
      const preferred = ocr[index] && !overlaps(ocr[index].segs, seg) ? ocr[index] : null;
      const target = preferred ?? ocr.find(track => !overlaps(track.segs, seg));
      if (target) {
        target.segs = [...target.segs, seg].sort((a, b) => a.t0 - b.t0);
        continue;
      }
      // a track of its own gets a style of its own, named like it (「文字识别 3」)
      const { id, name } = freeOcrId([...kept, ...ocr]);
      ocr.push({
        ...incoming, id, name,
        ja: { ...incoming.ja, style: name }, zh: { ...incoming.zh, style: name },
        segs: [seg],
      });
    }
  });
  return { tracks: [...kept, ...ocr.filter(track => track.segs.length)], cueCount };
}

/**
 * A finished 1-minute trial goes straight into the document (undoable via Ctrl+Z, marked dirty for
 * autosave / Ctrl+S); the panel keeps what was there before so「撤销」can put it back.
 */
export function applyTrialResult(trial: readonly Track[], trialStyles: string, start: number, end: number): number {
  const d = docStore.get();
  const before = d.tracks.filter(isOcrTrack).map(cloneTrack);
  const { tracks, cueCount } = mergeTrialTracks(d.tracks, trial, start, end);
  const styles = cueCount > 0 ? ensureScreenTextStyles(d.styles, trialStyles, ocrStyleNames(tracks)) : d.styles;

  pushHistory();
  docStore.set({ tracks, styles });
  if (styles !== d.styles) setStyleSheet(styles);
  screenTextStore.set({ trialUndo: { before, cueCount, start, end } });

  bumpDoc();
  syncSubs();
  markDirty();
  return cueCount;
}

/** Put back the screen-text tracks as they were before the last trial; other edits stay */
export function undoTrial() {
  const undo = screenTextStore.get().trialUndo;
  if (!undo) return;
  const d = docStore.get();
  pushHistory();
  docStore.set({ tracks: [...d.tracks.filter(track => !isOcrTrack(track)), ...undo.before.map(cloneTrack)] });
  const curTrack = selStore.get().curTrack;
  if (curTrack >= docStore.get().tracks.length) restoreSelection(docStore.get().tracks.length - 1, -1);
  screenTextStore.set({ trialUndo: null });
  bumpDoc();
  syncSubs();
  markDirty();
  toast("已撤销试跑结果", false, null, 2400, true);
}
