// 大模型调用。走宿主的 llm.complete —— 插件页跑在 sandbox iframe 里，
// CSP 是 connect-src 'none'，自己发不出任何网络请求，密钥也归宿主管。
//
// 接口细节、role 怎么选、限额，见 docs/llm-engine.md。

/**
 * 改成 true 就回到 dummy：不联网，等 3 秒返回占位内容。
 *
 * 留着它是为了改界面时不烧用户的额度 —— 调表格渲染、日志排版这些跟模型
 * 无关的东西，跑一遍真实流水线要几分钟还花钱。
 */
const LLM_IS_STUB = false;

/**
 * 角色决定路由到哪一档模型，只有 lightweight 和 general_capable 两个。
 *
 * 两个阶段都用 general_capable：都要长文本的理解与重组 —— 阶段一从 800 行
 * 字幕产出结构化双模块，阶段二要信息无损地跨批次合并去重。lightweight 那档
 * 是给搜索循环里短输入短输出的判断题用的。
 */
const LLM_ROLE = "general_capable";

/**
 * 这两步都是「照规矩办事」，不需要创造性 —— 阶段一按固定的双模块格式输出，
 * 阶段二严格只回一个 JSON。sidecar 的默认 temperature 是 1.0（llm_worker.py），
 * 对这类任务太高，模型会跑偏格式。
 *
 * 这一条比模型档次更要紧，实测过：路由被钉在 gemini-3.5-flash-lite 时，
 * temperature 1.0 下阶段二连续失败（回散文不回 JSON），降到 0.01 之后同一个
 * 模型就能稳定输出合法 JSON。所以格式对不上时先查 temperature，别急着换模型。
 *
 * 为什么不是 0：宿主侧的校验是 `if request.Temperature != 0` 才转发，传 0
 * 会被当成「没给」，于是又落回默认的 1.0。0.01 是能真正送达的最小值。
 */
const LOW_TEMPERATURE = 0.01;

/** dummy 的假延迟。真调用是几十秒起步，这里短一点，够看清进度就行。 */
const STUB_DELAY_MS = 3_000;

/**
 * 每次调用最多试几遍。是**每个 call 各自**的额度，不是整条流水线共享。
 *
 * 5 次是权衡：模型偶尔不按格式输出，重试一两次通常就好了；但每次重试都是一次
 * 真实调用，会吃掉宿主 60 次/10 分钟的预算。3 批 + 1 汇总全部用满也才 20 次。
 */
const MAX_ATTEMPTS = 5;

/** 校验模型输出是否合格。不合格就抛错，callLLM 收到后重试。 */
type AnswerCheck = (text: string) => void;

/**
 * 调用大模型，输出不合格就重试。
 *
 * @param system 角色/规则，对应 assets 里那两份 prompt
 * @param user   本次要处理的内容，比如某一批的字幕正文
 * @param check  可选。判断这次输出能不能用；抛错即视为不合格
 * @returns 模型的原始文本输出
 *
 * 只对**输出不合格**重试，不对宿主的报错重试 —— 额度用尽、限流、「已有调用
 * 在飞」这些重试不可能成功，只会白烧配额，所以让它们直接往上抛。
 */
async function callLLM(system: string, user: string, check?: AnswerCheck): Promise<string> {
  let complaint = "";
  for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt += 1) {
    // 每次重试前都看一眼有没有请求停止。只在批与批之间检查是不够的：一批内部
    // 最多重试 5 次、每次几十秒，点了「停止」要等一两分钟才有反应。
    checkCancel();
    const text = LLM_IS_STUB
      ? await dummyAnswer(system, user)
      : await askOnce(system, user, complaint, attempt);
    if (!check) return text;
    try {
      check(text);
      return text;
    } catch (error) {
      complaint = (error as Error).message;
      logLine(`  ✗ 第 ${attempt}/${MAX_ATTEMPTS} 次输出不合格：${complaint}`);
      if (attempt === MAX_ATTEMPTS) {
        throw new Error(`重试 ${MAX_ATTEMPTS} 次仍然不合格：${complaint}`);
      }
    }
  }
  // 上面的循环要么 return 要么 throw，走不到这里；写出来只是让类型收敛。
  throw new Error("unreachable");
}

