import { backHome } from '../session';
import { editDirty, flushSave, saveStore } from '../store/saveStore';
import { modalStore, toast } from '../store/uiStore';

/**
 * 点窗口的 × 不退应用。主进程拦下 close 事件转发过来，判断和弹窗都在这边：
 * 没改动直接走，有改动弹页内确认框（原生 confirm 会冻结整个应用）。
 */
export function requestClose() {
  const st = saveStore.get();
  if ((!st.dirty || st.conflicted) && !editDirty()) { backHome(); return; }
  modalStore.set({ closeOpen: true });
}

// 强制更新包已下好，但主进程不会在编辑器开着时动手（会连累没保存的字幕），
// 它在等这个窗口关掉，所以这边替用户走一遍点 × 的流程。
let mandatoryUpdatePrompted = false;

export function promptMandatoryUpdate(s: { mandatory?: boolean; stage?: string; version?: string } | null | undefined) {
  if (!s?.mandatory || s.stage !== "waiting-editor") return;
  if (mandatoryUpdatePrompted) return;
  mandatoryUpdatePrompted = true;
  toast(`需要更新到 v${s.version}，请保存后关闭编辑器`, true);
  requestClose();
}

export function cancelClose() {
  modalStore.set({ closeOpen: false });
  mandatoryUpdatePrompted = false;
}

/** 「保存并关闭」：落盘成功才走，失败留在原地并说明 */
export async function saveAndClose() {
  await flushSave();
  if (editDirty()) { toast("视频轨保存失败，仍有未保存更改"); modalStore.set({ closeOpen: false }); return; }
  if (saveStore.get().conflicted) { backHome(); return; }   // 409：改动已作废，没什么可再拦的
  if (saveStore.get().dirty) { toast("保存失败，仍有未保存更改", true); modalStore.set({ closeOpen: false }); return; }
  backHome();
}
