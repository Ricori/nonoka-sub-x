# 转写引擎重构计划

> 状态（2026-11-18）：已完成并合进 main 的工作包见第 6 节开头的「已完成」列表，它们的交付摘要与原始说明在 `docs/transcription-engines-plan-done.md`（本文只留标题）。第九批 WP-H、WP-L 已于 2026-11-18 合进两个仓库的 main。第十批 WP-CL、WP-M2、WP7 已合进 main（WP-CL 也合进云端仓库，2026-11-18 至 19）。按第 92 问的精简方案，`--real`、`--cloud` 与线上实测集中到 WP8 各跑一次。WP-UI 已于 2026-11-19 合进 main（界面由用户自验）。只剩 WP8，在主会话里做（第 95 问）；2026-11-19 做了静态核对后按用户要求停下，进度与接手要点在主会话笔记目录的 `wp8-progress.md`。WP-M1 的最后一条完成标准先不做（第 70 问）。用户做过的决定都在第 4 节和附录 B，按问题编号查。本文档是交给子代理执行的工作说明，每个工作包可以单独领走。
> 写于 2026-11-10，基于 `main@1f14db3`、FineSub `v0.7.0@7123055`。
> **2026-11-11 整理成干净版本**（用户决定，附录 B 第 1 问）：现行决定直接写在正文里，不再有「以后者为准」的追加条款。每个决定的来历、被推翻的旧说法，以及旧条款编号到新编号的对照，见附录 A；向用户问过的问题和答复见附录 B；还没定的事见附录 C。整理前的版本在 git 历史里（`main` 上的 `docs/transcription-engines-plan.md`）。
> 相关文档：`docs/runtime-and-config.md`（WP-R0 的调查与设计答复 D1–D23，下称「R0 文档」；其中被本计划取代的答复已在那边标注，对照见附录 A.3）；`docs/engine-sandbox-experiment.md`（预制环境包与进程级沙箱两个前提的实验报告，下称「实验报告」）。本文档与 R0 文档冲突时以本文档为准。

## 0. 质量红线（高于本文档其余一切）

**FineSub 原先的转写、纠错、翻译质量必须保住。** 编排可以拆、可以重组，但结果不能变。

落到可检查的规则：

1. **算法文件上锁。** `third_party/finesub/src/finesub/` 下默认全部文件不许改，包括识别、分离、稳定化、说话人分离、纠错翻译的全部 LLM 代码、提示词模板、配置默认值。分包之后（WP-P1）这些文件搬进两个引擎包，锁跟着走：包里每个 FineSub 文件的哈希等于搬之前的值。运行时锁（`pylock.*.toml`）、运行时清单、模型清单**按内容**上锁：钉住的每一项记在 `src/nonoka_x/resources/RUNTIME_PINS.json`，由 `tests/test_runtime_pins.py` 看住，放在哪个文件里都一样。
2. **只解锁四处**，它们是编排、入口和安装设施，不是算法（另有一处按第 0.1 节第 5 条批准：知识库的访问点，由 WP-K 改）：
   - `finesub/stages.py`（`run_pipeline` 所在，要拆，WP-B）
   - `finesub/pipeline.py`（命令行前端，跟着删选项，WP-B）
   - `finesub/speech/joint/`（联合 ASR 后端，整个迁走，WP1）
   - `finesub_bootstrap/`（安装与运行时设施）。它的去向（4.2 第 16 条）：转写包**原样**带着它，唯一的改动是删掉「缺模型时自己下载」（WP-R7）；纠错包不用它，改用只含纠错所需名字的兼容包（WP-P1）；Nonoka 自己的代码不再 import 它。
3. **解锁的地方改完，FineSub 路径的结果必须和改之前一致**，由回归闸门证明（WP-Q）。「一致」默认是逐字节；WP-Q 用未改动的代码连跑两次查明以下产物本身就不能逐字节复现，用户 2026-11-11 批准按下面的标准比：
   - `-stable.json`、`-aligned.json`：顶层 `metadata` 以外逐项相同（`metadata` 里是各阶段的耗时）；
   - `-vocal.ogg`：解码出的采样相同（Ogg 流序列号是随机的）；
   - `-vad.json`：去掉 `source.mtime_ns`（人声文件每次重写）、顶层 `timing` 和所有以 `_sec` 结尾的耗时字段后逐项相同。
   其余产物（`-raw.srt`、`-diar.json`、`-diar-rows.json`、`-diar-evidence.npz`、`-vad-energy.npz`、LLM 请求序列、`final.srt`、`annotated.csv`）仍是逐字节。工作包里写「与基线一致」的地方，一律指这条标准。云端用另一套较弱的标准（第 0.1 节第 3 条）。
4. **Nonoka 传给 FineSub 的参数和环境，重构前后等价。** 同一个任务请求最终传进去的每个参数、环境变量、生成的 `finesub.toml` 和模型目录都和现在一样。这里的「现在」指**干净环境下的现在**：机器上没有任何会影响 FineSub 的外部环境变量和外部配置文件时的行为。引擎改成密闭运行之后（4.5），所有机器都得到这个行为。已批准的例外见第 0.1 节。
5. **运行环境的内容等价。** 运行环境改成 Nonoka 构建、托管的预制包（4.2），装到用户机器上的东西必须与 `RUNTIME_PINS.json` 一致：Python 解释器的构建与版本，Python 包的名称、版本、wheel 哈希，以及每个包装出的文件与 wheel 的 `RECORD` 相符；模型的 revision 与文件哈希；工具的版本与哈希；决定加载哪个模型的常量。证明办法是**按文件清单比哈希**：构建流水线给每一层产出逐文件清单，闸门用它对照 `RUNTIME_PINS.json`，层管理器在用户机器上用同一份清单核对（4.2 第 19 条，WP-Q 表里的「预制层」一项）。改任何一个钉住的版本都属于第 7 条。目录可以改名和搬动，文件内容不能变。
6. **闸门不过就停下来报告**，不许为了过闸门去调参数、改算法文件或放宽比对标准。
7. 要动上锁的文件或钉住的版本（包括升级上游），是单独的、需要用户明确批准的事。重录闸门基线（`scripts/quality_gate.py --record`）同样要用户批准，并单独提交。

子代理如果发现某个工作包不动上锁文件就做不成，停下来交回，不要自己找变通。

### 0.1 用户批准过的偏离

以下几处是用户明确批准、有意改变结果或放宽标准的地方。除此之外，第 0 节的规则没有例外。

1. **桌面端关闭全部编译加速**（2026-11-11，提交 `85ae2b9`）：人声分离的 AOTI 与 JIT、qwen 裁判的编译步骤都关。`settings.bind_environment` 给每个 worker 设 `FINESUB_SEPARATOR_ACCEL=0`、`FINESUB_REFEREE_ACCEL=0`。环境基线（`tests/fixtures/quality/environment.json`，只多这两个变量）和真实样本基线（`test_data/quality-baseline/real/`，改在 eager 下重录）已按批准重录；旧的 AOTI 基线备份在 `test_data/quality-baseline/real-aoti-2026-11-11/`。用户知道并接受：多人样本关闭加速后识别文字有变化。理由：加速路径的结果与 eager 不同，而且取决于机器、输入长度和装没装编译器（实验报告 1.2.2）。由此 MSVC 不再是运行依赖，AOTI 产物不进模型层。
2. **锁里去掉 `onnx`**（2026-11-11，附录 B 第 4 问）：`onnx` 与 `onnx-weekly` 往同一个 `onnx/` 目录写文件，装出来的结果取决于安装顺序（2.9）。去掉 `onnx`，留 `onnx-weekly`。由 WP-R1 单独提交，`RUNTIME_PINS.json` 从 98 个包变成 97 个。
3. **云端只比结构和统计**（2026-11-11，附录 B 第 5 问）：Modal 容器按第一个输入的时长自行选择加速（2.10），云端结果不能逐次复现，所以 `--cloud` 不用第 0 节第 3 条，只比产物齐全、格式合法、段数、文字距离、说话人数等统计量是否在阈值内。阈值由 WP-C 实测后提案，**用户批准后**才写进闸门。
4. **环境基线随密钥交付方式重录一次**（R0 文档 D19）：密钥改走进程环境变量后，`environment.json` 里 `FINESUB_ENV_FILE` 的内容必然对不上，WP-R5 按新方式重录，单独提交。
5. **知识库的访问点可以改**（2026-11-12，附录 B 第 37–40 问）：知识库独立成第三个包（4.6 第 35a 条），转写和纠错两半对知识库的**直接访问**（读条目、匹配、写回知识更新、加写锁等）改成经知识库包的协议调用。批准范围只限访问方式：选哪些条目、怎么匹配、怎么渲染、提示词、知识更新的内容一律不变。涉及的上锁文件包括 `stages.build_asr_context`（WP-B 之后已冻结）和纠错那一半读写知识库的模块，精确清单由 WP-P0 第 10 问列出，由 WP-K 实施。标准不降：发给 LLM 的请求序列、ASR 上下文文本、知识更新写出的内容都与基线逐字节一致。`QUALITY_LOCK.json` 只在 WP-K 的专门提交里更新，提交信息写明本条。

## 1. 目标

**最终目标（用户 2026-11-11）：FineSub 完全独立，成为两个可插拔的部件——「FineSub 转写引擎」和「finesub correction」。FineSub 转写引擎与其他转写引擎地位相同，随时可以替换；finesub correction 接任何转写引擎的结果。** 下面各条都服从这个目标。

