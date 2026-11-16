import { useEffect, useState } from "react";
import type { Dispatch, ReactNode, SetStateAction } from "react";
import type { FineSubSettingsState } from "../bridge/settings.ts";
import type { CacheStatus } from "../bridge/library.ts";
import type { RelocationProgress, StorageDestination, StorageStatus, StorageTarget } from "../bridge/storage.ts";
import { StorageLocationsCard } from "../components/StorageLocationsCard.tsx";
import { LlmConfigurationCard } from "../components/LlmConfigurationCard.tsx";
import { KeyConfigurationCard } from "../components/KeyConfigurationCard.tsx";
import { ProxySettingsCard } from "../components/ProxySettingsCard.tsx";
import { RuntimeSection } from "../components/RuntimeSection.tsx";
import { TranscriptionEngineSection } from "../components/TranscriptionEngineSection.tsx";
import type { EngineChoice } from "../components/engineChoices.ts";
import { useRuntimeCache } from "../components/runtimeCache.ts";
import { Mark } from "../components/Mark.tsx";
import type { Capabilities } from "../providers/types.ts";
import "./SettingsPage.css";
import { Notice } from "../components/Notice.tsx";

/** The four groups of the page, top to bottom; links elsewhere scroll to one. */
export type SettingsAnchor = "engine" | "runtime" | "correction" | "storage";

export const settingsAnchorId = (anchor: SettingsAnchor) => `settings-${anchor}`;

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
  /** 跳过尺寸缓存重新测量两个目录；清理下载缓存之后用。 */
  onRefreshStorage: () => void;
  onDismissMessage: () => void;
  onDismissCacheMessage: () => void;
  onDismissStorageMessage: () => void;
  /** 本地服务的能力：转写引擎列表、纠错引擎、运行环节就绪。 */
  capabilities: Capabilities | null;
  engines: EngineChoice[];
  transcriptionEngine: string;
  onSelectEngine: (engine: string) => void;
  onOpenAccount: () => void;
  /** 「运行环境」一段，原样是从前的运行环境页。 */
  runtime: Omit<Parameters<typeof RuntimeSection>[0], "capabilities" | "storage" | "storageProgress" | "storageBusy" | "onChooseStorage" | "onRelocateStorage" | "onCancelStorage">;
  /** 打开页面时要滚到的一段；`seq` 变了就再滚一次（同一段可以连续跳两次）。 */
  anchor: { target: SettingsAnchor; seq: number } | null;
}

