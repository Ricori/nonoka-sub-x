import { createStore } from '../../home/lib/createStore';
import { ROW_H0 } from '../constants';
import { getLoadedDocument, getVid, notifySaved, setLoadedDocument } from '../session';
import { documents } from '../../bridge/documents.ts';
import { mediaLibrary } from '../../bridge/library.ts';
import type { EditDocument } from '../../documents/types.ts';
import { setStyleSheet } from '../ass';
import { mapDocumentTracks, mergeOcrTracks, nonOcrHash } from '../lib/screenTextDoc';
import { syncSubs } from '../lib/subtitles';
import { errText, p2, round3 } from '../utils';
import { bumpDoc, docStore } from './docStore';
import { restoreSelection, selStore } from './selectionStore';
import { toast } from './uiStore';
import { viewStore } from './viewStore';
import type { Seg } from '../types';
import { CN_STYLE, ORIGIN_STYLE, ensureScreenTextStyles } from '../../subtitles/styles';
import { cloneCover, clonePos } from '../../subtitles/position';

// 保存：手动（Ctrl+S / 保存按钮）+ 每 5 分钟自动一次（rev 乐观锁）。
// 视频轨剪辑是另一条线：存本地 library.json，不吃 rev 乐观锁，也不会和别人的编辑撞车。

interface SaveState {
  dirty: boolean;
  saving: boolean;
  /** 409 后本地只读（继续编辑但不再保存） */
  conflicted: boolean;
  stateText: string;
  stateCls: "" | "dirty" | "bad";
}

export const saveStore = createStore<SaveState>({
  dirty: false, saving: false, conflicted: false, stateText: "已加载", stateCls: "",
});

const setSaveState = (stateText: string, stateCls: SaveState["stateCls"] = "") =>
  saveStore.set({ stateText, stateCls });

export const setLoadedState = () => setSaveState("已加载");

/** 只标记「有未保存更改」，不再实时落盘——靠手动保存或 5 分钟定时保存 */
export function markDirty() {
  if (saveStore.get().conflicted) return;
  saveStore.set({ dirty: true });
  setSaveState("有未保存更改 · Ctrl+S 保存", "dirty");
}

export const packSeg = (s: Seg, _i: number): Seg => {
  const o: Seg = { t0: round3(s.t0), t1: round3(s.t1), ja: s.ja, zh: s.zh };
  if (s.k?.length) o.k = s.k.map(u => ({ t0: round3(u.t0), t1: round3(u.t1), text: u.text }));
  if (s.words) o.words = s.words;
  if (s.low_conf) o.low_conf = true;
  if (s.style?.ja || s.style?.zh) o.style = { ...s.style };
  if (s.pos) o.pos = clonePos(s.pos);
  if (s.cover) o.cover = cloneCover(s.cover);
  return o;
};

function savePayload() {
  const d = docStore.get();
  const base = getLoadedDocument();
  if (!base) throw new Error("编辑文档尚未加载");
  const packLane = (lane: { hidden: boolean; style: string | null }) => ({
    hidden: !!lane.hidden,
    style: lane.style || null,
  });
  return {
    ...base,
    rev: d.rev,
    title: d.title,
    subtitles: d.segs.map(packSeg),
    tracks: d.tracks.map(tr => ({
      id: tr.id, name: tr.name,
      ...(tr.source ? { source: { ...tr.source } } : {}),
      ja: packLane(tr.ja),
      zh: packLane(tr.zh),
      hja: tr.hja || ROW_H0, hzh: tr.hzh || ROW_H0,
      segs: tr.segs.map(packSeg),
    })),
    track_meta: d.trackMeta ? {
      name: d.trackMeta.name,
      ja: packLane(d.trackMeta.ja),
      zh: packLane(d.trackMeta.zh),
    } : {
      name: "默认轨", ja: { hidden: false, style: ORIGIN_STYLE }, zh: { hidden: false, style: CN_STYLE },
    },
    effects: d.effects,
    // sidecar 的 save() 只认显式列出的字段，靠 ...base 展开是带不过去的
    styles: d.styles,
  };
}

export interface SaveBaseline {
  rev: number;
  nonOcrHash: string;
}

export interface SaveDocumentIO {
  read(videoId: string): Promise<EditDocument>;
  save(videoId: string, doc: EditDocument): Promise<EditDocument | null>;
}

const defaultSaveIO: SaveDocumentIO = {
  read: (videoId) => documents.read(videoId),
  save: (videoId, doc) => documents.save(videoId, doc),
};

let saveBaseline: SaveBaseline | null = null;
let afterSaveCallbacks: Array<() => void> = [];

/** 记下最近一次与服务端对齐时的基线，供 409 冲突时判断非 ocr 部分是否被他人改过 */
export function setSaveBaseline(doc: EditDocument | null) {
  saveBaseline = doc ? { rev: Number(doc.rev) || 0, nonOcrHash: nonOcrHash(doc) } : null;
}

export function getSaveBaseline(): SaveBaseline | null {
  return saveBaseline;
}

/** 正在保存时收到后台事件（如 document:changed）先排队，等本次保存收尾后再执行 */
export function runAfterSave(fn: () => void) {
  if (!saveStore.get().saving) {
    fn();
    return;
  }
  afterSaveCallbacks.push(fn);
}

let queued = false;
let saveTimer: ReturnType<typeof setInterval> | undefined;

