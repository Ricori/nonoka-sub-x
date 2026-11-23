# 转写引擎与纠错引擎

本文说明 Nonoka X 中一个任务从媒体到字幕要经过哪些部件：转写层与纠错层的契约、已注册的引擎、FineSub 三个引擎包的发现与密闭拉起方式、三层预制运行环境，以及如何扩展接入新引擎。

相关文档：

- [系统架构与契约规范](architecture.md)：整体架构图、数据目录布局。
- [Provider 接口规范](provider-spec.md)：`capabilities()` 里的 `transcription_engines` / `correction_engines`（4.1.1）、请求的 `transcription` 字段（4.2）、`transcript.json` 与 `correction.json`（第 7 节）、错误码（第 5 节）。
- [FineSub 引擎集成、质量锁与上游对齐](engine.md)：三个包的目录、质量锁、包进程怎么密闭启动（2.1a）。

---

## 1. 两层，一条流水线

```text
TaskRequest
  │  transcription.engine（缺省 finesub）           correction.engine（缺省 finesub）
  ▼                                                  ▼
转写层注册表 ──► TranscriptionEngine.transcribe ──► TranscriptionResult ──► 纠错层注册表 ──► CorrectionEngine.run ──► CorrectionResult
                 媒体 → transcript.json             transcript.json        （目标过了 stable 才走）  raw / translated / final 字幕
                       + native 私有附件             + native 附件表                                 + correction.json
```

- **worker 只做编排**（`src/nonoka_x/worker.py`）：按请求里的 ID 从两个注册表取实现，没有任何引擎的专属分支。目标是 `stable` 或更早时只跑转写；目标是 `raw-srt` / `translated-srt` / `final-srt` 时再交给纠错引擎（`raw-srt` 也走它，只是不调 LLM）。
- **另外两个入口**不调转写引擎：
  - `source.kind = cloud_transcript`：云端识别好的结果（`transcript.json` 加附件）直接当作 `TranscriptionResult`，附件复制进 `tasks/<id>/native/<包>/`（[provider-spec.md](provider-spec.md) 4.2.1）。
  - 日文轴（`axis.kind = "ja"`）：轴的行直接交给纠错引擎的轴入口，只跑翻译。
- **没装纠错包时**，`raw-srt` 由核心自己从 `transcript.json` 渲染（`nonoka_x.transcription.srt`，`worker.core_renders_raw_srt`）；纠错、翻译、知识库各档报 `missing_engine`。本机 FineSub、云 API 引擎与云端识别的续跑都能这样出原始字幕：这时启动检查不看 `harness` 档（`local_provider._start`）。
- 两层之间唯一的必备产物是 Nonoka 的 `transcript.json`。引擎自己的产物是私有附件：键为 `<引擎>.<名字>`（如 `finesub.stable_json`），核心只登记、存放、转交，从不解析。

两层互相独立：纠错层只依赖转写层的**契约**（`nonoka_x.transcription` 与 `model.py`），不依赖任何转写引擎的实现；转写层不 import 纠错层（`tests/test_transcription_contract.py` 看住两个方向）。

## 2. 转写层（`src/nonoka_x/transcription/`）

### 2.1 契约

| 类型 | 内容 |
| :--- | :--- |
| `EngineSpec` | `id`（小写字母、数字与 `-`）、`label`、`kind`、`features`、`keys`（要的密钥名）、`environment`、`billing`；`provider` 由 `kind` 推出（`cloud-service` → `cloud`，其余 → `local`） |
| `EngineFeatures` | `speakers`、`word_timestamps`、`separate`、`diarization_levels`、`languages`（空表示不限） |
| `EngineEnvironment` | `components`、`models`（必需）、`optional_components`、`optional_models`、`gpu`（`none` / `optional` / `required`）。组件 ID 见第 5 节 |
| `TranscriptionEngine` | `spec`、`readiness(context) -> Readiness`、`transcribe(job, report) -> TranscriptionResult` |
| `TranscriptionJob` | 源文件、语言、任务目录、缓存目录、目标阶段、引擎选项 |
| `TranscriptionResult` | `transcript_json` 路径、`native` 附件表、`metadata` |
| `Reporter` | 进度、事件、日志的回报通道；取消也问它（`report.cancelled()`） |

