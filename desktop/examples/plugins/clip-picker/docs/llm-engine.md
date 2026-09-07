# 宿主的 LLM 能力（`llm.complete`）

这个插件调模型走的是宿主的 `llm.complete`，**不是自己发网络请求** —— 插件页跑在 sandbox iframe 里，CSP 是 `connect-src 'none'`，发不出任何请求。所以在页面里放 API key 输入框没有意义：填了也调不通，而且密钥本来就归宿主管。

对应代码：`desktop/internal/plugins/engine.go`，文档：`desktop/docs/PLUGINS.md` 的「引擎能力」一节。

## 调用形状

manifest 里声明权限：

```json
"permissions": ["media.list", "document.read", "llm.complete"]
```

页面里：

```ts
const answer = await rpc<LLMAnswer>("llm.complete", {
  role: "general_capable",
  maxTokens: 2048,        // 可选，0 或不给则用 sidecar 默认
  temperature: 0,         // 可选，0 不发送
  messages: [
    { role: "system", content: system },
    { role: "user", content: user },
  ],
}, 180_000);              // 超时要放大：rpc 默认 15 秒，模型几十秒起步
```

返回：

| 字段 | 说明 |
| --- | --- |
| `content` | 模型输出的文本 |
| `model` | 实际应答的模型 |
| `backend` | 走的哪条后端（API Key / 本机 CLI） |
| `fallbackUsed` | **没用上首选模型，降级了** |

`fallbackUsed` 值得写进日志。它为真意味着首选模型没能应答（额度、限流、报错），换了链上靠后的一个 —— 输出质量可能和平时不一样，排查问题时这条线索很值钱。

## `role` 只有两个

| role | 模型链（默认预设） |
| --- | --- |
| `lightweight` | `gemini-3.5-flash-lite` 免费 → 付费 |
| `general_capable` | `3.6-flash` → `3.5-flash` → `3.7-flash` → `3.8-flash`（免费）→ 付费 `3.7` → 付费 `3.8` |

链表是**降级顺序**，前一个失败就往后退。

FineSub 内部的用法（`third_party/finesub/src/finesub/llm/routing/config.py`）：`lightweight` 用于搜索循环里的判断题 —— 短输入短输出、量大、用 lite 不影响结果；`general_capable` 用于资料检索轮次和知识库更新，也就是需要真正理解和组织内容的活。

**本插件两个阶段都用 `general_capable`。** 两阶段都要长文本的理解与重组：阶段一从 800 行字幕里产出结构化双模块，阶段二要信息无损地跨批次合并去重。

### temperature 比模型档次更要紧（实测）

第一次接上真实调用时，阶段二连续失败 ——「模型没有返回 JSON 对象」。当时的判断是「路由被钉在 `flash-lite`，这档模型指令遵循太弱」。

**这个判断是错的。** 把 temperature 从 sidecar 默认的 1.0 降到 0.01 之后，**同一个 flash-lite 就能稳定输出合法 JSON**。真正的原因是采样温度，不是模型档次。

所以别急着换更贵的模型 —— 先确认 temperature。这里还有个容易踩的坑：宿主侧是 `if request.Temperature != 0` 才转发，**传 0 会被当成「没给」**，又落回 1.0。`0.01` 是能真正送达的最小值。

引擎另有 `audio_multimodal` 和 `lightweight_multimodal` 两个多模态角色，`llm.complete` **不放行**：这条接口没办法递媒体引用，开放了只会把纯文本路由到更贵的模型上。

## 插件管不了的事

- **没有 `model` 字段。** 用哪个模型是用户的配置。Go 侧注释：能指定模型的插件也就能绕开用户的选择。
- **拿不到密钥**，也拿不到 FineSub 自己的提示词模板（CC BY-SA，属引擎内部）。
- 走的是用户已配好的路由 —— API Key，或本机已登录的 Codex / Antigravity CLI。所以**供应商的报错原样带回来**（额度用尽、CLI 没登录、限流），因为插件没有别的办法分辨这几种失败。

## 限额

`engine.go:41-49`，宿主和 sidecar 双重校验。

| 限制 | 值 | 越限时的报错 |
| --- | --- | --- |
| 同时在飞 | 1（单插件） | `this plugin already has an LLM call in flight` |
| 10 分钟内 | 60 次 | `this plugin has made 60 LLM calls in the last 10 minutes; try again later` |
| prompt 总长 | 200 KB | `LLM messages exceed 200000 bytes` |
| messages 条数 | 64 | `LLM messages exceed 64 turns` |
| `maxTokens` | 1–32768 | `maxTokens must be between 1 and 32768` |
| `temperature` | 0–2 | `temperature must be between 0 and 2` |

还有几条形状校验：`role` 必须是那两个之一、`messages` 不能为空、每条的 role 只能是 `system` / `user` / `assistant`、**最后一条消息不能是空的**。

**越限直接报错，不排队。** 注释解释了原因 —— 页面显示成「卡住」而用户额度在背后流失，是更坏的结果。

## 这些限额对本插件意味着什么

**「同时只允许一个」不是问题。** `runAnalysis` 本来就是串行 for 循环，一批跑完再跑下一批。

**「60 次 / 10 分钟」够用。** 一场 4 小时直播约 5–7 批加 1 次汇总。极端情况：17000 句会切成 22 批，加汇总 23 次调用，仍在额度内。

**200 KB 那条要盯着，这是唯一的真风险。** 中文 UTF-8 是 3 字节/字：

| | 估算 |
| --- | --- |
| 阶段一每批 | 800 行 × 约 32 字 ≈ 25,600 字 ≈ **77 KB**，加 system 5 KB 和重叠行，约 85 KB |
| 阶段二 | 拼**全部**批次的分析结果。22 批 × 每批约 2000 字 ≈ 44,000 字 ≈ **132 KB** |

阶段一有余量。**阶段二会先撞墙**，因为它的输入随批数线性增长 —— 超长直播（4 小时以上、字幕密集）可能顶到 200 KB。

真撞上了，对策是把汇总也分组：每 8 批先汇总一次，再汇总这些汇总。那要改 `to_excel.ts` 的结构，`analysis.ts` 的 `joinAnalyses()` 也要能按组切。现在没做，因为还没遇到。

## 从 dummy 切到真实调用

`src/llm.ts` 里 `callLLM` 是唯一的接缝，上层的批次循环、进度、日志、错误处理一行都不用改。

1. 改函数体走 `rpc("llm.complete", …)`
2. `LLM_IS_STUB` 改成 `false`（界面上的 dummy 提示会跟着消失）
3. manifest 的 `permissions` 加 `llm.complete`
4. 重新打包安装（**权限变了必须重装**，联接只同步页面文件，不同步 manifest）

建议同时把 `model` / `backend` / `fallbackUsed` 记进日志 —— 出问题时你需要知道是哪个模型答的、有没有降级。
