import { Events } from "@wailsio/runtime";
import * as ScreenTextService from "../../bindings/github.com/Ricori/nonoka-x/desktop/internal/screentext/service.js";
import type { Options as ScreenTextOptions } from "../../bindings/github.com/Ricori/nonoka-x/desktop/internal/screentext/models.js";
import * as ProviderService from "../../bindings/github.com/Ricori/nonoka-x/desktop/internal/provider/service.js";
import type { EditTrack } from "../documents/types.ts";
import type { ReadinessIssue, TaskSnapshot } from "../providers/types.ts";

export type { ScreenTextOptions };

export interface ScreenTextHardwareCheck {
  backend?: string;
  driver?: string;
  driver_ok?: boolean;
  vram_mb?: number;
  nvdec?: boolean;
  models_ok?: boolean;
  marks_ok?: boolean;
  issues?: ReadinessIssue[];
  warnings?: ReadinessIssue[];
}

export interface ScreenTextCheckReport {
  schema: 1;
  ready: boolean;
  issues: ReadinessIssue[];
  warnings: ReadinessIssue[];
  busy: boolean;
  check?: ScreenTextHardwareCheck;
}

export interface ScreenTextProgressEvent {
  mediaId: string;
  taskId: string;
  snapshot: TaskSnapshot;
}

export interface DocumentChangedEvent {
  videoId: string;
  rev: number;
  reason: string;
  taskId?: string;
}

export interface ScreenTextTrialResult {
  schema: 1;
  task_id: string;
  tracks: EditTrack[];
  range: { start?: number; end?: number };
}

export interface GameTextBundle {
  game: string;
  version: string;
  fingerprint: string;
  file: string;
}

export interface GameTextListResult {
  schema: 1;
  games: GameTextBundle[];
}

export interface GameTextImportResult {
  cancelled?: boolean;
  imported?: GameTextBundle;
  games?: GameTextBundle[];
}

export const screenTextBridge = {
  check: (device = "auto") =>
    ProviderService.ScreenTextCheck(device) as Promise<unknown> as Promise<ScreenTextCheckReport>,
  result: (taskId: string) =>
    ProviderService.ScreenTextResult(taskId) as Promise<unknown> as Promise<ScreenTextTrialResult>,
  listGameTexts: () =>
    ProviderService.ScreenTextGameTexts() as Promise<unknown> as Promise<GameTextListResult>,
  importGameText: () =>
    ProviderService.ImportScreenTextGameText() as Promise<unknown> as Promise<GameTextImportResult>,
  start: (mediaId: string, options: ScreenTextOptions) =>
    ScreenTextService.Start(mediaId, options) as Promise<unknown> as Promise<TaskSnapshot>,
  status: (mediaId: string) =>
    ScreenTextService.Status(mediaId) as Promise<unknown> as Promise<TaskSnapshot | null>,
  cancel: (mediaId: string) =>
    ScreenTextService.Cancel(mediaId) as Promise<unknown> as Promise<TaskSnapshot>,
  onProgress: (handler: (event: ScreenTextProgressEvent) => void) =>
    Events.On("screentext:progress", (event) => {
      if (event.data) handler(event.data as ScreenTextProgressEvent);
    }),
  onDocumentChanged: (handler: (event: DocumentChangedEvent) => void) =>
    Events.On("document:changed", (event) => {
      if (event.data) handler(event.data as DocumentChangedEvent);
    }),
};