所有跨边界的类型都能经 JSON 往返（`to_dict` / `from_dict`）：同一套协议既给在 worker 进程里跑的引擎用，也给跑在独立进程里的包用（核心给包生成实现同一协议的代理对象，`engine_host`）。

### 2.2 注册的引擎

| ID | 名称 | `kind` | 在哪跑 | 要什么 |
| :--- | :--- | :--- | :--- | :--- |
| `finesub` | FineSub | `local-gpu` | 转写包 `engines/finesub-transcribe/` 的独立进程，沙箱里 | Python 环境层、模型层、工具层；显卡可选（没有只是警告）；`HF_TOKEN` 可选 |
| `moss-local` | MOSS 0.9B | `local-gpu` | Python 环境层子进程（或当前已带 ASR 依赖的进程），核心自带 | Python 环境层、MOSS 模型层（`models-moss`）、工具层；必须有 NVIDIA RTX 30 系及以上显卡（Compute Capability ≥ 8.0，约 8 GB 显存） |
| `mai2` | MAI-Transcribe-2 | `cloud-api` | worker 进程内（核心自带） | 工具层的 ffmpeg / ffprobe；`OPENROUTER_STT_KEY`，或 `AZURE_SPEECH_KEY` 加 `AZURE_SPEECH_ENDPOINT` |
| `gemini-transcribe` | Gemini 3.5 Transcribe | `cloud-api` | 同上 | 工具层；`GEMINI_FREE` 池里至少一个免费 Key |
| `moss-pro` | MOSS Pro | `cloud-api` | 同上 | 工具层；`MOSS_PRO` 池里至少一个 Key |
| `finesub-cloud` | 云端 FineSub | `cloud-service` | Nonoka Cloud（任务交给 Cloud Provider） | 已登录且有额度；桌面端列出，sidecar 不注册 |

- 三个云 API 引擎由 Nonoka 维护，共用 `engines/cloud.py`：ffprobe 取时长 → 调提供方（每次调用的原始回答缓存在任务的缓存目录，重跑不花额度）→ `lines.py` 断行 → 写 `transcript.json`。它们只在启动器环境里跑，不 import FineSub、torch、numpy。首次上传前会在任务日志里说明音频将离开本机。
- 可选增强模块（`transcription/enhance/`：Silero 空档重问、声纹重连、断句、强制对齐等）暂不启用；本地 MOSS（`engines/moss_local/`）暂不注册。
- 用户在设置页全局选定一个已就绪的转写引擎（未配置或未就绪的引擎在配置完凭据/环境前不可选中），新建任务时从全局设置填入请求；重试与续跑沿用请求里记的引擎（换引擎等于新任务）。

### 2.3 请求

`TaskRequest.transcription = {"engine": "<ID>", "options": {}}`，缺省视为 `finesub`，`engine=finesub` 时规范化结果与不带此字段完全相同。引擎用不上的字段被丢掉并在任务日志里记一条（不用 GPU 的丢 `device`、`gpu_tier`、`gpu_budget_gb`，不做人声分离的丢 `separate`、`vocal_profile`）；`diarization.level` 或 `language` 超出引擎声明的以 `invalid_request` 拒绝；不在注册表里的引擎以 `unsupported_engine` 拒绝。完整规则见 [provider-spec.md](provider-spec.md) 4.2。

### 2.4 `transcript.json`

```json
{
  "schema": 1,
  "engine": "mai2",
  "model": "mai-transcribe-2",
  "language": "ja",
  "duration": 3600.0,
  "lines": [
    {"id": "1", "start": 1.0, "end": 2.4, "text": "…", "speaker": "S1",
     "words": [{"text": "…", "start": 1.0, "end": 1.3}]}
  ],
  "metadata": {"via": "openrouter", "log": []}
}
```

`engine`、`language`、`duration`、`lines` 必填；行 `id` 非空且唯一，`0 ≤ start ≤ end`，`speaker` 若有须非空，`words` 每项 `start ≤ end`；行可重叠、不强制排序；未知字段保留并原样写回（`nonoka_x.transcription.model`）。

- 云 API 引擎：一行一个 `lines.py` 建出的行，`speaker` 是提供方自己的标签（缝合后的形式，如 `S0`、`w0:S1`；没有时为 `unknown`）。
- FineSub：由转写包从 stable 投影出来（只读 stable，不改它），一段一行、**不带说话人**——它的说话人只经纠错包进 `correction.json`。

