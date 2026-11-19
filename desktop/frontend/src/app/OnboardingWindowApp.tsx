import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Events } from "@wailsio/runtime";
import type { Snapshot as SidecarSnapshot } from "../../bindings/github.com/Ricori/nonoka-x/desktop/internal/sidecar/models.js";
import { cloudAccount, DEFAULT_CLOUD_BACKEND } from "../bridge/cloud.ts";
import type { CloudSession } from "../bridge/cloud.ts";
import { dataMigration } from "../bridge/migration.ts";
import type { MigrationStatus } from "../bridge/migration.ts";
import { desktopPreferences } from "../bridge/preferences.ts";
import { fineSubRuntime } from "../bridge/runtime.ts";
import type {
  PythonBootstrapState,
  RuntimeInstallTarget,
  RuntimeLayerId,
  RuntimeProvisionState,
} from "../bridge/runtime.ts";
import { fineSubSettings } from "../bridge/settings.ts";
import type { FineSubSettingsState } from "../bridge/settings.ts";
import { storageLocations } from "../bridge/storage.ts";
import type {
  RelocationProgress,
  StorageDestination,
  StorageStatus,
  StorageTarget,
} from "../bridge/storage.ts";
import { localProviderBridge, sidecarStatus } from "../bridge/wails.ts";
import { desktopWindows } from "../bridge/windows.ts";
import { engineChoices } from "../components/engineChoices.ts";
import { isMigratingError, migrationPollIntervalMs, migrationView } from "../components/migration.ts";
import { OnboardingPage } from "../pages/OnboardingPage.tsx";
import { LocalExecutionProvider } from "../providers/localProvider.ts";
import { DEFAULT_TRANSCRIPTION_ENGINE, type Capabilities } from "../providers/types.ts";
import { applyTheme, initialTheme } from "./theme.ts";
import type { Theme } from "./types.ts";

const sidecarRetryIntervalMs = 3_000;

const samePath = (left: string, right: string) =>
  left.replace(/[\\/]+$/, "").toLowerCase() === right.replace(/[\\/]+$/, "").toLowerCase();

function withRelocatedDirectory(status: StorageStatus | null, progress: RelocationProgress): StorageStatus | null {
  if (!status?.locations || !progress.destination) return status;
  return {
    ...status,
    locations: status.locations.map((location) =>
      location.target === progress.target
        ? { ...location, directory: progress.destination, custom: !samePath(progress.destination, location.default), missing: false }
        : location,
    ),
  };
}

