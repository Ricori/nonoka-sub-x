import { useEffect, useRef, useState } from "react";
import { Service } from "../../bindings/github.com/Ricori/nonoka-x/desktop/internal/provider/index.js";
import { Notice } from "../components/Notice.tsx";
import "./KnowledgePage.css";

interface Entry { id: string; name: string; category: string; intro?: string }
interface Detail extends Entry { content: string; version: string; rev: number; warnings?: string[] }
const labels: Record<string, string> = { streamer: "主播", common: "通用", style: "翻译风格" };
const call = <T,>(request: Record<string, unknown>) => Service.Knowledge(request) as unknown as Promise<T>;

function BookIcon() {
  return <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M12 6v15M3 4h5a4 4 0 0 1 4 2 4 4 0 0 1 4-2h5v15h-5a4 4 0 0 0-4 2 4 4 0 0 0-4-2H3Z" /></svg>;
}

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
    setBusy(true); setError(""); setNotice("");
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
      <div className="knowledge-intro"><span className="knowledge-intro-icon"><BookIcon /></span><div><strong>每一次翻译，都有所积累</strong><p>整理主播资料、专有名词与翻译习惯，让熟悉的内容保持准确。</p></div></div>
      <span className="knowledge-local"><i />本机知识库</span>
    </div>
    {error && <p className="knowledge-error" role="alert">{error}</p>}
    <Notice className="knowledge-notice" message={notice} tone="success" onDismiss={() => setNotice("")} />
    {pendingID && <div className="knowledge-toolbar" role="alert"><p>当前修改尚未保存，是否放弃修改并加载条目？</p><div className="knowledge-actions"><button className="quiet-button" onClick={() => setPendingID(null)}>继续编辑</button><button className="quiet-button" onClick={() => void select(pendingID, true)}>放弃并加载</button></div></div>}
    <div className="knowledge-layout">
      <aside className="knowledge-list">
        <div className="knowledge-list-heading"><h2>知识条目 <span>{entries.length}</span></h2><button className="knowledge-refresh" title="刷新列表" aria-label="刷新知识库列表" disabled={busy} onClick={() => void refresh()}><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true"><path d="M20 7v5h-5M4 17v-5h5M5 8a8 8 0 0 1 13-3l2 3M4 16l2 3a8 8 0 0 0 13-3" /></svg></button></div>
        <label className="knowledge-search"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true"><circle cx="10" cy="10" r="7" /><path d="m15 15 6 6" /></svg><input aria-label="搜索知识库" type="search" placeholder="搜索名称或简介…" value={query} onChange={e => setQuery(e.target.value)} /></label>
        <div className="knowledge-filters" role="group" aria-label="知识分类">{[["", "全部"], ...Object.entries(labels)].map(([value, label]) => <button key={value} aria-pressed={category === value} className={category === value ? "active" : ""} onClick={() => setCategory(value)}>{label}</button>)}</div>
        <div className="knowledge-list-caption"><span>{category ? labels[category] : "全部知识"}</span><small>{visible.length} 条</small></div>
        <div className="knowledge-entries">
          {visible.map(e => <button key={e.id} disabled={busy} className={detail?.id === e.id ? "selected" : ""} onClick={() => void select(e.id)} aria-pressed={detail?.id === e.id}>
            <span className={`knowledge-entry-icon category-${e.category}`}><BookIcon /></span><span className="knowledge-entry-copy"><strong>{e.name}</strong><small className={`knowledge-category category-${e.category}`}>{labels[e.category] ?? e.category}</small>{e.intro && <span className="knowledge-entry-description">{e.intro}</span>}</span><span className="knowledge-entry-arrow" aria-hidden="true">›</span>
          </button>)}
          {loaded && !visible.length && <div className="knowledge-list-empty"><BookIcon /><strong>{entries.length ? "没有找到相关条目" : "等待第一份知识"}</strong><p>{entries.length ? "试试其他关键词或分类。" : "转写时开启“读取知识库并自动更新”，这里就会逐渐丰富起来。"}</p></div>}
        </div>
      </aside>
      <div className="knowledge-editor">
        {detail ? <>
          <div className="knowledge-editor-heading"><div><span className={`knowledge-category category-${detail.category}`}>{labels[detail.category]}</span><h2>{detail.name}</h2></div>
            <div className="knowledge-actions"><button className="quiet-button" disabled={busy} onClick={() => void select(detail.id)}>重新加载</button><button className="primary-button" disabled={busy || !dirty} onClick={() => void save()}>保存修改</button></div>
          </div>
          <div className="knowledge-document-bar"><span>条目正文 <small>MARKDOWN</small></span><span className={`knowledge-save-state${dirty ? " dirty" : ""}`}><i />{dirty ? "有未保存的修改" : "已保存"}</span></div>
          <textarea aria-label="知识库条目内容" spellCheck={false} disabled={busy} value={draft} onChange={e => setDraft(e.target.value)} onKeyDown={e => { if ((e.ctrlKey || e.metaKey) && e.key === "s") { e.preventDefault(); void save(); } }} />
          <div className="knowledge-editor-footer"><span>{draft.length.toLocaleString()} 字符 · {draft.split("\n").length} 行</span><span><kbd>Ctrl / ⌘</kbd> + <kbd>S</kbd> 保存</span></div>
          <details className="knowledge-format-help"><summary>编辑格式说明</summary><p>保留标题和章节结构。术语行格式为「原名 | 中文定名 | 别名 | 描述」，保存后将在后续翻译中使用。</p></details>
        </> : <div className="knowledge-placeholder"><div className="knowledge-empty-art"><span /><BookIcon /></div><span className="knowledge-empty-eyebrow">你的翻译记忆</span><h2>{busy ? "正在加载知识库" : "从一条知识开始"}</h2><p>{busy ? "正在读取本机保存的内容…" : "选择左侧条目，查看积累的资料，\n或补充让下一次翻译更准确的细节。"}</p><div className="knowledge-empty-tags"></div></div>}
      </div>
    </div>
  </section>;
}
