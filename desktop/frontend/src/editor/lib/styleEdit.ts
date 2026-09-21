import { assStyles } from '../../bridge/assStyles';
import { lanesUsingStyle } from '../../subtitles/build';
import type { StyleBinding } from '../../subtitles/build';
import { DEFAULT_EFFECT_TRACK_ID } from '../../subtitles/effects';
import {
  cloneStyleText, patchStyleText, styleFieldsOf, uniqueStyleName,
} from '../../subtitles/styleEdit';
import type { StylePatch } from '../../subtitles/styleEdit';
import { getStyleNames, getStyleSheet, resolveStyle, setStyleSheet } from '../ass';
import { DEFAULT_STYLE_SHEET } from '../constants';
import { bumpDoc, docStore } from '../store/docStore';
import { markDirty } from '../store/saveStore';
import { toast } from '../store/uiStore';
import { docSource } from './assBuild';
import { bindStyle } from './edits';
import { pushHistory } from './history';
import { refreshFontMetrics, syncSubs } from './subtitles';
import type { Lang } from '../types';

/**
 * 样式表的写入口。样式跟着视频走（document.json 的 styles 字段），所以这里只管
 * 「内存生效 + 标脏」，落盘完全交给文档那条保存链路（Ctrl+S / 每 5 分钟自动保存）。
 * 也正因如此，拖动时每帧改样式不需要任何防抖写盘。
 *
 * 渲染读的是 ass.ts 那个模块级单例，不是 docStore，所以两边必须一起更新。
 */
export function setDocStyles(text: string, opt: { dirty?: boolean } = {}) {
  docStore.set({ styles: text });
  setStyleSheet(text);
  bumpDoc();     // 轨道标签/时间轴块的主题色取自样式主色（docStore::laneColor）
  syncSubs();    // 整份 ASS 重解析——只重画当前帧的话画的还是旧 track
  if (opt.dirty !== false) markDirty();
}

/** 面板与预览拖动的唯一写入口：按字段改一个样式 */
export function patchStyle(name: string, patch: StylePatch) {
  const { text, normalised } = patchStyleText(docStore.get().styles, name, patch);
  setDocStyles(text);
  if (normalised) toast("样式表的 Format 行装不下这个字段，已按标准 23 字段重排");
  // 换了字体，逐字特效的排版度量和覆盖层的框宽都得重量一遍
  if ("fontname" in patch) void refreshFontMetrics();
}

// 属性条上连续调整（拖着标签改数值、拖取色器）算一步撤销：
// 同一个样式、间隔不超过这么久的改动并进上一步
const PANEL_COALESCE_MS = 800;
let panelEdit = { name: "", at: 0 };

/** 属性条的写入口：与 patchStyle 相同，外加撤销（连续调整合成一步） */
export function patchStyleFromPanel(name: string, patch: StylePatch) {
  const now = Date.now();
  if (panelEdit.name !== name || now - panelEdit.at > PANEL_COALESCE_MS) pushHistory();
  panelEdit = { name, at: now };
  patchStyle(name, patch);
}

/** 某个样式当前的 23 字段原文（面板读值走它，绕开有损的 AssStyle） */
export const styleFields = (name: string) => styleFieldsOf(docStore.get().styles, name);

/**
 * 哪些 lane 正用着这个样式。大于一条就意味着拖一个会动一片，
 * 调用方据此给警示和「复制一份专属样式」。
 */
export const stylesInUse = (name: string): StyleBinding[] =>
  lanesUsingStyle(docSource(), getStyleSheet(), name);

/** trackId → bindStyle 认的那个 target */
function targetOf(trackId: string): { kind: "track"; ti: number } | { kind: "default" } | null {
  if (trackId === DEFAULT_EFFECT_TRACK_ID) return { kind: "default" };
  const ti = docStore.get().tracks.findIndex((track, index) => (track.id || `track-${index + 1}`) === trackId);
  return ti < 0 ? null : { kind: "track", ti };
}

const laneMetaOf = (trackId: string, lang: Lang) => {
  const target = targetOf(trackId);
  if (!target) return null;
  const d = docStore.get();
  return target.kind === "default" ? d.trackMeta?.[lang] ?? null : d.tracks[target.ti][lang];
};

/**
 * 为某条 lane 复制一份专属样式并改绑。共用样式被拖动时的出路：
 * 复制 + 改绑合成一个撤销步，之后改这条轨就只动这条轨。
 */
export function cloneStyleForLane(trackId: string, lang: Lang): string | null {
  const meta = laneMetaOf(trackId, lang);
  const target = targetOf(trackId);
  if (!meta || !target) return null;
  const current = resolveStyle(meta.style ?? "", lang);
  const text = docStore.get().styles;
  const name = uniqueStyleName(current, getStyleNames());
  pushHistory();
  setDocStyles(cloneStyleText(text, current, name).text);
  bindStyle(lang, name, target);   // 自带 refreshAll() + markDirty()
  return name;
}

/**
 * 绑定指向的样式本机没有、正按 JP/CN 回退显示时，用当前这副样子把它落成真样式。
 * 绑定本来就指着这个名字，所以不用改绑。
 */
export function materializeMissingStyle(trackId: string, lang: Lang): string | null {
  const meta = laneMetaOf(trackId, lang);
  const wanted = meta?.style;
  if (!wanted || getStyleNames().includes(wanted)) return null;
  const fallback = resolveStyle(wanted, lang);
  pushHistory();
  setDocStyles(cloneStyleText(docStore.get().styles, fallback, wanted).text);
  return wanted;
}

// ── 本机默认模板 ─────────────────────────────────────────────
// 样式表本身跟着视频走，但「新视频从哪套开头」仍然是一个本机偏好：
// 存在 <数据目录>/styles.ass，只在新建/首次打开文档时当种子，不参与渲染。

/** 新文档的种子：本机默认模板，没有就用写死的那套 */
export async function machineDefaultStyles(): Promise<string> {
  const stored = await assStyles.get().catch(() => "");
  return stored?.trim() ? stored : DEFAULT_STYLE_SHEET;
}

/** 把当前视频这套存成本机默认，之后新视频都从它开头 */
export async function saveAsMachineDefault(): Promise<void> {
  await assStyles.save(docStore.get().styles);
}
