# Nonoka X 系统架构与契约规范

本文档定义了 Nonoka X 的系统架构、执行协议、字幕文档模型以及云端同步规范。

---

## 1. 架构总览与设计原则

Nonoka X 是一个本地优先的 AI 字幕生产与编辑桌面应用，核心目标是将“字幕产品体验”与“转写算法流水线”深度解耦：

- **算法与流水线解耦**：底层由 [FineSub](https://github.com/caca2331/finesub) 提供人声分离、VAD、Whisper fw-refine、稳定化、LLM 纠错翻译和知识库流水线。
- **产品与编辑体验**：Nonoka X 负责桌面媒体库管理、多执行模式调度、统一字幕文档（EditDocument）、专业 JASSUB 轨道编辑与多格式导出。
- **同源双执行模式**：本地执行与云端执行运行完全相同的 FineSub 引擎快照、产物转换器和任务协议，仅在执行环境、鉴权方式与存储介质上有所区分。
- **本地优先**：用户媒体和字幕编辑文档默认始终保存在本机；云端任务与字幕云端同步作为独立扩展能力，不破坏离线编辑的核心体验。

```text
┌─────────────────────────────────────────────────────────────┐
│ Wails + React 19                                            │
│ 媒体库 / 任务设置 / 进度展示 / JASSUB 编辑器 / 本地导出        │
└──────────────────────┬──────────────────────────────────────┘
                       │ Nonoka X Execution Contract
                ┌──────┴──────┐
                │             │
┌───────────────▼──────┐  ┌───▼──────────────────────────────┐
│ Local Provider       │  │ Cloud Provider                   │
│ loopback sidecar     │  │ HTTPS API + 业务 Key             │
│ 读取本地媒体工作副本   │  │ 仅上传无损音频 / GPU 调度         │
└───────────────┬──────┘  └───┬──────────────────────────────┘
                │             │
                └──────┬──────┘
                       ▼
             同版本 FineSub Engine Bundle
                       │
             stable / annotated / final SRT
                       │
                       ▼
               Artifact Projector
                       │
                       ▼
                  EditDocument
                       │
                       ▼
               本地 Document Store
```

---

## 2. 核心组件与职责边界

### 2.1 FineSub Engine Bundle
由固定上游 commit 生成的完整独立引擎包，包含 `finesub` 核心、`finesub_bootstrap`、依赖锁定和资源清单。它专注于音频与文本流水线，对 UI 界面、媒体库和编辑器透明。

### 2.2 Local Provider（本地执行）
由 Wails 后端启动并监管的 Python sidecar 服务：
- 仅绑定 `127.0.0.1` 随机端口，启动时生成 256 位随机会话令牌，前端请求均需校验令牌。
- 直接读取本地媒体文件，不产生网络上传；执行时为任务建立受 LRU 容量上限管理的本机工作副本。
- 监管隔离 worker 进程；应用退出时将任务标记为 `interrupted`，支持下次启动时断点续跑。
- 管理本地 FineSub 运行时环境、模型下载、缓存与用户自配的 LLM API Key，以及把 LLM 环节路由到本机 Codex CLI / Antigravity CLI 的本地 Agent 选项。

### 2.3 Cloud Provider（云端执行）
通过 HTTPS 与 Nonoka 后端通信：
- 基于业务 Key 进行身份认证、任务额度扣减与单 Key 并发控制（默认最多 1 个任务）。
- 桌面端在本地提取无损音频（`.m4a` 容器）后通过 Presigned URL 上传，**云端不接收原视频，也不接收参与算法计算的视频帧**。另有一张本地已生成的封面缩略图（JPEG，$\le 5\text{ MB}$）随任务上传，仅用于云端媒体库列表展示，不进入转写或纠错流水线。本机跑完再同步上去的条目没有上传槽位，封面改随 `/v1/library/sync` 一起带上；取回字幕时封面也会写进本地媒体库，否则换一台机器打开就只剩占位图，而那边没有视频可以重新截帧。
- 云端 GPU 容器执行纯音频流水线，LLM 纠错与翻译固定使用识别文本（`correction.media=text`，`retrieval=none`）。
- 导入日文轴的任务是唯一不上传音轨的一种：识别那半条链整个用不上，云端只跑 LLM 翻译段（见 4.4）。
- 具备任务状态持久化、容器抢占自动恢复、断点续跑与产物下载能力。

### 2.4 Artifact Projector（产物投影层）
Projector 是连接算法与编辑器的核心防腐层。它负责将 FineSub 阶段产物映射为稳定的 Nonoka X 编辑文档：

```text
stable.json       -> 原始日文、词级时间戳、源段编号 (source_ids)
*-annotated.csv   -> 纠错日文、中文翻译、置信度、源段映射
final.srt         -> 最终后处理时间轴与中文
                    ↓
EditDocument      -> t0/t1/ja/zh/words/low_conf/tracks
```

前端严禁直接解析 FineSub 内部 JSON/CSV 文件，算法细节变化仅在 Projector 层适配并经契约测试覆盖。

### 2.5 Document Store（本地文档存储）
字幕编辑文档默认持久化于本地：
- 云端任务完成后下载产物清单，由本地 Projector 生成 `EditDocument`。
- 编辑修改、版本回滚、自定义音轨、ASS 样式配置与多格式导出全部在本地完成。
- 文档保存采用 `rev` 乐观锁与原子写入（临时文件写入后原子重命名），避免多窗口或并发写导致数据损坏。

---

## 3. 统一执行协议 (Execution Contract)

### 3.1 Provider 接口定义
> 完整的字段级契约、状态机、错误码、产物格式与自建 Provider 指南见 **[Provider 接口规范](provider-spec.md)**，本节只给出概览。

```ts
interface ExecutionProvider {
  capabilities(): Promise<Capabilities>;
  listTasks(): Promise<TaskListItem[]>;
  start(request: TaskRequest): Promise<TaskSnapshot>;
  status(taskId: string): Promise<TaskSnapshot>;
  events(taskId: string, afterCursor: number): Promise<TaskEventPage>;
  cancel(taskId: string): Promise<TaskSnapshot>;
  retry(taskId: string): Promise<TaskSnapshot>;
  resume(taskId: string): Promise<TaskSnapshot>;
  artifacts(taskId: string): Promise<ArtifactManifest>;
}
```

### 3.2 能力声明 (Capabilities)
前端根据 Provider 返回的 `features` 动态渲染可用选项，严禁硬编码区分。

```json
{
  "provider": "local",
  "adapter_schema": 1,
  "artifact_schema": 1,
  "engine": {
    "name": "finesub",
    "version": "0.5.1",
    "commit": "b9b2f10c80abd58ddff739b60461624f51c0789c"
  },
  "features": {
    "raw_srt": true,
    "translation": true,
    "video_multimodal": true,
    "knowledge": true,
    "resume": true,
    "diarization": false
  },
  "devices": [
    {"id": "cuda:0", "name": "NVIDIA GPU", "memory_mb": 12288}
  ],
  "runtime": {
    "ready": true,
    "issues": [],
    "stages": [
      {"id": "raw-srt", "label": "VAD、ASR 与原始字幕", "ready": true, "issues": []}
    ]
  },
  "settings": {"llm_key_configured": true}
}
```

`runtime.ready` 为 `false` 时前端直接以 `issues[0].message` 阻止任务提交；`stages` 让拦截精确到具体目标（仅生成原始字幕时不必要求 LLM 就绪）。

> **注意**：Cloud Provider 固定返回 `video_multimodal=false`。服务端独立校验请求，若收到视频多模态参数直接返回 400，不做静默降级。

### 3.3 任务请求 (TaskRequest)
```json
{
  "schema": 1,
  "provider": "local",
  "source": {
    "kind": "local_file",
    "path": "/absolute/path/video.mp4",
    "title": "video",
    "fingerprint": "..."
  },
  "target": "final-srt",
  "language": "ja",
  "device": "cuda:0",
  "gpu_tier": "auto",
  "separate": true,
  "vocal_profile": "quality",
  "correction": {
    "enabled": true,
    "media": "video",
    "retrieval": "local",
    "difficulty": "quality",
    "fast": "auto",
    "extra_info": "",
    "extra_style": ""
  },
  "knowledge": "update",
  "cleanup_intermediate": false
}
```
*云端请求的 `source` 为 `{"kind": "uploaded_audio", "object_id": "..."}`，不传递本地绝对路径。*

### 3.4 任务状态与快照 (TaskSnapshot)
```json
{
  "schema": 1,
  "task_id": "task_abc123",
  "provider": "local",
  "state": "running",
  "stage": "aligned",
  "progress": {
    "completed": 42,
    "total": 100,
    "unit": "segments",
    "message": "正在转写"
  },
  "engine": {
    "version": "0.5.1",
    "commit": "b9b2f10c80abd58ddff739b60461624f51c0789c"
  },
  "requested_capabilities": {},
  "effective_capabilities": {},
  "error": null,
  "last_cursor": 128,
  "created_at": "2026-08-28T00:00:00Z",
  "updated_at": "2026-08-28T00:01:00Z"
}
```
统一状态枚举：`queued` | `running` | `completed` | `failed` | `cancelled` | `interrupted`。

### 3.5 任务事件流 (TaskEvent)
```json
{
  "cursor": 17,
  "task_id": "task_abc123",
  "type": "progress",
  "timestamp": "2026-08-28T00:00:30Z",
  "payload": {
    "stage": "aligned",
    "completed": 42,
    "total": 100,
    "unit": "segments",
    "message": "正在识别"
  }
}
```
事件类型：`started` | `stage` | `progress` | `warning` | `log` | `completed` | `failed` | `cancelled`。通过递增 `cursor` 支持断线补读。

### 3.6 产物清单 (ArtifactManifest)
```json
{
  "schema": 1,
  "task_id": "task_abc123",
  "engine_commit": "b9b2f10c80abd58ddff739b60461624f51c0789c",
  "artifacts": {
    "stable_json": {
      "uri": "file:///.../video-stable.json",
      "sha256": "...",
      "bytes": 12345
    },
    "raw_srt": {
      "uri": "file:///.../video-raw.srt",
      "sha256": "...",
      "bytes": 8192
    },
    "annotated_csv": {
      "uri": "file:///.../video-annotated.csv",
      "sha256": "...",
      "bytes": 23456
    },
    "final_srt": {
      "uri": "file:///.../video.srt",
      "sha256": "...",
      "bytes": 10240
    }
  }
}
```

---

## 4. 字幕文档模型与存储

### 4.1 编辑核心句结构 (Seg)
```ts
interface Seg {
  t0: number;          // 起始时间 (秒)
  t1: number;          // 结束时间 (秒)
  ja: string;          // 日文（原文或纠错后）
  zh: string;          // 中文翻译
  words?: WordToken[]; // 词级时间戳
  low_conf?: boolean;  // 低置信度标记
}
```

### 4.2 字段映射与对齐规则
| EditDocument 字段 | FineSub 产物来源 | 备注 |
| --- | --- | --- |
| `t0 / t1` | final.srt 对应行时间轴 | 无 LLM 纠错时回退使用 stable 段时间 |
| `ja` | annotated.csv 的 `corrected_text` | 无 LLM 纠错时使用 stable 的 `text` |
| `zh` | annotated.csv / final.srt 翻译 | raw 模式时为空 |
| `words` | annotated 的 `source_ids` 映射合并 | 关联 stable 词级时间 |
| `low_conf` | annotated 的 `conf == low` | 标记需人工复核的段落 |

> **对齐断言**：当 annotated 与 final.srt 行数不一致时，Projector 必须判定失败并保留原始产物，禁止通过截断或模糊匹配猜测对齐。用户在编辑器中自定义增加的轨道属于 Nonoka X 文档本身，后台重投影时不得覆盖人工轨道。

### 4.3 本地存储目录布局
每个本地视频工程保存在数据目录下的 `documents/<video-id>/`：
- `document.json`：当前最新可编辑字幕文档。
- `original.json`：首次投影结果（用于比对修改与知识积累）。
- `history/<rev>.json`：历史版本快照。
- `peaks.json`：本地音频波形数据。
- `artifacts.json`：FineSub 产物元数据与哈希。
- `axis.json`：用户导入的已有轴（若有），见 4.4。

数据目录里有两个会长到几十 GB 的目录，可以在「设置 → 磁盘占用与位置」整体搬到别的盘：

| 目录 | 默认位置 | 内容 |
| :--- | :--- | :--- |
| 运行环境安装目录 | `<数据目录>/finesub` | FineSub 安装根：`runtime/`（含 Python 虚拟环境）、`models/`、`cache/`、`agent-capsules/` |
| 视频缓存目录 | `<数据目录>/videos` | 任务执行期间复制的视频工作副本，按缓存上限 LRU 回收 |

- 选择结果记在数据目录根部的 `storage.json`；字段为空即表示仍用默认位置，因此备份还原到另一台机器不会带着一个不存在的盘符。
- 迁移运行环境时桌面端会先停掉本地服务（它跑的 Python 就在这棵树里），移动完成后用新路径重新拉起；虚拟环境本身可以整体搬走，`pyvenv.cfg` 里唯一的绝对路径由 FineSub 的健康检查自行修复。
- 跨盘迁移直接写入目标目录，期间在同级放一个 `<目标>.nonoka-partial` 标记：`storage.json` 才是提交点，所以中途取消或崩溃时目标只是一堆孤立文件，Nonoka X 仍指向原目录。标记让下次迁移认出这是自己没做完的副本并**从中断处续传**（按文件大小跳过已就位的），而不是重新复制几十 GB；标记里记着来源路径，另一个目录的半成品不会被误认领。
- 失败或取消都不删已复制的部分，也不动原目录——只有目标完整落盘后才删源。
- 同盘迁移走 rename。Windows 上目录里只要还有打开的句柄（刚写完的 exe/dll 正被杀软扫描），rename 就会报 `Access is denied`，因此 rename 与删源都带退避重试；跨盘的 `ERROR_NOT_SAME_DEVICE` 不可重试，直接转复制路径。
- 工程文档、字幕、样式、偏好这类小文件始终留在数据目录，不随迁移移动。
- **安装前就问一次**：运行环境还没装（安装根小于 64 MB，只有 FineSub 的 store 标记）且用户没自己选过位置时，「运行环境」页会在下载开始前挂出一张位置卡，写明约需 16 GB 与所在盘剩余空间；点「一键准备全部」等大安装会先停在这张卡上确认。这时改位置不用复制任何文件，只是改一条记录——装完再搬就得跨盘挪十几 GB。所在盘装不下建议体量时卡片转为警告态，即使用户已经选过位置也会再问一次。判定逻辑集中在 `frontend/src/components/installLocation.ts`。

ASS 样式表不跟着文档走，整份存在数据目录根部的 `styles.ass`（机器级，所有视频共用）：
- 编辑器内可任意编辑，也可从任意 `.ass` 导入其 `[V4+ Styles]` 段（同名覆盖、新名追加）。
- 写死的 `JP` / `CN` 两个默认样式不写进这个文件，装载时与它合并；同名以 `styles.ass` 为准。
- 云端同步下来的文档若绑定了本机没有的样式名，预览与导出一律回退到 `JP`（原文）/ `CN`（译文），不丢输出线。

### 4.4 导入已有产物（空轴 / 单语轴 / 双语轴）

用户可以带着自己打好的轴开工。四种产物走三条完全不同的路，判型与解析在前端的 [`assAxis.ts`](../desktop/frontend/src/subtitles/assAxis.ts)，落地在 [`axis.py`](../src/nonoka_x/axis.py)：

| 轴型 | 判据 | 走法 |
| :--- | :--- | :--- |
| 空轴 | 只有时间，或只有反复出现的打轴批注 | 识别照常跑完，Projector 把结果按这条轴重排 |
| 日文轴 | 假名行占比 ≥ 30% 且中文行 < 30% | 轴进 `TaskRequest.axis`，由它合成 stable JSON，跳过人声分离 / VAD / ASR，只跑 LLM 翻译。本地与云端都支持，且跑的是同一个函数 |
| 中文轴 | 中文行占比 ≥ 60% 且**唯一率 ≥ 70%** | 不启动任务，直接落成 EditDocument（原文列留空） |
| 双语轴 | 假名行与中文行占比均 ≥ 30% | 同上，两列都有 |

几条约束：

- **两种运行环境同源**：日文轴的翻译由 `nonoka_x.axis.translate_axis` 完成，桌面 worker 与云端 LLM 容器调用的是同一个函数（云端镜像把 `src/` 挂到 `/opt/nonoka-x`，与引擎并排）。云端这条路不提取也不上传音轨——识别那半条链整个用不上，传上去的字节没有任何一段会读它；轴文本本身当然要上传，那正是要翻译的东西。
- **FineSub 没有「按给定轴识别」这回事**：`run_pipeline` 全部参数里没有任何时间轴入口，而引擎快照是哈希钉死的生成产物。空轴因此不进引擎，改由 Projector 在投影阶段重排——这条路对本地与云端一视同仁，轴只记在本机 `documents/<video-id>/axis.json`，从不上传。
- **重排的口径**：输出的行数恒等于轴的行数，每行带着轴自己的时间，识别不到内容的行留空交给用户填。词级时间戳是原始 ASR 的，因此只在**未经 LLM 纠错**时用来切分原文；纠错后的一行跨了几行轴时整句落在重叠最多的那一行，绝不在猜出来的位置切开。
- **说话人来自轴，不来自模型**：ASS 一人一个 Style 是这套轴的实际约定，人标的比 diarization 准，两个 Provider 也都声明 `features.diarization=false`。读出 ≥ 2 位说话人时按人分轨，`Default` / `JP` / `CN` 这类排版样式不算人。
- **两人同时说话是这条路唯一的失准点**：没有说话人识别，词只带「什么时候说的」而不带「谁说的」；重叠的两行都盖住同一批词，先开始的那行会把它们全拿走。刻意不改判据——没有说话人信息时换一种猜法只是另一种猜。解析器统计这类行数，弹窗据此提示需要人工校对；只有空轴会踩到（其余轴型的文字是用户自己写好的，不经识别）。
- **有文字不等于有内容**：真实空轴里常有打轴人留下的中文批注（"前压可以再紧紧"），靠重复度与真正的中文轴分开——批注翻来覆去就那几句，字幕几乎句句不同。

---

## 5. 云端同步与媒体库契约

### 5.1 身份、鉴权与配额
- 桌面端使用业务 Key 登录，请求头携带 `Authorization: Bearer <key>`。
- 服务端以 Key 的 SHA-256 作为索引，`ADMIN_TOKEN` 具备全局管理权限。
- 任务配额 `remaining` 仅在**创建新的云端 FineSub 转写任务**时扣减 1 次。
- **本地完成的字幕同步不消耗云端转写次数**。
- 单个业务 Key 同时最多并发运行 1 个云端转写任务。

### 5.2 媒体库指纹合并
登录后，客户端读取本地 `library.json` 与云端 `/v1/library`：
- 界面按媒体指纹合并呈现，本地卡片展示同步徽标。
- 若存在云端记录但缺失本地视频，提示“需要重新定位本地视频”。
- **云端永远不存储客户端本地绝对路径**。

### 5.3 本地任务自动同步流程
本地任务完成后，Go 后端自动触发同步：
1. 校验产物路径必须位于 Nonoka X `tasks/` 白名单内。
2. 提取 `stable_json`、`raw_srt`、`annotated_csv`、`final_srt`（总计限制 $\le 32\text{ MB}$）。
3. 调用 `POST /v1/library/sync` 上传字幕文本、引擎 commit 与媒体指纹。
4. 服务端按指纹更新对应记录，不上传音视频，不扣减配额。

### 5.4 无损音频提取与上传
云端任务只需纯音频：
- **压缩源音轨**（AAC/Opus/MP3/FLAC 等）：使用 `-c:a copy -f mp4` 原样提取入 `.m4a` 容器。
- **未压缩源或特殊格式**（PCM/WMA 等）：使用 `-c:a alac` 无损重编码。
- **不重采样、不下混、不改变位深**，保证上传音频零损失。
- 单个音频限制：时长 $\le 2\text{ 小时}$，对象大小 $\le 4\text{ GiB}$，Presigned PUT 有效期 4 小时。

---

## 6. 云端 API 规范

| 方法 | 路径 | 描述 | 配额影响 |
| :--- | :--- | :--- | :--- |
| `GET` | `/v1/session` | 验证当前 Key，获取剩余额度与进行中任务数 | 无 |
| `GET` | `/v1/library` | 获取云端视频列表（管理员返回所有 Key 的视频） | 无 |
| `POST` | `/v1/library/sync` | 同步本地已完成的字幕产物 | 无 |
| `DELETE` | `/v1/library/{video_id}` | 删除云端视频记录与字幕产物 | 无 |
| `POST` | `/v1/uploads/init` | 获取音频上传 Presigned URL | 无 |
| `POST` | `/v1/tasks` | 提交云端转写任务并启动 GPU Worker | **扣减 1 次** |
| `GET` | `/v1/tasks?limit={n}` | 查询当前 Key 的任务列表 | 无 |
| `GET` | `/v1/tasks/{id}` | 查询指定任务详情与阶段快照 | 无 |
| `GET` | `/v1/tasks/{id}/events` | 增量读取任务阶段事件与进度 | 无 |
| `POST` | `/v1/tasks/{id}/cancel` | 取消任务执行并保留已完成阶段断点 | 无 |
| `POST` | `/v1/tasks/{id}/resume` | 从中断/失败处断点续跑 | 不重复扣减 |
| `GET` | `/v1/admin/keys` | 管理员查询所有 Key 的密钥、余量与统计 | 无 |
| `POST` | `/v1/admin/keys` | 管理员创建新业务 Key | 无 |
| `PUT` | `/v1/admin/keys/{key_id}` | 管理员修改 Key 名称与余量 | 无 |
| `DELETE` | `/v1/admin/keys/{key_id}` | 管理员吊销 Key | 无 |

---

## 7. 安全模型

1. **本地 Sidecar 隔离**：仅监听 loopback，每次启动使用随机端口与 256 位令牌。
2. **用户 Key 保护**：本地配置的 LLM Key 保存在本地独立存储中，严禁写入日志、上传云端或暴露给前端持久化。
3. **媒体流沙盒**：本地内置的 Loopback 媒体服务器为视频流生成 24 字节随机 Token 路由，支持 HTTP 206 Range 分块传输，防止外部进程探测。
