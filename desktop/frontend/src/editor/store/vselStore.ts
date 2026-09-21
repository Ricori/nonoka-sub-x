import { createStore } from '../../home/lib/createStore';
import { playStore } from './playStore';
import { selStore } from './selectionStore';

/**
 * 视频轨上的选中：一整段片段，或拖出来的一段时间。与字幕句的选中互斥——
 * 选中字幕就清掉这里，Delete 键才不会删错东西。
 */
export type VSel =
  | { kind: "piece"; i: number }
  | { kind: "range"; t0: number; t1: number };

interface VselState {
  vsel: VSel | null;
  /**
   * 当前操作对象是不是视频轨（否则是字幕轨）。工具栏的「切分」按它决定切哪条：
   * 点了视频轨就是视频，点字幕轨/选字幕就回到字幕，默认字幕。和 vsel 分开记——
   * 切完一刀 vsel 会被清掉，但用户还在视频轨上，接着按「切分」应该继续切视频。
   */
  videoActive: boolean;
}

export const vselStore = createStore<VselState>({ vsel: null, videoActive: false });

export const clearVsel = () => vselStore.set({ vsel: null });
export const setVideoActive = (videoActive: boolean) => vselStore.set({ videoActive });

let prev = selStore.get();
selStore.subscribe(() => {
  const s = selStore.get();
  if (vselStore.get().vsel && (s.sel >= 0 || s.selSet.size)) clearVsel();
  // 用户选了字幕或换了轨就回到字幕轨。播放中跟着播放头自动选句不算——那不是用户在点
  const picked = s.curTrack !== prev.curTrack || s.sel !== prev.sel || s.selSet !== prev.selSet;
  if (picked && !playStore.get().playing && (s.sel >= 0 || s.selSet.size || s.curTrack !== prev.curTrack)) {
    setVideoActive(false);
  }
  prev = s;
});
