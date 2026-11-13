# 转写引擎重构计划

> 状态（2026-11-11）：WP-Q、WP-A、WP0、WP-R0 已完成并合进 main；WP-C 的 harness 已合进 main，云端基线还没录成（见 WP-C）；编译加速已在 `85ae2b9` 关闭；WP-R2、WP-B、WP-R1、WP1、WP4a、WP4b、WP4c、WP3a/b/c、WP-C、WP-P0、WP-M1（最后一条完成标准除外）、WP-R4、WP-K、WP-E1、WP-CL0、WP5、WP-E2、WP-E3 已完成并合进 main（WP-CL0 的预热脚本合进云端仓库的 main；各包按附录 B 的答复调整）；2026-11-12 用户决定知识库独立成第三个包、全部经协议访问（第 0.1 节第 5 条、4.6 第 35a 条、WP-K）；第五批（WP5、WP-E2、WP-E3，2026-11-13）已全部完成；第六批 WP6、WP-R5、WP-R6 已派给各自的会话（2026-11-13，附录 B 第 69 问）；WP-M1 的最后一条完成标准先不做（第 70 问）；三层已传上 R2（经代理的下载校验和速率限制规则用户决定先不做，见 WP-E2）；2026-11-13 用户决定模型层不再按单文件放 R2，迁移时模型补齐改为下整个模型层压缩包（附录 B 第 63、64 问）；其余未开工。本文档是交给子代理执行的工作说明，每个工作包可以单独领走。
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
- 桌面脚本 `desktop/scripts/resource-sources.mjs`、`build-offline-package.mjs`、`publish-github-mirror.mjs` 读 FineSub 的清单来准备离线包和镜像。
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

标 ⚠ 的开工前请确认，改了要回头调整对应工作包。条款按主题分节、连续编号；旧编号对照见附录 A.2。

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
已完成：WP-Q、WP-A、WP0、WP-R0、WP-R2、WP-B、WP-R1、WP1、WP4a、WP4b、WP4c、WP3a/b/c、WP-C、WP-P0、WP-M1（最后一条完成标准还没做，WP-E3 已完成、可以做了）、WP-R4、WP-K、WP-E1、WP-CL0、WP5、WP-E2、WP-E3

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

> 交付：`--cloud` 只比结构与统计，每次运行与**同一种加速**的基线比（run-1 AOTI、run-2 eager）。用户批准的阈值：段数比例 0.02、文字距离比例 0.005、说话人数差 0、覆盖差 0.002。实测：同加速 6 次运行各项为 0（覆盖最多 0.000017）；AOTI 对 eager，`sample_ja` 段数比例 0.0093、文字距离 0.0109，`sample_multi_person` 0.130 与 0.0506。未改动的代码上 `--cloud` 连跑两次都通过（一次 eager、一次 AOTI），全部花费约 $1.26，全程 L4。`--record cloud` 重录时固定 run-1 为 AOTI、run-2 为 eager（附录 B 第 44 问）。没测到的：L4 换到新 cpu_tag 强制重编 AOTI 包时结果是否变，遇到会以闸门失败的形式暴露。下面是原始说明。

harness 已在 main（`tests/quality_harness/cloud.py`、`quality_gate.py --cloud`、`--record cloud`）：临时 Modal 应用，与生产相同的镜像，GPU 固定 L4，未改动的调度入口。录出来的基线不能用（2.10）。

- 剩下要做的：
  1. 按第 0.1 节第 3 条改比对：只比结构与统计。要比的项：产物齐全（`stable.json`、`aligned.json`、`diar.json` 等，以云端实际产出为准）、格式合法（能被纠错包读入）、段数、逐段文字与基线的编辑距离（汇总成比例）、说话人数（开 `acoustics` 的样本）、总时长覆盖。
  2. **实测自然波动**：在未改动的代码上，每个样本跑若干次，AOTI 和 eager 两种加速都要覆盖到（可以靠样本顺序或单独的容器控制），量出上面每一项的波动范围。顺带查 L4 机群 CPU 不一致导致 AOTI 重编时结果是否变化。
  3. 把建议阈值和依据交用户批准。**批准之前不写进闸门、不录基线。**
  4. 批准后录基线，单独提交（基线文件不进仓库，提交的是阈值与比对代码）。
- 完成标准：`quality_gate.py --cloud` 对未改动的代码连跑两次都通过；交付说明里写明花费、各项波动、用的显卡、每次运行的加速状态。
- 之后：WP-R7、WP-CL、WP8 各跑一次 `--cloud`。

### WP-A 拆掉上游对齐机制（已完成）

同步脚本、补丁栈、`FILES.json`、相关测试、CI 快照校验已删；`UPSTREAM.json` 精简；`scripts/finesub_align.py`、`LOCAL_CHANGES.md` 已建；`llm/` 顶层与 `llm/knowledge/` 的归属已列。vendor 里其余非核心文件留给 WP-R7。

### WP-B 拆分 `run_pipeline`（已完成 2026-11-11）

> 交付：`run_transcription`、`run_post_stable(speaker_source=acoustic|line-labels, judge_audio=vocal|source)`、`run_pipeline`（固定 `acoustic` + `vocal`）；联合 ASR 的参数、分支和命令行选项已删，两个文件都不再 import `speech.joint`（目录留给 WP1）；`logs.prune`、`open_log` 复制进 `pipeline.py`；记在 `LOCAL_CHANGES.md` 第 0009 节。`--real` 通过。交回 WP-P1：`pipeline.py` 仍 import `finesub_bootstrap.locks` 的 `holding_lock`、`LockUnavailable`（批处理的跨进程锁），由纠错兼容包补上。删联合 ASR 的后果：旧的联合 ASR 任务续跑时按本地识别处理；带 `asr` 等键的旧批处理清单续跑时报 unknown manifest keys；上锁的 `settings.py` 里 `[asr] backend`、`[asr] aligner` 已没有读取方（Nonoka 从不传 `asr`，不受影响）。另：云端测试的 `conftest.py` 找本仓库读的是 `NONOKA_X_CHECKOUT`。下面是原始说明。

本计划里唯一改 FineSub 编排的包，单独一个子代理做，做完单独评审。

- 文件：`third_party/finesub/src/finesub/stages.py`、`pipeline.py`、`tests/test_split_stage_contract.py`、`third_party/finesub/LOCAL_CHANGES.md`。
- 内容：
  - 按 4.1 第 2 条拆出 `run_transcription` 和 `run_post_stable`，`run_pipeline` 留作薄组合。
  - `run_post_stable` 新增两个显式参数：说话人来源（声学时间线 / 转写行标签）和裁判听的音轨。原来由 `joint` 推出的行为改由它们决定。
  - 删掉 `asr`、`asr_aligner`、`asr_repair` 参数和联合 ASR 分支，`pipeline.py` 里相应的命令行选项一并删。做完后两个文件都不再 import `speech.joint`。
  - `pipeline.py` 还 import 依赖面清单之外的 `logs.open_log` 等（2.4）。拆的时候去掉这些 import；去不掉的列出来交回，由 WP-P1 的纠错兼容包补上（转写包带的是原件，本来就有）。
  - **只做搬移和拆分**：每个阶段调用哪个算法函数、传什么参数、按什么条件跳过，一律照原样；不顺手整理、不改默认值、不改日志以外的任何行为。
- 完成标准：
  - 闸门全部通过，含 `--real`：真实样本经 `run_pipeline` 的产物按第 0 节第 3 条与基线一致，LLM 请求序列与基线逐字节一致，调用等价测试通过。
  - 先 `run_transcription` 再 `run_post_stable`，与一次 `run_pipeline` 到同一目标，产物按第 0 节第 3 条一致。
  - `run_post_stable` 以「说话人来自行标签」跑一份只有 `line_speaker` 的 stable 到 `raw-srt`，全程不 import torch 和 numpy，并写出 `-diar-rows.json`。
  - 质量锁测试通过（上锁文件一个没动）。
  - 云端仓库的 `phases.py` 仍调得通 `run_pipeline`。

### WP0 契约与骨架（已完成）

5.1–5.3 的类型、两个注册表、`transcript.json` 的读写与校验、`nonoka_x.config` 与 `nonoka_x.runtime` 两个薄门面、FineSub 边界测试与过渡白名单都已建。向用户确认过的细节见附录 A.1。

### WP1 迁出 `speech/joint/`（已完成 2026-11-11）

