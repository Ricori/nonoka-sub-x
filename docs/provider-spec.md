# Nonoka Sub X Execution Provider 接口规范 (v1)

本文档是 Nonoka Sub X **执行提供方（Execution Provider）** 的标准接口规范。任何第三方只要完整实现本规范，即可作为独立的转写执行后端接入 Nonoka Sub X 桌面端，复用其媒体库、任务视图、Projector 投影层与 JASSUB 字幕编辑器。

内置的 Local Provider（本地 Python sidecar）与 Cloud Provider（Nonoka 云端）是本规范的两个参考实现，二者与自建 Provider 处于完全同等的地位，前端不对任何 Provider 做特判。

- 相关文档：[系统架构与契约规范](architecture.md)、[FineSub 引擎同步与构建规范](engine.md)、[Modal 云端后端](modal-backend.md)
- 规范版本：`adapter_schema = 1`，`artifact_schema = 1`

---

## 1. 职责边界

Provider 只负责**「把一个媒体源变成一组字幕产物」**，其余能力一律由宿主提供，Provider 不得越界实现。

| 属于 Provider 的职责 | **不**属于 Provider 的职责 |
| :--- | :--- |
| 声明自身能力与运行时就绪状态 | 媒体库管理、缩略图、视频缓存 |
| 接受 TaskRequest、创建并调度任务 | EditDocument 生成（由 Projector 统一完成） |
| 上报状态快照、增量事件与进度 | 字幕编辑、版本历史、乐观锁存储 |
| 取消 / 重试 / 断点续跑 | SRT / ASS / 压制视频导出 |
| 产出符合产物契约的 ArtifactManifest | 前端 UI 渲染与文案 |

**核心原则**：前端只根据 `capabilities()` 返回的 `features` 与 `runtime` 动态渲染，**严禁**按 `provider` 字段硬编码分支。因此 Provider 必须如实声明能力——不支持的能力必须声明为 `false` 并在收到相应请求时显式报错，**禁止静默降级**。

---

## 2. 两种接入形态

### 2.1 形态 A：进程内 Provider
直接在前端实现 `ExecutionProvider` TypeScript 接口。适用于：调用远端 HTTP 服务、封装已有 API 的场景。