## 3. 纠错层（`src/nonoka_x/correction/`）

| 类型 | 内容 |
| :--- | :--- |
| `CorrectionSpec` | `id`（目前只有 `finesub`）、`label`、`environment`（finesub correction 不要任何层） |
| `CorrectionEngine` | `spec`、`readiness(context, request)`、`run(job, report) -> CorrectionResult` |
| `CorrectionJob` | 转写结果（`TranscriptionResult`）、源媒体、目标（`raw-srt` / `translated-srt` / `final-srt`）、任务目录、纠错选项、知识库选项、说话人级别、标题等 |
| `CorrectionResult` | 产物清单（键名与 `ArtifactManifest` 相同） |

唯一的实现是 **finesub correction**（纠错包 `engines/finesub-correct/`）：

- 附件里有 `finesub.stable_json` 时**原样使用**那份 stable（连同 `finesub.diar_json`），一个字节都不改写，说话人来自声学时间线——FineSub 转写接 FineSub 纠错的输入与重构前相同。
- 没有时（云 API 引擎的结果），把 `transcript.json` 的行转成 stable，`speaker` 写成行标签，调 `run_post_stable` 时指明说话人来自行标签、裁判听源混音。
- 每个目标都写 `correction.json`（`raw-srt` 时是 `stable` 模式），供 Projector 与云端媒体库使用；产物对不齐时不写，而是由 worker 在清单顶层写 `projection_error`。格式与对应规则见 [provider-spec.md](provider-spec.md) 7.3、7.4。
- 只纠错、不翻译（中文源）、知识库读写、视频多模态、说话人裁判都在这一层；要不要知识库由纠错包回答，provider 与 worker 调同一个判断。

就绪：`harness`（从转写结果往后）与 `final-srt`、`knowledge`、`video-multimodal` 各档由纠错引擎回答——启动器有没有它声明的模块、工具层有没有 ffmpeg / ffprobe、LLM 目标有没有配好的模型。

## 4. 引擎包

### 4.1 四个包

| 包 | `kind` | 解释器 | 入口模块 | 要的密钥 |
| :--- | :--- | :--- | :--- | :--- |
| `finesub-transcribe` | `transcription` | `python-layer`（Python 环境层） | `finesub_transcribe` | `HF_TOKEN` |
| `finesub-correct` | `correction` | `launcher`（启动器） | `finesub_correct` | LLM 与检索的 Key、`NONOKA_CLOUD_KEY` |
| `finesub-knowledge` | `knowledge` | `launcher` | `finesub_knowledge_host` | 无 |
| `flowocr` | `ocr` | `python-layer`（`screen-text` 可选层） | `flowocr_host` | 无（严禁注入任何 LLM 密钥） |

每个包的形状：`PACKAGE.json`、`adapter/`（包自己的代码：协议入口、桥接、产物转换）、`src/`（FineSub 文件，逐字节等于分包前的上锁文件）、许可文件、`UPSTREAM.json`、`QUALITY_LOCK.json`、`LOCAL_CHANGES.md`；没有 `pyproject.toml`。详见 [engine.md](engine.md) 2.1。

### 4.2 `PACKAGE.json`

| 字段 | 说明 |
| :--- | :--- |
| `schema` | 固定为 1 |
| `id`、`label` | 包的标识（也是它私有目录与附件目录的名字）与显示名 |
| `kind` | `transcription` / `correction` / `knowledge` / `ocr`（`ocr` 包须声明 `ocr: {layer: "screen-text"}`，未知 `kind` 直接报错，绝不回退到 `correction`） |
| `entry`、`paths` | 入口模块；导入根（都是 `["adapter", "src"]`），相对包目录 |
| `interpreter` | `python-layer` 或 `launcher` |
| `secrets` | 包要的密钥名；核心只把这些交给它的进程 |
| `upstream` | 与 `UPSTREAM.json` 相同；`capabilities().engine` 由它得出 |
| `engine` | 包提供的引擎：转写包是完整的 `EngineSpec`（ID `finesub`），纠错包是 `CorrectionSpec`（ID `finesub`，不要任何层） |
| `readiness` | 就绪声明。转写包：`profile=managed-runtime`、支持的平台、Python 环境层里必须能 import 的模块；纠错包：`profile=launcher`、启动器要有的模块、哪些目标要 LLM |
| `gpu_tiers` | 转写包接受的显卡档位 |
| `caches` | 转写包在大文件根 `cache/<包>/` 下的编译缓存（`numba`） |

