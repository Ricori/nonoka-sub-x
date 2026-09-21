import { useEffect, useMemo, useState } from 'react';
import {
  DEFAULT_EFFECT_TRACK_ID, EFFECT_TEMPLATES, effectTargetKey, isColorParam,
} from '../../subtitles/effects';
import type { EffectParamDefinition } from '../../subtitles/effects';
import { disableEffect, enableEffect, exactEffectBinding, updateEffectParams } from '../lib/effects';
import { docStore } from '../store/docStore';
import { modalStore } from '../store/uiStore';
import type { SubtitleEffectBinding, SubtitleEffectTarget } from '../types';

interface TargetOption {
  key: string;
  label: string;
  target: SubtitleEffectTarget;
}

function ParamInput({ binding, param, onChange }: {
  binding: SubtitleEffectBinding;
  param: EffectParamDefinition;
  onChange(value: number | string): void;
}) {
  const stored = binding.params[param.key];
  if (isColorParam(param)) {
    const value = typeof stored === "string" ? stored : String(param.defaultValue);
    const swatch = value || String(param.defaultValue) || "#ffffff";
    return <label className="side-field">
      <span>{param.label}</span>
      <span className="side-color-field">
        <input type="color" value={swatch} disabled={!value}
          onChange={event => onChange(event.target.value)} />
        <label><input type="checkbox" checked={!value}
          onChange={event => onChange(event.target.checked ? "" : swatch)} /> 跟随样式</label>
      </span>
    </label>;
  }
  const value = Number(stored ?? param.defaultValue);
  return <label className="side-field">
    <span>{param.label}</span>
    <span className="side-number-field">
      <input type="number" min={param.min} max={param.max} step={param.step} value={value}
        onChange={event => onChange(Math.min(param.max, Math.max(param.min, Number(event.target.value))))} />
      <i>{param.suffix}</i>
    </span>
  </label>;
}

export function SideEffectsPanel() {
  const version = docStore.use(state => state.version);
  const { tracks, effects } = docStore.get();
  const [templateId, setTemplateId] = useState(EFFECT_TEMPLATES[0].id);
  const [targetKey, setTargetKey] = useState("all");

  const targets = useMemo<TargetOption[]>(() => {
    const values: TargetOption[] = [{ key: "all", label: "整个项目 · 全部字幕轴", target: { scope: "all" } }];
    const addTrack = (trackId: string, name: string) => {
      const track: SubtitleEffectTarget = { scope: "track", trackId };
      const ja: SubtitleEffectTarget = { scope: "lane", trackId, lang: "ja" };
      const zh: SubtitleEffectTarget = { scope: "lane", trackId, lang: "zh" };
      values.push(
        { key: effectTargetKey(track), label: `${name} · 双语`, target: track },
        { key: effectTargetKey(ja), label: `${name} · 日语原文`, target: ja },
        { key: effectTargetKey(zh), label: `${name} · 中文译文`, target: zh },
      );
    };
    addTrack(DEFAULT_EFFECT_TRACK_ID, "默认轨");
    tracks.forEach((track, index) => addTrack(track.id || `track-${index + 1}`, track.name || `轨道 ${index + 1}`));
    return values;
  }, [tracks, version]);

  useEffect(() => {
    if (!targets.some(option => option.key === targetKey)) setTargetKey("all");
  }, [targets, targetKey]);

  const template = EFFECT_TEMPLATES.find(item => item.id === templateId) ?? EFFECT_TEMPLATES[0];
  const activeTarget = targets.find(option => option.key === targetKey) ?? targets[0];
  const binding = exactEffectBinding(template.id, activeTarget.target);
  const templateCount = (id: string) => effects.filter(effect => effect.templateId === id && effect.enabled).length;

  return <div className="side-panel side-effects-panel">
    <div className="side-section">
      <label className="side-field"><span>应用范围</span>
        <select value={activeTarget.key} onChange={event => setTargetKey(event.target.value)}>
          {targets.map(option => <option key={option.key} value={option.key}>{option.label}</option>)}
        </select>
      </label>
    </div>
    <div className="side-section">
      <span className="side-section-title">模板</span>
      <div className="side-template-list">
        {EFFECT_TEMPLATES.map(item => <button key={item.id} type="button"
          className={item.id === template.id ? "on" : ""} onClick={() => setTemplateId(item.id)}>
          <span>{item.name}</span><small>{templateCount(item.id) ? `${templateCount(item.id)} 个范围` : "未使用"}</small>
        </button>)}
      </div>
    </div>
    <div className="side-section side-effect-settings">
      <div className="side-effect-heading"><strong>{template.name}</strong>
        <label className="side-switch"><input type="checkbox" checked={!!binding}
          onChange={event => event.target.checked
            ? enableEffect(template.id, activeTarget.target)
            : disableEffect(template.id, activeTarget.target)} />启用</label>
      </div>
      <p>{template.description}</p>
      {binding && <div className="side-param-list">
        {template.params.map(param => <ParamInput key={`${binding.id}:${param.key}`}
          binding={binding} param={param}
          onChange={value => updateEffectParams(template.id, activeTarget.target, { [param.key]: value })} />)}
      </div>}
      {template.needsKaraoke && <button type="button" className="btn side-wide-button"
        onClick={() => modalStore.set({ karaokeOpen: true })}>打开 K 轴逐字时间面板</button>}
    </div>
  </div>;
}
