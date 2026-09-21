// ASS 颜色编解码。纯函数，编辑器与插件宿主共用。
//
// ASS 的颜色字面量是 `&HAABBGGRR`：字节序与 CSS 反着来，而且 **AA 是「透明度」而不是
// 「不透明度」**——00 全不透明、FF 全透明。这一层负责把这份别扭关在门里，
// 门外一律用 { r, g, b, a }，a ∈ [0,1] 且 1 = 不透明。
//
// 六位的 `&HBBGGRR&`（不带 alpha）同样认，按不透明处理——样式表里两种写法都见得到。

export interface Rgba {
  r: number;
  g: number;
  b: number;
  /** 不透明度，0 = 全透明，1 = 全不透明（与 ASS 的 AA 字节相反） */
  a: number;
}

const COLOR_RE = /&h([0-9a-f]{2})?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})\b/i;

export const WHITE: Rgba = { r: 255, g: 255, b: 255, a: 1 };

const byte = (n: number) => Math.min(255, Math.max(0, Math.round(n)));
const hex2 = (n: number) => byte(n).toString(16).toUpperCase().padStart(2, "0");

/** 解析 ASS 颜色；认不出来就当白色（调用方拿不到 null 要处理的分支） */
export function parseAssColor(raw: string | null | undefined): Rgba {
  const m = COLOR_RE.exec((raw || "").trim());
  if (!m) return { ...WHITE };
  return {
    b: parseInt(m[2], 16),
    g: parseInt(m[3], 16),
    r: parseInt(m[4], 16),
    a: m[1] ? 1 - parseInt(m[1], 16) / 255 : 1,
  };
}

/** 一律出八位的 `&HAABBGGRR`（样式表里的既有写法），AA = 255 - 不透明度 */
export function formatAssColor(c: Rgba): string {
  return "&H" + hex2(255 - c.a * 255) + hex2(c.b) + hex2(c.g) + hex2(c.r);
}

/** 只要 RGB 的 `#rrggbb`（取色器、CSS 色块用） */
export const hexOf = (c: Rgba): string =>
  "#" + hex2(c.r).toLowerCase() + hex2(c.g).toLowerCase() + hex2(c.b).toLowerCase();

/** CSS 颜色：不透明时出 rgb()，否则 rgba() */
export const cssOf = (c: Rgba): string =>
  c.a >= 0.999 ? `rgb(${byte(c.r)},${byte(c.g)},${byte(c.b)})`
    : `rgba(${byte(c.r)},${byte(c.g)},${byte(c.b)},${c.a.toFixed(3)})`;

/** `#rgb` / `#rrggbb` / `#rrggbbaa` / 直接粘进来的 `&H…`；认不出返回 null */
export function parseCssHex(text: string): Rgba | null {
  const s = (text || "").trim();
  if (/^&h/i.test(s)) return COLOR_RE.test(s) ? parseAssColor(s) : null;
  const m = /^#?([0-9a-f]{3,8})$/i.exec(s);
  if (!m) return null;
  const h = m[1];
  if (h.length === 3) {
    return { r: parseInt(h[0] + h[0], 16), g: parseInt(h[1] + h[1], 16), b: parseInt(h[2] + h[2], 16), a: 1 };
  }
  if (h.length === 6 || h.length === 8) {
    return {
      r: parseInt(h.slice(0, 2), 16),
      g: parseInt(h.slice(2, 4), 16),
      b: parseInt(h.slice(4, 6), 16),
      a: h.length === 8 ? parseInt(h.slice(6, 8), 16) / 255 : 1,
    };
  }
  return null;
}

// ── HSV ↔ RGB（取色器的 SV 方块 + 色相条用）──────────────
// h ∈ [0,360)，s/v ∈ [0,1]。a 原样带过去，不参与换算。

export function rgbToHsv(c: Rgba): { h: number; s: number; v: number } {
  const r = c.r / 255, g = c.g / 255, b = c.b / 255;
  const max = Math.max(r, g, b), min = Math.min(r, g, b), d = max - min;
  let h = 0;
  if (d > 0) {
    if (max === r) h = ((g - b) / d) % 6;
    else if (max === g) h = (b - r) / d + 2;
    else h = (r - g) / d + 4;
    h *= 60;
    if (h < 0) h += 360;
  }
  return { h, s: max > 0 ? d / max : 0, v: max };
}

export function hsvToRgb(h: number, s: number, v: number, a: number): Rgba {
  const hh = ((h % 360) + 360) % 360 / 60;
  const c = v * s;
  const x = c * (1 - Math.abs((hh % 2) - 1));
  const m = v - c;
  const [r, g, b] = hh < 1 ? [c, x, 0] : hh < 2 ? [x, c, 0] : hh < 3 ? [0, c, x]
    : hh < 4 ? [0, x, c] : hh < 5 ? [x, 0, c] : [c, 0, x];
  return { r: byte((r + m) * 255), g: byte((g + m) * 255), b: byte((b + m) * 255), a };
}
