import { ROW_H0, ROW_MAX, ROW_MIN } from "../constants";
import { documents } from "../../bridge/documents.ts";
import { mediaLibrary } from "../../bridge/library.ts";
import { backHome, getVid, setLoadedDocument } from "../session";
import { bumpDoc, docStore } from "../store/docStore";
import { setLoadedState, saveStore } from "../store/saveStore";
import { loadStyleSheet } from "../store/styleStore";
import { select, selStore } from "../store/selectionStore";
import { askStore, ctxStore, modalStore, toast, toastStore } from "../store/uiStore";
import { videoStore } from "../store/videoStore";
import { ensureBlkWin, relayout, setDuration, syncZoomRange, viewStore } from "../store/viewStore";
import { playStore } from "../store/playStore";
import { clampN, errText } from "../utils";
import { unknownStyles } from "./assBuild";
import { migrateLegacyFadeBindings, normalizeEffectBindings } from "../../subtitles/effects";
import { normalizeKaraoke } from "../../subtitles/karaoke";
import { initSubtitles, preloadSubtitles, refreshFontMetrics } from "./subtitles";
import { setupVideo, showVideoFallback } from "./videoSource";
import { resetAutoGain } from "./wave";
import { resetHistory } from "./history";
import { dragStore } from "../store/dragStore";
import type { Clip, Seg, Track } from "../types";

function mapSegs(items: unknown[]): Seg[] {
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
    return segment;
  }).sort((left, right) => left.t0 - right.t0);
  for (let index = 0; index < out.length - 1; index++) {
    if (out[index].t1 > out[index + 1].t0) out[index].t1 = out[index + 1].t0;
  }
  return out;
}

function resetTransientState() {
  resetHistory();
  modalStore.set({
    bootDone: false, closeOpen: false, tplOpen: false, effectsOpen: false, karaokeOpen: false,
    trkPop: null, clipTip: null,
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
    transcoding: false, transcodePct: "", canTranscode: false, fbMsg: "", warn: "",
    usePath: null, badge: null, subBusy: null,
  });
  viewStore.set({ duration: 60, t0: 0, t1: 60, curClip: null, clips: [], blkWin: null });
}

export async function runBootSequence() {
  resetTransientState();
  const videoID = getVid();
  try {
    setupVideo().catch((error) => showVideoFallback(false, "视频加载失败：" + errText(error)));
    preloadSubtitles().catch(() => undefined);
    // 样式表存在本机，和文档一起装载：轨道绑定要靠它判断有没有落空
    const [data, peaks] = await Promise.all([
      documents.read(videoID),
      documents.peaks(videoID),
      loadStyleSheet(),
    ]);
    setLoadedDocument(data);

    const segs = mapSegs(data.subtitles);
    const fadeMs = (value: unknown) => Math.min(60_000, Math.max(0, Math.round(Number(value) || 0)));
    const tracks: Track[] = (data.tracks ?? []).map((track, index) => ({
      id: track.id || `t${Date.now().toString(36)}${index}`,
      name: track.name || `轨道 ${index + 1}`,
      ja: {
        hidden: !!track.ja?.hidden, style: track.ja?.style || null,
        fadeInMs: fadeMs(track.ja?.fadeInMs), fadeOutMs: fadeMs(track.ja?.fadeOutMs),
      },
      zh: {
        hidden: !!track.zh?.hidden, style: track.zh?.style || null,
        fadeInMs: fadeMs(track.zh?.fadeInMs), fadeOutMs: fadeMs(track.zh?.fadeOutMs),
      },
      hja: clampN(Number(track.hja), ROW_MIN, ROW_MAX, ROW_H0),
      hzh: clampN(Number(track.hzh), ROW_MIN, ROW_MAX, ROW_H0),
      segs: mapSegs(track.segs),
    }));
    const sourceMeta = data.track_meta ?? {
      name: "默认轨",
      ja: { hidden: false, style: "JP" },
      zh: { hidden: false, style: "CN" },
    };
    const trackMeta = {
      name: sourceMeta.name || "默认轨",
      ja: {
        hidden: !!sourceMeta.ja?.hidden, style: sourceMeta.ja?.style || "JP",
        fadeInMs: fadeMs(sourceMeta.ja?.fadeInMs), fadeOutMs: fadeMs(sourceMeta.ja?.fadeOutMs),
      },
      zh: {
        hidden: !!sourceMeta.zh?.hidden, style: sourceMeta.zh?.style || "CN",
        fadeInMs: fadeMs(sourceMeta.zh?.fadeInMs), fadeOutMs: fadeMs(sourceMeta.zh?.fadeOutMs),
      },
    };
    const effectSource = { tracks, trackMeta };
    const effects = migrateLegacyFadeBindings(normalizeEffectBindings(data.effects), effectSource);
    // 旧字段只用于一次迁移；内存与下一次保存都只认统一 effects。
    for (const lane of [trackMeta.ja, trackMeta.zh, ...tracks.flatMap(track => [track.ja, track.zh])]) {
      delete lane.fadeInMs;
      delete lane.fadeOutMs;
    }

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
      peaks,
    });
    resetAutoGain();
    setDuration(peaks?.duration || (segs.length ? segs[segs.length - 1].t1 + 2 : 60));

    const clips: Clip[] = ((await mediaLibrary.getClips(videoID).catch(() => [])) ?? [])
      .sort((left, right) => left.t0 - right.t0);
    viewStore.set({ clips });

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
    // 云端同步下来的文档常绑着本机没有的样式：照常出图（回退 JP/CN），但得说一声
    const unknown = unknownStyles();
    if (unknown.length) {
      toast("本机样式表里没有 " + unknown.join("、") + "，相关轨道已回退到默认 JP/CN 样式 · 点此编辑样式",
        true, () => modalStore.set({ tplOpen: true }));
    }
  } catch (error) {
    toast("打开字幕失败：" + errText(error), true);
    backHome();
  }
}