1. **去掉整套上游对齐机制。** 不再用同步脚本、补丁栈、「可由基线加补丁重建」来约束 FineSub 的代码。以后升级上游时只跟三块关键算法：转写引擎、纠错层、翻译层（第 3 节）。（WP-A 已完成。）
2. **转写引擎是可选的一等概念**，FineSub 只是其中一个，没有特权：worker、provider、就绪检查、界面里没有「FineSub 专属」的分支，它和别的引擎走同一个注册表、同一套 `EngineSpec`、同一个 `transcript.json` 出口。
3. **MAI-Transcribe-2、Gemini 3.5 Transcribe、MOSS Pro 由 Nonoka X 自己维护**：API 客户端、拼窗、断行、密钥、配额、缓存、就绪检查和运行环境都归 Nonoka，不再是 FineSub 的 `--asr` 后端。
4. **纠错翻译继续用 FineSub 的，封装成 finesub correction**，和 FineSub 转写引擎互不依赖：两者各自声明运行环境，各自就绪检查；只用其中一个时不需要另一个的任何东西。
5. **彻底隔离**：FineSub 分装成两个引擎包（转写包、纠错翻译包）和一个知识库包（4.6 第 35a 条），随时可以删掉其中任何一个或几个而不影响 Nonoka 运行；Nonoka 核心对 FineSub 零引用；包的产物由包转换成 Nonoka 的格式后交给核心保存（4.6）。分包之前的过渡形态见第 5 节。
6. **运行环境由 Nonoka 构建和托管**：FineSub 转写引擎的 Python 环境（连同 Python 解释器）、模型、工具做成预制环境包，由 Nonoka 的构建流水线产出、托管在 Cloudflare R2，用户机器下载解压即用，不再在用户机器上用 uv 逐个装包、去 Hugging Face 下模型，也不再用用户机器上的系统 Python（4.2）。配置和密钥的存储也归 Nonoka（4.3）。
7. **FineSub 的代码只留核心文件**：引擎源码、许可证文件、`UPSTREAM.json`、质量锁、`LOCAL_CHANGES.md`。分包后 `third_party/finesub/` 不再存在，两个引擎包是 FineSub 代码唯一的家。
8. **数据目录重新组织**：所有数据目录都可以改名和搬动；FineSub 相关的文件不再散落在主数据目录里（只在包的私有目录 `engines\<包>\` 下）。**旧版用户由迁移工具搬到新布局**，随第一个采用新布局的版本发布（4.4）。
9. **引擎密闭运行**：转写引擎在进程级沙箱里跑，有自己的用户目录和临时目录；两个引擎包的进程环境都从零构建、解释器隔离启动。机器上的环境变量、用户目录或当前目录里的配置文件都不能改变引擎的行为；要影响它，只能通过 Nonoka 的设置和任务请求（4.5）。纠错引擎不进沙箱，因为本地 Agent CLI 要用真实的用户目录。
10. **桌面端不用编译加速**（第 0.1 节第 1 条）。

不在本次范围：改 FineSub 的算法、升级 FineSub 版本、改 EditDocument 模型、改云端 Modal 容器的加速选择（2.10）。云端的调度由 WP-CL 重做（4.7）。

## 2. 已核实的事实

执行前不用再查，直接当前提用。行号以 `main@1f14db3` 为准，WP-R0 的更正已经并进来。

### 2.1 管线与边界

| 事实 | 位置 |
|---|---|
| FineSub 以 `stable.json` 为界：前半段产出它，后半段 `run_correction_task(CorrectionRequest)` 只读它 | `third_party/finesub/src/finesub/stages.py:474` |
| 两半的编排都在 `run_pipeline` 一个函数里；`stages.py` 共 1850 行，是胶水 | `stages.py:1087` |
| `run_pipeline` 跳过产物已在盘上的阶段；云端流程一直是先跑到 `stable`、桌面再接着跑 | `nonoka-sub-cloud/modal_compute/nonoka_x_compute/phases.py:659`、`src/nonoka_x/worker.py:345` |
| `run_pipeline` 有 `asr` 参数，`None` 时读 `[asr] backend` 配置，再回落 `local`。Nonoka 从不传它 | `stages.py:1099`、`worker.py:630` |
| 联合 ASR 的任务：不跑分离/识别/声学说话人分离，说话人取每行的 `line_speaker` 写成 `-diar-rows.json`，说话人裁判听源混音 | `stages.py:1270`、`:819`、`:516`、`speech/diarization/stage.py:424` |
| `speech/joint/` 之外真正 import 它的只有 `stages.py` 和 `pipeline.py`。`api_keys.py`、`settings.py`、`speech/runtime/device.py`、`resources.py` 里只是提到它的名字或留着给它用的配置项 | 全仓 grep |
| Nonoka 另有一条只跑后半段的路径：导入日文轴 | `src/nonoka_x/axis.py:145` |
| 续跑类任务只查 LLM 那一档就绪，不查 GPU/运行时/模型 | `src/nonoka_x/local_provider.py:1113` |
| worker 用托管运行时的 Python，没装就用启动器自己的 | `local_provider.py:922` |
| 启动器环境只有 httpx、pydantic、opencc，足够跑后半段 | `pyproject.toml` |
| 界面上本机/云端的选择已经叫「转写引擎」 | `desktop/frontend/src/components/TranscriptionDialog.tsx:612` |
| 桌面提交云端任务时按白名单组字段，新字段不会透传 | `desktop/internal/cloud/service.go:481` |
| FineSub 是 GPL-3.0-or-later（提示词模板 CC-BY-SA-4.0），Nonoka X 是 GPL-3.0，直接修改和搬移在许可上可行，声明文件要保留。预制包里各第三方组件的再分发许可，用户 2026-11-11 已核对没有问题 | `third_party/finesub/LICENSES.json`、`LICENSE` |
| Modal 上没有 LLM 容器（镜像只有 control、vocal、gpu_worker、scheduler），云端仓库里也没有任何地方调 `axis.translate_axis`；纠错翻译和日文轴翻译都在桌面跑 | 云端仓库全仓 grep |

### 2.2 `speech/joint/`（约 5300 行）的依赖

- **纯 Python**：`model.py`、`stitch.py`、`cache.py`、`quota.py`、`repair.py` 的文本对齐，三个云后端的请求与解析，Gemini 的时间修复/复读检测/续问，MOSS Pro 的输出上限续问与错文种重问。
- **断行 `lines.py` 调用 `speech/postprocessing/segmentation.py` 的句子 DP**。后者只 import 标准库和 FineSub 的纯 Python 模块（`settings`、`subtitles.metrics`、`text`、`recognition.segments`）。
- **需要 Python 环境层与模型**：`SpeechSpans`（Silero，空档重问用）、`relink.py`（声纹重连说话人）、`sbd.py`（断句模型，ONNX，import `onnx`）、`aligner.py`（强制对齐，要 GPU）、`moss_timing.py`（用 Silero 帧概率给 MOSS 段内定时）、`live.py`（Gemini Live 修补）、`moss_local/`（本地 MOSS，要 GPU）。

### 2.3 已拆掉的上游对齐机制（WP-A，已完成）

同步脚本、补丁栈、`FILES.json`、补丁栈测试、CI 的快照重建校验都已删除，换成质量锁 `third_party/finesub/QUALITY_LOCK.json` 与 `tests/test_finesub_quality_lock.py`。8 个补丁原本就已应用在 `third_party/finesub/src` 里，它们相对上游改了什么记在 `third_party/finesub/LOCAL_CHANGES.md`。

读 `UPSTREAM.json` 的 `commit`、版本或 `engine_bundle_id` 的地方很多（worker、local_provider、桌面暂存工具、离线包脚本、云端镜像、云端媒体库），所以这个文件和这几个字段保留。`desktop/internal/app/bundled/nonoka_x/` 是构建时暂存出来的副本，不要手改。

### 2.4 运行时与配置现在怎么依赖 FineSub

`finesub_bootstrap`（约 11600 行）同时扮演三个角色：

| 角色 | 谁在用 | 具体内容 |
|---|---|---|
| **安装管理** | Nonoka：`provision.py`、`runtime_install.py`、`model_install.py`、`download_route.py`、`asset_digest.py`、`config_file.py` | 按 `pylock.win-py312*.toml` 建虚拟环境（`environment.RuntimeEnvironment`）；按 `runtime-manifest.json` 装 ffmpeg、tokcount 等工具（`ResourceManager`、`shell.resource_specs`）；按 `model-manifest.json` 下载校验模型（`model_fetch`、`model_manifest`）；断点下载、镜像路由、目录布局（`AppPaths`）、原子写、文件锁、进程树 |
| **配置与密钥** | Nonoka：`settings.py` | 密钥存在 `finesub.env`，由 `finesub_bootstrap.secrets` 加保护读写；配置写在 `finesub.toml`；模型目录写在 `model_catalog.toml`。三者加状态文件、知识库目录通过 `FINESUB_ENV_FILE`、`FINESUB_CONFIG_FILE`、`FINESUB_MODEL_CATALOG`、`FINESUB_STATE_FILE`、`FINESUB_KNOWLEDGE_ROOT` 指给引擎（`settings.py:621`） |
| **引擎运行时读取** | FineSub 自己的上锁文件：`paths.py`、`state.py`、`api_keys.py`、`speech/runtime/hf_weights.py`、`vad_asr_stage.py`、分离器、两个说话人后端、`llm/client.py` 等三十多处 | 解析目录、读密钥、读写状态、确认并在缺失时自行下载钉住版本的模型（`model_ensure`）、子进程不弹窗、token 计数器 |

要点：

- 上锁的引擎文件运行时要 import `finesub_bootstrap` 里的 **48 个名字**（清单在 `tests/fixtures/quality/bootstrap-surface.json`），分属 17 个模块，逐个调用点见 R0 文档 1.1。按对结果的影响分三类：
  - 直接决定结果：`model_ensure`（加载哪个 revision 的模型）、`model_caches` / `model_manifest`（分离器用哪个 checkpoint、whisper 用哪个仓库）、`token_counter`（token 计数，决定分窗，进而决定提示词）、`secrets`（读密钥）、`artifacts`（产物文件名常量）；
  - 基础设施：`fsops`、`locks`、`processes`、`logs`、`paths`、`http_client`、`download_routes`、`model_fetch`、`hf_verify`、`agy_records`；
  - 纯数据：两份 pylock、运行时清单、模型清单、下载源（内容已记入 `RUNTIME_PINS.json`）。
- 按调用点划分（2026-11-11 grep）：`speech/` 用 `model_fetch`、`download_routes`、`hf_verify`、`model_caches`、`model_ensure`、`model_manifest`、`paths.default_data_root`、`processes`；纠错那一半（`llm/`、`api_keys.py`、`api_http.py`、`state.py`、`batch_state.py`、`paths.py` 等）用 `secrets`、`token_counter`、`agy_records`、`artifacts`、`fsops`、`http_client`、`locks`、`logs`、`paths`、`processes`，约 35 个名字。精确归属由 WP-P0 第 4 问定。
- 解锁文件 `pipeline.py` 还 import 依赖面清单之外的 `logs.prune`、`logs.open_log`、`locks.LockUnavailable`/`holding_lock`（`pipeline.py:674`、`:691`、`:1737`）。
- 引擎留的环境变量入口：`FINESUB_MODEL_DIR`、`FINESUB_CONFIG_FILE`、`FINESUB_ENV_FILE`、`FINESUB_STATE_FILE`、`FINESUB_MODEL_CATALOG`、`FINESUB_KNOWLEDGE_ROOT`。`FINESUB_MODEL_MANIFEST`、`FINESUB_DOWNLOAD_SOURCES`、`FINESUB_HF_ENDPOINT` 是 `finesub_bootstrap` 读的；`FINESUB_KEYRING` 不是环境变量，是 `.env` 里一行的名字（`finesub_bootstrap/secrets.py:58`）。
- 引擎读密钥时**进程环境变量优先于 `.env`**，不落盘就能读到全部密钥；例外是 `GEMINI_BASE_URL` 只从 `.env` 读（R0 文档 1.2）。引擎首次读到 `.env` 里的明文时会就地加密改写它，`FINESUB_ENV_PROTECT=0` 关掉（R0 文档 1.3；实验报告 1.4 在沙箱下复测成立）。
- worker 的环境变量由 `provision.worker_environment` 组装（`provision.py:968`）；现有安装位于 `<数据目录>/finesub/{runtime,models,cache}`。
- macOS 的运行时清单已经是 Nonoka 自己的（`src/nonoka_x/resources/runtime-manifest.macos-*.json`），macOS 只有媒体工具，没有 Python 运行时（`provision.py:233`）。
- 桌面脚本 `desktop/scripts/resource-sources.mjs`、`build-offline-package.mjs`、`publish-github-mirror.mjs` 读 FineSub 的清单来准备离线包和镜像。（WP-R6 后：镜像脚本已删，离线包脚本改读 Nonoka 的清单与 `layers.json`。）
- 云端：Modal 的 GPU worker 缺模型时靠引擎自己下载进 `/models` Volume；预热脚本 `prime_diarization_models` 也调引擎自己的 `hf_weights.prepare_directory`（`nonoka-sub-cloud/modal_compute/nonoka_x_compute/operations/prime.py:168`）。云端镜像整个复制 `third_party/finesub`（`images.py:159`、`:247`、`:264`），`PYTHONPATH` 含本仓库的 `src`。

### 2.5 外部环境现在从哪里漏进引擎

| 漏点 | 位置 |
|---|---|
| worker 的环境变量以 `os.environ.copy()` 为底，再叠加 Nonoka 的设置。机器上已有的任何变量都原样传给引擎 | `provision.py:968`、`local_provider.py` 的 `_engine_environment` |
| sidecar 主进程把 `FINESUB_*` 写进自己的 `os.environ`，worker 再继承；知识库目录和状态文件用 `setdefault`，机器上已有同名变量时外部值优先 | `sidecar.py:183` → `settings.py:620-628` |
| `CUDA_VISIBLE_DEVICES`：请求指定 `cuda:N` 时启动 worker 前无条件设一次，`worker.py:593` 是第二道「外部没设才设」 | `local_provider.py:1561`、`worker.py:593` |
| 三种 worker（任务、LLM、知识库）都用普通的 `python -m` 启动，没有隔离参数，`PYTHONPATH`、用户级 site-packages、`PYTHONSTARTUP` 都生效 | `local_provider.py:921`、`:937`、`:944` |
| sidecar 主进程自己也 import FineSub：读模型路由和模型目录（会读配置，必须挪进纠错包进程）、解析说话人产物、取显卡档位选项（这两项是纯数据，零次环境读取） | `settings.py:365`、`:384`、`:476`、`:947`、`:1064`，`projector.py:224`，`gpu_tier.py:48`；R0 文档 1.9 |
| FineSub 和 `finesub_bootstrap` 自己读四十多个 `FINESUB_*` 变量，其中有直接改行为的（`FINESUB_SEPARATOR_ACCEL`、`FINESUB_REFEREE_ACCEL`、`FINESUB_OVERLAP_CANDIDATES`、`FINESUB_SPEAKER_JUDGE_MARKS`、`FINESUB_TASK_STRATEGY`、`FINESUB_KB_SLIM` 等） | R0 文档 1.7.1 |
| 依赖的库也读环境变量：Hugging Face、torch、CUDA、CTranslate2、OpenMP、numba、Triton、代理、证书等。没有代理变量时 Python 在 Windows 上会去注册表读系统代理 | R0 文档 1.7.2 |
| **检出根判定**：目录里同时有 `pyproject.toml` 和 `src\finesub` 就算检出。`third_party/finesub` 满足它。`FINESUB_CHECKOUT_DATA=0` 挡住了配置、密钥、知识库、状态、日志、Agent，**挡不住**编译缓存 `<检出>\cache` 和 tokcount 候选 `<检出>\bin\…` | `finesub/paths.py:24-31`、`:140`、`:253`、`:324`；R0 文档 1.8 |
| 下载线路状态、HF 确认用的数据根与模型根、日志、参考数据、capsule、会话台账**总是**用 `%LOCALAPPDATA%\FineSub`（`finesub_bootstrap.paths.default_data_root()` 只看 `LOCALAPPDATA`），没有任何 `FINESUB_*` 变量能改；另有 `~\.cache\finesub`、`~\.cache\audio-separator`、`~\.finesub\state` 等回落 | `finesub_bootstrap/paths.py:127`、`finesub/paths.py:261`、`:316`；R0 文档 1.1、1.8 |
| Python 写 `.pyc`：源码目录可写、`.pyc` 过期或不存在时就写进源码目录的 `__pycache__\` | 实验报告 1.3 |

### 2.6 WP-Q 查实的事实（2026-11-11）

- **可复现性**：同一份代码、同一台机器，两次跑到 `raw-srt`：`-raw.srt`、`-diar.json`、`-diar-rows.json`、`-diar-evidence.npz`、`-vad-energy.npz` 逐字节一致；`-stable.json`、`-aligned.json` 只差 `metadata` 里 24–29 个耗时字段；`-vad.json` 只差 `source.mtime_ns` 和耗时；`-vocal.ogg` 字节不同、解码采样相同。比对标准见第 0 节第 3 条。
- **外部变量的影响**：录基线时去掉了本机 43 个可能有影响的变量（`CUDA_PATH`、`CUDA_PATH_V11_8`、`NVTOOLSEXT_PATH`、`INTEL_DEV_REDIST`、`MIC_LD_LIBRARY_PATH`、`ANTHROPIC_*`、`CLAUDE_*` 等），去掉前后识别结果相同。
- **已装运行时**：98 个包版本与锁一致，7 个工具都是钉住的版本，管线用到的模型（分离器、whisper、qwen 裁判、nemotron3、speaker-embedding）文件哈希全部匹配。
- **LLM 调用的汇合点**：所有 API 路由的调用都经过 `finesub.llm.llm_runtime.chat_complete(messages, **call_kwargs)`，`call_kwargs` 里是 provider、model、温度、`max_tokens`、thinking、seed 等全部参数。闸门在这里截获。
- **旧版数据布局**（迁移工具的输入，逐项见 R0 文档 1.13）：
  - 主数据目录 `%APPDATA%\Nonoka X`：`finesub.env`、`finesub.toml`、`model_catalog.toml`、`state`、`knowledge/`、`finesub-data/`（`download-routes.json`、`locations.json`）、`bootstrap/`（启动器自己的 Python 与工具）、`app-resources/`、`tasks/`、`documents/` 等，以及 `storage.json`（`runtimeDir` 指向大文件根）。
  - 大文件根：默认 `<主数据目录>/finesub`，可由 `storage.json` 改到别的盘（本机是 `D:\AIProject\nonoka-sub-envir\Nonoka X\finesub`），下有 `runtime/{python,ffmpeg,uv,yt-dlp,tokcount,aria2c,node,pot-provider}`（有的机器还有 `runtime/python-builds`）、`models/{audio-separator,huggingface}`、`.finesub-store.json`、`register-location.cmd`。venv 的 `pyvenv.cfg` 的 `home` 指向基础解释器（本机是系统的 `D:\Envir\Python312`）。
  - `%LOCALAPPDATA%\FineSub`：本地 Agent 的 capsule 目录、`download-routes.json`、`user-data/`。
  - 开发检出：`<仓库>/.local`（`scripts/run_local_sidecar.py`）。

### 2.7 预制环境包与沙箱的实验结论（2026-11-11，实验报告）

- **可重定位的 Python 能代替 venv**：python-build-standalone（PBS）3.12.1 与 3.12.16，用 `uv --no-config pip install --link-mode copy` 按锁把包**直接装进解释器自己的 `Lib\site-packages`**，不建 venv。与已装 venv 比，28286 个文件逐字节相同，唯一的实质差异是 `onnx/`（2.9）。搬到带空格的新路径不做任何修补，import 和两个样本都通过；`site-packages` 里没有写死绝对路径的文件，只有 `Scripts\*.exe` 入口启动器写死了路径（引擎走 `python -m`，不用它们）。
- **体积**：Python 环境 5.04 GiB（zstd -3 后 2.45 GiB），模型 4.00 GiB（2.98 GiB，其中 AOTI 产物 0.6 GiB，加速关闭后不再需要），引擎用的工具 0.92 GiB（0.35 GiB），其余工具 0.28 GiB（未压缩测）。zstd -3 用 Windows 自带 `tar.exe` 解压 2.45 GiB 的包用 28 秒，解压出的环境直接可用。
- **符号链接**：HF 缓存里有 24 个相对符号链接（`snapshots` → `blobs`）。Windows 上解压符号链接需要开发者模式或管理员权限。
- **沙箱成立**：把 `USERPROFILE`、`HOME`、`APPDATA`、`LOCALAPPDATA`、`TEMP`、`TMP` 指向空目录（实验报告 1.6），未改动的 `finesub_bootstrap` 加上锁引擎跑两个样本到 `raw-srt`，产物与基线一致；13 次运行在沙箱之外的受保护位置新增 0、删除 0、修改 0。原先「没有变量能改」的那些默认位置（`%LOCALAPPDATA%\FineSub`、`~\.cache\…`）都跟着变量进了沙箱，所以**转写引擎不需要兼容包去改向默认位置**。
- **TEMP 每个进程一份**：torch 2.11 的 AOTI 加载器在 Windows 上析构时对 `%TEMP%` 做 `remove_all`（实验报告 1.2.1），解压目录名也是固定的。加速关闭后不再加载 AOTI 包，这个缺陷不再触发，但沙箱 TEMP 仍然每进程一份，并由启动方持有一个排序最前的锚点文件 `!!nonoka-temp-anchor`，作为保险。
- **`.pyc`**：新环境第一次运行因为生成 `.pyc` 慢约 40 秒；源码目录可写时 `.pyc` 会写进源码目录。
- **检出判定**：引擎目录里不放 `pyproject.toml` 就不会被当成检出，检出模式的回落全部消失（实验报告 1.5）。
- **沙箱里仍会出现的东西**：NVIDIA ComputeCache（跟 `APPDATA`）、torch 的 NVRTC 内核缓存（跟 `TEMP`）、`torchinductor_<用户名>` 空目录、人声分离的临时目录。
- **没验证的**：注册表、`C:\Windows\Temp`、用 Known Folder API 找目录的原生库的写入；`HOMEDRIVE`/`HOMEPATH` 一并改向的效果（实验报告第 4 节）。

### 2.8 关闭编译加速之后（2026-11-11 读代码确认）

- `FINESUB_SEPARATOR_ACCEL=0` 时 `accel.resolve_accel_paths` 返回 `None`（`separator/accel.py:186-189`），`select_backend` 直接选 eager（`:548`），**不读也不写** `probe.json`，不建 inductor 缓存。所以模型层在运行时不会被改写（实验报告 1.3 看到的 `probe.json` 改写是开着加速时的行为）。
- `FINESUB_REFEREE_ACCEL=0` 时 qwen 裁判的 `_compile_wanted` 在建缓存目录之前就返回（`verification/qwen_referee.py:752`），不碰 `resolve_cache_dir()`。
- 不再需要 MSVC：`aoti_buildable()` 不再被调到；`local_provider._prepare_msvc_environment` 成为死路径（WP-H 删掉）。
- 代价是慢：实验里 `sample_ja` 在 eager 下 76 秒，AOTI 下 57–60 秒。

### 2.9 `onnx` 与 `onnx-weekly`（2026-11-11 查实）

- 锁（`uv pip compile … --extra asr --extra harness --extra runtime --extra cloud`）里同时有 `onnx 1.23.2` 和 `onnx-weekly 1.23.0.dev20260805`，两个 wheel 都往 `onnx/` 目录写文件。
- `onnx-weekly` 是 `audio-separator 0.44.5` 和 `onnx2torch-py313 1.6.0` 的硬依赖（装好的 `METADATA` 里的 `Requires-Dist`）。
- `onnx` 只来自 FineSub `pyproject.toml` 的 `[cloud]` extra（以及不进锁的 `[bench]`），注释写明是给 `speech/joint/sbd.py` 用的；FineSub 里 `import onnx` 的只有这一个文件，它随 WP1 迁到 Nonoka 的 `transcription/enhance/`。
- 两个包提供同一个 import 名 `onnx`。去掉 `onnx` 后 `sbd.py` 拿到的是 `onnx-weekly` 的模块。现在每台机器上的 `onnx/` 都是两者随机混合的结果。

### 2.10 云端（WP-C 查实，2026-11-11）

- `VocalSeparator._install_accel` 每个容器只决定一次 AOTI 还是 eager，依据是第一个输入的时长（< 270 秒选 eager）。同一个 Vocal 容器里的后续输入都跟着它。两次录制里先跑哪个样本不同，所有产物就都不同（段数 112→109、54→61）。生产环境里一个任务的识别结果因此取决于是哪个任务把容器热起来的。用户 2026-11-11 决定云端保持现状，不改。
- 加速方式相同、各自新容器时，云端按第 0 节第 3 条完全可复现。
- `sample_ja` 云端 stable 与 Windows 基线比：段数同为 109，64 段文字不同。
- 当前录下的 `test_data/quality-baseline/cloud/` 不能当基线用。

## 3. 对齐规则

### 3.1 拆掉什么，留下什么

| 拆掉 | 换成 |
|---|---|
| 同步脚本、补丁栈、基线文件、正反向重放测试 | 无（WP-A 已完成） |
| `FILES.json`（全部文件的哈希，用于证明「可重建」） | `QUALITY_LOCK.json`：上锁文件的哈希，用于证明「没被动过」 |
| 「不 fork、vendor 是生成产物、改动必须整理成补丁」的规则 | 第 0 节的质量红线 |
| `finesub_bootstrap/` 的安装与配置角色（Nonoka 现在用它装环境、存配置密钥） | Nonoka 的 `runtime/`（层管理器）、`config/`、构建流水线 |
| `finesub_bootstrap/` 的引擎运行时角色 | 转写包里原样保留（只删自下载）；纠错包里换成只含纠错所需名字的兼容包 |
| `pylock.*.toml`、运行时清单、模型清单、下载源（原在 `finesub_bootstrap/`） | `src/nonoka_x/resources/` 下 Nonoka 的清单，是构建流水线的输入，内容由 `RUNTIME_PINS.json` 看住。转写包里的 `finesub_bootstrap` 自带的那几份数据文件保留（引擎运行时读它们），内容同样由 `RUNTIME_PINS.json` 看住 |
| vendor 里的非核心文件 | 删除，见第 1 节第 7 条 |

`UPSTREAM.json` 保留，只留仓库、tag、commit、版本、`engine_bundle_id`。

质量锁由 `tests/test_finesub_quality_lock.py` 检查：`third_party/finesub/src/` 下除第 0 节解锁的四处外，每个文件的 sha256 等于 `QUALITY_LOCK.json` 里记录的值，文件不多不少（现为 550 个）。分包后每个包有自己的 `QUALITY_LOCK.json`。更新质量锁只能出现在用户批准的、专门改引擎的提交里。

### 3.2 以后升级上游时跟什么

这是给将来用的规则，本次重构不执行升级。

| 类别 | 范围 | 升级时 |
|---|---|---|
| **关键算法** | 转写引擎：`speech/` 下除 `joint/` 外的全部，加 `media/`、`subtitles/`、`text.py`。纠错层与翻译层：`llm/stages/`，以及 `llm/` 里决定提示词、分窗、回答解析、后处理的文件，含 `prompt_templates/` | 跟上游。每次升级过回归闸门并由用户看过质量对比 |
| **其余** | 编排、CLI、配置、`llm/agent/`、`llm/routing/`、LLM 调用设施、`llm/knowledge/` | 默认不跟。要跟也可以，同样受质量红线约束 |
| **`finesub_bootstrap/`** | 转写包里的那一份 | 可以跟上游，但删自下载的改动要重新做一遍（记在 `LOCAL_CHANGES.md`）；它的锁与清单变了就是改钉住的内容（第 0 节第 7 条），要重新构建并发布 Python 环境层 |
| **纠错兼容包** | Nonoka 自己的代码 | 不跟。上游若改了纠错那一半对 `finesub_bootstrap` 的用法，由依赖面测试发现，在兼容包里补 |

`scripts/finesub_align.py diff --ref <tag>` 取上游某个版本，只对关键算法的路径出报告。`llm/` 顶层和 `llm/knowledge/` 的逐文件归属已由 WP-A 列出。

## 4. 设计决定

标 ⚠ 的开工前请确认，改了要回头调整对应工作包。条款按主题分节、连续编号；旧编号对照见附录 A.2（已移到 `docs/transcription-engines-plan-done.md`）。

### 4.1 引擎层与纠错层

1. **引擎层 `src/nonoka_x/transcription/`，纠错层 `src/nonoka_x/correction/`，两层都是插件式的。** worker 只认注册表里的接口，按任务请求取引擎和纠错实现；FineSub 转写引擎和其他引擎一样只交出 `transcript.json` 加自己的原生产物，没有 FineSub 专属的编排分支。「FineSub 转写 + finesub correction」恰好能原样复用 stable（5.2），这是纠错层对原生产物的优化，不是 worker 的特殊路径。分包之前，调 FineSub 编排入口的只有 `transcription/engines/finesub.py` 和 `correction/finesub.py` 两个文件，两者互不 import；分包之后它们搬进两个引擎包的适配器（4.6）。
2. **`run_pipeline` 拆成两个显式入口**：`run_transcription`（到 stable 为止，含声学说话人分离）和 `run_post_stable`（stable 之后）。后者用参数明说说话人来自声学时间线还是来自转写行标签、裁判听哪条音轨，不再靠产物元数据里的后端名推断。`run_pipeline` 留作两者的薄组合，云端在 WP-CL 之前照旧调它；分包之后不再需要（云端只跑转写，4.7）。
3. **`speech/joint/` 整个迁出 FineSub**，用 `git mv` 搬进 `nonoka_x/transcription/` 再改造。`stages.py` 和 `pipeline.py` 里的 `asr` 选项与联合 ASR 分支一并删掉，FineSub 从此只有它自己的识别链路。
4. **云 API 引擎在启动器环境里就能跑**，只要 httpx 和 ffmpeg（工具层）。没有 N 卡、没下 Python 环境层的用户也能转写加翻译。
5. **断行沿用迁过来的 `lines.py`，句子 DP 复制一份成 Nonoka 自己的代码。** WP1 把 `speech/postprocessing/segmentation.py` 和它用到的纯 Python 部分（`recognition.segments` 的常量、`subtitles.metrics`、`text` 里的几个函数、相关配置默认值）复制到 `nonoka_x/transcription/segmentation/`，注明来源，之后由 Nonoka 维护。FineSub 里的原件不动。复制件与原件在同一批输入上输出相同，由测试证明（原件还在时录成基线）。云 API 引擎从此不 import `finesub` 的任何东西。
6. **第一版不接需要 Python 环境层的增强**（用户 2026-11-12，附录 B 第 42 问；原附录 C 第 1 项）：云 API 引擎只在启动器环境里跑。迁过来的增强代码（`transcription/enhance/`：空档重问用的 Silero、声纹重连、断句、强制对齐、Gemini Live 修补、MOSS 的 Silero 段内定时）原样留着、不接进引擎，后续阶段再定跑在哪、进不进沙箱。所以 MOSS Pro 的段内时间按字数插值，说话人只靠重叠时间跨窗对齐，MAI2 不做空档重问。
7. ⚠ **转写引擎的密钥由用户自带**，存进 Nonoka 的 `secrets.json`（4.3 第 21 条）。经 Nonoka Cloud 代理并扣额度留到后续阶段。
8. ⚠ **MOSS 指 MOSS Pro 云 API。** 本地 MOSS 的代码随 `speech/joint/` 一起迁出，但第一版不注册成引擎、不进界面，后续阶段再接。
9. **两层之间的中间格式是 Nonoka 自己的 `transcript.json`**（5.2）。转成 FineSub 的 stable JSON 是 finesub correction 内部的事。
10. **`provider` 含义不变**（任务由本机 sidecar 还是云端管理）。自研引擎都在本机 provider 下跑；云端 provider 只跑 FineSub 转写。
11. **换引擎等于新任务**。引擎记在任务请求里，重试和续跑沿用原引擎。

### 4.2 运行环境：预制环境包

12. **安装管理归 Nonoka。** `src/nonoka_x/runtime/` 是层管理器：下载、校验、解压、切换版本、修复、清理（WP-E3）。`src/nonoka_x/` 里没有任何地方 import `finesub_bootstrap`，由测试强制。
13. **清单是 Nonoka 的。** 运行时锁、工具清单、模型清单复制到 `src/nonoka_x/resources/`，是构建流水线的输入和权威来源（WP-R1）。钉住的内容（解释器构建与版本、包版本与哈希、模型 revision 与哈希、工具版本与哈希）记进 `RUNTIME_PINS.json`，由测试看住；改它需要用户批准（第 0 节第 5、7 条）。`pylock.win-py312.cn.toml` 与标准锁只差下载地址、哈希全同；不再做国内镜像（第 18 条）之后它对 Nonoka 没有用处，Nonoka 的清单只留标准锁（转写包里 `finesub_bootstrap` 自带的那份不动）。
14. **预制环境包分三层**（用户 2026-11-11），每层是一个 `tar.zst` 压缩包加一份逐文件清单：
    - **Python 环境层**（Windows）：python-build-standalone `cpython-3.12.1+20240107-x86_64-pc-windows-msvc-shared-install_only`（与现在的基础解释器同版本，附录 B 第 19 问），锁里的全部包直接装进解释器自己的 `Lib\site-packages`，**不建 venv**；去掉 PBS 自带的 pip 和 `README.txt`；全部 `.py` 预编译（第 17 条）。不再用用户机器上的系统 Python。约 2.45 GiB。
    - **模型层**（Windows）：模型清单里钉住 revision 的全部模型，保持引擎依赖的目录内部结构（R0 文档 1.4）；**不含** `audio-separator\accel\`（加速已关）；HF 缓存里的符号链接在构建时换成实文件（第 17 条）。约 3 GiB 以内。
    - **工具层**（Windows 一层，macOS 一层）：ffmpeg/ffprobe、tokcount、yt-dlp、aria2c、node、pot-provider，**一层装全**（用户 2026-11-11，附录 B 第 15 问）。uv 只在构建时用，不下发。macOS 只有这一层（内容按现有 `runtime-manifest.macos-*.json`）。
    - FineSub 引擎的**代码不进层**，随桌面安装包下发（`app-resources\<版本>\` 下，与现在相同），与 Nonoka 核心同步升级（附录 B 第 7 问）。
15. **各引擎需要哪几层**由 `EngineSpec.environment` 和 `PACKAGE.json` 声明：FineSub 转写引擎要三层全部；云 API 引擎只要工具层；它们的可选增强要 Python 环境层和模型层；finesub correction 不要任何层（跑在启动器环境里）。就绪检查和「去安装」的引导都从这里来。
16. **`finesub_bootstrap` 的去向**（用户 2026-11-11）：
    - **转写包原样带着它**（分包前就在 vendor 里），运行在沙箱里，引擎读写的默认位置由沙箱改向（4.5），不需要兼容包。唯一的改动：**删掉「缺模型时自己下载」**（实验报告第 3 节第 1 条，WP-R7）——`model_ensure.ensure_hf_model` 和 `model_fetch` 在模型缺失时报错而不是下载（上锁的调用方不能改，只能改这两个函数）；另外两条不经它的下载路径也要堵住：audio-separator 库自己从 GitHub 下（`separation.py:285-290`）由就绪检查保证分离器文件齐全，加载器按 `HF_ENDPOINT` 懒下载由 `HF_HUB_OFFLINE=1` 堵住。改动记进 `LOCAL_CHANGES.md`。它自带的锁、清单、下载源等数据文件不动。
    - **纠错包用兼容包**：只提供纠错那一半需要的名字（约 35 个，不含 `model_*`、`hf_verify`、`model_fetch`、`download_routes`），默认位置按 R0 文档 D21 改向 `engines\finesub-correct\` 下；在 WP-P1 里直接建在 `engines/finesub-correct/src/finesub_bootstrap/`，和拆进程一起生效（附录 B 第 21 问）。要求沿用原设计：是真实包、名字不多不少（测试强制）、import 链上没有 torch 和 numpy、不 import `nonoka_x`、位置取自启动上下文；决定结果的几个（`secrets` 读出的密钥、`token_counter` 的计数方式、`artifacts` 的文件名常量）与原实现语义相同，由调用等价、LLM 回放和 WP-H 的 `token_counter_source` 核对证明；第一版可以把原模块代码复制过来改，注明来源。
    - Nonoka 核心代码不 import 它（第 12 条）。
17. **预制包的构建规则**（WP-E1）：
    - **可重复**：同一份输入在干净目录里构建两次，逐文件清单完全相同；构建在本机进行，发布前必须做第二次构建并比对（附录 B 第 13 问）。
    - **`.pyc` 预编译**：Python 环境层和引擎代码都用 `unchecked-hash` 模式预编译（搬目录后仍有效），`.pyc` 进文件清单；引擎进程用 `-B` 启动，运行时不写字节码（附录 B 第 8 问）。引擎代码的预编译在桌面暂存（staging）时做。
    - **符号链接换实文件**：模型层里 `snapshots\…` 的链接替换成它指向的文件，不带对应的 `blobs\`。先实测 `hf_verify` 的校验标记和各加载器认这种布局，再跑 `--real`（附录 B 第 9 问）。
    - **证明与钉住的内容一致**：解释器构建的 sha256、每个包的名称与版本、安装时 uv 校验过的 wheel 哈希、每个包装出的文件与它 `RECORD` 里的哈希相符、模型文件哈希、工具版本与哈希，都与 `RUNTIME_PINS.json` 对照，不一致就不产出。
    - 清单里不出现构建机的绝对路径。层里写死构建机路径的文件只允许是 `Scripts\*.exe` 入口启动器和 `Scripts\numba` 的 shebang（它们在包的 `RECORD` 里，删掉会让内容对不上 `RECORD`，所以保留；搬目录后不能用，引擎走 `python -m`，不用它们），清单里标出来。
18. **托管在 Cloudflare R2**（用户 2026-11-11，WP-E2）：
    - 桶绑一个自定义域名，**公开读**；层压缩包按内容哈希命名、不可变（附录 B 第 17 问）。~~模型层另外按文件 sha256 存一份单文件~~：用户 2026-11-13 决定不放单文件（附录 B 第 63 问），R2 上只有三层的压缩包和清单；迁移时的模型补齐改为下整个模型层压缩包（4.4 第 25 条）。
    - 用 Cloudflare 的速率限制挡盗链。
    - 国内线路**依靠用户的本地代理**，不另做镜像（用户 2026-11-11）。下载走 Nonoka 的代理设置（R0 文档 D17）。GitHub 镜像脚本（`publish-github-mirror.mjs`）和国内镜像线路删掉。
    - 离线包保留，改成装同样的层文件。
19. **层的版本与核对**（用户 2026-11-11）：
    - 层的版本号 = 它逐文件清单的哈希。每个桌面版本里写死它要的各层版本；内容变了就整层替换，**第一版不做增量**（附录 B 第 10 问）。例外只有迁移时的模型补齐（4.4 第 25 条）。
    - 位置：`<大文件根>\layers\<层>\<版本>\`，旁边 `current.json` 指向在用的版本。新版本下载、校验、解压完再切指针；旧版本没有任务在用就删（附录 B 第 16 问）。
    - 就绪 = 当前版本目录存在且与清单相符。快速检查只比文件列表与大小；完整检查比 sha256（修复、迁移后、用户要求时做）。层在运行时是只读的：引擎不往层里写（2.8；`.pyc` 见第 17 条），由 WP-H 的写入检查证明。
20. **再分发许可**：用户 2026-11-11 已核对，预制包里各组件可以再分发。层里随附各组件的许可证文件（PBS 与 wheel 自带的保留，不删）。

### 4.3 配置与密钥

21. **配置和密钥归 Nonoka。** `src/nonoka_x/config/`：`<主数据目录>\settings.json` 与 `secrets.json`（R0 文档 2.1、2.2，D1、D2），是唯一的事实来源。用户和界面只跟它打交道。
22. **FineSub 要的东西由包内桥接派生。** 核心启动包的进程时给一份中性的启动上下文（R0 文档 1.15 的字段，第 33 条）；把它变成 `finesub.toml`、模型目录文件、`FINESUB_*` 环境变量是包内桥接的事，在包的进程里、import `finesub` 之前完成。派生文件放 `engines\<包>\run\<进程标识>\`，每个进程一份，进程结束删，启动时清残留（D6）；`finesub.toml` 照写 `[nonoka_models]`，与闸门基线同字节（D20）。分包之前，桥接先落在 `src/nonoka_x/finesub_bridge.py`（WP-R5），WP-P1 搬进包。
23. **密钥只走进程环境变量，不落盘**（D3）：只在用户改过 `GEMINI_BASE_URL` 时写一个只含这一项、不含任何密钥的 `.env`，并给引擎设 `FINESUB_ENV_PROTECT=0` 让它对派生文件只读不写（D22）。转写包只拿它声明要的密钥（`HF_TOKEN`，用户配了才有）；纠错包拿 LLM 与检索密钥和 `NONOKA_CLOUD_KEY`。

### 4.4 数据布局与迁移

24. **新布局**（R0 文档 1.12，按本计划调整）：
    - 主数据目录只放 Nonoka 自己的东西：`settings.json`、`secrets.json`、文档、任务、媒体库等；引擎包的私有目录在 `engines\<包 id>\`（D4），任务的原生产物在 `tasks\<id>\native\<包 id>\`（D7）。
    - 转写包的沙箱在 `engines\finesub-transcribe\sandbox\`（4.5 第 28 条）。
    - 大文件根叫 `runtime`（默认 `<主数据目录>\runtime`，搬过的为 `<所选位置>\Nonoka X\runtime`），由 `storage.json` 的 `runtimeDir` 记录；内部是 `layers\<层>\<版本>\` 加 `current.json`（第 19 条），另有 `cache\`（只放可以随时删的东西）。R0 文档 D5 的 `python\`、`tools\<工具>\<版本>\`、`models\…` 布局被取代。
    - 安装器自己的小状态在 `<主数据目录>\runtime-state\`；不再有 `locations.json`（D18）；不做国内镜像后也不再有下载线路状态。
    - `%LOCALAPPDATA%\FineSub`、`bootstrap\`、`app-resources\` 不碰（D8、D14）。
25. **已有用户的迁移**（用户 2026-11-11，WP-M1、WP-M2）：
    - **模型搬过去不重下**：旧大文件根的 `models\` 搬进新的模型层位置，按模型层清单逐文件核对 sha256；对上的留下；有缺的或不对的，就从 R2 下整个模型层压缩包，只解出缺的和不对的文件（用户 2026-11-13，附录 B 第 64 问；原为按文件补，R2 上已不放单文件）；全部对上就不下载。补齐后整个目录与清单相符，才登记成当前的模型层版本。`models\audio-separator\accel\`（AOTI 产物，约 0.6 GiB）、HF 的校验标记以外多出来的文件删掉。
    - **Python 环境和工具重新下载**，旧的清空：删掉旧的 Python 环境（`runtime\python\`、`runtime\python-builds\`）、旧工具（`runtime\{ffmpeg,uv,tokcount,yt-dlp,aria2c,node,pot-provider}\`）、缓存（`cache\`、uv 缓存、编译缓存，含 `third_party\finesub\cache\`、`app-resources\<版本>\…\cache\` 这类检出模式下的编译缓存）、`.finesub-store.json`、`register-location.cmd`。系统 Python 不碰。
    - **先删后下**（用户 2026-11-11，附录 B 第 2 问）：清空旧环境后再下载新的层。从清空到下载完成之间 FineSub 转写不可用，界面要说明并显示进度；下载失败可以重试，不回退到旧环境（旧环境已删）。
    - 设置和密钥读进 `settings.json`、`secrets.json`；`finesub.env`、`finesub.toml` 里 Nonoka 不认识的键和变量**丢弃**，迁移报告里列出被丢弃的表名、键名、变量名（不列值），旧文件改名留作备份（D11、D11c、D13）。迁移后派生出的 `finesub.toml` 与迁移前的语义相同，**只对 Nonoka 认识的键成立**：用户手写过 `[segmentation]`、`[pools]` 之类的表的，行为回到默认值，迁移报告里写明。
    - 知识库搬进 `engines\finesub-knowledge\`、限流状态搬进 `engines\finesub-correct\`，内容一字不改；任务目录里的 FineSub 产物归进 `native\<包 id>\`。
    - 迁移规则的其余部分（先出计划、目标已存在就停下不合并、同盘改名、`storage.json` 改写）按 R0 文档 2.7，其中涉及 venv 与 `pyvenv.cfg` 的条目（D10、D23）作废。

### 4.5 沙箱与密闭运行

26. **FineSub 的代码只在 Nonoka 拉起的引擎包进程里跑。** sidecar 主进程不 import 任何 FineSub 模块；它现在要的那几样（模型路由与目录、说话人产物解析、显卡档位选项）改成通过进程协议问包（4.6 第 33 条）。
27. **两个包的进程环境都从零构建，不继承**（附录 B 第 6 问）。分类表（R0 文档 1.7.3 为初稿，按本节调整）里每个变量属于三类之一：
    - **系统必需**：从父进程按名原样带的最小集合，逐个列名，不用通配。转写进程里，用户目录与临时目录相关的变量不在这一类，而是「Nonoka 设定」（第 28 条）。
    - **Nonoka 设定**：值完全来自 Nonoka 的配置、任务请求和目录布局，无条件写入，不用 `setdefault`。所有 `FINESUB_*`（含 `FINESUB_SEPARATOR_ACCEL=0`、`FINESUB_REFEREE_ACCEL=0`）、模型与缓存目录、代理（D17：用代理就写 `HTTP(S)_PROXY` 与 `NO_PROXY`，不用就写 `NO_PROXY=*`）、`CUDA_VISIBLE_DEVICES`、`PATH`（工具层目录加系统目录拼出）、`HF_HUB_OFFLINE=1`（转写进程）、`PYTHONPATH` 与 `PYTHONNOUSERSITE=1`（供孙进程用，D9）。
    - **其余一律不存在**，包括 MSVC 的 `INCLUDE`、`LIB` 等（不再需要，2.8）、`TORCHINDUCTOR_CACHE_DIR`。
28. **转写进程在进程级沙箱里跑**（用户 2026-11-11）：
    - `USERPROFILE`、`HOME`、`APPDATA`、`LOCALAPPDATA` 指向 `engines\finesub-transcribe\sandbox\home\` 下对应的目录，**跨运行共用**（NVIDIA ComputeCache、torch 内核缓存留在里面，免得每次重新 JIT；附录 B 第 18 问）；`TEMP`、`TMP` 指向 `sandbox\temp\<进程标识>\`，**每个进程一份**，由启动方在里面先建锚点文件 `!!nonoka-temp-anchor` 并在进程运行期间一直开着；进程结束删掉自己的 TEMP，启动时清掉不属于活进程的残留。
    - `HOMEDRIVE`、`HOMEPATH` 是否一并改向，由 WP-H 实测后定（实验没单独验证，附录 C）。
    - 模型变量 `FINESUB_MODEL_DIR`、`HF_HOME`、`TORCH_HOME` 指向模型层的当前版本。
    - 解释器是 Python 环境层的 `python.exe`，用 `-I -B` 启动；入口在 import 任何东西之前设 `sys.path`（只含转写包的源码目录，其中有原样的 `finesub_bootstrap`），并把自己的标准输出改成 UTF-8（R0 文档 1.10）。
    - 当前目录是任务目录。
    - 转写进程里没有本地 Agent CLI 相关的变量（D16 只给纠错包）。
29. **纠错进程不进沙箱**（用户 2026-11-11）：跑在启动器环境里，用真实的用户目录，本地 Agent CLI 照常可用。环境同样从零构建、`-I` 启动；`sys.path` 只含纠错包的源码目录（其中是兼容包）；兼容包把引擎的默认位置改向 `engines\finesub-correct\`（D21）；本地 Agent CLI 的配置与证书变量逐个列名带给它（D16）。
30. **外部的东西要生效，只能先成为 Nonoka 的设置。** 例如系统代理：由 Nonoka 读取、按自己的代理策略决定，再显式传给包。用户以前靠系统环境变量调 FineSub 的做法从此失效，需要的能力在 Nonoka 设置里提供。
31. **密闭有边界，如实列出**：显卡与驱动、网络本身；Nonoka 管理的目录里的层、缓存、状态文件、知识库；用户选用的本地 Agent CLI 自己的登录状态和配置（纠错进程）；由 Agent CLI 拉起的 MCP 服务子进程里用户级 site-packages 仍然生效（R0 文档 3.2 第 5 条）；沙箱管不到的写入途径——注册表、`C:\Windows\Temp`、用 Known Folder API 找目录的原生库（2.7）；系统的 VC++ 2015–2022 运行库（torch 的 `c10.dll` 要 `msvcp140.dll` ≥ 14.40，wheel 和 PBS 都不带，`src/nonoka_x/msvc_runtime.py` 在下载前检查）。MSVC 工具链不再是边界（2.8），系统 Python 也不再是（第 14 条）。
32. **用三种办法证明密闭，两个进程都做**（WP-H）：分类表覆盖扫描、投毒测试、读取审计；转写进程另加沙箱写入检查（运行前后对沙箱外的受保护位置做快照比对，做法同实验报告 1.0）。

### 4.6 彻底隔离：两个引擎包与知识库包

33. **FineSub 分装成两个引擎包，Nonoka 核心对它零引用。** `engines/finesub-transcribe/`（转写包）和 `engines/finesub-correct/`（纠错翻译包）。`src/nonoka_x/` 里不 import `finesub`、`finesub_bootstrap`，不出现 `FINESUB_`，不读写 FineSub 的任何文件格式，没有白名单。分包前在 `src/` 下的适配器、桥接在 WP-P1 搬进包里。`third_party/finesub/` 在分装后删除。
    - **代码冻结后原样分装。** 冻结点是 WP-B 和 WP1 做完之后。分装只搬文件，不改内容：包里每个 FineSub 文件的 sha256 等于 `QUALITY_LOCK.json` 里同一路径的值。依据（2026-11-11 查实）：`speech/` 不 import `llm/`；`llm/` 只用到 `speech/diarization/` 的 `labels`、`judgments`、`product`、`assign` 和 `speech/postprocessing/recall_repair` 的一个函数；其余共用的是顶层 16 个文件、`subtitles/`、`media/`。精确的文件清单由 WP-P0 算出。
    - **共用的底座两个包各带一份**，内容相同，各自上锁。`run_transcription` 归转写包、`run_post_stable` 归纠错包。
    - **两个包各跑各的进程**（顶层包名都是 `finesub`，不能在同一个进程里加载）。核心和包之间是进程协议（请求 JSON 进，事件流出，与现在的 worker 协议同形），核心不 import 包里的任何代码。包的就绪检查、模型路由与目录等需要问引擎的信息，也通过这个协议问。
    - **包靠清单被发现，可以随时删。** 每个包根目录有 `PACKAGE.json`：标识、种类（转写 / 纠错）、入口、能力声明、需要的层（第 15 条）、要的密钥名。核心启动时扫描 `engines/`，有哪个包就注册哪个。删掉转写包：其余引擎加 FineSub 纠错照常。删掉纠错包：所有转写引擎照常出原始字幕，纠错翻译、LLM 模型设置显示「未安装」。删掉知识库包：知识库页显示「未安装」，开了知识库或 ASR 上下文的请求被就绪检查拦下（第 35a 条）。两个都删：核心照常启动，云 API 引擎照常转写。为此原始字幕的渲染（`transcript.json` 到 SRT）由核心自己做。删除指代码层面可分离、打包时可以不带；界面不提供卸载。
    - **包目录不许是「源码检出」的形状**：不能同时有 `pyproject.toml` 和 `src\finesub`（2.5），测试断言。
    - **启动上下文**：运行时位置（层的当前版本目录）、模型目录、这个包的数据目录、派生文件目录、任务目录、按名字给的密钥、代理、设备、设置项、LLM 路由与端点（R0 文档 1.15，去掉 `runtime.manifests` 和 `download` 两个字段：转写包不再下载，纠错包不碰模型）。
34. **产物由包转换成 Nonoka 的格式后交给核心保存；原生产物是包的私有附件。**
    - 转写包交出 `transcript.json` 和 Nonoka 格式的说话人信息。它的原生产物（`-stable.json`、`-aligned.json`、`-diar*.json`、`-vad*.json`、`-vocal.ogg` 等）放在 `tasks\<id>\native\finesub-transcribe\`，核心只当不透明的附件登记，不解析。
    - 纠错包拿到任务时，如果附件里有 FineSub 的 stable，**原样使用，一个字节都不改写**（红线：FineSub 转写接 FineSub 纠错的输入与现在相同）；没有或被删了，就从 `transcript.json` 构造。
    - 纠错包交出字幕和 Nonoka 需要的结构化结果，转换在包的适配器里做；`projector.py` 现在解析 FineSub 说话人产物的部分搬进适配器。
    - 知识库不归纠错包，归知识库包（第 35a 条）。
    - 主数据目录和任务目录里，除包的私有目录外，没有 FineSub 格式的文件。
35. **分包之前写的东西按「将来要搬进包」来写。** WP4a、WP4b 的适配器、WP-R5 的桥接先落在 `src/` 下，但除协议类型外不 import `nonoka_x` 的其他模块，WP-P1 搬的时候就不用重写。
35a. **知识库独立成第三个包**（用户 2026-11-12，附录 B 第 37–40 问；编号是插入的，后面的条款号不变）：
    - `engines/finesub-knowledge/`（知识库包）持有知识库的数据和全部知识库代码（FineSub 的 `llm/knowledge/` 及其依赖、现 `knowledge_worker.py` 的管理操作）。数据在 `engines\finesub-knowledge\`，迁移时从主数据目录搬来，内容一字不改（WP-M2）。
    - **只有知识库包碰知识库数据。** 转写包（ASR 上下文的条目选择）和纠错包（读条目、注入、写回知识更新、写锁）都经知识库包的协议访问；为此改上锁文件里的访问点，按第 0.1 节第 5 条由 WP-K 实施。界面上的知识库管理也走这个协议。
    - 知识库包不在时：开了知识库（`knowledge` 不是 `off`）或 ASR 上下文要用知识库的请求，由就绪检查拦下并说明，不静默当作关闭（附录 B 第 39 问）。其余功能照常。
    - 待 WP-P0 第 10 问回答：协议的形状（包进程之间是否经核心转发、沙箱里的转写进程怎么连到知识库包）、知识库包自己的 LLM 调用（知识维护与更新要用 LLM）跑在哪个进程、用谁的 LLM 设施、调用开销。

### 4.7 云端

36. **云端只留转写包，交统一的 `transcript.json`**（用户 2026-11-11，WP-CL）。
    - Modal 容器（`nonoka-sub-cloud/modal_compute/`）只装转写包，跑到 stable 为止，在容器里由转写包的适配器转成 `transcript.json`。
    - 云端任务的结果改成与本机引擎同形：必备的 `transcript.json` 加不透明的私有附件（`stable.json`、`diar.json`）。Worker 里**只改任务结果这一部分**（`src/jobs/` 里登记、存取、下发结果产物的代码，以及随之需要的 `migrations/`）：它把附件当不透明文件存和发，不解析。
    - **不动** LLM 转发：Worker 里的 LLM 转发路由、`tokyo_llm/`。纠错包经 `nonoka-cloud` 路由调 LLM 的方式不变。
    - 桌面拿到云端结果时不需要任何 FineSub 包来转换：`transcript.json` 直接可用，附件交给纠错包原样使用。桌面上两个 FineSub 包都删掉时，云端 FineSub 转写照样能用，出到原始字幕为止。
    - 已经存在云端的旧任务结果只有 `stable_json`、`diar_json`：随第 38 条的一次性转换一起补出 `transcript.json`，原件转成附件。切换之后 Worker 和桌面都不再有旧字段的兼容路径。
37. **云端的运行环境保持现状**（用户 2026-11-11）：容器镜像仍按云端自己的方式装环境（Linux，不用预制包），仍由容器自行选择加速（2.10），所以云端结果不可逐次复现，`--cloud` 用较弱的标准（第 0.1 节第 3 条）。唯一的改动来自第 16 条删掉自下载：**云端部署前用一个独立的预热脚本**（不经引擎）按模型清单的钉住 revision 把模型放进 `/models` Volume 并校验，缺模型时任务报错（附录 B 第 20 问，WP-CL0）。

### 4.8 媒体库同步

38. **媒体库同步改成 Nonoka 的格式，一次做完**（用户 2026-11-11，WP-L）。现在同步的是四个 FineSub 格式的产物（`stable_json`、`raw_srt`、`annotated_csv`、`final_srt`），名字在桌面上传、Worker 白名单、桌面取回三处写死，取回后的投影还要求必须有 `stable_json`；分包之后这条链会断。
    - **同步的内容**：`transcript.json`、字幕（`raw_srt`、`final_srt`），以及纠错结果的 Nonoka 格式（暂名 `correction.json`，由纠错包的适配器从 `annotated.csv` 转出，承载投影需要的全部信息）。不同步任何 FineSub 格式的文件，私有附件不上云。格式由 WP-L 定稿后写进 `docs/provider-spec.md`。
    - **投影只吃 Nonoka 的格式**：`project_edit_document` 的输入从 stable 加 `annotated.csv` 改成 `transcript.json` 加 `correction.json`。任何转写引擎的任务都能投影、同步、取回，包括不经纠错的。红线：同一个 FineSub 任务，新旧投影出的字幕文档内容相同，由测试证明。
    - **不留兼容期**：不双写，Worker 的白名单直接换成新名字，桌面不保留读旧格式的路径。
    - **云端已有数据一次性转换**：媒体库条目和任务结果里的旧产物，用两个包的适配器里同一份转换代码批量转成新格式；原件挪到备份前缀下保留一段时间，确认无误后再删。转换前后条目数相同，每个条目能投影出字幕文档，抽样与转换前的投影结果对比。
    - **桌面强制升级**：发版时在发布清单里设 `minVersion`（已有机制，`desktop/internal/selfupdate/service.go` 的 `releaseMandatory`）。Worker 侧同时拒绝旧格式的同步请求并返回明确的升级提示。
    - **切换顺序**固定：先停写（Worker 进入维护态）→ 批量转换并校验 → 部署新 Worker → 发布新桌面版并设 `minVersion` → 解除维护态。任何一步校验不过就回到旧 Worker、旧数据。

### 4.9 引擎配置页

39. **「运行环境」页改成「引擎配置」页，全局只选一个转写引擎**（用户 2026-11-11，WP-UI）。重构完成后做，只重做这一块，媒体库、编辑器、导出不动。
    - **2026-11-13 改（用户，附录 B 第 73 问；与下面几条冲突处以此为准）**：不单独做引擎配置页，**去掉「运行环境」页**；环境安装、转写引擎、纠错翻译引擎的选择全放进**设置页**，一页长滚动，从上到下：转写引擎 / 运行环境 / 纠错与翻译 / 存储与网络，不加导航。纠错引擎是「当前纠错引擎」卡片（finesub correction 与就绪状态），LLM 提供商配置挂在它下面，有第二个纠错引擎再加单选。三组云引擎密钥放在设置页转写引擎面板里。新建任务对话框去掉本机 / 云端开关，只显示当前引擎和去设置页的入口；云端 FineSub 是引擎列表里的一项，请求带 `transcription.engine="finesub-cloud"`（Go 不转发）。这部分界面已由 WP6 第二阶段做完（2026-11-16）；依赖 WP-P1 / WP-H 的部分（删掉转写包后的表现、按包列占用、只装所选引擎缺的层）留在 WP-UI 以后补。
    - **全局单选**：用户在引擎配置页选定当前的转写引擎，之后所有新任务都用它。新建任务的对话框不再让选引擎，只显示当前用的是哪个，并给一个去引擎配置页的入口。任务请求里仍然记着引擎（提交时从全局设置填入），所以重试和续跑沿用原引擎（第 11 条）。
    - **可选项来自注册表**：本机的 FineSub 转写包（装了才出现）、MAI-Transcribe-2、Gemini 3.5 Transcribe、MOSS Pro，以及云端 FineSub。云端 FineSub 是列表里的一项：它不需要安装，就绪条件是已登录且有额度；选中它时任务的 `provider` 是云端，选其余各项时是本机。
    - **选哪个装哪个**：每个引擎声明自己要的层（第 15 条）。选中一个引擎时，页面列出它缺哪几层、要下载多少、装在哪，用户确认后只下载缺的层。云 API 引擎只要工具层和密钥；它们的可选增强（要 Python 环境层与模型层）在该引擎的面板里单独列为可选安装。引擎没装好之前不能设为当前引擎，或设了之后新建任务被拦住并指回这一页。
    - **切换时保留已装的东西**：换引擎只下新引擎缺的层，原来的不删，换回去不用重新下载。页面按引擎列出各自占的空间（共用的层只算一次并标明被谁共用），提供「清理未使用的」：删掉当前引擎和纠错包都用不到的层。有任务还在用某一层时不允许清理它。
    - **这一页只管转写引擎**：纠错翻译的模型路由、LLM 密钥、知识库留在现有的设置页和知识库页，不动。转写引擎自己的密钥（OpenRouter STT、Azure Speech、MOSS Pro）放在引擎配置页该引擎的面板里。
    - 安装位置、存储位置搬迁这些原「运行环境」页的功能保留，并入新页。层不含绝对路径，搬存储位置时整体搬目录即可（不再需要改写 `pyvenv.cfg`）。
    - 桌面本地已有的任务和文档由 WP-M2 处理：已投影好的字幕文档原样保留；任务目录里的旧产物转出 Nonoka 格式，原件归进私有附件目录。

## 5. 目标结构

最终形态（WP-P1 之后）：

```text
engines/
├── finesub-transcribe/        # FineSub 转写包；删掉不影响其余；在沙箱里跑
│   ├── PACKAGE.json           # 标识、入口、能力、需要的层、要的密钥名
│   ├── QUALITY_LOCK.json      # 包内每个 FineSub 文件的哈希，等于分装前的值
│   ├── LICENSE、LICENSES.json、UPSTREAM.json、LOCAL_CHANGES.md   # 没有 pyproject.toml
│   ├── adapter/               # 协议入口、桥接（启动上下文 -> FineSub 的配置与环境）、产物转换
│   └── src/
│       ├── finesub/           # speech/、共用底座、run_transcription
│       └── finesub_bootstrap/ # 原件，只删了自下载（4.2 第 16 条）
└── finesub-correct/           # FineSub 纠错翻译包；删掉不影响其余；不进沙箱
    ├── PACKAGE.json、QUALITY_LOCK.json、LICENSE、LICENSES.json、PROMPT_LICENSE.md、UPSTREAM.json、LOCAL_CHANGES.md
    ├── adapter/               # 协议入口、桥接、产物转换、知识库与模型路由的查询
    └── src/
        ├── finesub/           # llm/、它用到的几个 speech 模块、共用底座、run_post_stable
        └── finesub_bootstrap/ # 兼容包：只有纠错要的名字

