import { useEffect, useMemo, useState } from 'react';
import { shallowEqual } from '../../../home/lib/createStore';
import { CustomSelect } from '../../../components/CustomSelect';
import { BUNDLED_FONTS } from '../../constants';
import { parseSheet } from '../../../subtitles/styles';
import { fontsMissing, getPlayRes, getStyleNames } from '../../ass';
import { video } from '../../lib/media';
import { boxForLane } from '../../lib/stageHit';
import type { LaneRef } from '../../lib/stageHit';
import {
  cloneStyleForLane, materializeMissingStyle, patchStyleFromPanel, stylesInUse, styleFields,
} from '../../lib/styleEdit';
import { DEFAULT_EFFECT_TRACK_ID } from '../../../subtitles/effects';
import { bindStyle } from '../../lib/edits';
import { docStore } from '../../store/docStore';
import { askModal, modalStore, toast } from '../../store/uiStore';
import { PropRow, Segmented, SideSection, SliderField, ToggleChip } from '../ui/fields';
import { ColorField } from '../ui/ColorField';
import { StyleSelect } from '../SideTrackPanel';
import { ignoresMarginV } from '../../../subtitles/layout';

/**
 * 右栏「样式」页：覆盖 ASS 的全部 23 个字段，按 CapCut 的分节排版
 * （基础 / 颜色 / 描边 / 阴影 / 变换 / 边距），次要的几节默认收起。
 *
 * 读值一律走 styleFields（23 字段原文），绝不走 AssStyle——后者丢了 SecondaryColour /
 * Underline / StrikeOut / Angle / BorderStyle / Encoding，正好是这里要编辑的一大半。
 */

const num = (fields: Record<string, string>, key: string, fallback = 0) => {
  const value = Number(fields[key]);
  return Number.isFinite(value) ? value : fallback;
};
const flag = (fields: Record<string, string>, key: string) => num(fields, key) !== 0;
/** Aegisub 写 -1，libass 认非零；写 -1 是为了样式表在 Aegisub 里打开时干净 */
const flagValue = (on: boolean) => (on ? -1 : 0);

const OTHER_FONT = "__other_font__";

/** ASS 的 Alignment 是小键盘方位（1–9），拆成水平 × 垂直两组按钮更好点 */
const H_ALIGN = [
  { value: 0, title: "左对齐", label: <svg viewBox="0 0 16 16"><path d="M2.5 3.5h11M2.5 6.5h7M2.5 9.5h11M2.5 12.5h7" /></svg> },
  { value: 1, title: "居中", label: <svg viewBox="0 0 16 16"><path d="M2.5 3.5h11M4.5 6.5h7M2.5 9.5h11M4.5 12.5h7" /></svg> },
  { value: 2, title: "右对齐", label: <svg viewBox="0 0 16 16"><path d="M2.5 3.5h11M6.5 6.5h7M2.5 9.5h11M6.5 12.5h7" /></svg> },
];
const V_ALIGN = [
  { value: 2, title: "顶部", label: <svg viewBox="0 0 16 16"><path d="M2.5 2.5h11M6 5.5h4v8H6z" /></svg> },
  { value: 1, title: "垂直居中", label: <svg viewBox="0 0 16 16"><path d="M2.5 8h11M6 4h4v8H6z" /></svg> },
  { value: 0, title: "底部", label: <svg viewBox="0 0 16 16"><path d="M2.5 13.5h11M6 2.5h4v8H6z" /></svg> },
];

/**
 * lane 由样式页自己管（当前轨 + 原文/译文），不跟画面里的选中框绑死：
 * 点别处取消了选中，面板照样能改；点画面里的字幕时样式页会跟过来。
 */