function applyRebaseFromServer(latest: EditDocument) {
  const d = docStore.get();
  const incomingTracks = mapDocumentTracks(latest.tracks);
  const { tracks, ocrCount } = mergeOcrTracks(d.tracks, incomingTracks);
  const styles = ocrCount > 0 ? ensureScreenTextStyles(d.styles, latest.styles) : d.styles;
  setLoadedDocument(latest);
  setSaveBaseline(latest);
  docStore.set({ rev: Number(latest.rev) || 0, tracks, styles });
  if (styles !== d.styles) setStyleSheet(styles);
  const curTrack = selStore.get().curTrack;
  if (curTrack >= tracks.length) restoreSelection(tracks.length - 1, -1);
  bumpDoc();
  syncSubs();
}

async function tryRebaseAndRetry(videoId: string, io: SaveDocumentIO): Promise<EditDocument | null> {
  if (!saveBaseline) return null;
  let latest: EditDocument;
  try {
    latest = await io.read(videoId);
  } catch {
    return null;
  }
  if (nonOcrHash(latest) !== saveBaseline.nonOcrHash) return null;
  applyRebaseFromServer(latest);
  try {
    return await io.save(videoId, savePayload());
  } catch {
    return null;
  }
}

export async function doSave(io: SaveDocumentIO = defaultSaveIO) {
  const st = saveStore.get();
  if (st.saving) { queued = true; return; }
  if (!st.dirty || st.conflicted) return;
  saveStore.set({ saving: true, dirty: false });
  setSaveState("保存中…", "dirty");
  const videoId = getVid();
  const markConflicted = (msg: string) => {
    saveStore.set({ conflicted: true });
    setSaveState("版本冲突，已停止保存", "bad");
    toast(msg, true);
  };
  try {
    let saved: EditDocument | null = null;
    let rebased = false;
    try {
      saved = await io.save(videoId, savePayload());
    } catch (error) {
      if (errText(error).includes("revision conflict")) {
        saved = await tryRebaseAndRetry(videoId, io);
        if (saved) {
          rebased = true;
        } else {
          saveStore.set({ dirty: true });
          markConflicted("其他窗口已保存过这个视频，请关闭编辑器后重新打开");
          return;
        }
      } else {
        throw error;
      }
    }
    if (!saved) {
      saved = await tryRebaseAndRetry(videoId, io);
      if (saved) {
        rebased = true;
      } else {
        markConflicted("文档版本冲突，请关闭编辑器后重新打开");
        return;
      }
    }
    setLoadedDocument(saved);
    setSaveBaseline(saved);
    docStore.set({ rev: saved.rev });
    notifySaved();
    const dd = new Date();
    setSaveState("已保存 " + p2(dd.getHours()) + ":" + p2(dd.getMinutes()));
    if (rebased) toast("已自动合并后台新增的文字识别并保存");
  } catch {
    saveStore.set({ dirty: true });
    setSaveState("保存失败 · Ctrl+S 重试", "bad");
  } finally {
    saveStore.set({ saving: false });
    const pending = afterSaveCallbacks;
    afterSaveCallbacks = [];
    for (const cb of pending) cb();
    if (queued) { queued = false; void doSave(io); }
  }
}

// ── 视频轨保存 ────────────────────────────────────────────────────
// 每次剪辑都立刻落盘（本地 library.json，很小），排队串行免得旧的后到把新的盖掉
let editSaveVersion = 0, editSavedVersion = 0;
let editSaveTail: Promise<boolean> = Promise.resolve(true);

export const editDirty = () => editSavedVersion < editSaveVersion;

export function saveEdit() {
  const version = ++editSaveVersion;
  const pieces = (viewStore.get().pieces ?? []).map(p => ({ t0: p.t0, t1: p.t1 }));
  editSaveTail = editSaveTail.then(async () => {
    const ok = await mediaLibrary.setVideoEdit(getVid(), { pieces });
    if (!ok) throw new Error("本地媒体库中没有这个视频");
    editSavedVersion = version;
    return true;
  }).catch(e => {
    toast("视频轨保存失败：" + errText(e));
    return false;
  });
  return editSaveTail;
}

export async function flushEdit() {
  await editSaveTail;
  return editSavedVersion >= editSaveVersion;
}

/** 关闭编辑器前把本地改动（字幕 + 视频轨）全部落盘。
 *  导出不再走这里：SRT/ASS/MP4 都在本地按内存里这份文档拼，落不落盘与产物无关。 */
export async function flushSave() {
  let guard = 0;
  while ((saveStore.get().dirty || saveStore.get().saving) && !saveStore.get().conflicted && guard++ < 40) {
    if (!saveStore.get().saving) await doSave();
    else await new Promise(r => setTimeout(r, 200));
  }
  await flushEdit();
}

/** 手动保存（按钮 / Ctrl+S） */
export async function manualSave() {
  const st = saveStore.get();
  if (st.conflicted) { toast("版本冲突，无法保存，请刷新页面"); return; }
  if (!st.dirty && !st.saving && !editDirty()) { toast("没有需要保存的更改"); return; }
  await Promise.all([doSave(), flushEdit()]);
}

/** 每 5 分钟自动保存一次（仅在有未保存改动且未冲突时） */
export function startAutosave(ms: number) {
  clearInterval(saveTimer);
  saveTimer = setInterval(() => {
    const st = saveStore.get();
    if (st.dirty && !st.saving && !st.conflicted) void doSave();
  }, ms);
  return () => clearInterval(saveTimer);
}