src/nonoka_x/                  # 核心：对 FineSub 零引用
├── transcription/
│   ├── __init__.py            # EngineSpec、注册表、TranscriptionEngine 协议（WP0 已建）
│   ├── model.py               # transcript.json 的类型、校验与读写（WP0 已建）
│   ├── units.py               # Unit / Turn / Transcript：迁自 speech/joint 的内部模型（WP1）
│   ├── lines.py、stitch.py、cache.py、keys.py、audio.py、http.py   # 迁自 speech/joint（WP1）
│   ├── segmentation/          # Nonoka 自己的句子 DP（4.1 第 5 条）
│   ├── enhance/               # 需要 Python 环境层的可选修补
│   └── engines/               # mai2、gemini、moss_pro；没有 finesub.py
├── correction/                # 只有协议与注册表；没有 finesub.py
├── runtime/                   # 层管理器（WP-R2、WP-E3）
│   ├── __init__.py            # 组件（层）、状态、RuntimeManager 门面
│   ├── paths.py               # 目录布局
│   ├── manifests.py           # 读 resources/ 下的清单（WP-R1）
│   ├── layer_manifest.py      # 层的逐文件清单格式：构建流水线、闸门、层管理器共用（WP-E1）
│   ├── fetch.py               # 断点下载、校验、代理
│   ├── layers.py              # 下载、解压、切换版本、核对、修复、清理（WP-E3）
│   └── fsops.py               # 原子写、文件锁、进程树
├── config/                    # settings.json、secrets.json（WP-R4）
├── migration/                 # 旧版数据布局迁移（WP-M1、WP-M2）
├── engine_host.py             # 扫描 engines/、读 PACKAGE.json、构建进程环境与沙箱、按协议拉起包的进程
├── resources/                 # Nonoka 的清单、RUNTIME_PINS.json、layers.json（各层版本与地址）
└── worker.py                  # 只做编排：engine.transcribe() -> correction.run()

