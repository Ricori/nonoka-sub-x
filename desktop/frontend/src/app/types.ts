import type { CloudEntry } from "../bridge/cloud.ts";
import type { MediaEntry } from "../bridge/library.ts";
import type { TaskSnapshot } from "../providers/types.ts";

export type Section = "knowledge" | "library" | "tasks" | "plugin" | "plugins" | "runtime" | "keys" | "adminKeys" | "account" | "about";
export type NavigationSection = "knowledge" | "library" | "tasks" | "plugins" | "runtime" | "settings" | "adminKeys" | "about";
export type ExecutionMode = "local" | "cloud";
export type LoadState = "loading" | "ready" | "error";
export type LibraryFilter = "all" | "ready" | "running" | "cloud" | "missing";
export type SortMode = "recent" | "name" | "duration";
export type ViewMode = "grid" | "list";
export type Theme = "dark" | "light";

/** Which copy of a media's subtitles 删除字幕 removes. */
export type SubtitleDeleteScope = "local" | "cloud" | "both";

export type DialogState =
  | { kind: "rename"; entry: MediaEntry; value: string }
  | { kind: "delete-subtitles"; entry: MediaEntry; cloudEntry?: CloudEntry; scope: SubtitleDeleteScope }
  | { kind: "remove"; entry: MediaEntry }
  | { kind: "cloud-remove"; entry: CloudEntry };

export type LibraryItem =
  | { kind: "local"; entry: MediaEntry }
  | { kind: "cloud"; entry: CloudEntry };

/** Where the cloud half of the library stands. `offline` keeps the last
    listing on screen; `syncedAt` is 0 until a listing has arrived. */
export interface CloudSyncState {
  /** A key is stored on this machine, so the library is kept in sync. */
  linked: boolean;
  phase: "idle" | "syncing" | "offline";
  syncedAt: number;
  error: string;
}

export interface TaskHistoryEntry {
  taskId: string;
  provider: ExecutionMode;
  mediaId: string;
  title: string;
  snapshot: TaskSnapshot;
}

export const activeStates = new Set(["queued", "running"]);
export const recoverableStates = new Set(["failed", "cancelled", "interrupted"]);
export const taskHistoryLimit = 50;
