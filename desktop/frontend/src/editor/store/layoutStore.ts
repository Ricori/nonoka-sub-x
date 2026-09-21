import { createStore } from '../../home/lib/createStore';
import {
  LAYOUT_KEY, ROW_H0, ROW_MAX, ROW_MIN, SPECTRUM_ROW_H0, VIDEO_ROW_H0, VIDEO_ROW_MAX, VIDEO_ROW_MIN,
  WAVE_GAIN_MAX, WAVE_ROW_MAX, ZOOM_MAX,
} from '../constants';
import { clampN } from '../utils';
import { viewStore } from './viewStore';

// 与视频无关的本机偏好，存 localStorage；轨道隐藏/自定义轨行高随字幕数据存服务端。

interface RawLayout {
  sideW?: number; pps?: number; lblW?: number; tlH?: number;
  rowH?: { video?: number; wave?: number; ja?: number; zh?: number };
  waveGain?: number; audioView?: "wave" | "spectrum"; scrubAudio?: boolean; rowV?: number;
  /** 旧版把「隐藏原文轨」存在本机，现在迁到服务端的 track_meta 里 */
  hideJa?: boolean;
}

export const LAYOUT: RawLayout = (() => {
  try { return JSON.parse(localStorage.getItem(LAYOUT_KEY) || "null") || {}; } catch { return {}; }
})();

// 行高上下限与默认值调整过（现为 36–60、默认 48），旧版按老规则存的行高一律作废重来，
// 否则老值被钳在新下限上，看起来像"默认高度没生效"
if (LAYOUT.rowV !== 2) { delete LAYOUT.rowH; LAYOUT.rowV = 2; }

interface LayoutState {
  sideW: number;
  lblW: number;
  /** 轨道区可视高度（拖 hsplit 调）：轨道总高超过它就纵向滚动 */
  tlViewH: number;
  rowH: { video: number; wave: number; ja: number; zh: number };
  /** 波形显示增益：只放大画出来的高度，不动音频本身。0 = 自动 */
  waveGain: number;
  audioView: "wave" | "spectrum";
  /** 擦洗音：拖时间轴/步进时播一小段声音，默认不开 */
  scrubAudio: boolean;
  snap: boolean;
}

export const layoutStore = createStore<LayoutState>({
  sideW: clampN(LAYOUT.sideW, 340, 640, 400),
  lblW: clampN(LAYOUT.lblW, 90, 360, 160),
  tlViewH: clampN(LAYOUT.tlH, 150, 900, Math.round(window.innerHeight * .45)),
  rowH: {
    video: clampN(LAYOUT.rowH?.video, VIDEO_ROW_MIN, VIDEO_ROW_MAX, VIDEO_ROW_H0),
    wave: clampN(LAYOUT.rowH?.wave, LAYOUT.audioView === "spectrum" ? SPECTRUM_ROW_H0 : ROW_MIN,
      WAVE_ROW_MAX, LAYOUT.audioView === "spectrum" ? SPECTRUM_ROW_H0 : ROW_H0),
    ja: clampN(LAYOUT.rowH?.ja, ROW_MIN, ROW_MAX, ROW_H0),
    zh: clampN(LAYOUT.rowH?.zh, ROW_MIN, ROW_MAX, ROW_H0),
  },
  waveGain: (typeof LAYOUT.waveGain === "number" && isFinite(LAYOUT.waveGain) && LAYOUT.waveGain >= 0)
    ? Math.min(LAYOUT.waveGain, WAVE_GAIN_MAX) : 0,
  audioView: LAYOUT.audioView === "spectrum" ? "spectrum" : "wave",
  scrubAudio: LAYOUT.scrubAudio === true,
  snap: true,
});

/** 缩放的初值也来自本机偏好（下限等视口/时长就绪后再钳） */
viewStore.set({ pps: clampN(LAYOUT.pps, 0.001, ZOOM_MAX, 46) });

let saveTimer: ReturnType<typeof setTimeout> | undefined;

function persistLayout() {
  const l = layoutStore.get();
  try {
    localStorage.setItem(LAYOUT_KEY, JSON.stringify({
      sideW: l.sideW, pps: viewStore.get().pps, rowH: l.rowH, tlH: l.tlViewH,
      lblW: l.lblW, waveGain: l.waveGain, audioView: l.audioView,
      scrubAudio: l.scrubAudio, rowV: 2,
    }));
  } catch { /* 隐私模式下写不进去，忽略 */ }
}

export function saveLayout() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    saveTimer = undefined;
    persistLayout();
  }, 300);
}

/** 关闭独立编辑器窗口前立即写入，避免最后一次拖动仍在防抖队列中。 */
export function flushLayout() {
  clearTimeout(saveTimer);
  saveTimer = undefined;
  persistLayout();
}

/** 轨道区高度再按窗口收一道，轨道再多也挤不掉视频区 */
export const tlCap = () => Math.min(layoutStore.get().tlViewH, Math.max(180, window.innerHeight - 300));

export const setRowH = (key: "video" | "wave" | "ja" | "zh", v: number) =>
  layoutStore.set(s => ({ rowH: { ...s.rowH, [key]: v } }));
