import { createStore } from '../../home/lib/createStore';
import type { LaneRef } from '../lib/stageHit';

/**
 * 预览画面里的直接操控状态。选中锚定在 **lane**（哪条轨的哪个语言轴）而不是某一句：
 * 框宽随每句文本变，擦洗过去时句子来来去去，但用户心里选中的是「这条字幕」。
 */
export interface DragKind {
  kind: "move" | "resize" | "rotate";
  /** resize 的柄：nw / n / ne / e / se / s / sw / w */
  handle?: Handle;
}

export type Handle = "nw" | "n" | "ne" | "e" | "se" | "s" | "sw" | "w";

interface StageState {
  sel: LaneRef | null;
  drag: DragKind | null;
  /** 吸附参考线，PlayRes 坐标 */
  guides: { x: number[]; y: number[] };
  /** 框上那条行内提示（例如垂直居中不吃 MarginV） */
  hint: string | null;
  /** 上一次方向键微调的时刻。连着按算一步撤销，换了选中或隔久了就重新落栈 */
  nudgeAt: number;
}

export const stageStore = createStore<StageState>({
  sel: null, drag: null, guides: { x: [], y: [] }, hint: null, nudgeAt: 0,
});

export const selectLane = (sel: LaneRef | null) =>
  stageStore.set({ sel, guides: { x: [], y: [] }, hint: null, nudgeAt: 0 });

export const clearStageSelection = () => selectLane(null);
