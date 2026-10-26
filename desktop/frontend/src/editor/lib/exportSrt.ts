import { getVid } from '../session';
import { docStore } from '../store/docStore';
import { toast } from '../store/uiStore';
import { errText } from '../utils';
import { buildSrt, type SrtLang } from './srtBuild';
import { EDIT_SUFFIX, exportPieces } from './videoEdit';
import { mediaLibrary } from '../../bridge/library.ts';

/**
 * 文件名后缀：双语不加，单语标出来，免得三份导出重名互相覆盖。
 * 原文字段虽叫 ja，装的却是任务所选的原始语言（中文、英语、自动检测都可能），
 * 文档里又没记下它，只好看导出内容本身用的是哪种文字。
 */
function suffixOf(lang: SrtLang, content: string): string {
  if (lang === "both") return "";
  if (lang === "zh") return " - 中文";
  const text = content.replace(/^\d+\s*$|^[\d:,.]+ --> [\d:,.]+.*$/gm, "");
  const count = (re: RegExp) => text.match(re)?.length ?? 0;
  const kana = count(/[぀-ヿ]/g);
  const hangul = count(/[가-힯]/g);
  const han = count(/\p{Script=Han}/gu);
  const latin = count(/[A-Za-z]/g);
  const cjk = kana + hangul + han;
  // 日文句子里假名占比很高，夹几个片假名外来语的中文远到不了这个比例
  if (kana > 0 && kana >= cjk * 0.25) return " - 日文";
  if (hangul > 0 && hangul >= cjk * 0.5) return " - 韩文";
  // 与译文的「中文」区分开，原文本身是中文时两份导出也不会重名
  if (han > 0 && han * 2 >= latin) return " - 中文原文";
  if (latin > 0) return " - 英文";
  return " - 原文";
}

/**
 * 导出 SRT：所有未隐藏轨道按时间摊平成一条流（轨道与样式要保留就走 ASS）。
 * 与导出 ASS 一样整片、剪辑成片都在本地拼，不占服务端资源（对照基准见 srtBuild）。
 */
export async function exportSrt(lang: SrtLang) {
  try {
    const { title } = docStore.get();
    const pieces = exportPieces();
    let name = (title || getVid()).replace(/\.[a-z0-9]{2,4}$/i, "") || getVid();
    let content: string;
    if (pieces) {
      content = buildSrt(lang, pieces);
      name += EDIT_SUFFIX;
    } else {
      content = buildSrt(lang);
    }
    // 关了翻译的产物导「仅译文」会是空文件，与其存一个空壳不如直接说清楚
    if (!content.trim()) { toast("没有可导出的内容：可见轨道里该语言全是空的"); return; }
    const path = await mediaLibrary.saveSubtitle(name + suffixOf(lang, content) + ".srt", content);
    if (!path) return;
    toast("已导出 " + path + " · 点此打开所在文件夹",
      true, () => void mediaLibrary.revealInFolder(path), 5000, true);
  } catch (e) {
    toast("导出失败：" + errText(e));
  }
}