function Group({ anchor, title, note, children }: { anchor: SettingsAnchor; title: string; note?: string; children: ReactNode }) {
  return (
    <div className="settings-group" id={settingsAnchorId(anchor)}>
      <h2 className="section-title">{title}{note && <small>{note}</small>}</h2>
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
  // 模型提供商的 Key 归模型配置面板管，这里只留检索与下载凭据。
  const modelKeyNames = new Set((modelRouting?.providers ?? []).map((provider) => provider.keyName).filter(Boolean));
  const advancedKeys = keys.filter((key) => !modelKeyNames.has(key.name));

  // 运行环境换了目录、装完或删掉东西之后，下载缓存的数字也要跟着重算。
  const runtimeLocation = (storage?.locations ?? []).find((location) => location.target === "runtime");
  const runtimeCache = useRuntimeCache(`${runtimeLocation?.directory ?? ""}|${runtimeLocation?.bytes ?? 0}`, onRefreshStorage);

  // 几张卡共用一条保存结果；记下是谁发起的，提示就出现在那张卡里，
  // 而不是页面最底下、多半已经滚出屏幕的位置。
  const [messageSource, setMessageSource] = useState<"engine" | "model" | "advanced">("model");
  const saveFrom = (source: "engine" | "model" | "advanced") => (updates: Record<string, string | null>, name: string) => {
    setMessageSource(source);
    return onSaveKey(updates, name);
  };
  const tone = message.endsWith("已保存。") ? "success" : "warn";
  const noticeFor = (source: "engine" | "model" | "advanced") =>
    settings && messageSource === source ? <Notice className="keys-message" message={message} tone={tone} onDismiss={onDismissMessage} /> : null;

  useEffect(() => {
    if (!anchor) return;
    document.getElementById(settingsAnchorId(anchor.target))?.scrollIntoView({ block: "start", behavior: "smooth" });
  }, [anchor]);

  // 纠错引擎目前只有 finesub correction 一个，界面不提供选择（4.9 第 39 条）。
  const corrector = capabilities?.correction_engines?.[0] ?? null;
  const correctorIssue = !capabilities ? "本地服务尚未启动" : corrector && !corrector.ready ? corrector.issues[0]?.message ?? "尚未就绪" : "";

  return (
    <section className="keys-layout">
      <Group anchor="engine" title="转写引擎" note="新建任务都用这里选中的引擎；已有任务的重试和续跑沿用它原来的引擎">
        <TranscriptionEngineSection
          choices={engines}
          current={transcriptionEngine}
          settings={settings}
          drafts={drafts}
          busy={busy}
          setDrafts={setDrafts}
          onSaveKey={saveFrom("engine")}
          onSelect={onSelectEngine}
          onOpenAccount={onOpenAccount}
          onOpenRuntime={() => document.getElementById(settingsAnchorId("runtime"))?.scrollIntoView({ block: "start", behavior: "smooth" })}
        />
        {noticeFor("engine")}
      </Group>

      <Group anchor="runtime" title="运行环境" note="本机转写用的 Python 环境、模型与工具；云 API 引擎只要工具层">
        <RuntimeSection
          {...runtime}
          capabilities={capabilities}
          storage={storage}
          storageProgress={storageProgress}
          storageBusy={storageBusy}
          onChooseStorage={onChooseStorage}
          onRelocateStorage={onRelocateStorage}
          onCancelStorage={onCancelStorage}
        />
      </Group>

      <Group anchor="correction" title="纠错与翻译">
        <article className="panel correction-engine-card">
          <div>
            <strong>当前纠错引擎：finesub correction</strong>
            <small>接任何转写引擎的结果做 LLM 纠错、翻译与知识库处理；用下面配置的模型提供商</small>
          </div>
          <Mark>{correctorIssue ? "未就绪" : "已就绪"}</Mark>
          {correctorIssue && <p className="engine-issue">{correctorIssue}</p>}
        </article>
        {!settings && (
          <article className="panel unavailable-card">
            <strong>密钥服务尚未连接</strong>
            <p>请在桌面应用中配置密钥。</p>
            <Notice className="keys-message" message={message} tone={tone} onDismiss={onDismissMessage} />
          </article>
        )}
        {settings && modelRouting && <LlmConfigurationCard keys={keys} baseUrls={baseUrls} modelRouting={modelRouting} drafts={drafts} busy={busy} message={messageSource === "model" ? message : ""} messageTone={tone} onDismissMessage={onDismissMessage} setDrafts={setDrafts} onSave={saveFrom("model")} />}
        {settings && advancedKeys.length > 0 && (
          <details className="advanced-keys panel">
            <summary><span><strong>高级配置</strong><small>联网检索等可选凭据</small></span><i>{advancedKeys.length} 项</i></summary>
            <div className="key-grid">{advancedKeys.map((key) => <KeyConfigurationCard key={key.name} item={key} value={drafts[key.name] ?? ""} busy={busy} setDrafts={setDrafts} onSave={saveFrom("advanced")} />)}</div>
            {noticeFor("advanced")}
          </details>
        )}
      </Group>

      <Group anchor="storage" title="存储与网络" note="默认在系统盘，可整体迁移到其他磁盘；网络默认跟随系统代理">
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
          runtimeCache={runtimeCache}
          onDismissMessage={onDismissStorageMessage}
          onDismissCacheMessage={onDismissCacheMessage}
        />
        <ProxySettingsCard />
      </Group>
    </section>
  );
}
