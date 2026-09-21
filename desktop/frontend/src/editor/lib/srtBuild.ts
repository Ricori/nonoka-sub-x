import { buildSrtFrom } from '../../subtitles/build';
import type { SrtLang } from '../../subtitles/build';
import { docSource } from './assBuild';
import type { Piece } from '../../subtitles/pieces.ts';

export type { SrtLang };

/**
 * 编辑器这一侧的 SRT 入口：规则与 vod/api/edit.py::edit_export_srt 逐字相同，
 * 实现在 src/subtitles/build.ts（插件宿主共用同一份）。SRT 没有多轨概念，只能把
 * 各轨摊平成一条时间流：同时开口的两个人就是两条时间重叠的字幕，怎么摆由播放器决定。
 * 要保留轨道与样式走 ASS。
 */
export function buildSrt(lang: SrtLang, pieces?: readonly Piece[]): string {
  return buildSrtFrom(docSource(), lang, pieces);
}
