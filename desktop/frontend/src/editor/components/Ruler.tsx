import { shallowEqual } from '../../home/lib/createStore';
import { bindScrub } from '../lib/laneDrag';
import { isFocused, tOf, viewStore, xOf } from '../store/viewStore';
import { fmt } from '../utils';
import { joinPieces } from '../../subtitles/pieces.ts';

// 放大到 400px/s 时 0.5s 一格太疏，补上 0.1/0.2 两档（标签显示到秒的两位小数）
const STEPS = [0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800];

interface Tick { x: number; major: boolean; label?: string; endlab?: boolean }

/**
 * 一段时间连续的区间：原片上 [s0, s1) 这一截，标签从 o0 起算。
 * 聚焦成片时每组首尾相接的片段是一截，标签是成片时间；否则整个视图就是一截，标签是原片时间。
 */
interface Span { s0: number; s1: number; o0: number }

function spansOf(): Span[] {
  const v = viewStore.get();
  if (!isFocused()) return [{ s0: v.t0, s1: v.t1, o0: v.t0 }];
  let acc = 0;
  return joinPieces(v.pieces!).map(p => {
    const span = { s0: p.t0, s1: p.t1, o0: acc };
    acc += p.t1 - p.t0;
    return span;
  });
}

/**
 * 60 分钟 × 140px/s 的整条时间轴远超一次能铺的量，刻度只生成视口附近的一段。
 * 刻度按标签时间取整、逐截生成：成片时间跨过删掉的部分也是连续的，标签不会变成 00:13.4 这种零头。
 */
function ticksFor(left: number, vw: number, pps: number): Tick[] {
  const major = STEPS.find(s => s * pps >= 70) || 3600;
  const minor = major / 5;
  const endX = xOf(viewStore.get().t1);
  const ta = tOf(left - vw * 0.5), tb = tOf(left + vw * 1.5);
  const ticks: Tick[] = [];
  let lastO = -Infinity;
  spansOf().forEach(sp => {
    const a = Math.max(sp.s0, ta), b = Math.min(sp.s1, tb);
    if (b < a) return;
    const oa = sp.o0 + (a - sp.s0), ob = sp.o0 + (b - sp.s0);
    for (let o = Math.ceil(oa / minor - 1e-6) * minor; o <= ob + 1e-6; o += minor) {
      // 成片时间里相邻两截首尾相接，接缝上那一格前后各生成一次，只留一个
      if (o <= lastO + 1e-6) continue;
      lastO = o;
      const x = xOf(sp.s0 + (o - sp.o0));
      const isMajor = Math.abs(o / major - Math.round(o / major)) < 1e-6;
      ticks.push(isMajor
        // 亚秒刻度要带小数位，否则相邻标签一模一样
        ? { x, major: true, label: major < 1 ? fmt(o) : fmt(o).slice(0, 5), endlab: endX - x < 46 }
        : { x, major: isMajor });
    }
  });
  return ticks;
}

export function Ruler({ left, w }: { left: number; w: number }) {
  const { pps } = viewStore.use(
    s => ({ pps: s.pps, t0: s.t0, t1: s.t1, pieces: s.pieces, focus: s.focus }), shallowEqual);
  const ticks = ticksFor(left, w, pps);

  return (
    <div className="ruler" id="ruler" onPointerDown={e => bindScrub(e, false)}>
      {ticks.map(t => (
        <div key={t.x.toFixed(2)} className={"tick" + (t.major ? " major" : "") + (t.endlab ? " endlab" : "")}
          style={{ left: t.x + "px" }}>
          {t.label && <span>{t.label}</span>}
        </div>
      ))}
    </div>
  );
}
