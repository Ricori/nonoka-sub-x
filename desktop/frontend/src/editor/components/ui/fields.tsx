import { useEffect, useState } from 'react';
import { splitHandler } from '../../lib/split';

/**
 * 样式属性条用的小控件。视觉底子沿用特效面板的 .fx-param 那套，
 * 行为上多两样：标签可以左右拖着调值，以及失焦时钳进范围。
 */

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
  const [text, setText] = useState(String(value));
  // 外部改了值（拖动画面、撤销）时跟着回显，但别打断正在输入的那次
  useEffect(() => { setText(String(value)); }, [value]);

  const clamp = (raw: number) => Math.min(max, Math.max(min, raw));
  const finish = () => {
    const numeric = Number(text);
    const next = clamp(Number.isFinite(numeric) ? numeric : value);
    setText(String(next));
    if (next !== value) onInput(next);
  };

  return (
    <label className={"sp-field" + (disabled ? " off" : "")} title={title}>
      {/* 标签本身是个滑杆：拖着改值比对着数字框点上下箭头快得多 */}
      <span className="sp-label scrub"
        onPointerDown={disabled ? undefined : splitHandler(() => value, (v0, dx) => {
          onInput(clamp(Math.round((v0 + dx * step) / step) * step));
        })}>{label}</span>
      <span className="sp-control">
        <input type="number" value={text} min={min} max={max} step={step} disabled={disabled}
          onChange={event => {
            setText(event.target.value);
            const numeric = Number(event.target.value);
            if (event.target.value !== "" && Number.isFinite(numeric)) onInput(clamp(numeric));
          }}
          onBlur={finish}
          onKeyDown={event => { if (event.key === "Enter") finish(); }} />
        {suffix && <i>{suffix}</i>}
      </span>
    </label>
  );
}

export function Segmented<T extends string | number>({ label, value, options, onPick }: {
  label?: string;
  value: T;
  options: { value: T; label: string; title?: string }[];
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
