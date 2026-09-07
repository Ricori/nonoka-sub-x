import { Service as ProviderService } from "../../bindings/github.com/Ricori/nonoka-x/desktop/internal/provider/index.js";

export interface FineSubKeyState {
  name: string;
  label: string;
  purpose: string;
  configured: boolean;
  count: number;
  masked: Array<{ name: string; value: string }>;
  storage: "missing" | "protected" | "plaintext" | "unreadable";
}

export interface FineSubSettingsState {
  schema: 1;
  keys: FineSubKeyState[];
  baseUrls: FineSubBaseUrlState[];
  modelRouting: FineSubModelRoutingState;
  llmKeyConfigured: boolean;
  // 已保存的全局模型是否可用：选好提供商与模型，且凭据或本地 CLI 到位。
  llmReady: boolean;
  retrievalKeyConfigured: boolean;
  protection: "empty" | "protected" | "plaintext" | "unreadable";
}

export type FineSubModelProviderID = "gemini-free" | "gemini-paid" | "openai" | "anthropic" | "openai-compat" | "anthropic-compat" | "local-codex" | "local-agy" | "local-workbuddy";

export interface FineSubModelOption {
  id: string;
  label: string;
  supportsAudio: boolean;
  supportsVideo: boolean;
}

export interface FineSubModelRoute {
  provider: FineSubModelProviderID | "";
  model: string;
}

export interface FineSubModelProvider {
  id: FineSubModelProviderID;
  label: string;
  mode: "select" | "input";
  models: FineSubModelOption[];
  // 选中该提供商时预填的模型；input 模式为空。
  defaultModel: string;
  // 本地 Agent 提供商用自己的 CLI 订阅运行，不需要 API Key；available 表示该 CLI 是否已在 PATH 中。
  requiresKey: boolean;
  available: boolean;
  // 该提供商对应的 Key 与 Base URL 设置项；本地 Agent 两者皆为空。
  keyName: string;
  baseUrlName: string;
  // 兼容提供商没有官方地址兜底，Base URL 是必填项。
  customEndpoint: boolean;
  keyConfigured: boolean;
  // 几个提供商折叠成设置里的一行时共享的组 id，以及这一行的名字和本档位的名
  // 字。Gemini 的免费池与付费池是同一个服务的两档配额，对配置的人来说是一个
  // 提供商，对路由来说是两个 tier。不分组的提供商三者皆为空串。
  groupId: string;
  groupLabel: string;
  tierLabel: string;
  // 选中该提供商之前就该知道的事，没有则为空串
  note: string;
}

export interface FineSubModelRoutingState {
  providers: FineSubModelProvider[];
  defaultRoute: FineSubModelRoute;
  taskRoutes: Array<{ id: string; label: string; route: FineSubModelRoute }>;
}

export interface FineSubBaseUrlState {
  name: string;
  label: string;
  defaultValue: string;
  value: string;
  customized: boolean;
}

export const fineSubSettings = {
  read: () => ProviderService.Settings() as Promise<unknown> as Promise<FineSubSettingsState>,
  saveKeys: (keys: Record<string, string | null>) =>
    ProviderService.SaveKeys(keys) as Promise<unknown> as Promise<FineSubSettingsState>,
};
