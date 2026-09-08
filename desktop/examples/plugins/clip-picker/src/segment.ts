// 把字幕平均切成若干批。

/** 每批行数上限。 */
const MAX_LINES = 800;

/** 每批向前后各借多少行。 */
const OVERLAP = 10;

/**
 * 一批字幕。有两个范围：
 *
 * - **core** `[start, end)` —— 这一批真正负责的区间。时间轴和「句数」按它算，
 *   各批首尾相接，不会重复计时。
 * - **span** `[from, to)` —— core 前后各借 OVERLAP 行。正文取这个，让模型看得到
 *   上一批的结尾和下一批的开头，批次边界上的话题才不会被腰斩。
 */
interface Batch {
  no: number;
  /** core 的行下标区间 */
  start: number;
  end: number;
  /** span 的行下标区间 */
  from: number;
  to: number;
  /** span 范围内的句子，也就是要发给模型的那些 */
  lines: SubtitleLine[];
  /** core 的起止秒 */
  t0: number;
  t1: number;
}

/**
 * 从文档里取出可用的句子：丢掉时间不合法的，按 t0 排序。
 *
 * 排序是必要的 —— 文档里的顺序不保证按时间，而切分和后面的时间轴都假设有序。
 */
function usableLines(document_: SubtitleDocument): SubtitleLine[] {
  return (document_.subtitles ?? [])
    .filter((line) => Number.isFinite(line.t0) && Number.isFinite(line.t1))
    .slice()
    .sort((a, b) => a.t0 - b.t0);
}

/**
 * 平均切分。
 *
 * 先算出「至少要几批才能让每批都不超过上限」，再把行数平均分下去，各批最多差
 * 1 行。直接每批切满上限的话，最后会剩一个几十行的尾巴，那一批的处理粒度跟前面
 * 几批不是一回事 —— 1700 行会切成 567/567/566，而不是 800/800/100。
 */
function splitSubtitles(lines: SubtitleLine[], maxLines = MAX_LINES, overlap = OVERLAP): Batch[] {
  const total = lines.length;
  if (total === 0) return [];

  const count = Math.max(1, Math.ceil(total / maxLines));
  const base = Math.floor(total / count);
  const extra = total % count;            // 前 extra 批各多分到 1 行

  const batches: Batch[] = [];
  let cursor = 0;
  for (let index = 0; index < count; index += 1) {
    const size = base + (index < extra ? 1 : 0);
    const start = cursor;
    const end = cursor + size;
    cursor = end;

    const from = Math.max(0, start - overlap);
    const to = Math.min(total, end + overlap);
    batches.push({
      no: index + 1,
      start,
      end,
      from,
      to,
      lines: lines.slice(from, to),
      t0: lines[start]!.t0,
      t1: lines[end - 1]!.t1,
    });
  }
  return batches;
}

/** 这一批真正负责多少句（不含借来的重叠行）。 */
function coreCount(batch: Batch): number {
  return batch.end - batch.start;
}

/** 「hh:mm:ss – hh:mm:ss」。日志、prompt 抬头、合并时的分组标题都要用。 */
function batchRange(batch: Batch): string {
  return `${clock(batch.t0)} – ${clock(batch.t1)}`;
}
