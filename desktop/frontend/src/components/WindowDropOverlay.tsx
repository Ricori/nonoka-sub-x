import { useEffect, useRef, useState } from "react";
import { Events } from "@wailsio/runtime";
import "./WindowDropOverlay.css";

// Mirrors supportedExtensions in desktop/internal/library/service.go.
const mediaExtensions = new Set([
  "mp4", "m4v", "mov", "mkv", "webm",
  "mp3", "m4a", "aac", "flac", "wav", "ogg", "oga", "opus",
]);

function isFileDrag(event: DragEvent): boolean {
  return Array.from(event.dataTransfer?.types ?? []).includes("Files");
}

function payloadPaths(payload: unknown): string[] {
  if (Array.isArray(payload)) return payload.filter((item): item is string => typeof item === "string");
  if (!payload || typeof payload !== "object") return [];
  const record = payload as { files?: unknown; filenames?: unknown };
  const paths = Array.isArray(record.files) ? record.files : record.filenames;
  return Array.isArray(paths) ? paths.filter((item): item is string => typeof item === "string") : [];
}

function acceptedMedia(paths: string[]): { accepted: string[]; ignored: number } {
  const accepted = paths.filter((path) => mediaExtensions.has(path.split(".").pop()?.toLocaleLowerCase() ?? ""));
  return { accepted, ignored: paths.length - accepted.length };
}

interface WindowDropOverlayProps {
  onFilesDropped: (paths: string[], ignored: number) => void;
}

export function WindowDropOverlay({ onFilesDropped }: WindowDropOverlayProps) {
  const [active, setActive] = useState(false);
  const depth = useRef(0);

  useEffect(() => {
    const clear = () => {
      depth.current = 0;
      setActive(false);
    };
    const dragEnter = (event: DragEvent) => {
      if (!isFileDrag(event)) return;
      event.preventDefault();
      depth.current += 1;
      setActive(true);
    };
    const dragOver = (event: DragEvent) => {
      if (!isFileDrag(event)) return;
      event.preventDefault();
      if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
    };
    const dragLeave = (event: DragEvent) => {
      if (!isFileDrag(event)) return;
      event.preventDefault();
      depth.current -= 1;
      if (event.relatedTarget === null || depth.current <= 0) clear();
    };
    const drop = (event: DragEvent) => {
      if (isFileDrag(event)) event.preventDefault();
      clear();
    };
    window.addEventListener("dragenter", dragEnter);
    window.addEventListener("dragover", dragOver);
    window.addEventListener("dragleave", dragLeave);
    window.addEventListener("drop", drop);
    return () => {
      window.removeEventListener("dragenter", dragEnter);
      window.removeEventListener("dragover", dragOver);
      window.removeEventListener("dragleave", dragLeave);
      window.removeEventListener("drop", drop);
    };
  }, []);

  // On Windows the composition-hosted webview never sees an Explorer drag, so
  // the Go-side drop target reports enter/leave instead of DOM drag events.
  useEffect(() => Events.On("files:dragging", (event) => {
    depth.current = 0;
    setActive(event.data === true);
  }), []);

  useEffect(() => Events.On("files:dropped", (event) => {
    const paths = payloadPaths(event.data);
    if (!paths.length) return;
    const result = acceptedMedia(paths);
    if (result.accepted.length || result.ignored) onFilesDropped(result.accepted, result.ignored);
    setActive(false);
  }), [onFilesDropped]);

  return (
    <div className={`drop-overlay ${active ? "active" : ""}`} data-file-drop-target aria-hidden={!active}>
      <div className="drop-overlay-card"><span>＋</span><strong>松手即可导入视频或音频</strong><small>MP4 / MOV / MKV / WebM · MP3 / M4A / FLAC / WAV / Opus</small></div>
    </div>
  );
}
