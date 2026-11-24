import { desktopWindows } from '../../bridge/windows.ts';
import { buildPiecesAss, buildPlates, docSource, missingFonts, videoErased } from './assBuild.ts';
import { EDIT_SUFFIX, editSummary, exportPieces } from './videoEdit.ts';
import { getVid } from '../session.ts';
import { docStore } from '../store/docStore.ts';
import { toast } from '../store/uiStore.ts';
import { viewStore } from '../store/viewStore.ts';
import { errText } from '../utils.ts';

export function hasCoverBoxes(): boolean {
  const src = docSource();
  if (src.segs.some(seg => !!seg.cover?.box)) return true;
  return src.tracks.some(tr => !(tr.ja.hidden && tr.zh.hidden) && tr.segs.some(seg => !!seg.cover?.box));
}

/**
 * Snapshot the current document and video track and hand video export to a
 * modeless window. The ASS goes out already mapped to output time; Go joins
 * the same pieces, so subtitles and picture line up.
 */
export async function openExport(): Promise<void> {
  const edited = exportPieces();
  const pieces = edited ?? [{ t0: 0, t1: viewStore.get().duration }];
  const mediaID = getVid();
  const title = docStore.get().title;
  const baseName = (title || mediaID).replace(/\.[a-z0-9]{2,4}$/i, '') || mediaID;
  const suffix = edited ? EDIT_SUFFIX : '';

  try {
    // 视频已经抹过字：不画底板，也不再给导出窗口「抹除原字」的选项
    const erased = videoErased();
    const ass = buildPiecesAss(pieces, erased ? { plates: 'omit' } : {});
    const hasCover = !erased && hasCoverBoxes();
    const assNoPlates = hasCover ? buildPiecesAss(pieces, { plates: 'omit' }) : '';
    const platesAss = hasCover ? buildPlates() : '';
    const missing = await missingFonts();
    await desktopWindows.openVideoExport(
      mediaID, baseName + suffix + '.mp4', ass, assNoPlates, platesAss, pieces, editSummary(), missing,
    );
  } catch (error) {
    toast('无法打开视频导出窗口：' + errText(error), true);
  }
}
