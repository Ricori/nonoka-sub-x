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

const sourceNames: Record<string, string> = { custom: "自定义", system: "系统代理", environment: "环境变量" };

/** "http://127.0.0.1:7890" → 表单的三个字段；解析不了就给常见的本地默认值。 */
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

/** 和后端 netproxy.Custom.Normalize 同一套规则，不合法时直接在表单里说。 */
function validate(draft: Draft): string {
  const port = Number(draft.port);
  if (!draft.port.trim()) return "请填写端口";
  if (!Number.isInteger(port) || port < 1 || port > 65535) return "端口必须在 1 到 65535 之间";
  if (/[/@?#\s]/.test(draft.host.trim()) || draft.host.includes("://")) return "地址只填主机名或 IP，例如 127.0.0.1";
  return "";
}

const sameDraft = (a: Draft, b: Draft) => a.scheme === b.scheme && a.host.trim() === b.host.trim() && a.port.trim() === b.port.trim();

export function ProxySettingsCard() {
  const [custom, setCustom] = useState<ProxyCustom | null | undefined>(undefined);
  const [status, setStatus] = useState<ProxyStatus | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [tone, setTone] = useState<NoticeTone>("warn");

  // 表单的基准：自定义时是已保存的值，跟随系统时是系统代理本身。
  const baseline = useMemo<Draft | null>(() => {
    if (custom === undefined || !status) return null;
    if (custom) return draftFromCustom(custom);
    return status.system ? draftFromURL(status.system) : { scheme: "http", host: "127.0.0.1", port: "" };
  }, [custom, status]);

  const refreshStatus = useCallback(async () => {
    try {
      setStatus(await desktopPreferences.proxyStatus());
    } catch {
      // 读不到状态时保留上一次的，不打断表单。
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const [preferences, current] = await Promise.all([desktopPreferences.get(), desktopPreferences.proxyStatus()]);
        if (cancelled) return;
        setCustom(preferences.proxy ?? null);
        setStatus(current);
      } catch (error) {
        if (!cancelled) setMessage(`读取代理设置失败：${String(error)}`);
      }
    })();
    // 系统代理随时可能被代理客户端开关，回到窗口和每隔几秒都重新看一眼。
    const timer = window.setInterval(() => void refreshStatus(), 8000);
    window.addEventListener("focus", refreshStatus);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
      window.removeEventListener("focus", refreshStatus);
    };
  }, [refreshStatus]);

  // 没动过表单时，跟着基准走（例如系统代理换了端口）。
  const [edited, setEdited] = useState(false);
  useEffect(() => {
    if (baseline && !edited) setDraft(baseline);
  }, [baseline, edited]);

  const update = (patch: Partial<Draft>) => {
    setEdited(true);
    // 上一次保存的结果会挡住底栏里的校验提示，一编辑就收起。
    setMessage("");
    setDraft((current) => ({ ...(current ?? { scheme: "http", host: "127.0.0.1", port: "" }), ...patch }));
  };

  const save = async (value: ProxyCustom | null, done: string) => {
    setBusy(true);
    setMessage("");
    try {
      const saved = await desktopPreferences.save({ proxy: value });
      setCustom(saved.proxy ?? null);
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

  const problem = draft ? validate(draft) : "";
  const dirty = Boolean(draft && baseline && !sameDraft(draft, baseline));
  // 还没读到（或读取失败）时也按默认的「跟随系统」来描述。
  const following = !custom;

  const submit = () => {
    if (!draft || problem) return;
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

  // 系统代理开着时总说一声：跟随时说明线路从哪来，自定义时提醒它被覆盖了。
  let systemHint = "";
  if (status?.system) {
    const system = hostPort(status.system);
    if (!following) systemHint = `系统代理已启用（${system}），当前被自定义代理覆盖`;
    else if (status.source === "system" && !status.active) systemHint = `系统代理已启用（${system}），但端口没有响应，请确认代理客户端正在运行`;
    else systemHint = `系统代理已启用（${system}），应用正在跟随它`;
  }

  let footer = following ? "跟随系统代理" : "使用自定义代理";
  if (problem && edited) footer = problem;
  else if (dirty) footer = following ? "保存后改用自定义代理，不再跟随系统" : "有未保存的更改";

  return (
    <article className="panel proxy-card">
      <div className="proxy-row">
        <div className="proxy-row-label">
          <strong>当前线路</strong>
          <small>{following ? "跟随系统代理" : "自定义代理"}</small>
        </div>
        <div className="proxy-route-detail">
          <p className={`proxy-route${routeWarn ? " warn" : status?.active ? " active" : ""}`}>
            <i aria-hidden="true" />
            {route}
          </p>
          {systemHint && <small className="proxy-system-hint">{systemHint}</small>}
        </div>
      </div>

      <div className="proxy-row">
        <div className="proxy-row-label">
          <strong>代理服务器</strong>
          <small>{following ? "默认显示系统代理" : "已覆盖系统代理"}</small>
        </div>
        <div className="proxy-fields">
          <CustomSelect
            compact
            ariaLabel="代理类型"
            value={draft?.scheme ?? "http"}
            options={schemeOptions}
            disabled={busy || !draft}
            onChange={(scheme) => update({ scheme })}
          />
          <label className="proxy-host">
            <span>地址</span>
            <input
              value={draft?.host ?? ""}
              placeholder="127.0.0.1"
              spellCheck={false}
              disabled={busy || !draft}
              onChange={(event) => update({ host: event.target.value })}
              onKeyDown={(event) => event.key === "Enter" && submit()}
            />
          </label>
          <label className="proxy-port">
            <span>端口</span>
            <input
              value={draft?.port ?? ""}
              placeholder="7890"
              inputMode="numeric"
              disabled={busy || !draft}
              aria-invalid={Boolean(problem && edited)}
              onChange={(event) => update({ port: event.target.value.replace(/[^\d]/g, "") })}
              onKeyDown={(event) => event.key === "Enter" && submit()}
            />
          </label>
        </div>
      </div>

      <div className="proxy-footer">
        {message ? (
          <Notice className="proxy-footer-notice" message={message} tone={tone} onDismiss={() => setMessage("")} />
        ) : (
          <span className={problem && edited ? "warn" : undefined}>{footer}</span>
        )}
        <div className="proxy-actions">
          {custom && (
            <button type="button" className="secondary-button" disabled={busy} onClick={() => void save(null, "已恢复跟随系统代理。")}>
              恢复跟随系统
            </button>
          )}
          <button type="button" className="primary-button" disabled={busy || !dirty || Boolean(problem)} onClick={submit}>
            {busy ? "正在保存…" : "保存"}
          </button>
        </div>
      </div>
    </article>
  );
}