scripts/build_layers.py        # 构建流水线（WP-E1）
scripts/publish_layers.py      # 上传 R2（WP-E2）

third_party/finesub/           # 不存在
```

分包之前的过渡形态：FineSub 代码在 `third_party/finesub/`（只留核心文件，WP-R7），引擎与纠错的适配器在 `transcription/engines/finesub.py`、`correction/finesub.py`，桥接在 `src/nonoka_x/finesub_bridge.py`；worker 还是一个进程跑两半，用 vendor 里的 `finesub_bootstrap`；FineSub 的边界由 WP0 的边界测试看住（过渡白名单只许变短）。

| 部件 | 依赖 | 不依赖 |
|---|---|---|
| FineSub 转写引擎 | `run_transcription`、三层（Python 环境、模型、工具）、沙箱 | 纠错的任何东西 |
| finesub correction | `run_post_stable`、`finesub.llm`、启动器环境、兼容包 | 任何转写引擎的实现、任何层（声学说话人分离属于转写引擎，4.1 第 2 条） |

纠错注册表里目前只有 `finesub` 一项。留注册表是为了让边界清楚、能被测试约束，不是为了现在就做第二个。

### 5.1 协议

```python
@dataclass(frozen=True)
class EngineSpec:
    id: str                       # "finesub" | "finesub-cloud" | "mai2" | "gemini-transcribe" | "moss-pro"
    label: str
    kind: str                     # "local-gpu" | "cloud-api" | "cloud-service"
    features: EngineFeatures      # speakers, word_timestamps, separate, diarization_levels, languages
    keys: tuple[str, ...]         # 需要的密钥名
    environment: EngineEnvironment  # 必需与可选的层、GPU
    billing: str | None           # 计费说明（可选）
    # 派生属性 provider：cloud-service -> cloud，其余 -> local

