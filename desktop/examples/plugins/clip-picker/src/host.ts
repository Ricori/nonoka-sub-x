// 和宿主打交道的全部代码：RPC、主题、状态栏。

/** 取元素，取不到直接炸 —— 拼错 id 应该在第一次运行就暴露，而不是静默什么都不做。 */
function el<T extends HTMLElement = HTMLElement>(id: string): T {
  const node = document.getElementById(id);
  if (!node) throw new Error(`页面里没有 id="${id}" 的元素`);
  return node as T;
}

const pending = new Map<string, { resolve(value: unknown): void; reject(error: Error): void }>();
let sequence = 0;

/**
 * 调用一个宿主能力。宿主用 rpc.result 按 id 回来，所以每次调用都要带 id。
 * 泛型只是标注期望的形状，宿主返回什么不受这里约束。
 *
 * 超时不是为了网络抖动（这里没有网络），是为了「方法名打错」：宿主的
 * PluginPageHost 在 handler 表里找不到方法时**直接 return、什么都不回**，
 * 没有超时的话那就是一个永远 pending 的 Promise —— 页面无声无息地卡住，
 * 而且 pending 里那条也永远清不掉。压制、下载这类长任务要自己传大一点的值。
 */
function rpc<T>(method: string, params: Record<string, unknown> = {}, timeoutMs = 15_000): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    sequence += 1;
    const id = `r${sequence}`;
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error(`${method} 超过 ${Math.round(timeoutMs / 1000)} 秒没有响应`));
    }, timeoutMs);
    pending.set(id, {
      resolve: (value) => { clearTimeout(timer); resolve(value as T); },
      reject: (error) => { clearTimeout(timer); reject(error); },
    });
    window.nonoka.post(method, params, id);
  });
}

window.addEventListener("message", (event: MessageEvent<HostMessage>) => {
  const message = event.data;
  if (message?.source !== "nonoka-host") return;

  if (message.method === "host.info") {
    applyTheme(message.result as HostInfo);
    return;
  }
  if (message.method !== "rpc.result") return;

  const id = String(message.id);
  const waiter = pending.get(id);
  if (!waiter) return;   // 已超时或不是我们发的
  pending.delete(id);
  if (message.error) waiter.reject(new Error(String(message.error)));
  else waiter.resolve(message.result);
});

/** 最近一次 host.info。复制日志时用它标出是哪个插件。 */
let hostInfo: HostInfo | undefined;

/**
 * 插件页是独立文档，读不到宿主的 CSS 变量，主题得自己切。原生控件跟的是
 * color-scheme，不一起切的话浅色主题下 <select> 的下拉面板会是一块突兀的深色。
 */
function applyTheme(info: HostInfo | undefined): void {
  hostInfo = info ?? hostInfo;
  const light = info?.theme === "light";
  document.body.classList.toggle("light", light);
  document.documentElement.style.colorScheme = light ? "light" : "dark";
}

/**
 * 常驻提醒条。传 null 收起。
 *
 * 和 say() 的分工：say 是「现在进行到哪」，一直在变；notice 是「有件事你得知道」，
 * 会在那儿一直摆着。
 */
function notice(text: string | null, warn = false): void {
  const line = el("notice");
  line.hidden = text === null;
  if (text === null) return;
  line.textContent = text;
  line.classList.toggle("warn", warn);
}

function say(text: string, bad = false): void {
  const line = el("status");
  line.textContent = text;
  line.classList.toggle("bad", bad);
}

/**
 * 把文本送进剪贴板。
 *
 * 页面跑在 sandbox="allow-scripts" 的 iframe 里，源是不透明的，Clipboard API
 * 经常被直接拒绝；execCommand 虽然废弃但在用户点击里仍然可用。两条都不行时
 * 至少把文本放进那个文本框并全选，让用户自己按 Ctrl+C —— 这不算失败。
 */
async function copyText(text: string, done: string): Promise<void> {
  const area = el<HTMLTextAreaElement>("carrier");
  area.hidden = false;
  area.value = text;
  area.select();
  try {
    await navigator.clipboard.writeText(text);
    say(done);
    return;
  } catch { /* 落到下一条 */ }
  try {
    if (document.execCommand("copy")) {
      say(done);
      return;
    }
  } catch { /* 落到下一条 */ }
  say("这个环境不允许脚本写剪贴板。内容已经放进下面的文本框并全选，按 Ctrl+C 复制。", true);
}

/** 秒 -> hh:mm:ss */
function clock(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds || 0));
  const h = String(Math.floor(total / 3600)).padStart(2, "0");
  const m = String(Math.floor(total / 60) % 60).padStart(2, "0");
  const s = String(total % 60).padStart(2, "0");
  return `${h}:${m}:${s}`;
}
