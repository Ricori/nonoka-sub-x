// 文字识别面板里的「抹除原字并替换视频」：Go 端用 LaMa 把整片里每个原字框抹掉，
// 合回原音轨，存进视频缓存目录（<缓存>/erased/，不随缓存清理删除）；编辑器随后换成这个视频继续编辑。
// 原视频不动，随时可以换回去。抹过字的视频不再画底板（见 assBuild.ts::videoErased）。

import { mediaLibrary } from "../../bridge/library.ts";
import { getVid } from "../session";
import { IDLE_ERASE, screenTextStore } from "../store/screenTextStore";
import type { ScreenTextErase } from "../store/screenTextStore";
import { askModal, toast } from "../store/uiStore";
import { videoStore } from "../store/videoStore";
import { errText } from "../utils";
import { buildPlates } from "./assBuild";
import { hasCoverBoxes } from "./openExport";
import { syncSubs } from "./subtitles";
import { releaseVideo, setupVideo } from "./videoSource";

const fileName = (path: string) => path.split(/[\\/]/).pop() || path;

const setErase = (patch: Partial<ScreenTextErase>) =>
  screenTextStore.set((s) => ({ erase: { ...s.erase, ...patch } }));

/** 有原字框可抹（抹字视频上重新识别出的框也算：会从原视频重新抹一遍） */
export const canErase = (): boolean => hasCoverBoxes();

const setEraseLogState = (state: string) =>
  screenTextStore.set((s) => (s.eraseLog ? { eraseLog: { ...s.eraseLog, state } } : {}));

export function handleEraseProgress(progress: { phase?: string; done?: number; total?: number; taskId?: string }) {
  const st = screenTextStore.get();
  if (!st.erase.running) return;
  if (progress.taskId && st.eraseLog?.taskId !== progress.taskId) {
    screenTextStore.set({ eraseLog: { taskId: progress.taskId, state: "running" } });
  }
  if (!progress.total) return;
  const phase = progress.phase === "mux" ? "mux" : "erase";
  const pct = Math.max(0, Math.min(100, Math.round((progress.done || 0) / progress.total * 100)));
  setErase({ phase, pct });
}

/** 播放器先松开旧文件，再让 Go 换源；Windows 上读着的文件换不掉，所以多给它一次机会 */
async function switchToErased(vid: string, path: string) {
  setErase({ phase: "apply", pct: null });
  releaseVideo();
  let entry;
  try {
    entry = await mediaLibrary.useErasedVideo(vid, path);
  } catch {
    await new Promise((resolve) => setTimeout(resolve, 1000));
    entry = await mediaLibrary.useErasedVideo(vid, path);
  }
  videoStore.set({ erasedFrom: entry.erasedFrom ?? "" });
}

export async function startErase() {
  const st = screenTextStore.get();
  if (st.erase.running || !canErase()) return;
  const cpu = st.options.device === "cpu" || st.check?.check?.backend === "cpu";
  const ok = await askModal({
    title: "抹除画面原字？",
    hint: "抹掉整片里识别到的原字，换成新视频继续编辑。"
      + "原视频不会改动，随时能换回。"
      + (cpu ? "当前用 CPU 处理，会非常慢。" : "整片处理需要一些时间。"),
    okLabel: "开始抹除",
  });
  if (!ok) return;
  const vid = getVid();
  const plates = buildPlates();
  setErase({ ...IDLE_ERASE, running: true, startedAt: Date.now() });
  screenTextStore.set({ eraseLog: null });
  let outcome = "failed";
  try {
    const result = await mediaLibrary.eraseScreenText(vid, plates, st.options.device, "default");
    if (getVid() !== vid) return;
    if (result.applied) {
      const entry = await mediaLibrary.get(vid);
      videoStore.set({ erasedFrom: entry?.erasedFrom ?? "" });
    } else {
      await switchToErased(vid, result.path);
    }
    syncSubs();
    outcome = "completed";
    toast("已换成抹字后的视频");
  } catch (error) {
    const message = errText(error);
    if (message.includes("已取消")) outcome = "cancelled";
    if (getVid() === vid && !message.includes("已取消")) toast("抹除原字失败：" + message, true);
  } finally {
    if (getVid() === vid) {
      setErase({ ...IDLE_ERASE });
      setEraseLogState(outcome);
      // 换源失败时播放器已经松开了：重新挂上（还是旧视频也比黑屏好）
      if (!videoStore.get().src) await setupVideo().catch(() => undefined);
    }
  }
}

export function cancelErase() {
  if (!screenTextStore.get().erase.running) return;
  setErase({ cancelling: true });
  void mediaLibrary.cancelEraseScreenText(getVid()).catch(() => setErase({ cancelling: false }));
}

export async function restoreOriginalVideo() {
  const from = videoStore.get().erasedFrom;
  if (!from || screenTextStore.get().erase.running) return;
  const ok = await askModal({
    title: "换回原视频？",
    hint: `换回「${fileName(from)}」，画面里的原字会重新出现。缓存里的抹字视频会删掉。`,
    okLabel: "换回",
  });
  if (!ok) return;
  releaseVideo();
  try {
    const entry = await mediaLibrary.restoreOriginalVideo(getVid());
    videoStore.set({ erasedFrom: entry.erasedFrom ?? "" });
    syncSubs();
    toast("已换回原视频");
  } catch (error) {
    toast("换回原视频失败：" + errText(error), true);
  } finally {
    await setupVideo().catch(() => undefined);
  }
}

export function revealErasedVideo() {
  void mediaLibrary.get(getVid())
    .then((entry) => (entry?.sourcePath ? mediaLibrary.revealInFolder(entry.sourcePath) : undefined))
    .catch(() => undefined);
}
