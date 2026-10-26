import { useEffect, useMemo, useState } from 'react';
import {
  DEFAULT_EFFECT_TRACK_ID, EFFECT_TEMPLATES, effectTargetKey, isColorParam,
} from '../../../subtitles/effects.ts';
import type {
  EffectColorParam, EffectNumberParam, EffectParamDefinition,
} from '../../../subtitles/effects.ts';
import { disableEffect, enableEffect, exactEffectBinding, updateEffectParams } from '../../lib/effects';
import { karaokeSummary, karaokeTracks } from '../../lib/karaoke';
import { docStore } from '../../store/docStore';
import { modalStore } from '../../store/uiStore';
import type {
  SubtitleEffectBinding, SubtitleEffectTarget,
} from '../../types';

interface TargetOption {
  key: string;
  group: string;
  label: string;
  target: SubtitleEffectTarget;
}

function EffectNumberInput({ binding, param, onChange }: {
  binding: SubtitleEffectBinding;
  param: EffectNumberParam;
  onChange(value: number): void;
}) {
  const value = Number(binding.params[param.key] ?? param.defaultValue);
  const [text, setText] = useState(String(value));
  useEffect(() => { setText(String(value)); }, [binding.id, param.key]);
  const finish = () => {
    const numeric = Number(text || param.defaultValue);
    const normalized = Math.min(param.max, Math.max(param.min, Math.round(numeric || 0)));
    setText(String(normalized));
    onChange(normalized);
  };
  return <label className="fx-param">
    <span>{param.label}</span>
    <span className="fx-param-control">
      <input type="number" min={param.min} max={param.max} step={param.step} value={text}
        onChange={event => {
          const next = event.target.value;
          setText(next);
          if (next !== "") onChange(Number(next));
        }}
        onBlur={finish} />
      <i>{param.suffix}</i>
    </span>
  </label>;
}

/** 颜色参数：勾掉「跟随样式」才写颜色标签，于是同一个模板换个样式也不会被顶掉配色 */
function EffectColorInput({ binding, param, onChange }: {
  binding: SubtitleEffectBinding;
  param: EffectColorParam;
  onChange(value: string): void;
}) {
  const stored = binding.params[param.key];
  const value = typeof stored === "string" ? stored : String(param.defaultValue);
  const swatch = value || String(param.defaultValue) || "#ffffff";
  return <label className="fx-param" title={param.hint}>
    <span>{param.label}</span>
    <span className="fx-param-control fx-param-color">
      <input type="color" value={swatch} disabled={!value}
        onChange={event => onChange(event.target.value)} />
      <em>
        <input type="checkbox" checked={!value}
          onChange={event => onChange(event.target.checked ? "" : swatch)} />
        跟随样式
      </em>
    </span>
  </label>;
}

function EffectParamInput({ binding, param, onChange }: {
  binding: SubtitleEffectBinding;
  param: EffectParamDefinition;
  onChange(value: number | string): void;
}) {
  return isColorParam(param)
    ? <EffectColorInput binding={binding} param={param} onChange={onChange} />
    : <EffectNumberInput binding={binding} param={param} onChange={onChange} />;
}

