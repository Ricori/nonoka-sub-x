import { createStore } from '../../home/lib/createStore';
import { selStore } from './selectionStore';

/**
 * 视频轨上的选中：一整段片段，或拖出来的一段时间。与字幕句的选中互斥——
 * 选中字幕就清掉这里，Delete 键才不会删错东西。
 */
export type VSel =
  | { kind: "piece"; i: number }
  | { kind: "range"; t0: number; t1: number };

export const vselStore = createStore<{ vsel: VSel | null }>({ vsel: null });

export const clearVsel = () => vselStore.set({ vsel: null });

selStore.subscribe(() => {
  const s = selStore.get();
  if (vselStore.get().vsel && (s.sel >= 0 || s.selSet.size)) clearVsel();
});
