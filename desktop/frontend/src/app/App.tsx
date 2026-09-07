import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Events } from "@wailsio/runtime";
import type { Snapshot as SidecarSnapshot } from "../../bindings/github.com/Ricori/nonoka-x/desktop/internal/sidecar/models.js";
import { mediaLibrary } from "../bridge/library.ts";
import type { CacheStatus, ImportResult, MediaEntry } from "../bridge/library.ts";
import type { RelocationProgress, StorageDestination, StorageStatus, StorageTarget } from "../bridge/storage.ts";
import { storageLocations } from "../bridge/storage.ts";
import { cloudAccount, cloudLibraryOf, DEFAULT_CLOUD_BACKEND, hasCloudSubtitles } from "../bridge/cloud.ts";
import type { CloudEntry, CloudSession } from "../bridge/cloud.ts";
import { fineSubSettings } from "../bridge/settings.ts";
import type { FineSubSettingsState } from "../bridge/settings.ts";
import { fineSubRuntime } from "../bridge/runtime.ts";
import type { PythonBootstrapState, RuntimeInstallTarget, RuntimeProvisionState, RuntimeToolGroup } from "../bridge/runtime.ts";
import { desktopPreferences } from "../bridge/preferences.ts";
import { desktopTaskHistory } from "../bridge/taskHistory.ts";
import { desktopPlugins, mountedTools } from "../bridge/plugins.ts";
import type { InstalledPlugin, MountedPluginTool } from "../bridge/plugins.ts";
import { desktopWindows } from "../bridge/windows.ts";
import { localProviderBridge, sidecarStatus } from "../bridge/wails.ts";
import { PipelineController } from "../home/pipelineController.ts";
import { CloudExecutionProvider } from "../providers/cloudProvider.ts";
import type { PipelineState } from "../home/pipelineController.ts";
import { LocalExecutionProvider } from "../providers/localProvider.ts";
import type { Capabilities, TaskAxis, TaskRequest, TaskSnapshot } from "../providers/types.ts";
import { documents } from "../bridge/documents.ts";
import { MediaDialog } from "../components/MediaDialog.tsx";
import type { NoticeTone } from "../components/Notice.tsx";
import { TranscriptionDialog } from "../components/TranscriptionDialog.tsx";
import { UpdateButton, UpdateOverlay, useSelfUpdate } from "../components/UpdateCenter.tsx";
import { WindowDropOverlay } from "../components/WindowDropOverlay.tsx";
import { AccountPage } from "../pages/AccountPage.tsx";
import { AboutPage } from "../pages/AboutPage.tsx";
import { AdminKeysPage } from "../pages/AdminKeysPage.tsx";
import { RuntimePage } from "../pages/RuntimePage.tsx";
import { SettingsPage } from "../pages/SettingsPage.tsx";
import { TasksPage } from "../pages/TasksPage.tsx";
import { LibraryPage } from "../pages/LibraryPage.tsx";
import { PluginManagerPage } from "../plugins/PluginManagerPage.tsx";
import { PluginPageHost } from "../plugins/PluginPageHost.tsx";
import { parseTaskHistory } from "./format.ts";
import { reconcileTaskHistory } from "./taskHistory.ts";
import { applyTheme, initialTheme } from "./theme.ts";
import type { DialogState, ExecutionMode, LibraryFilter, LibraryItem, LoadState, NavigationSection, Section, SortMode, TaskHistoryEntry, Theme, ViewMode } from "./types.ts";
import { activeStates, taskHistoryLimit } from "./types.ts";

const taskPollIntervalMs = 10_000;
// Reconnect attempts while the local sidecar is not answering.
const sidecarRetryIntervalMs = 4_000;

/** Page notices carry their own tone so a completed action does not read as a
    warning. The helpers are module level, keeping setState callers dependency free. */
type PageNotice = { text: string; tone: NoticeTone };
const noNotice: PageNotice = { text: "", tone: "warn" };
const warnNotice = (text: string): PageNotice => ({ text, tone: "warn" });
const okNotice = (text: string): PageNotice => ({ text, tone: "success" });

function NavIcon({ kind }: { kind: NavigationSection }) {
  const paths = {
    library: "M4 6.5h16M6 3h12a2 2 0 0 1 2 2v14H4V5a2 2 0 0 1 2-2Zm3 7h6m-6 4h4",
    tasks: "M5 4h14v16H5zM8 8h8m-8 4h8m-8 4h5",
    plugins: "M8 3v4m8-4v4M6 7h12v4a6 6 0 0 1-5 5.9V21h-2v-4.1A6 6 0 0 1 6 11V7Z",
    runtime: "M4 5h16v14H4zM8 9h3m2 0h3m-8 4h8m-8 3h5",
    adminKeys: "M8.5 14.5 14 9m-1.5-2.5a3.5 3.5 0 1 1 5 5l-1 1-2-2-2 2-2-2-2 2-2.5-2.5 1-1Z",
    settings:
      "M12 8.5a3.5 3.5 0 1 0 0 7 3.5 3.5 0 0 0 0-7Zm0-5v2m0 13v2m8.5-8.5h-2m-13 0h-2m15-6.5-1.4 1.4M6.9 17.1l-1.4 1.4m13 0-1.4-1.4M6.9 6.9 5.5 5.5",
    about: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Zm0-10v6m0-10v.01",
  } as const;
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d={paths[kind]} />
    </svg>
  );
}

function PluginToolIcon() {
  return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 4h10v5l3 3-3 3v5H7v-5l-3-3 3-3V4Zm3 4h4m-4 4h4m-4 4h4" /></svg>;
}

