import { useEffect, useRef } from 'react';
import { shallowEqual } from '../../home/lib/createStore';
import {
  closeFind, refreshHits, replaceAllHits, replaceCurrent, setField, setQuery, setRepl,
  stepHit, toggleAllTracks, toggleMatchCase,
} from '../lib/find';
import { CustomSelect } from '../../components/CustomSelect';
import { docStore } from '../store/docStore';
import { findStore, type FindField } from '../store/findStore';
import { selStore } from '../store/selectionStore';
import { viewStore } from '../store/viewStore';

const FIELD_OPTIONS: { value: FindField; label: string }[] = [
  { value: "both", label: "原文 + 译文" },
  { value: "ja", label: "仅原文" },
  { value: "zh", label: "仅译文" },
];

/** 字幕列表顶部的查找/替换条（Ctrl+F 开关） */
export function FindBar() {
  const f = findStore.use(s => ({
    open: s.open, query: s.query, repl: s.repl, matchCase: s.matchCase, field: s.field,
    allTracks: s.allTracks, cursor: s.cursor, total: s.hits.length, focusSeq: s.focusSeq,
  }), shallowEqual);
  const ver = docStore.use(s => s.version);
  const curTrack = selStore.use(s => s.curTrack);
  const clip = viewStore.use(s => s.curClip);
  const inputRef = useRef<HTMLInputElement>(null);

  // 文本被改（检查器编辑、替换、撤销）、换轨、进出切片，命中都得整份重算
  useEffect(() => { if (f.open) refreshHits(); }, [f.open, ver, curTrack, clip]);
  useEffect(() => { if (f.open) { inputRef.current?.focus(); inputRef.current?.select(); } }, [f.open, f.focusSeq]);

  if (!f.open) return null;

  const hasHit = f.total > 0;
  const countText = !f.query ? "" : (hasHit ? (f.cursor + 1) + "/" + f.total : "无结果");

  const icon = (d: string) => <svg viewBox="0 0 16 16" aria-hidden="true"><path d={d} /></svg>;

  return (
    <div className="find-bar" onKeyDown={e => { if (e.key === "Escape") { e.preventDefault(); closeFind(); } }}>
      <div className="find-row">
        <span className="find-box">
          <input ref={inputRef} className="find-input" placeholder="查找字幕" spellCheck={false}
            value={f.query} onChange={e => setQuery(e.target.value)}
            onKeyDown={e => { if (e.key === "Enter") { e.preventDefault(); stepHit(e.shiftKey ? -1 : 1); } }} />
          {countText && <span className={"find-count" + (hasHit ? "" : " none")}>{countText}</span>}
        </span>
        <span className="find-actions">
          <button type="button" className="fbtn icon" title="上一处 (Shift+Enter)" disabled={!hasHit}
            onClick={() => stepHit(-1)}>{icon("M4 10l4-4 4 4")}</button>
          <button type="button" className="fbtn icon" title="下一处 (Enter)" disabled={!hasHit}
            onClick={() => stepHit(1)}>{icon("M4 6l4 4 4-4")}</button>
          <button type="button" className="fbtn icon" title="关闭 (Esc)"
            onClick={closeFind}>{icon("M4.5 4.5l7 7M11.5 4.5l-7 7")}</button>
        </span>
      </div>
      <div className="find-row">
        <span className="find-box">
          <input className="find-input" placeholder="替换为" spellCheck={false}
            value={f.repl} onChange={e => setRepl(e.target.value)}
            onKeyDown={e => { if (e.key === "Enter") { e.preventDefault(); replaceCurrent(); } }} />
        </span>
        <span className="find-actions">
          <button type="button" className="fbtn" disabled={!hasHit} onClick={replaceCurrent}>替换</button>
          <button type="button" className="fbtn" disabled={!hasHit} onClick={replaceAllHits}>全部替换</button>
        </span>
      </div>
      <div className="find-row opts">
        <button type="button" className={"fbtn tog" + (f.matchCase ? " on" : "")} title="区分大小写"
          aria-pressed={f.matchCase} onClick={toggleMatchCase}>Aa</button>
        <CustomSelect compact className="find-field" ariaLabel="在哪些文本里查找" value={f.field}
          options={FIELD_OPTIONS} onChange={value => setField(value)} />
        <button type="button" className={"fbtn tog" + (f.allTracks ? " on" : "")} title="跨所有轨道查找（跳转时会自动切轨）"
          aria-pressed={f.allTracks} onClick={toggleAllTracks}>所有轨道</button>
      </div>
    </div>
  );
}
