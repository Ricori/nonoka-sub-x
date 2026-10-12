import { mediaLibrary } from "../../bridge/library.ts";
import { getVid } from "../session";
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

export async function setupVideo() {
  mountVideo(await mediaLibrary.mediaURL(getVid()));
}

export async function pickVideoFile() {
  try {
    // A dismissed picker relinks nothing and returns an empty entry: leave the
    // player as it was rather than claiming a relink that never happened.
    const relinked = await mediaLibrary.relink(getVid());
    if (!relinked?.id) return;
    videoStore.set({ audioOnly: !!relinked.audioOnly });
    mountVideo(await mediaLibrary.mediaURL(getVid()));
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
