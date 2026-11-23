import { documents } from "../../bridge/documents.ts";
import { mediaLibrary } from "../../bridge/library.ts";
import { backHome, getVid, setLoadedDocument } from "../session";
import { bumpDoc, docStore } from "../store/docStore";
import { setLoadedState, saveStore, setSaveBaseline } from "../store/saveStore";
import { resetScreenTextStore } from "../store/screenTextStore";
import { select, selStore } from "../store/selectionStore";
import { askStore, ctxStore, modalStore, toast, toastStore } from "../store/uiStore";
import { selectLane } from "../store/stageStore";
import { videoStore } from "../store/videoStore";
import { ensureBlkWin, relayout, setDuration, syncZoomRange, viewStore } from "../store/viewStore";
import { playStore } from "../store/playStore";
import { errText } from "../utils";
import { unknownStyles } from "./assBuild";
import { machineDefaultStyles, setDocStyles } from "./styleEdit";
import { DEFAULT_EFFECT_TRACK_ID, normalizeEffectBindings } from "../../subtitles/effects";
import { CN_STYLE, ORIGIN_STYLE } from "../../subtitles/styles";
import { initSubtitles, preloadSubtitles, refreshFontMetrics } from "./subtitles";
import { setupVideo, showVideoFallback } from "./videoSource";
import { resetAutoGain } from "./wave";
import { resetHistory } from "./history";
import { dragStore } from "../store/dragStore";
import type { Lang } from "../types";
import { normalizePieces } from "../../subtitles/pieces.ts";
import { mapDocumentTracks, mapSegs } from "./screenTextDoc";
import { vselStore } from "../store/vselStore";

export { mapSegs };

function resetTransientState() {
  resetHistory();
  resetScreenTextStore();
  setSaveBaseline(null);
  modalStore.set({
    bootDone: false, closeOpen: false, tplOpen: false, effectsOpen: false, karaokeOpen: false,
    sideTab: "subtitle",
    trkPop: null,
  });
  ctxStore.set({ menu: null });
  askStore.set({ dialog: null });
  toastStore.set({ msg: "", show: false, sticky: false, ok: false, onClick: null });
  dragStore.set({ marquee: null, dropTi: null });
  saveStore.set({ dirty: false, saving: false, conflicted: false, stateText: "正在加载", stateCls: "" });
  selStore.set({ curTrack: -1, sel: -1, selSet: new Set(), preview: null });
  playStore.set({ t: 0, playing: false, rate: 1 });
  videoStore.set({
    src: "", fallbackOpen: false, collapsed: false, retrieving: false, retrievePct: "",
    transcoding: false, transcodePct: "", canTranscode: false, playError: false, fbMsg: "", warn: "",
    usePath: null, badge: null, subBusy: null, audioOnly: false,
  });
  viewStore.set({ duration: 60, t0: 0, t1: 60, pieces: null, focus: false, blkWin: null });
  vselStore.set({ vsel: null, videoActive: false });
}

/** 切到侧栏「样式」页，并选中第一条绑着缺失样式的 lane，StyleBar 里就能直接看到回退和补建入口 */
function openStyleTabAt(missing: string[]) {
  const { tracks, trackMeta } = docStore.get();
  const lanes: { trackId: string; lang: Lang; style: string | null | undefined }[] = [
    { trackId: DEFAULT_EFFECT_TRACK_ID, lang: "zh", style: trackMeta?.zh.style },
    { trackId: DEFAULT_EFFECT_TRACK_ID, lang: "ja", style: trackMeta?.ja.style },
    ...tracks.flatMap((track, index) => (["zh", "ja"] as Lang[]).map(lang => ({
      trackId: track.id || `track-${index + 1}`, lang, style: track[lang].style,
    }))),
  ];
  const hit = lanes.find(lane => lane.style && missing.includes(lane.style));
  modalStore.set({ sideTab: "style" });
  if (hit) selectLane({ trackId: hit.trackId, lang: hit.lang });
}

export async function runBootSequence() {
  resetTransientState();
  const videoID = getVid();
  try {
    setupVideo().catch((error) => showVideoFallback(false, "视频加载失败：" + errText(error)));
    preloadSubtitles().catch(() => undefined);
    const [data, peaks] = await Promise.all([
      documents.read(videoID),
      documents.peaks(videoID),
    ]);
    setLoadedDocument(data);
    setSaveBaseline(data);
    // 样式表跟着视频走。老文档和云端投影下来的那些没有这个字段，拿本机默认模板当种子——
    // 只装进内存不标脏：只看不改就关掉的文档不该平白 bump 一次 rev、多出一份历史快照。
    const styles = data.styles?.trim() ? data.styles : await machineDefaultStyles();

    const segs = mapSegs(data.subtitles);
    const tracks = mapDocumentTracks(data.tracks);
    const sourceMeta = data.track_meta ?? {
      name: "默认轨",
      ja: { hidden: false, style: ORIGIN_STYLE },
      zh: { hidden: false, style: CN_STYLE },
    };
    const trackMeta = {
      name: sourceMeta.name || "默认轨",
      ja: { hidden: !!sourceMeta.ja?.hidden, style: sourceMeta.ja?.style || ORIGIN_STYLE },
      zh: { hidden: !!sourceMeta.zh?.hidden, style: sourceMeta.zh?.style || CN_STYLE },
    };
    const effects = normalizeEffectBindings(data.effects);

    docStore.set({
      rev: data.rev || 0,
      title: data.title || videoID,
      videoFp: data.fp || null,
      segs,
      tracks,
      trackMeta,
      effects,
      knowledgeBase: "",
      canLearnKnowledge: false,
      knowledgeLearning: { status: "idle" },
      correctOnly: data.projection?.correct_only === true,
      peaks,
    });
    setDocStyles(styles, { dirty: false });
    resetAutoGain();
    const entry = await mediaLibrary.get(videoID).catch(() => null);
    videoStore.set({ audioOnly: !!entry?.audioOnly });
    // 视频轨片段存在本地媒体库里。打开时总是完整片：剪辑都在完整片里做，成片只供预览，要看再切过去
    const stored = normalizePieces(await mediaLibrary.getVideoEdit(videoID).catch(() => []));
    viewStore.set({ pieces: stored.length ? stored : null, focus: false });
    setDuration(peaks?.duration || (segs.length ? segs[segs.length - 1].t1 + 2 : 60));

    modalStore.set({ bootDone: true });
    bumpDoc();
    initSubtitles((text) => videoStore.set({ subBusy: text }))
      .catch((error) => toast("字幕预览渲染器加载失败：" + errText(error)));
    void refreshFontMetrics();
    relayout();
    syncZoomRange();
    ensureBlkWin(true);
    if (segs.length) select(0);
    setLoadedState();
    // 云端投影下来的文档常绑着这份样式表里没有的样式：照常出图（回退 origin/cn），但得说一声
    const unknown = unknownStyles();
    if (unknown.length) {
      toast("这个视频的样式表里没有 " + unknown.join("、") + "，相关轨道已回退到默认 origin/cn 样式 · 点此查看样式",
        true, () => openStyleTabAt(unknown), 3000);
    }
  } catch (error) {
    toast("打开字幕失败：" + errText(error), true);
    backHome();
  }
}
