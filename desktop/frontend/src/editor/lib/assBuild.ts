import { fontsMissing, getStyleMap, getStyleSheet } from '../ass';
import { docStore } from '../store/docStore';
import { buildAssFrom, outputLinesOf, piecesAss, unknownStylesOf } from '../../subtitles/build';
import type { Piece } from '../../subtitles/pieces.ts';
import type { OutputLine } from '../../subtitles/build';
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

export const buildAss = (): string => buildAssFrom(docSource(), getStyleSheet());

/** 成片 ASS：在整片那份外面包一层，按视频轨片段映射到成片时间 */
export const buildPiecesAss = (pieces: readonly Piece[]): string => piecesAss(buildAss(), pieces);

/** 当前会真的出现在画面上的那些样式所引用的字体里，系统找不到的那部分 */
export function missingFonts(): Promise<string[]> {
  const styleMap = getStyleMap();
  const names = new Set<string>();
  for (const L of outputLines()) {
    const st = styleMap[L.style];
    if (st && st.font) names.add(String(st.font).trim());
  }
  return fontsMissing([...names]);
}