export default function App() {
  const [executionMode, setExecutionMode] = useState<ExecutionMode>("local");
  const localProvider = useMemo(() => new LocalExecutionProvider(localProviderBridge), []);
  const cloudProvider = useMemo(() => new CloudExecutionProvider(cloudAccount), []);
  const localController = useMemo(() => new PipelineController(localProvider), [localProvider]);
  const cloudController = useMemo(() => new PipelineController(cloudProvider), [cloudProvider]);
  const controller = executionMode === "local" ? localController : cloudController;
  const [section, setSection] = useState<Section>("library");
  const [plugins, setPlugins] = useState<InstalledPlugin[]>([]);
  const [activePluginTool, setActivePluginTool] = useState<{ pluginId: string; toolId: string } | null>(null);
  const [pluginBusy, setPluginBusy] = useState("");
  const [pluginMessage, setPluginMessage] = useState("");
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [theme, setTheme] = useState<Theme>(initialTheme);
  const [libraryFilter, setLibraryFilter] = useState<LibraryFilter>("all");
  const [sortMode, setSortMode] = useState<SortMode>("recent");
  const [viewMode, setViewMode] = useState<ViewMode>("grid");
  const [query, setQuery] = useState("");
  const [loadState, setLoadState] = useState<LoadState>("loading");
  const [sidecar, setSidecar] = useState<SidecarSnapshot | null>(null);
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [message, setMessage] = useState("");
  const [media, setMedia] = useState<MediaEntry[]>([]);
  const [thumbnails, setThumbnails] = useState<Record<string, string>>({});
  const [cloudThumbnails, setCloudThumbnails] = useState<Record<string, string>>({});
  const [adoptFailed, setAdoptFailed] = useState<ReadonlySet<string>>(() => new Set());
  const [manualAdoptingMedia, setManualAdoptingMedia] = useState<ReadonlySet<string>>(() => new Set());
  const [adoptingCloud, setAdoptingCloud] = useState<ReadonlySet<string>>(() => new Set());
  const [reclaimingMedia, setReclaimingMedia] = useState<ReadonlySet<string>>(() => new Set());
  const [libraryBusy, setLibraryBusy] = useState(false);
  const [libraryMessage, setLibraryMessage] = useState<PageNotice>(noNotice);
  const [cacheStatus, setCacheStatus] = useState<CacheStatus | null>(null);
  const [cacheBusy, setCacheBusy] = useState(false);
  const [cacheMessage, setCacheMessage] = useState("");
  const [storageStatus, setStorageStatus] = useState<StorageStatus | null>(null);
  const [storageProgress, setStorageProgress] = useState<RelocationProgress | null>(null);
  const [storageBusy, setStorageBusy] = useState(false);
  const [storageMessage, setStorageMessage] = useState("");
  const [pipeline, setPipeline] = useState<PipelineState>(controller.current() as PipelineState);
  const [taskHistory, setTaskHistory] = useState<TaskHistoryEntry[]>([]);
  const [taskHistoryBusy, setTaskHistoryBusy] = useState(false);
  const [taskHistoryMessage, setTaskHistoryMessage] = useState<PageNotice>(noNotice);
  const [settings, setSettings] = useState<FineSubSettingsState | null>(null);
  const [runtimeProvision, setRuntimeProvision] = useState<RuntimeProvisionState | null>(null);
  // Failures of the install/cancel/remove actions used to land in `message`,
  // which the runtime page only renders while capabilities are missing — so a
  // refused removal looked like a page that simply did not react.
  const [provisionMessage, setProvisionMessage] = useState("");
  const [pythonBootstrap, setPythonBootstrap] = useState<PythonBootstrapState | null>(null);
  const [keyDraft, setKeyDraft] = useState<Record<string, string>>({});
  const [keysBusy, setKeysBusy] = useState(false);
  const [keysMessage, setKeysMessage] = useState("");
  const [cloudSession, setCloudSession] = useState<CloudSession | null>(null);
  const [cloudMedia, setCloudMedia] = useState<CloudEntry[]>([]);
  const [cloudCapabilities, setCloudCapabilities] = useState<Capabilities | null>(null);
  const [cloudLoading, setCloudLoading] = useState(true);
  const [loginKey, setLoginKey] = useState("");
  const [accountBusy, setAccountBusy] = useState(false);
  const [accountMessage, setAccountMessage] = useState("");
  const [activeMedia, setActiveMedia] = useState<MediaEntry | null>(null);
  const [dialog, setDialog] = useState<DialogState | null>(null);
  const [dialogBusy, setDialogBusy] = useState(false);
  const [transcriptionMedia, setTranscriptionMedia] = useState<MediaEntry | null>(null);
  const [transcriptionBusy, setTranscriptionBusy] = useState(false);
  const [transcriptionError, setTranscriptionError] = useState("");
  const selfUpdate = useSelfUpdate();
  const cloudThumbnailsTried = useRef(new Set<string>());
  const adoptedCloudEntries = useRef(new Set<string>());
  const syncedTasks = useRef(new Set<string>());
  const reconciledLocalTasks = useRef(new Set<string>());
  const reconciledCloudTasks = useRef(new Set<string>());
  const observedActiveCloudTasks = useRef(new Set<string>());
  const taskHistoryHydrated = useRef(false);
  const preferencesHydrated = useRef(false);
  // History persists to its own file, so it needs a hydration gate of its own:
  // saving before the stored history has loaded would overwrite it with [].
  const taskHistoryStored = useRef(false);
  const taskHistoryRef = useRef(taskHistory);
  const pythonBootstrapStarted = useRef(false);

  const refreshPlugins = useCallback(async () => {
    setPlugins(await desktopPlugins.list());
  }, []);

  useEffect(() => {
    void refreshPlugins().catch(() => undefined);
    return Events.On("plugins:changed", () => { void refreshPlugins().catch(() => undefined); });
  }, [refreshPlugins]);

  const pluginTools = useMemo(() => mountedTools(plugins), [plugins]);
  const activeMountedPlugin = useMemo<MountedPluginTool | null>(() => {
    if (!activePluginTool) return null;
    return pluginTools.find((item) => item.pluginId === activePluginTool.pluginId && item.tool.id === activePluginTool.toolId) ?? null;
  }, [activePluginTool, pluginTools]);

  useEffect(() => {
    if (section === "plugin" && !activeMountedPlugin) {
      setActivePluginTool(null);
      setSection("plugins");
    }
  }, [activeMountedPlugin, section]);

  const openPluginTool = useCallback((pluginId: string, toolId: string) => {
    setActivePluginTool({ pluginId, toolId });
    setSection("plugin");
  }, []);

  const installPlugin = useCallback(async () => {
    setPluginBusy("install");
    setPluginMessage("");
    try {
      const installed = await desktopPlugins.install();
      await refreshPlugins();
      if (installed.id) setPluginMessage(`已安装插件“${installed.name}”`);
    } catch (value) {
      setPluginMessage(`安装失败：${value instanceof Error ? value.message : String(value)}`);
    } finally {
      setPluginBusy("");
    }
  }, [refreshPlugins]);

  const togglePlugin = useCallback(async (plugin: InstalledPlugin) => {
    setPluginBusy(plugin.id);
    setPluginMessage("");
    try {
      await desktopPlugins.setEnabled(plugin.id, !plugin.enabled);
      await refreshPlugins();
      setPluginMessage(`${plugin.enabled ? "已停用" : "已启用"}插件“${plugin.name}”`);
    } catch (value) {
      setPluginMessage(`操作失败：${value instanceof Error ? value.message : String(value)}`);
    } finally {
      setPluginBusy("");
    }
  }, [refreshPlugins]);

  const uninstallPlugin = useCallback(async (plugin: InstalledPlugin, removeData: boolean) => {
    setPluginBusy(plugin.id);
    setPluginMessage("");
    try {
      await desktopPlugins.uninstall(plugin.id, removeData);
      await refreshPlugins();
      setPluginMessage(`已卸载插件“${plugin.name}”${removeData ? "并删除其数据" : "，插件数据已保留"}`);
    } catch (value) {
      setPluginMessage(`卸载失败：${value instanceof Error ? value.message : String(value)}`);
    } finally {
      setPluginBusy("");
    }
  }, [refreshPlugins]);

  useEffect(() => {
    taskHistoryRef.current = taskHistory;
    if (taskHistoryStored.current) void desktopTaskHistory.save(taskHistory.slice(0, taskHistoryLimit)).catch(() => undefined);
  }, [taskHistory]);

  useEffect(() => {
    void desktopPreferences.get().then((value) => {
      setTheme(value.homeTheme === "dark" ? "dark" : "light");
      setSidebarCollapsed(value.sidebarCollapsed);
      setViewMode(value.libraryView === "list" ? "list" : "grid");
    }).catch(() => undefined).finally(() => {
      preferencesHydrated.current = true;
    });
  }, []);

  useEffect(() => {
    void desktopTaskHistory.get().then((value) => {
      setTaskHistory(parseTaskHistory(value));
    }).catch(() => undefined).finally(() => {
      taskHistoryStored.current = true;
    });
  }, []);

  useEffect(() => {
    applyTheme(theme);
    if (preferencesHydrated.current) void desktopPreferences.save({ homeTheme: theme }).catch(() => undefined);
  }, [theme]);

  useEffect(() => {
    if (preferencesHydrated.current) void desktopPreferences.save({ sidebarCollapsed }).catch(() => undefined);
  }, [sidebarCollapsed]);

  useEffect(() => {
    if (preferencesHydrated.current) void desktopPreferences.save({ libraryView: viewMode }).catch(() => undefined);
  }, [viewMode]);

  // `silent` keeps the automatic reconnect polling from flashing the loading
  // state on every tick; explicit user refreshes still show their progress.
  const refresh = useCallback(async ({ silent = false }: { silent?: boolean } = {}) => {
    if (!silent) {
      setLoadState("loading");
      setMessage("");
    }
    try {
      const [status, bootstrap] = await Promise.all([sidecarStatus(), fineSubRuntime.pythonStatus()]);
      setSidecar(status);
      setPythonBootstrap(bootstrap);
      if (!status.running) {
        setCapabilities(null);
        // Without this the panel keeps offering install buttons against a
        // sidecar that is no longer answering.
        setRuntimeProvision(null);
        setMessage(bootstrap.state === "running" ? bootstrap.message : status.error || bootstrap.message || "本地执行服务尚未启动");
        setLoadState("error");
        return;
      }
      const [nextCapabilities, nextSettings, nextRuntimeProvision] = await Promise.all([
        localProvider.capabilities(),
        fineSubSettings.read(),
        fineSubRuntime.status(),
      ]);
      setCapabilities(nextCapabilities);
      setSettings(nextSettings);
      setRuntimeProvision(nextRuntimeProvision);
      setLoadState("ready");
    } catch (value) {
      setLoadState("error");
      const detail = value instanceof Error ? value.message : String(value ?? "");
      setMessage(detail || "无法连接 Wails 桌面桥接");
    }
  }, [localProvider]);

  const hydrateThumbnails = useCallback(async (entries: MediaEntry[]) => {
    const pairs = await Promise.all(
      entries
        .filter((entry) => entry.thumbnailAvailable)
        .map(async (entry) => {
          try {
            return [entry.id, await mediaLibrary.thumbnail(entry.id)] as const;
          } catch {
            return null;
          }
        }),
    );
    setThumbnails(Object.fromEntries(pairs.filter((pair) => pair !== null)));
  }, []);

  // Cloud-only entries have no local file to draw a frame from, so their cover
  // comes from the bucket the task uploaded it to. Each id is attempted once
  // per session: an entry started by a desktop that had no cover to send will
  // never grow one, and retrying on every library refresh only costs requests.
  useEffect(() => {
    const pending = cloudMedia.filter((entry) => entry.thumbnailAvailable && !cloudThumbnailsTried.current.has(entry.id));
    if (pending.length === 0) return;
    pending.forEach((entry) => cloudThumbnailsTried.current.add(entry.id));
    // Not cancelled on re-run either: the ids are already marked as tried, so
    // a teardown mid-fetch would lose those covers for the rest of the session.
    void Promise.all(pending.map(async (entry) => {
      try {
        return [entry.id, await cloudAccount.thumbnail(entry.id)] as const;
      } catch {
        return null;
      }
    })).then((pairs) => {
      const resolved = pairs.filter((pair) => pair !== null);
      if (resolved.length === 0) return;
      setCloudThumbnails((current) => ({ ...current, ...Object.fromEntries(resolved) }));
    });
  }, [cloudMedia]);

  const loadLibrary = useCallback(async () => {
    try {
      const entries = await mediaLibrary.list();
      setMedia(entries);
      await hydrateThumbnails(entries);
    } catch (value) {
      const detail = value instanceof Error ? value.message : String(value ?? "");
      setLibraryMessage(warnNotice(detail || "无法连接本地媒体库"));
    }
  }, [hydrateThumbnails]);

  // A cloud entry only merges into a local card by fingerprint, which makes it
  // look synced while the subtitles still live entirely in the cloud: the card
  // says 字幕已同步 and the button still offers to transcribe the media again.
  // This is the set of media that answers to a finished cloud entry and has no
  // local document yet -- derived during render rather than recorded when the
  // download starts, because a card that renders before the effect has run
  // would offer 开始任务 for that frame and spend a cloud task if clicked.
  const pendingAdoptions = useMemo(() => {
    if (!cloudSession?.authenticated) return [];
    return media
      // The media this app is transcribing right now is excluded: its own
      // pipeline projects the result the moment the task finishes, and both
      // paths would otherwise download the same artifacts at once.
      .filter((entry) => entry.available && !entry.documentAvailable && !entry.documentRemoved && entry.fingerprint && entry.id !== activeMedia?.id)
      .flatMap((entry) => {
        const remote = cloudMedia.find((item) => hasCloudSubtitles(item) && item.fingerprint === entry.fingerprint);
        return remote ? [{ key: `${remote.id}:${entry.id}`, localID: entry.id, videoID: remote.id }] : [];
      })
      .filter((pair) => !adoptFailed.has(pair.key));
  }, [activeMedia?.id, adoptFailed, cloudMedia, cloudSession?.authenticated, media]);
  const adoptingMedia = useMemo(() => new Set([
    ...pendingAdoptions.map((pair) => pair.localID),
    ...manualAdoptingMedia,
    ...reclaimingMedia,
  ]), [manualAdoptingMedia, pendingAdoptions, reclaimingMedia]);

  // Pull the finished text down, once per pair, whether the media arrived
  // through 关联本地视频, a plain import or a relink. Deliberately not
  // cancelled on re-run: the adoption changes the very library the set above
  // is derived from, so this re-runs while the download is still in flight,
  // and tearing the run down there dropped the refresh that makes the new
  // document visible. The tried-set is what keeps the work from repeating.
  useEffect(() => {
    const fresh = pendingAdoptions.filter((pair) => !adoptedCloudEntries.current.has(pair.key));
    if (fresh.length === 0) return;
    fresh.forEach((pair) => adoptedCloudEntries.current.add(pair.key));
    void (async () => {
      const adopted = new Set<string>();
      const failed = new Set<string>();
      for (const pair of fresh) {
        // Sequential on purpose: each projection round-trips through the local
        // provider.
        try {
          await cloudAccount.adoptLibraryEntry(pair.videoID, pair.localID);
          adopted.add(pair.localID);
        } catch {
          // Left for the user to transcribe locally: recording the failure is
          // what puts 开始任务 back on the card.
          failed.add(pair.key);
        }
      }
      if (failed.size > 0) setAdoptFailed((current) => new Set([...current, ...failed]));
      if (adopted.size === 0) return;
      setLibraryMessage(okNotice(`已从云端取回 ${adopted.size} 个视频的字幕，可以直接编辑。`));
      await loadLibrary();
      // The document exists -- this code is what wrote it. Asserting that on
      // top of the refresh keeps a library:changed snapshot taken before the
      // projection from putting these cards back to 取回字幕中.
      setMedia((current) => current.map((entry) => adopted.has(entry.id) ? { ...entry, documentAvailable: true } : entry));
    })();
  }, [loadLibrary, pendingAdoptions]);

  // Editing a cloud entry whose video never reached this machine. The
  // subtitles are what the user came for; the library keeps a placeholder for
  // the fingerprint so the document has an owner, and 关联视频 later fills that
  // same entry in.
  const editCloudEntry = useCallback(async (entry: CloudEntry) => {
    setLibraryMessage(noNotice);
    setAdoptingCloud((current) => new Set([...current, entry.id]));
    try {
      const localID = await cloudAccount.adoptCloudEntry(entry.id);
      await loadLibrary();
      await desktopWindows.openEditor(localID);
    } catch (value) {
      const detail = value instanceof Error ? value.message : String(value ?? "");
      setLibraryMessage(warnNotice(detail || "无法取回云端字幕"));
      await loadLibrary();
    } finally {
      setAdoptingCloud((current) => new Set([...current].filter((id) => id !== entry.id)));
    }
  }, [loadLibrary]);

  const adoptLocalSubtitles = useCallback(async (entry: MediaEntry, remote: CloudEntry) => {
    const key = `${remote.id}:${entry.id}`;
    adoptedCloudEntries.current.add(key);
    setManualAdoptingMedia((current) => new Set([...current, entry.id]));
    setLibraryMessage(noNotice);
    try {
      await cloudAccount.adoptLibraryEntry(remote.id, entry.id);
      setAdoptFailed((current) => new Set([...current].filter((item) => item !== key)));
      await loadLibrary();
      setMedia((current) => current.map((item) => item.id === entry.id ? { ...item, documentAvailable: true } : item));
      setLibraryMessage(okNotice(`已从云端取回“${entry.title}”的字幕。`));
    } catch (value) {
      setAdoptFailed((current) => new Set([...current, key]));
      setLibraryMessage(warnNotice(value instanceof Error ? value.message : String(value)));
    } finally {
      setManualAdoptingMedia((current) => new Set([...current].filter((id) => id !== entry.id)));
    }
  }, [loadLibrary]);

  const openEditor = useCallback(async (entry: MediaEntry) => {
    setLibraryMessage(noNotice);
    try {
      await desktopWindows.openEditor(entry.id);
    } catch (value) {
      setLibraryMessage(warnNotice(value instanceof Error ? value.message : String(value)));
    }
  }, []);

  const loadCacheStatus = useCallback(async () => {
    try {
      setCacheStatus(await mediaLibrary.cacheStatus());
    } catch {
      setCacheStatus(null);
    }
  }, []);

  const loadStorageStatus = useCallback(async (remeasure = false) => {
    try {
      const status = remeasure ? await storageLocations.refresh() : await storageLocations.status();
      setStorageStatus(status);
      setStorageProgress(status.progress);
    } catch {
      setStorageStatus(null);
    }
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
      setStorageProgress(await storageLocations.relocate(target, destination));
    } catch (value) {
      setStorageMessage(value instanceof Error ? value.message : String(value));
    } finally {
      setStorageBusy(false);
    }
  }, []);

  const resetStorage = useCallback(async (target: StorageTarget) => {
    setStorageBusy(true);
    setStorageMessage("");
    try {
      setStorageProgress(await storageLocations.reset(target));
    } catch (value) {
      setStorageMessage(value instanceof Error ? value.message : String(value));
    } finally {
      setStorageBusy(false);
    }
  }, []);

  const cancelStorage = useCallback(async () => {
    try {
      setStorageProgress(await storageLocations.cancel());
    } catch (value) {
      setStorageMessage(value instanceof Error ? value.message : String(value));
    }
  }, []);

  const loadCloud = useCallback(async () => {
    setCloudLoading(true);
    try {
      const cached = await cloudAccount.session();
      const session = cached.authenticated ? await cloudAccount.refreshSession() : cached;
      setCloudSession(session);
      if (!session.authenticated) {
        setCloudMedia([]);
        setCloudCapabilities(null);
        return;
      }
      setCloudMedia(await cloudLibraryOf(session));
      setCloudCapabilities(await cloudProvider.capabilities().catch(() => null));
      if (session.synced || session.syncFailed || session.syncError) {
        setAccountMessage(
          session.syncError
            ? `已登录，但自动同步检查失败：${session.syncError}`
            : `已补同步 ${session.synced ?? 0} 个本地字幕${session.syncFailed ? `，${session.syncFailed} 个失败` : ""}。`,
        );
      }
    } catch (value) {
      setCloudSession({ authenticated: false, running: 0, videos: [] });
      setCloudMedia([]);
      setCloudCapabilities(null);
      const detail = value instanceof Error ? value.message : String(value ?? "");
      if (detail && !detail.includes("Wails")) setAccountMessage(detail);
    } finally {
      setCloudLoading(false);
    }
  }, [cloudProvider]);

  const acceptImport = useCallback(async (result: ImportResult) => {
    const failures = result.failed ?? [];
    if (failures.length > 0) {
      setLibraryMessage(warnNotice(failures.map((failure) => `${failure.name}: ${failure.message}`).join("；")));
    } else if ((result.added ?? []).length > 0) {
      setLibraryMessage(okNotice(`已导入 ${(result.added ?? []).length} 个本地媒体`));
    }
    await loadLibrary();
  }, [loadLibrary]);

  const mediaDependencyMissing = capabilities?.runtime?.stages
    ?.find((stage) => stage.id === "media")
    ?.issues.some((issue) => issue.code === "missing_ffmpeg") === true;

  const importMedia = useCallback(async (paths?: string[]) => {
    if (mediaDependencyMissing) {
      setSection("runtime");
      setMessage("导入视频需要 FFmpeg 与 FFprobe，正在准备下载到 Nonoka Sub X 缓存目录。");
      if (runtimeProvision?.media_supported && runtimeProvision.job.state !== "running") {
        try {
          setRuntimeProvision(await fineSubRuntime.install("media"));
        } catch (value) {
          setMessage(value instanceof Error ? value.message : String(value));
        }
      }
      return;
    }
    setLibraryBusy(true);
    setLibraryMessage(noNotice);
    try {
      const result = paths ? await mediaLibrary.importPaths(paths) : await mediaLibrary.pickAndImport();
      await acceptImport(result);
    } catch (value) {
      const detail = value instanceof Error ? value.message : String(value ?? "");
      setLibraryMessage(warnNotice(detail || "媒体导入失败"));
    } finally {
      setLibraryBusy(false);
    }
  }, [acceptImport, mediaDependencyMissing, runtimeProvision]);

  useEffect(() => {
    void Promise.all([refresh(), loadLibrary(), loadCloud(), loadCacheStatus(), loadStorageStatus()]);
  }, [loadCacheStatus, loadCloud, loadLibrary, loadStorageStatus, refresh]);

  useEffect(() => controller.subscribe((state) => setPipeline({ ...state })), [controller]);

  useEffect(() => {
    if (runtimeProvision?.job.state !== "running") return;
    const timer = window.setInterval(() => {
      void fineSubRuntime.status().then((status) => {
        setRuntimeProvision(status);
        if (status.job.state !== "running") void refresh();
      }).catch(() => undefined);
    }, 1000);
    return () => window.clearInterval(timer);
  }, [refresh, runtimeProvision?.job.state]);

  useEffect(() => {
    if (!pipeline.snapshot || !activeStates.has(pipeline.snapshot.state)) return;
    const timer = window.setInterval(() => void controller.refresh(), taskPollIntervalMs);
    return () => window.clearInterval(timer);
  }, [controller, pipeline.snapshot]);

  useEffect(() => {
    if (runtimeProvision?.job.state !== "completed") return;
    void loadStorageStatus(true);
  }, [loadStorageStatus, runtimeProvision?.job.state]);

  useEffect(() => {
    // 迁移的进度和结束都从 Go 侧推过来。结束时重新拉一次状态，让路径、
    // 占用和剩余空间跟着换盘一起更新。
    return Events.On("storage:progress", (event) => {
      const progress = event.data as RelocationProgress | undefined;
      if (!progress) return;
      setStorageProgress(progress);
      if (!progress.active) {
        void loadStorageStatus(true);
        void loadCacheStatus();
      }
    });
  }, [loadCacheStatus, loadStorageStatus]);

  useEffect(() => {
    const offChanged = Events.On("library:changed", (event) => {
      const entries = Array.isArray(event.data) ? (event.data as MediaEntry[]) : [];
      setMedia(entries);
      void hydrateThumbnails(entries);
    });
    return () => {
      offChanged();
    };
  }, [hydrateThumbnails]);

  useEffect(() => Events.On("home:refresh", () => {
    void loadLibrary();
    void desktopPreferences.get().then((value) => {
      setTheme(value.homeTheme === "dark" ? "dark" : "light");
    }).catch(() => undefined);
  }), [loadLibrary]);

  const handleDroppedFiles = useCallback((paths: string[], ignored: number) => {
    setSection("library");
    const warning = ignored > 0 ? `已忽略 ${ignored} 个非视频文件；支持 MP4 / M4V / MOV / MKV / WebM。` : "";
    if (paths.length > 0) {
      void importMedia(paths).then(() => {
        if (warning) setLibraryMessage((current) => warnNotice(current.text ? `${current.text}；${warning}` : warning));
      });
    } else if (warning) {
      setLibraryMessage(warnNotice(warning));
    }
  }, [importMedia]);

  const rememberTask = useCallback((snapshot: TaskSnapshot, entry: MediaEntry) => {
    setTaskHistory((current) => [{
      taskId: snapshot.task_id,
      provider: snapshot.provider,
      mediaId: entry.id,
      title: entry.title,
      snapshot,
    }, ...current.filter((item) => item.taskId !== snapshot.task_id)].slice(0, taskHistoryLimit));
  }, []);

  const refreshTaskHistory = useCallback(async () => {
    setTaskHistoryBusy(true);
    setTaskHistoryMessage(noNotice);
    try {
      const current = taskHistoryRef.current;
      const refreshed = await Promise.all(current.map(async (item) => {
        if (item.provider === "cloud" && !cloudSession?.authenticated) return item;
        try {
          const taskProvider = item.provider === "local" ? localProvider : cloudProvider;
          const snap = await taskProvider.status(item.taskId);
          return { ...item, snapshot: { ...snap, started_at: snap.started_at || item.snapshot.started_at } };
        } catch {
          return item;
        }
      }));
      // History is local-only. Providers may update snapshots for rows already
      // saved here, but their task listings must never repopulate cleared rows.
      // Use commit-time state so an in-flight refresh cannot undo "clear".
      setTaskHistory((currentHistory) => reconcileTaskHistory(currentHistory, refreshed));
    } catch (value) {
      setTaskHistoryMessage(warnNotice(value instanceof Error ? value.message : String(value)));
    } finally {
      setTaskHistoryBusy(false);
    }
  }, [cloudProvider, cloudSession?.authenticated, localProvider]);

  const refreshActiveTaskHistory = useCallback(async () => {
    const active = taskHistoryRef.current.filter((item) => activeStates.has(item.snapshot.state));
    if (active.length === 0) return;
    const updates = await Promise.all(active.map(async (item) => {
      if (item.provider === "cloud" && !cloudSession?.authenticated) return null;
      try {
        const taskProvider = item.provider === "local" ? localProvider : cloudProvider;
        return await taskProvider.status(item.taskId);
      } catch {
        return null;
      }
    }));
    if (!updates.some((snapshot) => snapshot !== null)) return;
    const byTask = new Map(active.map((item, index) => [item.taskId, updates[index]]));
    setTaskHistory((current) => current.map((item) => {
      const snapshot = byTask.get(item.taskId);
      return snapshot ? { ...item, snapshot: { ...snapshot, started_at: snapshot.started_at || item.snapshot.started_at } } : item;
    }));
  }, [cloudProvider, cloudSession?.authenticated, localProvider]);

  useEffect(() => {
    if (taskHistoryHydrated.current || loadState === "loading" || taskHistory.length === 0) return;
    taskHistoryHydrated.current = true;
    void refreshTaskHistory();
  }, [loadState, refreshTaskHistory, taskHistory.length]);

  const hasActiveHistory = taskHistory.some((item) => activeStates.has(item.snapshot.state));
  useEffect(() => {
    if (!hasActiveHistory) return;
    void refreshActiveTaskHistory();
    const timer = window.setInterval(
      () => void refreshActiveTaskHistory(),
      taskPollIntervalMs,
    );
    return () => window.clearInterval(timer);
  }, [hasActiveHistory, refreshActiveTaskHistory]);

  // Session.running and cloud library entries are snapshots loaded at login;
  // task polling does not mutate either. Reconcile them as soon as one of this
  // desktop's cloud tasks leaves its active state, otherwise the global badge
  // stays "running" and the stale library row keeps cloud deletion disabled.
  useEffect(() => {
    for (const item of taskHistory) {
      if (item.provider === "cloud" && activeStates.has(item.snapshot.state)) {
        observedActiveCloudTasks.current.add(item.taskId);
      }
    }
    const settled = taskHistory.filter((item) =>
      item.provider === "cloud"
      && !activeStates.has(item.snapshot.state)
      && observedActiveCloudTasks.current.has(item.taskId));
    if (settled.length === 0) return;
    settled.forEach((item) => observedActiveCloudTasks.current.delete(item.taskId));

    // Apply the authoritative task snapshots immediately so the UI unlocks
    // without another network round trip, then refresh both cloud summaries to
    // pick up exact account counts, artifact flags, and timestamps.
    const states = new Map(settled.map((item) => [item.taskId, item.snapshot.state]));
    setCloudSession((current) => current
      ? { ...current, running: Math.max(0, current.running - settled.length) }
      : current);
    setCloudMedia((current) => current.map((entry) => {
      const state = states.get(entry.id);
      return state ? { ...entry, status: state } : entry;
    }));
    if (!cloudSession?.authenticated) return;
    void cloudAccount.refreshSession().then(async (session) => {
      setCloudSession(session);
      setCloudMedia(await cloudLibraryOf(session));
    }).catch(() => undefined);
  }, [cloudSession?.authenticated, taskHistory]);

  const actOnHistoryTask = useCallback(async (item: TaskHistoryEntry, action: "cancel" | "resume") => {
    const taskProvider = item.provider === "local" ? localProvider : cloudProvider;
    setTaskHistoryMessage(noNotice);
    try {
      const isCurrent = pipeline.snapshot?.task_id === item.taskId;
      // "继续" covers three states, and the local provider accepts only one of
      // them through resume: a shutdown leaves a task interrupted, while a
      // cancelled or failed one has to be retried. Both reuse the engine's
      // checkpoints, so the button means the same thing to the user either way.
      const restart = item.snapshot.state === "interrupted" ? "resume" : "retry";
      const nowIso = new Date().toISOString();
      if (action === "resume") {
        setTaskHistory((current) => current.map((record) =>
          record.taskId === item.taskId
            ? {
                ...record,
                snapshot: {
                  ...record.snapshot,
                  state: "queued",
                  stage: "queued",
                  started_at: nowIso,
                  error: null,
                  progress: null,
                },
              }
            : record,
        ));
      }
      const rawSnapshot = isCurrent
        ? action === "cancel"
          ? await controller.cancel()
          : restart === "resume" ? await controller.resume() : await controller.retry()
        : action === "cancel"
          ? await taskProvider.cancel(item.taskId)
          : restart === "resume" ? await taskProvider.resume(item.taskId) : await taskProvider.retry(item.taskId);
      if (!rawSnapshot) return;
      const snapshot: TaskSnapshot = action === "resume"
        ? { ...rawSnapshot, started_at: rawSnapshot.started_at || nowIso }
        : rawSnapshot;
      setTaskHistory((current) => current.map((record) => record.taskId === item.taskId ? { ...record, snapshot } : record));
    } catch (value) {
      setTaskHistoryMessage(warnNotice(value instanceof Error ? value.message : String(value)));
    }
  }, [cloudProvider, controller, localProvider, pipeline.snapshot?.task_id]);

  /** The library locks every start button while a task runs, so the card that
      shows the progress is also where the way out belongs -- reaching the task
      history page should not be the only way to stop a run. */
  const cancelActiveTask = useCallback(async () => {
    const taskId = pipeline.snapshot?.task_id;
    if (!taskId) return;
    setLibraryMessage(noNotice);
    try {
      const snapshot = await controller.cancel();
      if (!snapshot) return;
      setTaskHistory((current) => current.map((record) => record.taskId === taskId ? { ...record, snapshot } : record));
      setLibraryMessage(okNotice("已取消处理。已完成的环节会保留，可在处理历史中继续。"));
    } catch (value) {
      setLibraryMessage(warnNotice(value instanceof Error ? value.message : String(value)));
    }
  }, [controller, pipeline.snapshot?.task_id]);

  // History can contain local and cloud tasks at the same time. Keep both
  // readers available so every log panel follows the provider that owns it.
  const taskLogSources = useMemo(() => ({
    local: (taskId: string, afterCursor: number) => localProvider.events(taskId, afterCursor),
    cloud: (taskId: string, afterCursor: number) => cloudProvider.events(taskId, afterCursor),
  }), [cloudProvider, localProvider]);

  const clearTaskHistory = useCallback(async () => {
    const active = taskHistoryRef.current.filter((item) => activeStates.has(item.snapshot.state));
    const clearedCount = taskHistoryRef.current.length - active.length;
    if (clearedCount === 0) return;
    // Update the ref before awaiting disk I/O so any refresh already in flight
    // sees the cleared local history when it commits.
    taskHistoryRef.current = active;
    setTaskHistory(active);
    try {
      await desktopTaskHistory.save(active.slice(0, taskHistoryLimit));
      setTaskHistoryMessage(okNotice(active.length > 0
        ? `已清除 ${clearedCount} 条本地历史记录，进行中的任务已保留。`
        : "本地任务列表已清空。"));
    } catch (value) {
      setTaskHistoryMessage(warnNotice(`清空本地任务历史失败：${value instanceof Error ? value.message : String(value)}`));
    }
  }, []);

  const navigateLibrary = useCallback(() => {
    setSection("library");
    setLibraryFilter("all");
    setQuery("");
    // A task may have finished through history polling instead of the active
    // pipeline (for example after an app restart). Refresh on entry as a final
    // guard against rendering the pre-completion library snapshot.
    void loadLibrary();
  }, [loadLibrary]);

  const startMedia = useCallback(async (entry: MediaEntry) => {
    setLibraryMessage(noNotice);
    setTranscriptionError("");
    setTranscriptionMedia(entry);
  }, []);

  // 中文轴 / 双语轴：文字已经齐了，什么都不用算，直接落成可编辑文档。
  const importAxis = useCallback(async (axis: TaskAxis) => {
    if (!transcriptionMedia) return;
    const media = transcriptionMedia;
    setTranscriptionBusy(true);
    setTranscriptionError("");
    try {
      await documents.importAxis({
        videoID: media.id,
        axis,
        title: media.title,
        sourcePath: media.sourcePath,
        fingerprint: media.fingerprint,
        duration: media.duration,
      });
      setTranscriptionMedia(null);
      setMedia((entries) => entries.map((item) => item.id === media.id
        ? { ...item, documentAvailable: true, documentRemoved: false }
        : item));
      setLibraryMessage(okNotice(`已导入 ${axis.rows.length} 条字幕，可以直接编辑。`));
      void mediaLibrary.cacheMedia(media.id).then(() => Promise.all([loadLibrary(), loadCacheStatus()])).catch(() => {
        void loadLibrary();
      });
    } catch (value) {
      setTranscriptionError(value instanceof Error ? value.message : String(value));
    } finally {
      setTranscriptionBusy(false);
    }
  }, [loadCacheStatus, loadLibrary, transcriptionMedia]);

  const confirmTranscription = useCallback(async (mode: ExecutionMode, request: TaskRequest, axis: TaskAxis | null) => {
    if (!transcriptionMedia) return;
    setTranscriptionBusy(true);
    setTranscriptionError("");
    try {
      if (mode === "cloud" && !cloudSession?.authenticated) {
        throw new Error("请先使用 Key 登录云端账户");
      }
      // 投影是消费这条轴的一方，云端任务的产物也在本机投影，所以两种运行环境都记在
      // 本地文档旁边。没有轴时也要写一次：上一次导入留下的轴不该改变这一次的结果。
      if (axis) await documents.setAxis(transcriptionMedia.id, axis);
      else await documents.setAxis(transcriptionMedia.id, null).catch(() => undefined);
      const taskController = mode === "local" ? localController : cloudController;
      setExecutionMode(mode);
      const rawSnapshot = await taskController.start(request);
      const snapshot: TaskSnapshot = {
        ...rawSnapshot,
        started_at: rawSnapshot.started_at || new Date().toISOString(),
      };
      setActiveMedia(transcriptionMedia);
      rememberTask(snapshot, transcriptionMedia);
      setTranscriptionMedia(null);
      setSection("tasks");
      void mediaLibrary.cacheMedia(transcriptionMedia.id).then(() => Promise.all([loadLibrary(), loadCacheStatus()])).catch((value) => {
        setLibraryMessage(warnNotice(`任务已启动，但视频缓存创建失败：${value instanceof Error ? value.message : String(value)}`));
      });
    } catch (value) {
      setTranscriptionError(value instanceof Error ? value.message : String(value));
    } finally {
      setTranscriptionBusy(false);
    }
  }, [cloudController, cloudSession?.authenticated, loadCacheStatus, loadLibrary, localController, rememberTask, transcriptionMedia]);

  const saveCacheLimit = useCallback(async (limit: number) => {
    setCacheBusy(true);
    setCacheMessage("");
    try {
      setCacheStatus(await mediaLibrary.setCacheLimitGB(limit));
      await loadLibrary();
      setCacheMessage("缓存上限已保存，超出部分已按最近使用时间回收。");
    } catch (value) {
      setCacheMessage(value instanceof Error ? value.message : String(value));
    } finally {
      setCacheBusy(false);
    }
  }, [loadLibrary]);

  const clearCache = useCallback(async () => {
    setCacheBusy(true);
    setCacheMessage("");
    try {
      const status = await mediaLibrary.clearVideoCache();
      setCacheStatus(status);
      await loadLibrary();
      setCacheMessage(status.files > 0 ? "已清理缓存；正在编辑的视频副本暂时保留。" : "视频缓存已清理。");
    } catch (value) {
      setCacheMessage(value instanceof Error ? value.message : String(value));
    } finally {
      setCacheBusy(false);
    }
  }, [loadLibrary]);

  useEffect(() => {
    if (!pipeline.snapshot || !activeMedia) return;
    rememberTask(pipeline.snapshot, activeMedia);
  }, [activeMedia, pipeline.snapshot, rememberTask]);

  // Completion can arrive through the active pipeline or through task-history
  // polling. The latter is how a local run finishes after an app restart, and
  // used to update only the history row: the media card stayed at 等待处理 and
  // the cloud sync never ran. Reconcile every newly completed local task from
  // the durable history instead of tying these side effects to activeMedia.
  useEffect(() => {
    const completed = taskHistory.filter((item) =>
      item.provider === "local"
      && item.snapshot.state === "completed"
      && item.mediaId
      && !reconciledLocalTasks.current.has(item.taskId));
    if (completed.length === 0) return;
    completed.forEach((item) => reconciledLocalTasks.current.add(item.taskId));
    const current = completed.find((item) => item.taskId === pipeline.snapshot?.task_id);
    if (current) {
      // The local provider writes the document before publishing "completed".
      // Reflect that fact immediately instead of making the card wait for the
      // library refresh or the cloud-sync effect that follows it.
      setMedia((entries) => entries.map((entry) => entry.id === current.mediaId
        ? { ...entry, documentAvailable: true, documentRemoved: false }
        : entry));
      setLibraryMessage(okNotice(`"${current.title}" 字幕处理完成，可以直接编辑。`));
    }
    void Promise.all([loadLibrary(), loadCacheStatus()]);
  }, [loadCacheStatus, loadLibrary, pipeline.snapshot?.task_id, taskHistory]);

  // Fetch cloud artifacts as a durable completion side effect, independent of
  // which poller observed "completed". The active pipeline normally projects
  // them itself, but its first manifest request can race the backend publishing
  // the files; history polling and app restarts must reach the same outcome.
  useEffect(() => {
    if (!cloudSession?.authenticated) return;
    const latestByMedia = new Map<string, TaskHistoryEntry>();
    for (const item of taskHistory) {
      if (item.provider !== "cloud" || item.snapshot.state !== "completed" || !item.mediaId) continue;
      const entry = media.find((candidate) => candidate.id === item.mediaId);
      if (!entry || entry.documentAvailable || entry.documentRemoved) continue;
      const existing = latestByMedia.get(item.mediaId);
      if (!existing || Date.parse(item.snapshot.updated_at) > Date.parse(existing.snapshot.updated_at)) {
        latestByMedia.set(item.mediaId, item);
      }
    }
    // Only the newest completed run for a media item may write its document;
    // replaying older history afterward would silently replace fresh subtitles.
    const completed = [...latestByMedia.values()].filter((item) =>
      !reconciledCloudTasks.current.has(item.taskId)
      // Let the active controller finish its own status/artifact round first,
      // avoiding two projections writing the same local document concurrently.
      && !(item.taskId === pipeline.snapshot?.task_id && activeStates.has(pipeline.snapshot.state)));
    if (completed.length === 0) return;
    completed.forEach((item) => reconciledCloudTasks.current.add(item.taskId));
    // Hold the cards in 取回字幕中 for the whole download, not just for the frame
    // the snapshot flips on. Released only after the projection has marked the
    // documents available, so the card never falls back to 等待处理 in between.
    const reclaiming = completed.map((item) => item.mediaId);
    setReclaimingMedia((current) => new Set([...current, ...reclaiming]));
    const release = () => setReclaimingMedia((current) => {
      const next = new Set(current);
      reclaiming.forEach((id) => next.delete(id));
      return next;
    });
    void (async () => {
      const adopted = new Set<string>();
      const failed: Array<{ item: TaskHistoryEntry; error: unknown }> = [];
      for (const item of completed) {
        try {
          const alreadyProjected = item.taskId === pipeline.snapshot?.task_id && pipeline.artifacts !== null;
          if (!alreadyProjected) {
            // A completed snapshot can briefly precede artifact availability.
            // Retry a few times here because active-task polling stops at the
            // terminal state and would otherwise never attempt the download again.
            let lastError: unknown;
            for (let attempt = 0; attempt < 4; attempt += 1) {
              try {
                await cloudProvider.artifacts(item.taskId);
                lastError = undefined;
                break;
              } catch (value) {
                lastError = value;
                if (attempt < 3) await new Promise<void>((resolve) => window.setTimeout(resolve, 1500));
              }
            }
            if (lastError !== undefined) throw lastError;
          }
          adopted.add(item.mediaId);
        } catch (error) {
          reconciledCloudTasks.current.delete(item.taskId);
          failed.push({ item, error });
        }
      }
      if (adopted.size > 0) {
        await Promise.all([loadLibrary(), loadCacheStatus()]);
        setMedia((entries) => entries.map((entry) => adopted.has(entry.id)
          ? { ...entry, documentAvailable: true, documentRemoved: false }
          : entry));
        setTaskHistoryMessage(okNotice(adopted.size === 1
          ? "云端任务已完成，字幕已自动取回，可以直接编辑。"
          : `${adopted.size} 个云端任务的字幕已自动取回，可以直接编辑。`));
      }
      if (failed.length > 0) {
        const first = failed[0];
        const detail = first.error instanceof Error ? first.error.message : String(first.error);
        setTaskHistoryMessage(warnNotice(`“${first.item.title}”已完成，但自动取回字幕失败：${detail}`));
      }
    })().finally(release);
  }, [cloudProvider, cloudSession?.authenticated, loadCacheStatus, loadLibrary, media, pipeline.artifacts, pipeline.snapshot, taskHistory]);

  // Sync from the reconciled media document, not from activeMedia. This also
  // repairs a completed run discovered after restart. A remote local-sync entry
  // newer than the task is treated as its acknowledgement so reopening the app
  // does not upload the same historical result over and over.
  useEffect(() => {
    if (!cloudSession?.authenticated || cloudLoading) return;
    const latestByFingerprint = new Map<string, { item: TaskHistoryEntry; entry: MediaEntry }>();
    for (const item of taskHistory) {
      if (item.provider !== "local" || item.snapshot.state !== "completed" || syncedTasks.current.has(item.taskId)) continue;
      const entry = media.find((candidate) => candidate.id === item.mediaId);
      if (!entry?.documentAvailable || !entry.fingerprint) continue;
      const existing = latestByFingerprint.get(entry.fingerprint);
      if (!existing || Date.parse(item.snapshot.updated_at) > Date.parse(existing.item.snapshot.updated_at)) {
        latestByFingerprint.set(entry.fingerprint, { item, entry });
      }
    }
    const candidates = [...latestByFingerprint.values()].filter(({ item, entry }) => {
      const acknowledged = cloudMedia.some((remote) =>
        remote.fingerprint === entry.fingerprint
        && remote.status === "completed"
        && remote.source === "local_sync"
        && Date.parse(remote.updatedAt) >= Date.parse(item.snapshot.updated_at));
      return !acknowledged;
    });
    if (candidates.length === 0) return;
    candidates.forEach(({ item }) => syncedTasks.current.add(item.taskId));
    setAccountMessage("正在把本机字幕同步到云端…");
    void (async () => {
      try {
        let suppressed = 0;
        for (const { item, entry } of candidates) {
          const result = await cloudAccount.syncLocalTask(item.taskId, entry.id, entry.fingerprint, entry.title, entry.duration);
          if (result.status === "suppressed") suppressed += 1;
        }
        setCloudMedia(await cloudAccount.library());
        const uploaded = candidates.length - suppressed;
        setAccountMessage(uploaded === 0
          ? "已保留云端删除设置，本机字幕不会自动重新上传。"
          : suppressed > 0
            ? `已自动同步 ${uploaded} 个本机字幕；另有 ${suppressed} 个按云端删除设置保留在本机。`
            : uploaded === 1 ? "本机字幕已自动同步到云端。" : `已自动同步 ${uploaded} 个本机字幕到云端。`);
      } catch (value) {
        candidates.forEach(({ item }) => syncedTasks.current.delete(item.taskId));
        setAccountMessage(`自动同步失败：${value instanceof Error ? value.message : String(value)}`);
      }
    })();
  }, [cloudLoading, cloudMedia, cloudSession?.authenticated, media, taskHistory]);

  const renameMedia = useCallback((entry: MediaEntry) => {
    setDialog({ kind: "rename", entry, value: entry.title });
  }, []);

  const removeMedia = useCallback((entry: MediaEntry) => {
    setDialog({ kind: "remove", entry });
  }, []);

  const deleteLocalSubtitles = useCallback((entry: MediaEntry) => {
    setDialog({ kind: "delete-subtitles", entry });
  }, []);

  const deleteCloudMedia = useCallback((entry: CloudEntry) => {
    setDialog({ kind: "cloud-remove", entry });
  }, []);

  const submitDialog = useCallback(async () => {
    if (!dialog) return;
    setDialogBusy(true);
    try {
      if (dialog.kind === "rename") {
        const title = dialog.value.trim();
        if (!title || title === dialog.entry.title) {
          setDialog(null);
          return;
        }
        await mediaLibrary.rename(dialog.entry.id, title);
      } else if (dialog.kind === "remove") {
        await mediaLibrary.remove(dialog.entry.id, false);
      } else if (dialog.kind === "delete-subtitles") {
        // Same predicate the auto-adoption loop above matches on: suppressing
        // an entry it would not have picked up leaves the deleted subtitles
        // being pulled straight back down.
        const remote = cloudMedia.find((entry) => hasCloudSubtitles(entry) && entry.fingerprint === dialog.entry.fingerprint);
        if (remote) {
          const key = `${remote.id}:${dialog.entry.id}`;
          adoptedCloudEntries.current.add(key);
          setAdoptFailed((current) => new Set([...current, key]));
        }
        await mediaLibrary.deleteDocument(dialog.entry.id);
      } else {
        await cloudAccount.deleteLibraryEntry(dialog.entry.id);
        setTaskHistory((current) => current.filter((item) => item.taskId !== dialog.entry.id));
      }
      setDialog(null);
      await (dialog.kind === "cloud-remove" ? loadCloud() : loadLibrary());
    } catch (value) {
      setLibraryMessage(warnNotice(value instanceof Error ? value.message : String(value)));
      setDialog(null);
    } finally {
      setDialogBusy(false);
    }
  }, [cloudMedia, dialog, loadCloud, loadLibrary]);

  const relinkMedia = useCallback(async (entry: MediaEntry) => {
    try {
      await mediaLibrary.relink(entry.id);
      await loadLibrary();
    } catch (value) {
      setLibraryMessage(warnNotice(value instanceof Error ? value.message : String(value)));
    }
  }, [loadLibrary]);

  const associateCloudMedia = useCallback(async (entry: CloudEntry) => {
    setLibraryMessage(noNotice);
    setAdoptingCloud((current) => new Set([...current, entry.id]));
    try {
      // Create or reuse the subtitle placeholder first, then attach the chosen
      // file to that same local id. Going through the generic import flow here
      // creates a second media entry beside the downloaded subtitles.
      const localID = await cloudAccount.adoptCloudEntry(entry.id);
      const relinked = await mediaLibrary.relink(localID);
      await loadLibrary();
      if (relinked?.id) setLibraryMessage(okNotice(`已为“${entry.title}”关联本地视频。`));
    } catch (value) {
      setLibraryMessage(warnNotice(value instanceof Error ? value.message : String(value)));
      await loadLibrary();
    } finally {
      setAdoptingCloud((current) => new Set([...current].filter((id) => id !== entry.id)));
    }
  }, [loadLibrary]);

  const login = useCallback(async () => {
    setAccountBusy(true);
    setCloudLoading(true);
    setAccountMessage("");
    try {
      const session = await cloudAccount.login(DEFAULT_CLOUD_BACKEND, loginKey);
      setCloudSession(session);
      setLoginKey("");
      setCloudMedia(await cloudLibraryOf(session));
      setCloudCapabilities(await cloudProvider.capabilities().catch(() => null));
      setAccountMessage(
        session.syncError
          ? `登录成功，但自动同步检查失败：${session.syncError}`
          : `登录成功，已合并视频库并补同步 ${session.synced ?? 0} 个本地字幕${session.syncFailed ? `，${session.syncFailed} 个失败` : ""}。`,
      );
      setSection("library");
    } catch (value) {
      setAccountMessage(value instanceof Error ? value.message : String(value));
    } finally {
      setAccountBusy(false);
      setCloudLoading(false);
    }
  }, [cloudProvider, loginKey]);

  const logout = useCallback(async () => {
    await cloudAccount.logout();
    setCloudSession({ authenticated: false, running: 0, videos: [] });
    setCloudMedia([]);
    setCloudCapabilities(null);
    setAccountMessage("已退出；本地媒体库保持不变。");
  }, []);

  const saveKeys = useCallback(async (updates: Record<string, string | null>, keyName: string) => {
    const payload = Object.fromEntries(Object.entries(updates).map(([name, value]) => [name, typeof value === "string" ? value.trim() : value]));
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

  const installRuntime = useCallback(async (target: RuntimeInstallTarget) => {
    setProvisionMessage("");
    try {
      setRuntimeProvision(await fineSubRuntime.install(target));
    } catch (value) {
      setProvisionMessage(value instanceof Error ? value.message : String(value));
      await refresh({ silent: true });
    }
  }, [refresh]);

  const removeRuntime = useCallback(async () => {
    setProvisionMessage("");
    try {
      setRuntimeProvision(await fineSubRuntime.removeAll());
      await refresh();
    } catch (value) {
      setProvisionMessage(value instanceof Error ? value.message : String(value));
      // A refused or partial removal still changed what is on disk, so the
      // panel is reconciled with the sidecar rather than left as it was.
      await refresh({ silent: true });
    }
  }, [refresh]);

  const removeRuntimeGroup = useCallback(async (target: RuntimeToolGroup) => {
    setProvisionMessage("");
    try {
      setRuntimeProvision(await fineSubRuntime.removeGroup(target));
      await refresh();
    } catch (value) {
      setProvisionMessage(value instanceof Error ? value.message : String(value));
      await refresh({ silent: true });
    }
  }, [refresh]);

  const cancelRuntimeInstall = useCallback(async () => {
    setProvisionMessage("");
    try {
      setRuntimeProvision(await fineSubRuntime.cancel());
    } catch (value) {
      setProvisionMessage(value instanceof Error ? value.message : String(value));
      await refresh({ silent: true });
    }
  }, [refresh]);

  const installPython = useCallback(async () => {
    setSection("runtime");
    setMessage("正在准备 Python，以启用本地服务。");
    try {
      setPythonBootstrap(await fineSubRuntime.installPython());
    } catch (value) {
      setMessage(value instanceof Error ? value.message : String(value));
      setPythonBootstrap(await fineSubRuntime.pythonStatus().catch(() => null));
    }
  }, []);

  useEffect(() => {
    if (sidecar?.running || pythonBootstrap?.state !== "missing" || !pythonBootstrap.supported || pythonBootstrapStarted.current) return;
    pythonBootstrapStarted.current = true;
    void installPython();
  }, [installPython, pythonBootstrap, sidecar?.running]);

  const runtimeProvisionMissing = runtimeProvision === null;

  // The sidecar is started before the window opens, so the first refresh can
  // land while it is still handshaking, and a start that failed outright never
  // recovers on its own. A transient failure on the follow-up reads leaves the
  // same hole with the sidecar up but the provision state unknown. All of them
  // used to strand the runtime page on permanently disabled buttons until the
  // user found 「重新检查」. Python installation drives its own polling.
  useEffect(() => {
    if (pythonBootstrap?.state === "running") return;
    if (sidecar?.running && !runtimeProvisionMissing) return;
    const timer = window.setInterval(() => void refresh({ silent: true }), sidecarRetryIntervalMs);
    return () => window.clearInterval(timer);
  }, [pythonBootstrap?.state, refresh, runtimeProvisionMissing, sidecar?.running]);

  useEffect(() => {
    if (pythonBootstrap?.state !== "running") return;
    const timer = window.setInterval(() => {
      void fineSubRuntime.pythonStatus().then((status) => {
        setPythonBootstrap(status);
        setMessage(status.message);
        if (status.state === "ready") void refresh();
      }).catch(() => undefined);
    }, 1000);
    return () => window.clearInterval(timer);
  }, [pythonBootstrap?.state, refresh]);

  const runtimeReady = capabilities?.runtime?.ready === true;
  const issues = capabilities?.runtime?.issues ?? [];
  const title = section === "library"
    ? "媒体库"
    : section === "tasks"
      ? "处理任务"
      : section === "runtime"
        ? "运行环境"
        : section === "plugins"
          ? "插件管理"
          : section === "plugin"
            ? activeMountedPlugin?.tool.title ?? "插件工具"
            : section === "account"
              ? "云端账户"
              : section === "adminKeys"
                ? "Key 管理"
                : section === "about"
                  ? "关于"
                  : "设置";
  const remoteByFingerprint = useMemo(() => new Map(cloudMedia.filter((entry) => entry.fingerprint).map((entry) => [entry.fingerprint, entry])), [cloudMedia]);
  const cloudOnly = useMemo(() => {
    const localFingerprints = new Set(media.map((entry) => entry.fingerprint));
    return cloudMedia.filter((entry) => !entry.fingerprint || !localFingerprints.has(entry.fingerprint));
  }, [cloudMedia, media]);
  const taskActive = activeStates.has(pipeline.snapshot?.state ?? "");
  const rememberedActiveTaskCount = taskHistory.filter((item) => activeStates.has(item.snapshot.state)).length;
  const activeTaskCount = Math.max(rememberedActiveTaskCount, taskActive ? 1 : 0, cloudSession?.running ?? 0);
  const localRunningID = taskActive ? activeMedia?.id : undefined;
  const libraryItems = useMemo<LibraryItem[]>(() => [
    ...media.map((entry): LibraryItem => ({ kind: "local", entry })),
    ...cloudOnly.map((entry): LibraryItem => ({ kind: "cloud", entry })),
  ], [cloudOnly, media]);
  const visibleItems = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    return libraryItems.filter((item) => {
      const entry = item.entry;
      const matchesQuery = !needle || entry.title.toLocaleLowerCase().includes(needle)
        || (item.kind === "local" && item.entry.sourcePath.toLocaleLowerCase().includes(needle));
      if (!matchesQuery) return false;
      if (libraryFilter === "all") return true;
      if (libraryFilter === "cloud") return item.kind === "cloud";
      if (libraryFilter === "running") {
        return item.kind === "local"
          ? item.entry.id === localRunningID
          : activeStates.has(item.entry.status);
      }
      if (item.kind === "cloud") return false;
      if (libraryFilter === "ready") return item.entry.documentAvailable;
      return !item.entry.available;
    }).sort((left, right) => {
      if (sortMode === "name") return left.entry.title.localeCompare(right.entry.title, "zh-CN");
      if (sortMode === "duration") return right.entry.duration - left.entry.duration;
      const leftTime = left.kind === "local" ? left.entry.lastAccess || left.entry.addedAt : Date.parse(left.entry.updatedAt);
      const rightTime = right.kind === "local" ? right.entry.lastAccess || right.entry.addedAt : Date.parse(right.entry.updatedAt);
      return rightTime - leftTime;
    });
  }, [libraryFilter, libraryItems, localRunningID, query, sortMode]);
  const filterCounts: Record<LibraryFilter, number> = {
    all: libraryItems.length,
    ready: media.filter((entry) => entry.documentAvailable).length,
    running: media.filter((entry) => entry.id === localRunningID).length + cloudOnly.filter((entry) => activeStates.has(entry.status)).length,
    cloud: cloudOnly.length,
    missing: media.filter((entry) => !entry.available).length,
  };
  return (
    <div className={`app-shell ${sidebarCollapsed ? "sidebar-collapsed" : ""}`} data-file-drop-target>
      <WindowDropOverlay onFilesDropped={handleDroppedFiles} />
      <UpdateOverlay update={selfUpdate} />
      <aside className="sidebar">
        <div className="brand">
          <img className="brand-glyph" src="./assets/icon.png" alt="Nonoka Sub X" draggable={false} />
          <span className="brand-name">Nonoka Sub X</span>
          <button className="sidebar-collapse" aria-label={sidebarCollapsed ? "展开侧边栏" : "折叠侧边栏"} title={sidebarCollapsed ? "展开侧边栏" : "折叠侧边栏"} onClick={() => setSidebarCollapsed((value) => !value)}>{sidebarCollapsed ? "›" : "‹"}</button>
        </div>
        <span className="nav-label">工作区</span>
        <nav aria-label="主导航">
          {(["library", "tasks"] as const satisfies readonly NavigationSection[]).map((item) => (
            <button className={section === item ? "active" : ""} key={item} title={item === "library" ? "媒体库" : "处理任务"} onClick={() => setSection(item)}>
              <NavIcon kind={item} />
              <span className="nav-text">{item === "library" ? "媒体库" : "处理任务"}</span>
              {item === "library" && libraryItems.length > 0 && <small className="nav-count">{libraryItems.length}</small>}
              {item === "tasks" && activeTaskCount > 0 && <small className="nav-count active-count">{activeTaskCount}</small>}
            </button>
          ))}
        </nav>
        <span className="nav-label tools-label">工具</span>
        <nav className="plugin-tools-nav" aria-label="插件工具">
          {pluginTools.map((item) => (
            <button
              className={section === "plugin" && activePluginTool?.pluginId === item.pluginId && activePluginTool.toolId === item.tool.id ? "active" : ""}
              key={`${item.pluginId}:${item.tool.id}`}
              title={`${item.pluginName} · ${item.tool.title}`}
              onClick={() => openPluginTool(item.pluginId, item.tool.id)}
            >
              <PluginToolIcon />
              <span className="nav-text">{item.tool.title}</span>
            </button>
          ))}
          <button className={section === "plugins" ? "active" : ""} title="插件管理" onClick={() => setSection("plugins")}>
            <NavIcon kind="plugins" />
            <span className="nav-text">插件管理</span>
            {/* {plugins.length > 0 && <small className="nav-count">{plugins.length}</small>} */}
          </button>
        </nav>
        <span className="nav-label management-label">管理</span>
        <nav aria-label="管理导航">
          <button className={section === "runtime" ? "active" : ""} title="运行环境" onClick={() => setSection("runtime")}>
            <NavIcon kind="runtime" />
            <span className="nav-text">运行环境</span>
          </button>
          <button className={section === "keys" ? "active" : ""} title="设置" onClick={() => setSection("keys")}>
            <NavIcon kind="settings" />
            <span className="nav-text">设置</span>
          </button>
          {cloudSession?.admin && <button className={section === "adminKeys" ? "active" : ""} title="Key 管理" onClick={() => setSection("adminKeys")}>
            <NavIcon kind="adminKeys" />
            <span className="nav-text">Key 管理</span>
          </button>}
          <button className={section === "about" ? "active" : ""} title="关于" onClick={() => setSection("about")}>
            <NavIcon kind="about" />
            <span className="nav-text">关于</span>
          </button>
        </nav>
        <div className="sidebar-spacer" />
        <section className="sidebar-provider" aria-label="执行位置">
          <div className="sidebar-provider-status">
            <span className={`status-dot ${executionMode === "cloud" ? "online" : sidecar?.running ? "local" : ""}`} />
            <div>
              <strong>{executionMode === "cloud" ? "Nonoka Cloud" : "本地运行"}</strong>
              <small>{executionMode === "cloud" ? cloudSession?.admin ? "管理员 · 不限次" : `剩余 ${cloudSession?.remaining ?? "—"} 次` : runtimeReady ? "引擎已就绪" : "需要检查环境"}</small>
            </div>
          </div>
          <div className="sidebar-provider-switch">
            <button className={executionMode === "local" ? "active" : ""} disabled={taskActive} onClick={() => setExecutionMode("local")}>本机</button>
            <button className={executionMode === "cloud" ? "active" : ""} disabled={taskActive} onClick={() => cloudSession?.authenticated ? setExecutionMode("cloud") : setSection("account")}>云端</button>
          </div>
        </section>
        <button className="sidebar-account" aria-busy={cloudLoading} disabled={cloudLoading} onClick={() => setSection("account")} title="云端账户">
          <span className={`account-avatar ${cloudLoading ? "loading" : ""}`}>{cloudLoading ? <span className="account-spinner" aria-hidden="true" /> : "N"}</span>
          <span className="account-copy">
            <strong>{cloudLoading ? "正在验证账户" : cloudSession?.authenticated ? cloudSession.name || "未命名 Key" : "未登录云端"}</strong>
            <small>{cloudLoading ? "同步中" : cloudSession?.authenticated ? `已同步 ${cloudMedia.length} 个字幕记录` : "登录后同步媒体库"}</small>
          </span>
        </button>
      </aside>

      <main className="workspace">
        {/* Windows caption strip: empty on purpose so the whole band stays a
            non-client drag region — anything interactive placed here would be
            swallowed by the native hit test. */}
        <div className="workspace-titlebar" aria-hidden="true" />
        <div className={`workspace-scroll${section === "plugin" ? " plugin-workspace-scroll" : ""}`}>
          <header className="topbar">
            <div>
              {section === "library" && <span className="page-kicker">MY MEDIA COLLECTION</span>}
              <h1>{title}</h1>
            </div>
            {section === "library" && <label className="library-search"><span>⌕</span><input type="search" placeholder="搜索标题或文件名" value={query} onChange={(event) => setQuery(event.target.value)} /></label>}
            <div className="topbar-actions">
              <UpdateButton update={selfUpdate} />
              <button className="theme-button" aria-label="切换主题" title={theme === "dark" ? "切换到浅色模式" : "切换到深色模式"} onClick={() => setTheme((value) => value === "dark" ? "light" : "dark")}>{theme === "dark" ? "☀" : "☾"}</button>
              {section === "library" ? (
                <button className="primary-button" disabled={libraryBusy} onClick={() => void importMedia()}>{libraryBusy ? "正在导入…" : "＋ 添加媒体"}</button>
              ) : section === "tasks" ? (
                <button className="quiet-button" onClick={() => void refreshTaskHistory()} disabled={taskHistoryBusy}>{taskHistoryBusy ? "正在刷新…" : "刷新任务"}</button>
              ) : section === "runtime" ? (
                <button className="quiet-button" onClick={() => void refresh()} disabled={loadState === "loading"}>{loadState === "loading" ? "正在检查…" : "重新检查"}</button>
              ) : null}
            </div>
          </header>

          {!runtimeReady && loadState !== "loading" && (section === "library" || section === "tasks") && <section className={`runtime-banner warning`}>
            <div className="runtime-symbol">!</div>
            <div className="runtime-copy">
              <span className="eyebrow">本地执行环境</span>
              <h2>需要完成运行时配置</h2>
              <p>
                {message || issues[0]?.message || "正在读取 capabilities…"}
              </p>
            </div>
            <button onClick={() => setSection("runtime")}>查看详情 →</button>
          </section>}

          {section === "library" && (
            <LibraryPage
              items={libraryItems}
              visibleItems={visibleItems}
              thumbnails={thumbnails}
              cloudThumbnails={cloudThumbnails}
              adoptingMedia={adoptingMedia}
              adoptingCloud={adoptingCloud}
              remoteByFingerprint={remoteByFingerprint}
              filter={libraryFilter}
              filterCounts={filterCounts}
              sort={sortMode}
              view={viewMode}
              busy={libraryBusy}
              message={libraryMessage.text}
              messageTone={libraryMessage.tone}
              localRunningID={localRunningID}
              runningProgress={pipeline.snapshot?.progress?.total ? Math.min(100, pipeline.snapshot.progress.completed / pipeline.snapshot.progress.total * 100) : 8}
              taskActive={taskActive}
              syncing={cloudLoading}
              setFilter={setLibraryFilter}
              setSort={setSortMode}
              setView={setViewMode}
              onClearQuery={() => setQuery("")}
              onImport={importMedia}
              onOpen={(entry) => void openEditor(entry)}
              onStart={startMedia}
              onCancel={cancelActiveTask}
              onRename={renameMedia}
              onDeleteSubtitles={deleteLocalSubtitles}
              onRemove={removeMedia}
              onAdoptCloud={adoptLocalSubtitles}
              onEditCloud={editCloudEntry}
              onAssociateCloud={associateCloudMedia}
              onDeleteCloud={deleteCloudMedia}
              onRelink={relinkMedia}
              onDismissMessage={() => setLibraryMessage(noNotice)}
            />
          )}

          {section === "tasks" && (
            <TasksPage
              tasks={taskHistory}
              media={media}
              activeCount={activeTaskCount}
              message={taskHistoryMessage.text}
              messageTone={taskHistoryMessage.tone}
              pipelineError={pipeline.error?.message}
              logSources={taskLogSources}
              onNavigateLibrary={navigateLibrary}
              onOpenEditor={(entry) => void openEditor(entry)}
              onClearTasks={clearTaskHistory}
              onTaskAction={actOnHistoryTask}
              onDismissMessage={() => setTaskHistoryMessage(noNotice)}
            />
          )}

          {section === "plugins" && (
            <PluginManagerPage
              plugins={plugins}
              busy={pluginBusy}
              message={pluginMessage}
              onInstall={installPlugin}
              onToggle={togglePlugin}
              onUninstall={uninstallPlugin}
              onOpenTool={openPluginTool}
              onDismissMessage={() => setPluginMessage("")}
            />
          )}

          {section === "plugin" && activeMountedPlugin && (
            <PluginPageHost
              mounted={activeMountedPlugin}
              theme={theme}
              onOpenManager={() => setSection("plugins")}
              onOpenLibrary={() => setSection("library")}
              onOpenRuntime={() => setSection("runtime")}
            />
          )}

          {section === "runtime" && (
            <RuntimePage
              capabilities={capabilities}
              message={message}
              provisionMessage={provisionMessage}
              onDismissProvisionMessage={() => setProvisionMessage("")}
              provision={runtimeProvision}
              pythonBootstrap={pythonBootstrap}
              ready={runtimeReady}
              sidecar={sidecar}
              onCancelInstall={cancelRuntimeInstall}
              onInstall={installRuntime}
              onInstallPython={installPython}
              onRemoveAll={removeRuntime}
              onRemoveGroup={removeRuntimeGroup}
              storage={storageStatus}
              storageProgress={storageProgress}
              storageBusy={storageBusy}
              onChooseStorage={chooseStorage}
              onRelocateStorage={relocateStorage}
              onCancelStorage={cancelStorage}
            />
          )}

          {section === "keys" && (
            <SettingsPage
              settings={settings}
              drafts={keyDraft}
              busy={keysBusy}
              message={keysMessage}
              cache={cacheStatus}
              cacheBusy={cacheBusy}
              cacheMessage={cacheMessage}
              storage={storageStatus}
              storageProgress={storageProgress}
              storageBusy={storageBusy}
              storageMessage={storageMessage}
              setDrafts={setKeyDraft}
              onSaveKey={saveKeys}
              onSaveCacheLimit={saveCacheLimit}
              onClearCache={clearCache}
              onChooseStorage={chooseStorage}
              onRelocateStorage={relocateStorage}
              onResetStorage={resetStorage}
              onCancelStorage={cancelStorage}
              onDismissMessage={() => setKeysMessage("")}
              onDismissCacheMessage={() => setCacheMessage("")}
              onDismissStorageMessage={() => setStorageMessage("")}
            />
          )}

          {section === "adminKeys" && cloudSession?.admin && <AdminKeysPage />}

          {section === "account" && (
            <AccountPage session={cloudSession} media={cloudMedia} loginKey={loginKey} busy={accountBusy} message={accountMessage} onLoginKeyChange={setLoginKey} onLogin={login} onLogout={logout} onDismissMessage={() => setAccountMessage("")} />
          )}

          {section === "about" && <AboutPage update={selfUpdate} />}
        </div>
      </main>

      {dialog && <MediaDialog dialog={dialog} busy={dialogBusy} setDialog={setDialog} onSubmit={submitDialog} />}
      {transcriptionMedia && <TranscriptionDialog
        entry={transcriptionMedia}
        initialMode={executionMode}
        localReady={runtimeReady}
        localCapabilities={capabilities}
        cloudCapabilities={cloudCapabilities}
        settings={settings}
        localIssue={message || issues[0]?.message}
        cloudAuthenticated={cloudSession?.authenticated === true}
        cloudRemaining={cloudSession?.remaining ?? undefined}
        busy={transcriptionBusy}
        error={transcriptionError}
        onClose={() => { if (!transcriptionBusy) setTranscriptionMedia(null); }}
        onOpenRuntime={() => { setTranscriptionMedia(null); setSection("runtime"); }}
        onOpenAccount={() => { setTranscriptionMedia(null); setSection("account"); }}
        onOpenKeys={() => { setTranscriptionMedia(null); setSection("keys"); }}
        onImport={importAxis}
        onStart={confirmTranscription}
      />}
    </div>
  );
}
