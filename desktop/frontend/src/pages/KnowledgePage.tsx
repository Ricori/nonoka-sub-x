import { useEffect, useRef, useState } from "react";
import { Service } from "../../bindings/github.com/Ricori/nonoka-x/desktop/internal/provider/index.js";
import "./KnowledgePage.css";

interface Entry { id: string; name: string; category: string; intro?: string }
interface Detail extends Entry { content: string; version: string; rev: number; warnings?: string[] }
const labels: Record<string, string> = { streamer: "主播", common: "通用", style: "翻译风格" };
const call = <T,>(request: Record<string, unknown>) => Service.Knowledge(request) as unknown as Promise<T>;

export function KnowledgePage({ active }: { active: boolean }) {
  const [entries, setEntries] = useState<Entry[]>([]);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [draft, setDraft] = useState("");
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState("");
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [pendingID, setPendingID] = useState<string | null>(null);
  const loading = useRef(false);
  const dirty = detail !== null && draft !== detail.content;

  async function refresh() {
    if (loading.current) return;
    loading.current = true;
    setBusy(true); setError("");
    try {
      const data = await call<{ entries: Entry[] }>({ action: "list" });
      setEntries(data.entries); setLoaded(true);
    } catch (e) { setError(String(e)); }
    finally { loading.current = false; setBusy(false); }
  }
  useEffect(() => { if (active && !loaded) void refresh(); }, [active, loaded]);
  useEffect(() => {
    if (!dirty) return;
    const guard = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", guard);
    return () => window.removeEventListener("beforeunload", guard);
  }, [dirty]);

  async function select(id: string, discard = false) {
    if (busy) return;
    if (dirty && !discard) { setPendingID(id); return; }
    setPendingID(null);
    setBusy(true); setError(""); setNotice("");
    try {
      const data = await call<Detail>({ action: "read", id });
      setDetail(data); setDraft(data.content);
    } catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  }
  async function save() {
    if (!detail || busy || !dirty) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const data = await call<Detail>({ action: "save", id: detail.id, version: detail.version, content: draft });
      setDetail(data);
      setEntries(current => current.map(e => e.id === data.id ? { ...e, name: data.name } : e));
      if (data.warnings?.length) {
        setError(`部分修改未应用，当前草稿已保留：${data.warnings.join("；")}`);
      } else {
        setDraft(data.content); setNotice("已保存，下次翻译时会使用更新后的知识。");
      }
    } catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  }
  const visible = entries.filter(e => (!category || e.category === category) && `${e.name} ${e.intro ?? ""}`.toLocaleLowerCase().includes(query.toLocaleLowerCase()));
  return <section className="knowledge-page" hidden={!active} aria-label="知识库">
    <div className="knowledge-toolbar">
      <p>查看和编辑本机积累的主播、术语与翻译风格。{dirty && "当前有未保存的修改。"}</p>
      <button className="quiet-button" disabled={busy} onClick={() => void refresh()}>{busy ? "处理中…" : "刷新列表"}</button>
    </div>
    {error && <p className="knowledge-error" role="alert">{error}</p>}
    {notice && <p className="knowledge-notice" role="status">{notice}</p>}
    {pendingID && <div className="knowledge-toolbar" role="alert"><p>当前修改尚未保存，是否放弃修改并加载条目？</p><div className="knowledge-actions"><button className="quiet-button" onClick={() => setPendingID(null)}>继续编辑</button><button className="quiet-button" onClick={() => void select(pendingID, true)}>放弃并加载</button></div></div>}
    <div className="knowledge-layout">
      <aside className="knowledge-list">
        <input aria-label="搜索知识库" type="search" placeholder="搜索条目名称或简介" value={query} onChange={e => setQuery(e.target.value)} />
        <select aria-label="知识分类" value={category} onChange={e => setCategory(e.target.value)}>
          <option value="">全部分类</option>{Object.entries(labels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select>
        <small>{visible.length} 个条目</small>
        <div className="knowledge-entries">
          {visible.map(e => <button key={e.id} disabled={busy} className={detail?.id === e.id ? "selected" : ""} onClick={() => void select(e.id)} aria-pressed={detail?.id === e.id}>
            <strong>{e.name}</strong><small>{labels[e.category] ?? e.category}</small>{e.intro && <span>{e.intro}</span>}
          </button>)}
          {loaded && !visible.length && <p>{entries.length ? "没有匹配的条目" : "知识库还是空的。转写时选择“读取知识库并自动更新”，即可积累知识。"}</p>}
        </div>
      </aside>
      <div className="knowledge-editor">
        {detail ? <>
          <div className="knowledge-editor-heading"><div><h2>{detail.name}</h2><small>{labels[detail.category]} · Markdown 格式{dirty ? " · 未保存" : ""}</small></div>
            <div className="knowledge-actions"><button className="quiet-button" disabled={busy} onClick={() => void select(detail.id)}>重新加载</button><button className="primary-button" disabled={busy || !dirty} onClick={() => void save()}>保存修改</button></div>
          </div>
          <p>保留标题和章节结构；术语行格式为「原名 | 中文定名 | 别名 | 描述」。</p>
          <textarea aria-label="知识库条目内容" spellCheck={false} disabled={busy} value={draft} onChange={e => setDraft(e.target.value)} onKeyDown={e => { if ((e.ctrlKey || e.metaKey) && e.key === "s") { e.preventDefault(); void save(); } }} />
        </> : <div className="knowledge-placeholder">{busy ? "正在加载知识库…" : "选择左侧条目，查看和编辑内容"}</div>}
      </div>
    </div>
  </section>;
}
