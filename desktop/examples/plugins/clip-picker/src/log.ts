// 进度日志。默认收起，点「日志」展开。
//
// 状态栏（say）只留一句话，说「现在怎么样」；日志留全过程，说「刚才发生了什么」。
// 切分和后面的逐批处理都是多步骤的，出问题时只有一行状态看不出卡在哪一步。

/** 留多少行。直播切出来的批次不多，但后面每批都会写几条，留够翻。 */
const LOG_LIMIT = 500;

const logLines: string[] = [];

function logLine(text: string): void {
  const stamp = new Date().toTimeString().slice(0, 8);
  logLines.push(`[${stamp}] ${text}`);
  if (logLines.length > LOG_LIMIT) logLines.splice(0, logLines.length - LOG_LIMIT);

  const box = el("log");
  box.textContent = logLines.join("\n");
  // 只有展开时滚动才有意义；收起状态下 scrollHeight 是 0，赋值无害
  box.scrollTop = box.scrollHeight;
}

function clearLog(): void {
  logLines.length = 0;
  el("log").textContent = "";
}

function setLogVisible(visible: boolean): void {
  el("log").hidden = !visible;
  // 「复制日志」跟着日志一起出现：日志收起时它没有可复制的上下文，
  // 摆在状态行上只会让人猜它是干什么的。
  el("log-copy").hidden = !visible;
  // 只切 class 不改 textContent —— 按钮里有个箭头 <span>，改文字会把它删掉
  el("log-toggle").classList.toggle("open", visible);
  if (visible) el("log").scrollTop = el("log").scrollHeight;
}

function toggleLog(): void {
  setLogVisible(el("log").hidden);
}

/**
 * 把日志复制走，方便用户贴给我排查。
 *
 * 前面补一行环境信息：出问题时最先要问的就是「哪个插件、什么时候、模型是谁」，
 * 而模型那条本来就在日志里，这里补上前两样。
 */
function copyLog(): void {
  if (logLines.length === 0) {
    say("日志是空的。", true);
    return;
  }
  const header = `选片小助手 · ${hostInfo?.pluginId ?? "dev.nonoka.clip-picker"}`
    + ` · ${new Date().toLocaleString()}`;
  void copyText([header, "", ...logLines].join("\n"), `已复制 ${logLines.length} 行日志。`);
}
