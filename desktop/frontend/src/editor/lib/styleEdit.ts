import { assStyles } from '../../bridge/assStyles';
import { lanesUsingStyle, segsUsingStyle } from '../../subtitles/build';
import type { StyleBinding } from '../../subtitles/build';
import { DEFAULT_EFFECT_TRACK_ID } from '../../subtitles/effects';
import {
  cloneStyleText, patchStyleText, removeStyleText, styleFieldsOf, uniqueStyleName,
} from '../../subtitles/styleEdit';
import type { StylePatch } from '../../subtitles/styleEdit';
import { getStyleNames, getStyleSheet, resolveStyle, setStyleSheet } from '../ass';
import { DEFAULT_STYLE_SHEET } from '../constants';
import { bumpDoc, docStore } from '../store/docStore';
import { markDirty } from '../store/saveStore';
import { toast } from '../store/uiStore';
import { docSource } from './assBuild';
import { bindStyle, refreshAll } from './edits';
import { pushHistory } from './history';
import { refreshFontMetrics, syncSubs } from './subtitles';
import type { Lang, Seg } from '../types';

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
 * 为单句的某条 lane 复制一份专属样式并改绑：从这一句当前实际用的样式复制，
 * 复制 + 改绑合成一个撤销步。base 是新名字的词干，默认沿用原名。
 */
export function cloneStyleForSeg(seg: Seg, lang: Lang, current: string, base = current): string | null {
  if (!getStyleNames().includes(current)) return null;
  const name = uniqueStyleName(base, getStyleNames());
  pushHistory();
  setDocStyles(cloneStyleText(docStore.get().styles, current, name).text);
  seg.style = { ...seg.style, [lang]: name };
  refreshAll();
  return name;
}

// 「跟随轨道样式」开关关掉时自动复制出来的专属样式，名字都带这个前缀（专属样式_你好世界、
// 撞名时 专属样式_你好世界 2…）。开关重新打开、这份样式又没人用了，就靠它认出来顺手删掉，
// 免得来回拨几下样式表里堆满副本；用户自己建的样式不会带这个前缀，不会被误删。
const SEG_COPY_PREFIX = "专属样式_";
const isSegCopy = (name: string) => name.startsWith(SEG_COPY_PREFIX);

/**
 * 专属样式的名字：前缀 + 这一句的前 4 个字。空白和逗号先去掉——逗号会截断 Style 行，
 * 末尾的「空格+数字」又会被 uniqueStyleName 当成撞名序号削掉。
 */
const segCopyName = (seg: Seg, lang: Lang) =>
  SEG_COPY_PREFIX + Array.from((seg[lang] || "").replace(/[\s,]/g, "")).slice(0, 4).join("");

/** 「跟随轨道样式」关：从这一句眼下的样式（即轨道样式）复制一份专属样式并改绑 */
export const detachSegStyle = (seg: Seg, lang: Lang, current: string): string | null =>
  cloneStyleForSeg(seg, lang, current, segCopyName(seg, lang));

/**
 * 「跟随轨道样式」开：取消这一句的专属样式。那份样式是开关自动复制出来的、
 * 又没有别的轨道或句子用着，就从样式表里一起删掉（同一个撤销步）。
 */
export function followTrackStyle(seg: Seg, lang: Lang) {
  const own = seg.style?.[lang];
  if (!own) return;
  pushHistory();
  delete seg.style![lang];
  if (!seg.style!.ja && !seg.style!.zh) delete seg.style;
  if (isSegCopy(own) && !stylesInUse(own).some(b => b.direct) && !segsUsingStyle(docSource(), own)) {
    setDocStyles(removeStyleText(docStore.get().styles, own));
  } else {
    markDirty();
  }
  refreshAll();
}

/** 有多少个字幕块单独绑了这个样式（给「改动会一起生效」的提示用） */
export const segsUsingStyleCount = (name: string): number => segsUsingStyle(docSource(), name);

/**
 * 绑定指向的样式本机没有、正按 origin/cn 回退显示时，用当前这副样子把它落成真样式。
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
