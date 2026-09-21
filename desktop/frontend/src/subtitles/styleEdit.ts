import { ASS_FMT_DEFAULT, ASS_STYLE_FORMAT, BUILTIN_ASS_STYLES } from './constants.ts';
import { FIELD_FALLBACK, parseSheet, styleLine } from './styles.ts';
import type { StyleFields } from './styles.ts';

// 按字段改样式表。图形化面板和预览里的拖动都走这里，所以口径有两条硬要求：
//
// 1. **无损**：AssStyle 是 23 字段的有损投影（丢了 SecondaryColour / Underline /
//    StrikeOut / Angle / BorderStyle / Encoding），改一个字号不能把那六项抹掉。
//    所以读写一律走 StyleFields 原文，不经过 AssStyle。
// 2. **外科手术**：只重写目标那一行，别的字节一个不动。拖一下字幕就把用户手写的
//    注释、[Aegisub Project Garbage]、字段顺序全洗一遍太粗暴——整份重排留给样式模板
//    弹窗（mergeStyleText）。只有当文件自己的 Format 行装不下要改的字段时才退回重排。

/** 小写 ASS 字段名 → 新值。数字会被 String() 一下再写回 */
export type StylePatch = Record<string, string | number>;

const isEvents = (line: string) => /^\s*\[events\]/i.test(line);
const isFormat = (line: string) => /^\s*format\s*:/i.test(line);
const isStyle = (line: string) => /^\s*style\s*:/i.test(line);
const isStylesHead = (line: string) => /^\s*\[v4\+?\s*styles\]/i.test(line);

/** Style 行拆成字段数组；第一列是 Name */
const styleParts = (line: string) => line.replace(/^\s*style\s*:/i, "").split(",").map(s => s.trim());

/** 样式名不能含逗号（Style 行按逗号分列）和换行（会截断整行） */
export const safeStyleName = (name: string) => (name || "").replace(/[,\r\n]/g, " ").trim();

/** 把 patch 归一成字符串；顺带挡住会毁掉 Style 行的逗号与换行 */
function applyPatch(fields: StyleFields, patch: StylePatch): StyleFields {
  const out = { ...fields };
  for (const [key, value] of Object.entries(patch)) {
    out[key.toLowerCase()] = String(value).replace(/[,\r\n]/g, " ").trim();
  }
  return out;
}

/** [Events] 之前的行数——样式段只在它前面，之后的内容一律不碰 */
const eventsAt = (lines: string[]) => {
  const i = lines.findIndex(isEvents);
  return i < 0 ? lines.length : i;
};

/** 该位置生效的 Format 字段序；文件没写就用标准 23 字段 */
function formatKeysBefore(lines: string[], limit: number): string[] {
  for (let i = limit - 1; i >= 0; i--) {
    if (isFormat(lines[i])) return lines[i].replace(/^\s*format\s*:/i, "").toLowerCase().split(",").map(s => s.trim());
  }
  return ASS_FMT_DEFAULT;
}

/** 第一条同名 Style 行的下标（对齐 parseSheet 的 first-wins），没有返回 -1 */
function findStyleLine(lines: string[], limit: number, name: string): number {
  for (let i = 0; i < limit; i++) {
    if (isStyle(lines[i]) && styleParts(lines[i])[0] === name) return i;
  }
  return -1;
}

/** 写死的 origin/cn 的字段原文（样式表里没有这个名字时拿来当底子） */
let builtinFields: Record<string, StyleFields> | null = null;
const builtinOf = (name: string): StyleFields | undefined => {
  if (!builtinFields) builtinFields = parseSheet(BUILTIN_ASS_STYLES).fields;
  return builtinFields[name];
};

/**
 * 某个样式当前的 23 字段原文：样式表里那份优先 → 写死的 origin/cn → Aegisub 新建样式的默认值。
 * 面板读值、复制样式都走它（而不是有损的 AssStyle）。
 */
export function styleFieldsOf(sheetText: string, name: string): StyleFields {
  const own = parseSheet(sheetText).fields[name];
  const base = own ?? builtinOf(name) ?? {};
  const out: StyleFields = {};
  for (const key of ASS_FMT_DEFAULT) out[key] = base[key] ?? FIELD_FALLBACK[key];
  out.name = name;
  return out;
}

