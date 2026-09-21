import { useEffect } from 'react';
import {
  addSegmentAt, deleteSegment, extendCurrent, gotoNext, gotoPrev, nudgeToPlayhead, splitAtPlayhead,
} from '../lib/edits';
import { openFind } from '../lib/find';
import { redo, undo } from '../lib/history';
import { isScrubbing, scrubSound, seek, setPlaying } from '../lib/playback';
import { video } from '../lib/media';
import { cutAtPlayhead, deleteVsel } from '../lib/videoEdit';
import { manualSave } from '../store/saveStore';
import { playStore } from '../store/playStore';

/** 文本框里选中的那截字（用作 Ctrl+F 的初值），跨行的不要 */
function selectedText(): string {
  const el = document.activeElement as HTMLTextAreaElement | HTMLInputElement | null;
  if (!el || (el.tagName !== "TEXTAREA" && el.tagName !== "INPUT")) return "";
  const v = el.value.slice(el.selectionStart ?? 0, el.selectionEnd ?? 0);
  return /[\r\n]/.test(v) ? "" : v;
}

/** 全局快捷键。文本框内只放行 Ctrl+S / Ctrl+F，其余交给输入框自己 */
export function useShortcuts() {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // Ctrl+S / ⌘S 保存：在输入框内也生效，故放在 TEXTAREA 拦截之前
      if ((e.ctrlKey || e.metaKey) && (e.key === "s" || e.key === "S")) {
        e.preventDefault(); void manualSave(); return;
      }
      // Ctrl+F 查找替换：同样在文本框内也生效，选中的那截字直接拿来当查找词
      if ((e.ctrlKey || e.metaKey) && (e.key === "f" || e.key === "F")) {
        e.preventDefault(); openFind(selectedText()); return;
      }
      const tag = document.activeElement?.tagName;
      if (tag === "TEXTAREA" || tag === "INPUT") return;
      // 撤销/重做：Ctrl+Z、Ctrl+Y、Ctrl+Shift+Z（Ctrl+C 保持复制）。放在文本框拦截之后，
      // 故在原文/译文输入框里 Ctrl+Z 仍是输入框自带撤销
      if ((e.ctrlKey || e.metaKey) && (e.key === "z" || e.key === "Z")) {
        e.preventDefault();
        if (e.shiftKey) redo(); else undo();
        return;
      }
      if ((e.ctrlKey || e.metaKey) && (e.key === "y" || e.key === "Y")) {
        e.preventDefault(); redo(); return;
      }
      // Ctrl+B 在播放头处切分视频轨（与剪映一致）
      if ((e.ctrlKey || e.metaKey) && (e.key === "b" || e.key === "B")) {
        e.preventDefault(); cutAtPlayhead(); return;
      }
      const t = playStore.get().t;
      const noVideo = isScrubbing() && !video()?.paused;
      if (e.code === "Space") { e.preventDefault(); setPlaying(!playStore.get().playing); }
      else if (e.key === "ArrowLeft") { e.preventDefault(); seek(t - (e.shiftKey ? 1 : 0.1), { noVideo }); scrubSound({ jump: true }); }
      else if (e.key === "ArrowRight") { e.preventDefault(); seek(t + (e.shiftKey ? 1 : 0.1), { noVideo }); scrubSound({ jump: true }); }
      else if (e.key === "ArrowUp") { e.preventDefault(); gotoPrev(); }
      else if (e.key === "ArrowDown") { e.preventDefault(); gotoNext(); }
      else if (e.key === "i" || e.key === "I") { nudgeToPlayhead("in"); }
      else if (e.key === "o" || e.key === "O") { nudgeToPlayhead("out"); }
      else if (e.key === "n" || e.key === "N") { addSegmentAt(t); }
      else if (e.key === "d" || e.key === "D") { splitAtPlayhead(); }
      else if (e.key === "v" || e.key === "V") { extendCurrent(); }
      // 视频轨上有选中（片段或拖出来的时间段）就删它，否则删字幕句
      else if (e.key === "Delete") { if (!deleteVsel()) deleteSegment(); }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);
}
