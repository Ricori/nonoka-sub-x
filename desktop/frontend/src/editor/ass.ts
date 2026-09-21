import { BUNDLED_FONTS } from './constants.ts';
import { parseAssColor } from '../subtitles/color.ts';
import { assHeadOf, composeSheet, resolveStyleIn } from '../subtitles/styles.ts';
import type { StyleSheet } from '../subtitles/styles.ts';
import type { Lang } from './types';

// 当前文档那份样式表的模块级单例：同一个编辑器窗口只开一个视频，所以一份解析全窗口共用。
// 解析、合并与 ASS 头的拼法都在 src/subtitles/styles.ts（插件宿主共用同一份），这里只管
// 「当前装的是哪一份」。写入口在 editor/lib/styleEdit（要连 docStore 一起更新）。

export { mergeStyleText, parseSheet } from '../subtitles/styles.ts';

let sheet: StyleSheet = composeSheet("");

export const getStyleMap = () => sheet.styleMap;
export const getStyleNames = () => sheet.styleNames;
export const getPlayRes = () => sheet.playRes;
/** 当前样式表本体：导出/预览的拼装函数要整份喂进去 */
export const getStyleSheet = () => sheet;

/**
 * 装入本机样式表：写死的 JP/CN 打底，本机样式表接在后面；同名以本机那份为准，
 * 于是 JP/CN 永远存在（resolveStyle 的回退目标），但用户想重定义也拦得住。
 */
export function setStyleSheet(userText: string) {
  sheet = composeSheet(userText);
}

/** 样式表里没有的绑定回退到写死的 JP/CN——云端同步下来的文档常绑着本机没有的样式 */
export const resolveStyle = (name: string, lang: Lang): string => resolveStyleIn(sheet, name, lang);

/** 预览与导出共用的 ASS 头：[Script Info] + 合并后的 [V4+ Styles] */
export const assHead = () => assHeadOf(sheet);

/** 样式的主色，统一成 rgb() 形式（调用方要拆出 "r,g,b" 拼透明度） */
export function styleRgb(name: string | null, fallback: string): string {
  const st = name ? sheet.styleMap[name] : null;
  if (st) { const c = parseAssColor(st.c1); return `rgb(${c.r},${c.g},${c.b})`; }
  return fallback;
}

// ── 缺字检测 ──────────────────────────────────────────────
// 随包字体之外，样式表里引用的字体得靠系统装了同名的。
// document.fonts.check() 对本地字体不可靠，用经典的 canvas 宽度比对法：
// 拿目标字体和一个必然不存在的族名量同一串字，宽度不同就说明目标字体真被用上了。
const PROBE = "汉字AWMil測試0123";
const BOGUS = '"__nonoka_no_such_font__"';
const cssFam = (n: string) => `"${n.replace(/["\\]/g, "\\$&")}"`;

/**
 * 批量查缺字。必须是异步的：@font-face 声明的字体是懒加载的，
 * 没先 load 一遍就量宽，量到的是回退字体，会把装着的字体误报成缺失。
 */
export async function fontsMissing(names: string[]): Promise<string[]> {
  const list = names.filter(n => n && !BUNDLED_FONTS.some(f => f.toLowerCase() === n.toLowerCase()));
  if (!list.length) return [];
  await Promise.all(list.map(n => document.fonts.load(`40px ${cssFam(n)}`, PROBE).catch(() => {})));
  await document.fonts.ready;
  const cv = document.createElement("canvas").getContext("2d");
  if (!cv) return [];
  const width = (f: string) => { cv.font = `40px ${f}`; return cv.measureText(PROBE).width; };
  const bogus = width(BOGUS);
  return list.filter(n => width(`${cssFam(n)}, ${BOGUS}`) === bogus);
}
