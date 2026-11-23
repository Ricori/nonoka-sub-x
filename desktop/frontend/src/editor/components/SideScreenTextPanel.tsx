import { useEffect } from "react";
import { desktopWindows } from "../../bridge/windows.ts";
import { CustomSelect } from "../../components/CustomSelect.tsx";
import { seek } from "../lib/playback.ts";
import { isOcrTrack } from "../lib/screenTextDoc.ts";
import {
  cancelScreenText,
  discardTrialPreview,
  importGameTextBundle,
  refreshScreenTextEnvironment,
  requestStartScreenText,
  restoreScreenTextTask,
  startScreenText,
} from "../lib/screenTextMerge.ts";
import { docStore } from "../store/docStore.ts";
import { playStore } from "../store/playStore.ts";
import { patchScreenTextOptions, screenTextStore } from "../store/screenTextStore.ts";
import { modalStore } from "../store/uiStore.ts";
import { videoStore } from "../store/videoStore.ts";
import { viewStore } from "../store/viewStore.ts";
import { fmt } from "../utils.ts";
import { NumField, PropRow, Segmented, SideSection, Switch } from "./ui/fields.tsx";

const ACTIVE_STATES = new Set(["queued", "running"]);
const INSTALL_ISSUE_CODES = new Set([
  "missing_screen_text",
  "screen_text_layer_missing",
  "screen_text_layer_invalid",
  "screen_text_layer_outdated",
  "screen_text_not_installed",
  "ocr_package_missing",
]);

function stageLabel(stage: string, state: string): string {
  if (state === "queued") return "排队中…";
  switch (stage) {
    case "extract":
    case "screen-extract":
    case "recognize":
      return "正在识别文字…";
    case "tracks":
    case "build-tracks":
      return "正在整理文字区域…";
    case "match":
      return "正在匹配游戏剧本…";
    case "screen-translate":
    case "translate":
      return "正在翻译文字…";
    default:
      return stage ? `处理中（${stage}）…` : "正在处理文字识别…";
  }
}