export function EffectsModal() {
  const open = modalStore.use(state => state.effectsOpen);
  const version = docStore.use(state => state.version);
  const { tracks, effects } = docStore.get();
  const [templateId, setTemplateId] = useState(EFFECT_TEMPLATES[0].id);
  const [targetKey, setTargetKey] = useState("all");

  const targets = useMemo<TargetOption[]>(() => {
    const values: TargetOption[] = [{ key: "all", group: "整个项目", label: "全部字幕轴", target: { scope: "all" } }];
    const addTrack = (trackId: string, name: string) => {
      const trackTarget: SubtitleEffectTarget = { scope: "track", trackId };
      const jaTarget: SubtitleEffectTarget = { scope: "lane", trackId, lang: "ja" };
      const zhTarget: SubtitleEffectTarget = { scope: "lane", trackId, lang: "zh" };
      values.push(
        { key: effectTargetKey(trackTarget), group: name, label: "整条轨道（双语）", target: trackTarget },
        { key: effectTargetKey(jaTarget), group: name, label: "原文轴", target: jaTarget },
        { key: effectTargetKey(zhTarget), group: name, label: "中文译文轴", target: zhTarget },
      );
    };
    addTrack(DEFAULT_EFFECT_TRACK_ID, "默认轨");
    tracks.forEach((track, index) => addTrack(track.id, track.name || `轨道 ${index + 1}`));
    return values;
  }, [tracks, version]);

  useEffect(() => {
    if (!targets.some(option => option.key === targetKey)) setTargetKey("all");
  }, [targets, targetKey]);

  if (!open) return <div className="modal" hidden />;
  const template = EFFECT_TEMPLATES.find(item => item.id === templateId) ?? EFFECT_TEMPLATES[0];
  const activeTarget = targets.find(option => option.key === targetKey) ?? targets[0];
  const binding = exactEffectBinding(template.id, activeTarget.target);
  const grouped = new Map<string, TargetOption[]>();
  targets.forEach(option => grouped.set(option.group, [...(grouped.get(option.group) ?? []), option]));

  const close = () => modalStore.set({ effectsOpen: false });
  // 只框住译文的范围拿不到 K 轴，别在这里摆一份关于原文的统计和入口
  const translationOnly = activeTarget.target.scope === "lane" && activeTarget.target.lang === "zh";
  // 覆盖率按所有轨合计：绑定范围可以是「全部字幕轴」，只报一条轨会误导
  const coverage = karaokeTracks().map(karaokeSummary)
    .reduce((sum, item) => ({
      total: sum.total + item.total, ok: sum.ok + item.ok, readable: sum.readable + item.readable,
    }), { total: 0, ok: 0, readable: 0 });
  const karaokeCoverage = coverage.total
    ? `${coverage.ok} / ${coverage.total} 句已有 K 轴`
    + (coverage.readable ? `，另有 ${coverage.readable} 句可从中间产物读取` : "")
    : "还没有带原文的句子。";
  const templateCount = (id: string) => effects.filter(effect => effect.templateId === id && effect.enabled).length;

  return <div className="modal fx-modal" onMouseDown={event => {
    if (event.target === event.currentTarget) close();
  }} onKeyDown={event => {
    if (event.key === "Escape") { event.preventDefault(); close(); }
    event.stopPropagation();
  }}>
    <div className="box fx-box">
      <button className="x-close" title="关闭" onClick={close}>✕</button>
      <div className="fx-heading">
        <div><h3>特效字幕</h3><p>统一管理所有字幕轴；更具体的绑定会覆盖上级范围。</p></div>
      </div>
      <div className="fx-workspace">
        <aside className="fx-templates">
          <strong>模板库</strong>
          {EFFECT_TEMPLATES.map(item => <button key={item.id}
            className={item.id === template.id ? "on" : ""}
            onClick={() => setTemplateId(item.id)}>
            <span>{item.name}</span><small>{templateCount(item.id)} 个绑定</small>
          </button>)}
        </aside>

        <section className="fx-targets">
          <strong>应用范围</strong>
          <div className="fx-target-scroll">
            {[...grouped.entries()].map(([group, options]) => <div className="fx-target-group" key={group}>
              <h4>{group}</h4>
              {options.map(option => {
                const exact = exactEffectBinding(template.id, option.target);
                return <label key={option.key} className={option.key === activeTarget.key ? "active" : ""}
                  onClick={() => setTargetKey(option.key)}>
                  <input type="checkbox" checked={!!exact}
                    onClick={event => event.stopPropagation()}
                    onChange={event => {
                      setTargetKey(option.key);
                      if (event.target.checked) enableEffect(template.id, option.target);
                      else disableEffect(template.id, option.target);
                    }} />
                  <span>{option.label}</span>
                </label>;
              })}
            </div>)}
          </div>
        </section>

        <section className="fx-settings">
          <strong>{template.name}</strong>
          <p>{template.description}</p>
          <div className="fx-current-target">当前范围：{activeTarget.group} · {activeTarget.label}</div>
          {!binding ? <div className="fx-empty-setting">
            <span>此范围尚未启用该模板。</span>
            <button className="btn primary" onClick={() => enableEffect(template.id, activeTarget.target)}>启用模板</button>
          </div> : <>
            <div className="fx-param-list">
              {template.params.map(param => <EffectParamInput key={`${binding.id}:${param.key}`}
                binding={binding} param={param}
                onChange={value => updateEffectParams(template.id, activeTarget.target, { [param.key]: value })} />)}
            </div>
            <button className="btn danger fx-remove"
              onClick={() => disableEffect(template.id, activeTarget.target)}>从此范围移除</button>
          </>}
          {template.needsKaraoke && (translationOnly
            ? <div className="fx-karaoke">
              <small>K 轴暂时只能 K 原文</small>
            </div>
            : <div className="fx-karaoke">
              <div className="fx-karaoke-head">
                <strong>K 轴（逐字时间）</strong>
                <button className="btn" onClick={() => modalStore.set({ karaokeOpen: true })}>打开 K 轴面板</button>
              </div>
              <p>{karaokeCoverage}</p>
              <small>这个模板按 K 轴决定每个字什么时候出现；没有 K 轴的句子退回按字数均分。</small>
            </div>)}
          {template.kind === "generator" && <div className="fx-warning">
            生成型特效会把每个字符展开为主体和粒子事件；字符越多、粒子数越高，预览刷新越慢。
          </div>}
        </section>
      </div>
      <div className="fx-foot">
        <span>{effects.filter(effect => effect.enabled).length} 个特效绑定</span>
        <button className="btn primary" onClick={close}>完成</button>
      </div>
    </div>
  </div>;
}
