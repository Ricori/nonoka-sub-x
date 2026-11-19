import { useEffect, useState } from "react";
import type { Dispatch, SetStateAction } from "react";
import type { RuntimeInstallTarget, RuntimeProvisionState } from "../bridge/runtime.ts";
import type { FineSubKeyState, FineSubSettingsState } from "../bridge/settings.ts";
import { transcriptionEngineLabel } from "../app/format.ts";
import type { EngineChoice } from "./engineChoices.ts";
import { engineInstallView, engineLayerStatus } from "./engineLayers.ts";
import { InfoTip } from "./InfoTip.tsx";
import { Mark } from "./Mark.tsx";
import { Notice, type NoticeTone } from "./Notice.tsx";
import { formatStorageBytes } from "./StorageRelocation.tsx";
import "./TranscriptionEngineSection.css";

const KIND_TAGS: Record<EngineChoice["kind"], string> = {
  "local-gpu": "本机",
  "cloud-api": "云 API",
  "cloud-service": "Nonoka Cloud",
};

const KIND_NOTES: Record<EngineChoice["kind"], string> = {
  "local-gpu": "在本机完成语音识别。需要安装基础环境与 AI 模型。",
  "cloud-api": "在本机调用第三方语音识别接口。需要配置 API Key。",
  "cloud-service": "交给 Nonoka Cloud 识别，纠错与翻译在本机完成。需要登录云端账户。",
};

interface TranscriptionEngineSectionProps {
  choices: EngineChoice[];
  current: string;
  settings: FineSubSettingsState | null;
  drafts: Record<string, string>;
  busy: boolean;
  message?: string;
  messageTone?: NoticeTone;
  onDismissMessage?: () => void;
  provision: RuntimeProvisionState | null;
  migrationActive?: boolean;
  setDrafts: Dispatch<SetStateAction<Record<string, string>>>;
  onSaveKey: (updates: Record<string, string | null>, name: string) => Promise<void>;
  onSelect: (engine: string) => void;
  onOpenAccount: () => void;
  onOpenRuntime: () => void;
  onInstallLayers: (target: RuntimeInstallTarget) => void;
}

/** The key's state from the snapshot: Nonoka's own engine keys first, then the settings' keys. */
function keyState(settings: FineSubSettingsState | null, name: string): FineSubKeyState | null {
  return settings?.engineKeys?.find((item) => item.name === name) ?? settings?.keys.find((item) => item.name === name) ?? null;
}

function tooltipFor(item: EngineChoice, provision: RuntimeProvisionState | null, migrationActive: boolean): string {
  const parts = [KIND_NOTES[item.kind]];
  if (item.billing) parts.push(item.billing);
  if (item.kind === "local-gpu") {
    if (migrationActive) {
      parts.push("旧版升级正在更新运行环境。");
    } else {
      const status = engineLayerStatus(item.layers, provision);
      if (status.known && status.missing.length) {
        const missingBase = status.missing.some((layer) => layer !== "models");
        parts.push(missingBase ? "需先在下方完成基础环境初始化。" : `需下载 AI 模型（约 ${formatStorageBytes(status.downloadBytes)}）。`);
      }
    }
  }
  const issue = item.issues[0]?.message;
  if (!item.ready && issue) parts.push(`当前状态：${issue}`);
  return parts.join("\n");
}

function statusLabelOf(item: EngineChoice): string {
  if (item.ready) return "已就绪";
  if (item.kind === "cloud-api") return "未配置";
  if (item.kind === "cloud-service") return "未登录";
  return "未就绪";
}

function maskedSummary(state: FineSubKeyState, isAddress: boolean): string {
  if (!state.configured) return isAddress ? "未配置地址" : "未配置";
  if (state.masked.length > 0) {
    return state.masked.map((entry) => `${entry.name ? `${entry.name} · ` : ""}${entry.value}`).join("，");
  }
  return `${state.count} 个已配置`;
}