核心不启动进程就能知道的一切都来自这里：注册哪些引擎、它们的能力、就绪怎么判断、给哪些密钥。

### 4.3 发现与可删性

`nonoka_x.engine_host.discover()` 扫描引擎根目录（桌面在资源目录或工作目录下找 `engines/`，`NONOKA_ENGINES_DIR` 可指定，经 sidecar 的 `--engines` 传入；`NONOKA_X_ENGINES` 可覆盖，供测试与另一个检出用），有哪个包就注册哪个，目录不在也照常启动。删掉包的目录就删掉了它的引擎（`tests/test_engine_packages.py` 覆盖三种删法）：

| 删掉 | 结果 |
| :--- | :--- |
| 转写包 | 本机 FineSub 转写不出现；云 API 引擎、云端 FineSub 与 FineSub 纠错照常 |
| 纠错包 | 转写照常，原始字幕由核心渲染；纠错翻译各档报 `missing_engine` |
| 知识库包 | 读写知识库或要用知识库做 ASR 上下文的请求被就绪检查拦下（`missing_knowledge_package`），不静默当作关闭；纠错与翻译也被拦下，因为纠错要从知识库读取风格目录（`--style auto`，知识库关着也一样）；只到原始字幕的任务照常 |
| 转写与纠错两个包 | 核心照常启动；云端 FineSub 与云 API 引擎的任务能出到原始字幕 |

界面不提供卸载；删除指代码层面可分离、打包时可以不带。

### 4.4 进程协议

核心从不 import 包里的代码。`engine_host.PackageProcess` 用包声明的解释器拉起入口模块，两个方向都是一行一个 JSON：

- 核心 → 包：先发 `{"context": ...}`（启动上下文），之后每个请求一行 `{"op": ..., ...}`；
- 包 → 核心：`{"type": "reporter", "method", "args", "kwargs"}`（调用任务唯一的那个 reporter，所以两半都说的警告只出现一次）、`{"type": "event", "event"}`（原样转发的 worker 事件），每个请求以 `{"type": "result", "payload"}` 或 `{"type": "error", "payload"}` 结束。

各包回答的操作：

| 包 | 操作 |
| :--- | :--- |
| 转写包 | `transcribe`（跑到 stable 并写 `transcript.json`）、`transcript_json`（从已有的 stable 写 `transcript.json`，一次性转换用）、`knowledge`（告诉它知识库进程的地址） |
| 纠错包 | `begin`、`check`、`run`、`axis`（日文轴）、`describe`（投影时为 FineSub 自己的 stable 写 `stable` 模式的 `correction.json`）、`correction_json`（一次性转换用）、`catalog`、`product_rows`、`task_mode`、`knowledge`；sidecar 里常驻一个纠错进程，回答模型路由与目录这类要问 FineSub 的事 |
| 知识库包 | `prepare_subjects`、`serve`（成为本任务的知识库进程，回答连接地址）、`manage`（知识库页的列表、读、存） |
| 文字识别包（`flowocr`） | `check`（检查驱动/显卡/模型与派生图标记）、`recognize`（识别视频文字并输出归一化的 `ocr.json`）、`inpaint`（按底板 ASS 用 LaMa 抹除画面原字并输出视频中间成片 `cleaned.mp4`） |

一个任务读写知识库时，worker 为它起**一个**知识库进程，地址写进之后每个包进程的启动上下文；转写与纠错两半都经它访问知识库，只有知识库包碰知识库数据。

### 4.5 启动上下文

`nonoka_x.engine_contexts` 给每个进程一份中性的启动上下文，桥接（各包 `adapter/*/bridge.py`）在包的进程里、import `finesub` 之前把它变成 FineSub 的 `finesub.toml`、模型目录文件和 `FINESUB_*` 变量：