class TranscriptionEngine(Protocol):
    spec: EngineSpec
    def readiness(self, context: ReadinessContext) -> Readiness: ...
    def transcribe(self, job: TranscriptionJob, report: Reporter) -> TranscriptionResult: ...

class CorrectionEngine(Protocol):
    spec: CorrectionSpec          # id: "finesub"
    def readiness(self, context: ReadinessContext, request) -> Readiness: ...
    def run(self, job: CorrectionJob, report: Reporter) -> CorrectionResult: ...
```

- 契约全部做成纯 JSON 可往返（`to_dict`/`from_dict`）；取消检查放在 `Reporter` 上（`report.cancelled()`）。WP-P1 由 `engine_host` 给每个包生成实现同一协议的代理对象（WP0 的决定，附录 A.1）。
- `TranscriptionJob`：源文件、语言、任务目录、缓存目录、目标阶段、引擎选项。
- `TranscriptionResult`：`transcript_json` 路径、`native` 产物路径表、`metadata`。`native` 是核心不解析的不透明附件，键是 `<engine>.<name>`：FineSub 引擎交出 `finesub.stable_json`、`finesub.diar_json` 等；云端续跑下载到的 stable 也按这两个键进来。
- `CorrectionJob`：`TranscriptionResult`、源媒体路径、目标阶段（`raw-srt` / `translated-srt` / `final-srt`）、纠错选项、知识库选项、说话人级别、任务目录。
- `CorrectionResult`：产物清单。WP-P1 之前键名与现在的 ArtifactManifest 一致；WP-L 之后改为 Nonoka 的格式（`correction.json` 等），Projector 随之改（4.8 第 38 条）。
- finesub correction 负责 stable 之后的全部：写说话人行标签、LLM 纠错翻译、后处理。目标是 `raw-srt` 时也走它，只是不调 LLM；分包之后原始字幕的渲染改由核心做。

### 5.2 `transcript.json`

转写层交给纠错层的唯一必备产物：

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

规则（WP0 已实现）：`engine`、`language`、`duration`、`lines` 必填，`model`、`metadata` 可缺；行 `id` 非空且唯一，`0 ≤ start ≤ end`，`speaker` 若有须非空；`words` 每项 `start ≤ end`；行可重叠、不强制排序；未知字段保留并原样写回；缺省字段不写。

finesub correction 拿到后：

- 有 `native["finesub.stable_json"]`：**原样使用**这份 stable（连同 `finesub.diar_json`），一个字节都不改写；说话人来源是声学时间线。
- 否则：把 `lines` 转成 stable JSON，`speaker` 写成 `line_speaker`，调 `run_post_stable` 时指明说话人来自行标签、裁判听源混音。

FineSub 转写引擎也要写 `transcript.json`（从它的 stable 投影出来，只读 stable，不改它）。

### 5.3 请求与能力声明

`TaskRequest` 增加 `"transcription": {"engine": "mai2", "options": {}}`，缺省视为 `finesub`；`correction.engine` 缺省为 `finesub`，界面不提供选择。旧请求和已落盘的任务不用迁移。

`Capabilities` 增加：

```json
"transcription_engines": [
  {"id": "mai2", "label": "MAI-Transcribe-2", "kind": "cloud-api",
   "ready": false, "issues": [{"code": "missing_key", "message": "…"}],
   "features": {"speakers": true, "word_timestamps": true, "separate": false,
                "diarization_levels": ["off", "acoustics", "judge-text", "judge-audio"]},
   "billing": "按音频时长计费，约 $0.10/小时"}
],
"correction_engines": [{"id": "finesub", "ready": true, "issues": []}]
```

`runtime.stages` 里 `raw-srt` 的就绪改为按所选引擎判断：云 API 引擎只要工具层和密钥。

## 6. 工作包

```text
已完成：WP-Q、WP-A、WP0、WP-R0、WP-R2、WP-B、WP-R1、WP1、WP4a、WP4b、WP4c、WP3a/b/c、WP-C、WP-P0、WP-M1（最后一条完成标准还没做，WP-E3 已完成、可以做了）、WP-R4、WP-K、WP-E1、WP-CL0、WP5、WP-E2、WP-E3、WP-R5、WP-R6、WP6、WP-R7、WP-P1、WP-H、WP-L、WP-CL、WP-M2、WP7、WP-UI

引擎线：
WP-B ──┬─► WP1 ──► WP3a / WP3b / WP3c ───────────────┐
       ├─► WP4a ─┐                                    │
       └─► WP4b ─┴─► WP4c ────────────────────────────┤
WP-R4 ──► WP5 ──► WP6 ────────────────────────────────┤
WP7（文档部分随时，云端部分等 WP-B）───────────────────┴─► WP8

运行环境线：
WP-R1（清单、去 onnx）──► WP-E1（构建流水线）──► WP-E2（R2 发布）──┐
WP-R2（下载与文件设施）──┬─────────────────────────────► WP-E3（层管理器）
                         └─► WP-M1（迁移核心）──► WP-R4（配置与密钥）
WP-E3、WP-R4 ──► WP-R5（桥接）
WP-E3 ──► WP-R6（桌面、脚本、离线包）
WP-A、WP-B、WP-R5、WP-R6、WP-CL0、WP-C ──► WP-R7（删自下载、vendor 只留核心）

分包线：
WP-B、WP1 ──► WP-P0 ──► WP-K（知识库访问点改协议）─┐
WP4c、WP5、WP-R7 ──────────────────────────────────┴─► WP-P1 ──┬─► WP-H（密闭与沙箱）
                                 ├─► WP-L ──► WP-M2（迁移集成）
                                 └─► WP-CL（云端重做）

