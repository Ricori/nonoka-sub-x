import { useCallback, useEffect, useMemo, useState } from "react";
import { desktopPreferences, type ProxyCustom, type ProxyStatus } from "../bridge/preferences.ts";
import { CustomSelect } from "./CustomSelect.tsx";
import { Notice, type NoticeTone } from "./Notice.tsx";
import "./ProxySettingsCard.css";

type Draft = { scheme: string; host: string; port: string };

const schemeOptions = [
  { value: "http", label: "HTTP" },
  { value: "socks5", label: "SOCKS5" },
] as const;

const modeOptions = [
  { value: "system", label: "跟随系统代理" },
  { value: "custom", label: "自定义代理" },
] as const;

const sourceNames: Record<string, string> = { custom: "自定义", system: "系统代理", environment: "环境变量" };

function draftFromURL(proxy: string): Draft {
  try {
    const parsed = new URL(proxy);
    return {
      scheme: parsed.protocol.startsWith("socks") ? "socks5" : "http",
      host: parsed.hostname.replace(/^\[|\]$/g, "") || "127.0.0.1",
      port: parsed.port,
    };
  } catch {
    return { scheme: "http", host: "127.0.0.1", port: "" };
  }
}

function draftFromCustom(custom: ProxyCustom): Draft {
  return { scheme: custom.scheme, host: custom.host, port: String(custom.port) };
}

function hostPort(proxy: string): string {
  try {
    return new URL(proxy).host || proxy;
  } catch {
    return proxy;
  }
}