export function SideScreenTextPanel() {
  const state = screenTextStore.use((s) => s);
  const audioOnly = videoStore.use((s) => s.audioOnly);
  const duration = viewStore.use((s) => s.duration);
  docStore.use((s) => s.version);

  const ocrTracks = docStore.get().tracks.filter(isOcrTrack);
  const ocrCueCount = ocrTracks.reduce((sum, track) => sum + track.segs.length, 0);

  useEffect(() => {
    void refreshScreenTextEnvironment();
    void restoreScreenTextTask();
  }, []);

  // 1. 音频文件没有视频画面
  if (audioOnly) {
    return (
      <div className="side-page">
        <div className="side-panel">
          <div className="st-empty">
            <div className="st-empty-title">纯音频项目</div>
            <p className="st-empty-desc">当前媒体没有视频画面，无法进行文字识别。</p>
          </div>
        </div>
      </div>
    );
  }

  // 2. macOS 不支持
  const unsupported = state.platform === "darwin"
    || state.check?.issues.some((issue) => issue.code === "unsupported_platform");
  if (unsupported) {
    return (
      <div className="side-page">
        <div className="side-panel">
          <div className="st-empty">
            <div className="st-empty-title">当前系统暂不支持</div>
            <p className="st-empty-desc">文字识别目前仅支持 Windows，macOS 暂不提供该功能。</p>
          </div>
        </div>
      </div>
    );
  }

  // 3. 未安装（只在主页面「设置 → 运行环境」安装，编辑器里不提供安装）
  const installMissing = (state.layerState && state.layerState !== "ready")
    || (!state.layerState && state.check?.issues.some((issue) => INSTALL_ISSUE_CODES.has(issue.code)));
  if (installMissing) {
    return (
      <div className="side-page">
        <div className="side-panel">
          <div className="st-empty">
            <div className="st-empty-title">未安装文字识别层</div>
            <p className="st-empty-desc">
              {state.layerAvailable
                ? "需要先在主页面「设置 → 运行环境」安装文字识别层，安装完成后这里会自动变为可用。"
                : "当前版本暂未提供文字识别层下载，可先在主页面「设置 → 运行环境」查看状态或离线安装。"}
            </p>
            <div className="st-actions">
              <button type="button" className="btn primary" onClick={() => void desktopWindows.openRuntimeSettings()}>
                打开设置
              </button>
              <button type="button" className="btn" disabled={state.checking}
                onClick={() => void refreshScreenTextEnvironment()}>
                {state.checking ? "检查中…" : "重新检查"}
              </button>
            </div>
          </div>
        </div>
      </div>
    );
  }

  const running = Boolean(state.task && ACTIVE_STATES.has(state.task.state));
  const progress = state.task?.progress;
  const pct = progress && progress.total && progress.total > 0
    ? Math.max(0, Math.min(100, Math.round((progress.completed / progress.total) * 100)))
    : null;
  const hw = state.check?.check;
  const checkIssues = state.check?.issues ?? [];
  const checkWarnings = state.check?.warnings ?? [];
  const blocked = Boolean(state.check && !state.check.ready);
  const { options } = state;

  return (
    <div className="side-page side-screentext-page">
      <div className="side-panel">
        {/* 4. 运行中 */}
        {running && state.task && (
          <SideSection title={state.taskIsTrial ? "试跑 1 分钟" : "正在识别"}>
            <div className="st-progress-card">
              <div className="st-progress-head">
                <span>{stageLabel(state.task.stage, state.task.state)}</span>
                {pct !== null && <span className="st-mono">{pct}%</span>}
              </div>
              <div className={"st-bar" + (pct === null ? " indeterminate" : "")}>
                <i style={{ width: pct !== null ? `${pct}%` : "40%" }} />
              </div>
              {progress?.message && <div className="st-hint">{progress.message}</div>}
              <div className="st-hint">
                可继续编辑语音字幕；如果同时在跑语音转写，两边会争抢显卡资源。
              </div>
              <div className="st-actions">
                <button type="button" className="btn" disabled={state.busy} onClick={() => void cancelScreenText()}>
                  取消任务
                </button>
              </div>
            </div>
          </SideSection>
        )}

        {/* 5. 试 1 分钟预览结果 */}
        {!running && state.trial && (
          <SideSection title="试跑预览">
            <div className="st-trial-card">
              <div className="st-trial-summary">
                <strong>{state.trial.cueCount} 处文字识别结果</strong>
                <span className="st-mono">{fmt(state.trial.start)} – {fmt(state.trial.end)}</span>
              </div>
              <p className="st-hint">
                当前仅以临时叠加层显示在预览画面里，尚未写进字幕文档。
              </p>
              <div className="st-actions">
                <button type="button" className="btn" onClick={() => seek(state.trial!.start)}>
                  跳到试用段
                </button>
                <button type="button" className="btn primary" disabled={state.busy}
                  onClick={() => void requestStartScreenText(false)}>
                  采用并全跑
                </button>
                <button type="button" className="btn" onClick={discardTrialPreview}>
                  丢弃
                </button>
              </div>
            </div>
          </SideSection>
        )}

        {/* 已有手动修改的 ocr* 轨：重新识别前先问策略 */}
        {state.editedPrompt && (
          <SideSection title="已有手动修改的文字识别轨">
            <div className="st-warn-card">
              <p className="st-hint">
                检测到现有文字识别轨已被手动修改。重新识别时要如何处理已改过的轨道？
              </p>
              <div className="st-actions vertical">
                <button type="button" className="btn primary" disabled={state.busy}
                  onClick={() => void startScreenText(false, "replace")}>
                  替换现有文字识别轨
                </button>
                <button type="button" className="btn" disabled={state.busy}
                  onClick={() => void startScreenText(false, "keep")}>
                  保留已改过的为普通轨，再追加新结果
                </button>
                <button type="button" className="btn"
                  onClick={() => screenTextStore.set({ editedPrompt: false })}>
                  取消
                </button>
              </div>
            </div>
          </SideSection>
        )}

        {/* 硬件 / 就绪提示 */}
        {hw && (
          <div className="st-hw-bar">
            <span>
              后端：{hw.backend === "cuda" ? "GPU (CUDA)" : hw.backend === "cpu" ? "CPU（较慢）" : (hw.backend || "自动")}
              {hw.vram_mb ? ` · ${Math.round(hw.vram_mb / 1024)} GB` : ""}
            </span>
            <button type="button" className="side-text-btn" disabled={state.checking}
              onClick={() => void refreshScreenTextEnvironment()}>
              {state.checking ? "检查中…" : "刷新"}
            </button>
          </div>
        )}

        {checkIssues.map((issue, idx) => (
          <div key={issue.code + idx} className="st-notice bad">{issue.message}</div>
        ))}
        {checkWarnings.map((warn, idx) => (
          <div key={warn.code + idx} className="st-notice warn">{warn.message}</div>
        ))}
        {state.error && <div className="st-notice bad">{state.error}</div>}

        {/* 完成摘要 / 已有文字识别轨 */}
        {!running && ocrTracks.length > 0 && (
          <div className="st-notice ok">
            <span>当前文档已有 {ocrTracks.length} 条文字识别轨（共 {ocrCueCount} 句）。</span>
            <button type="button" className="side-text-btn"
              onClick={() => modalStore.set({ sideTab: "track" })}>查看轨道</button>
          </div>
        )}

        {/* 设置区 */}
        <SideSection title="识别范围">
          <PropRow label="时间范围">
            <Segmented value={options.rangeMode}
              options={[{ value: "all", label: "整片" }, { value: "custom", label: "自定义" }]}
              onPick={(value) => patchScreenTextOptions({
                rangeMode: value,
                ...(value === "custom" && options.end <= options.start
                  ? { start: Math.floor(playStore.get().t || 0), end: Math.ceil(duration || 60) }
                  : {}),
              })} />
          </PropRow>
          {options.rangeMode === "custom" && (
            <div className="st-range-row">
              <NumField label="起" value={Math.round(options.start)} min={0} max={Math.max(1, Math.ceil(duration))}
                suffix="s" onInput={(value) => patchScreenTextOptions({ start: value })} />
              <NumField label="止" value={Math.round(options.end)} min={0} max={Math.max(1, Math.ceil(duration))}
                suffix="s" onInput={(value) => patchScreenTextOptions({ end: value })} />
              <button type="button" className="side-text-btn" title="把开始时间设为当前播放头"
                onClick={() => patchScreenTextOptions({ start: Math.floor(playStore.get().t || 0) })}>
                当前位置
              </button>
            </div>
          )}
          <PropRow label="跳过主字幕带" title="跳过画面底部常规语音字幕区，避免和默认语音轨重复">
            <Switch on={options.skipMain} onToggle={(skipMain) => patchScreenTextOptions({ skipMain })} />
          </PropRow>
          <PropRow label="识别区域" title="限制只识别画面里框出的区域，不框选则识别整帧">
            <div className="st-roi-ctl">
              <button type="button" className={"side-text-btn" + (state.drawingRoi ? " on" : "")}
                onClick={() => screenTextStore.set({ drawingRoi: !state.drawingRoi })}>
                {state.drawingRoi ? "取消框选" : "在画面上框选"}
              </button>
              {options.roi && (
                <button type="button" className="side-text-btn"
                  onClick={() => patchScreenTextOptions({ roi: null })}>
                  恢复整帧
                </button>
              )}
            </div>
          </PropRow>
          {state.drawingRoi && (
            <div className="st-hint">在左侧预览画面上按住左键拖出识别区域，按 Esc 取消。</div>
          )}
          {options.roi && !state.drawingRoi && (
            <div className="st-hint st-mono">
              区域：{Math.round(options.roi[0] * 100)}%,{Math.round(options.roi[1] * 100)}% – {Math.round(options.roi[2] * 100)}%,{Math.round(options.roi[3] * 100)}%
            </div>
          )}
        </SideSection>

        <SideSection title="译文与遮盖">
          <PropRow label="翻译">
            <Segmented value={options.translate}
              options={[{ value: "zh", label: "简体中文" }, { value: "none", label: "仅原文" }]}
              onPick={(translate) => patchScreenTextOptions({ translate })} />
          </PropRow>
          <PropRow label="遮盖方式" title="底板会用原字周围的背景色盖住原字；导出压制时还可选 LaMa 抹除原字">
            <Segmented value={options.cover}
              options={[{ value: "plate", label: "纯色底板" }, { value: "none", label: "不画底板" }]}
              onPick={(cover) => patchScreenTextOptions({ cover })} />
          </PropRow>
          <PropRow label="游戏剧本" title="导入 gtd-bundle/1 游戏剧本包后，识别到的游戏台词会直接匹配官方中文译文">
            <CustomSelect className="sd-select" ariaLabel="游戏剧本" value={options.game}
              options={[
                { value: "", label: "不使用" },
                ...state.games.map((item) => ({
                  value: item.game,
                  label: item.version ? `${item.game} (${item.version})` : item.game,
                })),
              ]}
              onChange={(game) => patchScreenTextOptions({ game })} />
          </PropRow>
          <PropRow label="">
            <button type="button" className="side-text-btn" disabled={state.busy}
              onClick={() => void importGameTextBundle()}>
              导入游戏剧本包 (.zip)…
            </button>
          </PropRow>
          <PropRow label="运算设备">
            <CustomSelect className="sd-select" ariaLabel="文字识别运算设备" value={options.device}
              options={[
                { value: "auto", label: "自动（优先 GPU）" },
                { value: "cuda", label: "GPU (CUDA)" },
                { value: "cpu", label: "CPU（较慢）" },
              ]}
              onChange={(device) => patchScreenTextOptions({ device: device as "auto" | "cuda" | "cpu" })} />
          </PropRow>
        </SideSection>

        {/* 操作按钮 */}
        {!running && (
          <div className="sd-foot st-foot">
            <button type="button" className="btn primary wide"
              disabled={state.busy || blocked}
              onClick={() => void requestStartScreenText(false)}>
              {ocrTracks.length > 0 ? "重新识别整片" : "开始文字识别"}
            </button>
            <button type="button" className="btn wide"
              disabled={state.busy || blocked}
              title="从当前播放头起识别 1 分钟，仅临时预览，不改动文档"
              onClick={() => void requestStartScreenText(true)}>
              从当前位置试 1 分钟
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
