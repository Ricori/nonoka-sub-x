import { desktopWindows } from '../../bridge/windows.ts';
import { buildPiecesAss, missingFonts } from './assBuild.ts';
import { EDIT_SUFFIX, editSummary, exportPieces } from './videoEdit.ts';
import { getVid } from '../session.ts';
import { docStore } from '../store/docStore.ts';
import { toast } from '../store/uiStore.ts';
import { viewStore } from '../store/viewStore.ts';
import { errText } from '../utils.ts';

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
    const ass = buildPiecesAss(pieces);
    const missing = await missingFonts();
    await desktopWindows.openVideoExport(
      mediaID, baseName + suffix + '.mp4', ass, pieces, editSummary(), missing,
    );
  } catch (error) {
    toast('无法打开视频导出窗口：' + errText(error), true);
  }
}
