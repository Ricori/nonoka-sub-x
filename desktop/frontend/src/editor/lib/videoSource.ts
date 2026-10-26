import { mediaLibrary } from "../../bridge/library.ts";
import { getVid } from "../session";
import { playStore } from "../store/playStore";
import { toast } from "../store/uiStore";
import { videoStore } from "../store/videoStore";
import { errText } from "../utils";

export const mountVideo = (url: string) =>
  videoStore.set({ src: url, fallbackOpen: false, badge: null });

export function showRetrieving(pctText = "") {
  videoStore.set({ retrieving: true, retrievePct: pctText, collapsed: false, fallbackOpen: true });
}

export function showVideoFallback(collapsed: boolean, msgText?: string) {
  videoStore.set({
    retrieving: false,
    collapsed,
    fallbackOpen: true,
    warn: "",
    usePath: null,
    ...(msgText != null ? { fbMsg: msgText, canTranscode: false } : {}),
  });
}

export function showPlaybackError(message: string) {
  showVideoFallback(false, message);
  videoStore.set({ canTranscode: true });
}

/**
 * 挂视频。4K、1440p 这类重素材每次 seek 都要从远处的关键帧整帧解码，拖播放头会排成长队；
 * 它们在后台转一份 720p 的预览代理（关键帧密），做好之前先播原片，做好后自动换上
 * （见 switchToProxy）。代理只管预览：转写、导出照旧读原片。
 */
export async function setupVideo() {
  const id = getVid();
  const preview = await mediaLibrary.previewURL(id, false);
  mountVideo(preview.url);
  if (!preview.heavy || preview.proxy) return;
  videoStore.set({ badge: "准备预览代理…" });
  mediaLibrary.buildProxy(id).catch((error) => proxyFailed(errText(error)));
}

export function proxyFailed(message: string) {
  videoStore.set({ badge: null });
  toast("预览代理生成失败，继续播放原片：" + message, true);
}

let proxyWait: (() => void) | null = null;

/**
 * 代理做好了：换源。正在播就等用户停下再换——播放中换 src 会把画面和播放头一起打断。
 * 换源后播放头不动，VideoStage 在新源的 loadedmetadata 里把 video 对回播放头。
 */
export async function switchToProxy() {
  const preview = await mediaLibrary.previewURL(getVid(), false);
  if (!preview.proxy) { videoStore.set({ badge: null }); return; }
  const swap = () => {
    if (videoStore.get().src === preview.url) return;
    videoStore.set({ src: preview.url, badge: null });
    toast("已换用 720p 预览代理，拖动更流畅；导出仍使用原片");
  };
  proxyWait?.();
  proxyWait = null;
  if (!playStore.get().playing) { swap(); return; }
  videoStore.set({ badge: "预览代理已就绪 · 暂停后切换" });
  const off = playStore.subscribe(() => {
    if (playStore.get().playing) return;
    off();
    proxyWait = null;
    swap();
  });
  proxyWait = off;
}

export async function pickVideoFile() {
  try {
    // A dismissed picker relinks nothing and returns an empty entry: leave the
    // player as it was rather than claiming a relink that never happened.
    const relinked = await mediaLibrary.relink(getVid());
    if (!relinked?.id) return;
    videoStore.set({ audioOnly: !!relinked.audioOnly });
    await setupVideo();
    toast("已重新关联本地视频");
  } catch (error) {
    const message = errText(error);
    if (message) toast("重新关联失败：" + message, true);
  }
}

export async function attachChosen() {
  await pickVideoFile();
}

export async function transcodeToH264() {
  videoStore.set({
    transcoding: true,
    transcodePct: "",
    warn: "",
    usePath: null,
    fallbackOpen: true,
    collapsed: false,
  });
  try {
    const result = await mediaLibrary.transcodeToH264(getVid());
    videoStore.set({ transcoding: false, transcodePct: "", canTranscode: false, badge: null });
    mountVideo(result.url);
    toast("转码完成，已存入缓存目录");
  } catch (error) {
    const message = errText(error);
    videoStore.set({ transcoding: false, transcodePct: "", badge: null });
    if (!message.includes("已取消")) toast("转码失败：" + message, true);
  }
}

export function cancelTranscode() {
  void mediaLibrary.cancelTranscode(getVid());
}