export function StyleBar({ lane }: { lane: LaneRef }) {
  const { trackId, lang } = lane;
  const { styles, version } = docStore.use(
    s => ({ styles: s.styles, version: s.version }), shallowEqual);
  const [missing, setMissing] = useState<string[]>([]);

  const box = trackId ? boxForLane({ trackId, lang }) : null;
  const name = box?.styleName ?? "";
  const fields = useMemo(() => (name ? styleFields(name) : null), [name, styles, version]);

  /** 这份样式表里出现过的所有颜色，给取色器当调色板 */
  const palette = useMemo(() => {
    const seen = new Set<string>();
    for (const style of Object.values(parseSheet(styles).fields)) {
      for (const key of ["primarycolour", "secondarycolour", "outlinecolour", "backcolour"]) {
        const raw = style[key];
        if (raw) seen.add(raw);
      }
    }
    return [...seen];
  }, [styles]);

  const bindings = useMemo(() => (name ? stylesInUse(name) : []), [name, styles, version]);
  // 缺字检测是异步的（@font-face 懒加载，没先 load 一遍量到的是回退字体）
  const fontName = fields?.fontname ?? "";
  useEffect(() => {
    if (!fontName) { setMissing([]); return; }
    let alive = true;
    fontsMissing([fontName])
      .then(names => { if (alive) setMissing(names); })
      .catch(() => { if (alive) setMissing([]); });
    return () => { alive = false; };
  }, [fontName]);

  const fontOptions = useMemo(() => {
    const names = [...new Set([...BUNDLED_FONTS, fontName, ...Object.values(parseSheet(styles).fields)
      .map(style => style.fontname)].filter(Boolean))];
    return [
      ...names.map(font => ({ value: font, label: font })),
      { value: OTHER_FONT, label: "其他字体…", hint: "输入系统里已安装的字体名" },
    ];
  }, [styles, fontName]);

  if (!box || !fields) {
    return <div className="side-empty">在画面里点选一行字幕，或在上方切换轨道</div>;
  }

  const set = (patch: Record<string, string | number>) => patchStyleFromPanel(name, patch);
  const align = num(fields, "alignment", 2);
  const hAlign = (align - 1) % 3;
  const vAlign = Math.floor((align - 1) / 3);
  const play = getPlayRes();
  const media = video();
  // 绑定写的名字这份样式表里没有，正按 JP/CN 顶着用
  const unbound = box.boundStyle && !getStyleNames().includes(box.boundStyle) ? box.boundStyle : null;
  const shared = bindings.filter(binding => binding.direct).length > 1
    || bindings.some(binding => !binding.direct);

  // 这条 lane 在绑定上对应哪个目标：默认轨存在 trackMeta 里，自定义轨按 id 找下标
  const laneTarget = box.trackId === DEFAULT_EFFECT_TRACK_ID
    ? { kind: "default" as const }
    : {
      kind: "track" as const,
      ti: docStore.get().tracks.findIndex((track, index) => (track.id || `track-${index + 1}`) === box.trackId),
    };

  async function pickOtherFont() {
    const typed = await askModal({ title: "使用其他字体", hint: "填系统里已安装的字体名", value: fontName, okLabel: "使用" });
    if (typeof typed === "string" && typed.trim()) set({ fontname: typed.trim() });
  }

  return (
    <div className="style-bar">
      <SideSection title="样式">
        <PropRow label="轨道样式" title="这条轨的这一行用哪个样式；下面的改动改的是这个样式本身">
          <StyleSelect lang={box.lang} value={box.boundStyle} allowNone={laneTarget.kind === "track"}
            onPick={value => bindStyle(box.lang, value, laneTarget)} />
        </PropRow>
        {bindings.length > 1 && (
          <div className={"sd-shared" + (shared ? " warn" : "")}>
            <span title={bindings.map(b => `${b.trackName} · ${b.lang === "ja" ? "原文" : "译文"}`).join("\n")}>
              {bindings.length} 条轨共用「{name}」，改动会一起生效
            </span>
            <button type="button" className="side-text-btn" title="改成只作用于这条轨道，之后拖动不再影响别人"
              onClick={() => {
                const created = cloneStyleForLane(box.trackId, box.lang);
                if (created) toast("已复制专属样式「" + created + "」并改绑");
              }}>复制专属样式</button>
          </div>
        )}
      </SideSection>

      {unbound && (
        <div className="sb-note">
          <span>这条轨绑的是样式表里没有的「{unbound}」，正按 {name} 显示</span>
          <button type="button" className="side-text-btn" onClick={() => {
            const created = materializeMissingStyle(box.trackId, box.lang);
            if (created) toast("已用当前样子新建样式「" + created + "」");
          }}>用当前样子新建「{unbound}」</button>
        </div>
      )}

      <SideSection title="基础">
        <PropRow label="字体" title={missing.length ? "系统里没装这个字体，预览会用回退字体" : undefined}>
          <CustomSelect className={"sd-select" + (missing.length ? " bad" : "")} ariaLabel="字体"
            value={fontName} options={fontOptions}
            onChange={value => { if (value === OTHER_FONT) void pickOtherFont(); else set({ fontname: value }); }} />
        </PropRow>
        <SliderField label="字号" value={num(fields, "fontsize", 70)} min={8} max={400} rangeMax={200}
          onInput={value => set({ fontsize: value })} />
        <PropRow label="样式">
          <span className="sd-chips">
            <ToggleChip label="B" title="粗体" on={flag(fields, "bold")}
              onToggle={on => set({ bold: flagValue(on) })} />
            <ToggleChip label="I" title="斜体" on={flag(fields, "italic")}
              onToggle={on => set({ italic: flagValue(on) })} />
            <ToggleChip label="U" title="下划线" on={flag(fields, "underline")}
              onToggle={on => set({ underline: flagValue(on) })} />
            <ToggleChip label="S" title="删除线" on={flag(fields, "strikeout")}
              onToggle={on => set({ strikeout: flagValue(on) })} />
          </span>
        </PropRow>
        <PropRow label="对齐方式">
          <span className="sd-align">
            <Segmented value={hAlign} options={H_ALIGN}
              onPick={value => set({ alignment: vAlign * 3 + value + 1 })} />
            <Segmented value={vAlign} options={V_ALIGN}
              onPick={value => set({ alignment: value * 3 + hAlign + 1 })} />
          </span>
        </PropRow>
      </SideSection>

      <SideSection title="颜色">
        <PropRow label="文字">
          <ColorField value={fields.primarycolour} palette={palette}
            onInput={value => set({ primarycolour: value })} />
        </PropRow>
        <PropRow label="卡拉 OK" title="卡拉 OK 里还没唱到的那一截用它——没有 K 轴的话画面上看不出变化">
          <ColorField value={fields.secondarycolour} palette={palette}
            onInput={value => set({ secondarycolour: value })} />
        </PropRow>
      </SideSection>

      <SideSection title="描边" collapsible>
        <PropRow label="颜色">
          <ColorField value={fields.outlinecolour} palette={palette}
            onInput={value => set({ outlinecolour: value })} />
        </PropRow>
        <SliderField label="粗细" value={num(fields, "outline")} min={0} max={40} rangeMax={20} step={0.5}
          onInput={value => set({ outline: value })} />
      </SideSection>

      <SideSection title="阴影" collapsible>
        <PropRow label="类型">
          <Segmented value={num(fields, "borderstyle", 1)}
            options={[
              { value: 1, label: "投影", title: "BorderStyle 1：描边 + 阴影" },
              { value: 3, label: "底框", title: "BorderStyle 3：阴影色变成整块不透明底框" },
            ]}
            onPick={value => set({ borderstyle: value })} />
        </PropRow>
        <PropRow label="颜色" title="阴影色；类型选「底框」时它是底框的颜色">
          <ColorField value={fields.backcolour} palette={palette}
            onInput={value => set({ backcolour: value })} />
        </PropRow>
        <SliderField label="距离" value={num(fields, "shadow")} min={0} max={40} rangeMax={20} step={0.5}
          onInput={value => set({ shadow: value })} />
      </SideSection>

      <SideSection title="变换" collapsible defaultOpen={false}>
        <SliderField label="横向缩放" value={num(fields, "scalex", 100)} min={10} max={400} rangeMax={200} suffix="%"
          onInput={value => set({ scalex: value })} />
        <SliderField label="纵向缩放" value={num(fields, "scaley", 100)} min={10} max={400} rangeMax={200} suffix="%"
          onInput={value => set({ scaley: value })} />
        <SliderField label="字间距" value={num(fields, "spacing")} min={-20} max={100} rangeMax={40}
          onInput={value => set({ spacing: value })} />
        <SliderField label="旋转" value={num(fields, "angle")} min={-360} max={360} suffix="°"
          onInput={value => set({ angle: value })} />
      </SideSection>

      <SideSection title="边距" collapsible defaultOpen={false}>
        <SliderField label="左边距" value={num(fields, "marginl")} min={0} max={play.x - 1}
          onInput={value => set({ marginl: value })} />
        <SliderField label="右边距" value={num(fields, "marginr")} min={0} max={play.x - 1}
          onInput={value => set({ marginr: value })} />
        <SliderField label="垂直边距" value={num(fields, "marginv")} min={0} max={play.y - 1}
          disabled={ignoresMarginV(align)}
          title={ignoresMarginV(align) ? "垂直居中时 libass 忽略 MarginV" : undefined}
          onInput={value => set({ marginv: value })} />
      </SideSection>

      <div className="sd-foot">
        <span className="sb-meta">
          {media?.videoWidth ? `视频 ${media.videoWidth}×${media.videoHeight}` : ""}
        </span>
        <button type="button" className="side-text-btn" onClick={() => modalStore.set({ tplOpen: true })}>用文本编辑样式表…</button>
      </div>
    </div>
  );
}
