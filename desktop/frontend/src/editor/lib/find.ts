import { activeHit, findStore, type FindField, type Hit } from '../store/findStore';
import { orderedTis, segsOf, tiPos } from '../store/docStore';
import { markDirty } from '../store/saveStore';
import { curSegs, select, selStore, setActiveTrack } from '../store/selectionStore';
import { toast } from '../store/uiStore';
import { listedIdx } from '../store/viewStore';
import { refreshAll } from './edits';
import { pushHistory } from './history';
import { seek } from './playback';
import type { Lang, Seg } from '../types';

/**
 * 字幕查找/替换。命中集合是「现算」的：句文本随时可能被检查器、撤销、拖动改掉，
 * 存下来的下标一会儿就过期，所以文档一变就整份重算（见 FindBar 的 refreshHits）。
 */

/** 命中排序/定位用的位置：轨序 → 句下标 → lane → 字符位置 */
interface Anchor {
  ti: number;
  i: number;
  lang: Lang;
  pos: number;
}

const langRank = (lang: Lang) => (lang === "ja" ? 0 : 1);

const langsOf = (field: FindField): Lang[] => (field === "both" ? ["ja", "zh"] : [field]);

/**
 * text 里所有命中的 [start, end)。不区分大小写时两边各自转小写再找——
 * 个别字符转小写后长度会变（如 İ → i̇），那样下标就对不上原文了，这时退回区分大小写。
 */
export function matchRanges(text: string, q: string, matchCase: boolean): [number, number][] {
  if (!text || !q) return [];
  let hay = text, needle = q;
  if (!matchCase) {
    const lt = text.toLowerCase(), lq = q.toLowerCase();
    if (lt.length === text.length && lq.length === q.length) { hay = lt; needle = lq; }
  }
  const out: [number, number][] = [];
  for (let from = 0; ;) {
    const k = hay.indexOf(needle, from);
    if (k < 0) break;
    out.push([k, k + needle.length]);
    from = k + needle.length;   // 命中不重叠：「aa」在「aaa」里算一处
  }
  return out;
}

/** 按当前条件扫一遍，结果已按 轨 → 句 → lane → 位置 有序 */
function collectHits(): Hit[] {
  const f = findStore.get();
  if (!f.query) return [];
  const tis = f.allTracks ? orderedTis() : [selStore.get().curTrack];
  const langs = langsOf(f.field);
  const hits: Hit[] = [];
  for (const ti of tis) {
    const arr = segsOf(ti);
    // 聚焦成片时只找成片里还有的句：列表、时间轴显示的也是这些
    for (const i of listedIdx(arr)) {
      const seg = arr[i];
      for (const lang of langs) {
        for (const [start, end] of matchRanges(seg[lang], f.query, f.matchCase)) {
          hits.push({ ti, i, seg, lang, start, end });
        }
      }
    }
  }
  return hits;
}

const cmpHit = (h: Hit, a: Anchor) =>
  (tiPos(h.ti) - tiPos(a.ti)) || (h.i - a.i) || (langRank(h.lang) - langRank(a.lang)) || (h.start - a.pos);

/** 没指定锚点时：先跟住当前那处命中，其次跟住列表选中的句 */
function currentAnchor(): Anchor | null {
  const cur = activeHit();
  if (cur) return { ti: cur.ti, i: cur.i, lang: cur.lang, pos: cur.start };
  const { curTrack, sel } = selStore.get();
  return sel < 0 ? null : { ti: curTrack, i: sel, lang: "ja", pos: 0 };
}

/**
 * 重算命中并把光标落到锚点之后的第一处（锚点之后没有就绕回第一处）。
 * 传 null = 不跟随，直接落到第一处。
 */
export function refreshHits(anchor?: Anchor | null) {
  const hits = collectHits();
  const a = anchor === undefined ? currentAnchor() : anchor;
  let cursor = hits.length ? 0 : -1;
  if (hits.length && a) {
    const k = hits.findIndex(h => cmpHit(h, a) >= 0);
    if (k >= 0) cursor = k;
  }
  findStore.set({ hits, cursor });
}

