# Nonoka X 系统架构与契约规范

本文档定义了 Nonoka X 的系统架构、执行协议、字幕文档模型以及云端同步规范。

---

## 1. 架构总览与设计原则

Nonoka X 是一个本地优先的 AI 字幕生产与编辑桌面应用，核心目标是将“字幕产品体验”与“转写算法流水线”深度解耦：

- **产品与算法解耦**：Nonoka X 负责桌面媒体库、任务调度、统一字幕文档（EditDocument）、JASSUB 轨道编辑与多格式导出；转写与纠错翻译的算法由可插拔的引擎提供。
- **两层引擎**：转写层（`nonoka_x.transcription`）把媒体变成 Nonoka 自己的 `transcript.json`；纠错层（`nonoka_x.correction`）从 `transcript.json`（加转写引擎的私有附件）往后出字幕、纠错、翻译。worker 只按任务请求从两层的注册表里取实现，没有任何引擎的专属分支。详见 [转写引擎与纠错引擎](transcription-engines.md)。
- **FineSub 是可插拔的引擎包**：[FineSub](https://github.com/caca2331/finesub) 的人声分离、VAD、Whisper fw-refine、稳定化、说话人分离、LLM 纠错翻译与知识库，分装在 `engines/` 下的三个包里（转写、纠错翻译、知识库），各跑在自己的进程里；核心不 import 包里的任何代码，删掉哪个包，核心照常启动、其余引擎照常可用。
- **托管、密闭的运行环境**：本机 FineSub 转写跑在 Nonoka 构建、托管在 R2 的三层预制包上（Python 环境、模型、工具），不用用户机器上的 Python；每个包进程的环境都从零构建，转写进程另在沙箱里跑。
- **云端只做识别**：Cloud Provider 只在 GPU 容器里跑 FineSub 转写包到 stable，交出与本机转写引擎同形的结果（`transcript.json` 加不透明附件）；纠错与翻译取回本机，用本机配置的模型完成。
- **本地优先**：用户媒体和字幕编辑文档默认始终保存在本机；云端任务与字幕云端同步作为独立扩展能力，不破坏离线编辑的核心体验。

```text
┌──────────────────────────────────────────────────────────────────────┐
│ Wails + React 19                                                     │
│ 媒体库 / 设置（引擎、运行环境）/ 任务进度 / JASSUB 编辑器 / 本地导出      │
└───────────────┬────────────────────────────────────┬─────────────────┘
                │ Nonoka X Execution Contract        │
┌───────────────▼──────────────────┐  ┌──────────────▼──────────────────┐
│ Local Provider（loopback sidecar）│  │ Cloud Provider（Go 两段式 relay）│
│ 配置与密钥 / 层管理器 / 迁移        │  │ Worker + Modal：只跑 FineSub 转写包│
└───────────────┬──────────────────┘  └──────────────┬──────────────────┘
                │ 每个任务一个 worker 进程（只做编排）    │ transcript.json + 附件，取回后
                ▼                                     │ 以 cloud_transcript 交本机续跑
  转写层：注册表按 transcription.engine 取              │
  ├─ finesub ──────────► engines/finesub-transcribe（独立进程，沙箱，三层）
  ├─ mai2 / gemini-transcribe / moss-pro（核心自带，worker 进程内，只要工具层）
  └─ finesub-cloud ───► 任务交给 Cloud Provider ───────┘
                │
                │ transcript.json + 引擎的私有附件（tasks/<id>/native/<包>/）
                ▼
  纠错层：注册表按 correction.engine 取
  └─ finesub ──────────► engines/finesub-correct（独立进程，启动器解释器）
                           ⇅ 知识库协议
                         engines/finesub-knowledge（独立进程）
                │
                │ correction.json + raw / final SRT
                ▼
        Artifact Projector ──► EditDocument ──► 本地 Document Store
```

本机运行环境（大文件根 `runtime/`，见 4.3）：

```text
runtime/layers/
├── python/<版本>/   Python 环境层：python-build-standalone 3.12.1 + 锁里的全部包（不建 venv）
├── models/<版本>/   模型层：模型清单里钉住 revision 的全部模型
└── tools/<版本>/    工具层：ffmpeg、ffprobe、tokcount、yt-dlp、aria2c、node、pot-provider
     每层旁边一个 current.json 指向在用的版本；层在运行时只读

FineSub 转写进程 = Python 环境层的 python.exe -I -B
                 + 模型层（模型目录变量指向它，离线加载）
                 + 工具层（PATH 里只有工具层与系统目录）
                 + 沙箱（用户目录与临时目录改向 engines/finesub-transcribe/sandbox/）
纠错、知识库进程 = 启动器的 python -I，不进沙箱；工具同样只认工具层
云 API 引擎      = 在 worker 进程里跑，只要工具层的 ffmpeg / ffprobe 和各自的密钥
```

---

## 2. 核心组件与职责边界

### 2.1 引擎层与纠错层
- **转写层** `src/nonoka_x/transcription/`：`EngineSpec`（ID、种类 `local-gpu` / `cloud-api` / `cloud-service`、能力、要的密钥、要的运行环境）、`TranscriptionEngine` 协议、注册表与 `transcript.json` 的读写校验。核心自带三个云 API 引擎（MAI-Transcribe-2、Gemini 3.5 Transcribe、MOSS Pro），在 worker 进程里跑，不 import FineSub。
- **纠错层** `src/nonoka_x/correction/`：只有协议、注册表和 `correction.json` 的读写校验；目前唯一的实现是 FineSub 纠错包（ID `finesub`），界面不提供选择。
- **worker** `src/nonoka_x/worker.py`：只做编排——转写引擎交出 `TranscriptionResult`（`transcript.json` 路径加 `native` 附件表），目标过了 `stable` 再交给纠错引擎。没装纠错包时，`raw-srt` 由核心从 `transcript.json` 渲染（现状例外见 [transcription-engines.md](transcription-engines.md) 第 1 节）。
- 契约细节见 [转写引擎与纠错引擎](transcription-engines.md) 与 [Provider 接口规范](provider-spec.md) 第 4.1.1、7 节。

### 2.2 引擎包与 Engine Host
FineSub 按固定的上游 commit（`UPSTREAM.json`）分装成三个包，代码随桌面安装包下发，不进运行环境层：

| 包 | 种类 | 解释器 | 内容 |
| :--- | :--- | :--- | :--- |
| `engines/finesub-transcribe/` | 转写 | Python 环境层 | `run_transcription`（到 stable 为止，含声学说话人分离）、`finesub_bootstrap` 原件（删了自下载） |
| `engines/finesub-correct/` | 纠错 | 启动器 | `run_post_stable`（stable 之后的全部 LLM 纠错翻译与后处理）、兼容 `finesub_bootstrap` |
| `engines/finesub-knowledge/` | 知识库 | 启动器 | 知识库的全部代码；转写与纠错两半都经它的协议读写知识库 |

- 每个包根目录有 `PACKAGE.json`（标识、种类、入口、解释器、要的密钥名、引擎的能力与运行环境声明、就绪声明）。核心的 `nonoka_x.engine_host` 扫描 `engines/`，有哪个包就注册哪个；它拉起包的进程，经一行一个 JSON 的协议交换启动上下文、请求、事件与结果。
- 包里的 FineSub 文件逐字节等于分包前的上锁文件（各包的 `QUALITY_LOCK.json`），规则见 [FineSub 引擎集成、质量锁与上游对齐](engine.md)。
- 引擎读写的私有数据在 `<数据目录>/engines/<包>/`，任务的原生产物在 `tasks/<id>/native/<包>/`；核心只把原生产物当不透明附件登记，不解析。

### 2.3 运行环境：三层预制包与沙箱
- **层管理器** `src/nonoka_x/runtime/`：按 `resources/layers.json` 从 R2（`https://layers.nonoka.online/`）下载各层的 `tar.zst` 与逐文件清单，校验、解压、切换 `current.json`、修复、清理。层的版本号取自它清单的哈希；就绪 = 当前版本目录与清单相符。构建与发布见 [预制环境层手册](runtime-layers.md)。
- **各引擎要什么**由 `EngineSpec.environment` / `PACKAGE.json` 声明：FineSub 转写要三层；云 API 引擎只要工具层；FineSub 纠错与知识库不要层（跑在启动器里），只用工具层里的 ffmpeg、ffprobe、tokcount。
- **密闭运行**：包进程的环境从零构建，每个变量要么是按名带的系统必需项、要么由 Nonoka 无条件写入，其余一律不存在；解释器以 `-I` 启动。机器上的外部环境变量（代理、HF 镜像、`CUDA_VISIBLE_DEVICES`、线程数……）和外部配置文件不再影响引擎，外部的东西要生效只能先成为 Nonoka 的设置。
- **转写进程的沙箱**：用户目录与临时目录指向 `engines/finesub-transcribe/sandbox/`（`home/` 跨运行共用，`temp/<进程>/` 每进程一份、结束即删），当前目录是任务目录，模型离线加载（`HF_HUB_OFFLINE=1`）。纠错与知识库进程不进沙箱，用真实的用户目录（本地 Agent CLI 要自己的登录状态）。启动细节见 [engine.md](engine.md) 2.1a（受进程级沙箱约束，OS 级全局缓存与显卡驱动行为除外）。
- **桌面端关闭全部编译加速**：人声分离与 qwen 裁判都走 eager，结果不随机器与编译器变化，代价是更慢。

### 2.4 Local Provider（本地执行）
由 Wails 后端启动并监管的 Python sidecar 服务：
- 仅绑定 `127.0.0.1` 随机端口，启动时生成 256 位随机会话令牌，前端请求均需校验令牌。
- 直接读取本地媒体文件，不产生网络上传（云 API 引擎除外：它们把音频发给所选的第三方 API）；执行时为任务建立受 LRU 容量上限管理的本机工作副本。
- 每个任务拉起一个隔离 worker 进程，worker 再拉起所需的包进程；应用退出时将任务标记为 `interrupted`，支持下次启动时断点续跑。
- 持有 Nonoka 自己的配置与密钥（`settings.json`、`secrets.json`，`nonoka_x.config`）：密钥只按包声明的名字经进程环境变量交给包，不落盘；FineSub 要的 `finesub.toml` 等由包内桥接在包的进程里派生。
- 管理运行环境层的安装与清理、用户自配的 LLM API Key，以及把 LLM 环节路由到本机 Codex CLI / Antigravity CLI 的本地 Agent 选项。
- 启动时先检查旧版数据布局，需要时迁移（`nonoka_x.migration`）；迁移的本地步骤完成之前，除迁移接口外一律回 `503 migrating`。

### 2.5 Cloud Provider（云端执行）
通过 HTTPS 与 Nonoka 后端（Cloudflare Worker + Modal，仓库 `nonoka-sub-cloud`）通信：
- 基于业务 Key 进行身份认证、任务额度扣减与单 Key 并发控制（默认最多 1 个任务）。
- 桌面端在本地提取无损音频（`.m4a` 容器）后通过 Presigned URL 上传，**云端不接收原视频，也不接收参与算法计算的视频帧**。另有一张本地已生成的封面缩略图（JPEG，$\le 5\text{ MB}$）随任务上传，仅用于云端媒体库列表展示，不进入转写或纠错流水线。本机跑完再同步上去的条目没有上传槽位，封面改随 `/v1/library/sync` 一起带上；取回字幕时封面也会写进本地媒体库，否则换一台机器打开就只剩占位图，而那边没有视频可以重新截帧。
- Modal 容器只装 FineSub 转写包，跑到 stable 为止，在容器里由转写包的适配器转出 `transcript.json`；作业结果是 `transcript.json` 加不透明附件（`finesub.stable_json`、`finesub.diar_json`），Worker 只存、只发，不解析。容器的运行环境按云端自己的方式装（Linux，不用预制层），加速由容器自行选择。
- 桌面的 relay（`desktop/internal/cloud/relay.go`）取回结果后以 `source.kind=cloud_transcript` 交给本机 Provider 续跑纠错与翻译（[provider-spec.md](provider-spec.md) 2.3）。桌面上两个 FineSub 包都不在时，云端任务照样能出到原始字幕。
- 导入日文轴的任务不走云端：它已经是识别结果，没有要云端识别的东西，桌面直接提示改用本机运行（见 4.4）。
- 具备任务状态持久化、容器抢占自动恢复、断点续跑与产物下载能力。

### 2.6 Artifact Projector（产物投影层）
Projector 是连接算法与编辑器的防腐层，**只读 Nonoka 的格式**，不解析任何引擎的私有文件：

```text
correction.json   -> 纠错引擎的结果：每行起止、原文、译文、说话人、低置信、词级时间
transcript.json   -> 没有 correction.json 时：转写引擎的结果，一行一句、只有原文
                    ↓
EditDocument      -> t0/t1/ja/zh/words/low_conf/tracks
```

FineSub 产物（stable、annotated 表、final SRT）到 `correction.json` 的转换在纠错包的适配器里做（[provider-spec.md](provider-spec.md) 7.4）。前端严禁直接解析引擎内部的 JSON/CSV 文件，算法细节变化只在包的适配器里适配，并经契约测试覆盖。

### 2.7 Document Store（本地文档存储）
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
    "version": "0.7.0",
    "commit": "7123055da0cd6e037d8e37feb53b82f9e428c5cb"
  },
  "features": {
    "raw_srt": true,
    "translation": true,
    "video_multimodal": true,
    "knowledge": true,
    "resume": true,
    "diarization": true
  },
  "devices": [
    {"id": "cuda:0", "name": "NVIDIA GPU", "memory_mb": 12288}
  ],
  "runtime": {
    "ready": true,
    "issues": [],
    "stages": [
      {"id": "raw-srt", "label": "生成原始字幕", "ready": true, "issues": []}
    ]
  },
  "transcription_engines": [
    {"id": "finesub", "label": "FineSub", "kind": "local-gpu", "provider": "local", "ready": true, "issues": []}
  ],
  "correction_engines": [{"id": "finesub", "ready": true, "issues": []}],
  "settings": {"llm_key_configured": true}
}
```

`runtime.ready` 为 `false` 时前端直接以 `issues[0].message` 阻止任务提交；`stages` 让拦截精确到具体目标（仅生成原始字幕时不必要求 LLM 就绪），其中 `raw-srt` 档按所选转写引擎判断，其余档由纠错引擎给。`engine` 是已装引擎包所带的 FineSub 版本，一个包都没装时为空串。`transcription_engines`、`correction_engines` 列出注册表里的引擎及各自的就绪状态，字段见 [provider-spec.md](provider-spec.md) 4.1.1。

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
  "knowledge_subjects": [
    { "id": "已有主体的 id", "kind": "streamer", "subject": "主播的官方或源语言名称", "aliases": "", "description": "" },
    { "kind": "work", "subject": "新建主体的名称", "aliases": "常用译名、旧名", "description": "用于消歧的主体背景" }
  ],
  "transcription": {"engine": "finesub", "options": {}},
  "cleanup_intermediate": false
}
```
*云端请求的 `source` 为 `{"kind": "uploaded_audio", "object_id": "..."}`，不传递本地绝对路径；云端识别完成后桌面的本机续跑请求用 `{"kind": "cloud_transcript", ...}`。`transcription` 缺省视为 `finesub`，引擎记在请求里，重试与续跑沿用原引擎。*

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
    "version": "0.7.0",
    "commit": "7123055da0cd6e037d8e37feb53b82f9e428c5cb"
  },
  "transcription_engine": "finesub",
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
    "transcript_json": {
      "uri": "file:///.../tasks/<id>/transcript.json",
      "sha256": "...",
      "bytes": 12345
    },
    "correction_json": {
      "uri": "file:///.../tasks/<id>/correction.json",
      "sha256": "...",
      "bytes": 34567
    },
    "raw_srt": {
      "uri": "file:///.../video-raw.srt",
      "sha256": "...",
      "bytes": 8192
    },
    "final_srt": {
      "uri": "file:///.../video.srt",
      "sha256": "...",
      "bytes": 10240
    },
    "stable_json": {
      "uri": "file:///.../tasks/<id>/native/finesub-transcribe/video-stable.json",
      "sha256": "...",
      "bytes": 23456
    }
  }
}
```
`transcript_json`、`correction_json` 和两份字幕是 Nonoka 的格式；其余键（`stable_json`、`annotated_csv` 等）是引擎的私有附件，宿主只列出、不解析（[provider-spec.md](provider-spec.md) 4.5）。

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
Projector 逐行照抄 `correction.json`（没有时用 `transcript.json`，一行一句、`zh` 为空）：

| EditDocument 字段 | `correction.json` 的项 | 备注 |
| --- | --- | --- |
| `t0 / t1` | `start` / `end` | 零时长的行已由引擎补到 0.04 秒 |
| `ja` | `source` | `stable` 模式下是识别文字 |
| `zh` | `translation` | `stable` 模式没有，为空串 |
| `words` | `words` | 引擎自己的词对象，原样进文档 |
| `low_conf` | `low_confidence` | 标记需人工复核的段落 |
| 所在轨道 | `speaker` | 说话人分离的结果，只决定行落在哪条说话人轨 |

FineSub 产物到这些项的对应（final SRT 的时间、annotated 表的纠错文字与说话人列、stable 的词级时间）由纠错包的适配器算，规则见 [provider-spec.md](provider-spec.md) 7.4。

> **对齐断言**：FineSub 的 annotated 表与 final SRT 对不上时，纠错包不写 `correction.json`，而是在产物清单顶层给出 `projection_error`，Projector 以它判定失败并保留原始产物，禁止通过截断或模糊匹配猜测对齐。用户在编辑器中自定义增加的轨道属于 Nonoka X 文档本身，后台重投影时不得覆盖人工轨道。

**说话人轨（FineSub 0.6.0 说话人分离）**：投影的顺序是「投影 → 有导入轴就按轴重排 → 否则按说话人
分轨」。轴里标了说话人时以轴为准（一人一个 Style 比模型准）；轴没标说话人、或根本没有轴时，用引擎的
说话人标签：发言最多的人留在主轨，其余每人一条 `diar<n>` 轨（名称「说话人 s1」，或人写的名字），
没有标签的行留在主轨，最多 `MAX_SPEAKERS` 条。投影生成的轨道 id 以 `axis` / `diar` 开头，重投影时
整组替换；用户在编辑器里建的轨道（`t…`）原样保留，主轨的样式也保留，只更新轨道名。

### 4.3 本地存储目录布局
主数据目录（Windows 默认 `%APPDATA%\Nonoka X`）只放 Nonoka 自己的东西；引擎的私有文件只在 `engines/` 与任务的 `native/` 下（`nonoka_x.runtime.paths`）：

```text
<数据目录>/
├── settings.json、secrets.json   Nonoka 的配置与密钥（唯一的事实来源；密钥绑定当前 Windows 账户保护）
├── storage.json                  搬过家的大目录记在这里（见下）
├── runtime-state/                层管理器与迁移自己的小状态（如迁移日志 migration.json）
├── documents/<video-id>/         字幕工程（见下）
├── tasks/<id>/                   本机任务：request.json、快照、事件、artifacts.json、
│   │                             transcript.json、correction.json、字幕
│   └── native/<包>/              引擎的原生产物（如 FineSub 的 -stable.json、-annotated.csv），核心不解析
├── engines/
│   ├── finesub-transcribe/       转写包的私有目录：run/<进程>/（派生的配置，进程结束即删）、
│   │                             sandbox/home/（沙箱的用户目录，跨运行共用）、sandbox/temp/<进程>/
│   ├── finesub-correct/          纠错包：run/、限流状态 state、兼容包的 user-data/ 等
│   └── finesub-knowledge/        知识库包：knowledge/（知识库本身）
├── runtime/                      大文件根（可搬家，见下表）
└── videos/                       视频缓存（可搬家）
```

`<数据目录>` 里没有 FineSub 格式的配置文件：`finesub.toml`、`.env` 之类由包内桥接在 `engines/<包>/run/<进程>/` 里临时派生，每个进程一份。`%LOCALAPPDATA%\FineSub` 不再使用，也不去碰。

每个本地视频工程保存在 `documents/<video-id>/`：
- `document.json`：当前最新可编辑字幕文档（含这个视频自己的 ASS 样式表，见下）。
- `original.json`：首次投影结果（用于比对修改与知识积累）。
- `history/<rev>.json`：历史版本快照。
- `peaks.json`：本地音频波形数据。
- `artifacts.json`：投影所用任务的产物清单（路径与哈希；云端媒体库同步读它）。
- `axis.json`：用户导入的已有轴（若有），见 4.4。

数据目录里有两个会长到几十 GB 的目录，分别在「设置 → 运行环境」与「设置 → 存储与网络」中支持整体搬到别的盘：

| 目录 | 默认位置 | 内容 |
| :--- | :--- | :--- |
| 大文件根（运行环境） | `<数据目录>/runtime` | 预制运行环境层 `layers/<层>/<版本>/`（`python`、`models`、`tools` 三层，每层旁边一个 `current.json` 指向在用的版本）与 `cache/`（只放随时可删的东西：`downloads/` 是还没装完的层压缩包，断点续传用；`<包>/` 是引擎的编译缓存）。格式与安装见 `docs/runtime-layers.md` |
| 视频缓存目录 | `<数据目录>/videos` | 任务执行期间复制的视频工作副本，按缓存上限 LRU 回收 |

- 选择结果记在数据目录根部的 `storage.json`；字段为空即表示仍用默认位置，因此备份还原到另一台机器不会带着一个不存在的盘符。
- 迁移运行环境时桌面端会先停掉本地服务（它的 worker 用的 Python 层就在这棵树里），把整个 `runtime/` 搬过去，再用新路径重新拉起。层里不含任何绝对路径（`docs/runtime-layers.md` 1.4、1.5），搬完不需要任何修补；层管理器的就绪检查照常通过。
- 桌面端自己找托管工具（视频编辑、导出用的 FFmpeg，下载器用的 yt-dlp 与 PO Token）也在工具层里找：`layers/tools/current.json` 指向的版本目录下 `<工具 id>/`（`desktop/internal/managedtools`）。
- 跨盘迁移直接写入目标目录，期间在同级放一个 `<目标>.nonoka-partial` 标记：`storage.json` 才是提交点，所以中途取消或崩溃时目标只是一堆孤立文件，Nonoka X 仍指向原目录。标记让下次迁移认出这是自己没做完的副本并**从中断处续传**（按文件大小跳过已就位的），而不是重新复制几十 GB；标记里记着来源路径，另一个目录的半成品不会被误认领。
- 失败或取消都不删已复制的部分，也不动原目录——只有目标完整落盘后才删源。
- 同盘迁移走 rename。Windows 上目录里只要还有打开的句柄（刚写完的 exe/dll 正被杀软扫描），rename 就会报 `Access is denied`，因此 rename 与删源都带退避重试；跨盘的 `ERROR_NOT_SAME_DEVICE` 不可重试，直接转复制路径。
- 工程文档、字幕、样式、偏好这类小文件始终留在数据目录，不随迁移移动。
- **首次启动位置确认**：首次打开应用且基础环境（工具层或 Python 环境层）尚未安装时，桌面端会自动引导至「设置 → 运行环境」并在安装前展示位置确认卡，提示下载体量与目标盘剩余空间。此时更换安装目录只需更新记录、无需复制大文件；确认后安装默认基础环境（工具层与 Python 环境层），AI 模型层则按需单独下载与删除（判定与界面逻辑见 `desktop/frontend/src/app/App.tsx` 与 `desktop/frontend/src/components/RuntimeSection.tsx`）。

ASS 样式表**跟着视频走**，整段 `[V4+ Styles]` 原文存在 `document.json` 的 `styles` 字段里：
- 于是在预览画面里直接拖字幕、改颜色只影响当前这个视频；样式也随文档一起进历史版本、
  随导入导出一起走。落盘走文档那条 `rev` 乐观锁链路（Ctrl+S / 每 5 分钟自动保存）。
- 数据目录根部的 `styles.ass` 降级为**新文档的默认模板**（机器级偏好），只在文档还没有
  自己那份样式表时当种子用，不参与渲染；样式编辑器里可以「存为本机默认」更新它。
- 编辑器内可任意编辑，也可从任意 `.ass` 导入其 `[V4+ Styles]` 段（同名覆盖、新名追加）。
- 写死的 `origin` / `cn` 两个默认样式不写进文档，装载时与它合并；同名以文档那份为准。
- 绑定了这个视频的样式表里没有的样式名时，预览与导出一律回退到 `origin`（原文）/ `cn`（译文），
  不丢输出线；打开文档时会提示一次。

### 4.4 导入已有产物（空轴 / 单语轴 / 双语轴）

用户可以带着自己打好的轴开工。四种产物走三条完全不同的路，判型与解析在前端的 [`assAxis.ts`](../desktop/frontend/src/subtitles/assAxis.ts)，落地在 [`axis.py`](../src/nonoka_x/axis.py)：

| 轴型 | 判据 | 走法 |
| :--- | :--- | :--- |
| 空轴 | 只有时间，或只有反复出现的打轴批注 | 识别照常跑完，Projector 把结果按这条轴重排 |
| 日文轴 | 假名行占比 ≥ 30% 且中文行 < 30% | 轴进 `TaskRequest.axis`，worker 不调任何转写引擎，把轴的行直接交给纠错引擎的轴入口（FineSub 纠错包的 `axis` 操作），跳过人声分离 / VAD / ASR，只跑 LLM 翻译。只在本机跑：云端只做识别，桌面对日文轴的云端任务直接提示改用本机运行 |
| 中文轴 | 中文行占比 ≥ 60% 且**唯一率 ≥ 70%** | 不启动任务，直接落成 EditDocument（原文列留空） |
| 双语轴 | 假名行与中文行占比均 ≥ 30% | 同上，两列都有 |

几条约束：

- **日文轴只跑纠错那一半**：识别那半条链整个用不上，所以不需要任何转写引擎，也不需要 Python 环境层与模型层；纠错包没装时由就绪检查拦下。云端只做识别，日文轴没有要云端做的事，桌面在上传之前就拒绝（`desktop/internal/cloud/service.go`）。
- **FineSub 没有「按给定轴识别」这回事**：FineSub 的识别入口（`run_transcription`）全部参数里没有任何时间轴入口，而包里的算法文件是哈希钉死的。空轴因此不进引擎，改由 Projector 在投影阶段重排——这条路对本地与云端一视同仁，轴只记在本机 `documents/<video-id>/axis.json`，从不上传。
- **重排的口径**：输出的行数恒等于轴的行数，每行带着轴自己的时间，识别不到内容的行留空交给用户填。词级时间戳是原始 ASR 的，因此只在**未经 LLM 纠错**时用来切分原文；纠错后的一行跨了几行轴时整句落在重叠最多的那一行，绝不在猜出来的位置切开。
- **说话人优先来自轴**：ASS 一人一个 Style 是这套轴的实际约定，人标的比 diarization 准。读出 ≥ 2 位说话人时按人分轨，`Default` / `JP` / `CN` / `origin` 这类排版样式不算人，此时转写对话框不再提供说话人分离。轴没标说话人时（空轴），本次运行若开了说话人分离，每一行取落在它上面发言最多的那位说话人，再按说话人分轨。
- **两人同时说话是这条路唯一的失准点**：词只带「什么时候说的」而不带「谁说的」；重叠的两行都盖住同一批词，先开始的那行会把它们全拿走。刻意不改判据——说话人标签只决定一行归谁，不在行之间搬词，换一种猜法只是另一种猜。解析器统计这类行数，弹窗据此提示需要人工校对；只有空轴会踩到（其余轴型的文字是用户自己写好的，不经识别）。
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
媒体库只存 Nonoka 的格式和字幕（`docs/provider-spec.md` 7.5）：`transcript_json`、`correction_json`、`raw_srt`、`final_srt`。引擎自己的产物（FineSub 的 stable、annotated 表等）是任务目录里的私有附件，不上云。本地任务完成后，Go 后端自动触发同步：
1. 校验产物路径必须位于 Nonoka X `tasks/` 白名单内。
2. 提取上面四个（总计限制 $\le 32\text{ MB}$）；必须有 `transcript_json` 或 `correction_json`，否则为旧版格式任务，提示先迁移。
3. 调用 `POST /v1/library/sync` 上传这些文本、引擎 commit 与媒体指纹。两种文档格式都不带的请求（旧桌面版）服务端回 `426` 并提示升级。
4. 服务端按指纹更新对应记录，不上传音视频，不扣减配额。

取回（adopt）时桌面下载同样四个名字，交给 sidecar 的 `POST /v1/documents/project`：有 `correction.json` 就逐行照抄它，否则从 `transcript.json` 投影（`provider-spec.md` 7.1）。云端已有的旧条目由一次性转换脚本（云端仓库 `modal_compute/nonoka_x_compute/operations/library_convert.py`）转成新格式，步骤见 `docs/library-cutover.md`。

### 5.4 无损音频提取与上传
云端任务只需纯音频：
- **压缩源音轨**（AAC/Opus/MP3/FLAC 等）：使用 `-c:a copy -f mp4` 原样提取入 `.m4a` 容器。
- **未压缩源或特殊格式**（PCM/WMA 等）：使用 `-c:a alac` 无损重编码。
- **不重采样、不下混、不改变位深**，保证上传音频零损失。
- 单个音频限制：时长 $\le 2\text{ 小时}$，对象大小 $\le 4\text{ GiB}$，Presigned PUT 有效期 4 小时。

---

## 6. 云端 API 规范

完整的接口、数据与部署说明在云端仓库（`nonoka-sub-cloud`）的 `docs/cloud-api.md`；下表是桌面端用到的部分。

| 方法 | 路径 | 描述 | 配额影响 |
| :--- | :--- | :--- | :--- |
| `GET` | `/v1/session` | 验证当前 Key，获取剩余额度与进行中任务数 | 无 |
| `GET` | `/v1/capabilities` | 云端引擎版本与能力；`artifact_schema` 是作业结果的格式（2：`transcript.json` 加附件），与桌面不一致时桌面拒绝提交 | 无 |
| `GET` | `/v1/library` | 获取云端视频列表（管理员返回所有 Key 的视频） | 无 |
| `POST` | `/v1/library/sync` | 同步本地已完成的字幕产物（只收 `transcript_json`、`correction_json`、`raw_srt`、`final_srt`，见 5.3） | 无 |
| `GET` | `/v1/library/{video_id}/artifacts[/{name}]` | 库条目的产物清单与原字节 | 无 |
| `DELETE` | `/v1/library/{video_id}` | 删除云端视频记录与字幕产物 | 无 |
| `POST` | `/v1/uploads/init` | 获取音频上传 Presigned URL | 无 |
| `POST` | `/v1/tasks` | 提交云端识别作业（目标 `stable`）并启动 GPU Worker | **扣减 1 次**（有未兑现票据时不扣） |
| `GET` | `/v1/tasks?limit={n}` | 查询当前 Key 的任务列表 | 无 |
| `GET` | `/v1/tasks/{id}` | 查询指定任务详情与阶段快照 | 无 |
| `GET` | `/v1/tasks/{id}/events` | 增量读取任务阶段事件与进度 | 无 |
| `GET` | `/v1/tasks/{id}/artifacts[/{name}]` | 作业结果：`transcript_json` 与不透明附件（`finesub.stable_json`、`finesub.diar_json`）；旧名字 `stable_json`、`diar_json` 回 `426` 提示更新桌面 | 无 |
| `POST` | `/v1/tasks/{id}/cancel` | 取消任务执行并保留已完成阶段断点 | 无 |
| `POST` | `/v1/tasks/{id}/retry`、`/resume` | 重跑 / 从中断处断点续跑 | 不重复扣减 |
| `DELETE` | `/v1/tasks/{id}` | 结果已取回：释放云端资源，日志保留 | 无 |
| `GET` | `/v1/admin/keys` | 管理员查询所有 Key 的密钥、余量与统计 | 无 |
| `POST` | `/v1/admin/keys` | 管理员创建新业务 Key | 无 |
| `PUT` | `/v1/admin/keys/{key_id}` | 管理员修改 Key 名称与余量 | 无 |
| `DELETE` | `/v1/admin/keys/{key_id}` | 管理员吊销 Key | 无 |

---

## 7. 安全模型

1. **本地 Sidecar 隔离**：仅监听 loopback，每次启动使用随机端口与 256 位令牌。
2. **用户 Key 保护**：本地配置的 Key 只存在 `secrets.json`（绑定当前 Windows 账户的保护），严禁写入日志、上传云端或暴露给前端持久化。交给引擎包时只按包在 `PACKAGE.json` 里声明的名字、经进程环境变量传，不写进任何派生文件：转写包只拿 `HF_TOKEN`（配了才有），纠错包拿 LLM 与检索的 Key。
3. **媒体流沙盒**：本地内置的 Loopback 媒体服务器为视频流生成 24 字节随机 Token 路由，支持 HTTP 206 Range 分块传输，防止外部进程探测。
4. **引擎进程密闭**：包进程的环境从零构建、解释器隔离启动，外部环境变量与外部配置文件进不去；FineSub 转写进程的用户目录与临时目录改向沙箱，模型离线加载、不自行下载。由 `tests/test_engine_hermetic.py`（覆盖扫描、投毒、读取审计）、`tests/test_engine_sandbox.py` 与闸门 `--real` 的沙箱写入检查证明。