export function TranscriptionEngineSection(props: TranscriptionEngineSectionProps) {
  const {
    choices,
    current,
    settings,
    drafts,
    busy,
    message = "",
    messageTone = "warn",
    onDismissMessage,
    provision,
    migrationActive = false,
    setDrafts,
    onSaveKey,
    onSelect,
    onOpenAccount,
    onOpenRuntime,
    onInstallLayers,
  } = props;

  const [modalEngineId, setModalEngineId] = useState<string | null>(null);
  const chosen = choices.find((item) => item.id === current) ?? null;
  const editingEngine = modalEngineId ? choices.find((item) => item.id === modalEngineId) ?? null : null;

  useEffect(() => {
    if (!modalEngineId) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setModalEngineId(null);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [modalEngineId]);

  const modalKeys = editingEngine ? editingEngine.keys : [];
  const modalUpdates = Object.fromEntries(
    modalKeys.flatMap((name) => {
      const val = (drafts[name] ?? "").trim();
      return val ? [[name, val]] : [];
    }),
  ) as Record<string, string>;
  const modalHasChanges = Object.keys(modalUpdates).length > 0;

  const saveModalKeys = async () => {
    if (!editingEngine || !modalHasChanges) return;
    await onSaveKey(modalUpdates, `${editingEngine.label} 凭据`);
  };

  return (
    <div className="engine-section">
      {!chosen && (
        <p className="engine-missing" role="alert">
          {`当前转写引擎「${transcriptionEngineLabel(current)}」不可用（可能已被移除），请在下面重新选择。`}
        </p>
      )}
      <div className="engine-list panel" role="radiogroup" aria-label="转写引擎">
        {choices.map((item) => {
          const selected = item.id === current;
          const selectable = item.ready;
          const install = !migrationActive && item.kind === "local-gpu" ? engineInstallView(item, provision) : null;
          const onlyModelsMissing = install !== null && install.target === "models";
          const tip = tooltipFor(item, provision, migrationActive);
          return (
            <div className={`engine-row ${selected ? "selected" : ""} ${!selectable ? "disabled" : ""}`.trim()} key={item.id}>
              <div
                role="radio"
                tabIndex={selectable ? 0 : -1}
                aria-checked={selected}
                aria-disabled={!selectable}
                className="engine-row-main"
                onClick={() => {
                  if (selectable) onSelect(item.id);
                }}
                onKeyDown={(event) => {
                  if (selectable && (event.key === " " || event.key === "Enter")) {
                    event.preventDefault();
                    onSelect(item.id);
                  }
                }}
              >
                <span className="engine-radio" aria-hidden="true" />
                <div className="engine-row-title">
                  <strong>{item.label}</strong>
                  <small className={`engine-kind engine-kind-${item.kind}`}>{KIND_TAGS[item.kind]}</small>
                  {selected && <small className="engine-current">当前</small>}
                  <InfoTip text={tip} label={item.label} />
                </div>
                <div className="engine-row-actions" onClick={(event) => event.stopPropagation()}>
                  {item.kind === "cloud-api" && item.keys.length > 0 && (
                    <button
                      type="button"
                      className="quiet-button engine-action-btn"
                      onClick={() => setModalEngineId(item.id)}
                    >
                      配置 Key
                    </button>
                  )}
                  {item.kind === "local-gpu" && !migrationActive && install && (
                    install.running ? (
                      <button type="button" className="quiet-button engine-action-btn" onClick={onOpenRuntime}>
                        查看进度
                      </button>
                    ) : onlyModelsMissing ? (
                      <button
                        type="button"
                        className="primary-button engine-action-btn"
                        disabled={install.blocked !== ""}
                        title={install.blocked || `下载 AI 模型（约 ${formatStorageBytes(install.total)}）`}
                        onClick={() => onInstallLayers("models")}
                      >
                        下载模型
                      </button>
                    ) : (
                      <button type="button" className="quiet-button engine-action-btn" onClick={onOpenRuntime}>
                        去初始化
                      </button>
                    )
                  )}
                  {item.kind === "cloud-service" && !item.ready && (
                    <button type="button" className="quiet-button engine-action-btn" onClick={onOpenAccount}>
                      去登录
                    </button>
                  )}
                  <Mark tone={item.ready ? "ok" : "neutral"}>{statusLabelOf(item)}</Mark>
                </div>
              </div>
            </div>
          );
        })}
      </div>

      {editingEngine && (
        <div
          className="settings-modal-backdrop"
          role="presentation"
          onClick={() => setModalEngineId(null)}
        >
          <div
            className="settings-modal panel"
            role="dialog"
            aria-modal="true"
            aria-labelledby="engine-key-modal-title"
            onClick={(event) => event.stopPropagation()}
          >
            <header className="settings-modal-header">
              <div className="settings-modal-title-block">
                <h3 id="engine-key-modal-title">{editingEngine.label} · 凭据配置</h3>
                {editingEngine.billing && <p className="settings-modal-subtitle">{editingEngine.billing}</p>}
              </div>
              <button
                type="button"
                className="settings-modal-close"
                aria-label="关闭"
                onClick={() => setModalEngineId(null)}
              >
                ×
              </button>
            </header>

            <div className="settings-modal-body">
              <div className="modal-key-list">
                {modalKeys.map((name) => {
                  const state = keyState(settings, name);
                  const isAddress = name.endsWith("_ENDPOINT");
                  if (!state) {
                    return <p className="engine-key-unknown" key={name}>尚未读取到凭据状态，请稍后重试</p>;
                  }
                  const value = drafts[name] ?? "";
                  return (
                    <div className="modal-key-item" key={name}>
                      <div className="modal-key-item-top">
                        <strong>{state.label}</strong>
                        <small title={maskedSummary(state, isAddress)}>{maskedSummary(state, isAddress)}</small>
                      </div>
                      {state.purpose && <p className="modal-key-desc">{state.purpose}</p>}
                      <div className="modal-key-input-row">
                        <input
                          aria-label={state.label}
                          type={isAddress ? "url" : "password"}
                          autoComplete="off"
                          autoCapitalize="none"
                          autoCorrect="off"
                          spellCheck={false}
                          placeholder={state.configured ? "输入新值以替换" : isAddress ? "https://…" : "粘贴 API Key"}
                          value={value}
                          onChange={(event) => setDrafts((drafted) => ({ ...drafted, [name]: event.target.value }))}
                        />
                        {state.configured && (
                          <button
                            type="button"
                            className="runtime-remove-button runtime-compact-btn"
                            disabled={busy}
                            onClick={() => void onSaveKey({ [name]: null }, state.label)}
                          >
                            清除
                          </button>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>

            <footer className="settings-modal-footer">
              <div className="settings-modal-status">
                {message ? (
                  <Notice className="modal-footer-notice" message={message} tone={messageTone} onDismiss={onDismissMessage} />
                ) : (
                  <span>{modalHasChanges ? "有未保存的更改" : "输入后点击保存"}</span>
                )}
              </div>
              <div className="settings-modal-actions">
                <button type="button" className="quiet-button" onClick={() => setModalEngineId(null)}>关闭</button>
                <button
                  type="button"
                  className="primary-button"
                  disabled={busy || !modalHasChanges}
                  onClick={() => void saveModalKeys()}
                >
                  {busy ? "正在保存…" : "保存"}
                </button>
              </div>
            </footer>
          </div>
        </div>
      )}
    </div>
  );
}
