// 视频轨的片段：原片上要进成片的那几段，按时间先后就是成片顺序。字幕始终用原片时间，
// 只在导出和显示时经 srcToOut 映射到成片时间——删掉的部分在编辑器里只是空隙，
// 原片和字幕数据都不动。规则与 internal/library/video_edit.go 的 normalizePieces 一致。

export interface Piece { t0: number; t1: number }

/** 比这短的片段直接丢；比这窄的空隙视作贴合（两帧宽的缝在成片里只是一闪） */
export const MIN_PIECE = 0.05;

const r3 = (v: number) => Math.round(v * 1000) / 1000;

/**
 * 排序、钳进 [0, duration]、合并重叠。相邻片段（切分点）保持分开，编辑器要靠它们
 * 单独选中和删除；窄于 MIN_PIECE 的空隙收拢成贴合。duration <= 0 视为未知，不钳上界。
 */
export function normalizePieces(pieces: readonly Piece[], duration = 0): Piece[] {
  const valid = pieces
    .map(p => ({ t0: Math.max(0, p.t0), t1: duration > 0 ? Math.min(duration, p.t1) : p.t1 }))
    .filter(p => isFinite(p.t0) && isFinite(p.t1) && p.t1 - p.t0 >= MIN_PIECE)
    .map(p => ({ t0: r3(p.t0), t1: r3(p.t1) }))
    .sort((a, b) => a.t0 - b.t0);
  const out: Piece[] = [];
  for (const p of valid) {
    const last = out[out.length - 1];
    if (last && p.t0 < last.t1) { last.t1 = Math.max(last.t1, p.t1); continue; }
    if (last && p.t0 - last.t1 < MIN_PIECE) p.t0 = last.t1;
    if (p.t1 - p.t0 >= MIN_PIECE) out.push(p);
  }
  return out;
}

/** 把首尾相接的片段并成一段：导出时切分点不该变成一次拼接（拼接处的淡入淡出会让声音一顿） */
export function joinPieces(pieces: readonly Piece[]): Piece[] {
  const out: Piece[] = [];
  for (const p of pieces) {
    const last = out[out.length - 1];
    if (last && p.t0 - last.t1 < MIN_PIECE) last.t1 = Math.max(last.t1, p.t1);
    else out.push({ ...p });
  }
  return out;
}

export const piecesDuration = (pieces: readonly Piece[]) =>
  pieces.reduce((sum, p) => sum + (p.t1 - p.t0), 0);

/** 含 t 的片段下标；落在空隙里返回 -1。区间左闭右开 */
export function pieceAt(pieces: readonly Piece[], t: number): number {
  let lo = 0, hi = pieces.length - 1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (t < pieces[mid].t0) hi = mid - 1;
    else if (t >= pieces[mid].t1) lo = mid + 1;
    else return mid;
  }
  return -1;
}

/**
 * 原片时间 → 成片时间。单调不减：落在空隙里的点压到下一段的起点，首段之前是 0，
 * 末段之后是成片总长。所以一句字幕 [a, b] 映射成 [f(a), f(b)]——整句都在空隙里的
 * 缩成 0 长，被删掉头尾的自然裁短，中间被删一截的保留成一条、时长变短。
 */
export function srcToOut(pieces: readonly Piece[], t: number): number {
  let acc = 0;
  for (const p of pieces) {
    if (t < p.t0) return acc;
    if (t < p.t1) return acc + (t - p.t0);
    acc += p.t1 - p.t0;
  }
  return acc;
}

/** 成片时间 → 原片时间。正好落在拼接点上取后一段的起点 */
export function outToSrc(pieces: readonly Piece[], o: number): number {
  if (!pieces.length) return o;
  let acc = 0;
  for (const p of pieces) {
    const len = p.t1 - p.t0;
    if (o < acc + len) return p.t0 + Math.max(0, o - acc);
    acc += len;
  }
  return pieces[pieces.length - 1].t1;
}

/** pieces 减去 [a, b)：落在里面的部分挖掉，跨边界的片段裁短或一分为二 */
export function subtractRange(pieces: readonly Piece[], a: number, b: number): Piece[] {
  const out: Piece[] = [];
  for (const p of pieces) {
    if (p.t1 <= a || p.t0 >= b) { out.push({ ...p }); continue; }
    if (p.t0 < a) out.push({ t0: p.t0, t1: a });
    if (p.t1 > b) out.push({ t0: b, t1: p.t1 });
  }
  return normalizePieces(out);
}

/** pieces 与 [a, b) 取交：只留下这个区间里的部分 */
export function intersectRange(pieces: readonly Piece[], a: number, b: number): Piece[] {
  return normalizePieces(pieces
    .filter(p => p.t1 > a && p.t0 < b)
    .map(p => ({ t0: Math.max(p.t0, a), t1: Math.min(p.t1, b) })));
}

/** 把 [a, b) 里原本的空隙补回来（补上的部分各自成段，已有的切分点不动；要并成一段见 joinPieces） */
export function addRange(pieces: readonly Piece[], a: number, b: number): Piece[] {
  const out = pieces.map(p => ({ ...p }));
  let cur = a;
  for (const p of pieces) {
    if (p.t1 <= cur) continue;
    if (p.t0 >= b) break;
    if (p.t0 > cur) out.push({ t0: cur, t1: p.t0 });
    cur = Math.max(cur, p.t1);
  }
  if (cur < b) out.push({ t0: cur, t1: b });
  return normalizePieces(out);
}

/** 在 t 处切一刀；t 离片段两端太近（切出来的一截不到 MIN_PIECE）时原样返回 */
export function splitAt(pieces: readonly Piece[], t: number): Piece[] {
  const i = pieceAt(pieces, t);
  if (i < 0) return pieces.map(p => ({ ...p }));
  const p = pieces[i];
  if (t - p.t0 < MIN_PIECE || p.t1 - t < MIN_PIECE) return pieces.map(q => ({ ...q }));
  const at = r3(t);
  return [
    ...pieces.slice(0, i).map(q => ({ ...q })),
    { t0: p.t0, t1: at }, { t0: at, t1: p.t1 },
    ...pieces.slice(i + 1).map(q => ({ ...q })),
  ];
}
