import type { Dispatch, SetStateAction } from "react";
import type { FineSubSettingsState } from "../bridge/settings.ts";
import type { CacheStatus } from "../bridge/library.ts";
import type { RelocationProgress, StorageDestination, StorageStatus, StorageTarget } from "../bridge/storage.ts";
import { StorageLocationsCard } from "../components/StorageLocationsCard.tsx";
import { LlmConfigurationCard } from "../components/LlmConfigurationCard.tsx";
import { KeyConfigurationCard } from "../components/KeyConfigurationCard.tsx";
import "./SettingsPage.css";
import { Notice } from "../components/Notice.tsx";

interface SettingsPageProps {
  settings: FineSubSettingsState | null;
  drafts: Record<string, string>;
  busy: boolean;
  message: string;
  cache: CacheStatus | null;
  cacheBusy: boolean;
  cacheMessage: string;
  storage: StorageStatus | null;
  storageProgress: RelocationProgress | null;
  storageBusy: boolean;
  storageMessage: string;
  setDrafts: Dispatch<SetStateAction<Record<string, string>>>;
  onSaveKey: (updates: Record<string, string | null>, name: string) => Promise<void>;
  onSaveCacheLimit: (limit: number) => Promise<void>;
  onClearCache: () => Promise<void>;
  onChooseStorage: (target: StorageTarget) => Promise<StorageDestination | null>;
  onRelocateStorage: (target: StorageTarget, destination: string) => Promise<void>;
  onResetStorage: (target: StorageTarget) => Promise<void>;
  onCancelStorage: () => Promise<void>;
  onDismissMessage: () => void;
  onDismissCacheMessage: () => void;
  onDismissStorageMessage: () => void;
}

export function SettingsPage(props: SettingsPageProps) {
  const {
    settings,
    drafts,
    busy,
    message,
    cache,
    cacheBusy,
    cacheMessage,
    storage,
    storageProgress,
    storageBusy,
    storageMessage,
    setDrafts,
    onSaveKey,
    onSaveCacheLimit,
    onClearCache,
    onChooseStorage,
    onRelocateStorage,
    onResetStorage,
    onCancelStorage,
    onDismissMessage,
    onDismissCacheMessage,
    onDismissStorageMessage,
  } = props;
  const keys = settings?.keys ?? [];
  const baseUrls = settings?.baseUrls ?? [];
  const modelRouting = settings?.modelRouting;
  // 模型提供商的 Key 归模型配置面板管，这里只留检索与下载凭据。
  const modelKeyNames = new Set((modelRouting?.providers ?? []).map((provider) => provider.keyName).filter(Boolean));
  const advancedKeys = keys.filter((key) => !modelKeyNames.has(key.name));
  return (
    <section className="keys-layout">
      <article className="panel keys-intro">
        <span className="eyebrow">LLM & service credentials</span>
        <h2>模型与服务密钥</h2>
        <p><span className="keys-intro-cloud" aria-hidden="true">☁</span>使用 Nonoka Cloud 时无需配置，以下仅用于本地处理</p>
      </article>

      {modelRouting && <LlmConfigurationCard keys={keys} baseUrls={baseUrls} modelRouting={modelRouting} drafts={drafts} busy={busy} setDrafts={setDrafts} onSave={onSaveKey} />}

      {advancedKeys.length > 0 && <details className="advanced-keys panel"><summary><span><strong>高级配置</strong><small>联网检索等可选凭据</small></span><i>{advancedKeys.length} 项</i></summary><div className="key-grid">{advancedKeys.map((key) => <KeyConfigurationCard key={key.name} item={key} value={drafts[key.name] ?? ""} busy={busy} setDrafts={setDrafts} onSave={onSaveKey} />)}</div></details>}

      <StorageLocationsCard
        status={storage}
        progress={storageProgress}
        busy={storageBusy}
        message={storageMessage}
        cache={cache}
        cacheBusy={cacheBusy}
        cacheMessage={cacheMessage}
        onChoose={onChooseStorage}
        onRelocate={onRelocateStorage}
        onReset={onResetStorage}
        onCancel={onCancelStorage}
        onSaveCacheLimit={onSaveCacheLimit}
        onClearCache={onClearCache}
        onDismissMessage={onDismissStorageMessage}
        onDismissCacheMessage={onDismissCacheMessage}
      />

      {!settings && <article className="panel unavailable-card"><strong>密钥服务尚未连接</strong><p>请在桌面应用中配置密钥。</p></article>}
      <Notice className="keys-message" message={message} onDismiss={onDismissMessage} />
    </section>
  );
}
