import { useCallback, useEffect, useState } from "react";
import { desktopUpdate } from "../bridge/update.ts";
import type { ReleaseNotes, UpdateStatus } from "../bridge/update.ts";
import "./UpdateCenter.css";

export const stageLabels: Record<string, string> = {
  checking: "正在检查更新…",
  download: "正在下载新版本…",
  verify: "正在校验安装包…",
  unpack: "正在准备安装…",
  ready: "新版本已就绪，退出后自动安装",
  "waiting-editor": "关闭字幕工作台后即可完成更新",
  installing: "正在安装，应用即将重启…",
  retry: "下载失败，正在重试…",
};

export interface SelfUpdate {
  status: UpdateStatus | null;
  notes: ReleaseNotes | null;
  install: () => void;
  dismissNotes: () => void;
}

/** Subscribes once to the updater lifecycle. Downloads run in the background
    and the swap happens on exit; the renderer only surfaces a staged build and
    any mandatory gate. */
export function useSelfUpdate(): SelfUpdate {
  const [status, setStatus] = useState<UpdateStatus | null>(null);
  const [notes, setNotes] = useState<ReleaseNotes | null>(null);

  useEffect(() => {
    const off = desktopUpdate.onStatus(setStatus);
    // The first status event can precede this subscription, so read the current
    // value as well rather than waiting for the next transition.
    void desktopUpdate.status().then(setStatus).catch(() => undefined);
    return off;
  }, []);

  useEffect(() => desktopUpdate.onReady(() => {
    void desktopUpdate.status().then(setStatus).catch(() => undefined);
  }), []);

  // Notes are written by the previous build and handed over exactly once, so a
  // failure here is silent: it only costs the user a changelog.
  useEffect(() => {
    void desktopUpdate.consumeReleaseNotes()
      .then((value) => { if (value?.notes) setNotes(value); })
      .catch(() => undefined);
  }, []);

  const install = useCallback(() => { void desktopUpdate.install(); }, []);
  const dismissNotes = useCallback(() => setNotes(null), []);
  return { status, notes, install, dismissNotes };
}

/** Topbar affordance for an optional update. The build is already staged and
    installs itself when the app exits, so this only offers to bring it forward. */
export function UpdateButton({ update }: { update: SelfUpdate }) {
  const { status, install } = update;
  if (!status?.ready || status.mandatory || status.stage === "installing") return null;
  return (
    <button className="update-button" type="button" title="新版本已下载完成，退出应用后自动安装；点击立即重启安装" onClick={install}>
      <svg className="btn-icon" viewBox="0 0 16 16" aria-hidden="true">
        <path d="M13 8a5 5 0 1 1-1.3-3.35M13 2.8v2.7h-2.7" />
      </svg>
      更新到 v{status.version}
    </button>
  );
}

/** Mandatory updates take over the window, and the changelog shown after a
    successful install is a plain dialog. */
export function UpdateOverlay({ update }: { update: SelfUpdate }) {
  const { status, notes, dismissNotes } = update;
  const mandatory = status?.mandatory ? status : null;
  if (mandatory) {
    const percent = mandatory.total > 0 ? Math.round(mandatory.done / mandatory.total * 100) : 0;
    const determinate = mandatory.stage === "download" && mandatory.total > 0;
    return (
      <div className="modal-backdrop update-gate" role="alertdialog" aria-modal="true" aria-label="必须更新">
        <div className="dialog">
          <h2>需要更新到 v{mandatory.version}</h2>
          <p>这是一个必需版本，Nonoka Sub X 会自动下载并重启完成安装。</p>
          <div className={`update-progress ${determinate ? "" : "indeterminate"}`} role="progressbar"
            aria-valuenow={determinate ? percent : undefined} aria-valuemin={0} aria-valuemax={100}>
            <span style={determinate ? { width: `${percent}%` } : undefined} />
          </div>
          <p className="update-stage">
            {stageLabels[mandatory.stage] ?? "正在更新…"}{determinate ? ` ${percent}%` : ""}
          </p>
        </div>
      </div>
    );
  }
  if (!notes) return null;
  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="更新说明">
      <div className="dialog">
        <h2>已更新到 v{notes.version}</h2>
        <pre className="update-notes">{notes.notes.trim()}</pre>
        <div className="dialog-actions">
          <button className="primary-button" type="button" onClick={dismissNotes}>知道了</button>
        </div>
      </div>
    </div>
  );
}
