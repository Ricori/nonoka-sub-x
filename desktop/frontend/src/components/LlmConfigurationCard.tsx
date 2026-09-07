import { useRef } from "react";
import type { Dispatch, SetStateAction } from "react";
import type { FineSubBaseUrlState, FineSubKeyState, FineSubModelProvider, FineSubModelRoute, FineSubModelRoutingState } from "../bridge/settings.ts";
import type { ProviderChoice } from "./llmRouting.ts";
import { choiceIdOf, effectiveRoute, hasDraft, loadModelMemory, pickModelForProvider, preferredMember, providerChoices, routeSettingName, saveModelMemory } from "./llmRouting.ts";
import { Mark } from "./Mark.tsx";
import "./LlmConfigurationCard.css";

interface LlmConfigurationCardProps {
  keys: FineSubKeyState[];
  baseUrls: FineSubBaseUrlState[];
  modelRouting: FineSubModelRoutingState;
  drafts: Record<string, string>;
  busy: boolean;
  setDrafts: Dispatch<SetStateAction<Record<string, string>>>;
  onSave: (updates: Record<string, string | null>, name: string) => Promise<void>;
}

export function LlmConfigurationCard({ keys, baseUrls, modelRouting, drafts, busy, setDrafts, onSave }: LlmConfigurationCardProps) {
  // 每个路由下各提供商最近用过的模型。切走再切回来时用它复原，
  // 否则手填模型 ID 的提供商每次都得重新输入。通过 localStorage 持久化，
  // 避免关闭页面或切换其他提供商保存后丢失。
  const modelMemory = useRef<Record<string, Record<string, string>>>(loadModelMemory());
  // 空值不进记忆：离开一个还没填模型的提供商时写下空串，会把已经保存过的
  // 模型 ID 顶掉，下次切回来就是空输入框。
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
  // 已保存的凭据：只有这样的提供商才允许在任务路由里被选中。
  const isConfigured = (provider: FineSubModelProvider) => {
    if (!provider.requiresKey) return true;
    if (!provider.keyConfigured) return false;
    return !provider.customEndpoint || Boolean(urlOf(provider)?.value);
  };
  // 连同本次草稿一起算：Key 与全局模型可以在同一次保存里落盘。
  const isReady = (provider: FineSubModelProvider) => {
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
  const keyUpdates = Object.fromEntries(
    keys.flatMap((item) => credentialNames.has(item.name) && drafts[item.name]?.trim() ? [[item.name, drafts[item.name].trim()]] : []),
  ) as Record<string, string>;
  const urlUpdates = Object.fromEntries(
    baseUrls.flatMap((setting) => {
      if (!credentialNames.has(setting.name) || !hasDraft(drafts, setting.name)) return [];
      const value = drafts[setting.name].trim().replace(/\/+$/, "");
      return [[setting.name, !value || value === setting.defaultValue ? null : value]];
    }),
  ) as Record<string, string | null>;
  const routeUpdates = Object.fromEntries(
    ["default", ...modelRouting.taskRoutes.map((item) => item.id)].flatMap((routeID) => (["provider", "model"] as const).flatMap((field) => {
      const name = routeSettingName(routeID, field);
      return hasDraft(drafts, name) ? [[name, drafts[name].trim() || null]] : [];
    })),
  ) as Record<string, string | null>;
  const updates = { ...keyUpdates, ...urlUpdates, ...routeUpdates };

  const blocker = !selected
    ? "先选择一个提供商"
    : selected.requiresKey && !(selectedKey?.configured || (drafts[selected.keyName] ?? "").trim())
      ? `请填写 ${selected.label} API Key`
      : selected.customEndpoint && !urlValue(selected).trim()
        ? `请填写 ${selected.label} Base URL`
        : !defaultRoute.model
          ? "请选择全局模型"
          : "";

  const renderModelControl = (routeID: string, provider: FineSubModelProvider | undefined, model: string, placeholder: string) => {
    const modelName = routeSettingName(routeID, "model");
    if (!provider) return <input aria-label={`${routeID} 模型`} value="" disabled placeholder={placeholder} />;
    const editModel = (value: string) => {
      rememberModel(routeID, provider.id, value);
      setDrafts((current) => ({ ...current, [modelName]: value }));
    };
    if (provider.mode === "select") {
      return (
        <select aria-label={`${routeID} 模型`} value={model} onChange={(event) => editModel(event.target.value)}>
          {provider.models.map((item) => <option key={item.id} value={item.id}>{item.label} · {item.id}</option>)}
        </select>
      );
    }
    return <input aria-label={`${routeID} 模型 ID`} autoComplete="off" autoCapitalize="none" autoCorrect="off" spellCheck={false} placeholder={`填写 ${provider.label} 模型 ID`} value={model} onChange={(event) => editModel(event.target.value)} />;
  };

  // 下拉给的是行，路由存的是档位。Gemini 折成一行以后这两者不再相等，所以
  // 选中一行要先解析出启用哪一档；其余提供商的行只有一个成员，解析是恒等的。
  const selectChoice = (routeID: string, choiceID: string) => {
    const choice = choices.find((item) => item.id === choiceID);
    if (!choice) {
      selectProvider(routeID, "");
      return;
    }
    const current = effectiveRoute(routeID, savedRouteOf(routeID), drafts).provider;
    selectProvider(routeID, preferredMember(choice, { current, usable: isReady }).id);
  };

  // 一行的凭据状态取「有没有任何一档能用」：只配了付费池时，Gemini 这一行照样
  // 是配好的，不该标成未配置。
  const choiceSuffix = (choice: ProviderChoice) => {
    const single = choice.members.length === 1 ? choice.members[0] : undefined;
    if (single && !single.requiresKey) return single.available ? "（本地 CLI）" : "（未检测到 CLI）";
    return choice.members.some(isConfigured) ? "" : "（未配置凭据）";
  };

  const tierLabelOf = (provider: FineSubModelProvider) =>
    `${provider.tierLabel || provider.label}${isConfigured(provider) ? "" : "（未配置 Key）"}`;

  /** 这一行的档位选择器，只在确实有多档可选时出现。
   *
   * `members` 由调用方过滤：任务路由里没有填 Key 的入口，所以那边只列已经配好
   * 的档位；全局那一格的 Key 输入框就在下面，所以两档都列得出来。
   */
  const renderTierControl = (routeID: string, choice: ProviderChoice | undefined, current: string, members: FineSubModelProvider[]) => {
    if (!choice || members.length < 2) return null;
    return (
      <select aria-label={`${routeID} ${choice.label} 档位`} value={current} onChange={(event) => selectProvider(routeID, event.target.value)}>
        {members.map((item) => <option key={item.id} value={item.id}>{tierLabelOf(item)}</option>)}
      </select>
    );
  };

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

  const renderTaskRoute = (routeID: string, saved: FineSubModelRoute) => {
    const route = effectiveRoute(routeID, saved, drafts);
    const provider = modelRouting.providers.find((item) => item.id === route.provider);
    const choice = provider ? choiceOf(provider.id) : undefined;
    // 任务路由里没有配置凭据的入口，所以只列出已经配置好的提供商；
    // 已保存但凭据被清空的那一项仍要留着，否则会无声消失。
    const offered = (item: ProviderChoice) => item.members.filter((member) => isConfigured(member) || member.id === saved.provider);
    const options = choices.filter((item) => offered(item).length > 0);
    const tier = choice && provider ? renderTierControl(routeID, choice, provider.id, offered(choice)) : null;
    return (
      <div className={tier ? "llm-route-controls tiered" : "llm-route-controls"}>
        <select aria-label={`${routeID} 提供商`} value={choiceIdOf(provider)} onChange={(event) => selectChoice(routeID, event.target.value)}>
          <option value="">跟随全局模型</option>
          {options.map((item) => (
            <option key={item.id} value={item.id}>{item.label}{item.members.length === 1 && item.members[0].requiresKey === false && !item.members[0].available ? "（未检测到 CLI）" : ""}</option>
          ))}
        </select>
        {tier}
        {renderModelControl(routeID, provider, route.model, "使用全局模型")}
      </div>
    );
  };

  return (
    <article className="panel llm-config-card">
      <div className="llm-config-heading">
        <div>
          <span className="eyebrow">Model configuration</span>
          <h2>模型配置</h2>
          <p>LLM 模型用于纠错、规划、研究与知识处理。先选择提供商与全局模型，再为它填写 api key。</p>
        </div>
        <Mark>{globalReady ? (selected?.label ?? "已配置") : "待配置"}</Mark>
      </div>

      <section className="llm-model-routing">
        <div className="llm-routing-heading">
          <span><strong>提供商与全局模型</strong></span>
        </div>
        <div className="llm-default-route">
          <div className="llm-route-controls">
            <select aria-label="全局提供商" value={choiceIdOf(selected)} onChange={(event) => selectChoice("default", event.target.value)}>
              <option value="">选择提供商</option>
              {choices.map((item) => (
                <option key={item.id} value={item.id}>{item.label}{choiceSuffix(item)}</option>
              ))}
            </select>
            {renderModelControl("default", selected, defaultRoute.model, "先选择提供商")}
          </div>
        </div>

        {selected && (
          <div className="llm-provider-setup">
            {!selected.requiresKey ? (
              <>
                <p className="llm-routing-note">
                  {selected.label}用你自己登录的 CLI 订阅运行，无需 API Key。检测结果：{selected.available ? "已检测到该 CLI。" : "尚未检测到该 CLI，安装后会自动识别。"}
                </p>
                {/* 提供商自己声明的注意事项，后端给什么就显示什么——没有针对某一家
                    写死的分支，下次再有一条只需改后端。 */}
                {selected.note ? <p className="llm-provider-note">{selected.note}</p> : null}
              </>
            ) : (
              <>
                {selectedChoice && selectedChoice.members.length > 1 && (
                  <label className="llm-credential-row">
                    <span>
                      <strong>{selectedChoice.label} 档位</strong>
                      <small>各有自己的 Key 与模型清单，同时只启用一个</small>
                    </span>
                    <select aria-label={`${selectedChoice.label} 档位`} value={selected.id} onChange={(event) => selectProvider("default", event.target.value)}>
                      {selectedChoice.members.map((item) => <option key={item.id} value={item.id}>{tierLabelOf(item)}</option>)}
                    </select>
                  </label>
                )}
                <label className="llm-credential-row">
                  <span>
                    <strong>{selected.label} API Key</strong>
                    <small>{selectedKey?.masked.length ? selectedKey.masked.map((entry) => `${entry.name ? `${entry.name} · ` : ""}${entry.value}`).join("，") : (selectedKey?.configured ? `${selectedKey.count} 个已配置` : "未配置，保存后该提供商才可用")}</small>
                  </span>
                  <input aria-label={`${selected.label} API Key`} type="password" autoComplete="off" autoCapitalize="none" autoCorrect="off" spellCheck={false} placeholder={selectedKey?.configured ? "输入新值以替换" : `粘贴 ${selected.label} API Key`} value={drafts[selected.keyName] ?? ""} onChange={(event) => setDrafts((current) => ({ ...current, [selected.keyName]: event.target.value }))} />
                  {selectedKey?.configured && <button type="button" className="danger-link" disabled={busy} onClick={() => void onSave({ [selected.keyName]: null }, `${selected.label} API Key`)}>清除</button>}
                </label>
                {selectedUrl && (
                  <label className="llm-credential-row">
                    <span>
                      <strong>{selected.label} Base URL</strong>
                      <small>兼容端点必填，例如 https://vendor.example/v1</small>
                    </span>
                    <input aria-label={`${selected.label} Base URL`} type="url" autoComplete="off" autoCapitalize="none" autoCorrect="off" spellCheck={false} placeholder="https://vendor.example/v1" value={urlValue(selected)} onChange={(event) => setDrafts((current) => ({ ...current, [selectedUrl.name]: event.target.value }))} />
                  </label>
                )}
              </>
            )}
          </div>
        )}

        {globalReady ? (
          <details className="llm-task-routing">
            <summary><span><strong>自定义任务模型</strong><small>覆盖指定环节，其余跟随全局模型</small></span><i>配置 {modelRouting.taskRoutes.length} 个环节</i></summary>
            <div className="llm-task-route-list">
              {modelRouting.taskRoutes.map((item) => (
                <div className="llm-task-route-row" key={item.id}>
                  <span><strong>{item.label}</strong><small>{item.id}</small></span>
                  {renderTaskRoute(item.id, item.route)}
                </div>
              ))}
            </div>
          </details>
        ) : (
          <div className="llm-task-routing llm-task-routing-locked">
            <span><strong>自定义任务模型</strong><small>完成提供商与全局模型配置后可用</small></span>
            <i>{blocker}</i>
          </div>
        )}
      </section>

      <div className="llm-config-footer">
        <span>{blocker || (Object.keys(updates).length ? "配置已就绪，保存后生效" : "配置已保存")}</span>
        <button className="primary-button" disabled={busy || !globalReady || Object.keys(updates).length === 0} onClick={() => void onSave(updates, "模型配置")}>{busy ? "正在保存…" : "保存模型配置"}</button>
      </div>
    </article>
  );
}
