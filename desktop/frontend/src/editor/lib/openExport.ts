import { desktopWindows } from '../../bridge/windows.ts';
import { buildClipAss, missingFonts } from './assBuild.ts';
import { getVid } from '../session.ts';
import { docStore } from '../store/docStore.ts';
import { toast } from '../store/uiStore.ts';
import { viewStore } from '../store/viewStore.ts';
import type { Clip } from '../types.ts';
import { errText, fmt } from '../utils.ts';

/** Snapshot the current document and hand video export to a modeless window. */
export async function openExport(clip: Clip | null): Promise<void> {
  const view = viewStore.get();
  const exportClip = clip || view.curClip;
  const t0 = exportClip ? exportClip.t0 : view.t0;
  const t1 = exportClip ? exportClip.t1 : view.t1;
  const mediaID = getVid();
  const title = docStore.get().title;
  const baseName = (title || mediaID).replace(/\.[a-z0-9]{2,4}$/i, '') || mediaID;
  const suffix = exportClip ? ` - ${exportClip.name}` : '';
  const rangeLabel = `${exportClip ? `切片「${exportClip.name}」` : '完整片'}　${fmt(t0)} → ${fmt(t1)}　共 ${(t1 - t0).toFixed(1)}s`;

  try {
    const ass = buildClipAss(t0, t1);
    const missing = await missingFonts();
    await desktopWindows.openVideoExport(
      mediaID, baseName + suffix + '.mp4', ass, t0, t1, rangeLabel, missing,
    );
  } catch (error) {
    toast('无法打开视频导出窗口：' + errText(error), true);
  }
}
