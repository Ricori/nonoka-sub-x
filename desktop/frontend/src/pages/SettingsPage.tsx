import { useState } from "react";
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
  onOpenStorage: (target: StorageTarget) => Promise<void>;
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
    onOpenStorage,
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

  // 两张卡共用一条保存结果；记下是谁发起的，提示就出现在那张卡里，
  // 而不是页面最底下、多半已经滚出屏幕的位置。
  const [messageSource, setMessageSource] = useState<"model" | "advanced">("model");
  const saveFrom = (source: "model" | "advanced") => (updates: Record<string, string | null>, name: string) => {
    setMessageSource(source);
    return onSaveKey(updates, name);
  };
  const tone = message.endsWith("已保存。") ? "success" : "warn";
  const noticeFor = (source: "model" | "advanced") =>
    settings && messageSource === source ? <Notice className="keys-message" message={message} tone={tone} onDismiss={onDismissMessage} /> : null;

  return (
    <section className="keys-layout">
      {!settings && (
        <article className="panel unavailable-card">
          <strong>密钥服务尚未连接</strong>
          <p>请在桌面应用中配置密钥。</p>
          <Notice className="keys-message" message={message} tone={tone} onDismiss={onDismissMessage} />
        </article>
      )}

      {settings && (
        <div className="settings-group">
          <h2 className="section-title">模型与服务</h2>
          {modelRouting && <LlmConfigurationCard keys={keys} baseUrls={baseUrls} modelRouting={modelRouting} drafts={drafts} busy={busy} message={messageSource === "model" ? message : ""} messageTone={tone} onDismissMessage={onDismissMessage} setDrafts={setDrafts} onSave={saveFrom("model")} />}

          {advancedKeys.length > 0 && (
            <details className="advanced-keys panel">
              <summary><span><strong>高级配置</strong><small>联网检索等可选凭据</small></span><i>{advancedKeys.length} 项</i></summary>
              <div className="key-grid">{advancedKeys.map((key) => <KeyConfigurationCard key={key.name} item={key} value={drafts[key.name] ?? ""} busy={busy} setDrafts={setDrafts} onSave={saveFrom("advanced")} />)}</div>
              {noticeFor("advanced")}
            </details>
          )}
        </div>
      )}

      <div className="settings-group">
        <h2 className="section-title">磁盘占用<small>默认在系统盘，可整体迁移到其他磁盘</small></h2>
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
          onOpen={onOpenStorage}
          onSaveCacheLimit={onSaveCacheLimit}
          onClearCache={onClearCache}
          onDismissMessage={onDismissStorageMessage}
          onDismissCacheMessage={onDismissCacheMessage}
        />
      </div>
    </section>
  );
}