/** 规范化兜底：整份按标准 Format 重排（会丢注释，调用方要提示用户） */
function normalised(sheetText: string, name: string, patch: StylePatch): string {
  const sheet = parseSheet(sheetText);
  const order = sheet.fields[name] ? sheet.order : [...sheet.order, name];
  const info = sheet.playRes
    ? `[Script Info]\nPlayResX: ${sheet.playRes.x}\nPlayResY: ${sheet.playRes.y}\n\n`
    : "";
  const lines = order.map(n => styleLine(n, n === name
    ? applyPatch(styleFieldsOf(sheetText, name), patch)
    : sheet.fields[n]));
  return info + ["[V4+ Styles]", ASS_STYLE_FORMAT, ...lines, ""].join("\n");
}

/**
 * 把 patch 写回样式表原文。命中就原地改那一行，其余字节不动；样式表里没有这个名字
 * 就现造一条（底子取写死的 origin/cn 或 Aegisub 默认值）插在 [Events] 之前。
 *
 * 文件自己的 Format 行装不下要改的字段时只能整份重排，此时 normalised 为 true，
 * 调用方应当告诉用户「样式表已按标准 Format 规范化」——那一步会丢注释。
 */
export function patchStyleText(sheetText: string, rawName: string, patch: StylePatch):
  { text: string; normalised: boolean } {
  const name = safeStyleName(rawName);
  if (!name) return { text: sheetText, normalised: false };

  const lines = (sheetText || "").split("\n");
  const limit = eventsAt(lines);
  const keys = formatKeysBefore(lines, limit);
  const patchKeys = Object.keys(patch).map(k => k.toLowerCase());

  // 文件的 Format 列不全，这一行表达不了要改的字段：只能整份按标准 Format 重排
  if (patchKeys.some(k => !keys.includes(k))) {
    return { text: normalised(sheetText, name, patch), normalised: true };
  }

  const emit = (fields: StyleFields) =>
    "Style: " + keys.map(k => (k === "name" ? name : fields[k] ?? FIELD_FALLBACK[k])).join(",");

  const at = findStyleLine(lines, limit, name);
  if (at >= 0) {
    const parts = styleParts(lines[at]);
    const fields: StyleFields = {};
    keys.forEach((k, i) => { if (i < parts.length) fields[k] = parts[i]; });
    lines[at] = emit(applyPatch(fields, patch));
    return { text: lines.join("\n"), normalised: false };
  }

  // 没有这个样式：拿写死的 origin/cn 或默认值当底子现造一条
  const seed: StyleFields = {};
  const base = builtinOf(name);
  for (const k of keys) seed[k] = base?.[k] ?? FIELD_FALLBACK[k] ?? "";
  const line = emit(applyPatch(seed, patch));

  // 插在 [Events] 之前的最后一条 Style 行之后；没有就跟在 Format 行后面；
  // 连 [V4+ Styles] 段都没有就把整段补出来
  let insertAt = -1;
  for (let i = limit - 1; i >= 0; i--) {
    if (isStyle(lines[i])) { insertAt = i + 1; break; }
  }
  if (insertAt < 0) {
    for (let i = limit - 1; i >= 0; i--) {
      if (isFormat(lines[i]) || isStylesHead(lines[i])) { insertAt = i + 1; break; }
    }
  }
  if (insertAt < 0) {
    const head = ["[V4+ Styles]", ASS_STYLE_FORMAT, line];
    // 段前留一个空行，除非文件本来就是空的或已经以空行收尾
    const pad = limit > 0 && lines[limit - 1].trim() ? [""] : [];
    lines.splice(limit, 0, ...pad, ...head);
  } else {
    lines.splice(insertAt, 0, line);
  }
  return { text: lines.join("\n"), normalised: false };
}

/** 复制一条样式（同一份字段，换个名字）。已存在同名则原样返回 */
export function cloneStyleText(sheetText: string, from: string, to: string):
  { text: string; normalised: boolean } {
  const fields = styleFieldsOf(sheetText, from);
  const patch: StylePatch = {};
  for (const key of ASS_FMT_DEFAULT) if (key !== "name") patch[key] = fields[key];
  return patchStyleText(sheetText, to, patch);
}

/** 不与 taken 里任何名字相撞的新名字：speaker1 → speaker1 2 → speaker1 3 … */
export function uniqueStyleName(base: string, taken: Iterable<string>): string {
  const used = new Set<string>();
  for (const n of taken) used.add(n);
  const stem = safeStyleName(base).replace(/\s+\d+$/, "") || "样式";
  if (!used.has(stem)) return stem;
  for (let i = 2; i < 1000; i++) {
    const candidate = `${stem} ${i}`;
    if (!used.has(candidate)) return candidate;
  }
  return `${stem} ${Date.now()}`;
}
