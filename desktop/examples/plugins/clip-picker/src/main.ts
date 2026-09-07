// 接线与启动。必须是 tsconfig 的 files 里最后一个：只有这里有顶层执行代码，
// 前面各文件都只做声明，所以拼接顺序不会踩到「用了还没初始化的东西」。

// 插件页是 iframe，切走就被宿主销毁 —— 流水线的循环跑在页面的 JS 里，进度和
// 已完成的批次都留不下来，宿主侧也没有通用存储能提前接住它们。只能提醒。
const RUNNING_NOTICE = "处理期间请不要切换页面：插件页会被销毁，进度和已经跑完的批次都会丢失。想中途停下就点「停止」，会在当前这一批返回后停。";
const DONE_NOTICE = "结果只存在这个页面里，切换页面就会丢 —— 先「复制为表格」存走。";

/** 流水线是不是正在跑。「开始」按钮靠它决定自己该是开始还是停止。 */
let running = false;

/**
 * 阶段一跑完就存下来。
 *
 * 阶段二失败（重试 5 次仍不合格、额度用尽、限流、超时）时，前面那些分析结果
 * 本来会随着局部变量一起丢掉 —— 而它们是这条流水线里最贵的东西：一场四小时的
 * 直播是六次模型调用、两三分钟。偏偏阶段二正是实际会失败的那一步。
 *
 * 存住之后「重新汇总」就只花一次调用，不用把分析重跑一遍。
 * 注意它跳过的是阶段一，不是跑一部分 —— 出来的仍是整场直播的完整表。
 */
let stageOne: { batches: Batch[]; analyses: string[] } | null = null;

function setResummarize(visible: boolean): void {
  el("resummarize").hidden = !visible;
}

/**
 * 跑完整条流水线。返回 false 表示「正常地没跑出结果」（比如文档里没有句子），
 * 出错则抛出 —— 界面怎么收场由 start() 统一决定。
 */
async function runPipeline(media: MediaSummary): Promise<boolean> {
  clearLog();
  logLine(`开始：「${media.title}」（${clock(media.duration)}）`);
  say("正在读取字幕文档…");

  const document_ = await rpc<SubtitleDocument>("document.read", { mediaId: media.id });
  const lines = usableLines(document_);
  logLine(`读到字幕文档 rev ${document_.rev}，${lines.length} 句`);
  if (lines.length === 0) {
    say("这份文档里没有字幕句子。", true);
    logLine("没有可用的句子，停下");
    return false;
  }

  const batches = splitSubtitles(lines);
  const sizes = batches.map(coreCount);
  const spread = batches.length > 1 ? `每批 ${Math.min(...sizes)}–${Math.max(...sizes)} 行` : `${sizes[0]} 行`;
  logLine(`${lines.length} 句切成 ${batches.length} 批，${spread}`
    + `（上限 ${MAX_LINES} 行，前后各重叠 ${OVERLAP} 行）`);
  for (const batch of batches) {
    logLine(`  第 ${batch.no}/${batches.length} 批  ${batchRange(batch)}`
      + `  ${coreCount(batch)} 句（含重叠 ${batch.lines.length}）`);
  }

  const analyses = await runAnalysis(batches);
  // 存在这里而不是等整条跑完：阶段二失败时它才有意义
  stageOne = { batches, analyses };
  setResummarize(true);

  checkCancel();
  return summarize();
}

/**
 * 只跑阶段二：把**全部**批次的分析合并成一张表。
 *
 * 「跳过阶段一」不等于「只处理一部分」—— stageOne 只在 runAnalysis 全部跑完
 * 之后才被赋值，中途取消或失败都会直接抛出，所以它要么是完整的要么不存在。
 */
async function summarize(): Promise<boolean> {
  if (!stageOne) throw new Error("没有可汇总的分析结果，先跑一次完整流程");
  showSummary(await runToExcel(joinAnalyses(stageOne.batches, stageOne.analyses)));

  const tail = LLM_IS_STUB ? "（dummy 内容，不是模型结果）" : "";
  say(`完成${tail}。点「复制为表格」，去 Excel 里 Ctrl+V。`);
  return true;
}

