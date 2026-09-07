// 阶段二：把各批的分析结果合并，让模型汇总成一份 JSON。
//
// system prompt = assets/to_excel.md（打包时内联进页面）
// user prompt   = analysis.ts 拼好的那份「全场记录」

/**
 * 汇总成表。返回模型的原始输出（按 to_excel.md 的要求应该是一个 JSON 对象）。
 *
 * 只调一次：合并本身要求模型同时看到全部批次，拆开调就没法把跨批次的重复
 * 时间段合起来。
 */
async function runToExcel(record: string): Promise<string> {
  if (!record.trim()) throw new Error("没有可汇总的分析结果");

  const system = promptAsset("asset-to-excel", "to_excel.md");

  logLine(`汇总开始：全场记录 ${record.length} 字，system prompt ${system.length} 字`);
  say("正在汇总成表格…");

  // 失败不在这里 catch：main 的那层已经会记「失败：…」，再记一遍只是把同一句
  // 话写两行。分析那边之所以 catch，是因为它能补上「第几批」这个额外信息。
  const startedAt = Date.now();
  // 校验就是「能不能解析成表」本身 —— parseSummary 抛错即视为不合格，
  // callLLM 会带着错误原因重试。
  const text = await callLLM(system, record, parseSummary);
  logLine(`汇总完成：返回 ${text.length} 字，用时 ${((Date.now() - startedAt) / 1000).toFixed(1)} 秒`);
  return text;
}
