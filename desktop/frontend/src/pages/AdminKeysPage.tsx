import { useCallback, useEffect, useState } from "react";
import { cloudAccount } from "../bridge/cloud.ts";
import type { CloudAdminKey } from "../bridge/cloud.ts";
import "./AdminKeysPage.css";
import { Notice } from "../components/Notice.tsx";

interface KeyDraft {
  name: string;
  remaining: string;
}

const plaintextKeyPattern = /^[A-Za-z0-9._~-]{1,256}$/;

function generateKey(): string {
  const bytes = new Uint8Array(18);
  crypto.getRandomValues(bytes);
  return `nonoka_${Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("")}`;
}

function errorMessage(value: unknown): string {
  return value instanceof Error ? value.message : String(value ?? "操作失败");
}

export function AdminKeysPage() {
  const [keys, setKeys] = useState<CloudAdminKey[]>([]);
  const [drafts, setDrafts] = useState<Record<string, KeyDraft>>({});
  const [name, setName] = useState("");
  const [key, setKey] = useState("");
  const [remaining, setRemaining] = useState("5");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [pendingDelete, setPendingDelete] = useState<CloudAdminKey | null>(null);

  const refresh = useCallback(async (clearMessage = true) => {
    setBusy(true);
    if (clearMessage) setMessage("");
    try {
      const next = await cloudAccount.adminKeys();
      setKeys(next);
      setDrafts(Object.fromEntries(next.map((item) => [item.id, { name: item.name, remaining: String(item.remaining) }])));
    } catch (value) {
      setMessage(errorMessage(value));
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  const create = async () => {
    const quota = Number(remaining);
    if (!name.trim() || !plaintextKeyPattern.test(key.trim()) || !Number.isInteger(quota) || quota < 0) {
      setMessage("请填写名称、1–256 位 URL 安全字符 Key 和不小于 0 的整数余量。");
      return;
    }
    setBusy(true);
    setMessage("");
    try {
      const issued = await cloudAccount.createAdminKey(name.trim(), key.trim(), quota);
      setName("");
      setKey("");
      setRemaining("5");
      const next = await cloudAccount.adminKeys();
      setKeys(next);
      setDrafts(Object.fromEntries(next.map((item) => [item.id, { name: item.name, remaining: String(item.remaining) }])));
      setMessage(`已创建「${issued.name}」，完整 Key：${issued.key}`);
    } catch (value) {
      setMessage(errorMessage(value));
    } finally {
      setBusy(false);
    }
  };

  const update = async (item: CloudAdminKey) => {
    const draft = drafts[item.id];
    const quota = Number(draft?.remaining);
    if (!draft?.name.trim() || !Number.isInteger(quota) || quota < 0) {
      setMessage("名称不能为空，余量必须是不小于 0 的整数。");
      return;
    }
    setBusy(true);
    try {
      await cloudAccount.updateAdminKey(item.id, draft.name.trim(), quota);
      setMessage(`已更新「${draft.name.trim()}」。`);
      await refresh(false);
    } catch (value) {
      setMessage(errorMessage(value));
      setBusy(false);
    }
  };

  const remove = async () => {
    const item = pendingDelete;
    if (!item) return;
    setBusy(true);
    try {
      await cloudAccount.deleteAdminKey(item.id);
      setPendingDelete(null);
      setMessage(`已删除「${item.name}」。`);
      await refresh(false);
    } catch (value) {
      setMessage(errorMessage(value));
      setBusy(false);
    }
  };

  const copy = async (value: string) => {
    if (!value) {
      setMessage("Key 为空。");
      return;
    }
    try {
      await navigator.clipboard.writeText(value);
      setMessage("完整 Key 已复制。");
    } catch {
      setMessage("复制失败，请手动选择 Key。");
    }
  };

  return (
    <section className="admin-keys-layout">
      <article className="panel admin-key-create">
        <div className="admin-key-heading"><div><h2>添加 Key</h2><p>为使用者设置名称、自定义登录 Key 与剩余云端转写次数。</p></div><button className="quiet-button" disabled={busy} onClick={() => void refresh()}>刷新</button></div>
        <div className="admin-key-form">
          <label><span>名称</span><input maxLength={100} placeholder="例如：字幕组" value={name} onChange={(event) => setName(event.target.value)} /></label>
          <label className="admin-key-secret"><span>自定义 Key</span><div><input maxLength={256} autoComplete="off" autoCapitalize="none" autoCorrect="off" spellCheck={false} placeholder="输入或生成 Key" value={key} onChange={(event) => setKey(event.target.value)} /><button className="quiet-button" type="button" onClick={() => setKey(generateKey())}>生成</button></div></label>
          <label><span>剩余次数</span><input type="number" min="0" max="1000000" step="1" value={remaining} onChange={(event) => setRemaining(event.target.value)} /></label>
          <button className="primary-button" disabled={busy} onClick={() => void create()}>{busy ? "处理中…" : "添加 Key"}</button>
        </div>
      </article>

      <Notice className="admin-key-message" message={message} onDismiss={() => setMessage("")} />

      <article className="panel admin-key-list-panel">
        <div className="admin-key-heading"><div><h2>已发放 Key</h2></div><small>共 {keys.length} 个</small></div>
        {!busy && keys.length === 0 ? <div className="admin-key-empty">还没有业务 Key</div> : (
          <div className="admin-key-list">
            {keys.map((item) => {
              const draft = drafts[item.id] ?? { name: item.name, remaining: String(item.remaining) };
              return <section className="admin-key-row" key={item.id}>
                <div className="admin-key-edit-fields">
                  <label><span>名称</span><input value={draft.name} onChange={(event) => setDrafts((current) => ({ ...current, [item.id]: { ...draft, name: event.target.value } }))} /></label>
                  <label><span>剩余次数</span><input type="number" min="0" max="1000000" step="1" value={draft.remaining} onChange={(event) => setDrafts((current) => ({ ...current, [item.id]: { ...draft, remaining: event.target.value } }))} /></label>
                </div>
                <div className="admin-key-value"><span>完整 Key</span><code>{item.key || "—"}</code><button className="quiet-button" disabled={!item.key} onClick={() => void copy(item.key)}>复制</button></div>
                <div className="admin-key-stats"><span>已提交 {item.submitted ?? 0}</span><span className={(item.running ?? 0) > 0 ? "running" : ""}>进行中 {item.running ?? 0}</span><span>创建于 {item.createdAt ? new Date(item.createdAt).toLocaleString() : "—"}</span></div>
                <div className="admin-key-actions"><button className="danger-link" disabled={busy} onClick={() => setPendingDelete(item)}>删除</button><button className="primary-button" disabled={busy} onClick={() => void update(item)}>保存修改</button></div>
              </section>;
            })}
          </div>
        )}
      </article>

      {pendingDelete && <div className="modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget && !busy) setPendingDelete(null); }}>
        <section className="dialog admin-key-delete-dialog" role="dialog" aria-modal="true" aria-labelledby="admin-key-delete-title">
          <span className="eyebrow">Revoke access</span>
          <h2 id="admin-key-delete-title">删除 Key</h2>
          <p>确定删除“{pendingDelete.name}”吗？该 Key 会立即失效，但它已经提交的视频和字幕不会被删除。</p>
          <code>{pendingDelete.key || pendingDelete.id}</code>
          <div className="dialog-actions"><button disabled={busy} onClick={() => setPendingDelete(null)}>取消</button><button className="danger-button" disabled={busy} onClick={() => void remove()}>{busy ? "正在删除…" : "确认删除"}</button></div>
        </section>
      </div>}
    </section>
  );
}