/**
 * 「开始」按钮兼作「停止」。
 *
 * 做成同一个按钮而不是禁用 + 另加一个：跑起来之后那个位置上唯一有意义的操作
 * 就是停下，多摆一个灰着的「开始」只是占地方。
 */
function setRunning(on: boolean): void {
  running = on;
  const button = el<HTMLButtonElement>("start");
  button.textContent = on ? "停止" : "开始";
  button.classList.toggle("danger", on);
  button.classList.toggle("primary", !on);
  button.disabled = false;

  // 这些跑起来之后没有意义：换字幕档或重读列表，结果都对不上现在选中的那个；
  // 「重新汇总」在跑的时候再点一次也只是白等
  el<HTMLButtonElement>("reload").disabled = on;
  el<HTMLSelectElement>("media").disabled = on;
  el<HTMLButtonElement>("resummarize").disabled = on;
}

function requestStop(): void {
  requestCancel();
  const button = el<HTMLButtonElement>("start");
  button.textContent = "停止中";
  button.disabled = true;
  say("正在停止：等当前这一批返回后停下。");
  logLine("用户请求停止");
}

/**
 * 跑一件长任务的外壳：锁控件、展开日志、收拾提醒条、分辨「停止」和「失败」。
 *
 * 完整流水线和「重新汇总」共用它 —— 这些收尾动作各写一遍的话，漏掉一处就会
 * 留一条过期的警告挂在界面上，或者按钮一直卡在「停止」。
 */
async function runGuarded(task: () => Promise<boolean>): Promise<void> {
  resetCancel();
  setRunning(true);
  hideSummary();
  // 自动展开日志：这一跑要好几分钟，状态栏只有一行，不展开的话用户看不到
  // 进行到第几批，容易以为卡住了。
  setLogVisible(true);
  notice(RUNNING_NOTICE, true);
  try {
    notice(await task() ? DONE_NOTICE : null);
  } catch (error) {
    const detail = (error as Error).message;
    notice(null);
    if (detail === CANCELLED) {
      say("已停止。");
      logLine("已停止");
    } else {
      // 分析结果还在的话说一声 —— 否则用户会以为整轮都白跑了，直接重来一遍，
      // 白花几分钟和一整轮额度。
      const kept = stageOne ? `（阶段一的 ${stageOne.analyses.length} 批分析都还在，点「重新汇总」直接出整场的表，不用重跑分析）` : "";
      say(`失败：${detail}${kept}`, true);
      logLine(`失败：${detail}`);
    }
  } finally {
    setRunning(false);
  }
}

async function start(): Promise<void> {
  if (running) {
    requestStop();
    return;
  }

  const media = selectedMedia();
  if (!media) {
    say("先选一份字幕档。", true);
    return;
  }
  // 没字幕的选项已经置灰，但键盘操作和列表刷新的时机差仍可能选中，所以再挡一次
  if (!media.documentAvailable) {
    say(`「${media.title}」还没有字幕文档，换一个。`, true);
    return;
  }

  // 新的一轮，上一轮存的分析作废
  stageOne = null;
  setResummarize(false);
  await runGuarded(() => runPipeline(media));
}

el("start").addEventListener("click", () => void start());
el("resummarize").addEventListener("click", () => {
  if (running) return;
  void runGuarded(summarize);
});
el("reload").addEventListener("click", () => void loadMedia());
el("log-toggle").addEventListener("click", toggleLog);
el("log-copy").addEventListener("click", copyLog);
el("copy").addEventListener("click", copyTable);
el("preview").addEventListener("click", openPreview);
el("overlay-close").addEventListener("click", closePreview);
// 点浮层外围也关。判 target === currentTarget 是为了只认背景本身，
// 点面板内部时事件冒泡上来会误关。
el("overlay").addEventListener("click", (event) => {
  if (event.target === event.currentTarget) closePreview();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closePreview();
});

// 主动问一次主题。宿主在页面加载后也会自己推一次 host.info，但两边有竞态，
// 先问一下更稳。
window.nonoka.post("host.getInfo", {});

void loadMedia();
