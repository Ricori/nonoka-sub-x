import { useEffect, useMemo, useState } from 'react';
import { shallowEqual } from '../../../home/lib/createStore';
import { BUNDLED_FONTS } from '../../constants';
import { parseSheet } from '../../../subtitles/styles';
import { fontsMissing, getPlayRes, getStyleNames } from '../../ass';
import { video } from '../../lib/media';
import { boxForLane } from '../../lib/stageHit';
import {
  cloneStyleForLane, materializeMissingStyle, patchStyleFromPanel, stylesInUse, styleFields,
} from '../../lib/styleEdit';
import { docStore } from '../../store/docStore';
import { stageStore } from '../../store/stageStore';
import { modalStore, toast } from '../../store/uiStore';
import { AlignGrid, NumField, Segmented, ToggleChip } from '../ui/fields';
import { ColorField } from '../ui/ColorField';
import { ignoresMarginV } from '../../../subtitles/layout';

/**
 * 选中字幕后出现在预览下方的属性条，覆盖 ASS 的全部 23 个字段。
 *
 * 刻意**不是浮层**：字幕绝大多数底部对齐，浮在舞台下沿正好盖住要调的那行字。
 * 做成 .preview-pane 这个 flex 列里的常规子项，舞台变矮、fitStage 靠已有的
 * ResizeObserver 自动重适配，画面一个像素都不会被遮。
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

export function StyleBar() {
  // shallowEqual 不吃 null，所以按字段选，别整个 sel 对象比
  const { trackId, lang } = stageStore.use(
    s => ({ trackId: s.sel?.trackId ?? "", lang: s.sel?.lang ?? "zh" }), shallowEqual);
  const { styles, version } = docStore.use(
    s => ({ styles: s.styles, version: s.version }), shallowEqual);
  const [more, setMore] = useState(false);
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

  if (!box || !fields) return null;

  const set = (patch: Record<string, string | number>) => patchStyleFromPanel(name, patch);
  const align = num(fields, "alignment", 2);
  const play = getPlayRes();
  const media = video();
  // 绑定写的名字这份样式表里没有，正按 JP/CN 顶着用
  const unbound = box.boundStyle && !getStyleNames().includes(box.boundStyle) ? box.boundStyle : null;
  const shared = bindings.filter(binding => binding.direct).length > 1
    || bindings.some(binding => !binding.direct);

  return (
    <div className="style-bar">
      <div className="sb-row">
        <span className={"sb-name" + (shared ? " warn" : "")}
          title={bindings.map(b => `${b.trackName} · ${b.lang === "ja" ? "原文" : "译文"}`).join("\n")}>
          {name}
          {bindings.length > 1 && <em>{bindings.length} 条轨在用</em>}
        </span>
        {bindings.length > 1 && (
          <button className="sb-act" title="改成只作用于这条轨道，之后拖动不再影响别人"
            onClick={() => {
              const created = cloneStyleForLane(box.trackId, box.lang);
              if (created) toast("已复制专属样式「" + created + "」并改绑");
            }}>复制专属样式</button>
        )}

        <span className="sb-sep" />

        <label className="sp-field sb-font" title={missing.length ? "系统里没装这个字体，预览会用回退字体" : "字体"}>
          <span className="sp-label">字体</span>
          <span className="sp-control">
            <input list="sb-fonts" spellCheck={false} value={fields.fontname}
              className={missing.length ? "bad" : undefined}
              onChange={event => set({ fontname: event.target.value })} />
          </span>
        </label>
        <datalist id="sb-fonts">
          {[...new Set([...BUNDLED_FONTS, ...Object.values(parseSheet(styles).fields)
            .map(style => style.fontname).filter(Boolean)])].map(font => <option key={font} value={font} />)}
        </datalist>

        <NumField label="字号" value={num(fields, "fontsize", 70)} min={8} max={400}
          onInput={value => set({ fontsize: value })} />

        <span className="sb-chips">
          <ToggleChip label="B" title="粗体" on={flag(fields, "bold")}
            onToggle={on => set({ bold: flagValue(on) })} />
          <ToggleChip label="I" title="斜体" on={flag(fields, "italic")}
            onToggle={on => set({ italic: flagValue(on) })} />
          <ToggleChip label="U" title="下划线" on={flag(fields, "underline")}
            onToggle={on => set({ underline: flagValue(on) })} />
          <ToggleChip label="S" title="删除线" on={flag(fields, "strikeout")}
            onToggle={on => set({ strikeout: flagValue(on) })} />
        </span>

        <span className="sb-sep" />

        <ColorField label="主色" value={fields.primarycolour} palette={palette}
          onInput={value => set({ primarycolour: value })} />
        <ColorField label="次色" value={fields.secondarycolour} palette={palette}
          title="卡拉 OK 里还没唱到的那一截用它——没有 K 轴的话画面上看不出变化"
          onInput={value => set({ secondarycolour: value })} />
        <ColorField label="描边" value={fields.outlinecolour} palette={palette}
          onInput={value => set({ outlinecolour: value })} />
        <ColorField label="阴影" value={fields.backcolour} palette={palette}
          title="阴影色；边框样式选「底框」时它是底框的颜色"
          onInput={value => set({ backcolour: value })} />

        <NumField label="描边" value={num(fields, "outline")} min={0} max={40} step={0.5}
          onInput={value => set({ outline: value })} />
        <NumField label="阴影" value={num(fields, "shadow")} min={0} max={40} step={0.5}
          onInput={value => set({ shadow: value })} />

        <span className="sb-sep" />
        <AlignGrid value={align} onPick={value => set({ alignment: value })} />

        <span className="sb-spacer" />
        <button className="sb-act" onClick={() => setMore(value => !value)}>
          {more ? "收起 ▴" : "更多 ▾"}
        </button>
      </div>

      {unbound && (
        <div className="sb-note">
          这条轨绑的是样式表里没有的「{unbound}」，正按 {name} 显示
          <button className="sb-act" onClick={() => {
            const created = materializeMissingStyle(box.trackId, box.lang);
            if (created) toast("已用当前样子新建样式「" + created + "」");
          }}>用当前样子新建「{unbound}」</button>
        </div>
      )}

      {more && (
        <div className="sb-row wrap">
          <NumField label="横向缩放" value={num(fields, "scalex", 100)} min={10} max={400} suffix="%"
            onInput={value => set({ scalex: value })} />
          <NumField label="纵向缩放" value={num(fields, "scaley", 100)} min={10} max={400} suffix="%"
            onInput={value => set({ scaley: value })} />
          <NumField label="字间距" value={num(fields, "spacing")} min={-20} max={100}
            onInput={value => set({ spacing: value })} />
          <NumField label="旋转" value={num(fields, "angle")} min={-360} max={360} suffix="°"
            onInput={value => set({ angle: value })} />

          <Segmented label="边框" value={num(fields, "borderstyle", 1)}
            options={[
              { value: 1, label: "描边+阴影", title: "BorderStyle 1" },
              { value: 3, label: "不透明底框", title: "BorderStyle 3：阴影色变成整块底框" },
            ]}
            onPick={value => set({ borderstyle: value })} />

          <span className="sb-sep" />
          <NumField label="左边距" value={num(fields, "marginl")} min={0} max={play.x - 1}
            onInput={value => set({ marginl: value })} />
          <NumField label="右边距" value={num(fields, "marginr")} min={0} max={play.x - 1}
            onInput={value => set({ marginr: value })} />
          <NumField label="垂直边距" value={num(fields, "marginv")} min={0} max={play.y - 1}
            disabled={ignoresMarginV(align)}
            title={ignoresMarginV(align) ? "中间对齐（4/5/6）时 libass 忽略 MarginV" : undefined}
            onInput={value => set({ marginv: value })} />

          <span className="sb-spacer" />
          <span className="sb-meta">
            {/* 画布 {play.x}×{play.y} */}
            {media?.videoWidth ? `视频 ${media.videoWidth}×${media.videoHeight}` : ""}
          </span>
          <button className="sb-act" onClick={() => modalStore.set({ tplOpen: true })}>用文本编辑…</button>
        </div>
      )}
    </div>
  );
}
