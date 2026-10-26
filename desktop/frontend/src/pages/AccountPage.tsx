import type { CloudEntry, CloudSession } from "../bridge/cloud.ts";
import "./AccountPage.css";
import { Notice } from "../components/Notice.tsx";

interface AccountPageProps {
  session: CloudSession | null;
  media: CloudEntry[];
  loginKey: string;
  busy: boolean;
  message: string;
  onLoginKeyChange: (value: string) => void;
  onLogin: () => Promise<void>;
  onLogout: () => Promise<void>;
  onDismissMessage: () => void;
}

export function AccountPage({ session, media, loginKey, busy, message, onLoginKeyChange, onLogin, onLogout, onDismissMessage }: AccountPageProps) {
  return (
    <section className="account-layout">
      <article className="panel account-card">
        <span className="eyebrow">Key authentication</span>
        <h2>{session?.authenticated ? session.admin ? "管理员已连接" : "已连接云端服务" : "使用 Nonoka Key 登录"}</h2>
        {session?.authenticated ? (
          <>
            <dl>
              <div><dt>名称</dt><dd>{session.name || "未命名 Key"}</dd></div>
              <div><dt>服务</dt><dd>Nonoka Cloud</dd></div>
              <div><dt>剩余云端任务</dt><dd>{session.admin ? "不限次" : session.remaining ?? "—"}</dd></div>
              <div><dt>进行中</dt><dd>{session.running}</dd></div>
              <div><dt>云端视频</dt><dd>{media.length}</dd></div>
            </dl>
            <button className="danger-button" onClick={() => void onLogout()}>退出登录</button>
          </>
        ) : (
          <div className="login-form">
            <label>登录 Key<input type="password" autoComplete="off" autoCapitalize="none" autoCorrect="off" spellCheck={false} placeholder="输入分配给你的 Key" value={loginKey} onChange={(event) => onLoginKeyChange(event.target.value)} /></label>
            <button className="primary-button" disabled={busy || !loginKey.trim()} onClick={() => void onLogin()}>{busy ? "正在验证…" : "登录并同步视频库"}</button>
          </div>
        )}
        <Notice className="account-message" message={message} onDismiss={onDismissMessage} />
      </article>
      <article className="panel sync-policy">
        <span className="eyebrow">Sync policy</span>
        <h2>同步规则</h2>
        <ul>
          <li>登录后按媒体指纹合并本地和云端记录</li>
          <li>云端容器只接受最长 2 小时的纯音频</li>
        </ul>
      </article>
    </section>
  );
}