function validate(draft: Draft): string {
  const port = Number(draft.port);
  if (!draft.port.trim()) return "请填写端口";
  if (!Number.isInteger(port) || port < 1 || port > 65535) return "端口必须在 1 到 65535 之间";
  if (/[/@?#\s]/.test(draft.host.trim()) || draft.host.includes("://")) return "地址只填主机名或 IP，例如 127.0.0.1";
  return "";
}

const sameDraft = (a: Draft, b: Draft) => a.scheme === b.scheme && a.host.trim() === b.host.trim() && a.port.trim() === b.port.trim();

export function ProxySettingsCard({ embedded = false }: { embedded?: boolean }) {
  const [custom, setCustom] = useState<ProxyCustom | null | undefined>(undefined);
  const [mode, setMode] = useState<"system" | "custom">("system");
  const [status, setStatus] = useState<ProxyStatus | null>(null);
  const [draft, setDraft] = useState<Draft>({ scheme: "http", host: "127.0.0.1", port: "7890" });
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [tone, setTone] = useState<NoticeTone>("warn");
  const [edited, setEdited] = useState(false);

  const baseline = useMemo<Draft | null>(() => {
    if (custom === undefined || !status) return null;
    if (custom) return draftFromCustom(custom);
    return status.system ? draftFromURL(status.system) : { scheme: "http", host: "127.0.0.1", port: "7890" };
  }, [custom, status]);

  const refreshStatus = useCallback(async () => {
    try {
      setStatus(await desktopPreferences.proxyStatus());
    } catch {
      // ignore transient status failures
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const [preferences, current] = await Promise.all([desktopPreferences.get(), desktopPreferences.proxyStatus()]);
        if (cancelled) return;
        const saved = preferences.proxy ?? null;
        setCustom(saved);
        setMode(saved ? "custom" : "system");
        setStatus(current);
      } catch (error) {
        if (!cancelled) setMessage(`读取代理设置失败：${String(error)}`);
      }
    })();
    const timer = window.setInterval(() => void refreshStatus(), 8000);
    window.addEventListener("focus", refreshStatus);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
      window.removeEventListener("focus", refreshStatus);
    };
  }, [refreshStatus]);

  useEffect(() => {
    if (baseline && !edited) setDraft(baseline);
  }, [baseline, edited]);

  const update = (patch: Partial<Draft>) => {
    setEdited(true);
    setMessage("");
    setDraft((current) => ({ ...current, ...patch }));
  };

  const save = async (value: ProxyCustom | null, done: string) => {
    setBusy(true);
    setMessage("");
    try {
      const saved = await desktopPreferences.save({ proxy: value });
      setCustom(saved.proxy ?? null);
      setMode(saved.proxy ? "custom" : "system");
      setEdited(false);
      setTone("success");
      setMessage(done);
      await refreshStatus();
    } catch (error) {
      setTone("warn");
      setMessage(`保存失败：${String(error)}`);
    } finally {
      setBusy(false);
    }
  };

  const handleModeChange = (next: string) => {
    setMessage("");
    if (next === "system") {
      setMode("system");
      setEdited(false);
      if (custom) void save(null, "已切换为跟随系统代理。");
    } else {
      setMode("custom");
    }
  };

  const problem = mode === "custom" ? validate(draft) : "";
  const dirty = mode === "custom" && (!custom || (baseline ? !sameDraft(draft, baseline) : true));

  const submit = () => {
    if (mode !== "custom" || problem) return;
    void save({ scheme: draft.scheme, host: draft.host.trim() || "127.0.0.1", port: Number(draft.port) }, "代理设置已保存。");
  };

  let route = "正在读取…";
  let routeWarn = false;
  if (status) {
    if (status.active) {
      route = `${hostPort(status.active)}（${sourceNames[status.source] ?? "代理"}）`;
    } else if (status.configured) {
      route = `${hostPort(status.configured)} 未响应，暂时直连`;
      routeWarn = true;
    } else {
      route = "直连（未检测到系统代理）";
    }
  }

  const proxyTip = [
    "应用内环境下载、云端同步与模型请求使用的代理线路；默认跟随系统代理。",
    status?.system ? `系统代理：${hostPort(status.system)}` : "当前未检测到系统代理",
  ].join("\n");

  const content = (
    <div className="proxy-compact">
      <div className="proxy-summary-row">
        <div className="proxy-summary-left" title={proxyTip}>
          <strong>网络代理</strong>
          <p className={`proxy-route${routeWarn ? " warn" : status?.active ? " active" : ""}`}>
            <i aria-hidden="true" />
            {route}
          </p>
        </div>
        <div className="proxy-mode-control">
          <CustomSelect
            compact
            ariaLabel="代理模式"
            value={mode}
            options={modeOptions.map((item) => ({ value: item.value, label: item.label }))}
            disabled={busy || custom === undefined}
            onChange={handleModeChange}
          />
        </div>
      </div>

      {mode === "custom" && (
        <div className="proxy-custom-bar">
          <div className="proxy-fields">
            <CustomSelect
              compact
              ariaLabel="代理类型"
              value={draft.scheme}
              options={schemeOptions.map((item) => ({ value: item.value, label: item.label }))}
              disabled={busy}
              onChange={(scheme) => update({ scheme })}
            />
            <label className="proxy-host">
              <span>地址</span>
              <input
                value={draft.host}
                placeholder="127.0.0.1"
                spellCheck={false}
                disabled={busy}
                onChange={(event) => update({ host: event.target.value })}
                onKeyDown={(event) => event.key === "Enter" && submit()}
              />
            </label>
            <label className="proxy-port">
              <span>端口</span>
              <input
                value={draft.port}
                placeholder="7890"
                inputMode="numeric"
                disabled={busy}
                aria-invalid={Boolean(problem && edited)}
                onChange={(event) => update({ port: event.target.value.replace(/[^\d]/g, "") })}
                onKeyDown={(event) => event.key === "Enter" && submit()}
              />
            </label>
          </div>
          <button
            type="button"
            className="primary-button runtime-compact-btn"
            disabled={busy || !dirty || Boolean(problem)}
            onClick={submit}
          >
            {busy ? "保存中…" : "保存"}
          </button>
        </div>
      )}

      {((problem && edited) || message) && (
        <div className="proxy-inline-status">
          {message ? (
            <Notice className="proxy-footer-notice" message={message} tone={tone} onDismiss={() => setMessage("")} />
          ) : (
            <span className="warn">{problem}</span>
          )}
        </div>
      )}
    </div>
  );

  if (embedded) return content;
  return <article className="panel proxy-card">{content}</article>;
}