export default function OnboardingWindowApp() {
  const localProvider = useMemo(() => new LocalExecutionProvider(localProviderBridge), []);
  const [theme, setTheme] = useState<Theme>(initialTheme);
  const [transcriptionEngine, setTranscriptionEngine] = useState(DEFAULT_TRANSCRIPTION_ENGINE);
  const [sidecar, setSidecar] = useState<SidecarSnapshot | null>(null);
  const [pythonBootstrap, setPythonBootstrap] = useState<PythonBootstrapState | null>(null);
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [settings, setSettings] = useState<FineSubSettingsState | null>(null);
  const [runtimeProvision, setRuntimeProvision] = useState<RuntimeProvisionState | null>(null);
  const [provisionMessage, setProvisionMessage] = useState("");
  const [migration, setMigration] = useState<MigrationStatus | null>(null);
  const [migrationRetrying, setMigrationRetrying] = useState(false);

  const [storageStatus, setStorageStatus] = useState<StorageStatus | null>(null);
  const [storageProgress, setStorageProgress] = useState<RelocationProgress | null>(null);
  const [storageBusy, setStorageBusy] = useState(false);
  const [storageMessage, setStorageMessage] = useState("");

  const [cloudSession, setCloudSession] = useState<CloudSession | null>(null);
  const [loginKey, setLoginKey] = useState("");
  const [accountBusy, setAccountBusy] = useState(false);
  const [accountMessage, setAccountMessage] = useState("");

  const [keyDraft, setKeyDraft] = useState<Record<string, string>>({});
  const [keysBusy, setKeysBusy] = useState(false);
  const [keysMessage, setKeysMessage] = useState("");

  const preferencesHydrated = useRef(false);
  const pythonBootstrapStarted = useRef(false);
  const storageEventSeq = useRef(0);
  const storageStatusSeq = useRef(0);

  // 挂载完成后立刻通知 Go 宿主展示窗口（不可用 requestAnimationFrame：Windows 隐藏 WebView2 不会触发 rAF）
  useEffect(() => {
    void Events.Emit("onboarding:ready", true);
  }, []);

  useEffect(() => {
    void desktopPreferences.get().then((value) => {
      setTheme(value.homeTheme === "dark" ? "dark" : "light");
      if (value.transcriptionEngine) setTranscriptionEngine(value.transcriptionEngine);
    }).catch(() => undefined).finally(() => {
      preferencesHydrated.current = true;
    });
  }, []);

  useEffect(() => {
    applyTheme(theme);
    if (preferencesHydrated.current) void desktopPreferences.save({ homeTheme: theme }).catch(() => undefined);
  }, [theme]);

  const loadStorageStatus = useCallback(async (remeasure = false) => {
    const request = ++storageStatusSeq.current;
    try {
      const status = remeasure ? await storageLocations.refresh() : await storageLocations.status();
      if (request !== storageStatusSeq.current) return;
      setStorageStatus(status);
      setStorageProgress(status.progress);
    } catch {
      if (request === storageStatusSeq.current) setStorageStatus(null);
    }
  }, []);

  const refresh = useCallback(async () => {
    try {
      const [status, bootstrap] = await Promise.all([sidecarStatus(), fineSubRuntime.pythonStatus()]);
      setSidecar(status);
      setPythonBootstrap(bootstrap);
      if (!status.running) {
        setCapabilities(null);
        setRuntimeProvision(null);
        return;
      }
      const nextMigration = await dataMigration.status().catch(() => null);
      setMigration(nextMigration);
      if (nextMigration && nextMigration.dataOpen === false) {
        setCapabilities(null);
        setRuntimeProvision(null);
        return;
      }
      const [nextCapabilities, nextSettings, nextProvision] = await Promise.all([
        localProvider.capabilities(),
        fineSubSettings.read(),
        fineSubRuntime.status(),
      ]);
      setCapabilities(nextCapabilities);
      setSettings(nextSettings);
      setRuntimeProvision(nextProvision);
    } catch (value) {
      if (isMigratingError(value)) {
        setMigration(await dataMigration.status().catch(() => null));
      }
    }
  }, [localProvider]);

  const loadCloudSession = useCallback(async () => {
    try {
      const cached = await cloudAccount.session();
      const session = cached.authenticated ? await cloudAccount.refreshSession().catch(() => cached) : cached;
      setCloudSession(session);
    } catch {
      setCloudSession({ authenticated: false, running: 0, videos: [] });
    }
  }, []);

  useEffect(() => {
    void Promise.all([refresh(), loadStorageStatus(), loadCloudSession()]);
  }, [loadCloudSession, loadStorageStatus, refresh]);

  // 自动拉起隔离的 Python 3.12 启动器
  const installPython = useCallback(async () => {
    try {
      setPythonBootstrap(await fineSubRuntime.installPython());
    } catch {
      setPythonBootstrap(await fineSubRuntime.pythonStatus().catch(() => null));
    }
  }, []);

  useEffect(() => {
    if (sidecar?.running || pythonBootstrap?.state !== "missing" || !pythonBootstrap.supported || pythonBootstrapStarted.current) return;
    pythonBootstrapStarted.current = true;
    void installPython();
  }, [installPython, pythonBootstrap, sidecar?.running]);

  // 轮询 Python 启动器安装进度
  useEffect(() => {
    if (pythonBootstrap?.state !== "running") return;
    const timer = window.setInterval(() => {
      void fineSubRuntime.pythonStatus().then((status) => {
        setPythonBootstrap(status);
        if (status.state === "ready") void refresh();
      }).catch(() => undefined);
    }, 1000);
    return () => window.clearInterval(timer);
  }, [pythonBootstrap?.state, refresh]);

  // Sidecar 尚未就绪或 provision 尚未拉取时静默重试
  const runtimeProvisionMissing = runtimeProvision === null;
  useEffect(() => {
    if (pythonBootstrap?.state === "running") return;
    if (sidecar?.running && !runtimeProvisionMissing) return;
    const timer = window.setInterval(() => void refresh(), sidecarRetryIntervalMs);
    return () => window.clearInterval(timer);
  }, [pythonBootstrap?.state, refresh, runtimeProvisionMissing, sidecar?.running]);

  // 轮询运行环境下载进度
  useEffect(() => {
    if (runtimeProvision?.job.state !== "running") return;
    const timer = window.setInterval(() => {
      void fineSubRuntime.status().then((status) => {
        setRuntimeProvision(status);
        if (status.job.state !== "running") {
          void refresh();
          void loadStorageStatus(true);
        }
      }).catch(() => undefined);
    }, 1000);
    return () => window.clearInterval(timer);
  }, [loadStorageStatus, refresh, runtimeProvision?.job.state]);

  // 监听目录迁移进度事件
  useEffect(() => {
    return Events.On("storage:progress", (event) => {
      const progress = event.data as RelocationProgress | undefined;
      if (!progress) return;
      storageEventSeq.current += 1;
      setStorageProgress(progress);
      if (progress.stage === "completed") setStorageStatus((current) => withRelocatedDirectory(current, progress));
      if (!progress.active) void loadStorageStatus(true);
    });
  }, [loadStorageStatus]);

  // 轮询旧版数据迁移进度
  const migrationPanel = useMemo(() => migrationView(migration, { dismissed: false }), [migration]);
  const migrationPolling = migrationPanel.polling;
  useEffect(() => {
    if (!migrationPolling) return;
    let previous = migration;
    const timer = window.setInterval(() => {
      void dataMigration.status().then((next) => {
        setMigration(next);
        const opened = previous?.dataOpen === false && next.dataOpen !== false;
        const downloaded = previous?.state === "downloading" && next.state !== "downloading";
        previous = next;
        if (opened || downloaded) void refresh();
      }).catch(() => undefined);
    }, migrationPollIntervalMs);
    return () => window.clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [migrationPolling, refresh]);

  // 登录态变化时刷新设置里的 LLM 提供商列表（Nonoka Cloud 提供商）
  const cloudAuthenticated = cloudSession?.authenticated;
  useEffect(() => {
    if (cloudAuthenticated === undefined || !sidecar?.running) return;
    void fineSubSettings.read().then(setSettings).catch(() => undefined);
  }, [cloudAuthenticated, sidecar?.running]);

  const selectTranscriptionEngine = useCallback((engine: string) => {
    setTranscriptionEngine(engine);
    void desktopPreferences.save({ transcriptionEngine: engine }).catch(() => undefined);
  }, []);

  const installRuntime = useCallback(async (target: RuntimeInstallTarget) => {
    setProvisionMessage("");
    try {
      setRuntimeProvision(await fineSubRuntime.install(target));
    } catch (value) {
      setProvisionMessage(value instanceof Error ? value.message : String(value));
      await refresh();
    }
  }, [refresh]);

  const installLayerOffline = useCallback(async (layer: RuntimeLayerId) => {
    setProvisionMessage("");
    try {
      const picked = await fineSubRuntime.chooseOfflineArchive(layer);
      if (picked.cancelled || !picked.path) return;
      let latest = await fineSubRuntime.status();
      if (latest.job.state === "running") {
        setRuntimeProvision(await fineSubRuntime.cancel());
        const deadline = Date.now() + 8000;
        while (Date.now() < deadline) {
          await new Promise((resolve) => window.setTimeout(resolve, 150));
          latest = await fineSubRuntime.status();
          setRuntimeProvision(latest);
          if (latest.job.state !== "running") break;
        }
        if (latest.job.state === "running") {
          setProvisionMessage("正在停止当前的在线下载，请稍后再选择离线包安装。");
          return;
        }
      }
      setRuntimeProvision(await fineSubRuntime.installLayerOffline(layer, picked.path));
    } catch (value) {
      setProvisionMessage(value instanceof Error ? value.message : String(value));
      await refresh();
    }
  }, [refresh]);

  const cancelRuntimeInstall = useCallback(async () => {
    setProvisionMessage("");
    try {
      setRuntimeProvision(await fineSubRuntime.cancel());
    } catch (value) {
      setProvisionMessage(value instanceof Error ? value.message : String(value));
      await refresh();
    }
  }, [refresh]);

  const applyStorageAction = useCallback(async (action: () => Promise<RelocationProgress>) => {
    const seen = storageEventSeq.current;
    const progress = await action();
    if (storageEventSeq.current === seen) setStorageProgress(progress);
  }, []);

  const chooseStorage = useCallback(async (target: StorageTarget): Promise<StorageDestination | null> => {
    setStorageMessage("");
    try {
      return await storageLocations.choose(target);
    } catch (value) {
      setStorageMessage(value instanceof Error ? value.message : String(value));
      return null;
    }
  }, []);

  const relocateStorage = useCallback(async (target: StorageTarget, destination: string) => {
    setStorageBusy(true);
    setStorageMessage("");
    try {
      await applyStorageAction(() => storageLocations.relocate(target, destination));
    } catch (value) {
      setStorageMessage(value instanceof Error ? value.message : String(value));
    } finally {
      setStorageBusy(false);
    }
  }, [applyStorageAction]);

  const cancelStorage = useCallback(async () => {
    try {
      await applyStorageAction(() => storageLocations.cancel());
    } catch (value) {
      setStorageMessage(value instanceof Error ? value.message : String(value));
    }
  }, [applyStorageAction]);

  const login = useCallback(async () => {
    setAccountBusy(true);
    setAccountMessage("");
    try {
      const session = await cloudAccount.login(DEFAULT_CLOUD_BACKEND, loginKey);
      setCloudSession(session);
      setLoginKey("");
      setAccountMessage("登录成功，已连接 Nonoka Cloud。");
    } catch (value) {
      setAccountMessage(value instanceof Error ? value.message : String(value));
    } finally {
      setAccountBusy(false);
    }
  }, [loginKey]);

  const saveKeys = useCallback(async (updates: Record<string, string | null>, keyName: string) => {
    const payload = Object.fromEntries(
      Object.entries(updates).map(([name, value]) => [name, typeof value === "string" ? value.trim() : value]),
    );
    if (!Object.keys(payload).length) {
      setKeysMessage("请输入 Key 后再保存。");
      return;
    }
    setKeysBusy(true);
    setKeysMessage("");
    try {
      setSettings(await fineSubSettings.saveKeys(payload));
      setKeyDraft((current) => Object.fromEntries(Object.entries(current).filter(([name]) => !Object.hasOwn(payload, name))));
      setKeysMessage(`${keyName} 已保存。`);
      await refresh();
    } catch (value) {
      setKeysMessage(value instanceof Error ? value.message : String(value));
    } finally {
      setKeysBusy(false);
    }
  }, [refresh]);

  const retryMigration = useCallback(async () => {
    setMigrationRetrying(true);
    try {
      setMigration(await dataMigration.retry());
    } catch (value) {
      setMigration((current) => current && { ...current, error: value instanceof Error ? value.message : String(value) });
    } finally {
      setMigrationRetrying(false);
    }
  }, []);

  const completeOnboarding = useCallback(() => {
    void desktopPreferences.save({ onboardingCompleted: true }).catch(() => undefined).finally(() => {
      void desktopWindows.completeOnboarding();
    });
  }, []);

  const engines = useMemo(
    () => engineChoices(capabilities, { authenticated: cloudSession?.authenticated === true, remaining: cloudSession?.remaining ?? undefined }),
    [capabilities, cloudSession?.authenticated, cloudSession?.remaining],
  );

  return (
    <OnboardingPage
      theme={theme}
      onToggleTheme={() => setTheme((value) => (value === "dark" ? "light" : "dark"))}
      sidecar={sidecar}
      pythonBootstrap={pythonBootstrap}
      provision={runtimeProvision}
      provisionMessage={provisionMessage}
      onDismissProvisionMessage={() => setProvisionMessage("")}
      capabilities={capabilities}
      engines={engines}
      transcriptionEngine={transcriptionEngine}
      onSelectEngine={selectTranscriptionEngine}
      onInstallPython={installPython}
      onInstall={installRuntime}
      onInstallOffline={installLayerOffline}
      onCancelInstall={cancelRuntimeInstall}
      onRefresh={refresh}
      storage={storageStatus}
      storageProgress={storageProgress}
      storageBusy={storageBusy}
      storageMessage={storageMessage}
      onDismissStorageMessage={() => setStorageMessage("")}
      onChooseStorage={chooseStorage}
      onRelocateStorage={relocateStorage}
      onCancelStorage={cancelStorage}
      cloudSession={cloudSession}
      loginKey={loginKey}
      accountBusy={accountBusy}
      accountMessage={accountMessage}
      onLoginKeyChange={setLoginKey}
      onLogin={login}
      onDismissAccountMessage={() => setAccountMessage("")}
      settings={settings}
      drafts={keyDraft}
      keysBusy={keysBusy}
      keysMessage={keysMessage}
      setDrafts={setKeyDraft}
      onSaveKey={saveKeys}
      onDismissKeysMessage={() => setKeysMessage("")}
      migration={migration}
      migrationView={migrationPanel}
      migrationRetrying={migrationRetrying}
      onRetryMigration={() => void retryMigration()}
      onComplete={completeOnboarding}
    />
  );
}
