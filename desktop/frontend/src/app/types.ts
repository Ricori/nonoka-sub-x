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

export type DialogState =
  | { kind: "rename"; entry: MediaEntry; value: string }
  | { kind: "delete-subtitles"; entry: MediaEntry }
  | { kind: "remove"; entry: MediaEntry }
  | { kind: "cloud-remove"; entry: CloudEntry };

export type LibraryItem =
  | { kind: "local"; entry: MediaEntry }
  | { kind: "cloud"; entry: CloudEntry };

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
