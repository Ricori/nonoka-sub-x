import { useEffect, useState } from "react";
import type { Dispatch, ReactNode, SetStateAction } from "react";
import type { FineSubSettingsState } from "../bridge/settings.ts";
import type { CacheStatus } from "../bridge/library.ts";
import type { RelocationProgress, StorageDestination, StorageStatus, StorageTarget } from "../bridge/storage.ts";
import { StorageLocationsCard } from "../components/StorageLocationsCard.tsx";
import { LlmConfigurationCard } from "../components/LlmConfigurationCard.tsx";
import { ProxySettingsCard } from "../components/ProxySettingsCard.tsx";
import { RuntimeSection } from "../components/RuntimeSection.tsx";
import { TranscriptionEngineSection } from "../components/TranscriptionEngineSection.tsx";
import type { EngineChoice } from "../components/engineChoices.ts";
import { useRuntimeCache } from "../components/runtimeCache.ts";
import type { Capabilities } from "../providers/types.ts";
import "./SettingsPage.css";
import { Notice } from "../components/Notice.tsx";

/** The four groups of the page, top to bottom; links elsewhere scroll to one. */
export type SettingsAnchor = "engine" | "runtime" | "correction" | "storage";

export const settingsAnchorId = (anchor: SettingsAnchor) => `settings-${anchor}`;

/** 界面隐藏的底层凭据（模型从 Nonoka 托管层下载，不再暴露 HF_TOKEN）。 */
const HIDDEN_ADVANCED_KEYS = new Set(["HF_TOKEN"]);

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
  onRefreshStorage: () => void;
  onDismissMessage: () => void;
  onDismissCacheMessage: () => void;
  onDismissStorageMessage: () => void;
  capabilities: Capabilities | null;
  engines: EngineChoice[];
  transcriptionEngine: string;
  onSelectEngine: (engine: string) => void;
  onOpenAccount: () => void;
  runtime: Omit<Parameters<typeof RuntimeSection>[0], "storage" | "storageProgress" | "storageBusy" | "storageMessage" | "onDismissStorageMessage" | "onChooseStorage" | "onRelocateStorage" | "onResetStorage" | "onCancelStorage" | "onOpenStorage" | "runtimeCache">;
  anchor: { target: SettingsAnchor; seq: number } | null;
}

function Group({ anchor, title, children }: { anchor: SettingsAnchor; title: string; children: ReactNode }) {
  return (
    <div className="settings-group" id={settingsAnchorId(anchor)}>
      <h2 className="section-title settings-group-title">{title}</h2>
      {children}
    </div>
  );
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
    onRefreshStorage,
    onDismissMessage,
    onDismissCacheMessage,
    onDismissStorageMessage,
    capabilities,
    engines,
    transcriptionEngine,
    onSelectEngine,
    onOpenAccount,
    runtime,
    anchor,
  } = props;

  const keys = settings?.keys ?? [];
  const baseUrls = settings?.baseUrls ?? [];
  const modelRouting = settings?.modelRouting;
  const modelKeyNames = new Set((modelRouting?.providers ?? []).map((provider) => provider.keyName).filter(Boolean));
  // 联网搜索配置：仅保留检索凭据，隐藏 HF_TOKEN。
  const searchKeys = keys.filter((key) => !modelKeyNames.has(key.name) && !HIDDEN_ADVANCED_KEYS.has(key.name));

  const runtimeLocation = (storage?.locations ?? []).find((location) => location.target === "runtime");
  const runtimeCache = useRuntimeCache(`${runtimeLocation?.directory ?? ""}|${runtimeLocation?.bytes ?? 0}`, onRefreshStorage);

  const [messageSource, setMessageSource] = useState<"engine" | "model">("model");
  const saveFrom = (source: "engine" | "model") => (updates: Record<string, string | null>, name: string) => {
    setMessageSource(source);
    return onSaveKey(updates, name);
  };
  const tone = message.endsWith("已保存。") ? "success" : "warn";

  useEffect(() => {
    if (!anchor) return;
    document.getElementById(settingsAnchorId(anchor.target))?.scrollIntoView({ block: "start", behavior: "smooth" });
  }, [anchor]);

  const corrector = capabilities?.correction_engines?.[0] ?? null;
  const correctorIssue = !capabilities ? "本地服务尚未启动" : corrector && !corrector.ready ? corrector.issues[0]?.message ?? "尚未就绪" : "";

  return (
    <section className="keys-layout">
      <Group anchor="engine" title="转写引擎">
        <TranscriptionEngineSection
          choices={engines}
          current={transcriptionEngine}
          settings={settings}
          drafts={drafts}
          busy={busy}
          message={messageSource === "engine" ? message : ""}
          messageTone={tone}
          onDismissMessage={onDismissMessage}
          setDrafts={setDrafts}
          onSaveKey={saveFrom("engine")}
          provision={runtime.provision}
          migrationActive={runtime.migrationActive}
          onSelect={onSelectEngine}
          onOpenAccount={onOpenAccount}
        />
      </Group>

      <Group anchor="runtime" title="运行环境">
        <RuntimeSection
          {...runtime}
          storage={storage}
          storageProgress={storageProgress}
          storageBusy={storageBusy}
          storageMessage={storageMessage}
          onDismissStorageMessage={onDismissStorageMessage}
          onChooseStorage={onChooseStorage}
          onRelocateStorage={onRelocateStorage}
          onResetStorage={onResetStorage}
          onCancelStorage={onCancelStorage}
          onOpenStorage={onOpenStorage}
          runtimeCache={runtimeCache}
        />
      </Group>

      <Group anchor="correction" title="纠错与翻译">
        {!settings ? (
          <article className="panel unavailable-card">
            <strong>配置服务尚未连接</strong>
            <Notice className="keys-message" message={message} tone={tone} onDismiss={onDismissMessage} />
          </article>
        ) : modelRouting ? (
          <LlmConfigurationCard
            keys={keys}
            baseUrls={baseUrls}
            modelRouting={modelRouting}
            searchKeys={searchKeys}
            correctorIssue={correctorIssue}
            drafts={drafts}
            busy={busy}
            message={messageSource === "model" ? message : ""}
            messageTone={tone}
            onDismissMessage={onDismissMessage}
            setDrafts={setDrafts}
            onSave={saveFrom("model")}
          />
        ) : null}
      </Group>

      <Group anchor="storage" title="存储与网络">
        <article className="panel storage-network-merged">
          <StorageLocationsCard
            embedded
            status={storage}
            progress={storageProgress}
            busy={storageBusy}
            message={storageMessage}
            cache={cache}
            cacheBusy={cacheBusy}
            cacheMessage={cacheMessage}
            targets={["video"]}
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
          <div className="storage-network-divider" />
          <ProxySettingsCard embedded />
        </article>
      </Group>
    </section>
  );
}