async function askOnce(system: string, user: string, complaint: string, attempt: number): Promise<string> {
  const messages = [
    { role: "system", content: system },
    { role: "user", content: user },
  ];
  // 重试时把上一次哪儿不对告诉它。空手重试等于碰运气，指出问题命中率高得多。
  if (complaint) {
    messages.push({
      role: "user",
      content: `你上一次的回答不合格：${complaint}\n请严格按 system 里的格式要求重新输出，不要有任何额外说明。`,
    });
  }

  // 限额是按**字节**算的（200 KB），中文一个字三字节，所以别用 .length 估。
  // 记进日志是为了撞限之前就能看出离上限还有多远 —— 阶段二的输入随批数
  // 线性增长，是先撞墙的那个。
  const bytes = new TextEncoder().encode(messages.map((m) => m.content).join("")).length;
  logLine(`callLLM  role=${LLM_ROLE}  ${Math.round(bytes / 1024)} KB / 200 KB`
    + (attempt > 1 ? `  （第 ${attempt} 次尝试）` : ""));

  // 超时给 3 分钟：rpc 默认的 15 秒是为了让「方法名打错、宿主静默不回」能报
  // 出来，模型调用几十秒起步，长的更久。
  let answer: LLMAnswer;
  try {
    answer = await rpc<LLMAnswer>("llm.complete", {
      role: LLM_ROLE,
      temperature: LOW_TEMPERATURE,
      maxTokens: 16_384,
      messages,
    }, 180_000);
  } catch (error) {
    throw new Error(explainHostError((error as Error).message));
  }

  // fallbackUsed 为真表示首选模型没能应答（额度、限流、报错），换了链上靠后
  // 的一个 —— 输出质量可能和平时不一样，出问题时这是关键线索。
  // 首行预览是排查用的：模型不按要求输出时（比如该给 JSON 却给了散文），
  // 只记字数看不出问题出在哪一步。
  logLine(`  ← ${answer.model}（${answer.backend}）`
    + `${answer.fallbackUsed ? " · 已降级到备用模型" : ""}`
    + `  ${answer.content.length} 字：${firstLine(answer.content)}`);
  return answer.content;
}

/**
 * 把宿主的英文报错翻成能照着做的话。
 *
 * 这两条都出自 internal/plugins/engine.go 的 claimLLMCall，是宿主自己的限流，
 * 不是供应商的话 —— 供应商的原话（额度用尽、CLI 没登录）要原样保留，那是插件
 * 分辨失败类型的唯一依据。所以这里只认这两条，其余照抄。
 */
function explainHostError(message: string): string {
  // 典型触发：上一轮跑到一半切走了页面。页面没了，但那次请求还在宿主里跑完，
  // 期间插件的「同时只允许一个」名额被占着。它会自己释放 —— provider 那层有
  // 330 秒的兜底超时，所以最坏五分半后一定能再开始。
  if (message.includes("already has an LLM call in flight")) {
    return "上一次的模型调用还没结束，多半是上次跑到一半切走了页面 —— 那次请求仍在后台跑完。"
      + "等一分钟再点「开始」（最长五分半后一定能开始）。";
  }
  if (message.includes("LLM calls in the last")) {
    return `模型调用太频繁，宿主限制 10 分钟内最多 ${LLM_CALLS_PER_WINDOW} 次。歇一会儿再试。`
      + `原文：${message}`;
  }
  return message;
}

/** 和宿主 engine.go 的 llmCallsPerWindow 对齐，只用于文案。 */
const LLM_CALLS_PER_WINDOW = 60;

/* ---------- 以下是 LLM_IS_STUB 时走的假实现 ---------- */

async function dummyAnswer(system: string, user: string): Promise<string> {
  logLine(`callLLM（dummy）system ${system.length} 字 / user ${user.length} 字，等 ${STUB_DELAY_MS / 1000} 秒`);
  await delay(STUB_DELAY_MS);

  // 要 JSON 的那一步（to_excel）得回 JSON，否则解析和出表这两段永远跑不到。
  // 判据就看 system prompt 有没有要求只输出 JSON。
  if (/只输出\s*JSON|纯\s*JSON/i.test(system)) return dummySheetJSON();

  return [
    "【这是 dummy callLLM 的占位输出，不是模型结果】",
    `收到 system ${system.length} 字、user ${user.length} 字。`,
    `system 首行：${firstLine(system)}`,
    `user 首行：${firstLine(user)}`,
  ].join("\n");
}

/** 形状照着 to_excel.md 的约定，内容是占位。 */
function dummySheetJSON(): string {
  const rows = [
    ["00:00:00", "00:12:30", "占位分类A", "dummy callLLM 的占位内容<br>• 第二个要点", "00:05:12 [占位] 【占位标题】<br>详情/理由：占位"],
    ["00:12:30", "00:41:05", "占位分类B", "dummy callLLM 的占位内容，接上真实模型后这里是本段的实际描述", ""],
    ["00:41:05", "01:03:44", "占位分类C", "dummy callLLM 的占位内容", "00:50:01 [占位] 【占位标题】"],
  ].map(([start, end, category, detail, highlight]) => ({
    start, end, category, detail, highlight, editor: "",
  }));
  return JSON.stringify({ sheet_date: "", rows }, null, 2);
}

function delay(ms: number): Promise<void> {
  return new Promise((done) => setTimeout(done, ms));
}

function firstLine(text: string): string {
  const line = text.split("\n").find((item) => item.trim().length > 0) ?? "";
  return line.length > 60 ? `${line.slice(0, 60)}…` : line;
}
