import { useState } from "react";
import type { Dispatch, SetStateAction } from "react";
import type { RuntimeInstallTarget, RuntimeProvisionState } from "../bridge/runtime.ts";
import type { FineSubKeyState, FineSubSettingsState } from "../bridge/settings.ts";
import { transcriptionEngineLabel } from "../app/format.ts";
import type { EngineChoice } from "./engineChoices.ts";
import { engineInstallView, engineLayerStatus, layerLabel } from "./engineLayers.ts";
import { KeyConfigurationCard } from "./KeyConfigurationCard.tsx";
import { Mark } from "./Mark.tsx";
import { formatStorageBytes } from "./StorageRelocation.tsx";
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
  /** 运行环境层的状态：引擎列表据此列出缺哪几层、要下多少、已占多少。 */
  provision: RuntimeProvisionState | null;
  setDrafts: Dispatch<SetStateAction<Record<string, string>>>;
  onSaveKey: (updates: Record<string, string | null>, name: string) => Promise<void>;
  onSelect: (engine: string) => void;
  onOpenAccount: () => void;
  onOpenRuntime: () => void;
  /** 只装选中引擎缺的层；进度与重试在下方「运行环境」里。 */
  onInstallLayers: (target: RuntimeInstallTarget) => void;
}

/** The key's state from the snapshot: Nonoka's own engine keys first, then the settings' keys (Gemini shares GEMINI_FREE with the LLM side). */
function keyState(settings: FineSubSettingsState | null, name: string): FineSubKeyState | null {
  return settings?.engineKeys?.find((item) => item.name === name) ?? settings?.keys.find((item) => item.name === name) ?? null;
}

/** 列表里每行的一句层状态：缺哪几层、要下多少，或已占多少。 */
function layerLine(item: EngineChoice, provision: RuntimeProvisionState | null): string {
  if (item.kind === "cloud-service") return "";
  const status = engineLayerStatus(item.layers, provision);
  if (!status.known || !status.required.length) return "";
  if (status.missing.length) {
    const size = status.downloadBytes > 0 ? ` · 需下载约 ${formatStorageBytes(status.downloadBytes)}` : "";
    return `缺 ${status.missing.map(layerLabel).join("、")}${size}`;
  }
  return `运行环境层已装好 · 占用 ${formatStorageBytes(status.installedBytes)}`;
}

export function TranscriptionEngineSection(props: TranscriptionEngineSectionProps) {
  const { choices, current, settings, drafts, busy, provision, setDrafts, onSaveKey, onSelect, onOpenAccount, onOpenRuntime, onInstallLayers } = props;
  const [confirmInstall, setConfirmInstall] = useState("");
  const chosen = choices.find((item) => item.id === current) ?? null;
  const chosenIssue = chosen && !chosen.ready ? chosen.issues[0]?.message ?? "尚未就绪" : "";
  return (
    <div className="engine-section">
      {!chosen && (
        <p className="engine-missing" role="alert">
          {`当前转写引擎「${transcriptionEngineLabel(current)}」不可用（可能已被移除），新建任务会被拦下。请在下面重新选择一个。`}
        </p>
      )}
      {chosen && chosenIssue && (
        <p className="engine-missing" role="alert">
          {`当前转写引擎「${chosen.label}」尚未就绪，新建任务会被拦下：${chosenIssue}。装好它缺的部分，或在下面换一个引擎。`}
        </p>
      )}
      <div className="engine-list panel" role="radiogroup" aria-label="转写引擎">
        {choices.map((item) => {
          const selected = item.id === current;
          const issue = item.issues[0]?.message ?? "";
          const layers = layerLine(item, provision);
          const install = selected ? engineInstallView(item, provision) : null;
          const confirming = install !== null && confirmInstall === item.id;
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
                  {layers && <small className="engine-row-layers">{layers}</small>}
                  {item.billing && <small className="engine-row-billing">{item.billing}</small>}
                </span>
                <Mark>{item.ready ? "已就绪" : "未就绪"}</Mark>
              </button>
              {selected && (
                <div className="engine-row-detail">
                  {!item.ready && issue && (
                    <p className="engine-issue">
                      {issue}
                      {item.kind === "local-gpu" && !install && <button type="button" className="inline-link" onClick={onOpenRuntime}>去安装运行环境</button>}
                      {item.kind === "cloud-service" && <button type="button" className="inline-link" onClick={onOpenAccount}>前往登录</button>}
                    </p>
                  )}
                  {install && (
                    <div className="engine-install">
                      <p>
                        {`只下载它缺的层：${install.layers.map((layer) => layer.bytes > 0 ? `${layer.label}（约 ${formatStorageBytes(layer.bytes)}）` : layer.label).join("、")}`}
                        {install.total > 0 && `，共约 ${formatStorageBytes(install.total)}`}
                        {install.root && <>；装在 <code>{install.root}</code></>}
                        。已装好的层不会重新下载，换引擎也不会删掉它们。
                      </p>
                      {install.running
                        ? (
                          <p className="engine-install-running">
                            正在安装运行环境层
                            <button type="button" className="inline-link" onClick={onOpenRuntime}>查看进度</button>
                          </p>
                        )
                        : (
                          <button
                            type="button"
                            className={confirming ? "primary-button" : "quiet-button"}
                            disabled={install.blocked !== ""}
                            title={install.blocked || undefined}
                            onBlur={() => setConfirmInstall("")}
                            onClick={() => {
                              if (!confirming) {
                                setConfirmInstall(item.id);
                                return;
                              }
                              setConfirmInstall("");
                              onInstallLayers(install.target);
                            }}
                          >
                            {confirming ? `确认下载${install.total > 0 ? `约 ${formatStorageBytes(install.total)}` : ""}` : install.failed ? "重试安装所缺的层" : "下载所缺的层"}
                          </button>
                        )}
                    </div>
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
