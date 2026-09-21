// 纯函数工具：时间码、数值钳制、ASS 字段编解码。无状态，可独立使用。

/** MM:SS.cc（厘秒） */
export function fmt(s: number): string {
  s = Math.max(0, s);
  const m = Math.floor(s / 60), sec = Math.floor(s % 60), cs = Math.floor((s % 1) * 100);
  return String(m).padStart(2, "0") + ":" + String(sec).padStart(2, "0") + "." + String(cs).padStart(2, "0");
}

export const round3 = (x: number) => Math.round(x * 1000) / 1000;

export const p2 = (n: number) => String(n).padStart(2, "0");

/** 切片详情里用时分秒：切片动辄几分钟，厘秒没意义 */
export const fmtHMS = (sec: number) => {
  const v = Math.max(0, Math.round(sec));
  return `${p2(Math.floor(v / 3600))}:${p2(Math.floor(v % 3600 / 60))}:${p2(v % 60)}`;
};

export const fmtDur = (sec: number) => {
  const v = Math.max(0, Math.round(sec));
  const h = Math.floor(v / 3600), m = Math.floor(v % 3600 / 60), s = v % 60;
  return (h ? h + "时" : "") + (h || m ? m + "分" : "") + s + "秒";
};

/** 片段长度：一分钟以内带一位小数（剪掉的停顿常常不到一秒），更长的走 fmtDur */
export const fmtLen = (sec: number) => sec < 60 ? Math.max(0, sec).toFixed(1) + "秒" : fmtDur(sec);

export const fmtMB = (n: number) => !n ? "—" : n >= 1024 ** 3
  ? (n / 1024 ** 3).toFixed(2) + " GB" : (n / 1024 ** 2).toFixed(0) + " MB";

export function clampN(v: unknown, lo: number, hi: number, d: number): number {
  return (typeof v === "number" && isFinite(v)) ? Math.min(Math.max(v, lo), hi) : d;
}

/** 主进程抛回来的错误可能带一层调用包装前缀，剥掉只留 Go 端写的那句话 */
export const errText = (e: any) => String(e?.message || e || "未知错误")
  .replace(/^Error invoking remote method '[^']*':\s*/, "")
  .replace(/^Error:\s*/, "");

// ── ASS 字段编解码 ────────────────────────────────────────────────
// 时间码与字段转义搬到了 src/subtitles/format.ts（插件宿主共用同一份），按原名再导出。
export { assNm, assSec, assTs, assTx } from '../subtitles/format.ts';

// ASS 颜色（&HAABBGGRR，AA 是透明度而非不透明度）搬到了 src/subtitles/color.ts，
// 取色器、拼装管线与插件宿主共用同一份，这里按原名再导出。
export { cssOf, formatAssColor, hexOf, parseAssColor, parseCssHex } from '../subtitles/color.ts';
export type { Rgba } from '../subtitles/color.ts';
