import { useEffect, useRef, useState } from "react";
import type { Dispatch, SetStateAction } from "react";
import type { FineSubBaseUrlState, FineSubKeyState, FineSubModelProvider, FineSubModelRoute, FineSubModelRoutingState } from "../bridge/settings.ts";
import { fineSubSettings } from "../bridge/settings.ts";
import type { ProviderChoice } from "./llmRouting.ts";
import { choiceIdOf, effectiveRoute, hasDraft, loadModelMemory, modelTestTimeoutSeconds, pickModelForProvider, preferredMember, providerChoices, routeSettingName, saveModelMemory } from "./llmRouting.ts";
import { InfoTip } from "./InfoTip.tsx";
import { Mark } from "./Mark.tsx";
import { CustomSelect } from "./CustomSelect.tsx";
import { Notice } from "./Notice.tsx";
import type { NoticeTone } from "./Notice.tsx";
import "./LlmConfigurationCard.css";

interface LlmConfigurationCardProps {
  keys: FineSubKeyState[];
  baseUrls: FineSubBaseUrlState[];
  modelRouting: FineSubModelRoutingState;
  /** 联网搜索凭据（已过滤掉 HF_TOKEN）。 */
  searchKeys?: FineSubKeyState[];
  correctorIssue?: string;
  drafts: Record<string, string>;
  busy: boolean;
  message?: string;
  messageTone?: NoticeTone;
  onDismissMessage?: () => void;
  setDrafts: Dispatch<SetStateAction<Record<string, string>>>;
  onSave: (updates: Record<string, string | null>, name: string) => Promise<void>;
}

function maskedOf(key: FineSubKeyState | undefined): string {
  if (!key?.configured) return "未配置";
  if (key.masked.length > 0) {
    return key.masked.map((entry) => `${entry.name ? `${entry.name} · ` : ""}${entry.value}`).join("，");
  }
  return `${key.count} 个已配置`;
}

