import { useEffect, useRef, useState } from 'react';
import {
  cssOf, formatAssColor, hexOf, hsvToRgb, parseAssColor, parseCssHex, rgbToHsv,
} from '../../../subtitles/color';
import type { Rgba } from '../../../subtitles/color';

/**
 * ASS 颜色的取色器。项目里没有任何颜色库，也不打算加，所以这份是自绘的：
 * SV 方块 + 色相条 + 不透明度条 + hex 输入 + 吸管 + 样式表里已用过的颜色。
 *
 * ASS 那个反过来的 alpha 字节（00 不透明、FF 全透明）只活在 subtitles/color.ts 里，
 * 界面上一律说「不透明度 0–100 %」。
 *
 * **吸管只能用原生 EyeDropper**：<video> 来自跨域的环回服务（一 drawImage 画布就被污染），
 * 字幕画布又被 JASSUB transferControlToOffscreen 走了，两层像素都读不出来。
 * EyeDropper 取的是 OS 级屏幕像素，不受同源策略约束，而且能从别的窗口取参考色。
 * 浏览器没有它就不显示这个按钮——没有可用的降级方案。
 */

const hasEyeDropper = typeof window !== "undefined" && "EyeDropper" in window;

export function ColorField({ label, value, title, palette, disabled, onInput }: {
  label?: string;
  /** ASS 原文，如 &H00FFF9FD */
  value: string;
  title?: string;
  /** 样式表里已经用过的颜色，点一下直接套用 */
  palette: string[];
  disabled?: boolean;
  onInput(assColor: string): void;
}) {
  const [open, setOpen] = useState(false);
  const anchor = useRef<HTMLButtonElement>(null);
  const colour = parseAssColor(value);

  return (
    <span className={"sp-color" + (disabled ? " off" : "")} title={title}>
      {label && <span className="sp-label">{label}</span>}
      <button type="button" ref={anchor} className="sp-swatch" aria-expanded={open} disabled={disabled}
        onClick={() => setOpen(value => !value)}>
        <i style={{ background: cssOf(colour) }} />
      </button>
      {open && !disabled && <ColorPopover anchor={anchor.current} colour={colour} palette={palette}
        onClose={() => setOpen(false)}
        onInput={next => onInput(formatAssColor(next))} />}
    </span>
  );
}

function ColorPopover({ anchor, colour, palette, onInput, onClose }: {
  anchor: HTMLElement | null;
  colour: Rgba;
  palette: string[];
  onInput(colour: Rgba): void;
  onClose(): void;
}) {
  const box = useRef<HTMLDivElement>(null);
  const [hex, setHex] = useState(hexOf(colour));
  // 色相在灰度下是丢失的（s=0 时任何 h 都一样），所以拖 SV 方块时要自己记着
  const [hue, setHue] = useState(() => rgbToHsv(colour).h);
  const hsv = rgbToHsv(colour);
  const h = hsv.s > 0.004 && hsv.v > 0.004 ? hsv.h : hue;

  useEffect(() => { setHex(hexOf(colour)); }, [colour.r, colour.g, colour.b]);

  // 点外面关掉，照抄轨道弹层那套
  useEffect(() => {
    const onDown = (event: PointerEvent) => {
      const target = event.target instanceof Node ? event.target : null;
      if (target && (box.current?.contains(target) || anchor?.contains(target))) return;
      onClose();
    };
    document.addEventListener("pointerdown", onDown);
    return () => document.removeEventListener("pointerdown", onDown);
  }, [anchor, onClose]);

  const rect = anchor?.getBoundingClientRect();
  // 上面放不下（锚点靠近窗口顶部，比如侧栏上半截）就翻到下面
  const below = !!rect && rect.top < 300 && window.innerHeight - rect.bottom > rect.top;
  const drag = (el: HTMLElement | null, event: React.PointerEvent, read: (x: number, y: number) => void) => {
    if (!el) return;
    event.preventDefault();
    el.setPointerCapture(event.pointerId);
    const area = el.getBoundingClientRect();
    const at = (moveEvent: { clientX: number; clientY: number }) => read(
      Math.min(1, Math.max(0, (moveEvent.clientX - area.left) / area.width)),
      Math.min(1, Math.max(0, (moveEvent.clientY - area.top) / area.height)),
    );
    at(event);
    const move = (moveEvent: PointerEvent) => at(moveEvent);
    const done = () => {
      el.removeEventListener("pointermove", move);
      el.removeEventListener("pointerup", done);
      el.removeEventListener("pointercancel", done);
    };
    el.addEventListener("pointermove", move);
    el.addEventListener("pointerup", done);
    el.addEventListener("pointercancel", done);
  };

  async function pickFromScreen() {
    try {
      const { sRGBHex } = await new window.EyeDropper!().open();
      const picked = parseCssHex(sRGBHex);
      if (picked) onInput({ ...picked, a: colour.a });   // 只换 RGB，保留不透明度
    } catch {
      /* 用户按 Esc 取消会抛 AbortError，忽略 */
    }
  }

  return (
    <div className="cp-pop" ref={box} style={rect ? {
      left: Math.max(8, Math.min(rect.left, window.innerWidth - 236)) + "px",
      ...(below
        ? { top: (rect.bottom + 8) + "px" }
        : { bottom: (window.innerHeight - rect.top + 8) + "px" }),
    } : undefined}
      onPointerDown={event => event.stopPropagation()}>

      <div className="cp-sv" style={{ background: `hsl(${h} 100% 50%)` }}
        onPointerDown={event => drag(event.currentTarget, event,
          (x, y) => onInput(hsvToRgb(h, x, 1 - y, colour.a)))}>
        <i className="cp-thumb" style={{ left: hsv.s * 100 + "%", top: (1 - hsv.v) * 100 + "%" }} />
      </div>

      <div className="cp-hue"
        onPointerDown={event => drag(event.currentTarget, event, x => {
          setHue(x * 360);
          onInput(hsvToRgb(x * 360, hsv.s, hsv.v, colour.a));
        })}>
        <i className="cp-thumb" style={{ left: (h / 360) * 100 + "%" }} />
      </div>

      <div className="cp-alpha" style={{ "--cp-solid": hexOf(colour) } as React.CSSProperties}
        onPointerDown={event => drag(event.currentTarget, event, x => onInput({ ...colour, a: x }))}>
        <i className="cp-thumb" style={{ left: colour.a * 100 + "%" }} />
      </div>

      <div className="cp-row">
        <input className="cp-hex" spellCheck={false} value={hex}
          onChange={event => {
            setHex(event.target.value);
            const parsed = parseCssHex(event.target.value);
            if (parsed) onInput({ ...parsed, a: event.target.value.trim().length === 9 ? parsed.a : colour.a });
          }}
          onBlur={() => setHex(hexOf(colour))} />
        <span className="cp-opacity">
          {Math.round(colour.a * 100)}<i>% 不透明</i>
        </span>
        {hasEyeDropper && (
          <button type="button" className="cp-dropper" title="从屏幕上取色（可以取预览画面，也可以取别的窗口）"
            onClick={() => void pickFromScreen()}>🖉</button>
        )}
      </div>

      {palette.length > 0 && (
        <div className="cp-palette" title="这份样式表里已经用过的颜色">
          {palette.map(raw => (
            <button key={raw} type="button" style={{ background: cssOf(parseAssColor(raw)) }}
              title={raw} onClick={() => onInput(parseAssColor(raw))} />
          ))}
        </div>
      )}
    </div>
  );
}
