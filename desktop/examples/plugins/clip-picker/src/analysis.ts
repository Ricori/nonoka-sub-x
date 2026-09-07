// 阶段一：逐批把字幕交给模型分析。
//
// system prompt = assets/analysis.md（打包时内联进页面）
// user prompt   = 这一批的字幕正文

/** 一行字幕在 prompt 里长什么样：带时间戳，模型要靠它输出时间轴。 */
function lineText(line: SubtitleLine): string {
  const zh = (line.zh ?? "").trim();
  const ja = (line.ja ?? "").trim();
  return `[${clock(line.t0)}] ${zh || ja}`;
}

/**
 * 把一批拼成 user prompt。
 *
 * 重叠的行**单独标出来**：它们的存在是为了让模型看懂批次边界上的上下文，
 * 但如果不说清楚，模型会连它们一起写进时间轴，相邻两批就会给出重叠的时间段。
 * analysis.md 要求时间轴无缝衔接，这里必须把「负责范围」和「参考范围」分开讲。
 */
function batchUserPrompt(batch: Batch, total: number): string {
  const leading = batch.start - batch.from;           // 前面借了几行
  const core = coreCount(batch);
  const before = batch.lines.slice(0, leading);
  const body = batch.lines.slice(leading, leading + core);
  const after = batch.lines.slice(leading + core);

  const parts: string[] = [
    `这是第 ${batch.no} / ${total} 批。`,
    `你只负责 ${batchRange(batch)} 这一段，时间轴条目不要超出这个范围。`,
  ];
  if (before.length > 0) {
    parts.push(
      `\n【上文，共 ${before.length} 行，仅供理解上下文，不要为它们生成条目】\n`
      + before.map(lineText).join("\n"),
    );
  }
  parts.push(`\n【本批内容，共 ${body.length} 行】\n` + body.map(lineText).join("\n"));
  if (after.length > 0) {
    parts.push(
      `\n【下文，共 ${after.length} 行，仅供理解上下文，不要为它们生成条目】\n`
      + after.map(lineText).join("\n"),
    );
  }
  return parts.join("\n");
}

/**
 * 逐批跑分析，返回每批的原始输出。
 *
 * 串行而不是并发：真实模型有速率限制，而且串行才让「跑到第几批了」这句话有意义。
 * 某一批失败就停下并抛出 —— 已完成的那些会随异常一起交回给调用方，不白跑。
 */
async function runAnalysis(list: Batch[]): Promise<string[]> {
  const system = promptAsset("asset-analysis", "analysis.md");

  logLine(`分析开始：${list.length} 批，system prompt ${system.length} 字`);
  const results: string[] = [];

  for (const batch of list) {
    // 停止只在批与批之间生效 —— 正在飞的那次调用中断不了
    checkCancel();
    const user = batchUserPrompt(batch, list.length);
    const label = `第 ${batch.no}/${list.length} 批  ${batchRange(batch)}`;
    logLine(`${label}  送出 ${user.length} 字`);
    say(`正在分析 ${label}…`);

    const startedAt = Date.now();
    try {
      const text = await callLLM(system, user, checkAnalysis);
      const spent = ((Date.now() - startedAt) / 1000).toFixed(1);
      results.push(text);
      logLine(`${label}  返回 ${text.length} 字，用时 ${spent} 秒`);
    } catch (error) {
      logLine(`${label}  失败：${(error as Error).message}`);
      throw error;
    }
  }

  logLine(`分析完成：${results.length} 批`);
  return results;
}

/**
 * 把各批输出拼成一份「全场记录」，交给阶段二。
 *
 * 带上批号和时间范围：模型看到的只是一串文本，不标出每段覆盖哪个时间区间，
 * 它没法判断顺序，也分不清两组之间是接续还是重复 —— 而 to_excel.md 要求
 * 合并成一份连续的、按 start 升序的记录。
 */
function joinAnalyses(list: Batch[], results: string[]): string {
  return results.map((text, index) => {
    const batch = list[index];
    const head = batch
      ? `===== 第 ${batch.no} / ${list.length} 批（${batchRange(batch)}）=====`
      : `===== 第 ${index + 1} 组 =====`;
    return `${head}\n${text.trim()}`;
  }).join("\n\n");
}

/**
 * 这一批的输出能不能用。不合格 callLLM 会重试。
 *
 * 只查两件最要命的：不能是空的（或短得不可能是分析），以及必须有时间轴条目 ——
 * 阶段二完全靠「模块一」的时间段生成表格行，没有它这一批等于白跑。
 *
 * 故意不查得更细：查得越死，模型只是换了个合法写法就会被打回，白白烧掉 5 次
 * 重试和用户的额度。
 */
const TIMELINE_HINT = /\d{1,2}:\d{2}(?::\d{2})?\s*[-–—]\s*\d{1,2}:\d{2}/;

function checkAnalysis(text: string): void {
  const body = text.trim();
  if (body.length < 50) throw new Error(`输出太短（${body.length} 字），不像是分析结果`);
  if (!TIMELINE_HINT.test(body)) {
    throw new Error("没有找到「模块一」的时间轴条目（形如 00:12:30 - 00:15:00）");
  }
}
