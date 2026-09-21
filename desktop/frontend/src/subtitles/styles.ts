import { ASS_FMT_DEFAULT, ASS_STYLE_FORMAT, BUILTIN_ASS_STYLES } from './constants.ts';
import type { AssStyle, Lang } from './types.ts';

// 样式表解析与合成（预览渲染 + 绑定下拉 + 导出 + 插件宿主共用）。样式表跟着视频走，
// 存在文档的 styles 字段里（见 editor/lib/styleEdit），这里只管把「文档那份 + 写死的
// origin/cn」合成一份 StyleSheet：解析、规范化的 [V4+ Styles] 段、以及导出时的 ASS 头
// 都由它给出。编辑器把结果放进模块级单例（editor/ass.ts），插件宿主则按需现合成一份。

/** 一条 Style 行拆成「字段名 → 原文」，保留 AssStyle 里没有的字段（副色/描边样式/编码等） */
export type StyleFields = Record<string, string>;

export interface Sheet {
  order: string[];
  fields: Record<string, StyleFields>;
  playRes: { x: number; y: number } | null;
}

/** 合成后的样式表：查样式、拼 ASS 头都只认这一份 */
export interface StyleSheet {
  styleMap: Record<string, AssStyle>;
  styleNames: string[];
  playRes: { x: number; y: number };
  stylesBlock: string;
}

/** Format 行没列到的字段用这里的值补齐，取值同 Aegisub 新建样式 */
export const FIELD_FALLBACK: Record<string, string> = {
  name: "", fontname: "方正准圆_GBK", fontsize: "70",
  primarycolour: "&H00FFFFFF", secondarycolour: "&H000000FF",
  outlinecolour: "&H00000000", backcolour: "&H00000000",
  bold: "0", italic: "0", underline: "0", strikeout: "0",
  scalex: "100", scaley: "100", spacing: "0", angle: "0",
  borderstyle: "1", outline: "2", shadow: "2", alignment: "2",
  marginl: "10", marginr: "10", marginv: "30", encoding: "1",
};

/** 只解析 Style 行 + PlayRes；Format 行决定字段顺序，缺省用标准 23 字段 */
export function parseSheet(text: string): Sheet {
  const sheet: Sheet = { order: [], fields: {}, playRes: null };
  let fmtKeys = ASS_FMT_DEFAULT;
  for (const raw of (text || "").split(/\r?\n/)) {
    const line = raw.trim(), low = line.toLowerCase();
    if (low.startsWith("[events]")) break;   // Events 段（若有）不解析
    if (low.startsWith("playresx:")) {
      sheet.playRes = { x: +line.slice(9).trim() || 1920, y: sheet.playRes?.y || 1080 };
      continue;
    }
    if (low.startsWith("playresy:")) {
      sheet.playRes = { x: sheet.playRes?.x || 1920, y: +line.slice(9).trim() || 1080 };
      continue;
    }
    if (low.startsWith("format:")) { fmtKeys = low.slice(7).split(",").map(s => s.trim()); continue; }
    if (!low.startsWith("style:")) continue;
    const parts = line.slice(6).split(",").map(s => s.trim());
    const fields: StyleFields = {};
    fmtKeys.forEach((k, i) => { if (i < parts.length) fields[k] = parts[i]; });
    const name = fields.name || "";
    if (!name || sheet.fields[name]) continue;
    sheet.fields[name] = fields;
    sheet.order.push(name);
  }
  return sheet;
}

const num = (fields: StyleFields, key: string, fallback: number) =>
  +(fields[key] ?? FIELD_FALLBACK[key]) || fallback;

