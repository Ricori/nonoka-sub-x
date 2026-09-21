import { getStyleNames, resolveStyle } from '../ass';
import { CustomSelect } from '../../components/CustomSelect';
import {
  bindStyle, deleteTrack, newTrack, renameTarget, toggleDefaultHidden, toggleTrackHidden,
} from '../lib/edits';
import { docStore, segsOf } from '../store/docStore';
import { selStore, setActiveTrack } from '../store/selectionStore';
import { modalStore } from '../store/uiStore';
import type { Lang } from '../types';
import { PropRow, SideSection, Switch } from './ui/fields';

const NEW_STYLE = "__new_style__";

/** 给某条 lane 绑样式的下拉：轨道页和样式页共用。不出这一行靠眼睛（hidden），不靠样式 */
export function StyleSelect({ lang, value, onPick }: {
  lang: Lang; value: string | null; onPick(value: string): void;
}) {
  const names = getStyleNames();
  const effective = value && names.includes(value) ? value : resolveStyle(value || "", lang);
  const options = [
    ...names.map(name => ({ value: name, label: name })),
    { value: NEW_STYLE, label: "＋ 新增样式…" },
  ];
  return <CustomSelect className="sd-select" ariaLabel="字幕样式" value={effective} options={options}
    onChange={picked => {
      if (picked === NEW_STYLE) { modalStore.set({ tplOpen: true }); return; }
      onPick(picked);
    }} />;
}

const EyeIcon = ({ off }: { off: boolean }) => <svg viewBox="0 0 16 16" aria-hidden="true">
  <path d="M1.5 8S4 3.5 8 3.5 14.5 8 14.5 8 12 12.5 8 12.5 1.5 8 1.5 8Z" /><circle cx="8" cy="8" r="2" />
  {off && <path d="m2.5 2.5 11 11" />}
</svg>;

export function SideTrackPanel() {
  docStore.use(state => state.version);
  const curTrack = selStore.use(state => state.curTrack);
  const { tracks, trackMeta } = docStore.get();
  const isDefault = curTrack < 0;
  const track = isDefault ? null : tracks[curTrack];
  const target = isDefault ? { kind: "default" as const } : { kind: "track" as const, ti: curTrack };
  const name = isDefault ? (trackMeta?.name || "默认轨") : (track?.name || `轨道 ${curTrack + 1}`);
  const lane = (lang: Lang) => isDefault ? trackMeta?.[lang] : track?.[lang];

  const toggleHidden = (ti: number, lang: Lang) => ti < 0 ? toggleDefaultHidden(lang) : toggleTrackHidden(ti, lang);
  const laneOf = (ti: number, lang: Lang) => ti < 0 ? trackMeta?.[lang] : tracks[ti]?.[lang];

  return <div className="side-page">
    <div className="side-panel">
      <SideSection title="轨道"
        extra={<button type="button" className="side-text-btn" onClick={() => void newTrack()}>
          <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 3v10M3 8h10" /></svg>新建
        </button>}>
        <div className="trk-list" role="listbox" aria-label="轨道列表">
          {[-1, ...tracks.map((_, index) => index)].map(ti => {
            const label = ti < 0 ? (trackMeta?.name || "默认轨") : (tracks[ti]?.name || `轨道 ${ti + 1}`);
            return <div key={ti < 0 ? "default" : tracks[ti].id} role="option" aria-selected={ti === curTrack}
              className={"trk-item" + (ti === curTrack ? " on" : "")}
              onClick={() => setActiveTrack(ti, { silent: true })}>
              <span className="trk-name">{label}</span>
              <span className="trk-count">{segsOf(ti).length} 句</span>
              {(["ja", "zh"] as Lang[]).map(lang => {
                const hidden = !!laneOf(ti, lang)?.hidden;
                return <button key={lang} type="button" className={"trk-eye lang-" + lang + (hidden ? " off" : "")}
                  title={(lang === "ja" ? "原文" : "译文") + (hidden ? "：已隐藏，点击显示" : "：点击隐藏")}
                  onClick={event => { event.stopPropagation(); toggleHidden(ti, lang); }}>
                  <EyeIcon off={hidden} /><span>{lang === "ja" ? "原" : "译"}</span>
                </button>;
              })}
            </div>;
          })}
        </div>
      </SideSection>

      <SideSection title="基础">
        <PropRow label="名称">
          <input className="sd-input" spellCheck={false} value={name}
            onChange={event => renameTarget(event.target.value, target)}
            onBlur={event => renameTarget(event.target.value.trim(), target)} />
        </PropRow>
      </SideSection>

      {(["ja", "zh"] as Lang[]).map(lang => {
        const meta = lane(lang);
        return <SideSection key={lang} title={lang === "ja" ? "原文" : "译文"}
          extra={<Switch on={!meta?.hidden} title={meta?.hidden ? "显示这一行" : "隐藏这一行"}
            onToggle={() => toggleHidden(curTrack, lang)} />}>
          <PropRow label="样式">
            <StyleSelect lang={lang} value={meta?.style ?? null}
              onPick={value => bindStyle(lang, value, target)} />
          </PropRow>
        </SideSection>;
      })}

      {!isDefault && <div className="sd-foot">
        <button type="button" className="side-text-btn danger wide"
          onClick={() => void deleteTrack(curTrack)}>删除当前轨道</button>
      </div>}
    </div>
  </div>;
}
