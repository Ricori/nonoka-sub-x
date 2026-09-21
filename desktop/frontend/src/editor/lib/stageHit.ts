import { cuesOf, generatorOf } from '../../subtitles/build';
import { bandBlock, layoutBlock, vGroupOf } from '../../subtitles/layout';
import type { BlockRect } from '../../subtitles/layout';
import { getStyleSheet } from '../ass';
import { playStore } from '../store/playStore';
import { outputLines } from './assBuild';
import type { Lang, Seg } from '../types';

/**
 * 画面上那些字幕块各自占哪块地方——预览里点选、拖动、画选中框都靠它。
 *
 * 坐标取自 src/subtitles/layout.ts，也就是逐字特效用的同一份排版复算，所以框和 libass
 * 真正画出来的字是同一套口径。两处近似要知道：
 *
 * - 字宽是 canvas 量出来逼近 libass 的（见 metrics.ts），有几个像素的出入；
 * - libass 的碰撞避让这里只做了简易模拟（见下），复杂场合会对不齐。
 *
 * 两者都只影响**画出来的框**。拖动落盘的数值是按位移增量算的，不吃这些误差。
 */
export interface SubtitleBox {
  /** outputLines 里的下标，也就是堆叠次序：越大越靠上层 */
  index: number;
  trackId: string;
  lang: Lang;
  /** 已经过 resolveStyle 回退的样式名——画面上真正生效的那个 */
  styleName: string;
  /** 绑定上写的名字；样式表里没有它时与 styleName 不同 */
  boundStyle: string | null;
  trackName: string;
  seg: Seg | null;
  text: string;
  block: BlockRect;
  /** 被碰撞避让往回推了多少（0 = 没推）。不为 0 时框会偏离 MarginV 说的位置 */
  shifted: number;
  /** 这条线绑了生成型特效，真实画面是逐字 \pos 事件，框只是近似 */
  approximate: boolean;
}

export interface LaneRef { trackId: string; lang: Lang }

export const sameLane = (a: LaneRef | null, b: LaneRef | null) =>
  !!a && !!b && a.trackId === b.trackId && a.lang === b.lang;

/**
 * libass 的 fix_collisions 的简易模拟：下对齐的行互相挤时往上让，上对齐的往下让。
 * 中间对齐那三个不动——libass 不像边对齐那样堆叠它们，硬堆会把框画到没有字的地方。
 *
 * 这里只处理本编辑器会发出的那种事件（全是 Layer 0、没有 \pos、默认碰撞模式）。
 */
function avoidCollisions(boxes: SubtitleBox[], playResY: number) {
  let floor = playResY;      // 下对齐组：已占用区域的上沿
  let ceiling = 0;           // 上对齐组：已占用区域的下沿
  for (const box of boxes) {
    const group = vGroupOf(styleAlign(box));
    if (group === 1) {
      const overflow = box.block.top + box.block.height - floor;
      if (overflow > 0) shift(box, -overflow);
      floor = box.block.top;
    } else if (group === 3) {
      const overflow = ceiling - box.block.top;
      if (overflow > 0) shift(box, overflow);
      ceiling = box.block.top + box.block.height;
    }
  }
}

const styleAlign = (box: SubtitleBox) => getStyleSheet().styleMap[box.styleName]?.align ?? 2;

function shift(box: SubtitleBox, dy: number) {
  box.block.top += dy;
  for (const line of box.block.lines) line.top += dy;
  box.shifted += dy;
}

/** 当前播放头时刻画面上的所有字幕块，按绘制顺序（后画的在数组后面） */
export function subtitleBoxesAt(t = playStore.get().t): SubtitleBox[] {
  const sheet = getStyleSheet();
  const boxes: SubtitleBox[] = [];
  outputLines().forEach((line, index) => {
    const style = sheet.styleMap[line.style];
    if (!style) return;
    // 时间口径必须与 buildAssFrom 一致：钳重叠会改出点，自己再算一遍就会选错句
    const cue = cuesOf(line).find(c => t >= c.t0 && t < c.t1);
    if (!cue) return;
    // 取原始文本：assTx 会把换行转成 \N，layoutBlock 认的是 \n
    const text = (cue.segment[line.lang] || "").trim();
    if (!text) return;
    boxes.push({
      index,
      trackId: line.trackId,
      lang: line.lang,
      styleName: line.style,
      boundStyle: line.meta.style,
      trackName: line.name,
      seg: cue.segment as Seg,
      text,
      block: layoutBlock(text, style, sheet),
      shifted: 0,
      approximate: !!generatorOf(line),
    });
  });
  avoidCollisions(boxes, sheet.playRes.y);
  return boxes;
}

/**
 * 某条 lane 在当前时刻的框。没有可见句时给一个「带」——擦洗时每越过一段空隙就丢一次
 * 选中会很难用，所以空隙里也要能继续选中和拖动。
 */
export function boxForLane(lane: LaneRef, t = playStore.get().t): SubtitleBox | null {
  const live = subtitleBoxesAt(t).find(box => sameLane(box, lane));
  if (live) return live;

  const sheet = getStyleSheet();
  const found = outputLines().findIndex(line => line.trackId === lane.trackId && line.lang === lane.lang);
  if (found < 0) return null;
  const line = outputLines()[found];
  const style = sheet.styleMap[line.style];
  if (!style) return null;
  return {
    index: found,
    trackId: line.trackId,
    lang: line.lang,
    styleName: line.style,
    boundStyle: line.meta.style,
    trackName: line.name,
    seg: null,
    text: "",
    block: bandBlock(style, sheet),
    shifted: 0,
    approximate: !!generatorOf(line),
  };
}

const inside = (box: SubtitleBox, x: number, y: number) =>
  x >= box.block.left && x <= box.block.left + box.block.width
  && y >= box.block.top && y <= box.block.top + box.block.height;

/**
 * 点中了哪一块。命中多个时默认取最上层（数组末尾），`cycleFrom` 给的是上一次命中的
 * 下标，用来在重叠的块之间轮换（Alt+点击）。
 *
 * 命中测试用整块的外接矩形而不是逐行：短行之间留空档会让人觉得点不中。
 */
export function hitTest(boxes: SubtitleBox[], x: number, y: number, cycleFrom?: number): SubtitleBox | null {
  const hits = boxes.filter(box => inside(box, x, y));
  if (!hits.length) return null;
  if (cycleFrom === undefined) return hits[hits.length - 1];
  const at = hits.findIndex(box => box.index === cycleFrom);
  return hits[(at + hits.length - 1) % hits.length];
}
