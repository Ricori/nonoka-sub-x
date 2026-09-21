import { useEffect } from "react";
import { Events } from "@wailsio/runtime";
import { desktopUpdate } from "../../bridge/update.ts";
import { AUTOSAVE_MS } from "../constants";
import { promptMandatoryUpdate } from "../lib/closeFlow";
import { getVid, isLeaving, reportBootError } from "../session";
import { flushLayout } from "../store/layoutStore";
import { editDirty, saveStore, startAutosave } from "../store/saveStore";
import { videoStore } from "../store/videoStore";
import { fmt } from "../utils";

export function useDesktopEvents() {
  useEffect(() => {
    const onError = (event: ErrorEvent) => reportBootError(event.error || event.message);
    const onRejection = (event: PromiseRejectionEvent) => reportBootError(event.reason);
    addEventListener("error", onError);
    addEventListener("unhandledrejection", onRejection);
    return () => {
      removeEventListener("error", onError);
      removeEventListener("unhandledrejection", onRejection);
    };
  }, []);

  useEffect(() => {
    const onBeforeUnload = (event: BeforeUnloadEvent) => {
      flushLayout();
      if (isLeaving()) return;
      const state = saveStore.get();
      if ((state.dirty && !state.conflicted) || editDirty()) {
        event.preventDefault();
        event.returnValue = "";
      }
    };
    const onPageHide = () => flushLayout();
    addEventListener("beforeunload", onBeforeUnload);
    addEventListener("pagehide", onPageHide);
    return () => {
      removeEventListener("beforeunload", onBeforeUnload);
      removeEventListener("pagehide", onPageHide);
      flushLayout();
    };
  }, []);

  // 强制更新等的就是这个窗口关掉；状态事件可能早于编辑器脚本就绪，所以补查一次。
  useEffect(() => {
    const off = desktopUpdate.onStatus(promptMandatoryUpdate);
    void desktopUpdate.status().then(promptMandatoryUpdate).catch(() => undefined);
    return off;
  }, []);

  useEffect(() => startAutosave(AUTOSAVE_MS), []);

  useEffect(() => Events.On("media:progress", (event) => {
    const progress = event.data as { id?: string; stage?: string; done?: number; total?: number };
    if (progress.stage === "transcode" && progress.id === getVid() && progress.total) {
      const pct = Math.max(0, Math.min(100, Math.round((progress.done || 0) / progress.total * 100)));
      videoStore.set({
        badge: `转码 ${pct}%`,
        transcodePct: `${pct}%（${fmt(progress.done || 0)} / ${fmt(progress.total)}）`,
      });
    }
  }), []);
}