- 所有包：`paths.package_data`（`engines/<包>/`）、`paths.run`（`engines/<包>/run/<进程>/`，派生文件放这里，进程结束即删，启动时清残留）、`paths.state_file`（限流状态，在纠错包目录）、`paths.knowledge_root`（知识库，在知识库包目录）、`runtime.tools`（工具层里 ffmpeg、ffprobe、tokcount 的目录）；
- 转写包：`runtime.models`（模型层当前版本）、`runtime.cache`、`runtime.python`，它声明的密钥，LLM 路由，代理；
- 纠错包：它声明的密钥，LLM 路由、端点与钉住的模型，代理，`agent_cli.path`（本机探测到的本地 Agent CLI 的目录）；
- 知识库包：只有路径。
- 文字识别包（`ocr`）：不调 `engine_secrets`，不传入任何 API Key；`runtime` 填入 `screen-text` 层的 `python.exe`、`models\`、工具层 `tools`、缓存目录 `runtime/cache/flowocr` 以及游戏剧本目录 `engines/flowocr/gametext`。

## 5. 运行环境

五层预制包装在大文件根 `runtime/layers/<层>/<版本>/`，由层管理器（`src/nonoka_x/runtime/`）从 R2 下载（或由用户在界面上按层选择与 R2 完全一致的本地 `.tar.zst` 离线包导入）、核对、切换：

| 层 | 内容 | 谁要 |
| :--- | :--- | :--- |
| `python` | python-build-standalone 3.12.1，锁里的全部包装进解释器自己的 `site-packages`（不建 venv），`.py` 全部预编译 | FineSub 转写、MOSS 0.9B |
| `models` | 模型清单里 FineSub 默认路径要的 5 个模型（不含编译加速产物） | FineSub 转写 |
| `models-moss` | `moss-transcribe-diarize` 与裁剪后的 `speaker-embedding`（`wespeaker-cpu.ts` + `plda/*`，约 26 MB） | MOSS 0.9B |
| `tools` | ffmpeg、ffprobe、tokcount、yt-dlp、aria2c、node、pot-provider | 所有引擎；桌面自己的导出与下载器 |
| `screen-text` | 可选层（仅 Windows）：独立 Python 3.12 + FlowOCR 依赖（ORT-GPU/CUDA 13/cuDNN 9.13/OpenCV）+ PP-OCRv6 与 LaMa 模型及预生成派生图 | 文字识别层（编辑器「文字识别」功能，`flowocr` 包） |

引擎在 `EngineEnvironment` 里用组件 ID 声明要什么（`nonoka_x.runtime`）：`asr-runtime` = Python 环境层；`media` = 工具层的 ffmpeg 加 ffprobe；单个工具按它自己的 ID；层按 `python` / `models` / `models-moss` / `tools`；模型按模型清单里的 ID（`separator`、`whisper`、`qwen-referee`、`moss-transcribe-diarize`，可选的 `nemotron3`、`speaker-embedding`）。就绪 = 要的组件在、当前版本与清单相符；**工具只认工具层**，用户 PATH 上的 ffmpeg 不算，核心自己的 `media` 档也一样。工具层与 Python 环境层属于默认基础层，安装后支持更新与完整核对、不提供整层删除；`models` 与 `models-moss` 两个模型层均支持按需下载、按层离线安装与单层删除（`DELETE /v1/runtime/provision?layer=models|models-moss`）；存在旧版本残留时可在设置页一键清理。

FineSub 引擎的代码不进层，随桌面安装包下发，与核心同步升级。

## 6. 密闭与沙箱

- 包进程的环境从零构建：每个变量要么是按名从父进程带的系统必需项，要么由 Nonoka 无条件写入（`engine_host.CLASSIFICATION` 加各包桥接里的分类表），其余一律不存在。解释器 `-I` 启动（转写进程另加 `-B`），`PATH` 只有工具层与系统目录（纠错进程另加本地 Agent CLI 的目录，脚本 CLI 再加 node 的目录）。代理总是显式写。
- 所以机器上的外部环境变量（代理、HF 镜像、`CUDA_VISIBLE_DEVICES`、线程数……）和外部配置文件不再影响引擎；要生效只能先成为 Nonoka 的设置。
- 转写与文字识别进程（`kind ∈ {transcription, ocr}`）都在沙箱里：用户目录指向各自包的 `sandbox/home/`，临时目录指向 `sandbox/temp/<进程>/`（每进程一份，结束即删），当前目录是任务目录，`HF_HUB_OFFLINE=1`，缺模型时报错而不是下载。`ocr` 进程额外写入 `PYTHONDONTWRITEBYTECODE=1`（防止子进程向只读层写 `.pyc`），且在启动子进程前将适配器绑定到带 `KILL_ON_JOB_CLOSE` 的 Windows Job Object，防止监督者/ORT/解码子进程残留。`cuda:N` 时由适配器设 `CUDA_VISIBLE_DEVICES`。
- 纠错与知识库进程不进沙箱，用真实的用户目录；本地 Agent CLI 的配置与证书变量只带给纠错进程。
- 桌面关闭全部编译加速（`FINESUB_SEPARATOR_ACCEL=0`、`FINESUB_REFEREE_ACCEL=0`）：人声分离与 qwen 裁判都走 eager。

逐项的环境变量分类与证明办法见 [engine.md](engine.md) 2.1a。

## 7. 云端 FineSub

- Modal 容器（云端仓库 `modal_compute/`）只装转写包，跑到 stable，在容器里由转写包适配器的同一个投影函数转出 `transcript.json`。容器的运行环境按云端自己的方式装（Linux，不用预制层），加速由容器按输入自选，因此闸门 `--cloud` 按结构与统计指标比对（段数差、文本编辑距离、说话人数与覆盖率）。缺模型时任务报错，模型由部署前的预热脚本放进 Volume。
- 作业结果：`transcript_json` 加不透明附件 `finesub.stable_json`、`finesub.diar_json`（键名与本机 `native` 相同）；Worker 只存、只发，不解析。
- 桌面 relay 取回后以 `cloud_transcript` 交本机续跑；纠错包拿到的 stable 就是附件原件。两个 FineSub 包都不在时，云端任务出到原始字幕为止。
- LLM 转发（Nonoka Cloud 模型）与云端识别无关，纠错包照旧经 `nonoka-cloud` 路由调用。

## 8. 再加一个引擎

**云 API 转写引擎**（在核心里，像 MAI2）：

1. 在 `src/nonoka_x/transcription/engines/` 写后端与 `TranscriptionEngine`（多半继承 `cloud.CloudEngine`），`EngineSpec` 声明能力、`keys`、`environment=EngineEnvironment(components=("media",))`；
2. 在 `transcription/keys.py` 与配置门面登记密钥规格（只给自己的引擎用，不派生给 FineSub）；
3. 把 `模块:类` 加进 `worker.TRANSCRIPTION_ENGINES`（worker 与 `local_provider` 都经 `worker.build_engines` 建注册表）；设置页按 `keys` 列输入框；
4. 测试：契约（`tests/test_transcription_contract.py`）、引擎自己的回放测试；`transcript.json` 交给 finesub correction 要能走到 `final-srt`。

**引擎包**（独立进程，像 FineSub）：在 `engines/<id>/` 放 `PACKAGE.json`、`adapter/` 与代码，入口模块按第 4.4 节的协议回答 `transcribe`（转写包）或 `check` / `run`（纠错包）；需要的运行环境用组件 ID 声明；交出 `transcript.json`（或 `correction.json`），自己的产物放进 `native/<包>/`。核心不需要改任何一行就能发现它；要不要进沙箱、密钥给哪些，由 `engine_host` 按包的种类与解释器决定。

纠错层目前只有一个实现，界面不提供选择；注册表是为了让边界清楚、能被测试约束。

## 9. 由什么看住

| 约束 | 测试 |
| :--- | :--- |
| 两层互不依赖、契约可往返 | `tests/test_transcription_contract.py` |
| 核心不 import FineSub、不出现 `FINESUB_`、不读写 FineSub 文件格式 | `tests/test_finesub_boundary.py` |
| 包的形状、文件去向、兼容包名单、互相 import 不到、可删性 | `tests/test_engine_packages.py` |
| 包里的 FineSub 文件没被动过 | `tests/test_finesub_quality_lock.py` |
| 运行环境的钉住内容 | `tests/test_runtime_pins.py`、闸门的「预制层」一项 |
| 密闭与沙箱 | `tests/test_engine_hermetic.py`、`tests/test_engine_sandbox.py`、闸门 `--real` 的 `sandbox` 一项 |
| 新旧投影一致、媒体库往返 | `tests/test_projection_equivalence.py`、`tests/test_library_roundtrip.py` |
| FineSub 路径的结果不变 | `scripts/quality_gate.py`（默认、`--real`、`--layers`、`--cloud`），质量红线见 [engine.md](engine.md) 第 1 节 |
