import { HIDDEN_H, ROW_H0 } from '../constants';
import { bumpDoc, docStore } from '../store/docStore';
import { layoutStore, saveLayout, setRowH } from '../store/layoutStore';
import { markDirty } from '../store/saveStore';
import { foldJa } from './edits';
import type { RowSpec } from '../types';

/**
 * 时间轴的行：视频行 + 音频行 + 默认轨两条 lane + 每条自定义轨两条 lane。标签栏和轨道区共用这一份，
 * 两列高度才对得上。前四行的高度存本机，自定义轨高度随轨道对象存服务端。
 */
export function buildRows(): RowSpec[] {
  const { trackMeta, tracks } = docStore.get();
  const l = layoutStore.get();
  const fold = foldJa();
  const rows: RowSpec[] = [
    {
      key: "video", kind: "video", ti: -1, lang: null, fold: false, vis: true, local: true,
      height: l.rowH.video, setHeight: v => setRowH("video", v),
    },
    {
      key: "wave", kind: "wave", ti: -1, lang: null, fold: false, vis: true, local: true,
      height: l.rowH.wave, setHeight: v => setRowH("wave", v),
    },
    {
      key: "d-ja", kind: "lane", ti: -1, lang: "ja", fold, vis: !trackMeta?.ja.hidden, local: true,
      height: l.rowH.ja, setHeight: v => setRowH("ja", v),
    },
    {
      key: "d-zh", kind: "lane", ti: -1, lang: "zh", fold: false, vis: !trackMeta?.zh.hidden, local: true,
      height: l.rowH.zh, setHeight: v => setRowH("zh", v),
    },
  ];
  tracks.forEach((tr, ti) => {
    rows.push({
      key: tr.id + "-ja", kind: "lane", ti, lang: "ja", fold, vis: !tr.ja.hidden, local: false,
      height: tr.hja || ROW_H0, setHeight: v => { tr.hja = v; bumpDoc(); },
    });
    rows.push({
      key: tr.id + "-zh", kind: "lane", ti, lang: "zh", fold: false, vis: !tr.zh.hidden, local: false,
      height: tr.hzh || ROW_H0, setHeight: v => { tr.hzh = v; bumpDoc(); },
    });
  });
  return rows;
}

/** 隐藏行压成 22px 标签行（保留眼睛按钮以便再显示）；整体折叠的行彻底不占位 */
export const rowDisplayH = (r: RowSpec) => r.fold ? 0 : (r.vis ? r.height : HIDDEN_H);

/** 拖行间手柄改高度：本机偏好写 localStorage，自定义轨高度存服务端 */
export function applyRowHeight(r: RowSpec, v: number) {
  r.setHeight(v);
  if (r.local) saveLayout(); else markDirty();
}
