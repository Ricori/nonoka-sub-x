import { getStyleNames, resolveStyle } from '../ass';
import {
  bindStyle, deleteTrack, newTrack, renameTarget, toggleDefaultHidden, toggleTrackHidden,
} from '../lib/edits';
import { docStore } from '../store/docStore';
import { selStore, setActiveTrack } from '../store/selectionStore';
import { modalStore } from '../store/uiStore';
import type { Lang } from '../types';

function StyleSelect({ lang, value, allowNone, onPick }: {
  lang: Lang; value: string | null; allowNone: boolean; onPick(value: string): void;
}) {
  const names = getStyleNames();
  const effective = !value && allowNone ? "" : (value && names.includes(value) ? value : resolveStyle(value || "", lang));
  return <select value={effective} onChange={event => {
    if (event.target.value === "__new__") { modalStore.set({ tplOpen: true }); return; }
    onPick(event.target.value);
  }}>
    {allowNone && <option value="">（不导出）</option>}
    {names.map(name => <option key={name} value={name}>{name}</option>)}
    <option value="__new__">＋ 新增样式…</option>
  </select>;
}

export function SideTrackPanel() {
  docStore.use(state => state.version);
  const curTrack = selStore.use(state => state.curTrack);
  const { tracks, trackMeta } = docStore.get();
  const isDefault = curTrack < 0;
  const track = isDefault ? null : tracks[curTrack];
  const target = isDefault ? { kind: "default" as const } : { kind: "track" as const, ti: curTrack };
  const name = isDefault ? (trackMeta?.name || "默认轨") : (track?.name || `轨道 ${curTrack + 1}`);
  const lane = (lang: Lang) => isDefault ? trackMeta?.[lang] : track?.[lang];

  return <div className="side-panel side-track-panel">
    <div className="side-section">
      <label className="side-field"><span>当前轨道</span>
        <select value={String(curTrack)} onChange={event => setActiveTrack(Number(event.target.value), { silent: true })}>
          <option value="-1">{trackMeta?.name || "默认轨"}</option>
          {tracks.map((item, index) => <option key={item.id} value={index}>{item.name || `轨道 ${index + 1}`}</option>)}
        </select>
      </label>
      <button type="button" className="btn side-wide-button" onClick={() => void newTrack()}>＋ 新建轨道</button>
    </div>
    <div className="side-section">
      <label className="side-field"><span>轨道名称</span>
        <input spellCheck={false} value={name}
          onChange={event => renameTarget(event.target.value, target)}
          onBlur={event => renameTarget(event.target.value.trim(), target)} />
      </label>
    </div>
    {(["ja", "zh"] as Lang[]).map(lang => {
      const meta = lane(lang);
      return <div className="side-section side-lane-settings" key={lang}>
        <div className="side-lane-heading"><strong>{lang === "ja" ? "日语原文" : "中文译文"}</strong>
          <label className="side-switch"><input type="checkbox" checked={!meta?.hidden}
            onChange={() => isDefault ? toggleDefaultHidden(lang) : toggleTrackHidden(curTrack, lang)} />显示</label>
        </div>
        <label className="side-field"><span>字幕样式</span>
          <StyleSelect lang={lang} value={meta?.style ?? null} allowNone={!isDefault}
            onPick={value => bindStyle(lang, value, target)} />
        </label>
      </div>;
    })}
    {!isDefault && <div className="side-section">
      <button type="button" className="btn danger side-wide-button"
        onClick={() => void deleteTrack(curTrack)}>删除当前轨道</button>
    </div>}
  </div>;
}