云端：WP-C（结构与统计，阈值经批准）──► WP-CL0（预热脚本）──► WP-R7；WP-R7、WP-CL、WP8 各跑一次 --cloud
收尾：WP-UI（等 WP-P1、WP-L、WP-H、WP6、WP-E3）──► WP8
交叉：WP-M1 的最后一条完成标准已不再等别的包（WP-E1、WP-E3 已完成）；WP-M2 等 WP-E3、WP-R4、WP-R5、WP-R7、WP-P1、WP-L。
```

**发版约束**：一次性发布（用户 2026-11-11，附录 B 第 14 问）。改变磁盘布局或运行环境的工作包（WP-E3、WP-R4、WP-R5、WP-R7、WP-P1、WP-H）、WP-M1、WP-M2 和引擎线进同一个版本；不能发一个「布局已改、迁移工具还没有」的版本。另加一条：**发版前，这个版本要的每一层都已上传 R2，并从外网（不经代理）下载、校验通过**，否则不发（原为经代理和不经代理各一次，用户 2026-11-13 删去经代理的一次，附录 B 第 68 问）。

**开工前提**：真实样本基线在 `test_data/` 下不进仓库，只在本机有；需要 `--real` 的工作包要在本机的主检出里跑。WP-E1 产出的层也只在本机（`dist/layers/`，不进仓库）。

**写文件的规矩**：两个子代理不要同时写同一个文件；每个包列出的文件归它独占。**只有 WP-B、WP1 和 WP-R7 可以写 `third_party/finesub/src/`，且只限第 0 节解锁的四处：WP-B 写 `stages.py`、`pipeline.py`，WP1 搬走 `speech/joint/`，WP-R7 改 `finesub_bootstrap/` 的自下载（WP-R1 改它的两份锁，只删 `onnx` 一项）。WP-P1 把整个目录原样分装进两个引擎包，只搬不改。**

两条线靠 WP0 定的门面解耦：引擎线的新代码取密钥、查运行时一律走 `nonoka_x.config` 和 `nonoka_x.runtime` 的门面，不直接碰 `settings.py`、`provision.py`。门面先由现有实现垫着，运行环境线再把底下换掉。

### WP-Q 质量基线与回归闸门（已完成 2026-11-11）

**用法**

```text
D:\Envir\Python312\python scripts/quality_gate.py          # 不用 GPU 的各项，约 25 秒
D:\Envir\Python312\python scripts/quality_gate.py --real   # 再加真实样本与已装运行时实测，约 3 分钟
D:\Envir\Python312\python scripts/quality_gate.py --cloud  # 云端（WP-C；基线未录成）
```

**WP-R1 之后**，本机已装的运行时与新锁对不上（锁里去掉了 `onnx`），`--real` 改用按新锁装的 PBS 环境，跑之前设 `NONOKA_QUALITY_PYTHON=D:\Development\nonoka-sub-x\.tmp\pbs-3.12.1\python\python.exe`（附录 B 第 27 问；模型与工具仍只读地取自已装运行时）。WP-E1 出层后改为指向层。**WP-E3 合并后（2026-11-13）**，旧布局（`%APPDATA%\Nonoka X\finesub\…` 的 venv）不再被认，`--real` 改为设 `NONOKA_QUALITY_DATA_DIR`、`NONOKA_QUALITY_INSTALL_DIR` 指向一个装好三层的数据目录和大文件根（不再设 `NONOKA_QUALITY_PYTHON`）；索引默认读 `resources/layers.json`，测别的版本时用 `NONOKA_QUALITY_LAYERS_INDEX` 指向另一份。现成的一份是 WP-E3 从 R2 装的：数据目录 `C:\Users\Chika\AppData\Local\Temp\nonoka e3 r2\data`、根 `C:\Users\Chika\AppData\Local\Temp\nonoka e3 r2\runtime`（在 Temp 下，被清掉就用层管理器重装）；WP-M1 的最后一条完成标准做完后改为开发机自己的新布局。跑过 `--real` 的 Python 层会多出 numba 的 46 个缓存文件，此后对它跑 `--layers` 会报这 46 处，在 WP-R5 / WP-H 设好 `NUMBA_CACHE_DIR` 之前属已知情况。几个会话同时工作时，跑 `--real` 前先 `mkdir .tmp\real-gpu.lock` 抢锁（里面写 `owner.txt`），跑完删掉。

退出码 0 通过、1 有一项失败、2 没有失败但有「未判定」的产物。每项失败都会指出哪个场景、哪个算法入口的哪个参数、或哪个文件不一样，以及基线文件的位置。`--record <calls|environment|replay|real|cloud>` 用当前代码重录某项基线，只能在用户批准后单独提交（第 0 节第 7 条）。

**各项与基线**

| 检查 | 比什么 | 基线 |
|---|---|---|
| 质量锁 | 550 个上锁文件的 sha256，不多不少 | `third_party/finesub/QUALITY_LOCK.json`；`tests/test_finesub_quality_lock.py` |
| 运行时钉住内容 | 两份 pylock 的 98 个包（WP-R1 后 97 个；版本、wheel/sdist 哈希）、7 个工具、11 个模型、下载源、`model_caches`/`artifacts` 常量；上锁引擎对 `finesub_bootstrap` 的 48 个名字都可解析。`--real` 时再实测已装运行时 | `src/nonoka_x/resources/RUNTIME_PINS.json`、`tests/fixtures/quality/bootstrap-surface.json`；`tests/test_runtime_pins.py` |
| **预制层**（WP-E1 加） | 每一层的逐文件清单对照 `RUNTIME_PINS.json`：解释器构建的 sha256；每个 `*.dist-info` 的名称与版本对锁；每个包装出的文件对它 `RECORD` 里的哈希，`site-packages` 里没有不属于任何 `RECORD` 的文件（预编译的 `.pyc` 与列明的例外除外）；构建记录里 uv 校验过的 wheel 哈希对锁；模型文件对模型清单；工具版本与哈希对工具清单。给一个已解压的层目录时逐文件重算 sha256 | `RUNTIME_PINS.json`；`--layers <层目录或清单>` |
| 调用等价 | 32 个请求场景，记录上锁算法入口收到的参数、纠错时的模型路由和产物清单 | `tests/fixtures/quality/calls/`；`tests/test_finesub_call_equivalence.py` |
| worker 环境与派生文件 | FineSub 读的环境变量（路径按角色记），`FINESUB_CONFIG_FILE`、`FINESUB_MODEL_CATALOG`、`FINESUB_ENV_FILE` 指向的文件内容。启动器那部分（`PYTHON*`、`PATH` 等）只记录不比对。`85ae2b9` 起含 `FINESUB_SEPARATOR_ACCEL=0`、`FINESUB_REFEREE_ACCEL=0` | `tests/fixtures/quality/environment.json` |
| LLM 回放 | 10 个用例、30 个请求，在 `llm_runtime.chat_complete` 截获，比提示词全文、路由与调用参数；用确定性回答跑完，比 `final.srt`、`annotated.csv`、`translated.srt` | `tests/fixtures/quality/replay/`（仓库内 5 个）、`test_data/quality-baseline/replay/`（真实样本 5 个，不进仓库） |
| 真实样本 | 两个样本跑到 `raw-srt`（多人样本开 `acoustics`），与基线按第 0 节第 3 条比对；运行元数据里的加速状态必须是 eager | `test_data/quality-baseline/real/`（eager，`85ae2b9` 时重录；不进仓库） |
| 沙箱写入（WP-H 加） | 转写进程运行前后，沙箱之外的受保护位置（实验报告 1.0 的清单，加上各层目录与引擎源码目录）新增、删除、修改都是 0 | `--real` 时实测 |
| 云端（WP-C） | 产物齐全、格式合法，段数、文字距离、说话人数等统计量在用户批准的阈值内（第 0.1 节第 3 条） | `test_data/quality-baseline/cloud/`（未录成） |

**录制条件**：基线都在干净环境下录（2.6）。不用 GPU 的检查在子进程里去掉一切可能有影响的变量，用户目录也换成空目录；真实样本只去掉变量，保留用户目录。

**后续工作包用它时要注意**

- 调用等价和回放的入口是 `validate_request`、`FineSubSettings`（`update_keys`、`set_cloud_account`）、`LocalProvider._engine_environment()`、`nonoka_x.worker.main`。哪个工作包改掉了其中一个，就由它改 harness（`tests/quality_harness/`）去调新的等价入口，**基线不动**；改完闸门仍须全部通过。
- 真实样本的运行方式跟着 `LocalProvider._default_worker_command` 和 `_engine_environment` 走；运行环境换成层以后，由 WP-E3 让 `tests/quality_harness/real.py` 和 `pins.py` 找到层里的解释器和模型（只改 harness 的定位，基线不动）。
- `RUNTIME_PINS.json` 现在由 `finesub_bootstrap` 的原件推导；WP-R1 把推导来源换成 Nonoka 的清单。
- 没覆盖到的地方：本地 Agent CLI 与 Gemini 原生路由、媒体上传、联网检索、已装工具的 PATH 派生；真实模型跑到 `final-srt` 的效果（WP8 人工对照）。
- **回放基线是在没有 `tokcount`、没有 `opencc` 的系统 Python 下录的**，而用户实际运行时两者都有。用户 2026-11-11 决定保持现状，不补录。所以「`tokcount` 有没有被找到」「`opencc` 有没有生效」闸门看不见，由 WP-H 和 WP8 专门确认。
- `--real` 对已装运行时的实测偏松（包只比版本）；WP-E1 的「预制层」一项补上了逐文件的比对。

### WP-C 云端基线（已完成 2026-11-12）

已完成。交付摘要与原始说明移到 `docs/transcription-engines-plan-done.md` 的同名一节。

### WP-A 拆掉上游对齐机制（已完成）

已完成。交付摘要与原始说明移到 `docs/transcription-engines-plan-done.md` 的同名一节。

### WP-B 拆分 `run_pipeline`（已完成 2026-11-11）

已完成。交付摘要与原始说明移到 `docs/transcription-engines-plan-done.md` 的同名一节。

### WP0 契约与骨架（已完成）

已完成。交付摘要与原始说明移到 `docs/transcription-engines-plan-done.md` 的同名一节。

### WP1 迁出 `speech/joint/`（已完成 2026-11-11）

已完成。交付摘要与原始说明移到 `docs/transcription-engines-plan-done.md` 的同名一节。

### WP3a MAI-Transcribe-2

已完成。交付摘要与原始说明移到 `docs/transcription-engines-plan-done.md` 的同名一节。

### WP3b Gemini 3.5 Transcribe

已完成。交付摘要与原始说明移到 `docs/transcription-engines-plan-done.md` 的同名一节。

### WP3c MOSS Pro

已完成。交付摘要与原始说明移到 `docs/transcription-engines-plan-done.md` 的同名一节。

### WP4a FineSub 转写引擎（已完成 2026-11-11）

已完成。交付摘要与原始说明移到 `docs/transcription-engines-plan-done.md` 的同名一节。

### WP4b finesub correction（已完成 2026-11-11）

已完成。交付摘要与原始说明移到 `docs/transcription-engines-plan-done.md` 的同名一节。

### WP4c worker 编排（已完成 2026-11-12）

已完成。交付摘要与原始说明移到 `docs/transcription-engines-plan-done.md` 的同名一节。

### WP5 请求校验、就绪检查、密钥（已完成 2026-11-13）

已完成。交付摘要与原始说明移到 `docs/transcription-engines-plan-done.md` 的同名一节。

### WP6 桌面界面与 Go（已完成 2026-11-16，含第二阶段设置页重组）

已完成。交付摘要与原始说明移到 `docs/transcription-engines-plan-done.md` 的同名一节。

### WP-R0 运行时与配置的接口调查（已完成）

已完成。交付摘要与原始说明移到 `docs/transcription-engines-plan-done.md` 的同名一节。

### WP-R1 Nonoka 自己的清单，去掉 `onnx`（已完成 2026-11-11）

已完成。交付摘要与原始说明移到 `docs/transcription-engines-plan-done.md` 的同名一节。

### WP-R2 下载与文件设施（已完成 2026-11-11）

已完成。交付摘要与原始说明移到 `docs/transcription-engines-plan-done.md` 的同名一节。

### WP-E1 预制包构建流水线（已完成 2026-11-13）

已完成。交付摘要与原始说明移到 `docs/transcription-engines-plan-done.md` 的同名一节。

### WP-E2 R2 托管与发布（新；已完成 2026-11-13）

已完成。交付摘要与原始说明移到 `docs/transcription-engines-plan-done.md` 的同名一节。

### WP-E3 层管理器（新，取代原 WP-R3；已完成 2026-11-13）

已完成。交付摘要与原始说明移到 `docs/transcription-engines-plan-done.md` 的同名一节。

### WP-R4 配置与密钥存储（已完成 2026-11-13）

已完成。交付摘要与原始说明移到 `docs/transcription-engines-plan-done.md` 的同名一节。

### WP-R5 FineSub 桥接（已完成 2026-11-16）

已完成。交付摘要与原始说明移到 `docs/transcription-engines-plan-done.md` 的同名一节。

### WP-R6 桌面、脚本、离线包与文档（已完成 2026-11-16）

已完成。交付摘要与原始说明移到 `docs/transcription-engines-plan-done.md` 的同名一节。

### WP-R7 删自下载，vendor 只留核心文件（已完成 2026-11-17）

已完成。交付摘要与原始说明移到 `docs/transcription-engines-plan-done.md` 的同名一节。

### WP-M1 迁移核心（重写；除最后一条完成标准外已完成 2026-11-12；最后一条 2026-11-13 起可以做）

> 交付：`python -m nonoka_x.migration [--dry-run] [--json] [--yes] [--data-dir] [--checkout]`；旧根在同一父目录下改名为 `runtime`，永不跨盘复制；新根已存在、有未结束的任务、记录的旧根不存在、旧根或模型目录里有目录链接时阻断。11 步（设置、知识库、限流状态、任务产物、模型补齐、下载新层留接口跳过），每步写进 `runtime-state\migration.json`，每个操作点被杀后都能续上。边界测试加了 `LEGACY_LAYOUT` 放行，只许 `migration/legacy.py` 写出旧文件名。本机 `--dry-run`：旧根 10.21 GiB，搬 4.00 GiB 模型，删 6.22 GiB（`accel\` 只有 5.94 MiB）。交回 WP-M2：桌面未完成的存储搬家只在目标旁留 `.nonoka-partial`，Python 侧查不到；日志里已跳过的步骤怎么补跑。最后一条完成标准（真把开发机搬过去）等 WP-E1、WP-E3。下面是原始说明。

等 WP-R2（`runtime/paths.py`）做完就开工。

- 文件：`src/nonoka_x/migration/**`、`tests/test_migration.py`。
- 内容：按 4.4 第 25 条和 R0 文档 2.7（D10、D23 两条作废）。
  - 一个命令行入口，不接进 sidecar 启动，不碰界面。
  - 探测旧布局（R0 文档 1.13 的清单，含 `storage.json` 指向别的盘的大文件根和开发检出 `.local`）。没有旧布局就什么都不做。
  - 先出迁移计划（搬什么、删什么、要下载多少、目标位置），再执行；提供只读的 `--dry-run`。
  - 模型：旧 `models\` 同盘改名进模型层的位置，删 `accel\`；交给 WP-E3 的模型补齐（WP-E3 之前这一步跳过并说明）。
  - 清空旧的 Python 环境、工具、缓存（4.4 第 25 条的清单）；系统 Python、`%LOCALAPPDATA%\FineSub`、`bootstrap\`、`app-resources\` 不碰。
  - 下载新层的一步留好位置，实现是 WP-E3；设置和密钥一步留好位置，实现是 WP-R4 的 `config/migrate.py`。在那之前这两步跳过并说明。
  - 任务、文档、知识库的数据内容一字不改。
- 完成标准：
  - 在一份旧布局副本上（含大文件根在另一个盘的情形、有 `python-builds` 的情形、模型不全的情形）跑完，文件各就各位，该删的删了，没有任何从 Hugging Face 的下载；`--dry-run` 不动任何文件；各有测试。
  - 用它把开发机的安装搬到新布局，WP-E3 完成后由 WP-E3 补齐模型并装上 WP-E1 构建的 Python 环境层和工具层，`quality_gate.py --real` 通过。

### WP-M2 迁移的启动集成、续跑与回滚（已完成 2026-11-19）

已完成。交付摘要与原始说明移到 `docs/transcription-engines-plan-done.md` 的同名一节。

### WP-P0 分包清单（已完成 2026-11-12）

已完成。交付摘要与原始说明移到 `docs/transcription-engines-plan-done.md` 的同名一节。

### WP-K 知识库访问点改走协议（已完成 2026-11-13）

已完成。交付摘要与原始说明移到 `docs/transcription-engines-plan-done.md` 的同名一节。

### WP-P1 分装成两个引擎包（已完成 2026-11-18，实为三个包）

已完成。交付摘要与原始说明移到 `docs/transcription-engines-plan-done.md` 的同名一节。

### WP-H 密闭运行与沙箱（已完成 2026-11-18）

已完成。交付摘要与原始说明移到 `docs/transcription-engines-plan-done.md` 的同名一节。

### WP-CL0 云端模型预热脚本（已完成 2026-11-13）

已完成。交付摘要与原始说明移到 `docs/transcription-engines-plan-done.md` 的同名一节。

### WP-CL 云端重做：只留转写包（已完成 2026-11-18）

已完成。交付摘要与原始说明移到 `docs/transcription-engines-plan-done.md` 的同名一节。

### WP-L 媒体库同步改用 Nonoka 的格式（已完成 2026-11-18）

已完成。交付摘要与原始说明移到 `docs/transcription-engines-plan-done.md` 的同名一节。

### WP-UI 引擎配置页（已完成 2026-11-19，界面由用户自验）

已完成。交付摘要与原始说明移到 `docs/transcription-engines-plan-done.md` 的同名一节。

### WP8 集成验证

> 进度（2026-11-19）：静态核对已做——引擎包里的 FineSub 文件相对 `1f14db3` 只在批准范围内变过（`stages.py`、自下载两处、两份锁去 `onnx`、WP-K 的 18 个知识库访问点、纠错与知识库包的兼容包），`speech/joint/` 不在任何包里，`third_party/finesub` 已不存在（git 里没有文件，本地残留 2026-11-19 已清掉）。`src/nonoka_x/` 没有 `import finesub`，但字符串 `finesub` 出现在 95 个文件里（引擎 id、附件键、注释），这条完成标准怎么理解要问用户。其余各项都还没做，见主会话笔记目录 `wp8-progress.md`；2026-11-19 按用户要求清掉了全部临时数据（含 `--real` 的运行环境与迁移验收副本），接手时要先重建，见笔记目录 `main-session-handoff.md`。

> 补充（2026-11-19，第 92 问）：`--real`（含原属 WP-M2 的一次）、`--cloud`、线上 LLM 那一程（WP-CL 没实跑）、三个云引擎的真实调用都在本包各做一次；迁移一项沿用 WP-M2 阶段 2 在真实安装副本上的验收（笔记目录 `wp-m2-delivery.md`），另挑一个改写过的旧云端任务点重试；WP-M1 最后一条在用户换新版时作为迁移一项的实测；跑 `--real` 前先修好或绕开 `scripts/build_layers.py stage` 的过时之处（见归档 WP7 一节的交回）。

- 全量测试：`D:\Envir\Python312\python -m pytest -q`（仓库根）、`cd desktop/frontend && npm test && npm run typecheck`、`cd desktop && go test ./...`、`cd nonoka-sub-cloud/modal_compute && python -m pytest -q tests`。
- 最终闸门：`scripts/quality_gate.py --real`、`--layers`（对 R2 上要发布的层）和 `--cloud` 全部通过；用纠错进程跑的纠错里 `token_counter_source` 仍是 `tokcount`；两个引擎包里的 FineSub 文件相对 `1f14db3` 只有 `stages.py`、`pipeline.py`、`finesub_bootstrap` 的自下载部分变过，`speech/joint/` 不在，其余每个文件的哈希等于 WP-Q 的 `QUALITY_LOCK.json`；`third_party/finesub/` 不存在；`src/nonoka_x/` 里搜不到 `finesub`。
- 可删性：实际删掉 `engines/finesub-transcribe/`、删掉 `engines/finesub-correct/`、两个都删，各启动一次应用并跑一个任务，结果符合 4.6 第 33 条。
- 可替换性：不装 Python 环境层、不注册 FineSub 转写引擎时，用一个云引擎加 finesub correction 跑到 `final-srt`；反过来 FineSub 转写引擎的结果交给 finesub correction 以外的实现（测试用假实现）也能走完。
- 迁移：在一份真实的旧版用户数据目录副本上装新版，迁移后照常使用，模型没有重下，三层从 R2 下载装好，主数据目录里除 `engines\` 外没有 FineSub 相关文件。
- 新用户：在一台干净的 Windows（没有系统 Python、没有开发者模式）上全新安装，从 R2 装好三层（经不经代理不限，附录 B 第 68 问），跑一个任务到 `final-srt`。
- FineSub 路径端到端：一个真实样本用真实模型跑到 `final-srt`，把字幕交给用户和基线对照着看一遍。闸门证明的是「发给模型的东西没变」，这一步是给人看最终效果。
- 真实调用：三个云引擎各跑一个 `test_data/audio` 样本到 `final-srt`，分别在装了和没装 Python 环境层的环境下。需要用户提供密钥，没有密钥的引擎如实标记为未验证。

## 7. 约定

- 提交信息不加 Co-Authored-By，不 push。
- Python 测试用系统解释器 `D:\Envir\Python312\python`（开发机上；用户机器上不再用系统 Python）。`ruff` 现有 13 个旧错误不算回归。
- 每个工作包交付前跑 `scripts/quality_gate.py`；动了识别、运行环境、环境变量或目录布局的包再跑 `--real`；动了层的再跑 `--layers`。
- 上锁文件不许写。保留 FineSub 的 `LICENSE`、`LICENSES.json`、`PROMPT_LICENSE.md`。
- 每个工作包的交付说明里写明闸门结果；没跑或没过要如实说。
- 计划没写清楚、有两种以上合理做法、或一项新决定和旧条款冲突时，停下来问用户，不要自己定了再告知。

## 8. 风险与取舍

| 风险 | 影响 | 应对 |
|---|---|---|
| 拆 `run_pipeline` 时某个阶段的参数、跳过条件或默认值被带偏 | FineSub 的转写或纠错结果变化 | WP-B 只做搬移；调用等价测试的记录点在编排之下；真实样本按第 0 节第 3 条比对；单独评审 |
| 转写产物本身不可逐字节复现 | 闸门没法用逐字节比对 | WP-Q 已查实（2.6）；用户批准了替代标准（第 0 节第 3 条） |
| 闸门只覆盖选中的样本和请求组合 | 没覆盖到的组合里可能有差异 | WP-Q 的请求矩阵覆盖所有目标阶段和开关；WP8 再加一次人工对照 |
| 迁出的 `speech/joint/` 从此不跟上游 | 上游对这几个后端的修补要自己看着搬 | 这是本次的明确取舍 |
| 没装 Python 环境层的时候云引擎缺少空档重问和声纹重连 | 偶发漏段不会被补；续问后同一人可能被记成两个说话人；MOSS Pro 行内时间是插值的 | 只影响自研引擎，不影响 FineSub 路径；任务日志里说明，装了自动启用 |
| 没有了「可由基线加补丁完整重建」的保证 | 无法机械地还原出纯上游代码 | `LOCAL_CHANGES.md` 记下所有本地改动；质量锁看住算法文件 |
| **预制层的内容与钉住的不一致**（构建时某个包装错、多了或少了文件） | 所有用户拿到同一个错误的环境，识别结果变化 | 「预制层」闸门逐文件比对 `RUNTIME_PINS.json`；两次构建清单相同才发布；层管理器在用户机器上按同一份清单核对；`--real` 用层跑 |
| **构建机不可信**（开发机被入侵或被污染） | 有问题的东西被所有用户直接运行 | 构建输入只有钉住的清单，每个下载都按哈希校验；干净目录第二次构建、清单必须相同；对象按内容哈希命名、不可变，`layers.json` 随桌面版本发布、写死各层版本；R2 写凭据只在本机。残余风险：本机本身被入侵时两次构建都可能被污染，这一点只能靠本机的安全保障 |
| **托管流量、费用与盗链** | 每个新用户约 5.8 GiB；公开地址被盗链会推高请求费用 | R2 不收出口流量费、按请求计费；速率限制挡盗链；手册里写明看用量的地方和估算方法（WP-E2）；离线包作为不依赖 R2 的途径 |
| R2 或自定义域名不可用、国内访问慢 | 新用户装不上环境，迁移后的老用户用不了 FineSub | 依靠用户的本地代理（用户决定）；断点续传；离线包；发版前从外网（不经代理）下载校验一次（附录 B 第 68 问） |
| **层的版本与增量更新**：第一版整层替换 | 改一个包也要重下 2.45 GiB | 改钉住的内容本来就要用户批准，频率低；模型层很少变；增量留到以后。迁移时模型有缺或不对也是下整个模型层压缩包（附录 B 第 64 问） |
| **先删后下**的迁移 | 从清空旧环境到下载完成之间 FineSub 转写不可用；下载失败时没有旧环境可回退 | 用户的决定；界面显示进度与说明；断点续传、重启后接着下、可重试；工具层先装好，云 API 引擎先可用；离线包可以代替下载 |
| 迁移搬来的旧模型不全或损坏 | 引擎缺模型报错 | 层管理器按清单逐文件核对，只补缺的和不对的；补齐前不登记为就绪 |
| 符号链接换成实文件后，加载器或 `hf_verify` 不认这种布局 | 模型加载失败或悄悄走了别的路径 | WP-E1 先实测再 `--real`；`HF_HUB_OFFLINE=1` 让懒下载失败而不是悄悄联网 |
| **沙箱漏写**：注册表、`C:\Windows\Temp`、Known Folder API、`HOMEDRIVE`/`HOMEPATH` | 引擎仍可能写到沙箱之外，或读到沙箱之外的东西 | 列为边界（4.5 第 31 条）；WP-H 的沙箱写入检查覆盖实验报告的受保护位置、层目录和源码目录；`HOMEDRIVE`/`HOMEPATH` 由 WP-H 实测后交用户定 |
| 两个转写进程同时跑时共用沙箱用户目录 | 缓存目录互相写 | 只共用缓存类目录（ComputeCache、内核缓存，本身支持并发）；TEMP 每进程一份；WP-H 加一个两进程并发的测试 |
| **关闭加速后的速度** | 人声分离慢约 25–30%（实验里 `sample_ja` 76 秒对 57–60 秒），长视频更明显 | 用户的决定（结果确定、不依赖 MSVC）；`CHANGELOG.md` 写明；云端不受影响 |
| 用户机器的 VC++ 运行库缺失或太旧 | torch 加载失败（`WinError 126/1114`），而且要在下载完 2.45 GiB 之后才暴露 | 下载前检查（`msvc_runtime.py`），措辞照旧；是否随层带上运行库见附录 C |
| PBS 3.12.1 是老版本，不再有安全更新 | 解释器本身的已知漏洞 | 用户选择与现状同版本；升级解释器属于改钉住的内容，以后单独做 |
| 去掉 `onnx` 后环境与用户机器上现有的不同 | 现在每台机器的 `onnx/` 是随机混合的结果，新环境是确定的 `onnx-weekly` | 用户批准；`--real` 在去掉后的环境上验证；FineSub 路径不 import `onnx` |
| 重建进程的 `PATH` 时丢了 `tokcount`，或纠错环境里没了 `opencc` | token 计数退回估算，分窗和提示词变化；繁简折叠失效。回放基线不含这两条路径，闸门抓不到 | WP-H 和 WP8 专门核对 `token_counter_source` 与 `opencc` |
| 派生的 `finesub.toml` 漏了某个键，引擎悄悄用了默认值 | 质量相关的设置失效 | 调用等价测试比对派生文件。迁移时 Nonoka 不认识的键按用户决定丢弃，迁移报告列名，受影响的用户行为回到默认值（D11c） |
| 引擎运行时自己改写配置或密钥文件 | 派生文件与 Nonoka 的存储不同步 | 每进程一份派生文件、进程结束即删（D6）；`FINESUB_ENV_PROTECT=0`（D22）；派生 `.env` 里没有密钥（D3） |
| 迁移旧配置时丢密钥或丢设置 | 用户升级后要重新配置，或任务失败 | 旧文件留作备份；迁移往返测试；设置那几步失败时回滚并提示 |
| 纠错兼容包与原 `finesub_bootstrap` 语义有出入（token 计数、密钥读取、产物文件名、默认位置） | 纠错结果悄悄变化 | 名单与可解析性测试；`RUNTIME_PINS` 的常量；调用等价与 LLM 回放（回放里 token 计数方式不同会让分窗和提示词变化，逐字节能抓到）；WP-H 的 `token_counter_source` 核对 |
| 删掉自下载时误改了模型齐全时的行为 | 加载的 revision 或路径变化 | WP-R7 只改缺失分支；`--real`、`--cloud` 都要过 |
| 删掉自下载后云端缺模型 | 云端任务失败 | WP-CL0 先把云端换成独立的预热脚本，WP-R7 排在它之后；`--cloud` 验证 |
| 上游升级改了引擎对 `finesub_bootstrap` 的用法 | 转写包：要重新做删自下载的改动；纠错兼容包缺名字 | 依赖面测试先失败；`LOCAL_CHANGES.md` 记着改法 |
| 迁移中断（断电、杀进程、磁盘满） | 数据目录半新半旧 | 迁移日志可续；设置与数据那几步可回滚；同盘只改名；动手前先算空间（含要下载的量） |
| 用户迁移后又装回旧版本 | 旧版本找不到搬走的数据，旧环境也已删掉 | 迁移前的配置文件留作备份；更新说明写明不支持降级 |
| 布局改了但迁移工具没跟上同一版本 | 老用户升级后数据「消失」 | 发版约束：一次性发布 |
| 环境收得太紧，漏掉某个系统必需的变量 | torch 找不到 DLL；DPAPI 读不出密钥；本地 Agent CLI 起不来 | R0 文档 1.7 先列清；WP-H 逐项验证 |
| 环境收得不够，分类表漏了某个库读的变量 | 仍有外部影响 | 读取审计测试能抓到 FineSub 自己读的；第三方库在原生代码里读的抓不到，靠「其余一律不存在」兜住 |
| 用户以前靠系统环境变量调 FineSub（代理、HF 镜像、指定显卡、线程数） | 升级后这些设置不再生效 | 这是密闭的本意；常用的几项在 Nonoka 设置里提供，更新说明里写明 |
| 开发机本身不干净，基线带着外部影响 | 闸门以一个被污染的行为为准 | WP-Q 录基线前先清点并去掉外部变量 |
| 运行环境线和引擎线同时改 `local_provider.py` 一带 | 冲突 | 靠 WP0 的门面解耦；WP5 排在 WP-R4 之后；WP-H 排在最后 |
| 云端结果不可逐次复现，只用较弱的标准比 | 云端的细微质量变化闸门看不出来 | 用户的决定（云端保持现状）；阈值按实测的自然波动定并经用户批准；WP8 人工看一次 |
| 云端镜像仍按自己的方式装环境 | 本地与云端的环境不是同一份 | 用户的决定；云端本来就是 Linux 上另一套装法 |
| `correction.json` 漏了投影原先从 `annotated.csv` 读的某一项 | 取回或重新投影后的字幕文档丢信息 | WP-L 先逐项列出再定格式；新旧投影对同一批产物结果相同 |
| 云端数据批量转换出错或中途失败 | 用户的云端媒体库条目打不开 | 先停写；原件挪到备份前缀不删；脚本可重跑；先在数据副本上跑通；校验不过就回退 |
| 强制升级期间有用户没升级，或升级失败 | 这些用户的同步被拒绝 | `minVersion` 让旧版本自动安装新版；Worker 对旧格式请求返回明确提示；本地数据不受影响 |
| 切换之后才发现新格式有缺陷 | 新旧格式都已在线上，回退困难 | 备份前缀下的原件保留到确认无误；切换前在副本上完整演练一遍 |
| 分装时漏了文件，或某个文件只在特定分支才被 import | 包在特定任务上才报缺模块 | WP-P0 用静态图加运行时记录取并集；WP-P1 要求 550 个文件逐个有去向；闸门在分装后的包上全跑 |
| 两段进程之间交接的产物与原先同进程时不同 | FineSub 默认路径的纠错输入变化 | stable 作为私有附件原样交接；`--real` 和 LLM 回放比对 |
| 共用底座有两份，日后只改了一份 | 两个包行为不一致 | 代码冻结；各自的质量锁；升级上游时由分装脚本从同一来源生成两份 |
| 私有附件被用户或清理工具删掉 | 纠错改从 `transcript.json` 重建 stable，输入与原生的不完全相同 | 任务日志里写明用的是哪种输入 |

## 附录 A 决定的来历

已移到 `docs/transcription-engines-plan-done.md` 末尾（时间线、旧条款编号对照、R0 文档 D1–D23 的现状）。

## 附录 B 向用户问过的问题与答复

2026-11-11 整理本版时提问。「推荐」是提问时给的建议；答复不同于推荐的已注明。

| # | 问题 | 答复 |
|---|---|---|
| 1 | 计划文档继续追加，还是整理成干净文档（保留来历） | 整理成干净文档（推荐项） |
| 2 | 迁移时「清空旧环境」的范围，以及先下后删还是先删后下 | 范围按提议：旧 Python 环境（`runtime\python`、`python-builds`）、旧工具（7 个）、缓存（`cache\`、uv 缓存、编译缓存）清空，模型搬走不重下，`%LOCALAPPDATA%\FineSub` 与系统 Python 不碰，模型里的 `accel\` 一起删。顺序：**先删后下**（推荐的是先下后删） |
| 3 | 纠错包怎么拿到 `finesub_bootstrap`（不在沙箱里，原样带会写进真实的 `%LOCALAPPDATA%\FineSub`） | 纠错包用兼容包，只做纠错要的名字，默认位置按 D21 改向（推荐项）。原先为它设计的密闭运行（WP-H）对纠错进程照做 |
| 4 | 锁里去掉 `onnx` 还是 `onnx-weekly` | 去掉 `onnx`（推荐项） |
| 5 | 云端基线用什么较弱的标准 | **只比结构和统计**（推荐的是按加速方式分组录两份基线、标准不放松） |
| 6 | 转写进程的密闭做到哪一步 | 沙箱 + 环境从零构建 + `-I`，三种证明两个进程都做（推荐项） |
| 7 | FineSub 引擎代码随什么下发 | 随桌面安装包（推荐项） |
| 8 | `.pyc` 怎么处理 | 构建时 `unchecked-hash` 预编译、运行时 `-B`（推荐项） |
| 9 | 模型层里的符号链接 | 构建时换实文件（推荐项） |
| 10 | 层的版本与更新 | 按内容定版本，整层替换，第一版不做增量（推荐项） |
| 11 | 云端统计标准的阈值怎么定 | WP-C 实测自然波动后提案，用户批准后再写进闸门（推荐项） |
| 12 | R0 文档 D1–D23 的现状标注（附录 A.3） | 同意（推荐项） |
| 13 | 预制包在哪里构建 | 本机，发布前干净目录第二次构建验证（推荐项） |
| 14 | 发版约束还按一次性发布吗 | 仍然一次性发布，另加「R2 上各层已上传并外网下载校验过」（推荐项） |
| 15 | 工具层怎么切，镜像与离线包怎么办 | **工具一层**，去掉 GitHub 镜像，离线包改装同样的层文件（推荐的是工具按组件分包） |
| 16 | 大文件根里层怎么放 | `runtime\layers\<层>\<版本>\` 加 `current.json`（推荐项） |
| 17 | R2 怎么对外提供下载 | 公开自定义域名，按内容哈希命名，速率限制挡盗链（推荐项） |
| 18 | 沙箱目录怎么分 | 用户目录跨运行共用，TEMP 每进程一份加锚点（推荐项） |
| 19 | Python 环境层用哪个 PBS 解释器 | 3.12.1（与现在相同，推荐项） |
| 20 | 去掉自下载与「云端保持现状」冲突怎么办 | 两边都删；云端部署前用独立预热脚本按清单把模型放进 Volume，缺模型报错（推荐项，WP-CL0） |
| 21 | 纠错兼容包在分包之前放哪 | 分包时再建，直接建在 `engines/finesub-correct/src/finesub_bootstrap/`（推荐项） |
| 22 | 迁移搬来的模型与模型层对不上时怎么办 | 哈希对上的留下，缺的或不对的按文件从 R2 补（推荐项） |
| 23 | cn 锁不复制进 Nonoka 的清单（原附录 C 第 5 项，WP-R1 开工前问） | 确认不复制；Nonoka 的清单只留标准锁，`finesub_bootstrap` 自带的 cn 锁照旧，WP-R1 只删其中的 `onnx`（推荐项） |
| 24 | WP-R2：`download_route.py`、`asset_digest.py` 运行时给 `finesub_bootstrap` 和 `finesub.api_http` 打的补丁（代理线路策略、应用代理策略、云端摘要源），不 import FineSub 就留不住，怎么办 | 补丁代码原样搬进新建的 `src/nonoka_x/finesub_bridge.py`（WP-R5 的文件，提前建），旧函数名保留并委托过去，调用方不改，行为不变（推荐项） |
| 25 | WP-R2：`runtime_install.py`、`provision.py` 还在用镜像/备用源阶梯，删镜像时调用方怎么办 | **连调用方一起删**，`tests/test_runtime_install.py` 里断言镜像的测试按新行为改；这三个文件加进 WP-R2 的清单，只删镜像那一块（推荐的是随 WP-E3 删） |
| 26 | WP-R1：锁变了以后本机已装的运行时被当成过期，还合不合并 | 合并（推荐项）。WP-E1 出层之前，`--real` 用按新锁装的 PBS 环境 |
| 27 | 那个 PBS 环境放哪 | 搬到主检出的 `.tmp\pbs-3.12.1`（git 忽略），各会话跑 `--real` 时设 `NONOKA_QUALITY_PYTHON` 指向它（推荐项） |
| 28 | 闸门的钉住检查和 `pins.py write` 还从 `finesub_bootstrap` 推导，`write` 会漏掉 `interpreter` | 改读 Nonoka 的清单并带上 `interpreter`，`finesub_bootstrap` 的两份锁仍在钉住范围内（推荐项） |
| 29 | `pyproject.toml` 只打包 `resources/*.json`，新锁进不了 wheel | 加上 `resources/*.toml`，并确认桌面暂存也带上它（推荐项） |
| 30 | VC++ 运行库继续依赖系统安装，还是随 Python 环境层带上（原附录 C 第 4 项） | 继续依赖系统安装：`msvc_runtime.py` 下载前检查并引导安装，列为密闭边界（推荐项） |
| 31 | macOS 的工具层在哪里构建（原附录 C 第 7 项） | **这次先不做 macOS**：WP-E1 只做 Windows 三层，macOS 工具层另定（推荐的是本机打包、Mac 上验证） |
| 32 | 模型层放哪些模型（原附录 C 第 6 项） | 由 WP-E1 先查清每条模型在什么设置下会被加载、云 API 引擎的可选增强要哪些，列表带大小交用户定，定了再构建（推荐项）；仍未定，见附录 C 第 6 项 |
| 33 | WP1：sbd 的断句模型（xlm-roberta，约 1.1 GB，不在模型清单里）本机没有，行为基线怎么录 | 下到主检出 `.tmp\sbd-hf\` 录真基线，未改动代码 + 已装运行时解释器（只读）录、PBS 环境里比；取模型的位置本包不动，放哪由 WP-E1 定（推荐项） |
| 34 | WP1：`enhance/`、`moss_local/` 依赖 FineSub 的 Silero 加载、声纹与 PLDA、`hf_weights` 自下载、device/resources | 用到的函数复制进 Nonoka，注明来源；模型目录由 `nonoka_x.runtime` 新增的 `model_dir()` 给，不自下载，缺模型就跳过增强（推荐项） |
| 35 | WP1：清单外的 `repair.py`、`stage.py`、`__init__.py`、`quota.py` 等放哪 | `transcription/repair.py`；`engines/joint_stage.py`（原样搬，WP3 拆）；后端名常量进 `engines/__init__.py`，`GPU_LOCK` 进 `enhance/__init__.py`；`quota.py` 并进 `keys.py`；`lines.py` 用到的 segments、line_groups 函数复制进 `segmentation/`（推荐项） |
| 36 | WP1：云引擎密钥名与配置 | 改名 `OPENROUTER_STT_KEY`、`AZURE_SPEECH_KEY`、`AZURE_SPEECH_ENDPOINT`（`MOSS_PRO`、`GEMINI_FREE` 不变），只读 Nonoka 的存储、不读进程环境变量；照搬「取前 N 个」，不认 FineSub 的 `[pools]`/`[providers]`；`[asr] repair`、`moss_pro_paid`、`split_length_scale` 改成参数、沿用原默认值；配额台账 `<主数据目录>\state\cloud-asr-quota.json`（`config.state_file()`）。旧名字到新名字的映射由 WP-R4 的迁移做（推荐项） |
| 37 | 知识库独立拆包，包里放什么 | **全部经知识库包的协议**：只有知识库包碰知识库数据，转写和纠错两半经协议访问（推荐的是数据和管理归新包、两边各带原样的知识库代码直接读写，不动上锁文件） |
| 38 | 这等于按第 0 节第 7 条改上锁文件，怎么推进 | **现在就批准这类改动**：知识库访问点都可以改，由 WP-K 实施，闸门证明结果逐字节不变（推荐的是 WP-P0 先列清单、按清单批准） |
| 39 | 知识库包不在时，开了知识库或 ASR 上下文的任务怎么办 | 就绪检查拦下，不静默关闭（推荐项） |
| 40 | 知识库数据放哪 | `engines\finesub-knowledge\`，迁移时搬过去（推荐项） |
| 41 | WP3a/b/c 共用 `joint_stage.py`、`repair.py`、`lines.py`、`enhance/`，怎么安排 | 一个会话按 3a → 3b → 3c 依次做，每个引擎单独提交，最后拆掉 `joint_stage.py`（推荐项） |
| 42 | 云引擎的可选增强（要 torch）跑在哪（原附录 C 第 1 项） | **第一版不做增强**：云引擎只用启动器环境，增强代码留着不接，后续阶段再定（推荐的是单独的增强进程、进沙箱） |
| 43 | WP3 开工前的 8 问（行为基线、MOSS 空档补问、续调说话人提示、MAI2 费用预估、回答缓存位置、共用代码文件、Gemini repair、`transcript.json` 的 speaker） | 全部推荐项：WP1 的回放与断行基线不改，引擎层期望按「基线减去重问/补问插入的单元」推导；MOSS Pro 不补问空档；续调说话人记为新人时发一次 `moss-relink` 告警；MAI2 费用按实际上传的窗口总时长（含重叠、扣已缓存）× $0.10/小时，开跑前作为告警发；原始回答缓存放 `TranscriptionJob.cache_dir`，由 worker 保证每任务一份；共用代码放新建的 `engines/cloud.py`；Gemini repair 固定 off；speaker 用原 `line_speaker` 标签、没有的写 `unknown`。MOSS Pro 的 `paid` 默认 true（免费额度用完会扣预付余额，用户已知） |
| 44 | WP-C 计划外加的：`--record cloud` 重录时固定 run-1 为 AOTI、run-2 为 eager | 保留（推荐项） |
| 45 | WP-P0 的 9 个待定点 | 全部推荐项：纠错包带知识库代码的原样副本（只用模块级 import 的纯函数与常量），测试证明它不碰数据；命令行与死代码不进任何包；知识更新的 LLM 调用留在纠错进程，只有存储读写走协议；包进程之间由核心分配端点、直连；每个转写或纠错进程配一个知识库进程；默认路径的 `raw.srt` 仍由纠错包写，核心渲染只在换引擎或纠错包不在时用；知识库包有自己的兼容包（14 个名字）；WP-K 的判据是不 open 知识库下的文件、不 `sqlite3.connect`，路径解析不算；GPU 档位写成转写包 `PACKAGE.json` 的静态声明 |
| 46 | WP-P0 交回的 3 项（批准范围的边上） | 都确认：`knowledge/update.py`、`entries.py` 的存储调用算纠错那半的访问点，在第 0.1 节第 5 条范围内；MCP 子进程用端点文件找知识库服务，不改 `client.py`、`agent_session_host.py`；每进程一个知识库进程，`base.py` 的写锁与快照不改 |
| 47 | WP-R4：WP-R5 之前 FineSub 侧怎么过渡 | 全部推荐项：新存储是唯一真实来源，每次保存设置、云端登录与启动时整体派生 FineSub 的三个文件；派生到 `engines\finesub-shared\`；sidecar 启动时只对还没迁移的数据目录自动迁移设置与密钥；细则 1–9 按推荐 |
| 48 | WP-R4 偏离细则第 7 条：旧变量名（keyring 行与三个旧密钥名）放进 `migration/legacy.py`，不放 `config/migrate.py`（WP0 的测试要求 `config/` 里不出现 FineSub 的名字） | 批准（2026-11-13）；边界测试只许 `migration/legacy.py` 这一个文件、旧文件名与 `FINESUB_*` 两类越界 |
| 49 | WP-R4 偏离 WP0 的测试：登记过的新密钥名可以经 `ConfigStore.update` 写进 `secrets.json`（细则第 8 条） | 批准（2026-11-13）；这类密钥不进设置页的密钥列表、不派生给 FineSub |
| 50 | WP-K 的 K1–K8 用例与 8 个实施问题（K1 只用仓库内合成样本；闸门加 `knowledge` 检查；代码放 `src/finesub_knowledge/`；WP-P1 之前由 worker 进程配对拉起知识库进程，允许改 `worker.py` 三处；`multiprocessing.connection` + JSON；`build_asr_context` 只换碰数据的两个名字；只把被调函数与 `KnowledgeRepo` 换成协议、句柄后续使用也经协议；回放 harness 的拒网如需改只放行这一种连接） | 全部推荐项（2026-11-12） |
| 51 | WP-K 为固定知识库日期，在未改动的代码上重录 K 基线一次（只差日期与由它算出的哈希，LLM 请求不变） | 接受（2026-11-13） |
| 52 | WP-K：桌面打包与 wheel 收进 `src/finesub_knowledge` | 加进打包（2026-11-13） |
| 53 | WP-E1 的构建细则：模型层放哪些模型、工具层布局、两次构建怎么做到相同、层清单放哪、修改时间、wheel 哈希怎么记、版本号长短 | 模型层只放默认路径 5 个；工具层平铺；同一位置清空后重建；清单在包内也在包旁；修改时间固定、清单不记；先自己下载校验 wheel、再让 uv 装本地文件；版本号截成前 16 位（2026-11-12） |
| 54 | 分离器的 `download_checks.json`（上游移动分支上的索引）在模型层里怎么来；闸门要求两份模型清单一致而 vendor 的不能改 | 钉到上游 `8a65b784`；只改 Nonoka 的清单与 `RUNTIME_PINS.json`，闸门放过 vendor 那份没钉的这一项（2026-11-12） |
| 55 | `real.py` 先清 `NONOKA_QUALITY_*` 再定位，`--real` 用不上层里的模型和工具 | 改：清环境前先定位（推荐项，2026-11-12） |
| 56 | WP-CL0：云端预热放哪些模型；缺的怎么放进 Volume；已有的校验不过怎么办；没有哈希的小文件怎么校验 | 只放云端实际加载的 4 个；同库同参数下载、脚本自己校验；报错、不动文件；只看在不在（2026-11-12） |
| 57 | WP-CL0：`--cloud` 要从临时 Volume 读模型，得改本仓库的 harness；要不要顺手更新 `compute.md`；Modal 上的花费 | 在本仓库开分支改 harness；更新 `compute.md`；Modal 上的全部运行（约 $0.55）同意，中途被打断后同意重跑；用完后删掉临时 Volume（2026-11-12） |
| 58 | 第五批开哪几个包 | WP5、WP-E2、WP-E3 三个并行（推荐项）；`layers.json` 的格式由主会话先定、写进计划（WP-E2 一节） |
| 59 | WP-E2 的 R2 桶与域名（原附录 C 第 8 项） | 新建公开读桶 `nonoka-x-layers`，自定义域名 `layers.nonoka.online`（推荐项） |
| 60 | R2 上没被任何已发布版本引用的旧版本保留多久（原附录 C 第 8 项） | 不自动删；脚本列出没被引用的版本和各自大小，删除由用户确认（推荐项） |
| 61 | WP-E2 往 R2 上传约 10 GiB 公开内容并做外网下载校验 | 批准：桶建好后由会话用用户本机的凭据上传，传之前列出对象与大小（推荐项） |
| 62 | WP5：改计划清单以外的三个文件；FineSub 的 `raw-srt` 档用原来的判断还是 WP4a 的；worker 怎么拿到数据目录；请求校验与就绪的 8 条细则 | 三个都允许；原样搬原来的判断；命令行加参数；按这套做（都是推荐项，2026-11-13） |
| 63 | WP-E2：模型要不要自托管（原计划模型层压缩包之外再按文件放 33 个单文件） | 只不放单文件：R2 上只有三层的压缩包和清单，6 个对象约 5.73 GiB；`layers.json` 的 `files.layers` 为空列表（2026-11-13） |
| 64 | 不放单文件后，迁移时搬来的模型有缺或不对从哪补（4.4 第 25 条） | 下整个模型层压缩包，只解出缺的和不对的文件；全部对上就不下（推荐项，2026-11-13） |
| 65 | WP-E2：上传怎么实现；凭据放哪；「已发布版本」怎么认；速率限制规则怎么写；Cloudflare 上谁来建；经代理的外网校验 | 自己写 S3 签名（httpx）；放在被 git 忽略的 `scripts\.env`（`.gitignore` 加一行）；git 的 `v*` 标签上的 `layers.json` 加工作区这份；只匹配 `/v1/python/`、`/v1/models/`、`/v1/tools/`、按 IP 10 秒 50 次封 10 秒，但**先不建**；会话代建桶、用户自己绑域名和建令牌；代理 `http://127.0.0.1:7890`，这次没开，**以后再补，发版前必须补**（2026-11-13） |
| 66 | WP-E3：安装路径太深怎么办；下载的压缩包装好后留不留 | 完整路径超过 259 个字符就不装，提示换短路径，不看系统长路径设置；装好就删（离线包不删）（都是推荐项，2026-11-13） |
| 67 | WP-E3：迁移的两个钩子现在登不登记；清单外多出来的文件算不算不就绪；迁移补齐时引擎戳记怎么办；层版本不对时用不用旧的 | 现在登记（批准改 `migration/hooks.py`）；不算，按名字列在 `extra`，修复时清掉；按构建的公式重写，sha256 等于清单才采用；不用，显示「需要更新」（都是推荐项，2026-11-13） |
| 68 | 经代理的外网下载校验先不做之后，发版约束里「经代理和不经代理各下载校验一次」怎么办 | 删去经代理的一次，发版前只要求不经代理下载校验通过；WP8 新用户验收的「经代理从 R2 装好三层」也改为不限经不经代理；速率限制规则也先不建（2026-11-13） |
| 69 | 第六批开哪几个包 | WP6、WP-R5、WP-R6 三个并行（推荐项，2026-11-13） |
| 70 | WP-M1 的最后一条完成标准（用迁移命令把开发机真实的安装搬到新布局）现在做不做 | 先不做：等开发机准备换到新版时再做，在那之前真实安装继续只读（推荐项，2026-11-13） |
| 71 | WP-R5：派生在哪个进程做；两份桥接怎么分；sidecar 自己的 FineSub 绑定；外部变量与 MSVC 遗留；`NUMBA_CACHE_DIR` 指向哪；`engines\finesub-shared\` 残留；环境基线的比对范围；知识库与状态文件变量 | 全部推荐项：sidecar 在拉起 worker 前派生、交给子进程；转写与纠错分两份，分包前 task worker 合用；sidecar 只读打包默认值；去掉继承的 `FINESUB_*`、删两个 MSVC 函数（批准改 `local_provider.py`），从零构建留给 WP-H；`<大文件根>\cache\finesub-transcribe\numba\`；sidecar 启动时删；比对范围加上密钥名、Base URL 名、`NUMBA_CACHE_DIR`、`FINESUB_ENV_PROTECT`；无条件写、位置等 WP-M2（2026-11-13） |
| 72 | WP-R6：离线包装什么；断网怎么模拟；计划外文件；视频工具卸载按钮；预估占用；测试大文件；桌面端找工具改到工具层 | 三层加启动器；进程级断网；`engine.md` 3.4、`LOCAL_CHANGES.md` 一句、`download_route.py` 死代码、`offline_pack.go` 注释、`managedtools/resolve.go`、`storage_service.go` 都允许；只删页面按钮；14 GiB；**做完就删**（非推荐项）；改到工具层，**按 `current.json` 就用、不核对版本**（非推荐项）（2026-11-13） |
| 73 | WP6（在 WP6 会话里直接说的）：去掉运行环境页，环境安装、转写引擎、纠错翻译引擎的选择全放进设置页，LLM 提供商配置放在纠错引擎下面；什么时候做；对话框开关；纠错引擎怎么呈现；设置页布局；provider-spec 谁写；云端请求带不带引擎；任务列表的引擎从哪来；遥测；三组密钥放哪 | WP-R6 合并后在 WP6 会话里做，依赖 WP-P1 / WP-H 的以后补；对话框去掉开关、云端 FineSub 成为引擎列表一项；「当前纠错引擎」卡片下挂 LLM 配置；**一页长滚动**、不加导航（推荐的是左侧分组导航）；WP6 写；带 `finesub-cloud`、Go 不转发；**Python 快照带引擎**（推荐的是前端提交时记，批准改 `local_provider.py`）；不做；放在设置页转写引擎面板（2026-11-13） |
| 74 | WP6 第二阶段：三组云引擎密钥在设置页怎么取；当前转写引擎存哪；WP-R6 / WP-R5 交来的界面事项；对话框与未就绪引擎 | 设置快照另加 `engineKeys`（原 `keys` 不变，WP0 测试那一句随之改）；存 Go 桌面偏好 `transcriptionEngine`；全做，**numba 缓存纳入清理**（非推荐项）；按引擎藏选项，未就绪的引擎可选但新建任务被拦、指回设置页（其余都是推荐项，2026-11-16） |
| 75 | 第七批开什么（前提满足的只有 WP-R7 和 WP7） | 只开 WP-R7；WP7 的文档写最终架构，留到 WP-P1、WP-H 之后、WP8 之前（推荐项，2026-11-16） |
| 76 | WP-R7 的 `quality_gate.py --cloud`（Modal L4，按 WP-C 实测一次约 $0.6） | 批准，上限 $1.5：跑一次，失败可以重跑一次，超出先问（推荐项，2026-11-16） |
| 77 | WP-R7 开工前五问：缺模型怎么报错；只为下载服务的代码；桥接与路由的死代码和云端摘要源；暂存预编译用哪个解释器、编哪些；计划外的小改动 | `ModelUnavailable(VerificationMismatch)` 上抛，`HF_HUB_OFFLINE=1` 仍留给 WP-H；下载代码删掉；死代码全删，含云端摘要源与 `asset_digest.py`；只预编译引擎代码、钉 3.12.1；文档、`engine_packages.py`、Taskfile 补 `src/finesub_knowledge`、云端注释都改（全部推荐项，2026-11-16） |
| 78 | `release.yml` 要装 Python 3.12.1 才能暂存预编译 | 同意（2026-11-16） |
| 79 | WP-R7 的 `--cloud`：L4 排队太慢 | 第一次 L4 叫停改 A10；A10 统计超阈值（基线是 L4 录的）后选「再在 L4 跑一次」，通过；共 $0.448，在 $1.5 上限内（2026-11-17） |
| 80 | 第八批开什么（WP-R7 做完后前提满足的只有 WP-P1；WP7 已定在 WP-P1、WP-H 之后，WP-M1 最后一条先不做） | 「继续下一批任务」：只开 WP-P1；删 `third_party/finesub` 后云端镜像在 WP-CL 之前怎么办，由 WP-P1 查清后问用户；本包不跑 Modal（2026-11-17） |
| 81 | WP-P1 开工十问：分不分阶段；「`src/nonoka_x/` 搜不到 `finesub`」怎么判；产物挪进私有目录后闸门怎么比；sidecar 里要问 FineSub 的事怎么问；WP5 交回的知识库检查；`--real` 各进程用哪个解释器；暂存预编译哪些包；云端怎么办；计划外的文件；原始字幕由核心渲染的范围 | 全部推荐项（2026-11-17）：同一分支两阶段、中间不停；按代码判（import、`FINESUB_` 字面量、FineSub 文件名），注释、`resources/` 钉住的数据、`migration/legacy.py`、引擎 id 不算；现在就挪进 `native\<包>\`，harness 映射回基线路径、基线不动；就绪由核心按 `PACKAGE.json` 判，FineSub 查询交给纠错包的常驻进程；「要不要知识库」由纠错包回答，provider 与 worker 同一判断；纠错与知识库进程在闸门里用照桌面做法建的启动器（`NONOKA_QUALITY_LAUNCHER_PYTHON`）；三个包都用 3.12.1 预编译；云端仓库只改挂载来源与档位名单、不跑 `--cloud`；计划外的文件都批准，`finesub_align.py` 改读三个包的并集；只在纠错包不在时由核心渲染 `raw.srt` |
| 82 | WP-P1 阶段二两问：兼容包「名字不多不少」怎么落实；依赖面基线少了 3 个名字 | `__all__` 恰为名单，辅助名字保留原名并逐个写明原因（`COMPAT_HELPERS`）；只从 `bootstrap-surface.json` 删掉 `try_lock`、`holding_activity_barrier`、`remove_project_records_under`（使用者 `batch_state.py`、`agent_cleanup.py` 不进任何包），其余不动（均为推荐项，2026-11-17） |
| 83 | 第九批开什么（WP-P1 之后 WP-H、WP-L、WP-CL 都能开；WP-L 与 WP-CL 都改 `desktop/internal/cloud/` 和结果、同步的格式） | WP-H 与 WP-L 并行，`local_provider.py` 按函数分工、主会话合并；WP-CL 等 WP-L 合并后作为第十批（推荐项，2026-11-18） |
| 84 | WP-L 的转换脚本要在线上云端数据的副本上验证 | 开工后由 WP-L 查清要读的数据、副本的做法与存放、用完怎么删，再交用户批准；批准前不碰线上数据（推荐项，2026-11-18） |
| 85 | WP-H 九问：纠错进程的 PATH；知识库进程的环境；各进程的当前目录；沙箱写入检查的范围；计划外的 4 处文件；显卡变量；工具目录；`HOMEDRIVE` / `HOMEPATH`；环境基线 | 纠错 PATH 由 Nonoka 显式拼出；知识库同纠错、去掉 Agent 部分；转写的当前目录是任务目录、其余是 run 目录；严格区加观察区；4 处都允许；适配器 `cuda:N` 时无条件设显卡变量；**工具只用工具层**（非推荐项）；`HOMEDRIVE` / `HOMEPATH` 改向沙箱；环境基线当时**先不重录**（非推荐项，合并时再定，见第 87 问）；其余都是推荐项（2026-11-18） |
| 86 | WP-L 八问：`correction.json` 的形状；词级时间；说话人放哪；调用等价的产物清单；转换范围；转换入口；线上只读副本；同步细节 | 存对好的字幕行；词对象原样放；说话人只放 `correction.json`；比清单时跳过两项、基线不动；只转媒体库，任务结果留给 WP-CL；两个包各加一个协议操作（批准改转写包的 `__main__.py`），本机命令行脚本、不上 Modal；批准用 wrangler 只读复制 D1 `library` 表与它指到的 R2 对象，超过 2 GB 或 2000 条再问，用完删；白名单、426、产物顺序、`relay.go` 作业部分留给 WP-CL 都按推荐（全部推荐项，2026-11-18） |
| 87 | 合并 WP-H、WP-L 时：环境基线 `tests/fixtures/quality/environment.json` 比对部分多两行 `HF_HUB_OFFLINE=1`，重录吗 | 批准重录，单独提交（`fcea2b2`；不比对的启动器部分随密闭一起变）（推荐项，2026-11-18） |
| 88 | 第十批开什么（WP-CL、WP-M2、WP-UI、WP7 前提都已满足）；WP-CL 的 Modal 花费上限 | 开 WP-CL 与 WP-M2，WP-UI、WP7 后开；`--cloud` 合计上限 $1（约 3 次）（2026-11-18） |
| 89 | 合并 WP-M2 阶段 1 时：合并后新代码的 sidecar 会自动迁移它打开的旧数据目录；迁移未完成时的门控；主数据目录里的 `*.pre-migration` 备份与留下的 `finesub-data\`；边界测试新豁免 `("migration/legacy.py", "package:finesub-transcribe")` | 直接合并，用户自己注意别让新代码指向开发机的真实安装；保持门控（推荐项）；没用的文件全部删除（用户自定）；同意豁免（2026-11-18） |
| 90 | WP-M2 阶段 2：删 `*.pre-migration` 与 `finesub-data\` 的时机；有本机打不开的旧密钥时删不删 `finesub.env.pre-migration` | 本地步骤过了回滚点（旧环境开删）就删，不等下载；照删，报告里写明随备份删掉的密钥名，「除 `engines\` 外无 FineSub 文件」没有例外（推荐的是下载后再删、这种情况保留）（2026-11-18） |
| 91 | 附录 C 第 9 问：迁移「先删后下」期间，界面要不要提示改用云端 FineSub 或云 API 引擎 | 不提示，只说明下载完成之前 FineSub 转写不可用（2026-11-18） |
| 92 | 剩余工作怎么最省（可以减少测试） | 按主会话方案：评审时分支基于当前 main 就不重跑，main 动过才在试合并上跑一次 pytest 与默认闸门；子会话开发时只跑相关测试、交付前全量一次；`--real`（含 WP-M2 阶段 2 的那次）、`--cloud`、线上 LLM 与三个云引擎的真实调用集中到 WP8 各一次；WP-M2 阶段 2 的副本验收由 WP8 沿用；WP-M1 最后一条在换新版时作为 WP8 迁移一项的实测；合并 WP-CL 时重录 4 个云端调用基线（单独提交）；WP7 在 WP-CL 合并后开，WP-UI 等 WP-M2 阶段 2 合并后开（2026-11-18） |
| 93 | 合并 WP-M2 阶段 2 与 WP7 时：合不合并；CHANGELOG 记在哪个版本；WP7 多写的 4 条（可选转写引擎、日文轴只在本机、云端新格式需要更新、引擎启动时确定一次代理）；WP-H 之后录不到包进程的 `scripts/engine_packages_real_record.py` | 两个都合并；改成 `0.8.0`；都留；删掉脚本（都是推荐项或用户选项，2026-11-19） |
| 94 | WP-UI 开工前：没装知识库包时 `--style auto` 要不要在就绪里拦；核心 media 档的 ffmpeg 口径；沙箱 home 的 NVIDIA ComputeCache 进不进「清理」 | 在就绪里拦（推荐项）；统一只认工具层（推荐项）；不列进清理（2026-11-19） |
| 95 | WP-UI 阶段 2 与 WP8 在哪做；WP8 的 `--cloud` 花费上限；真实调用用的密钥；干净 Windows 上的全新安装 | WP-UI 阶段 1 用子代理做，阶段 2 与 WP8 都在主会话里做；`--cloud` 合计上限 $1；线上 LLM 那一程用用户在对话里给的 Nonoka Cloud 账户 key（不写进文档、日志与提交）；三个云引擎的真实调用这次不验证，报告里标为未验证；全新安装这次先不做（2026-11-19） |
| 96 | 合并 WP-UI 阶段 1（`7d4518d`，合并 `a3cc842`）时：缺说话人分离模型的请求要不要在就绪里拦；中继遇到迁移时的 60 次轮询上限；「清理未使用的」把当前引擎用不到的整层（约 10 GB）列为可删；被中断的任务算不算占用层 | 合并；在就绪里拦；迁移期间不计上限；整层要单独勾选才删，默认只清没人用的旧版本；被中断的任务仍算占用（都是推荐项，2026-11-19） |
| 97 | 合并 WP-UI 补改（`49a10e4`，合并 `71edf0a`）时：缺说话人分离模型、缺知识库包的检查只在新建任务时判，重试或续跑旧任务时要不要也拦 | 合并；不用（推荐项，2026-11-19） |
| 98 | WP-UI 阶段 2 的预览验收怎么做（只能起真的桌面应用、用屏幕操作点；整套要从 R2 下几 GB、真删层） | 精简验收：能用接口与自动化测试证明的用接口验；界面由用户自己点一遍（2026-11-19） |

更早的问答：WP-R0 的 D1–D23 见 R0 文档第 0 节；WP0 的细节见附录 A.1（已移到 `docs/transcription-engines-plan-done.md`）；沙箱实验中的决定见实验报告。

## 附录 C 还没定的事

开工到相应工作包时先问用户，不要自己定。

| # | 事项 | 什么时候问 | 现有线索 |
|---|---|---|---|
| 1 | ~~云 API 引擎的可选增强跑在哪~~ | 已答复 | 见附录 B 第 42 问：第一版不做增强，后续阶段再定 |
| 2 | ~~转写进程的 `HOMEDRIVE`、`HOMEPATH` 是否一并改向~~ | 已答复 | 见附录 B 第 85 问：实测后改向沙箱 |
| 3 | 云端统计标准的具体阈值 | WP-C 实测后 | 附录 B 第 11 问 |
| 4 | ~~VC++ 运行库~~ | 已答复 | 见附录 B 第 30 问 |
| 5 | ~~cn 锁不复制进 Nonoka 的清单~~ | 已答复 | 见附录 B 第 23 问 |
| 6 | ~~模型层包含哪些模型~~ | 已答复 | 见附录 B 第 53 问：只放默认路径的 5 个 |
| 7 | ~~macOS 的工具层在哪里构建~~ | 已答复 | 见附录 B 第 31 问；macOS 工具层另定 |
| 8 | ~~R2 上旧层版本的保留期限，以及桶名、域名~~ | 已答复 | 见附录 B 第 59、60 问 |
| 9 | ~~迁移「先删后下」期间，界面要不要提示改用云端 FineSub 或云 API 引擎~~ | 已答复 | 见附录 B 第 91 问：不提示 |