/** 跳到某处命中：切到它所在的轨，选中那一句并把播放头移过去 */
function gotoHit(h: Hit) {
  setActiveTrack(h.ti, { silent: true });
  const i = curSegs().indexOf(h.seg);   // 句可能已被增删挪位，按引用重新定位
  if (i < 0) return;
  select(i);
  seek(h.seg.t0 + 0.01);
}

/** 上一个 / 下一个（到头绕回另一端） */
export function stepHit(dir: 1 | -1) {
  const { hits, cursor, query } = findStore.get();
  if (!query) return;
  if (!hits.length) { toast("没有匹配的字幕"); return; }
  const n = hits.length;
  const next = cursor < 0 ? (dir > 0 ? 0 : n - 1) : (cursor + dir + n) % n;
  findStore.set({ cursor: next });
  gotoHit(hits[next]);
}

// ── 替换 ──────────────────────────────────────────────────────────
// 不动 low_conf：批量替换个别词不等于人把整句审过一遍，标记留着继续提示。

export function replaceCurrent() {
  const f = findStore.get();
  const h = activeHit();
  if (!f.query) return;
  if (!h) { toast("没有匹配的字幕"); return; }
  const text = h.seg[h.lang];
  const got = text.slice(h.start, h.end);
  const same = f.matchCase ? got === f.query : got.toLowerCase() === f.query.toLowerCase();
  if (!same) { refreshHits(); toast("字幕已变化，已重新查找"); return; }
  pushHistory();
  h.seg[h.lang] = text.slice(0, h.start) + f.repl + text.slice(h.end);
  refreshAll(); markDirty();
  // 光标落到「替换后文本之后」的第一处，替进去的内容本身含有查找词时才不会原地打转
  refreshHits({ ti: h.ti, i: h.i, lang: h.lang, pos: h.start + f.repl.length });
  const nx = activeHit();
  if (nx) gotoHit(nx);
  toast("已替换 1 处");
}

export function replaceAllHits() {
  const f = findStore.get();
  if (!f.query) return;
  const hits = collectHits();
  if (!hits.length) { toast("没有匹配的字幕"); return; }
  pushHistory();
  // 同一 lane 内从后往前替换：先改后面的，前面那些命中的下标才不会被挪掉
  const groups = new Map<Seg, Record<Lang, Hit[]>>();
  for (const h of hits) {
    let g = groups.get(h.seg);
    if (!g) { g = { ja: [], zh: [] }; groups.set(h.seg, g); }
    g[h.lang].push(h);
  }
  for (const [seg, g] of groups) {
    for (const lang of ["ja", "zh"] as Lang[]) {
      const list = g[lang];
      if (!list.length) continue;
      let text = seg[lang];
      for (let k = list.length - 1; k >= 0; k--) {
        text = text.slice(0, list[k].start) + f.repl + text.slice(list[k].end);
      }
      seg[lang] = text;
    }
  }
  refreshAll(); markDirty();
  refreshHits(null);
  toast("已替换 " + hits.length + " 处（共 " + groups.size + " 句）", false, null, undefined, true);
}

// ── 开关与输入 ────────────────────────────────────────────────────
/** seed：Ctrl+F 时若文本框里选了字，直接拿来当查找词 */
export function openFind(seed?: string) {
  const s = (seed || "").trim();
  findStore.set(prev => ({ open: true, focusSeq: prev.focusSeq + 1, query: s || prev.query }));
  refreshHits(s ? null : undefined);
}

export const closeFind = () => findStore.set({ open: false, hits: [], cursor: -1 });

export const toggleFind = () => (findStore.get().open ? closeFind() : openFind());

export function setQuery(query: string) { findStore.set({ query }); refreshHits(); }
export const setRepl = (repl: string) => findStore.set({ repl });
export function setField(field: FindField) { findStore.set({ field }); refreshHits(); }
export function toggleMatchCase() { findStore.set(s => ({ matchCase: !s.matchCase })); refreshHits(); }
export function toggleAllTracks() { findStore.set(s => ({ allTracks: !s.allTracks })); refreshHits(); }
