import { useEffect, useMemo, useState } from 'react';
import {
  DEFAULT_EFFECT_TRACK_ID, EFFECT_TEMPLATES, effectTargetKey, isColorParam,
} from '../../subtitles/effects';
import type { EffectParamDefinition } from '../../subtitles/effects';
import { formatAssColor, hexOf, parseAssColor, parseCssHex } from '../../subtitles/color';
import { CustomSelect } from '../../components/CustomSelect';
import { disableEffect, enableEffect, exactEffectBinding, updateEffectParams } from '../lib/effects';
import { docStore } from '../store/docStore';
import { modalStore } from '../store/uiStore';
import type { SubtitleEffectBinding, SubtitleEffectTarget } from '../types';
import { ColorField } from './ui/ColorField';
import { PropRow, SideSection, SliderField, Switch } from './ui/fields';

interface TargetOption {
  key: string;
  label: string;
  target: SubtitleEffectTarget;
}

/** 特效参数的颜色存 #rrggbb，取色器吃 ASS 颜色，两头各转一次（特效不管不透明度） */
const toAss = (hex: string) => formatAssColor(parseCssHex(hex) ?? { r: 255, g: 255, b: 255, a: 1 });
const fromAss = (ass: string) => hexOf(parseAssColor(ass));

function ParamInput({ binding, param, onChange }: {
  binding: SubtitleEffectBinding;
  param: EffectParamDefinition;
  onChange(value: number | string): void;
}) {
  const stored = binding.params[param.key];
  if (isColorParam(param)) {
    const value = typeof stored === "string" ? stored : String(param.defaultValue);
    const swatch = value || String(param.defaultValue) || "#ffffff";
    return <PropRow label={param.label} title={param.hint}>
      <span className="sd-inline">
        <ColorField value={toAss(swatch)} palette={[]} disabled={!value}
          onInput={ass => onChange(fromAss(ass))} />
        <label className="sd-follow">
          <Switch on={!value} title="跟随样式里的颜色" onToggle={on => onChange(on ? "" : swatch)} />跟随样式
        </label>
      </span>
    </PropRow>;
  }
  return <SliderField label={param.label} value={Number(stored ?? param.defaultValue)}
    min={param.min} max={param.max} step={param.step} suffix={param.suffix}
    onInput={value => onChange(value)} />;
}

export function SideEffectsPanel() {
  const version = docStore.use(state => state.version);
  const { tracks, effects } = docStore.get();
  const [templateId, setTemplateId] = useState(EFFECT_TEMPLATES[0].id);
  const [targetKey, setTargetKey] = useState("all");

  const targets = useMemo<TargetOption[]>(() => {
    const values: TargetOption[] = [{ key: "all", label: "整个项目", target: { scope: "all" } }];
    const addTrack = (trackId: string, name: string) => {
      const track: SubtitleEffectTarget = { scope: "track", trackId };
      const ja: SubtitleEffectTarget = { scope: "lane", trackId, lang: "ja" };
      const zh: SubtitleEffectTarget = { scope: "lane", trackId, lang: "zh" };
      values.push(
        { key: effectTargetKey(track), label: `${name} · 双语`, target: track },
        { key: effectTargetKey(ja), label: `${name} · 原文`, target: ja },
        { key: effectTargetKey(zh), label: `${name} · 译文`, target: zh },
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
  const onHere = (id: string) => !!exactEffectBinding(id, activeTarget.target);

  return <div className="side-page">
    <div className="side-head">
      <span className="side-head-label">应用到</span>
      <CustomSelect compact className="side-track-select" ariaLabel="应用范围" value={activeTarget.key}
        options={targets.map(option => ({ value: option.key, label: option.label }))} onChange={setTargetKey} />
    </div>
    <div className="side-panel">
      <SideSection title="特效">
        <div className="fx-grid">
          {EFFECT_TEMPLATES.map(item => {
            const count = templateCount(item.id);
            return <button key={item.id} type="button" title={item.description}
              className={"fx-card" + (item.id === template.id ? " on" : "")} onClick={() => setTemplateId(item.id)}>
              <span className={"fx-thumb fx-thumb-" + item.id}><b>Aa</b><i /><i /><i /></span>
              <span className="fx-card-name">{item.name}</span>
              {onHere(item.id) && <span className="fx-card-badge" title="当前范围已启用">✓</span>}
              {!onHere(item.id) && count > 0 && <span className="fx-card-count" title="在其他范围启用">{count}</span>}
            </button>;
          })}
        </div>
      </SideSection>

      <SideSection title={template.name}
        extra={<Switch on={!!binding} title={binding ? "关闭此范围的特效" : "对此范围启用"}
          onToggle={on => on
            ? enableEffect(template.id, activeTarget.target)
            : disableEffect(template.id, activeTarget.target)} />}>
        <p className="sd-desc">{template.description}</p>
        {template.needsKaraoke && <button type="button" className="side-text-btn wide"
          onClick={() => modalStore.set({ karaokeOpen: true })}>编辑 K 轴逐字时间…</button>}
        {binding
          ? template.params.map(param => <ParamInput key={`${binding.id}:${param.key}`}
            binding={binding} param={param}
            onChange={value => updateEffectParams(template.id, activeTarget.target, { [param.key]: value })} />)
          : <div className="side-empty small">打开右上角开关后可调整参数</div>}
      </SideSection>
    </div>
  </div>;
}
