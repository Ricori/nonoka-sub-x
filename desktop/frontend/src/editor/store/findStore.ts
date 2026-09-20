import { createStore } from '../../home/lib/createStore';
import type { Lang, Seg, Ti } from '../types';

/**
 * 查找/替换状态。命中（Hit）记的是句对象引用 + 那一刻的下标：句会被增删排序挪位置，
 * 真要跳过去时按引用重新定位，下标只用来给命中排序（轨 → 句 → lane → 位置）。
 */
export interface Hit {
  ti: Ti;
  i: number;
  seg: Seg;
  lang: Lang;
  start: number;
  end: number;
}

/** 在哪几个 lane 里找 */
export type FindField = "both" | "ja" | "zh";

interface FindState {
  open: boolean;
  query: string;
  repl: string;
  matchCase: boolean;
  field: FindField;
  /** 只找列表当前这条轨，还是所有轨 */
  allTracks: boolean;
  hits: Hit[];
  cursor: number;     // hits 下标，-1 = 没有命中
  focusSeq: number;   // 自增一次 = 查找框重新抢一次焦点（已经开着时再按 Ctrl+F）
}

export const findStore = createStore<FindState>({
  open: false, query: "", repl: "", matchCase: false, field: "both", allTracks: false,
  hits: [], cursor: -1, focusSeq: 0,
});

/** 当前停在哪一处命中（替换按钮、列表里的高亮都看它） */
export function activeHit(): Hit | null {
  const { hits, cursor } = findStore.get();
  return cursor >= 0 && cursor < hits.length ? hits[cursor] : null;
}
