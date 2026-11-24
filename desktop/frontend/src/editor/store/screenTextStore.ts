import { createStore } from "../../home/lib/createStore.ts";
import type { GameTextBundle, ScreenTextCheckReport } from "../../bridge/screenText.ts";
import type { TaskSnapshot } from "../../providers/types.ts";
import type { Track } from "../types.ts";

export interface ScreenTextFormOptions {
  rangeMode: "all" | "custom";
  start: number;
  end: number;
  skipMain: boolean;
  translate: "zh" | "none";
  cover: "plate" | "none";
  device: "auto" | "cuda" | "cpu";
  game: string;
  roi: [number, number, number, number] | null;
}

/** A 1-minute trial written straight into the document, and what to put back to undo it */
export interface ScreenTextTrialUndo {
  /** the document's screen-text tracks before the trial went in */
  before: Track[];
  cueCount: number;
  start: number;
  end: number;
}

/** 在面板里用 LaMa 抹掉原字、换掉视频的那一趟 */
export interface ScreenTextErase {
  running: boolean;
  /** erase = LaMa 抹字；mux = 合回音轨；apply = 换上新视频 */
  phase: "erase" | "mux" | "apply";
  /** 0..100；还没有进度时 null */
  pct: number | null;
  startedAt: number;
  cancelling: boolean;
}

/** 最近一次抹除的 LaMa 任务，给日志框用；抹除结束后仍保留，便于回看 */
export interface ScreenTextEraseLog {
  taskId: string;
  /** running | completed | failed | cancelled（与任务快照的 state 同名） */
  state: string;
}

export const IDLE_ERASE: ScreenTextErase = { running: false, phase: "erase", pct: null, startedAt: 0, cancelling: false };

export interface ScreenTextState {
  /** Platform reported by runtime provision (`windows`, `darwin`, `linux`, ...). */
  platform: string;
  /** Layer state from provision (`missing` | `ready` | `outdated` | `downloading` | `failed` | `unavailable`). */
  layerState: string;
  layerAvailable: boolean;
  /** Result of `GET /v1/screen-text/check`. */
  check: ScreenTextCheckReport | null;
  checking: boolean;
  options: ScreenTextFormOptions;
  /** Latest task snapshot for this video (running, completed, failed, cancelled). */
  task: TaskSnapshot | null;
  /** Whether the currently tracked task is a 1-minute trial run. */
  taskIsTrial: boolean;
  /** The last trial run, already in the document; the panel offers to undo it. */
  trialUndo: ScreenTextTrialUndo | null;
  /** Whether a full run completed during this editor session. */
  lastCompletedTracks: number | null;
  /** Installed game-script bundles (`gtd-bundle/1`). */
  games: GameTextBundle[];
  /** Drawing a region of interest (ROI) box on the video stage (Phase 6). */
  drawingRoi: boolean;
  erase: ScreenTextErase;
  eraseLog: ScreenTextEraseLog | null;
  busy: boolean;
  error: string;
}

export const DEFAULT_SCREEN_TEXT_OPTIONS: ScreenTextFormOptions = {
  rangeMode: "all",
  start: 0,
  end: 0,
  skipMain: false,
  translate: "zh",
  cover: "plate",
  device: "auto",
  game: "",
  roi: null,
};

export const INITIAL_SCREEN_TEXT_STATE: ScreenTextState = {
  platform: "",
  layerState: "",
  layerAvailable: true,
  check: null,
  checking: false,
  options: { ...DEFAULT_SCREEN_TEXT_OPTIONS },
  task: null,
  taskIsTrial: false,
  trialUndo: null,
  lastCompletedTracks: null,
  games: [],
  drawingRoi: false,
  erase: { ...IDLE_ERASE },
  eraseLog: null,
  busy: false,
  error: "",
};

export const screenTextStore = createStore<ScreenTextState>({ ...INITIAL_SCREEN_TEXT_STATE });

export function resetScreenTextStore() {
  screenTextStore.set({
    ...INITIAL_SCREEN_TEXT_STATE,
    options: { ...DEFAULT_SCREEN_TEXT_OPTIONS },
  });
}

export function patchScreenTextOptions(patch: Partial<ScreenTextFormOptions>) {
  screenTextStore.set((s) => ({ options: { ...s.options, ...patch } }));
}
