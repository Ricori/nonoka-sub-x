import { getVid } from '../session';
import { docStore } from '../store/docStore';
import { toast } from '../store/uiStore';
import { errText } from '../utils';
import { buildAss, buildPiecesAss } from './assBuild';
import { EDIT_SUFFIX, exportPieces } from './videoEdit';
import { mediaLibrary } from '../../bridge/library.ts';

/**
 * 导出 ASS。整片和剪辑成片都在本地拼（buildAss 与服务端 edit_export_ass 逐字一致，也正是
 * 预览用的那份），不走服务端：导出算的就是屏幕上这份文档，离线也能出，
 * 也不必先把改动 PUT 上去等一轮往返。
 */
export async function exportAss() {
  try {
    const { title } = docStore.get();
    const pieces = exportPieces();
    let name = (title || getVid()).replace(/\.[a-z0-9]{2,4}$/i, "") || getVid();
    let content: string;
    if (pieces) {
      content = buildPiecesAss(pieces);
      name += EDIT_SUFFIX;
    } else {
      content = buildAss();
    }
    const path = await mediaLibrary.saveSubtitle(name + ".ass", content);
    if (!path) return;
    toast("已导出 " + path + " · 点此打开所在文件夹",
      true, () => void mediaLibrary.revealInFolder(path), 5000, true);
  } catch (e) {
    toast("导出失败：" + errText(e));
  }
}