> 交付：`speech/joint/` 已 `git mv` 进 `transcription/`（布局按附录 B 第 35 问，记在 `LOCAL_CHANGES.md` 第 0010 节）；句子 DP 等纯 Python 部分复制进 `transcription/segmentation/`，Silero、声纹与 PLDA、device/resources 复制进 `enhance/`（第 34 问）；`http.py`、`audio.py`、`keys.py`、`reporting.py` 换成 Nonoka 自己的。门面新增 `ConfigStore.state_file(name)`、`RuntimeManager.model_dir(model_id)`（只比文件大小，不自下载）。行为基线：6 个合成场景（假 provider 按三家真实回答格式作答）加句子 DP 与 sbd，搬前录、搬后逐项一致；sbd 模型在主检出 `.tmp\sbd-hf\`。密钥新旧名对照（交 WP-R4）：`FINESUB_KEY_OPENROUTER_STT` → `OPENROUTER_STT_KEY`、`FINESUB_KEY_AZURE_SPEECH` → `AZURE_SPEECH_KEY`、`FINESUB_AZURE_SPEECH_ENDPOINT` → `AZURE_SPEECH_ENDPOINT`。有意的行为差异：密钥不认进程环境变量；`relink` 不再搜 FineSub 的说话人产物目录、不自下载；本地 MOSS 的 Triton 缓存在 `%TEMP%`；SOCKS 代理走直连并经报告器告警。下面是原始说明。

- 文件：`third_party/finesub/src/finesub/speech/joint/**`（迁走后删除）、`transcription/units.py`、`lines.py`、`stitch.py`、`cache.py`、`keys.py`、`audio.py`、`http.py`、`segmentation/**`、`enhance/**` 及对应测试。`speech/joint/model.py` 里的 `Unit`、`Turn`、`Transcript` 搬进 `transcription/units.py`：`transcription/model.py` 已被 WP0 用作 `transcript.json` 的类型，不要往里加。
- 内容：
  - **搬之前先留行为基线**：这部分代码不在闸门覆盖内。用录制的（或手写的）三个云后端回答，在未改动的代码上跑拼窗、跨窗说话人对齐、断行，把输出存成 `tests/fixtures/transcription/` 下的基线；搬完改完之后同样的输入必须得到同样的输出。
  - `git mv` 保留历史，再改 import。对 FineSub 的 `api_http`、`api_keys`、`reporting`、`settings`、`state` 以及 `speech/runtime` 的依赖换成 Nonoka 自己的；对 FineSub 算法模块的依赖一个不留：句子 DP 按 4.1 第 5 条复制成 `transcription/segmentation/`，先在原件上录好输出基线，复制件必须得到相同的输出。FineSub 里给联合 ASR 留的配置项和密钥池定义在上锁文件里，原样留着不管。
  - `audio.py`：截取上传用 MP3 的参数照 `finesub/media/ffmpeg.py:328`；ffmpeg 路径通过 `nonoka_x.runtime` 门面取。
  - `http.py`：代理沿用 Nonoka 的代理设置（R0 文档 D17）。
  - `keys.py` 取密钥一律走 `nonoka_x.config` 门面。配额台账写到 Nonoka 自己的状态文件，不写 `finesub.state`。
  - `enhance/` 里的模块全部惰性 import，并提供「Python 环境层是否可用」的统一判断。`sbd.py` import 的 `onnx` 在 WP-R1 之后由 `onnx-weekly` 提供（2.9）：如果 WP1 在 WP-R1 之后做，`sbd` 的行为基线要在去掉 `onnx` 的环境里也通过。
  - 三个云后端的文件先原样搬到 `engines/` 下，能 import 即可，改造留给 WP3。本地 MOSS 搬到 `engines/moss_local/`，不注册。
- 完成标准：`third_party/finesub/src/finesub/speech/joint/` 不存在；搬迁前录的行为基线全部一致；拼窗、缓存命中与失配、配额换 key、断行各有不联网的单元测试；在只装了启动器依赖的环境里 `import nonoka_x.transcription.lines` 成功；闸门通过。

> **WP3a/b/c 已完成（2026-11-12）**：`Mai2Engine`、`GeminiEngine`、`MossProEngine` 与共用的 `engines/cloud.py`，只要工具层，不 import `enhance/`、torch、numpy、finesub；`joint_stage.py` 已删（`JointAsrError` 挪进 `engines/__init__.py`）。按附录 B 第 43 问：WP1 的基线不改，引擎层期望由基线推导；MAI2 开跑前发 `cloud-asr-cost` 费用预估告警；MOSS Pro 不补问空档、续调说话人记为新人并发 `moss-relink` 告警；`transcript.json` 的 speaker 沿用 `line_speaker`。交回 WP5：MAI2 的三个密钥是二选一的两条路；新密钥名要 WP-R4 的存储才有值；三个引擎要加进 worker 的注册表。真实调用留给 WP8。

> **WP3a/b/c 的安排**（用户 2026-11-12，附录 B 第 41、42 问）：三个引擎由一个会话按 3a → 3b → 3c 依次做，每个引擎单独提交，最后拆掉 `engines/joint_stage.py`。第一版不接增强（4.1 第 6 条），下面各包里「接到 `enhance/`」「有无 Python 环境层两种情况都有测试」改为：引擎不 import `enhance/`，只测启动器环境下的行为；增强相关的测试留给后续阶段。

### WP3a MAI-Transcribe-2

- 文件：`engines/mai2.py`、`tests/test_engine_mai2.py`。
- 内容：包成 `TranscriptionEngine`；OpenRouter 与 Azure 两条路；空档重问接到 `enhance/`，Python 环境层不可用时跳过。开工前先问用户：可选增强跑在哪个解释器、进不进沙箱（附录 C）。
- 完成标准：用录制的两种回答格式做解析测试；多窗口拼接测试；缺密钥时 `readiness` 给出明确 issue；开跑前按时长报预估费用；有无 Python 环境层两种情况都有测试。

### WP3b Gemini 3.5 Transcribe

- 文件：`engines/gemini.py`、`tests/test_engine_gemini.py`。
- 内容：包成 `TranscriptionEngine`；密钥复用已配置的 `GEMINI_FREE` 池，不自动动用付费池；断句模型、Live 修补、强制对齐接到 `enhance/`。
- 完成标准：时间修复、复读检测、续问、配额耗尽换 key 各有测试；有无 Python 环境层两种情况都有测试。

### WP3c MOSS Pro

- 文件：`engines/moss_pro.py`、`tests/test_engine_moss_pro.py`。
- 内容：包成 `TranscriptionEngine`；段内定时在 Python 环境层可用时走 Silero，否则插值；声纹重连接到 `enhance/`，不可用时只靠重叠时间对齐说话人，对不上的记为新说话人。
- 完成标准：段级拼接、上限续问、配额台账各有测试；有无 Python 环境层两种情况都有测试。

### WP4a FineSub 转写引擎（已完成 2026-11-11）

> 交付：`FineSubEngine` 与 `SPEC`（id `finesub`，层暂按现有门面 id 声明，WP-E3 后换成三层）；`job.options` 由 worker 填好解析后的值，适配器只做 FineSub 侧映射。参数等价：26 个本地识别场景，算法入口参数与传给 `run_transcription` 的关键字都与基线的转写那一半相同（发现闸门盲区：调用矩阵里 ASR 上下文文本恒为空，单看算法入口查不出 `extra_info` 漏传）。`transcript.json` 只从 stable 投影，不写 speaker；`native` 交出 `finesub.*` 原生产物。真实样本经适配器跑到 stable 与基线一致。交回 WP4c：自相矛盾的请求要到 ASR 之后才报错（原先在分离前）；知识主体要在转写前建好；`install_proxy_route_policy()` 仍由 worker 装。交回分包：转写引擎读纠错包的知识库做 ASR 上下文；`TranscriptionJob.source` 是 `Path`，装不了 URL。下面是原始说明。

- 文件：`transcription/engines/finesub.py`、`tests/test_engine_finesub.py`。
- 内容：调 `run_transcription`。传的参数以 WP-Q 的调用基线为准，包括供 ASR 上下文用的 `extra_info`、`knowledge`。跑完只读 stable 投影出 `transcript.json`，并在 `native` 里交出 `finesub.stable_json`、`finesub.diar_json` 等。按 4.6 第 35 条写成将来要搬进包的样子。
- 完成标准：不 import `nonoka_x.correction`；闸门通过，尤其是 `--real` 下真实样本的 `stable.json` 按第 0 节第 3 条一致；`EngineSpec` 如实声明它要的三层，就绪检查只从这里来。

### WP4b finesub correction（已完成 2026-11-11）

> 交付：`FineSubCorrection`（不要任何层）、`place_native`（`worker.seed_cloud_stable` 改调它）、`run_source_text`（日文轴，`axis.translate_axis` 转调）；`CorrectionJob` 加了 `title`、`task_id`、`task_summary`（有默认值）。有原生 stable 走 `acoustic` + `vocal`，否则由 `transcript.json` 转 stable 走 `line-labels` + `source`。云端续跑只带时间线不带行标签时，由本模块调 FineSub 自己的 `_run_diarization_stage` 从已在盘上的时间线写行标签（不跑模型；不写的话裁判请求从 3 个变 2 个）。回放 10 个用例与 4 个云端续跑场景经新模块逐字节一致。交回 WP4c：没绑 FineSub reporter 时报告直接转给 Nonoka 的 `Reporter`，没有 `NonokaXReporter` 的去重与过滤；`prepare_knowledge_subjects` 的归属。已知：日文轴里「音频源却要视频参考」与 `fast=on` 同时出现时两条提示的先后对调。下面是原始说明。

- 文件：`correction/finesub.py`、`axis.py`、`tests/test_finesub_correction.py`。
- 内容：按 5.2 取得 stable，放到 FineSub 的路径，调 `run_post_stable`，参数以 WP-Q 的调用基线为准。`worker.seed_cloud_stable` 和 `axis.translate_axis` 里调 FineSub 的部分收进来。`axis.translate_axis` 内部改为转调本模块（签名保留只为少改桌面侧的调用点）。按 4.6 第 35 条写。
- 完成标准：
  - 闸门通过：原生 stable 输入下，发给 LLM 的请求序列与基线逐字节一致。
  - 一份只有 `lines` 的 `transcript.json` 经它跑到 `raw-srt`，全程不 import torch 和 numpy，并写出带说话人的 `-diar-rows.json`。
  - 不 import `nonoka_x.transcription.engines`；日文轴路径的现有测试全过。

### WP4c worker 编排（已完成 2026-11-12）

> 交付：worker 按 `transcription.engine`（缺省 `finesub`）与 `correction.engine` 从注册表取实现；注册用 `worker.py` 里的一张内置表（模块路径 + 工厂），WP-P1 由 `engine_host` 取代——**WP3 的三个云引擎还没加进这张表，由 WP5 加**。目标超过 `stable` 时先调纠错层的 `check()`（借 FineSub 的 `_prepare_run`，不跑阶段），保住「矛盾请求在分离前报错」；一个 `NonokaXReporter` 绑住两段，事件与现在相同；云端续跑直接构造 `TranscriptionResult`；`cache_dir` 是 `<task_dir>/cache`，每任务一份。可替换性有测试（假引擎、不注册 FineSub 也能跑到 `final-srt`）。`--real` 通过。留给后续：`CLOUD_NATIVE` 写死的两个云端键名（WP-CL）；worker 里 `for_task_mode`、`install_llm_model_override`、`prepare_knowledge_subjects` 等 FineSub 纠错准备代码（WP-K / WP-P1）。下面是原始说明。

- 文件：`worker.py`、`tests/test_worker_adapter.py`。
- 内容：按 `transcription.engine` 取引擎，转写，再按 `correction.engine` 交给纠错层。目标阶段不超过 `stable` 时只跑引擎。云端续跑成为「转写结果已在手」的一种输入：直接构造 `TranscriptionResult`，`native` 里放下载到的 stable。worker 里不再直接 import `finesub.stages`。
- 完成标准：
  - 闸门全部通过；默认引擎下同一输入的产物清单与现在一致；任务日志里不出现重复的「已有结果，跳过」；云端续跑路径的现有测试全过。
  - **可替换性**：worker 里没有按引擎名分支的代码；用一个只交 `transcript.json` 的测试用假引擎替换 FineSub，跑到 `final-srt` 不需要改 worker；不注册 FineSub 转写引擎（模拟没装）时，其他引擎加 finesub correction 照样能跑。

### WP5 请求校验、就绪检查、密钥（已完成 2026-11-13）

> 交付：`local_provider.py`、`transcription/engines/finesub.py`、`correction/finesub.py`、`worker.py` 及三处测试（附录 B 第 62 问批准改计划清单以外的三个文件）。请求校验：没有 `transcription` 时不补字段、引擎是 FineSub 时块原样保留，FineSub 请求的规范化结果与改前逐字节一样；不在注册表的引擎报 `unsupported_engine`；云 API 引擎只许到 `raw-srt`、`translated-srt`、`final-srt`，不能续云端识别或用日文轴，`separate`、`device`、`gpu_tier`、`gpu_budget_gb`、`vocal_profile` 去掉并在任务日志写一条；说话人分离档和语言超出引擎声明就报错。就绪：`raw-srt` 档由所选转写引擎给（FineSub 的判断从 `runtime_report` 原样搬进适配器，没有显卡仍只是警告，由 `EngineSpec.environment.gpu` 得出），续跑与三个 LLM 档由 finesub correction 给（原样搬）；云 API 引擎先过自己的（缺密钥 `missing_key`）再过续跑档；知识库包不在时启动就拒（`missing_knowledge_package`，和 worker 用同一个检查）。`capabilities()` 多 `transcription_engines`、`correction_engines`。三组密钥规格经 config 门面登记，`update_keys` 改走 `ConfigStore.update`，设置页快照仍是 12 个、不派生给 FineSub。worker 注册表加上 MAI2、Gemini、MOSS Pro，命令行加 `--data-dir`、`--install-dir`，门面只在云引擎用到时才打开。主会话复核：独立 pytest 893 通过，闸门含 `--real` 通过，没重录基线。交回：WP-P1——中文源、只纠错的任务在知识库包缺失时 provider 会拦而 worker 不拦（分包前碰不到）；WP6 / WP7——`docs/provider-spec.md` 还没写两个新列表和三个新错误码，设置页要加三组密钥的输入，前端类型要加两个列表；WP-E3——两个 FineSub 适配器的就绪只用 runtime 门面的 `status()` 和 `tool_path()`，换门面时保住这两个接口。下面是原始说明。

等 WP-R4 做完再开工：新增的密钥规格直接登记到 Nonoka 自己的密钥存储。

- 文件：`local_provider.py`、`tests/test_local_provider.py`；密钥规格通过 `nonoka_x.config` 门面登记。
- 内容：
  - `validate_request` 接受并规范化 `transcription`；引擎不支持的选项（如云 API 引擎的 `separate`、`device`、`gpu_tier`）规范化掉并在任务日志里说明。引擎是 `finesub` 时，规范化结果必须和现在完全一样。
  - 就绪检查按引擎分：`local_provider.py:243` 现在按本地 GPU 写死，拆成由各引擎的 `readiness` 提供；`_start` 里的就绪表（`:1113`）让云 API 引擎走续跑那一档再加上自己的密钥检查。
  - LLM 那几档的就绪检查（`harness`、`final-srt`、`knowledge`、`video-multimodal`）改由 finesub correction 的 `readiness` 提供，判断逻辑不变。
  - `capabilities()` 输出 `transcription_engines` 和 `correction_engines`。
  - 登记 OpenRouter STT、Azure Speech（密钥加端点）、MOSS Pro 池三组密钥规格。这些密钥只给 Nonoka 自己的引擎用，不派生给 FineSub。
  - 各引擎的就绪检查从 `EngineSpec.environment`（要哪几层）和层管理器得出。
- 完成标准：没装任何层（只有工具层）、没有 GPU 的环境里，配了 MAI2 密钥就能通过 `final-srt` 的启动检查；没配时报 `missing_key`；闸门通过。

### WP6 桌面界面与 Go

- 文件：`desktop/frontend/src/providers/types.ts`、`home/defaultRequest.ts`、`components/TranscriptionDialog.tsx`（及其 CSS）、`app/App.tsx`、`app/types.ts`、`providers/localProvider.ts`、`test/provider.test.mjs`；Go 侧 `desktop/internal/taskhistory`、`internal/telemetry` 如需记录引擎。
- 内容：只做管线——类型、请求里带上 `transcription` 字段、任务列表和历史显示所用引擎、Go 侧的记录。引擎的选择在引擎配置页（WP-UI）；对话框暂时保留现有的本机 / 云端开关，等 WP-UI 换掉。
- 完成标准：`npm run typecheck`、`npm test` 通过；选 FineSub 时发出的请求除多一个 `transcription` 字段外与现在相同。

### WP-R0 运行时与配置的接口调查（已完成）

结论与设计答复在 R0 文档。其中被本计划取代的答复（D5、D10、D12、D23 整条，D18、D21 部分）已在 R0 文档里标注，对照见附录 A.3。

### WP-R1 Nonoka 自己的清单，去掉 `onnx`（已完成 2026-11-11）

> 交付：`src/nonoka_x/resources/` 下的 `pylock.win-py312.toml`、`runtime-manifest.windows-x64.json`（含 `interpreter`：PBS 3.12.1，sha256 `fd5a9e0f…eae08a`）、`model-manifest.json`，读取接口 `runtime/manifests.py`；三份锁去掉 `onnx`（98 → 97，单独提交 `4c138fd`）。`tests/quality_harness/pins.py` 的 `derive()` 改读 Nonoka 的清单，`problems()` 同时对照 `finesub_bootstrap` 自己的文件（转写包运行时还读它们）；`pins.py write` 重新生成的记录与提交的逐字节相同。`pyproject.toml` 打包 `resources/*.toml`，桌面暂存本来就整目录复制。harness 认 `NONOKA_QUALITY_PYTHON`（见 WP-Q 用法）。合并后的 main 上 `--real` 通过。已知：仓库里跟踪的 `src/nonoka_x.egg-info/` 与代码不同步（原有问题）。下面是原始说明。

- 文件：`src/nonoka_x/resources/` 下的运行时锁、工具清单、模型清单，`runtime/manifests.py`，`tests/test_runtime_pins.py`，`RUNTIME_PINS.json`；`third_party/finesub/src/finesub_bootstrap/pylock.win-py312.toml`、`pylock.win-py312.cn.toml`（只删 `onnx` 一项）。
- 内容：
  1. 把 `finesub_bootstrap` 里的 `pylock.win-py312.toml`、`runtime-manifest.json`、`model-manifest.json` 复制成 Nonoka 的清单（cn 锁不复制，4.2 第 13 条）。清单格式可以按 Nonoka 的需要整理，但钉住的内容不变。`RUNTIME_PINS.json` 增加 Python 解释器一项：PBS `cpython-3.12.1+20240107-x86_64-pc-windows-msvc-shared-install_only` 的下载地址与 sha256（实验报告 2.0 记了 `fd5a9e0f…`，按发布方的完整值录）。
  2. **去掉 `onnx`**（第 0.1 节第 2 条，单独一个提交）：
     - 在 Nonoka 的锁和 `finesub_bootstrap` 的两份锁里，**手工删掉** `name = "onnx"`（1.23.2）那一个 `[[packages]]` 块，**不重新** `uv pip compile`（重新解析可能带动别的版本）；`onnx-weekly`、`onnx2torch-py313`、`onnxruntime` 不动。
     - `RUNTIME_PINS.json` 用 `tests/test_runtime_pins.py` 的推导函数重新生成，diff 只许少 `onnx` 这一项（包数 98 → 97）。
     - 重新验证：`tests/test_runtime_pins.py` 通过；用新锁在一个干净的 PBS 3.12.1 里装一次，没有 `onnx-1.23.2.dist-info`，`onnx/` 下每个文件与 `onnx_weekly-1.23.0.dev20260805.dist-info/RECORD` 相符，`python -I -c "import onnx, audio_separator.separator"` 成功；`quality_gate.py --real` 用这个环境跑通过（与 eager 基线一致）；WP1 已做的话，`sbd` 的行为基线仍通过。
     - 提交信息写明用户 2026-11-11 批准。
- 完成标准：`derived_pins()` 改为从 Nonoka 的清单推导后通过，钉住的每一项（含 `engine_constants`）与 WP-Q 记录的基线相同，只有 `onnx` 一项按批准去掉、解释器一项按批准新增。

### WP-R2 下载与文件设施（已完成 2026-11-11）

> 交付：`runtime/fsops.py`、`fetch.py`、`paths.py`；对 FineSub 的运行时补丁搬进 `finesub_bridge.py`（附录 B 第 24 问，WP-R5 接手）；镜像连同 `runtime_install.py` 里的调用方一起删（第 25 问），下载阶梯为「离线包 → 代理 → 直连 → 报错」。交回的调用方：WP-R6 管 `desktop/internal/app/python_bootstrap.go`、`download_route.go` 里的镜像与文案和 `publish-github-mirror.mjs`；WP-E3 管 `model_install.py:183` 的 `resolve_region` 和 `provision.py` 沿用的 `is_mirror_failure`。下面是原始说明。

- 文件：`runtime/fsops.py`、`runtime/fetch.py`、`runtime/paths.py` 及测试；`download_route.py`、`asset_digest.py`、`config_file.py` 改为用它们。
- 内容：原子写、文件锁、目录链接判断、进程树终止、断点下载与大小/sha256 校验、代理（按 Nonoka 的代理设置，D17）、目录布局（4.4 第 24 条，含 `runtime\layers\`）。以 `finesub_bootstrap` 的 `fsops`、`locks`、`processes`、`downloader`、`http_client`、`paths` 为起点复制过来，注明来源。**不做国内镜像和线路选择**（4.2 第 18 条）：`download_route.py` 的镜像与线路逻辑删掉，只留直连加代理；`download_route.py:366` 不再读 `FINESUB_GITHUB_FILE_PROXY`。
- 完成标准：这三个已有文件不再 import `finesub_bootstrap`；`tests/test_download_route.py`、`tests/test_asset_digest.py` 按新行为更新后全过，`tests/test_hf_mirror_transfer.py` 随镜像一起删掉或改写（交付说明里写明）；断点续传、摘要不符、经代理下载各有测试。

### WP-E1 预制包构建流水线（已完成 2026-11-13）

> 交付：`scripts/build_layers.py`（`build`、`compare`、`stage`）、`src/nonoka_x/runtime/layer_manifest.py`（构建、闸门、WP-E3 层管理器共用的清单格式）、闸门 `--layers`（`tests/quality_harness/layers.py`）、`tests/test_layer_build.py`、`docs/runtime-layers.md`（构建部分）。只做 Windows（附录 B 第 31 问）。按附录 B 第 53 问：模型层只放默认路径会加载的 5 个（separator、whisper、qwen-referee、nemotron3、speaker-embedding），实文件加引擎要的 `.finesub-verified.json` 与 `.verified` 戳记；工具层平铺 `<工具目录>\<压缩包原样内容>`、不含 uv；wheel 先自己下载校验、再让 uv 从本地文件装；清单既在压缩包根目录（`.nonoka-layer.json`）也在压缩包旁；所有文件修改时间固定为 2026-01-01 UTC、清单不记；版本号取清单 sha256 前 16 位。分离器的 `download_checks.json` 在 Nonoka 清单里钉到上游 `8a65b784`（vendor 那份不动，闸门只放过这一处，附录 B 第 54 问）。`tests/quality_harness/real.py` 改成清环境变量前先定位安装目录（第 55 问）。实测：python `8cc0ee612a5db785`（5.30 GiB，压缩 2.50 GiB）、models `f2a2b753498be37c`（3.99 / 2.97 GiB）、tools `b2e46916329ea7c4`（0.68 / 0.24 GiB），从零完整构建约 33.5 分钟；同一位置两次干净构建清单与压缩包都相同；`--layers` 通过（每个文件都比）；解压到带空格的路径后 import 与 CUDA 正常；用层里的解释器、模型、工具跑 `--real` 通过。产出在 `dist\layers\`（WP-E2 发布用），构建缓存 `.tmp\layer-build\` 约 13 GB 可删。交回：**numba 运行时会往 Python 层里写缓存**（跑 `--real` 后多 46 个 `.nbi`/`.nbc`），要在转写进程环境里设 `NUMBA_CACHE_DIR`（WP-R5 / WP-H）；WP-E3 解压时必须保留修改时间（否则分离器戳记失效、引擎往模型层写新戳），Python 层最长相对路径 138 字符，用户机器上的目录名要留意总长度。下面是原始说明。

- 文件：`scripts/build_layers.py`、`src/nonoka_x/runtime/layer_manifest.py`、`scripts/quality_gate.py` 与 `tests/quality_harness/`（加「预制层」检查，`--layers`）、`tests/test_layer_build.py`、`docs/runtime-layers.md`（构建与发布手册，本包写构建部分）。
- 内容：按 4.2 第 14、17 条构建三层。
  - 输入只有：Nonoka 的清单（WP-R1）和 `RUNTIME_PINS.json`。下载缓存放在构建目录里，不用用户级缓存（`uv --no-config`，`UV_CACHE_DIR` 指向构建目录）。
  - Python 环境层：下载并校验 PBS；`uv --no-config pip install --link-mode copy --python <pbs>\python.exe --requirement <锁>`；删 pip 与 `README.txt`；用 `compileall --invalidation-mode unchecked-hash` 预编译；`Lib\site-packages` 里不留 uv 探测时生成的零散 `.pyc` 以外的东西（这些也删或重新统一生成）。记录 uv 校验过的每个 wheel 的哈希。
  - 模型层：按模型清单的钉住 revision 下载（构建机上可以用 `huggingface_hub`，不经引擎），按清单校验，生成引擎要的校验标记，符号链接换实文件；不带 `accel\`。
  - 工具层：按工具清单下载校验，Windows、macOS 各一层。
  - 每层产出 `tar.zst`（zstd -3，可用 Windows 自带 `tar.exe` 解压）和逐文件清单（相对路径、大小、sha256；列明写死路径的例外文件）；层版本号 = 清单的哈希。
  - 闸门的「预制层」检查（WP-Q 表）在这里实现。
- 完成标准：
  - 干净目录里构建两次，三层的逐文件清单完全相同。
  - `quality_gate.py --layers` 对产出的层通过。
  - Python 环境层解压到一个带空格的新路径，`python -I -B -c "import torch, ctranslate2, faster_whisper, audio_separator, onnxruntime, transformers"` 成功，`torch.cuda.is_available()` 为真；运行一次后层目录里没有新文件（`-B` 生效）。
  - 模型层（无符号链接）在 `hf_verify` 和各加载器下可用；用层里的解释器、模型、工具跑 `quality_gate.py --real`（harness 临时指向层目录，不进沙箱）与 eager 基线一致。
  - 交付说明里写明三层的原始大小与压缩后大小、构建耗时。

### WP-E2 R2 托管与发布（新；已完成 2026-11-13）

> 交付：`scripts/publish_layers.py`（`index`、`plan`、`upload`、`verify`、`unreferenced`、`ratelimit`；S3 SigV4 签名用 httpx 自己写，不加依赖；上传先 HEAD，大小与 `x-amz-meta-sha256` 都对上才跳过，写入一律 `If-None-Match: *`，绝不覆盖；64 MiB 以上分片；发给 R2 的请求一律 `Accept-Encoding: identity`；凭据只从被 git 忽略的 `scripts\.env` 读，不打印、不记日志）、`src/nonoka_x/resources/layers.json`（格式没改，`files.layers` 为 `[]`）、`tests/test_publish_layers.py`（21 个，假 R2 校验签名与条件写入）、`docs/runtime-layers.md` 第 2 节（建桶步骤、发布步骤、速率限制验证、费用估算、看用量）、`.gitignore` 加 `scripts/.env`。按附录 B 第 65 问的答复做。Cloudflare 上：桶 `nonoka-x-layers`（APAC，`r2.dev` 关闭）由会话经用户本机已登录的 wrangler 建，自定义域名 `layers.nonoka.online` 由用户自己绑，R2 令牌由用户建。上传 6 个对象共 5.73 GiB（三层的压缩包与清单）；重传一次 6 个全部跳过、对象的 etag 与修改时间不变；不经代理的外网校验通过（13 分钟下完，三层逐文件 sha256 相符）。踩到的坑：R2 对 JSON 自动 gzip，HEAD 没有 `Content-Length`，所以一律带 `identity`。主会话复核：与 WP-E3 一起试合并，pytest 925 通过，闸门含 `--real` 通过，没重录基线。**没做的两项**（用户 2026-11-13 决定先不做）：①经代理的外网校验（`verify --proxy <代理>`；发版约束里已删去这一项，附录 B 第 68 问）；②速率限制规则（建好后跑 `ratelimit --direct` 确认出现 429）。用过的 R2 令牌用户已处理，WP-E2 的会话已删。下面是原始说明。

> 用户 2026-11-13（附录 B 第 59–61 问）：桶 `nonoka-x-layers`（新建、公开读，不复用云端任务用的私有桶 `nonoka-x-cloud`），自定义域名 `layers.nonoka.online`；桶、域名、速率限制规则由用户在 Cloudflare 上建，会话先写好脚本和手册里的建桶步骤，桶建好后用用户本机的凭据上传（传之前列出对象与大小），之后做外网下载校验；没被任何已发布版本引用的旧版本不自动删，脚本只列出来，删除由用户确认。

> **`layers.json` 的格式**（2026-11-13 定，WP-E2 写、WP-E3 读；要改先经主会话问用户）：
>
> ```json
> {
>   "schema": 1,
>   "platform": "windows-x64",
>   "baseUrl": "https://layers.nonoka.online/",
>   "layers": {
>     "python": {
>       "version": "8cc0ee612a5db785",
>       "manifest": {"path": "v1/python/<清单 sha256>.json", "size": 0, "sha256": "<清单 sha256>"},
>       "archive":  {"path": "v1/python/<压缩包 sha256>.tar.zst", "size": 0, "sha256": "<压缩包 sha256>"}
>     },
>     "models": {"...": "同上"},
>     "tools":  {"...": "同上"}
>   },
>   "files": {"layers": [], "path": "v1/files/{sha256}"}
> }
> ```
>
> - `path` 都相对 `baseUrl`；对象按内容哈希命名，上传后不再改（第 18 条）。
> - `version` 必须等于清单 sha256 的前 16 位（`layer_manifest.version_of`），读的一方要核对；清单的字节与压缩包里的 `.nonoka-layer.json` 相同。
> - `files`：列出的层里每个文件另按它的 sha256 存一份单文件，地址把 `{sha256}` 换成文件的 sha256。2026-11-13 起 `layers` 是空列表，不放任何单文件（附录 B 第 63 问）；键和读法保留，读的一方遇到空列表就没有按文件下载这条路。
> - 离线包里用同样的相对路径存这些对象，层管理器先找离线包、再联网。
> - 测试里 `baseUrl` 可以换成本地 HTTP 服务。


- 文件：`scripts/publish_layers.py`、`src/nonoka_x/resources/layers.json`（这个版本要的各层：版本、压缩包地址、大小、sha256、清单地址；模型层的单文件地址规则）、`docs/runtime-layers.md`（发布部分）、`tests/test_publish_layers.py`。
- 内容：按 4.2 第 18 条。
  - R2 桶、自定义域名、速率限制规则由用户在 Cloudflare 上建（账号与凭据只在用户本机，不进仓库、不进日志）；本包写清楚要建什么，脚本只用本机的凭据。
  - 上传幂等：对象按内容哈希命名，已存在就跳过，不覆盖；上传压缩包和清单（模型层的单文件按附录 B 第 63 问不传）。
  - 下架旧版本的规则：被任何已发布桌面版本引用的层不删。
  - 在手册里记下流量与请求费用的估算方法（每个新用户约 5.8 GiB），以及看用量的地方。
- 完成标准：脚本把 WP-E1 的产出传上去；从外网（~~经代理和~~不经代理~~各一次~~，附录 B 第 68 问）按 `layers.json` 下载、按清单校验通过；对同一对象重复上传不产生变化；速率限制规则生效（手册里有验证步骤）。

### WP-E3 层管理器（新，取代原 WP-R3；已完成 2026-11-13）

> 交付：`runtime/layers.py`（读 `layers.json` 并核对；装一层：清单 → 路径深度 → 磁盘空间 → 压缩包（离线包优先、从不删；否则断点续传、走 Nonoka 的代理设置）→ 系统 `tar.exe` 解到 `<版本>.unpack\`（保留修改时间）→ 按清单核对 → 改名 → 才切 `current.json` → 删下载的压缩包 → 没有未结束任务就删旧版本；被杀在每一步后都能续上；就绪 `ready/missing/outdated/damaged`，快速检查单趟扫描，真 Python 层 45563 个文件 0.46 s；`repair`；`unused()`/`remove_unused()`/`overview()`，删之前先改名到 `.retired`、被占用就整份留下；迁移用的 `complete_models()`），`provision.py` 重写在层上（对外状态结构不变，新增 `layers`、`unused_layers`、`unused_bytes`、`repair` 目标、`install_path_too_deep`、`runtime_in_use`；不建 venv、不调 uv、不从 HF 下载），`runtime/__init__.py` 门面（WP5 要的 `status()`、`tool_path()` 保住；`model_dir` 从模型层答），`runtime_install.py`、`model_install.py`，迁移钩子 `ModelCompletionHook`、`LayerDownloadHook` 登记进 `migration/hooks.py`；harness 只改定位（`real.py` 的 `provisioner_for`、`NONOKA_QUALITY_LAYERS_INDEX`，`pins.py` 量模型层与工具层）。`model_specs.py`、`msvc_runtime.py` 没改。`test_finesub_boundary.py` 去掉三条 WP-R3 过渡项。按附录 B 第 66、67 问的答复做。实测：从本地服务装真实三层约 3.6 分钟；从 R2 直连装真实三层（工具 55 s、Python 442 s、模型 331 s），完整核对全部就绪、下载缓存为空，`--real` 通过；整个大文件根搬到另一个盘、带空格的路径，不修补，就绪与 `--real` 都通过；真实三层最长相对路径 python 144、models 145 字符，默认位置算下来约 232。主会话复核：与 WP-E2 一起试合并，pytest 925 通过，闸门质量锁、钉住内容（installed runtime matches）、调用等价、环境、LLM 回放、知识库、`--real`（7 个逐字节、8 个按认可标准）通过，没重录基线；`--layers` 只报跑过 `--real` 后 Python 层多出的 46 个 numba 缓存文件，模型层与工具层干净。小改进（不挡合并）：层的下载走 `runtime.fetch.download_file`，没带 `Accept-Encoding: identity`，现在靠 httpx 自动解 gzip 才对，以后补上更稳。交回：WP-R5 / WP-H——`NUMBA_CACHE_DIR`；`local_provider.py` 的 `_prepare_msvc_environment`、`_clear_legacy_separator_decode_probes` 随「环境从零构建」删掉；`worker_environment` 仍是父环境加层路径。WP-R6——运行环境页文案（前端仍写「FineSub bootstrap 不可用」）、`remove_tool_group` 按钮（视频工具已并入工具层，不再单独卸载）、新的 `repair` 目标与字段；`download_route.py` 里 `offline_snapshot`、`offline_python`、`ensure_fast_enough`、`stage_environment`、`SpeedWatch` 已无调用方；Go 传给 sidecar 的 `--install-dir` 现在当作大文件根。WP-UI——「清理未使用的」用 `unused_layers()`/`remove_unused_layers()` 或门面 `layer_overview()`，sidecar 还没有对应路由。WP-M2——两个钩子已登记，迁移命令现在会真做模型补齐、下 Python 层和工具层。macOS 没有工具层，运行环境页在 mac 上显示不支持。下面是原始说明。

> `layers.json` 的格式见 WP-E2 一节（两包按同一份格式并行）。WP-E1 交来的：解压必须保留文件的修改时间（否则分离器的 `.verified` 戳记失效、引擎往模型层写新戳）；Python 层最长相对路径 138 字符；numba 会往 Python 层写缓存（`NUMBA_CACHE_DIR` 归 WP-R5 / WP-H，本包的只读核对要能指出是哪些文件）。


- 文件：`runtime/__init__.py`（把门面换成真实现）、`runtime/layers.py`、`provision.py`、`runtime_install.py`、`model_install.py`、`model_specs.py`、`msvc_runtime.py` 及对应测试（`tests/test_provision.py`、`test_runtime_install.py`、`test_model_install.py`、`test_offline_model_install.py` 按新行为重写）；`tests/quality_harness/real.py`、`pins.py`（只改定位）。
- 内容：按 4.2 第 19 条。
  - 按 `layers.json` 下载缺的层（断点续传，经 Nonoka 的代理设置），校验压缩包 sha256，解压到 `layers\<层>\<版本>\` 的临时目录，按清单核对（快速或完整），通过后切 `current.json`；旧版本没有任务在用就删。
  - 离线包优先：离线包里有对应的层文件就用它，不联网。
  - 就绪、修复（重新下载出问题的层）、清理（WP-UI 的「清理未使用的」要的查询：按引擎列出所需层及状态、每层占用、未被使用的层）。
  - 迁移用的模型补齐：给定一个已搬进来的模型目录，按清单核对；有缺的或不对的，就下整个模型层压缩包（先找离线包），只解出缺的和不对的文件（4.4 第 25 条，附录 B 第 64 问）。
  - 下载前照旧检查 VC++ 运行库（`msvc_runtime.py`），措辞照旧。
  - `provision.py` 对外的状态结构尽量保持不变，桌面「运行环境」页少改。不再建 venv、不再调 uv、不再从 Hugging Face 下模型；`msvc_runtime.py` 以外与 MSVC 有关的代码删掉。
- 完成标准：
  - 这几个文件不再 import `finesub_bootstrap`。
  - 空目录上从本地 HTTP 服务装三层（小的假层测流程），断点续传、摘要不符、清单不符、中途被杀各有测试；从 R2 装真实的三层至少一次（等 WP-E2）。
  - 装好的层上 `quality_gate.py --layers`（完整检查）通过，`quality_gate.py --real` 通过（harness 定位到层里的解释器和模型）。
  - 整个大文件根搬到别的盘、别的路径（带空格）后，不做任何修补，就绪检查通过、`--real` 通过。

### WP-R4 配置与密钥存储（已完成 2026-11-13）

> 交付：`config/store.py`（`settings.json`，稀疏，只存设过的路由、`baseUrls`、`cloud.llmBaseUrl`，原子写加 `.lock`）、`config/secrets.py`（`secrets.json` = `{schema, keyring, values}`；信封方案从 FineSub v0.7.0 `finesub_bootstrap/secrets.py` 复制，加解密函数与原版逐个相同，双向互通有测试；多 key 池的写法保留、只封装密钥部分）、`config/migrate.py`（接进迁移命令的 settings 一步）。迁移：认识的设置与密钥搬过来，`[nonoka_models]` 先经 `migrate_model_config`；三个旧密钥名改新名（新旧都在时新名为准）；不认识的键与变量丢弃，报告 `runtime-state\settings-migration.json` 只列名字；本机解不开的值不搬、留在备份；旧文件改名 `.pre-migration`；可续跑；遇到不是本迁移写出的新存储就停。按附录 B 第 47 问的过渡做法：FineSub 要读的 `finesub.env`、`finesub.toml`、`model_catalog.toml` 由新存储整体派生到 `engines\finesub-shared\`（与旧写入器对新文件的输出同字节），sidecar 启动时对还没迁移的数据目录自动迁移设置与密钥。按第 48、49 问调整了两处细则。闸门含 `--real` 通过，没有重录任何基线。交回：WP-R5 删掉 `engines\finesub-shared\` 与过渡期派生代码（`config_file.py` 已无调用方，一并处理）；WP5 经 `ConfigStore.update` / `key_pool` 存取新密钥（`*_ENDPOINT` 读出来是整条地址）；WP-M2 把迁移接进启动后去掉 `bind_environment` 里的自动迁移。下面是原始说明。

- 文件：`config/__init__.py`（把门面换成真实现）、`config/store.py`、`config/secrets.py`、`config/migrate.py`、`settings.py` 及测试。
- 内容：`settings.json`、`secrets.json`（R0 文档 2.1、2.2，D1、D2）；密钥沿用 FineSub 现行的信封方案（Windows DPAPI 绑账户），代码复制过来注明来源；读旧的 `finesub.env`、`finesub.toml` 的迁移逻辑放在 `config/migrate.py`，由本包接进 WP-M1 的迁移命令，迁移规则按 R0 文档 2.7 第 3 条；存储放在主数据目录里；`settings.py` 的快照接口对界面保持兼容，底下改读新存储。本包**不**负责生成 FineSub 的文件，那是 WP-R5。
- 完成标准：迁移往返测试——拿一组真实形态的旧文件（含 Nonoka 不认识的键、多 key 池、自定义端点、本地 Agent 路由）迁移后，Nonoka 认识的设置和密钥都读得回来，不认识的键和变量被丢弃并出现在迁移报告里（只列名字不列值）；密钥不出现在日志和快照里；`tests/test_settings.py` 全过。

### WP-R5 FineSub 桥接

- 文件：`finesub_bridge.py`、`tests/test_finesub_bridge.py`；`provision.worker_environment` 和 `settings.bind_environment` 里与 FineSub 有关的部分收进来（含 `85ae2b9` 加的两个加速开关）。
- 内容：按 4.3 第 22、23 条，每次启动 worker 时从 Nonoka 的配置、密钥、目录（层的当前版本目录）派生 `finesub.toml`、模型目录文件和全部 `FINESUB_*` 环境变量；派生文件放 `engines\<包>\run\<进程标识>\`；密钥只走环境变量。按 4.6 第 35 条写成将来要搬进两个包的样子：转写那一份和纠错那一份分开（各自只派生自己要的东西）。
- 完成标准：
  - 闸门的调用等价通过；派生出的 `finesub.toml`（含 `[nonoka_models]`，与基线同字节）和模型目录文件的内容与 WP-Q 基线一致。
  - **环境基线重录**（第 0.1 节第 4 条，D19，用户已批准）：**单独提交**，交付说明里逐项列出新旧基线的差异并说明每一项为什么是交付方式的变化而不是引擎行为的变化。只重录 `environment`，其余基线不动。
  - 迁移前后对照：同一份旧配置，迁移前引擎读到的与迁移后派生出的语义相同（只对 Nonoka 认识的键）。
  - `src/nonoka_x/` 里只有本文件和两个适配器知道 `FINESUB_*`（WP0 的边界测试，白名单变短）。
  - `quality_gate.py --real` 通过。

### WP-R6 桌面、脚本、离线包与文档

- 文件：`desktop/scripts/resource-sources.mjs`、`build-offline-package.mjs`、`publish-github-mirror.mjs`（删除）、`desktop/internal/app/python_bootstrap.go` 及其测试、`desktop/internal/storage/locations.go`、「运行环境」页与设置页相关前端文件、`docs/architecture.md` 4.3 节。
- 内容：
  - 离线包改成装 `layers.json` 列的层文件（压缩包加清单）；`resource-sources.mjs` 改读 Nonoka 的清单与 `layers.json`。
  - 删掉 GitHub 镜像脚本和界面里的国内镜像、下载线路选项（4.2 第 18 条）。
  - 大文件根改名 `runtime`（Go 的常量 `runtimeName`，`locations.go:30`）；「存储位置搬迁」整体搬 `runtime\`，不需要任何修补（层不含绝对路径）。
  - 界面文案里「FineSub 运行时」之类的说法按新归属改；Go 侧如果有读 FineSub 清单或环境变量的地方一并改。
- 完成标准：离线包能构建，其中的层文件与 R2 上同版本的逐字节相同；用离线包在断网的机器（或断网的虚拟机）上装好三层、`--real` 通过；`go test ./...`、前端测试通过；仓库里搜不到 `publish-github-mirror`。

### WP-R7 删自下载，vendor 只留核心文件（重写）

等 WP-A、WP-B、WP-R5、WP-R6、WP-CL0、WP-C 做完再开工。WP-CL0 必须在前：云端现在靠引擎自下载补模型，先换成独立的预热脚本，云端才不会因为本包而缺模型。

- 文件：`third_party/finesub/src/finesub_bootstrap/model_ensure.py`、`model_fetch.py`（只改「缺模型时下载」的部分）、`third_party/finesub/` 下第 1 节第 7 条以外的文件（`pyproject.toml`、`VERSION`、`README.md` 等）、读它们的构建与暂存脚本（`desktop/build/tools/stage/main.go`、`Taskfile.yml`、`tests/test_vendor_contract.py`）、`third_party/finesub/LOCAL_CHANGES.md`、`tests/test_runtime_pins.py`。
- 内容：
  - 按 4.2 第 16 条删掉自下载：`ensure_hf_model` 和 `model_fetch` 在模型缺失或校验不过时抛出一个明确的错误（说清缺哪个模型、在哪找），不开下载子进程、不联网。模型齐全时的行为（返回的路径、revision、校验）一字不变。`finesub_bootstrap` 其余部分不动。改动记进 `LOCAL_CHANGES.md`，写明为什么、怎么还原。
  - 删 vendor 的 `pyproject.toml`（从此不再是检出形状，qwen 裁判编译缓存那类检出回落消失；加速已关，实际不再写缓存）、`VERSION`、`README.md` 等，读它们的地方一并改（R0 文档 1.14）。
  - 桌面暂存时用 `unchecked-hash` 预编译引擎代码（4.2 第 17 条）。
- 完成标准：
  - `third_party/finesub/` 下只剩第 1 节第 7 条列的文件和 `src/`；质量锁测试通过（550 个上锁文件一个没动）。
  - 删一个模型目录后跑引擎：报明确的错误，没有任何网络请求（测试里打桩网络，断言零请求）。
  - `tests/test_runtime_pins.py` 通过；`quality_gate.py --real` 通过；`quality_gate.py --cloud` 通过（云端镜像复制的就是这份 vendor，靠 WP-CL0 的预热）。

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

### WP-M2 迁移的启动集成、续跑与回滚

等 WP-E3、WP-R4、WP-R5、WP-R7、WP-P1、WP-L 把新布局定型再开工。必须和改布局的那几个包进同一个版本（发版约束）。知识库从主数据目录搬进纠错包的数据目录、旧任务目录里的 FineSub 产物归进私有附件目录，也在这里做（4.6 第 34 条）。

- 文件：`src/nonoka_x/migration/**`（在 WP-M1 的基础上）、`tests/test_migration.py`，sidecar 启动段里调用它的地方，桌面上显示迁移进度和结果的地方。
- 内容：
  - 启动时、sidecar 打开任何数据之前调 WP-M1 的探测与执行。
  - 设置和密钥调 `config/migrate.py` 读进 Nonoka 的存储，旧文件改名留作备份。
  - **先删后下**：清空旧环境后由 WP-E3 下载新层；界面显示进度，说明下载完成之前 FineSub 转写不可用（云 API 引擎在工具层装好后即可用）；下载失败可以重试；用户关掉应用后下次启动接着下。
  - 可中断可续：每一步写进迁移日志，中途断电或被杀，下次启动从断点接着做。设置、密钥、知识库、任务这几步失败就回滚到迁移前的样子并在界面上说明；**环境清空之后没有回滚**（旧环境已删），只能继续下载。
  - 只迁一次：迁完写标记；新版本再也不读旧路径。
- 完成标准：
  - 拿一份真实形态的旧数据目录副本（含大文件根在另一个盘、有 Nonoka 不认识的配置键、多 key 池、本地 Agent 路由、做过的任务与知识库）迁移，迁完：Nonoka 认识的设置和密钥都读得回来，不认识的配置键出现在迁移报告里，任务和文档在界面里照常打开，模型没有重下（只补了缺的文件），三层就绪，`quality_gate.py --real` 通过。
  - 迁移在每一步被杀掉后都能续上，设置与数据那几步能回滚，各有测试。
  - 迁移后主数据目录里除 `engines\` 外没有 FineSub 相关的文件。

### WP-P0 分包清单（已完成 2026-11-12）

> 交付：`docs/engine-packages.md` 与 `docs/engine-packages/` 下三份清单（`scripts/engine_packages.py` 可重跑）。转写包 171 个文件、纠错包 466 个（加 11 个模块的兼容包）、知识库包 340 个（加 8 个模块的兼容包，14 个名字）；FineSub 命令行与死代码（40 个上锁文件加 `pipeline.py`）不进任何包。知识库访问点 76 个，分布在 19 个冻结文件里；现有闸门基本没覆盖有内容的知识库，列了 8 个要补录的用例（K1–K8）。用户的决定见附录 B 第 45、46 问。WP-K 改完访问点后重跑脚本得到 WP-P1 的最终清单。下面是原始说明。

等 WP-B、WP1 做完再开工（冻结点）。只读调查，不改代码，结论写进 `docs/engine-packages.md`。

- 要回答的问题：
  1. 两个包各自的**精确文件清单**：从 `run_transcription`、`run_post_stable` 和各自适配器要用的入口出发，静态 import 图加一次打桩运行时的实际 import 记录，两者取并集。提示词模板、包数据等非 `.py` 文件按谁读它来归。
  2. 共用底座有哪些文件；有没有只被一边用、可以不带的。
  3. 纠错包的清单在只装 httpx、pydantic、opencc 的环境里能不能完整 import，链上有没有 torch、numpy。
  4. `finesub_bootstrap` 依赖面的 48 个名字各归哪个包；纠错兼容包的精确名单（加上 `pipeline.py` 若归纠错包时要的那几个）。
  5. 核心现在直接用 FineSub 的每一处（`settings.py` 的模型路由与目录、`projector.py` 的说话人产物解析、`gpu_tier.py` 的档位选项、`axis.py`、`worker.py`、`llm_worker.py`、`knowledge_worker.py`、`local_provider.py`）：各归哪个包的适配器，协议上要加什么查询。
  6. 现有产物清单里每一项：是 Nonoka 格式、是中性格式（SRT），还是 FineSub 格式；FineSub 格式的由哪个包转换成什么，哪些只作私有附件。
  7. 任务续跑和重试现在靠盘上的 FineSub 产物跳过已完成的阶段；产物进了私有目录之后这个行为怎么保持。
  8. 知识库包（4.6 第 35a 条，数据在 `engines\finesub-knowledge\`）：界面上知识库管理的每个操作走协议的哪个查询。
  9. 进程拆成两段后启动开销多少（转写进程还要算上沙箱准备）；默认路径的任务日志和进度事件怎么保持与现在一致。
  10. **知识库的访问点**（第 0.1 节第 5 条）：转写和纠错两半每一处直接读写知识库的调用点（文件、函数、读还是写、要不要写锁）；知识库包的协议设计（查询与写入的种类、包进程之间怎么连、沙箱里的转写进程怎么连、调用开销）；每个要改的上锁文件怎么改（只换访问方式）；闸门现有基线覆盖没覆盖这些路径，没覆盖的列出要补录的用例（在未改动的代码上录，录之前经用户批准）。
- 完成标准：十个问题各有带文件位置的答案；三份文件清单（转写包、纠错包、知识库包）可以被脚本直接读；任何一条意味着必须改冻结文件的，单独标出并交回。

### WP-K 知识库访问点改走协议（已完成 2026-11-13）

> 交付：协议代码在顶层包 `src/finesub_knowledge/`（`protocol.py`、`client.py`、`server.py`、`manage.py`，不 import `nonoka_x`），WP-P1 时 client 与 protocol 复制进转写包、纠错包，server、manage、protocol 进知识库包。19 个冻结文件只改 import 行（65 个名字改从 `finesub_knowledge.client` 取，调用一行没动）；`QUALITY_LOCK.json` 在单独提交里只更新 18 个上锁文件的哈希（`stages.py` 本就不上锁），记在 `LOCAL_CHANGES.md` 第 0011 节。被调函数在配对的知识库进程里用原代码执行（每个客户端进程一个，第一次访问时由客户端拉起，`knowledge: none` 零开销）；仓库、存储、写锁以句柄留在知识库进程，回调与可变参数留在客户端；连接用 `multiprocessing.connection`（Windows 命名管道），一次性 authkey，报文 JSON；MCP 子进程经知识库目录旁的 `<目录名>.run\<pid>.json` 找服务（文件里有一次性 authkey，进程被强杀时会留下）。审计钩子测试证明转写和纠错两半的进程不打开知识库下的文件、不 `sqlite3.connect`；`knowledge` 不是 `none` 而知识库包不在时开跑前拦下。闸门新增 `knowledge` 一项与 `--record knowledge`，K1–K8 共 7 个用例（K3、K6 并进 K1、K2，另加 K1b），在未改动的代码上录；为固定知识库日期重录过一次（附录 B 第 51 问）。76 个访问点里闸门跑到 49 个，没跑到的（本地 Agent 路由、读音提示、知识更新的修复轮与风格标记、联网研究、并行窗口的 repin 与重试）只靠原函数原样远程执行与静态测试保证。打包：桌面暂存工具与 `pyproject.toml` 收进 `src/finesub_knowledge`（附录 B 第 52 问）。交回 WP-P1：`scripts/engine_packages.py` 要改成从 `finesub_knowledge.server.ALLOWED` 找知识库包的入口（现在算出的知识库包只有 55 个文件，不对）；知识库进程改由核心分配端点（客户端已认 `FINESUB_KNOWLEDGE_ENDPOINT`）。下面是原始说明。

等 WP-P0 做完再开工，排在 WP-P1 之前。按第 0.1 节第 5 条、4.6 第 35a 条。

- 文件：WP-P0 第 10 问列出的上锁文件（只改访问方式）、知识库包的协议客户端与服务端（先放在 `third_party/finesub/` 之外、按「将来要搬进包」来写，WP-P1 搬进三个包）、`knowledge_worker.py`、`QUALITY_LOCK.json` 与 `tests/test_finesub_quality_lock.py`、对应测试、`third_party/finesub/LOCAL_CHANGES.md`。
- 内容：
  - 闸门没覆盖到的知识库路径，先按 WP-P0 的用例清单在未改动的代码上补录基线（用户批准后单独提交）。
  - 把每个访问点换成协议调用；协议的服务端在知识库包里，用原来的知识库代码原样完成读、匹配、写更新和加锁。
  - 更新质量锁：只有 WP-P0 清单上的文件哈希变；单独提交，提交信息写明第 0.1 节第 5 条。
- 完成标准：闸门全部通过，含 `--real`；LLM 请求序列、ASR 上下文文本、知识更新写出的文件与基线逐字节一致；转写和纠错两半的进程里不再直接打开知识库目录（测试断言）；知识库包不在时，开了知识库的请求被就绪检查拦下（测试）。

### WP-P1 分装成两个引擎包

等 WP-P0、WP-K、WP4c、WP5、WP-R7 做完再开工。做完之后才做 WP-H、WP-L、WP-CL。**按 4.6 第 35a 条分装成三个包**（转写包、纠错包、知识库包），下面写「两个包」的地方都按三个包理解。

- 文件：`engines/finesub-transcribe/**`、`engines/finesub-correct/**`（新建）；`third_party/finesub/**`（搬空后删除）；`src/nonoka_x/finesub_bridge.py`、`transcription/engines/finesub.py`、`correction/finesub.py`（搬进包）；`src/nonoka_x/engine_host.py`（新建）、`worker.py`、`local_provider.py`、`settings.py`、`projector.py`、`gpu_tier.py`、`axis.py`、`llm_worker.py`、`knowledge_worker.py` 里用 FineSub 的地方；桌面暂存工具与离线包脚本；`tests/quality_harness/`（只改定位与入口，基线不动）。
- 内容：
  - 按 WP-P0 的清单用 `git mv` 分装，共用底座复制成两份。`finesub_bootstrap` 原件（WP-R7 改过自下载的那份）整个进转写包。每个包生成自己的 `QUALITY_LOCK.json`（转写包的含 `finesub_bootstrap`）。
  - **建纠错兼容包** `engines/finesub-correct/src/finesub_bootstrap/`：按 4.2 第 16 条和 WP-P0 第 4 问的名单；默认位置按 D21 改向 `engines\finesub-correct\`；R0 文档 1.11 表里纠错那些名字的实现说明照用。
  - 按 4.6 实现：`PACKAGE.json`、进程协议、`engine_host.py`、worker 拆成两段进程、启动上下文、包内桥接、产物转换、私有附件目录、原始字幕渲染改由核心做。转写进程用 Python 环境层的解释器，纠错进程用启动器的解释器。
  - 核心里所有用 FineSub 的地方改成走协议；WP0 的过渡白名单清零。
  - 本包只做到「两段进程能跑、结果不变」；环境从零构建、`-I`、沙箱由 WP-H 做。
- 完成标准：
  - **包目录不许是「源码检出」的形状**：测试断言两个包里都没有 `pyproject.toml`。
  - **只搬不改**：两个包里每个 FineSub 文件的 sha256 等于分装前 `QUALITY_LOCK.json` 里同一路径的值；分装前上锁的 550 个文件每个至少出现在一个包里，没出现的逐个列出并说明为什么两边都不需要。
  - 兼容包：提供的名字与名单不多不少，import 链上没有 torch、numpy，不 import `nonoka_x`；纠错包里上锁文件对它的 import 全部可解析（测试）。
  - `src/nonoka_x/` 里搜不到 `finesub`、`FINESUB_`（测试断言，无白名单）；`third_party/finesub/`、`src/nonoka_x/finesub_bridge.py` 不存在。
  - 闸门全部通过，含 `--real`：默认路径下纠错包拿到的 stable 与基线按第 0 节第 3 条一致，LLM 请求序列逐字节一致。
  - **可删性**，各有自动测试：删掉转写包目录、删掉纠错包目录、两个都删，三种情况下核心的测试全过；前两种里留下的那个包照常跑完自己的闸门；第三种里一个云 API 引擎（打桩）能跑到 `raw-srt`。
  - 转写包的进程里 import 不到纠错包的文件，反之亦然（测试断言）。
  - 主数据目录和任务目录里，包的私有目录之外没有 FineSub 格式的文件。

### WP-H 密闭运行与沙箱（重写）

等 WP-P1、WP5 都做完再开工：它要封的是分包之后的最终形态（两个包各自的进程）。中性的那部分环境（系统必需变量、`PATH`、解释器隔离启动、沙箱目录与锚点）由核心的 `engine_host.py` 构建，`FINESUB_*` 由包内桥接从启动上下文生成。

- 文件：`src/nonoka_x/engine_host.py`（环境与沙箱）、两个包的桥接、`local_provider.py` 里拉起进程的地方、`tests/test_engine_hermetic.py`、`tests/test_engine_sandbox.py`、`scripts/quality_gate.py`（加沙箱写入检查）。
- 内容：
  - 按 4.5 实现：两个进程的环境从零构建、无条件写入、`-I` 启动（转写进程另加 `-B`）、入口设 `sys.path` 与标准输出编码；分类表以 R0 文档 1.7.3 为初稿，按 4.5 第 27 条调整（删掉 MSVC 一行；转写进程的用户目录与临时目录变量改为「Nonoka 设定」；加 `HF_HUB_OFFLINE=1`），1.7.2 里未逐项核实的库变量由读取审计补全。分类表放在 `engine_host.py`（中性部分）和包内桥接（`FINESUB_*`）里，是唯一的事实来源；每一项写明归类理由。
  - 转写进程的沙箱按 4.5 第 28 条：共用的 `sandbox\home\`、每进程的 `sandbox\temp\<进程>\` 与锚点、结束删、启动清残留。实测 `HOMEDRIVE`/`HOMEPATH` 改不改向的效果，结论交用户定（附录 C）。
  - 纠错进程按 4.5 第 29 条：不进沙箱，本地 Agent CLI 的变量按 D16 逐个列名带。
  - 2.5 列的每个漏点逐条关掉，包括 `setdefault`、`CUDA_VISIBLE_DEVICES` 的条件设置、sidecar 把 `FINESUB_*` 写进自己环境的做法；删掉 `_prepare_msvc_environment`（2.8）。
- 证明密闭的测试，两个进程都做：
  1. **覆盖扫描**：扫两个包里所有读环境变量的地方（含转写包的 `finesub_bootstrap` 原件和纠错兼容包），每个名字都必须在分类表里。
  2. **投毒测试**：把父进程环境里所有已知变量设成垃圾值，在当前目录、真实用户目录（测试里用假的）里放伪造的配置文件和密钥文件，然后构建进程环境、派生配置、跑调用等价测试。结果必须与干净父进程下的完全相同。
  3. **读取审计**：给进程装一个记录环境变量读取的钩子，跑一遍打桩的完整管线，引擎读过的每个名字都必须在分类表里，且读到的值都来自 Nonoka。
  4. **沙箱写入检查**（转写进程）：`--real` 时运行前后对沙箱外的受保护位置做快照比对，新增、删除、修改都是 0；层目录和引擎源码目录同样是 0（没有 `.pyc`、没有 `probe.json`）。
- 完成标准：
  - 四种测试通过；闸门全部通过；`quality_gate.py --real` 在密闭环境与沙箱下通过。
  - 加速状态：真实样本运行元数据里是 eager，没有因为任何原因打开。
  - **确认 token 计数来源和繁简折叠没有变**：在纠错进程下各跑一次改动前后的纠错，任务产物里记录的 `token_counter_source` 相同（仍是 `tokcount`，没有因为 `PATH` 重建找不到它而退回估算），`opencc` 可 import。回放基线不含这两条路径（WP-Q 的说明），只能在这里查。
  - 密钥读取、模型加载、ffmpeg 调用、本地 Agent CLI 路由（如果本机装了）各验证一次；没条件验证的如实标记。
  - 报告里列出最终的分类表，以及 4.5 第 31 条那些边界各自的现状。

### WP-CL0 云端模型预热脚本（已完成 2026-11-13）

> 交付：云端仓库 `modal_compute/nonoka_x_compute/operations/model_prime.py`（只读 Nonoka 的模型清单，不 import finesub、finesub_bootstrap；缓存布局与 `.finesub-verified.json` 标记的格式和引擎相同，测试把两边对上）与 `prime.py` 的 `check_models`（Volume 只读挂载，只检查）、`prime_models`（补齐缺的）、`prime_diarization_models`（改为只补说话人分离的两个）；`NONOKA_X_PRIME_VOLUME` 把它们指向另一个已存在的 Volume；`modal_compute/docs/compute.md` 更新。按附录 B 第 56 问：只放云端实际加载的 4 个（whisper、qwen-referee、nemotron3、speaker-embedding，分离器烤在 Vocal 镜像里）；和引擎用同一个库、同样的参数下载，脚本自己逐文件校验；已在但校验不过就报错、不动文件；没有哈希的小文件只看在不在；只有钉住的快照整个不在时才下载，下载后校验不过照引擎的规矩写失败标记。本仓库 `tests/quality_harness/cloud.py` 加测量用变量 `NONOKA_QUALITY_CLOUD_MODELS_VOLUME`（不设时行为不变，第 57 问）。实测：生产 Volume 只读检查 4 个全部在、校验通过，没有改动；空的临时 Volume 预热约 1 分钟放齐，之后 `--cloud` 通过、各项统计为 0，前后 Volume 上 149 项完全相同，日志里没有下载；云端测试 156 个通过。花费：已出账约 $0.28，第二次闸门那一小时未出账，估计合计 $0.33–0.43（用户批过中途重跑）。临时 Volume `nonoka-x-models-wpcl0-tmp` 经用户同意已删。交回：生产 Volume 上还有 6 个没人读的旧分离器文件，没动，清不清理由用户定。下面是原始说明。

- 文件：云端仓库 `modal_compute/nonoka_x_compute/operations/prime.py`（或新文件）及其测试。
- 内容：按 4.7 第 37 条写一个不经引擎的预热脚本：读本仓库的模型清单（钉住的 revision 与文件哈希），把云端要的模型放进 `/models` Volume 并逐文件校验；已经在的、校验通过的跳过。`prime_diarization_models` 改用它。不改云端的调度、镜像的其余部分和加速选择。
- 完成标准：在当前 Volume 上跑一遍，结果是「全部已在、校验通过」，没有改动任何文件；在一个空的临时 Volume 上跑一遍，放齐后引擎（还带着自下载）跑 `--cloud` 不再触发任何下载（看日志）；云端测试全过。交付说明里写明花费。

### WP-CL 云端重做：只留转写包

等 WP-P1 做完再开工。改 `nonoka-sub-cloud/modal_compute/` 和 Worker 里任务结果那一部分；LLM 转发路由和 `tokyo_llm/` 一个文件都不动（4.7 第 36 条）。开工前先列出 Worker 里要改的文件交回确认。

- 文件：云端仓库的 `modal_compute/**`、`src/jobs/` 里与结果产物有关的文件及其测试、需要时的 `migrations/`；本仓库 `desktop/internal/cloud/`（下载与提交的字段白名单）、核心里云端续跑的入口。
- 内容：
  - Modal 镜像只装 `engines/finesub-transcribe/`，经包的入口跑到 stable 并转出 `transcript.json`；不再装纠错相关的代码。镜像的 Python 依赖、加速选择照旧（4.7 第 37 条）；缺模型时任务报错，模型由 WP-CL0 的脚本预先放好。
  - 结果契约改为 `transcript.json` 加不透明附件；Worker 存、发，不解析。
  - 桌面侧云端续跑改成「转写结果已在手」：`transcript.json` 直接进核心，附件登记成私有附件。
  - `test_source_contract.py` 钉的哈希一并更新。
- 完成标准：
  - `quality_gate.py --cloud` 按 WP-C 批准的标准通过；云端测试和 Worker 测试全过。
  - 云端仓库的 `git diff` 不涉及 LLM 转发路由和 `tokyo_llm/`。
  - 一个新的云端任务从提交到桌面出 `final-srt` 走通，纠错拿到的 stable 是附件原件，LLM 调用照旧经 Nonoka Cloud 转发。
  - 删掉桌面上两个 FineSub 包，同一个云端任务能出到原始字幕。
  - 一个改动前存下的旧结果，经 4.7 第 36 条的一次性转换后仍能在桌面上接着跑。

### WP-L 媒体库同步改用 Nonoka 的格式

等 WP-P1 做完再开工；WP-M2 等它（本地旧任务的转换用同一份转换代码）。规则见 4.8 第 38 条。和 WP-CL 都要改云端仓库，两者不同时写同一个文件：WP-CL 管 `src/jobs/`，本包管 `src/library/`。

- 文件：
  - 本仓库：`src/nonoka_x/projector.py`、`local_provider.py` 的投影入口与产物清单、`desktop/internal/cloud/service.go`（同步与取回）、`relay.go`、`desktop/internal/plugins/engine.go` 的产物顺序、`desktop/frontend/src/providers/types.ts`、两个引擎包适配器里的转换代码、`docs/provider-spec.md`、`docs/architecture.md` 第 5 节。
  - 云端仓库：`src/library/**` 及其测试、需要时的 `migrations/`、一次性转换脚本（放 `modal_compute/nonoka_x_compute/operations/`，在装了两个包的环境里跑）。
- 内容：
  1. 定 `correction.json` 的格式：列出现在投影从 `annotated.csv`、`final.srt`、`correction_mode_json` 里读的每一项，一项不漏。
  2. 纠错包适配器产出 `correction.json`；投影改成只吃 `transcript.json` 加 `correction.json`。
  3. 桌面同步与取回、Worker 的媒体库白名单换成新名字，不留旧名字。Worker 对旧格式的同步请求返回明确的升级提示。
  4. 写一次性转换脚本：读云端每个媒体库条目和任务结果的旧产物，转成新格式，原件挪到备份前缀；可重跑，转过的跳过；出一份逐条目的结果报告。
  5. 写切换手册（4.8 第 38 条的顺序），含每一步的校验和回退办法。
- 完成标准：
  - **投影等价**：对 WP-Q 回放基线里每个用例的产物，旧投影和新投影得到的字幕文档内容相同；说话人、只纠错不翻译、日文轴各有用例。
  - 云 API 引擎只出原始字幕的任务能同步、能在另一个数据目录里取回并打开。
  - 转换脚本在一份云端数据的副本上跑完：条目数不变，每个条目都能投影，抽样与转换前的投影结果相同；重跑不产生变化。
  - `src/library/` 和桌面的同步代码里搜不到 `stable_json`、`annotated_csv`。
  - `go test ./...`、前端测试、Worker 测试全过；闸门通过。
- 不在本包自动执行：对线上数据的转换和切换。那是发版时由用户按手册执行或明确批准后执行的操作。

### WP-UI 引擎配置页

重构的其余部分做完之后开工：等 WP-P1、WP-L、WP-H、WP6、WP-E3，排在 WP8 之前，随同一个版本发布。规则见 4.9 第 39 条。

- 文件：`desktop/frontend/src/pages/RuntimePage.tsx`（及 CSS，改成引擎配置页）、`components/TranscriptionDialog.tsx`（去掉引擎选择）、`components/InstallLocationCard.tsx`、`StorageLocationsCard.tsx`、`StorageRelocation.tsx`、`runtimeCache.ts`、`bridge/runtime.ts`、`app/App.tsx`（导航与文案）、`pages/SettingsPage.tsx`（只移走转写引擎的密钥，其余不动）、`home/defaultRequest.ts`、`providers/types.ts`；Go 侧 `desktop/internal/preferences`（当前引擎的设置项）；Python 侧 `nonoka_x.runtime` 门面的层查询（WP-E3 已提供）。
- 内容：
  - 引擎列表由 `Capabilities.transcription_engines` 生成，单选；每项显示就绪状态、缺哪几层、下载量、占用空间、计费说明和「音频会离开本机」提示。
  - 选中未就绪的引擎时走安装流程：只下它缺的层，进度和失败重试沿用现有的安装界面。
  - 云 API 引擎的面板里放它自己的密钥配置和可选增强的安装开关。
  - 「清理未使用的」：列出将删除的层和能腾出的空间，确认后执行；被任务占用的层不可清理。
  - 当前引擎存成一项设置（`settings.json`）；新建任务时由它填入请求的 `transcription.engine`。对话框显示当前引擎和去配置页的入口，引擎不支持的选项（计算设备、显卡档位、人声分离）不显示，说话人选项按引擎的 `features` 给。
  - 当前引擎被删掉（包被移除）或变得不可用时，页面提示并要求重选，不静默回落到别的引擎。
- 完成标准：
  - `npm run typecheck`、`npm test`、`go test ./...` 通过。
  - 用预览实际走一遍：在各引擎之间切换；选一个未安装的引擎并装完；装到一半取消再继续；清理未使用的层；删掉转写包后页面的表现；当前引擎不可用时新建任务被拦住。
  - 从 FineSub 换到云 API 引擎再换回来，没有任何重新下载。
  - 设置页里纠错翻译、LLM、知识库相关的部分与改动前逐项相同。
  - 选 FineSub 时发出的请求与 WP6 完成时相同。

### WP7 云端仓库与文档

- 云端仓库：确认 `modal_compute/nonoka_x_compute/phases.py` 在 WP-B 之后仍调得通（它不传 `asr`，预期不用改）；云端测试全过。需要改时提交在 nonoka-sub-cloud 仓库；`test_source_contract.py` 钉了哈希的文件动了要一起更新。
- 本仓库文档：`docs/architecture.md`（架构图加引擎层与纠错层，FineSub 画成两个可插拔部件，运行环境画成三层预制包与沙箱）、`docs/provider-spec.md`、新增 `docs/transcription-engines.md`、`CHANGELOG.md`（写明数据目录的新布局、迁移会清空旧环境并重新下载、关闭加速后变慢）。

### WP8 集成验证

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

### A.1 时间线

- **2026-11-10**：计划初稿（基于 `main@1f14db3`）。
- **2026-11-11 上午**：FineSub 拆成「FineSub 转写引擎」和「finesub correction」两个部件；`finesub_bootstrap` 解锁并整个删除，引擎需要的东西全由 Nonoka 提供（由兼容包提供 48 个名字）；`third_party/finesub` 只留核心文件；数据目录可以改名和搬动，旧版用户由迁移工具搬过来；真实样本的比对标准（第 0 节第 3 条）。
- **2026-11-11 复核**：新增 WP-C；WP-M 拆成 WP-M1、WP-M2；WP-R7 排到 WP-R6 之后，WP-H 排到 WP-R7 之后；LLM 回放基线保持现状（不含 tokcount 与 opencc 生效的路径）；引擎线与运行时线一次性发布。
- **2026-11-11 彻底隔离**：FineSub 代码在 WP-B、WP1 之后冻结，原样分装成两个引擎包，核心零引用；两个包各跑各的进程；`stable.json` 等原生产物是转写包的私有附件；知识库归纠错包；云端只留转写包并重做；媒体库同步改成 Nonoka 的格式（一次做完，不留兼容期）；「运行环境」页改成引擎配置页、全局单选（云端 FineSub 是列表里的一项）。
- **2026-11-11 WP0**（向用户确认的细节）：契约纯 JSON 可往返，`native` 是不透明附件；`EngineSpec.kind` 增加 `cloud-service`，云端 FineSub 的 id 是 `finesub-cloud`；`register_key_spec` 只登记到内存；边界测试按 AST 判；过渡白名单按「文件 × 越界种类」记；`transcript.json` 的校验规则（5.2）；`EngineSpec` 加可选 `billing`、`features` 加 `languages`；`transcription/model.py` 只放 `transcript.json` 的类型，`Unit` 等搬到 `units.py`。
- **2026-11-11 WP-R0**：23 项设计答复 D1–D23（R0 文档第 0 节）。对计划的更正：密钥只走环境变量（原第 32 条「必须落盘时」作废）；迁移时不认识的键丢弃（原第 18 条「原样透传」作废）；`%LOCALAPPDATA%\FineSub` 总是被用、检出判定挡不住编译缓存（2.5 的更正）。
- **2026-11-11 沙箱实验**（实验报告）：预制环境包与进程级沙箱两个前提有条件成立；用户在实验中决定删掉 `finesub_bootstrap` 的自下载。
- **2026-11-11 编译加速关闭**（提交 `85ae2b9`）：桌面端关闭分离器 AOTI/JIT 与 qwen 裁判加速；环境基线和真实样本基线按批准重录。
- **2026-11-11 预制环境包 + 进程级沙箱**（用户决定，本次整理的起因）：
  - A. 转写引擎改成「预制环境包 + 进程级沙箱」：下载自托管的预制包、解压即用，托管在 Cloudflare R2，分 Python 环境、模型、工具三层；Python 也带在包里（PBS，不建 venv），不用系统 Python；建立可重复、带清单、能证明与 `RUNTIME_PINS.json` 一致的构建流水线；再分发许可已核对；国内线路靠用户的本地代理，不做镜像；迁移时模型搬过去不重下、Python 环境重新下载、旧环境清空；沙箱给引擎进程独立的用户目录和临时目录，沙箱里原样带 `finesub_bootstrap`，转写引擎不需要兼容包；去掉 `finesub_bootstrap` 的自下载；纠错引擎不动，不进沙箱，继续跑在启动器环境里。
  - B. 编译加速全部关闭（见上）。MSVC 依赖消失，AOTI 产物不进模型包。
  - C. 云端保持现状，云端基线只能用较弱的标准（标准见附录 B 第 5、11 问）。
  - D. 锁里 `onnx` 和 `onnx-weekly` 互相覆盖，从锁里去掉一个（去掉哪个见附录 B 第 4 问）。
  - E. 新方案必须遵守的约束：每个引擎进程有自己的临时目录；`.pyc` 不能写进引擎源码目录；模型层的 `probe.json` 改写（查实加速关闭后不再发生，2.8）；模型层里的符号链接；引擎目录里不放 `pyproject.toml`。
  - 同日把计划整理成本版（附录 B 第 1 问）。
- **2026-11-12 知识库独立拆包**（附录 B 第 37–40 问）：知识库成为第三个包，只有它碰知识库数据，转写和纠错两半经协议访问；为此批准改上锁文件里的知识库访问点（第 0.1 节第 5 条），新增 WP-K；数据放 `engines\finesub-knowledge\`；知识库包不在时就绪检查拦下。起因是 WP4a 交回：转写引擎做 ASR 上下文要读知识库，而原计划知识库归纠错包。

### A.2 旧条款编号对照（整理前 → 本版）

| 整理前 | 本版 | 说明 |
|---|---|---|
| 第 0 节第 1–4、6、7 条 | 第 0 节同号 | 第 2 条改写了 `finesub_bootstrap` 的去向 |
| 第 0 节第 5 条「运行环境的内容等价」 | 第 0 节第 5 条 | 改写：证明办法改为按层的逐文件清单比哈希 |
| — | 第 0.1 节 | 新增：用户批准过的偏离 |
| 第 1 节第 5 条「边界收成四个文件」 | 第 1 节第 5 条、第 5 节末的过渡形态 | 改写为彻底隔离；四个文件只是过渡 |
| 第 1 节第 6 条「运行时由 Nonoka 管理」 | 第 1 节第 6 条 | 改写为预制环境包 |
| 第 1 节第 9、10 条 | 第 1 节第 9、5 条 | 第 9 条加了沙箱 |
| 第 4 节第 1–11 条 | 4.1 第 1–11 条 | 第 2 条：`run_pipeline` 分包后不再需要；第 6 条「托管运行时」改为 Python 环境层；第 7 条密钥存进 `secrets.json` |
| 第 12 条 安装管理归 Nonoka | 4.2 第 12 条 | 改为层管理器 |
| 第 13 条 清单是 Nonoka 的 | 4.2 第 13 条 | 加了 cn 锁不复制 |
| 第 14 条 配置和密钥归 Nonoka | 4.3 第 21 条 | |
| 第 15 条 桥接模块派生 | 4.3 第 22 条 | 不再派生密钥文件（D3） |
| 第 16 条 `finesub_bootstrap` 由兼容包取代 | 4.2 第 16 条 | **推翻**：转写包原样带原件（只删自下载），纠错包用兼容包；「缺模型时自己下载」由「保留」改为「删掉」 |
| 第 17 条 数据目录 | 4.4 第 24 条 | 大文件根内部改按层划分（D5 被取代） |
| 第 18 条 无损迁移、不重新下载 | 4.4 第 25 条 | **推翻一半**：模型不重下；Python 环境和工具清空后重新下载 |
| 第 19 条 环境需求向运行时管理器声明 | 4.2 第 15 条 | 组件改为层 |
| 第 20 条 只在 worker 里跑 | 4.5 第 26 条 | |
| 第 21 条 环境从零构建 | 4.5 第 27 条 | 加了转写进程的沙箱变量；MSVC 变量删掉 |
| 第 22 条 解释器隔离启动 | 4.5 第 28、29 条 | 转写进程加 `-B` |
| 第 23 条 文件发现封住 | 4.5 第 28、29 条 | 转写进程靠沙箱封，纠错进程靠兼容包封 |
| 第 24 条 外部的东西先成为设置 | 4.5 第 30 条 | |
| 第 25 条 密闭的边界 | 4.5 第 31 条 | MSVC、系统 Python 不再是边界；加了沙箱管不到的写入、VC++ 运行库 |
| 第 26 条 三种证明 | 4.5 第 32 条 | 两个进程都做，转写进程加沙箱写入检查 |
| 第 27–31 条 分包 | 4.6 第 33 条 | 合并 |
| 第 32 条 启动上下文与密钥 | 4.3 第 22、23 条，4.6 第 33 条 | 启动上下文去掉 `runtime.manifests`、`download` |
| 第 33 条 产物与私有附件 | 4.6 第 34 条 | |
| 第 34 条 云端只留转写包 | 4.7 第 36 条 | |
| 第 35 条 按将来要搬进包来写 | 4.6 第 35 条 | 兼容包不再提前写 |
| 第 36 条 媒体库同步 | 4.8 第 38 条 | |
| 第 37 条 引擎配置页 | 4.9 第 39 条 | 组件改为层 |
| — | 4.2 第 14、17–20 条，4.7 第 37 条 | 新增：三层、构建规则、R2 托管、层的版本与核对、许可、云端运行环境 |

工作包：WP-R3 由 **WP-E3 层管理器**取代；新增 **WP-E1 构建流水线**、**WP-E2 R2 托管与发布**、**WP-CL0 云端模型预热脚本**；**WP-R7** 由「删除 `finesub_bootstrap`、建兼容包」改为「删自下载、vendor 只留核心」（兼容包移到 WP-P1，只给纠错包）；**WP-H** 改为两个进程的密闭运行加转写进程的沙箱，去掉 AOTI 检查；**WP-M1、WP-M2** 改为模型搬、环境清空后重下；**WP-R1** 加了去掉 `onnx` 和钉住解释器；**WP-R2** 去掉镜像与线路；**WP-R6** 去掉 GitHub 镜像、离线包改装层；**WP-C** 改为结构与统计标准；WP-P0、WP-P1、WP-UI、WP-CL、WP8 按层与沙箱调整。

### A.3 R0 文档 D1–D23 的现状（用户 2026-11-11 确认，附录 B 第 12 问）

| 编号 | 现状 | 说明 |
|---|---|---|
| D1、D2、D3、D4、D6、D7、D8、D11、D11c、D13、D14、D15、D17、D19、D20、D22 | 有效 | D3、D17、D22 对两个包都适用 |
| D9 | 有效 | 对两个进程都适用；转写进程的 `PYTHONPATH` 只含转写包的源码目录（其中是原样的 `finesub_bootstrap`） |
| D16 | 有效 | 只给纠错包进程 |
| D5 | **已被取代** | 大文件根仍叫 `runtime`，内部改为 `layers\<层>\<版本>\`（4.2 第 19 条） |
| D10 | **已被取代** | 不再用系统 Python（4.2 第 14 条） |
| D12 | **已被取代** | 组件改为三层（4.2 第 14、15 条） |
| D23 | **已被取代** | 没有 venv，旧环境清空（4.4 第 25 条） |
| D18 | **部分取代** | `runtime-state\`、不要 `locations.json` 仍有效；不做镜像后不再有下载线路状态 |
| D21 | **部分取代** | 只对纠错包的兼容包有效；转写包没有兼容包，默认位置由沙箱改向；`models`、`cache` 两项不再需要 |

R0 文档里这几条已按此加了标注（只加标注，不删原文）。

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

更早的问答：WP-R0 的 D1–D23 见 R0 文档第 0 节；WP0 的细节见附录 A.1；沙箱实验中的决定见实验报告。

## 附录 C 还没定的事

开工到相应工作包时先问用户，不要自己定。

| # | 事项 | 什么时候问 | 现有线索 |
|---|---|---|---|
| 1 | ~~云 API 引擎的可选增强跑在哪~~ | 已答复 | 见附录 B 第 42 问：第一版不做增强，后续阶段再定 |
| 2 | 转写进程的 `HOMEDRIVE`、`HOMEPATH` 是否一并改向 | WP-H 实测后 | 实验报告 1.6：没观察到需要，但没单独验证 |
| 3 | 云端统计标准的具体阈值 | WP-C 实测后 | 附录 B 第 11 问 |
| 4 | ~~VC++ 运行库~~ | 已答复 | 见附录 B 第 30 问 |
| 5 | ~~cn 锁不复制进 Nonoka 的清单~~ | 已答复 | 见附录 B 第 23 问 |
| 6 | ~~模型层包含哪些模型~~ | 已答复 | 见附录 B 第 53 问：只放默认路径的 5 个 |
| 7 | ~~macOS 的工具层在哪里构建~~ | 已答复 | 见附录 B 第 31 问；macOS 工具层另定 |
| 8 | ~~R2 上旧层版本的保留期限，以及桶名、域名~~ | 已答复 | 见附录 B 第 59、60 问 |
| 9 | 迁移「先删后下」期间，界面要不要提示改用云端 FineSub 或云 API 引擎 | WP-M2 开工前 | 工具层装好后云 API 引擎即可用 |
