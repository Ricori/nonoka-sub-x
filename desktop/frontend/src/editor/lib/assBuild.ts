import { fontsMissing, getStyleMap, getStyleSheet } from '../ass';
import { docStore } from '../store/docStore';
import { screenTextStore } from '../store/screenTextStore';
import {
  buildAssFrom, buildPlatesAss, outputLinesOf, piecesAss, unknownStylesOf, usedStylesOf,
} from '../../subtitles/build';
import type { BuildAssOptions } from '../../subtitles/build';
import type { Piece } from '../../subtitles/pieces.ts';
import type { OutputLine } from '../../subtitles/build';
import { composeSheet, ensureScreenTextStyle } from '../../subtitles/styles';
import type { SubtitleSource } from '../../subtitles/types';

/**
 * 编辑器这一侧的入口：把 docStore 里那份就地可变的文档喂给 src/subtitles 的拼装管线，
 * 拼出交给 libass 的 ASS；普通字幕和特效模板都在 src/subtitles 的公共管线里生成，
 * 于是预览、导出和插件宿主拿到的字幕只差「谁来渲染」。
 */
export const docSource = (): SubtitleSource => {
  const { segs, tracks, trackMeta, effects } = docStore.get();
  return { segs, tracks, trackMeta, effects };
};

export const outputLines = (): OutputLine[] => outputLinesOf(docSource(), getStyleSheet());

/**
 * 有绑定、但这个视频的样式表里查不到的样式名。这些线会回退到 origin/cn 照常出图
 * （见 outputLines），换了副样子却一声不响，所以打开文档时得拿它提一句。
 */
export const unknownStyles = (): string[] => unknownStylesOf(docSource(), getStyleSheet());

/** options.plates: "draw" (default) or "omit" — burn export omits plates when it erases the text instead */
export const buildAss = (options: BuildAssOptions = {}): string =>
  buildAssFrom(docSource(), getStyleSheet(), options);

/** 预览画布用的 ASS：除了文档本身，还叠加「试 1 分钟」的临时文字识别（不写进文档） */
export function buildPreviewAss(): string {
  const trial = screenTextStore.get().trial;
  if (!trial?.tracks.length) return buildAss();
  const base = docSource();
  const sheet = composeSheet(ensureScreenTextStyle(docStore.get().styles));
  return buildAssFrom({ ...base, tracks: [...base.tracks, ...trial.tracks] }, sheet);
}

/** Plates for every cover box (fill or not): the text-removal mask for burn export */
export const buildPlates = (): string => buildPlatesAss(docSource(), getStyleSheet());

/** 成片 ASS：在整片那份外面包一层，按视频轨片段映射到成片时间 */
export const buildPiecesAss = (pieces: readonly Piece[], options: BuildAssOptions = {}): string =>
  piecesAss(buildAss(options), pieces);

/** buildPlates mapped to the edited cut's timeline */
export const buildPiecesPlates = (pieces: readonly Piece[]): string => piecesAss(buildPlates(), pieces);

/** 会真的出现在画面上的那些样式（轨道的 + 句子单独绑的）所引用的字体 */
export function usedFonts(): Set<string> {
  const styleMap = getStyleMap();
  const fonts = new Set<string>();
  for (const name of usedStylesOf(docSource(), getStyleSheet())) {
    const st = styleMap[name];
    if (st && st.font) fonts.add(String(st.font).trim());
  }
  return fonts;
}

/** 当前会真的出现在画面上的那些样式所引用的字体里，系统找不到的那部分 */
export const missingFonts = (): Promise<string[]> => fontsMissing([...usedFonts()]);
