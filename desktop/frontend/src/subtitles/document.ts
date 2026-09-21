import { DEFAULT_STYLE_SHEET } from './constants.ts';
import { buildAssFrom, buildSrtFrom, clipAss } from './build.ts';
import { normalizeEffectBindings } from './effects.ts';
import type { SrtLang } from './build.ts';
import { composeSheet } from './styles.ts';
import type { SubtitleSource } from './types.ts';
import type { EditDocument } from '../documents/types.ts';

/**
 * 服务端读回的 EditDocument → 拼装管线的输入。编辑器把文档摊进 docStore 再拼，
 * 插件宿主没有 docStore，直接按同样的字段映射喂进去。
 */
export function sourceOfDocument(document: EditDocument): SubtitleSource {
  const source: SubtitleSource = {
    segs: document.subtitles ?? [],
    tracks: document.tracks ?? [],
    trackMeta: document.track_meta ?? null,
    effects: normalizeEffectBindings(document.effects),
  };
  return source;
}

/**
 * 该用哪份样式表：文档自带的优先，其次是传进来的本机默认模板（老文档还没有 styles
 * 字段时就走它，与编辑器打开文档时的种子口径一致），最后才是写死的那套。
 */
export const styleSheetText = (docStyles?: string, fallback?: string) =>
  docStyles?.trim() || fallback?.trim() || DEFAULT_STYLE_SHEET;

export interface SubtitleRange { t0: number; t1: number }

/**
 * 文档 → ASS，与编辑器导出的那份逐字相同。
 * fallback 是「本机默认模板」，只在文档还没有自己那份样式表时才用得上。
 */
export function documentAss(document: EditDocument, fallback?: string, range?: SubtitleRange): string {
  const sheet = composeSheet(styleSheetText(document.styles, fallback));
  const full = buildAssFrom(sourceOfDocument(document), sheet);
  return range ? clipAss(full, range.t0, range.t1) : full;
}

/** 文档 → SRT，与编辑器导出的那份逐字相同 */
export function documentSrt(document: EditDocument, lang: SrtLang, range?: SubtitleRange): string {
  const source = sourceOfDocument(document);
  return range ? buildSrtFrom(source, lang, range.t0, range.t1) : buildSrtFrom(source, lang);
}
