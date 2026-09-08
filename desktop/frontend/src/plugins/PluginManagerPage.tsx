import { useState } from "react";
import type { InstalledPlugin } from "../bridge/plugins.ts";
import { Notice } from "../components/Notice.tsx";
import "./plugins.css";

interface PluginManagerPageProps {
  plugins: InstalledPlugin[];
  busy: string;
  message: string;
  onInstall: () => Promise<void>;
  onToggle: (plugin: InstalledPlugin) => Promise<void>;
  onUninstall: (plugin: InstalledPlugin, removeData: boolean) => Promise<void>;
  onOpenTool: (pluginId: string, toolId: string) => void;
  onDismissMessage: () => void;
}

export function PluginManagerPage(props: PluginManagerPageProps) {
  const { plugins, busy, message, onInstall, onToggle, onUninstall, onDismissMessage } = props;
  const [pendingUninstall, setPendingUninstall] = useState<{ plugin: InstalledPlugin; removeData: boolean } | null>(null);
  const uninstallBusy = pendingUninstall !== null && busy === pendingUninstall.plugin.id;
  const systemPlugins = plugins.filter((plugin) => plugin.system);
  const userPlugins = plugins.filter((plugin) => !plugin.system);

  const renderCard = (plugin: InstalledPlugin) => {
    const working = busy === plugin.id;
    return (
      <article className={`panel plugin-card ${plugin.enabled ? "" : "disabled"}`} key={plugin.id}>
        <div className="plugin-card-mark">{plugin.name.trim().charAt(0).toUpperCase() || "P"}</div>
        <div className="plugin-card-copy">
          <div className="plugin-card-title">
            <h3>{plugin.name}</h3>
            <span>v{plugin.version}{!plugin.system && ` · ${plugin.publisher || plugin.id}`}</span>
            {plugin.system && <i className="system">内置</i>}
            <i className={plugin.enabled ? "enabled" : ""}>{plugin.enabled ? "已启用" : "已停用"}</i>
          </div>
          <p>{plugin.description || `${plugin.contributes.tools?.length ?? 0} 个工具`}</p>
          {(plugin.permissions?.length ?? 0) > 0 && (
            <div className="plugin-permissions" title="插件申请的宿主能力">
              {plugin.permissions?.map((permission) => <span key={permission}>{permissionLabel(permission)}</span>)}
            </div>
          )}
        </div>
        <div className="plugin-card-actions">
          <button className="quiet-button" disabled={working} onClick={() => void onToggle(plugin)}>{working ? "请稍候…" : plugin.enabled ? "停用" : "启用"}</button>
          {/* 内置插件是程序的一部分，卸载没有意义，只能停用。 */}
          {!plugin.system && (
            <button className="danger-text-button" disabled={working} onClick={() => setPendingUninstall({ plugin, removeData: false })}>卸载</button>
          )}
        </div>
      </article>
    );
  };

  const confirmUninstall = async () => {
    if (!pendingUninstall) return;
    await onUninstall(pendingUninstall.plugin, pendingUninstall.removeData);
    setPendingUninstall(null);
  };

  return (
    <section className="plugin-manager-layout">
      <Notice className="plugin-message" message={message} tone={message.includes("失败") ? "warn" : "success"} onDismiss={onDismissMessage} />
      <article className="panel plugin-manager-hero">
        <div>
          <span className="eyebrow">扩展 Nonoka Sub X</span>
          <h2>插件管理</h2>
          <p>安装的插件会挂载在左侧“工具”板块。停用会隐藏入口，但保留插件和数据。</p>
        </div>
        <button className="primary-button" disabled={busy !== ""} onClick={() => void onInstall()}>
          {busy === "install" ? "正在安装…" : "＋ 安装插件"}
        </button>
      </article>

      {systemPlugins.length > 0 && (
        <>
          <h3 className="plugin-group-title">内置插件</h3>
          <div className="plugin-card-list">{systemPlugins.map(renderCard)}</div>
        </>
      )}

      <h3 className="plugin-group-title">已安装的插件<small>你自己安装的 .nonoka-plugin 包</small></h3>
      {userPlugins.length === 0 ? (
        <article className="panel plugin-empty">
          <span className="plugin-empty-icon">◇</span>
          <h3>还没有安装插件</h3>
          <p>选择一个 <code>.nonoka-plugin</code> 文件，安装后工具入口会立即出现在左侧。</p>
          <button className="quiet-button" disabled={busy !== ""} onClick={() => void onInstall()}>选择插件包</button>
        </article>
      ) : (
        <div className="plugin-card-list">{userPlugins.map(renderCard)}</div>
      )}

      {pendingUninstall && (
        <div className="modal-backdrop" role="presentation" onMouseDown={(event) => {
          if (event.target === event.currentTarget && !uninstallBusy) setPendingUninstall(null);
        }}>
          <section className="dialog plugin-uninstall-dialog" role="alertdialog" aria-modal="true" aria-labelledby="plugin-uninstall-title">
            <span className="eyebrow">Uninstall plugin</span>
            <h2 id="plugin-uninstall-title">卸载“{pendingUninstall.plugin.name}”</h2>
            <p>插件会从左侧工具栏移除并删除已安装的程序文件。你可以选择是否同时删除它保存的设置和数据。</p>
            <label className="dialog-check plugin-remove-data">
              <input
                type="checkbox"
                checked={pendingUninstall.removeData}
                disabled={uninstallBusy}
                onChange={(event) => setPendingUninstall({ ...pendingUninstall, removeData: event.target.checked })}
              />
              <span><strong>同时删除插件数据</strong><small>包括该插件保存的设置和工作数据，此操作无法撤销。</small></span>
            </label>
            <div className="dialog-actions">
              <button disabled={uninstallBusy} onClick={() => setPendingUninstall(null)}>取消</button>
              <button className="danger-button" disabled={uninstallBusy} onClick={() => void confirmUninstall()}>{uninstallBusy ? "正在卸载…" : "确认卸载"}</button>
            </div>
          </section>
        </div>
      )}
    </section>
  );
}

const PERMISSION_LABELS: Record<string, string> = {
  "media.list": "读取媒体列表",
  "media.import": "导入媒体库",
  "media.export-video": "压制字幕导出视频",
  "document.read": "读取字幕文档",
  "document.write": "修改字幕文档",
  "subtitle.export": "导出字幕文件",
  "tools.yt-dlp": "使用受控的 yt-dlp",
  "tools.cookies": "保存登录 Cookie",
  "ffmpeg.extract-audio": "使用 FFmpeg 导出音频",
  "state.persist": "保存插件私有状态",
};

function permissionLabel(permission: string): string {
  return PERMISSION_LABELS[permission] ?? permission;
}
