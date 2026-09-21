import { createStore } from '../../home/lib/createStore';
import type { Clip, CtxItem, TrackPopTarget } from '../types';

// 编辑器的提示 / 页内弹窗 / 浮层。都是模块级单例，任何地方都能直接调。

// ── 轻提示 ────────────────────────────────────────────────────────
interface ToastState {
  msg: string;
  show: boolean;
  sticky: boolean;
  ok: boolean;
  onClick: (() => void) | null;
  seq: number;   // 同一句话再弹一次也要重新走一遍动画
}

export const toastStore = createStore<ToastState>({
  msg: "", show: false, sticky: false, ok: false, onClick: null, seq: 0,
});

let toastTimer: ReturnType<typeof setTimeout> | undefined;

/** durationMs 不传时 sticky 常驻、其余 2.6s 消失；传了一律按它来（可点但也该自己消失的提示） */
export function toast(msg: string, sticky?: boolean, onClick?: (() => void) | null, durationMs?: number, ok?: boolean) {
  toastStore.set(s => ({
    msg, show: true, sticky: !!sticky, ok: !!ok, onClick: onClick || null, seq: s.seq + 1,
  }));
  clearTimeout(toastTimer);
  const dur = durationMs != null ? durationMs : (sticky ? 0 : 2600);
  if (dur) toastTimer = setTimeout(() => toastStore.set({ show: false }), dur);
}

// ── 页内输入/确认弹窗 ─────────────────────────────────────────────
// 原生的都不能用：prompt 在这类壳里直接抛「不受支持」，confirm 会冻结整个应用。
// value 传字符串 = 输入模式（resolve 内容或 null）；传 null = 确认模式（resolve 布尔）。
export interface AskState {
  title: string;
  hint: string;
  value: string | null;
  okLabel: string;
  danger: boolean;
}

export const askStore = createStore<{ dialog: AskState | null }>({ dialog: null });

let askResolve: ((v: any) => void) | null = null;

export function askModal(opts: {
  title: string; hint?: string; value?: string | null; okLabel?: string; danger?: boolean;
}): Promise<any> {
  const { title, hint = "", value = null, okLabel = "确定", danger = false } = opts;
  askStore.set({ dialog: { title, hint, value, okLabel, danger } });
  return new Promise(res => { askResolve = res; });
}

export const setAskValue = (value: string) =>
  askStore.set(s => ({ dialog: s.dialog ? { ...s.dialog, value } : null }));

export function closeAsk(ok: boolean) {
  const dialog = askStore.get().dialog;
  const resolve = askResolve;
  askResolve = null;
  askStore.set({ dialog: null });
  if (!resolve || !dialog) return;
  const isInput = dialog.value !== null;
  resolve(ok ? (isInput ? dialog.value : true) : (isInput ? null : false));
}

// ── 右键 / 下拉菜单 ───────────────────────────────────────────────
export interface CtxState {
  items: CtxItem[];
  x: number;
  y: number;
  /** 给了就钉在这个矩形下方（贴右边缘对齐），不跟着鼠标跑 */
  anchor: DOMRect | null;
}

export const ctxStore = createStore<{ menu: CtxState | null }>({ menu: null });

export function showCtx(ev: { preventDefault(): void; clientX: number; clientY: number }, items: CtxItem[], anchor?: Element | null) {
  ev.preventDefault();
  ctxStore.set({
    menu: { items, x: ev.clientX, y: ev.clientY, anchor: anchor ? anchor.getBoundingClientRect() : null },
  });
}

export const closeCtx = () => ctxStore.set({ menu: null });

// ── 轨道设置弹层 / 各类模态 ───────────────────────────────────────
interface ModalState {
  /** 轨道设置弹层：null = 关；否则记下作用对象和锚点矩形 */
  trkPop: { target: TrackPopTarget; rect: DOMRect } | null;
  closeOpen: boolean;      // 关闭确认
  tplOpen: boolean;        // ASS 模板
  effectsOpen: boolean;    // 特效字幕管理
  karaokeOpen: boolean;    // K 轴（逐字时间）面板
  bootDone: boolean;       // 加载遮罩是否撤掉
  /** 切片提示气泡 */
  clipTip: { clip: Clip; x: number; y: number } | null;
  /** 右侧工作区当前标签；放在 store 里，顶栏和快捷入口也能直接切换。 */
  sideTab: "subtitle" | "style" | "effects" | "track";
}

export const modalStore = createStore<ModalState>({
  trkPop: null, closeOpen: false, tplOpen: false, effectsOpen: false, karaokeOpen: false,
  bootDone: false, clipTip: null, sideTab: "subtitle",
});

export const openTrackPop = (target: TrackPopTarget, anchor: Element) =>
  modalStore.set({ trkPop: { target, rect: anchor.getBoundingClientRect() } });
export const closeTrackPop = () => modalStore.set({ trkPop: null });

// 顶栏是窗口拖动区，不派发鼠标事件给页面；这两个弹层开着时得让它临时收起拖动，
// 否则点顶栏关不掉弹层
function syncDragLock() {
  const open = !!ctxStore.get().menu || !!modalStore.get().trkPop;
  document.body.classList.toggle("ctx-open", open);
}
ctxStore.subscribe(syncDragLock);
modalStore.subscribe(syncDragLock);
