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

export interface ScreenTextTrialState {
  taskId: string;
  tracks: Track[];
  cueCount: number;
  start: number;
  end: number;
}

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
  /** Temporary overlay tracks from a finished 1-minute trial (never written to docStore). */
  trial: ScreenTextTrialState | null;
  /** Whether a full run completed during this editor session. */
  lastCompletedTracks: number | null;
  /** Pending confirmation when re-running over manually edited `ocr*` tracks. */
  editedPrompt: boolean;
  /** Installed game-script bundles (`gtd-bundle/1`). */
  games: GameTextBundle[];
  /** Drawing a region of interest (ROI) box on the video stage (Phase 6). */
  drawingRoi: boolean;
  busy: boolean;
  error: string;
}

export const DEFAULT_SCREEN_TEXT_OPTIONS: ScreenTextFormOptions = {
  rangeMode: "all",
  start: 0,
  end: 0,
  skipMain: true,
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
  trial: null,
  lastCompletedTracks: null,
  editedPrompt: false,
  games: [],
  drawingRoi: false,
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
