import { useEffect, useState } from 'react';
import { splitHandler } from '../../lib/split';

/**
 * 样式属性条用的小控件。视觉底子沿用特效面板的 .fx-param 那套，
 * 行为上多两样：标签可以左右拖着调值，以及失焦时钳进范围。
 */

/** 失焦/回车时钳进范围的数字框；外部改了值（拖动画面、撤销）时跟着回显 */
function NumInput({ value, min, max, step = 1, disabled, className, onInput }: {
  value: number; min: number; max: number; step?: number; disabled?: boolean; className?: string;
  onInput(value: number): void;
}) {
  const [text, setText] = useState(String(value));
  useEffect(() => { setText(String(value)); }, [value]);

  const clamp = (raw: number) => Math.min(max, Math.max(min, raw));
  const finish = () => {
    const numeric = Number(text);
    const next = clamp(Number.isFinite(numeric) ? numeric : value);
    setText(String(next));
    if (next !== value) onInput(next);
  };

  return <input type="number" className={className} value={text} min={min} max={max} step={step} disabled={disabled}
    onChange={event => {
      setText(event.target.value);
      const numeric = Number(event.target.value);
      if (event.target.value !== "" && Number.isFinite(numeric)) onInput(clamp(numeric));
    }}
    onBlur={finish}
    onKeyDown={event => { if (event.key === "Enter") finish(); }} />;
}

export function NumField({ label, value, min, max, step = 1, suffix, disabled, title, onInput }: {
  label: string;
  value: number;
  min: number;
  max: number;
  step?: number;
  suffix?: string;
  disabled?: boolean;
  title?: string;
  onInput(value: number): void;
}) {
  const clamp = (raw: number) => Math.min(max, Math.max(min, raw));
  return (
    <label className={"sp-field" + (disabled ? " off" : "")} title={title}>
      {/* 标签本身是个滑杆：拖着改值比对着数字框点上下箭头快得多 */}
      <span className="sp-label scrub"
        onPointerDown={disabled ? undefined : splitHandler(() => value, (v0, dx) => {
          onInput(clamp(Math.round((v0 + dx * step) / step) * step));
        })}>{label}</span>
      <span className="sp-control">
        <NumInput value={value} min={min} max={max} step={step} disabled={disabled} onInput={onInput} />
        {suffix && <i>{suffix}</i>}
      </span>
    </label>
  );
}

/**
 * 侧栏里的「标签 + 滑杆 + 数字框」一行。滑杆范围可以比数字框窄（rangeMax）：
 * 字号能填到 400，但滑杆拉满到 400 的话常用的 30–120 只剩一小截。
 */
export function SliderField({ label, value, min, max, step = 1, rangeMax, suffix, disabled, title, onInput }: {
  label: string;
  value: number;
  min: number;
  max: number;
  step?: number;
  rangeMax?: number;
  suffix?: string;
  disabled?: boolean;
  title?: string;
  onInput(value: number): void;
}) {
  const top = rangeMax ?? max;
  const fill = Math.min(1, Math.max(0, (value - min) / (top - min || 1))) * 100;
  return (
    <div className={"sd-prop sd-slider" + (disabled ? " off" : "")} title={title}>
      <span className="sd-label">{label}</span>
      <input type="range" min={min} max={top} step={step} value={Math.min(top, value)} disabled={disabled}
        style={{ "--fill": fill + "%" } as React.CSSProperties}
        onChange={event => onInput(Number(event.target.value))} />
      <span className="sd-num">
        <NumInput value={value} min={min} max={max} step={step} disabled={disabled} onInput={onInput} />
        {suffix && <i>{suffix}</i>}
      </span>
    </div>
  );
}

/** 开关：CapCut 那种胶囊拨钮 */
export function Switch({ on, disabled, title, onToggle }: {
  on: boolean; disabled?: boolean; title?: string; onToggle(on: boolean): void;
}) {
  return (
    <button type="button" role="switch" aria-checked={on} title={title} disabled={disabled}
      className={"sd-switch" + (on ? " on" : "")} onClick={() => onToggle(!on)}><i /></button>
  );
}

/** 侧栏的一节：标题行（可折叠、右侧可挂开关或按钮）+ 内容 */
export function SideSection({ title, extra, collapsible, defaultOpen = true, children }: {
  title: string;
  extra?: React.ReactNode;
  collapsible?: boolean;
  defaultOpen?: boolean;
  children?: React.ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const shown = !collapsible || open;
  return (
    <section className={"sd-section" + (shown ? "" : " closed")}>
      <header className="sd-section-head">
        {collapsible
          ? <button type="button" className="sd-section-title" aria-expanded={open} onClick={() => setOpen(v => !v)}>
            <svg viewBox="0 0 16 16" aria-hidden="true"><path d="m6 4 4 4-4 4" /></svg>{title}
          </button>
          : <span className="sd-section-title">{title}</span>}
        {extra && <span className="sd-section-extra">{extra}</span>}
      </header>
      {shown && children && <div className="sd-section-body">{children}</div>}
    </section>
  );
}

/** 侧栏里的「标签 · 控件」一行 */
export function PropRow({ label, title, children }: { label: string; title?: string; children: React.ReactNode }) {
  return (
    <div className="sd-prop" title={title}>
      <span className="sd-label">{label}</span>
      <div className="sd-control">{children}</div>
    </div>
  );
}

export function Segmented<T extends string | number>({ label, value, options, onPick }: {
  label?: string;
  value: T;
  options: { value: T; label: React.ReactNode; title?: string }[];
  onPick(value: T): void;
}) {
  return (
    <span className="sp-seg-wrap">
      {label && <span className="sp-label">{label}</span>}
      <span className="sp-seg">
        {options.map(option => (
          <button key={String(option.value)} type="button" title={option.title}
            className={option.value === value ? "on" : ""}
            onClick={() => onPick(option.value)}>{option.label}</button>
        ))}
      </span>
    </span>
  );
}

export function ToggleChip({ label, on, title, onToggle }: {
  label: string; on: boolean; title?: string; onToggle(on: boolean): void;
}) {
  return (
    <button type="button" title={title} className={"sp-chip" + (on ? " on" : "")}
      aria-pressed={on} onClick={() => onToggle(!on)}>{label}</button>
  );
}

/** ASS 的 Alignment 就是小键盘的方位：7 8 9 在上，1 2 3 在下 */
const ALIGN_ROWS = [[7, 8, 9], [4, 5, 6], [1, 2, 3]];
const ALIGN_TITLE: Record<number, string> = {
  7: "左上", 8: "中上", 9: "右上",
  4: "左中", 5: "正中", 6: "右中",
  1: "左下", 2: "中下", 3: "右下",
};

export function AlignGrid({ value, onPick }: { value: number; onPick(value: number): void }) {
  return (
    <span className="sp-align" title="对齐方式（小键盘方位）">
      {ALIGN_ROWS.map((row, index) => (
        <span key={index}>
          {row.map(align => (
            <button key={align} type="button" title={ALIGN_TITLE[align]}
              className={align === value ? "on" : ""}
              onClick={() => onPick(align)} />
          ))}
        </span>
      ))}
    </span>
  );
}