function toStyle(name: string, f: StyleFields): AssStyle {
  return {
    name,
    font: f.fontname ?? FIELD_FALLBACK.fontname,
    size: num(f, "fontsize", 70),
    c1: f.primarycolour ?? FIELD_FALLBACK.primarycolour,
    c3: f.outlinecolour ?? FIELD_FALLBACK.outlinecolour,
    c4: f.backcolour ?? FIELD_FALLBACK.backcolour,
    bold: num(f, "bold", 0), italic: num(f, "italic", 0),
    scx: num(f, "scalex", 100), scy: num(f, "scaley", 100), sp: num(f, "spacing", 0),
    outline: num(f, "outline", 0), shadow: num(f, "shadow", 0),
    align: num(f, "alignment", 2),
    ml: num(f, "marginl", 0), mr: num(f, "marginr", 0), mv: num(f, "marginv", 0),
  };
}

/** 按标准 23 字段顺序重新拼一行，Format 顺序被改过的外来样式也能原样落地 */
export const styleLine = (name: string, f: StyleFields) =>
  "Style: " + ASS_FMT_DEFAULT.map(k => (k === "name" ? name : f[k] ?? FIELD_FALLBACK[k])).join(",");

/**
 * 合成本机样式表：写死的 origin/cn 打底，本机样式表接在后面；同名以本机那份为准，
 * 于是 origin/cn 永远存在（resolveStyleIn 的回退目标），但用户想重定义也拦得住。
 */
export function composeSheet(userText: string): StyleSheet {
  const user = parseSheet(userText);
  const builtin = parseSheet(BUILTIN_ASS_STYLES);
  const styleMap: Record<string, AssStyle> = {};
  const styleNames: string[] = [];
  const lines: string[] = [];
  for (const name of [...builtin.order, ...user.order]) {
    if (styleMap[name]) continue;
    const fields = user.fields[name] ?? builtin.fields[name];
    styleMap[name] = toStyle(name, fields);
    styleNames.push(name);
    lines.push(styleLine(name, fields));
  }
  return {
    styleMap,
    styleNames,
    playRes: user.playRes ?? { x: 1920, y: 1080 },
    stylesBlock: `[V4+ Styles]\n${ASS_STYLE_FORMAT}\n${lines.join("\n")}\n`,
  };
}

/**
 * 把导入的 ASS 里的 [V4+ Styles] 并进现有样式表：同名覆盖、新名追加，原有的排在前面，
 * 全部按标准 Format 重排（导入的文件用什么字段顺序都不影响落地的这一份）。
 */
export function mergeStyleText(current: string, incoming: string) {
  const cur = parseSheet(current);
  const inc = parseSheet(incoming);
  const added: string[] = [];
  const updated: string[] = [];
  const lines: string[] = [];
  const seen = new Set<string>();
  for (const name of [...cur.order, ...inc.order]) {
    if (seen.has(name)) continue;
    seen.add(name);
    const from = inc.fields[name];
    if (from) (cur.fields[name] ? updated : added).push(name);
    lines.push(styleLine(name, from ?? cur.fields[name]));
  }
  // 重排会把 Style 行以外的东西丢掉，PlayRes 是其中唯一还起作用的，单独接回去
  const info = cur.playRes
    ? `[Script Info]\nPlayResX: ${cur.playRes.x}\nPlayResY: ${cur.playRes.y}\n\n`
    : "";
  const text = info + ["[V4+ Styles]", ASS_STYLE_FORMAT, ...lines, ""].join("\n");
  return { text, added, updated };
}

/** 写死的两个默认样式名：原文 / 译文 */
export const ORIGIN_STYLE = "origin";
export const CN_STYLE = "cn";

/** 样式表里没有的绑定回退到写死的 origin/cn——云端同步下来的文档常绑着本机没有的样式 */
export const resolveStyleIn = (sheet: StyleSheet, name: string, lang: Lang): string =>
  sheet.styleMap[name] ? name : (lang === "ja" ? ORIGIN_STYLE : CN_STYLE);

/** 预览与导出共用的 ASS 头：[Script Info] + 合并后的 [V4+ Styles] */
export const assHeadOf = (sheet: StyleSheet) =>
  "[Script Info]\nScriptType: v4.00+\n"
  + `PlayResX: ${sheet.playRes.x}\nPlayResY: ${sheet.playRes.y}\n`
  + "WrapStyle: 2\nScaledBorderAndShadow: yes\n\n" + sheet.stylesBlock;