### 2.2 形态 B：Sidecar Provider
实现第 [9](#9-sidecar-provider-http-路由规范) 节定义的 HTTP 路由，由宿主（Go 后端）监管进程生命周期并转发调用，前端侧再包一层薄适配器。适用于：需要本地重型运行时（Python / GPU / 模型）的场景。

两种形态对外暴露的**数据契约完全一致**，本规范第 3–8 节对两者同时生效。

---

## 3. ExecutionProvider 接口定义

```ts
interface ExecutionProvider {
  /** 声明能力、引擎身份与运行时就绪状态。前端在每次 start 前调用。 */
  capabilities(): Promise<Capabilities>;

  /** 返回该 Provider 名下的历史任务列表，用于任务中心与断点恢复。 */
  listTasks(): Promise<TaskListItem[]>;

  /** 创建并调度任务。必须校验请求，失败即抛错，不得创建半成品任务。 */
  start(request: TaskRequest): Promise<TaskSnapshot>;

  /** 查询任务当前快照。必须幂等、可高频轮询。 */
  status(taskId: string): Promise<TaskSnapshot>;

  /** 拉取 cursor 大于 afterCursor 的增量事件。 */
  events(taskId: string, afterCursor: number): Promise<TaskEventPage>;

  /** 取消任务，保留已完成阶段的断点。 */
  cancel(taskId: string): Promise<TaskSnapshot>;

  /** 重试 failed / cancelled 的任务。 */
  retry(taskId: string): Promise<TaskSnapshot>;

  /** 续跑 interrupted 的任务。 */
  resume(taskId: string): Promise<TaskSnapshot>;

  /** 返回已完成任务的产物清单。 */
  artifacts(taskId: string): Promise<ArtifactManifest>;
}
```

**通用约束**

1. 所有方法均为异步，失败以抛出异常（Sidecar 形态为非 2xx + 错误体）表达，**禁止**返回 `null` 或空对象表示失败。
2. 所有返回体必须携带 `schema: 1`，宿主会在适配层强校验，字段不符直接判定为不兼容 Provider。
3. 所有返回体中的 `provider` 字段必须等于该 Provider 自身的 ID。
4. `status` / `events` / `artifacts` 必须对**已终结**的任务持续可读（至少直到宿主主动清理），否则断点恢复与产物投影会失败。

---

## 4. 数据模型

### 4.1 Capabilities

```jsonc
{
  "provider": "my-provider",       // 必填，Provider 唯一 ID
  "adapter_schema": 1,             // 必填，固定为 1
  "artifact_schema": 1,            // 必填，固定为 1
  "engine": {
    "name": "finesub",             // 可选，引擎名
    "version": "0.5.1",            // 必填，引擎版本
    "commit": "b9b2f10c80abd58ddff739b60461624f51c0789c", // 必填，引擎 commit
    "bundle_id": "finesub-0.5.1+b9b2f10c80ab"             // 可选，引擎包标识
  },
  "features": {                    // 必填，六个布尔位缺一不可
    "raw_srt": true,               // 支持 target=raw-srt
    "translation": true,           // 支持 target=final-srt（纠错 + 翻译）
    "video_multimodal": true,      // 支持 correction.media=video
    "knowledge": true,             // 支持 knowledge=collect/update
    "resume": true,                // 支持 resume()
    "diarization": false           // 说话人分离（v1 保留位）
  },
  "devices": [                     // 必填，可为空数组
    { "id": "cuda:0", "name": "NVIDIA GeForce RTX 4070", "memory_mb": 12288 }
  ],
  "runtime": {                     // 强烈建议提供：前端据此拦截不可用的启动
    "ready": true,
    "issues": [],                  // [{ code, message }]，ready=false 时不得为空
    "stages": [                    // 分阶段就绪，用于精确到目标的拦截
      { "id": "media",            "label": "媒体探测与音频准备",     "ready": true, "issues": [] },
      { "id": "raw-srt",          "label": "VAD、ASR 与原始字幕",     "ready": true, "issues": [] },
      { "id": "final-srt",        "label": "LLM 纠错、翻译与最终字幕", "ready": true, "issues": [] },
      { "id": "knowledge",        "label": "知识库收集与更新",        "ready": true, "issues": [] },
      { "id": "video-multimodal", "label": "视频多模态纠错",          "ready": true, "issues": [] }
    ],
    "localAgent": "..."            // 可选，本地代理 / 运行时描述
  },
  "settings": { "llm_key_configured": true }  // 可选
}
```

> **`runtime.ready` 的语义**：宿主在 `start()` 之前会读取 `capabilities()`，`runtime.ready === false` 时直接以 `issues[0].message` 报错并阻止提交。Provider 若不提供 `runtime`，前端将视为未就绪。

### 4.2 TaskRequest

```jsonc
{
  "schema": 1,
  "provider": "my-provider",
  "source": { "kind": "local_file", "path": "/abs/video.mp4", "title": "video",
              "fingerprint": "...", "video_id": "abc123", "duration": 3600 },
  "target": "final-srt",
  "language": "ja",
  "device": "cuda:0",
  "gpu_tier": "auto",
  "separate": true,
  "vocal_profile": "quality",
  "correction": {
    "enabled": true,
    "media": "audio",
    "retrieval": "local",
    "difficulty": "quality",
    "fast": "auto",
    "extra_info": "",
    "extra_style": ""
  },
  "knowledge": "update",
  "llm_model": { "correction": "local-agy-media-gemini-3_7-flash" },
  "cleanup_intermediate": false
}
```

| 字段 | 类型 / 枚举 | 必填 | 说明 |
| :--- | :--- | :---: | :--- |
| `schema` | `1` | ✅ | 不为 1 时必须以 `unsupported_schema` 拒绝 |
| `provider` | Provider ID | ✅ | 与自身不符时以 `unsupported_provider` 拒绝 |
| `source` | `Source` | ✅ | 见 4.2.1 |
| `target` | `raw-srt` \| `final-srt` | ✅ | 其他值以 `unsupported_target` 拒绝 |
| `language` | 语言码 | — | 默认 `ja` |
| `device` | `cpu` \| `cuda` \| `cuda:<n>` | — | 默认 `cuda`；应校验并在不支持时报错 |
| `gpu_tier` | `auto` \| `cpu` \| `entry` \| `standard` \| `standard_large_vram` \| `high` | — | 默认 `auto`（由引擎探测本机显卡）。取代了 0.4.x 的 `gpu_budget_gb`；仍带旧字段的请求由 worker 按 `4→entry、8→standard、12/16→standard_large_vram` 转换一次并在任务日志里说明。`auto` 最高只选到 `standard_large_vram`，`high` 需要显卡自报 24GB 以上 |
| `separate` | `boolean` | — | 默认 `true`。仅在输入本身已是纯人声（已分离的人声轨、录音棚干声）时可设为 `false`：产物位置与格式不变（`<stem>-vocal.ogg`，16 kHz 单声道），只是由源音直接转出。⚠ 该分离而没分离不会报任何错，只是识别质量整体下降 |
| `vocal_profile` | `cost` \| `quality` | — | 人声分离档位；不支持的档位必须显式报错 |
| `correction.enabled` | `boolean` | — | `false` 时等价于跳过 LLM 环节 |
| `correction.media` | `text` \| `audio` \| `video` | — | `video` 需 `features.video_multimodal=true` |
| `correction.retrieval` | `none` \| `local` \| `native` | — | 知识检索来源 |
| `correction.difficulty` | `efficiency` \| `intermediate` \| `quality` | — | LLM 质量档 |
| `correction.fast` | `auto` \| `on` \| `off` | — | 快速通道开关 |
| `correction.extra_info` / `extra_style` | `string` | — | 用户补充的背景信息与风格要求 |
| `knowledge` | `none` \| `collect` \| `update` | — | 需 `features.knowledge=true` |
| `llm_model` | `string` \| `{任务组: string}` | — | 本次运行的模型覆盖，等价于引擎的 `--llm-model`。字符串钉住所有任务组，对象只钉它点名的那些。值是模型组或路由 target；名字由引擎的路由加载器校验。⚠ 钉住即**替换整条链**——钉住的模型做不了的调用会直接失败，后面不再挂任何兜底 |
| `cleanup_intermediate` | `boolean` | — | 完成后是否清理中间产物 |
| `axis` | `Axis` | — | 用户导入的已有产物；见 4.2.2。只在 `kind="ja"` 时出现，其余轴型不进任务请求 |

#### 4.2.2 Axis（导入已有产物）

用户可以带着自己打好的轴开工。四种产物走三条路，只有一种会进 `TaskRequest`：

| 轴型 | 含义 | 走法 |
| :--- | :--- | :--- |
| `empty` | 只有时间，没有文字 | 识别照常跑；宿主在投影阶段把结果重排到这条轴上，Provider 不必知道它的存在 |
| `ja` | 有原文、缺译文 | 以 `axis` 进入 `TaskRequest`：Provider 跳过整条识别链，只跑 LLM 翻译。宿主自带的两个 Provider 都支持——云端连音轨都不上传。不支持时必须以 `invalid_axis` 显式拒绝，**禁止**忽略该字段后照常识别 |
| `zh` / `bi` | 译文齐了 | 不启动任何任务，宿主直接落成 EditDocument |

```ts
interface Axis {
  kind: "empty" | "ja" | "zh" | "bi";
  filename: string;
  rows: Array<{
    t0: number;    // 秒，t1 > t0 ≥ 0
    t1: number;
    ja: string;    // 原文，空串表示这一行没有
    zh: string;    // 译文
    spk: string;   // 说话人（取自 ASS 的 Style 名）；"" 表示这份轴没标
  }>;
}
```

收到 `kind` 不是 `ja` 的 `axis`、或 `kind="ja"` 却搭配 `target="raw-srt"` 时，一律以 `invalid_axis` 拒绝——两者都意味着宿主与 Provider 对这条轴的理解不一致，静默跑完的代价是白花一次识别。

#### 4.2.1 Source 变体

Provider 必须**只**接受自己声明支持的 `source.kind`，收到其他 kind 时以 `invalid_source` 拒绝。

```ts
// 本地文件：路径必须是绝对路径且真实存在，Provider 必须自行校验
interface LocalSource {
  kind: "local_file";
  path: string;          // 绝对路径
  title: string;
  fingerprint?: string;  // 媒体指纹，用于库内合并
  video_id?: string;     // 文档 ID；仅允许 [A-Za-z0-9_-]，长度 ≤ 80
  duration?: number;     // 秒
}

// 已上传音频：远端 Provider 使用，不传递任何本地绝对路径
interface UploadedAudioSource {
  kind: "uploaded_audio";
  object_id: string;     // 上传对象标识
  title: string;
  fingerprint?: string;
  duration?: number;
}
```

自定义 Provider 可新增 `kind`，但必须在自身文档中声明，并保证宿主构造请求时可提供所需字段。

### 4.3 TaskSnapshot 与状态机

```jsonc
{
  "schema": 1,
  "task_id": "0f3c9a...",          // Provider 内唯一，建议使用 hex / URL-safe 字符集
  "provider": "my-provider",
  "state": "running",
  "stage": "aligned",              // 见第 8 节阶段词汇表；未进入任何阶段时为空串
  "progress": {                    // 无进度信息时为 null
    "completed": 42,
    "total": 100,                  // 未知总量时为 null
    "unit": "segments",
    "message": "正在识别"
  },
  "engine": { "version": "0.5.1", "commit": "b9b2f10c..." },
  "requested_capabilities": { "target": "final-srt", "video_multimodal": false },
  "effective_capabilities": { "target": "final-srt", "video_multimodal": false },
  "error": null,                   // 或 { "code": "...", "message": "..." }
  "last_cursor": 128,              // 当前已产生的最大事件 cursor
  "created_at": "2026-08-28T00:00:00Z",  // RFC 3339 UTC，以 Z 结尾
  "updated_at": "2026-08-28T00:01:00Z"
}
```

**状态枚举与合法转换**

```text
                                      ┌──► completed  (终态)
                                      │
  start() ──► queued ──► running ─────┼──► failed      ──retry()──┐
                 ▲                    │                            │
                 │                    ├──► cancelled   ──retry()──┤
                 │                    │                            │
                 │                    └──► interrupted ─resume()──┤
                 └────────────────────────────────────────────────┘
  cancel() 可作用于 queued / running，对终态任务幂等返回当前快照
```

| 状态 | 含义 | 终态 | 允许的操作 |
| :--- | :--- | :---: | :--- |
| `queued` | 已受理，等待调度 | 否 | `cancel` |
| `running` | 正在执行 | 否 | `cancel` |
| `completed` | 成功完成，产物可读 | 是 | — |
| `failed` | 执行失败，`error` 必须非空 | 是 | `retry` |
| `cancelled` | 用户取消 | 是 | `retry` |
| `interrupted` | 非用户意图的中断（进程退出、容器抢占） | 否\* | `resume` |

\* `interrupted` 不是终态：它表示「可续跑」。Provider 在进程 / 容器重启后必须能识别遗留的 `running` 任务并将其标记为 `interrupted`。

**硬性要求**

- `cancel()` 对已处于终态的任务必须**幂等返回当前快照**，不得报错。
- `retry()` 仅接受 `failed` / `cancelled`，`resume()` 仅接受 `interrupted`；状态不符必须返回 `invalid_state`（HTTP 409）。
- `retry` / `resume` 均应复用已有断点，**不得从零重跑**（若引擎本身无检查点，须在自身文档中声明）。
- 任务进入 `completed` 时，`artifacts()` 必须已经可用；宿主观察到 `completed` 后会立即拉取产物。

### 4.4 TaskEvent 与 TaskEventPage

```jsonc
// events(taskId, afterCursor) 的返回
{
  "schema": 1,
  "task_id": "0f3c9a...",
  "events": [
    {
      "cursor": 17,
      "task_id": "0f3c9a...",
      "type": "progress",
      "timestamp": "2026-08-28T00:00:30Z",
      "payload": { "stage": "aligned", "completed": 42, "total": 100,
                   "unit": "segments", "message": "正在识别" }
    }
  ],
  "next_cursor": 17,
  "state": "running"
}
```

**事件类型与 payload 约定**

| `type` | payload 关键字段 | 语义 |
| :--- | :--- | :--- |
| `started` | — | 任务开始执行 |
| `stage` | `stage`, `message`, `reused?` | 进入新阶段；`reused=true` 表示命中断点被跳过 |
| `progress` | `stage`, `completed`, `total`, `unit`, `message` | 阶段内进度；`total` 未知时为 `null` |
| `warning` | `code`, `message`, `impact?`, `action?` | 非致命告警 |
| `log` | `message`, `fields?` | 诊断日志 |
| `completed` | `artifacts`（ArtifactManifest） | 成功结束 |
| `failed` | `message`, `stage?` | 失败结束 |
| `cancelled` | — | 已取消 |

**cursor 规则（必须严格遵守）**

1. `cursor` 在单个任务内**从 1 开始严格单调递增**，不得重复、不得回退、不得跨任务复用。
2. `events(taskId, after)` 返回所有 `cursor > after` 的事件，按 cursor 升序排列。
3. `next_cursor` = 本页最后一个事件的 cursor；本页为空时**原样返回入参 `after`**。
4. 单页建议上限 500 条；截断时前端会以 `next_cursor` 继续拉取。
5. 事件必须持久化：宿主在断线、重启、切换页面后都会以 `after=0` 或末次 cursor 重新补读完整日志。
6. `stage` / `progress` 事件必须同步更新 `TaskSnapshot.stage` 与 `progress`——前端进度条读快照，日志面板读事件流，两者不一致会直接体现为 UI 抖动。

### 4.5 ArtifactManifest

```jsonc
{
  "schema": 1,
  "task_id": "0f3c9a...",
  "engine_commit": "b9b2f10c80abd58ddff739b60461624f51c0789c",
  "artifacts": {
    "stable_json":   { "uri": "file:///.../video-stable.json",   "sha256": "...", "bytes": 12345 },
    "raw_srt":       { "uri": "file:///.../video-raw.srt",       "sha256": "...", "bytes": 8192  },
    "annotated_csv": { "uri": "file:///.../video-annotated.csv", "sha256": "...", "bytes": 23456 },
    "final_srt":     { "uri": "file:///.../video.srt",           "sha256": "...", "bytes": 10240 }
  }
}
```

- 四个键均为**可选**，但组合受第 7 节约束；`stable_json` 事实上必需。
- `uri` 为 `file://` 时必须是本机绝对路径且**位于宿主任务目录之内**（宿主会做白名单校验，越界一律拒绝投影）。
- `sha256` 与 `bytes` 用于完整性校验与云端同步限额统计，必须真实。
- `engine_commit` 用于产物与引擎版本的可追溯绑定。

### 4.6 TaskListItem

```jsonc
// listTasks() 的 Sidecar 返回体：{ "schema": 1, "tasks": [ ... ] }
{
  "snapshot": { /* TaskSnapshot */ },
  "media_id": "abc123",     // 对应 source.video_id，可为空串
  "title": "video"          // 展示名，可回退为路径或 task_id
}
```

---

## 5. 错误模型

**Sidecar 形态**统一返回：

```json
{ "error": { "code": "invalid_source", "message": "Local source path must be absolute" } }
```

**进程内形态**抛出携带同样 `code` / `message` 的异常。

### 5.1 标准错误码

| `code` | HTTP | 触发时机 |
| :--- | :---: | :--- |
| `invalid_json` | 400 | 请求体不是 JSON 对象 |
| `invalid_request` | 400 | 字段类型或取值非法 |
| `unsupported_schema` | 400 | `schema != 1` |
| `unsupported_provider` | 400 | `provider` 与自身不符 |
| `unsupported_target` | 400 | `target` 不在枚举内 |
| `invalid_source` | 400 | `source.kind` / 路径 / `video_id` 非法 |
| `source_not_found` | 400 | 源文件不存在 |
| `invalid_axis` | 400 | 导入的轴不合法，或它的轴型不该启动任务（见 4.2.2） |
| `invalid_session` | 401 | Sidecar 会话令牌校验失败 |
| `not_found` | 404 | 路由不存在 |
| `task_not_found` | 404 | 未知 `task_id` |
| `invalid_state` | 409 | `retry` / `resume` 的状态前置不满足 |
| `artifacts_not_ready` | 409 | 任务尚未产出产物清单 |
| `missing_engine` / `invalid_engine` | 503 | 引擎快照缺失或损坏 |
| *运行时就绪 issue code* | 503 | `start()` 时目标阶段未就绪，直接返回该 issue |
| `internal_error` | 500 | 未归类的内部异常 |

### 5.2 任务失败原因码（写入 `TaskSnapshot.error.code`）

面向用户的可操作分类，Provider 应尽量归类而非一律 `engine_failed`：

`missing_llm_key`（未配置可用的模型提供商）、`missing_gpu`（CUDA / 驱动不可用）、`missing_model`（模型权重缺失）、`insufficient_disk`（磁盘空间不足）、`missing_dependency`（依赖缺失）、`engine_failed`（兜底）。

---

## 6. 能力协商与拒绝原则

| 场景 | 正确做法 | 禁止行为 |
| :--- | :--- | :--- |
| 不支持视频多模态 | `features.video_multimodal=false`，收到 `correction.media=video` 时返回 400 | 静默改用 `media=text` 执行 |
| 不支持某个 `vocal_profile` | 收到不支持的档位返回 `invalid_request` | 静默按默认档位执行 |
| 不支持知识库 | `features.knowledge=false`，收到 `knowledge=collect/update` 时报错 | 忽略该字段 |
| 运行时未就绪 | `runtime.ready=false` + 明确 `issues`，`start()` 返回 503 | 先建任务再失败 |

`requested_capabilities` 与 `effective_capabilities` 必须如实记录「用户请求了什么」与「实际执行了什么」；若二者不一致，Provider 有义务在 `warning` 事件中说明原因。

---

## 7. 产物契约（Projector 输入要求）

宿主的 **Artifact Projector** 会把产物投影成 EditDocument。Projector 的对齐规则是**零猜测**的：任何不一致都会判定失败并保留原始产物，因此产物格式必须严格达标。

### 7.1 组合规则

| 提供的产物 | 投影模式 | 结果 |
| :--- | :--- | :--- |
| 仅 `stable_json` | `stable` | 仅原文轨，`zh` 为空 |
| `stable_json` + `annotated_csv` + `final_srt` | `final` | 原文 + 译文 + 词级时间 + 低置信度标记 |
| `annotated_csv` 与 `final_srt` 只给其一 | — | **投影失败**，二者必须成对出现 |

### 7.2 `stable_json`

```jsonc
{
  "segments": [
    {
      "id": "1",                 // 段落唯一 ID，同一文件内不得重复
      "start": 12.34,            // 秒；必须 end > start
      "end": 15.67,
      "text": "原文文本",
      "words": [ /* 词级时间戳，原样透传给编辑器 */ ],
      "low_conf": false          // 亦接受 low_confidence / confidence_label="low"
    }
  ]
}
```
顶层也可直接是 segments 数组。列表**不得为空**，任一段落时长非正即判定失败。

### 7.3 `annotated_csv`

以 `|` 分隔的 **9 列**文本，`#` 开头为注释行：

```text
kind|position|duration|gap|corrected|translation|conf|chars|note
```

| 列 | 说明 |
| :--- | :--- |
| `kind` | `sub`（默认）或 `insert`（新增句，无源段） |
| `position` | 逗号分隔的 stable `id` 列表；`kind != insert` 时不得为空，且必须能在 `stable_json` 中找到 |
| `duration` | 必须可解析为浮点数 |
| `corrected` | 纠错后的原文，`\n` 表示换行 |
| `translation` | 译文 |
| `conf` | `high` / `median` / `low`（兼容 1–9 整数：≥7 → high，≥4 → median，其余 low） |
| `gap` / `chars` / `note` | 保留列，Projector 不使用但必须占位 |

### 7.4 `final_srt`

标准 SRT。每条 cue 必须有文本且 `end > start`。

> **对齐断言（不可协商）**：`annotated_csv` 的有效行数与 `final_srt` 的 cue 数**必须完全相等**。不相等时 Projector 直接失败，不会截断或模糊匹配。这是保证字幕与时间轴不错位的最后一道防线。

---

## 8. 阶段词汇表

`TaskSnapshot.stage` 与 `stage` 事件应尽量使用下列标准阶段 ID——前端据此显示中文阶段名并计算进度条区间。未识别的阶段不会报错，但会退化为通用文案与默认进度带。

| `stage` | 显示名 | 典型含义 |
| :--- | :--- | :--- |
| `queued` | 等待调度 | 排队中 |
| `starting` | 准备运行环境 | 拉起进程 / 容器 |
| `gpu-queued` | 等待语音处理 | 等待 GPU 资源 |
| `pipeline` | 准备处理 | 流水线初始化 |
| `vocal` | 处理音频 | 人声分离 |
| `aligned` | 语音识别 | ASR / 对齐 |
| `postprocess-queued` | 等待字幕处理 | 等待 CPU 后处理资源 |
| `stable` | 整理识别结果 | 稳定化，产出 `stable_json` |
| `raw-srt` | 生成字幕 | 产出 `raw_srt` |
| `llm-queued` | 等待纠错与翻译 | 等待 LLM 资源 |
| `translated-srt` | 纠错与翻译 | 产出 `annotated_csv` |
| `final-srt` | 整理最终字幕 | 产出 `final_srt` |
| `completed` / `failed` / `cancelled` / `interrupted` | 对应状态文案 | 终结阶段 |

---

## 9. Sidecar Provider HTTP 路由规范

形态 B 必须实现下列路由。宿主启动 Sidecar 进程后，从其 **stdout 首行**读取握手信息：

```json
{"schema":1,"host":"127.0.0.1","port":51234,"token":"<url-safe-random>"}
```

- **必须**只绑定 `127.0.0.1`，端口由系统分配（bind 到 `0`）。
- **必须**每次启动生成一个新的高熵会话令牌（≥ 256 bit），所有请求校验 `Authorization: Bearer <token>`，校验失败返回 401 `invalid_session`。令牌比较必须使用常量时间比较。
- 收到 `SIGTERM` 时应优雅停机：将仍在运行的任务置为 `interrupted` 并追加一条 `warning` 事件，而不是留下 `running` 僵尸快照。

| 方法 | 路径 | 对应接口 | 成功状态码 |
| :--- | :--- | :--- | :---: |
| `GET` | `/v1/capabilities` | `capabilities()` | 200 |
| `GET` | `/v1/tasks?limit={n}` | `listTasks()` | 200 |
| `POST` | `/v1/tasks` | `start(request)` | 202 |
| `GET` | `/v1/tasks/{id}` | `status(id)` | 200 |
| `GET` | `/v1/tasks/{id}/events?after={cursor}` | `events(id, cursor)` | 200 |
| `GET` | `/v1/tasks/{id}/artifacts` | `artifacts(id)` | 200 |
| `POST` | `/v1/tasks/{id}/cancel` | `cancel(id)` | 200 |
| `POST` | `/v1/tasks/{id}/retry` | `retry(id)` | 200 |
| `POST` | `/v1/tasks/{id}/resume` | `resume(id)` | 200 |

**可选路由**（提供本地运行时管理或用户 Key 配置能力的 Provider 需要实现，否则前端相应面板不可用）：

| 方法 | 路径 | 说明 |
| :--- | :--- | :--- |
| `GET` / `POST` / `DELETE` | `/v1/runtime/provision` | 查询 / 安装 / 卸载运行时 |
| `POST` | `/v1/runtime/provision/cancel` | 取消安装 |
| `GET` | `/v1/settings` | 读取配置快照（**不得**回传密钥明文） |
| `PUT` | `/v1/settings/keys` | 写入用户自持 API Key |

### 9.1 隔离 Worker 的 NDJSON 约定（可选实现范式）

参考实现把真实计算放进独立子进程，父进程只做状态机与持久化。子进程协议：**stdin 一行 TaskRequest JSON，stdout 每行一条事件**。

```jsonc
{"type":"stage","payload":{"stage":"vocal","message":"正在处理"}}
{"type":"progress","payload":{"stage":"aligned","completed":42,"total":100,"unit":"segments","message":"正在识别"}}
{"type":"completed","payload":{"artifacts":{ /* ArtifactManifest */ }}}
```

父进程负责：为事件分配 `cursor`、追加写入 `events.jsonl`、更新 `snapshot.json`、把无法解析的行降级为 `log` 事件（native 库常在 Windows ANSI 代码页输出非 UTF-8 字节，**不得**因解码失败让任务崩溃）。

---

## 10. 安全与资源要求

1. **本地绑定**：Sidecar 只监听 loopback + 随机端口 + 随机会话令牌，禁止监听 `0.0.0.0`。
2. **密钥保护**：用户自持的 LLM API Key 只落在本地独立存储，**严禁**写入事件流、日志、快照或回传前端持久化。`/v1/settings` 只返回「是否已配置」的布尔位。
3. **路径白名单**：`file://` 产物必须落在任务目录内；宿主会拒绝目录穿越与非本机 URI。
4. **远端 Provider 的数据最小化**：如需上传，应只上传完成任务所必需的数据，并在自身文档中明确说明上传内容、留存时长与删除方式。
5. **输入校验**：`task_id`、`video_id` 等参与路径拼接的标识必须限定字符集（如 hex 或 `[A-Za-z0-9_-]`，长度 ≤ 80）后再落盘。
6. **原子写入**：快照与产物清单应先写临时文件再原子重命名，避免宿主读到半截 JSON。
7. **进程监管**：Provider 退出时必须终止自己派生的全部子进程 / 子调用，取消任务时应级联终止对应的计算容器。

---

## 11. 接入步骤

### 11.1 形态 A（进程内）

1. 在 [`desktop/frontend/src/providers/types.ts`](../desktop/frontend/src/providers/types.ts) 的 `ProviderID` 联合类型中加入新的 Provider ID。
   > v1 的 `ProviderID` 是封闭联合（当前为 `"local" | "cloud"`），新增 Provider 需要改动此处，这是有意为之的编译期约束。
2. 新建 `desktop/frontend/src/providers/myProvider.ts`，实现 `ExecutionProvider`，并在每个返回体上做 `schema` / `provider` 强校验（参照 [`localProvider.ts`](../desktop/frontend/src/providers/localProvider.ts)）。
3. 在 [`desktop/frontend/src/app/App.tsx`](../desktop/frontend/src/app/App.tsx) 中实例化，并为其创建一个 `PipelineController`。
4. 若需要共享注册表，用 `ProviderRegistry.register(id, provider)` 注册后再 `get(id)` 取用。
5. 在 [`defaultRequest.ts`](../desktop/frontend/src/home/defaultRequest.ts) 中补一个该 Provider 的默认 `TaskRequest` 构造函数。

### 11.2 形态 B（Sidecar）

1. 实现第 9 节的 HTTP 路由与 stdout 握手协议（参照 [`src/nonoka_x/sidecar.py`](../src/nonoka_x/sidecar.py) 与 [`src/nonoka_x/local_provider.py`](../src/nonoka_x/local_provider.py)）。
2. 在 Go 后端新增一个 service，负责进程生命周期、令牌注入与 JSON 转发（参照 [`desktop/internal/provider/service.go`](../desktop/internal/provider/service.go)）。
3. 通过 Wails bindings 暴露方法，再在前端包一层形态 A 的薄适配器。

---

## 12. 一致性检查清单

实现完成后，逐条自检：

**契约层**
- [ ] 所有返回体带 `schema: 1`，`provider` 字段与自身 ID 一致
- [ ] `capabilities()` 六个 `features` 位齐全，`runtime.ready=false` 时 `issues` 非空
- [ ] 声明为 `false` 的能力，在收到对应请求时显式报错而非静默降级
- [ ] `TaskRequest` 全字段校验：`schema` / `provider` / `source.kind` / `target` / `device` / 各枚举值

**状态机**
- [ ] 六种 `state` 全部可达且语义正确；`failed` 必带 `error`
- [ ] `cancel()` 对终态任务幂等返回快照，不报错
- [ ] `retry()` 只接受 `failed` / `cancelled`，`resume()` 只接受 `interrupted`，否则 409 `invalid_state`
- [ ] 进程重启后遗留的 `running` 任务被识别为 `interrupted`
- [ ] `retry` / `resume` 复用断点，不从零重跑

**事件流**
- [ ] `cursor` 单任务内从 1 起严格递增，无重复无回退
- [ ] `events(id, after)` 只返回 `cursor > after`，升序，空页时 `next_cursor === after`
- [ ] 事件持久化，支持从 `after=0` 完整补读
- [ ] `stage` / `progress` 事件与 `TaskSnapshot.stage` / `progress` 保持同步
- [ ] 非 UTF-8 / 非 JSON 的子进程输出被降级为 `log`，不导致任务失败

**产物**
- [ ] `completed` 时 `artifacts()` 立即可用，否则返回 409 `artifacts_not_ready`
- [ ] `stable_json` 段落 ID 唯一、时长为正、列表非空
- [ ] `annotated_csv` 严格 9 列，`position` 引用的 ID 均存在于 `stable_json`
- [ ] `annotated_csv` 行数与 `final_srt` cue 数完全相等
- [ ] `annotated_csv` 与 `final_srt` 成对出现，`sha256` / `bytes` 真实

**安全**
- [ ] Sidecar 只绑 `127.0.0.1`，令牌随机且常量时间比较
- [ ] API Key 不出现在任何事件、日志、快照中
- [ ] 产物路径落在任务目录内，标识符字符集受限
- [ ] 退出时子进程全部回收，取消时级联终止

---

## 13. 版本演进规则

- `adapter_schema` 与 `artifact_schema` 独立演进：前者约束任务协议，后者约束产物格式。
- **v1 内只允许新增可选字段**。任何字段的删除、重命名或枚举收窄都必须提升对应 schema 版本。
- 新增 `features` 位时默认视为 `false`，老 Provider 无需改动即可继续工作。
- 宿主在适配层对 `schema` 做强校验：版本不匹配时直接拒绝该 Provider，而不是尝试兼容执行。