export function LlmConfigurationCard({
  keys,
  baseUrls,
  modelRouting,
  searchKeys = [],
  correctorIssue = "",
  drafts,
  busy,
  message = "",
  messageTone = "warn",
  onDismissMessage,
  setDrafts,
  onSave,
}: LlmConfigurationCardProps) {
  const [modalOpen, setModalOpen] = useState(false);
  const modelMemory = useRef<Record<string, Record<string, string>>>(loadModelMemory());

  useEffect(() => {
    if (!modalOpen) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setModalOpen(false);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [modalOpen]);

  const rememberModel = (routeID: string, providerID: string, model: string) => {
    if (!providerID || !model.trim()) return;
    const perRoute = modelMemory.current[routeID] ?? (modelMemory.current[routeID] = {});
    perRoute[providerID] = model.trim();
    saveModelMemory(modelMemory.current);
  };
  const savedRouteOf = (routeID: string): FineSubModelRoute =>
    routeID === "default"
      ? modelRouting.defaultRoute
      : (modelRouting.taskRoutes.find((item) => item.id === routeID)?.route ?? { provider: "", model: "" });

  const keyOf = (provider: FineSubModelProvider) => provider.keyName ? keys.find((item) => item.name === provider.keyName) : undefined;
  const urlOf = (provider: FineSubModelProvider) => provider.customEndpoint && provider.baseUrlName ? baseUrls.find((item) => item.name === provider.baseUrlName) : undefined;
  const urlValue = (provider: FineSubModelProvider) => {
    const setting = urlOf(provider);
    if (!setting) return "";
    return hasDraft(drafts, setting.name) ? drafts[setting.name] : (setting.value || setting.defaultValue);
  };
  const isConfigured = (provider: FineSubModelProvider) => {
    if (provider.kind === "cloud") return provider.available;
    if (!provider.requiresKey) return true;
    if (!provider.keyConfigured) return false;
    return !provider.customEndpoint || Boolean(urlOf(provider)?.value);
  };
  const isReady = (provider: FineSubModelProvider) => {
    if (provider.kind === "cloud") return provider.available;
    if (!provider.requiresKey) return true;
    const key = keyOf(provider);
    if (!key?.configured && !(drafts[provider.keyName] ?? "").trim()) return false;
    return !provider.customEndpoint || Boolean(urlValue(provider).trim());
  };

  const choices = providerChoices(modelRouting.providers);
  const choiceOf = (providerID: string) => {
    const provider = modelRouting.providers.find((item) => item.id === providerID);
    return choices.find((item) => item.id === choiceIdOf(provider));
  };

  const defaultRoute = effectiveRoute("default", modelRouting.defaultRoute, drafts);
  const selected = modelRouting.providers.find((item) => item.id === defaultRoute.provider);
  const selectedChoice = selected ? choiceOf(selected.id) : undefined;
  const selectedKey = selected ? keyOf(selected) : undefined;
  const selectedUrl = selected ? urlOf(selected) : undefined;
  const globalReady = Boolean(selected && defaultRoute.model && isReady(selected));

  const credentialNames = new Set(modelRouting.providers.flatMap((provider) => [provider.keyName, provider.customEndpoint ? provider.baseUrlName : ""].filter(Boolean)));
  const searchKeyNames = new Set(searchKeys.map((item) => item.name));

  // 外层卡片保存：全局模型 + 提供商 Key / Base URL
  const globalKeyUpdates = Object.fromEntries(
    keys.flatMap((item) => credentialNames.has(item.name) && drafts[item.name]?.trim() ? [[item.name, drafts[item.name].trim()]] : []),
  ) as Record<string, string>;
  const globalUrlUpdates = Object.fromEntries(
    baseUrls.flatMap((setting) => {
      if (!credentialNames.has(setting.name) || !hasDraft(drafts, setting.name)) return [];
      const value = drafts[setting.name].trim().replace(/\/+$/, "");
      return [[setting.name, !value || value === setting.defaultValue ? null : value]];
    }),
  ) as Record<string, string | null>;
  const globalRouteUpdates = Object.fromEntries(
    (["provider", "model"] as const).flatMap((field) => {
      const name = routeSettingName("default", field);
      return hasDraft(drafts, name) ? [[name, drafts[name].trim() || null]] : [];
    }),
  ) as Record<string, string | null>;
  const globalUpdates = { ...globalKeyUpdates, ...globalUrlUpdates, ...globalRouteUpdates };
  const globalDirty = Object.keys(globalUpdates).length > 0;

  // 弹窗保存：按环节指定模型 + 联网搜索凭据
  const taskRouteUpdates = Object.fromEntries(
    modelRouting.taskRoutes.flatMap((item) => (["provider", "model"] as const).flatMap((field) => {
      const name = routeSettingName(item.id, field);
      return hasDraft(drafts, name) ? [[name, drafts[name].trim() || null]] : [];
    })),
  ) as Record<string, string | null>;
  const searchUpdates = Object.fromEntries(
    searchKeys.flatMap((item) => searchKeyNames.has(item.name) && drafts[item.name]?.trim() ? [[item.name, drafts[item.name].trim()]] : []),
  ) as Record<string, string>;
  const modalUpdates = { ...taskRouteUpdates, ...searchUpdates };
  const modalDirty = Object.keys(modalUpdates).length > 0;

  const blocker = !selected
    ? "先选择一个提供商"
    : selected.kind === "cloud" && !selected.available
      ? "请先登录 Nonoka Cloud 账号"
      : selected.requiresKey && !(selectedKey?.configured || (drafts[selected.keyName] ?? "").trim())
        ? `请填写 ${selected.label} API Key`
        : selected.customEndpoint && !urlValue(selected).trim()
          ? `请填写 ${selected.label} Base URL`
          : !defaultRoute.model
            ? "请选择全局模型"
            : "";

  const selectProvider = (routeID: string, nextProvider: string) => {
    const saved = savedRouteOf(routeID);
    const current = effectiveRoute(routeID, saved, drafts);
    rememberModel(routeID, current.provider, current.model);
    const next = modelRouting.providers.find((item) => item.id === nextProvider);
    const model = pickModelForProvider({
      provider: next,
      remembered: modelMemory.current[routeID]?.[nextProvider],
      savedModel: saved.provider === nextProvider ? saved.model : "",
    });
    setDrafts((drafted) => ({
      ...drafted,
      [routeSettingName(routeID, "provider")]: nextProvider,
      [routeSettingName(routeID, "model")]: model,
    }));
  };

  const selectChoice = (routeID: string, choiceID: string) => {
    const choice = choices.find((item) => item.id === choiceID);
    if (!choice) {
      selectProvider(routeID, "");
      return;
    }
    const current = effectiveRoute(routeID, savedRouteOf(routeID), drafts).provider;
    selectProvider(routeID, preferredMember(choice, { current, usable: isReady }).id);
  };

  const choiceSuffix = (choice: ProviderChoice) => {
    const single = choice.members.length === 1 ? choice.members[0] : undefined;
    if (single?.kind === "cloud") return single.available ? "（免费）" : "（需登录云端）";
    if (single && !single.requiresKey) return single.available ? "（本地 CLI）" : "（未检测到 CLI）";
    return choice.members.some(isConfigured) ? "" : "（未配置）";
  };

  const tierLabelOf = (provider: FineSubModelProvider) =>
    `${provider.tierLabel || provider.label}${isConfigured(provider) ? "" : "（未配置 Key）"}`;

  const renderModelControl = (routeID: string, provider: FineSubModelProvider | undefined, model: string, placeholder: string) => {
    const modelName = routeSettingName(routeID, "model");
    if (!provider) return <input aria-label={`${routeID} 模型`} value="" disabled placeholder={placeholder} />;
    const editModel = (value: string) => {
      rememberModel(routeID, provider.id, value);
      setDrafts((current) => ({ ...current, [modelName]: value }));
    };
    if (provider.mode === "select") {
      return (
        <CustomSelect
          ariaLabel={`${routeID} 模型`}
          value={model}
          options={provider.models.map((item) => ({ value: item.id, label: `${item.label} · ${item.id}` }))}
          onChange={editModel}
        />
      );
    }
    return <input aria-label={`${routeID} 模型 ID`} autoComplete="off" autoCapitalize="none" autoCorrect="off" spellCheck={false} placeholder={`填写 ${provider.label} 模型 ID`} value={model} onChange={(event) => editModel(event.target.value)} />;
  };

  const renderTierControl = (routeID: string, choice: ProviderChoice | undefined, current: string, members: FineSubModelProvider[]) => {
    if (!choice || members.length < 2) return null;
    return (
      <CustomSelect
        ariaLabel={`${routeID} ${choice.label} 档位`}
        value={current}
        options={members.map((item) => ({ value: item.id, label: tierLabelOf(item) }))}
        onChange={(value) => selectProvider(routeID, value)}
      />
    );
  };

  const renderTaskRoute = (routeID: string, saved: FineSubModelRoute) => {
    const route = effectiveRoute(routeID, saved, drafts);
    const provider = modelRouting.providers.find((item) => item.id === route.provider);
    const choice = provider ? choiceOf(provider.id) : undefined;
    const offered = (item: ProviderChoice) => item.members.filter((member) => isConfigured(member) || member.id === saved.provider);
    const options = choices.filter((item) => offered(item).length > 0);
    const tier = choice && provider ? renderTierControl(routeID, choice, provider.id, offered(choice)) : null;
    return (
      <div className={tier ? "llm-route-controls tiered" : "llm-route-controls"}>
        <CustomSelect
          ariaLabel={`${routeID} 提供商`}
          menuMaxHeight={360}
          value={choiceIdOf(provider)}
          options={[
            { value: "", label: "跟随全局模型" },
            ...options.map((item) => ({
              value: item.id,
              label: `${item.label}${item.members.length === 1 && item.members[0].kind === "local" && !item.members[0].available ? "（未检测到 CLI）" : ""}`,
            })),
          ]}
          onChange={(value) => selectChoice(routeID, value)}
        />
        {tier}
        {renderModelControl(routeID, provider, route.model, "使用全局模型")}
      </div>
    );
  };

  const [testState, setTestState] = useState<{ status: "running" | "ok" | "failed"; text: string; route: string } | null>(null);
  const savedProvider = modelRouting.providers.find((item) => item.id === modelRouting.defaultRoute.provider);
  const savedReady = Boolean(savedProvider && modelRouting.defaultRoute.model && isConfigured(savedProvider));
  const savedRouteKey = `${modelRouting.defaultRoute.provider}/${modelRouting.defaultRoute.model}`;
  const shownTest = testState && !globalDirty && testState.route === savedRouteKey ? testState : null;

  const runTest = async () => {
    onDismissMessage?.();
    const route = savedRouteKey;
    setTestState({ status: "running", text: "正在测试…", route });
    const started = performance.now();
    try {
      const result = await fineSubSettings.testModel(modelTestTimeoutSeconds(savedProvider));
      const seconds = ((performance.now() - started) / 1000).toFixed(1);
      const reply = result.content.trim().replace(/\s+/g, " ").slice(0, 32);
      setTestState({
        status: "ok",
        text: `连接正常 · ${seconds}s${reply ? ` · 「${reply}」` : ""}`,
        route,
      });
    } catch (error) {
      setTestState({ status: "failed", text: `测试失败：${error instanceof Error ? error.message : String(error)}`, route });
    }
  };

  const searchConfiguredCount = searchKeys.filter((item) => item.configured).length;
  const customTaskRouteCount = modelRouting.taskRoutes.filter((item) => Boolean(item.route.provider && item.route.model)).length;

  const providerHintTip = selected && !selected.requiresKey
    ? [
      selected.kind === "cloud"
        ? `通过 Nonoka Cloud 账号运行（${selected.available ? "已登录" : "未登录"}）`
        : `本机 CLI 订阅，无需 API Key（${selected.available ? "已检测到" : "未检测到"}）`,
      selected.note || "",
    ].filter(Boolean).join("\n")
    : "";

  const isLlmReady = !correctorIssue && globalReady;

  return (
    <>
      <article className="panel llm-config-card llm-config-compact">
        <section className="llm-model-routing llm-model-routing-flush">
          <div className="llm-row">
            <span className="llm-row-label">
              <span className="modal-key-name">
                <strong>全局模型</strong>
                {providerHintTip && <InfoTip text={providerHintTip} label="运行方式" />}
              </span>
            </span>
            <div className="llm-row-field">
              <div className="llm-route-controls">
                <CustomSelect
                  ariaLabel="全局提供商"
                  menuMaxHeight={420}
                  value={choiceIdOf(selected)}
                  options={[
                    { value: "", label: "选择提供商" },
                    ...choices.map((item) => ({
                      value: item.id,
                      label: `${item.label}${choiceSuffix(item)}`,
                      disabled: item.members.every((member) => member.kind === "cloud" && !member.available) && item.id !== choiceIdOf(selected),
                    })),
                  ]}
                  onChange={(value) => selectChoice("default", value)}
                />
                {renderModelControl("default", selected, defaultRoute.model, "先选择提供商")}
              </div>
            </div>
            <div className="llm-row-status">
              <Mark tone={isLlmReady ? "ok" : "neutral"}>{isLlmReady ? "已就绪" : "待配置"}</Mark>
            </div>
          </div>

          {selected?.requiresKey && (
            <>
              {selectedChoice && selectedChoice.members.length > 1 && (
                <label className="llm-row">
                  <span className="llm-row-label"><strong>档位</strong></span>
                  <CustomSelect
                    ariaLabel={`${selectedChoice.label} 档位`}
                    value={selected.id}
                    options={selectedChoice.members.map((item) => ({ value: item.id, label: tierLabelOf(item) }))}
                    onChange={(value) => selectProvider("default", value)}
                  />
                </label>
              )}
              <label className="llm-row">
                <span className="llm-row-label">
                  <strong>API Key</strong>
                  <small title={maskedOf(selectedKey)}>{maskedOf(selectedKey)}</small>
                </span>
                <input
                  aria-label={`${selected.label} API Key`}
                  type="password"
                  autoComplete="off"
                  autoCapitalize="none"
                  autoCorrect="off"
                  spellCheck={false}
                  placeholder={selectedKey?.configured ? "输入新值以替换" : `粘贴 ${selected.label} API Key`}
                  value={drafts[selected.keyName] ?? ""}
                  onChange={(event) => setDrafts((current) => ({ ...current, [selected.keyName]: event.target.value }))}
                />
                {selectedKey?.configured && (
                  <button
                    type="button"
                    className="runtime-remove-button runtime-compact-btn"
                    disabled={busy}
                    onClick={() => {
                      setTestState(null);
                      void onSave({ [selected.keyName]: null }, `${selected.label} API Key`);
                    }}
                  >
                    清除
                  </button>
                )}
              </label>
              {selectedUrl && (
                <label className="llm-row">
                  <span className="llm-row-label"><strong>Base URL</strong></span>
                  <input
                    aria-label={`${selected.label} Base URL`}
                    type="url"
                    autoComplete="off"
                    autoCapitalize="none"
                    autoCorrect="off"
                    spellCheck={false}
                    placeholder="https://vendor.example/v1"
                    value={urlValue(selected)}
                    onChange={(event) => setDrafts((current) => ({ ...current, [selectedUrl.name]: event.target.value }))}
                  />
                </label>
              )}
            </>
          )}
        </section>

        <div className="llm-config-footer">
          <div className="llm-footer-left">
            {!modalOpen && message && !shownTest ? (
              <Notice className="llm-footer-notice" message={message} tone={messageTone} onDismiss={onDismissMessage} />
            ) : (
              <span className={shownTest ? `llm-test-${shownTest.status}` : undefined} title={shownTest?.text}>
                {shownTest?.text || blocker || (globalDirty ? "有未保存的更改" : "已保存")}
              </span>
            )}
            {customTaskRouteCount > 0 && <small className="summary-pill">{`${customTaskRouteCount} 个环节已自定义`}</small>}
            {searchConfiguredCount > 0 && <small className="summary-pill">联网搜索已配置</small>}
          </div>
          <div className="llm-config-actions">
            <button
              type="button"
              className="secondary-button"
              onClick={() => setModalOpen(true)}
            >
              其他配置
            </button>
            <button
              type="button"
              className="secondary-button"
              disabled={busy || globalDirty || !savedReady || testState?.status === "running"}
              title={globalDirty ? "请先保存后再测试" : "向全局模型发送一次测试请求"}
              onClick={() => void runTest()}
            >
              {testState?.status === "running" ? "测试中…" : "测试"}
            </button>
            <button
              type="button"
              className="primary-button"
              disabled={busy || !globalReady || !globalDirty}
              onClick={() => {
                setTestState(null);
                void onSave(globalUpdates, "全局模型配置");
              }}
            >
              {busy ? "正在保存…" : "保存"}
            </button>
          </div>
        </div>
      </article>

      {modalOpen && (
        <div className="settings-modal-backdrop" role="presentation" onClick={() => setModalOpen(false)}>
          <div
            className="settings-modal settings-modal-wide panel"
            role="dialog"
            aria-modal="true"
            aria-labelledby="correction-config-modal-title"
            onClick={(event) => event.stopPropagation()}
          >
            <header className="settings-modal-header">
              <div className="settings-modal-title-block">
                <h3 id="correction-config-modal-title">其他配置</h3>
                <p className="settings-modal-subtitle">按环节覆盖全局模型，以及配置本地联网搜索凭据</p>
              </div>
              <button
                type="button"
                className="settings-modal-close"
                aria-label="关闭"
                onClick={() => setModalOpen(false)}
              >
                ×
              </button>
            </header>

            <div className="settings-modal-body llm-modal-body">
              <section className="modal-section">
                <div className="modal-section-heading">
                  <h4>按环节指定模型</h4>
                </div>
                {savedReady || globalReady ? (
                  <div className="llm-model-routing">
                    {modelRouting.taskRoutes.map((item) => (
                      <div className="llm-row" key={item.id}>
                        <span className="llm-row-label"><strong title={item.id}>{item.label}</strong></span>
                        {renderTaskRoute(item.id, item.route)}
                      </div>
                    ))}
                  </div>
                ) : (
                  <p className="engine-key-unknown">请先在外部卡片完成全局模型配置后再按环节覆盖。</p>
                )}
              </section>

              {searchKeys.length > 0 && (
                <section className="modal-section">
                  <div className="modal-section-heading">
                    <h4>联网搜索配置</h4>
                    <small className="modal-section-note">用于本地联网检索专有名词与背景资料（可选）</small>
                  </div>
                  <div className="modal-key-list">
                    {searchKeys.map((item) => (
                      <div className="modal-key-item" key={item.name}>
                        <div className="modal-key-item-top">
                          <strong>{item.label}</strong>
                          <small title={maskedOf(item)}>{maskedOf(item)}</small>
                        </div>
                        {item.purpose && <p className="modal-key-desc">{item.purpose}</p>}
                        <div className="modal-key-input-row">
                          <input
                            aria-label={item.label}
                            type="password"
                            autoComplete="off"
                            autoCapitalize="none"
                            autoCorrect="off"
                            spellCheck={false}
                            placeholder={item.configured ? "输入新值以替换" : `粘贴 ${item.label} Key`}
                            value={drafts[item.name] ?? ""}
                            onChange={(event) => setDrafts((current) => ({ ...current, [item.name]: event.target.value }))}
                          />
                          {item.configured && (
                            <button
                              type="button"
                              className="runtime-remove-button runtime-compact-btn"
                              disabled={busy}
                              onClick={() => void onSave({ [item.name]: null }, item.label)}
                            >
                              清除
                            </button>
                          )}
                        </div>
                      </div>
                    ))}
                  </div>
                </section>
              )}
            </div>

            <footer className="settings-modal-footer">
              <div className="settings-modal-status">
                {message ? (
                  <Notice className="modal-footer-notice" message={message} tone={messageTone} onDismiss={onDismissMessage} />
                ) : (
                  <span>{modalDirty ? "有未保存的更改" : "已保存"}</span>
                )}
              </div>
              <div className="settings-modal-actions">
                <button type="button" className="quiet-button" onClick={() => setModalOpen(false)}>关闭</button>
                <button
                  type="button"
                  className="primary-button"
                  disabled={busy || !modalDirty}
                  onClick={() => void onSave(modalUpdates, "环节与联网搜索配置")}
                >
                  {busy ? "正在保存…" : "保存"}
                </button>
              </div>
            </footer>
          </div>
        </div>
      )}
    </>
  );
}
