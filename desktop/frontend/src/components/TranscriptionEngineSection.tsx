import type { Dispatch, SetStateAction } from "react";
import type { FineSubKeyState, FineSubSettingsState } from "../bridge/settings.ts";
import { transcriptionEngineLabel } from "../app/format.ts";
import type { EngineChoice } from "./engineChoices.ts";
import { KeyConfigurationCard } from "./KeyConfigurationCard.tsx";
import { Mark } from "./Mark.tsx";
import "./TranscriptionEngineSection.css";

const KIND_TAGS: Record<EngineChoice["kind"], string> = {
  "local-gpu": "本机运行",
  "cloud-api": "云 API",
  "cloud-service": "Nonoka Cloud",
};

const KIND_NOTES: Record<EngineChoice["kind"], string> = {
  "local-gpu": "在这台电脑上识别，音频不离开本机；需要下方「运行环境」里的 Python 环境、模型与工具",
  "cloud-api": "在本机调用第三方语音识别服务，音频会发送给该服务；只需要工具层和它的 Key",
  "cloud-service": "音轨上传到 Nonoka Cloud 识别，纠错与翻译回到本机；需要登录云端账户",
};

interface TranscriptionEngineSectionProps {
  choices: EngineChoice[];
  current: string;
  settings: FineSubSettingsState | null;
  drafts: Record<string, string>;
  busy: boolean;
  setDrafts: Dispatch<SetStateAction<Record<string, string>>>;
  onSaveKey: (updates: Record<string, string | null>, name: string) => Promise<void>;
  onSelect: (engine: string) => void;
  onOpenAccount: () => void;
  onOpenRuntime: () => void;
}

/** The key's state from the snapshot: Nonoka's own engine keys first, then the settings' keys (Gemini shares GEMINI_FREE with the LLM side). */
function keyState(settings: FineSubSettingsState | null, name: string): FineSubKeyState | null {
  return settings?.engineKeys?.find((item) => item.name === name) ?? settings?.keys.find((item) => item.name === name) ?? null;
}

export function TranscriptionEngineSection(props: TranscriptionEngineSectionProps) {
  const { choices, current, settings, drafts, busy, setDrafts, onSaveKey, onSelect, onOpenAccount, onOpenRuntime } = props;
  const chosen = choices.find((item) => item.id === current) ?? null;
  return (
    <div className="engine-section">
      {!chosen && (
        <p className="engine-missing" role="alert">
          {`当前转写引擎「${transcriptionEngineLabel(current)}」不可用（可能已被移除），新建任务会被拦下。请在下面重新选择一个。`}
        </p>
      )}
      <div className="engine-list panel" role="radiogroup" aria-label="转写引擎">
        {choices.map((item) => {
          const selected = item.id === current;
          const issue = item.issues[0]?.message ?? "";
          return (
            <div className={`engine-row ${selected ? "selected" : ""}`.trim()} key={item.id}>
              <button
                type="button"
                role="radio"
                aria-checked={selected}
                className="engine-row-main"
                onClick={() => onSelect(item.id)}
              >
                <span className="engine-radio" aria-hidden="true" />
                <span className="engine-row-copy">
                  <span className="engine-row-title">
                    <strong>{item.label}</strong>
                    <small className={`engine-kind engine-kind-${item.kind}`}>{KIND_TAGS[item.kind]}</small>
                    {selected && <small className="engine-current">当前</small>}
                  </span>
                  <small className="engine-row-note">{KIND_NOTES[item.kind]}</small>
                  {item.billing && <small className="engine-row-billing">{item.billing}</small>}
                </span>
                <Mark>{item.ready ? "已就绪" : "未就绪"}</Mark>
              </button>
              {selected && (
                <div className="engine-row-detail">
                  {!item.ready && issue && (
                    <p className="engine-issue">
                      {issue}
                      {item.kind === "local-gpu" && <button type="button" className="inline-link" onClick={onOpenRuntime}>去安装运行环境</button>}
                      {item.kind === "cloud-service" && <button type="button" className="inline-link" onClick={onOpenAccount}>前往登录</button>}
                    </p>
                  )}
                  {item.kind === "cloud-api" && item.keys.length > 0 && (
                    <div className="key-grid engine-keys">
                      {item.keys.map((name) => {
                        const state = keyState(settings, name);
                        return state
                          ? <KeyConfigurationCard key={name} item={state} value={drafts[name] ?? ""} busy={busy} setDrafts={setDrafts} onSave={onSaveKey} address={name.endsWith("_ENDPOINT")} />
                          : <p className="engine-key-unknown" key={name}>{`${name}：本地服务尚未报告这个 Key 的状态`}</p>;
                      })}
                    </div>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
