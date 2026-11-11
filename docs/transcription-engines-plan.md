# 转写引擎重构计划

> 状态：WP-Q 已完成（2026-11-11），其余未开工。本文档是交给子代理执行的工作说明，每个工作包可以单独领走。
> 写于 2026-11-10，基于 `main@1f14db3`、FineSub `v0.7.0@7123055`。
> 2026-11-11 修订（用户决定）：FineSub 拆成可插拔的「FineSub 转写引擎」和「finesub correction」两个部件；`finesub_bootstrap` 解锁并整个删除，引擎需要的东西全由 Nonoka 提供；`third_party/finesub` 只留核心文件；数据目录可以改名和搬动，FineSub 相关文件移出主数据目录，旧版用户由迁移工具搬过来；真实样本的比对标准见第 0 节第 3 条。

> 2026-11-11 复核修订（用户决定）：新增 WP-C，动代码前先录云端基线；WP-M 拆成 WP-M1（迁移核心，提前做）和 WP-M2（启动集成、界面、续跑与回滚）；WP-R7 排到 WP-R6 之后，WP-H 排到 WP-R7 之后；LLM 回放基线保持现状（不含 tokcount 与 opencc 生效的路径）；引擎线与运行时线仍一次性发布。
> 2026-11-11 彻底隔离（用户决定）：FineSub 的代码在 WP-B、WP1 之后冻结，原样分装成两个互相独立的引擎包（转写包、纠错翻译包），Nonoka 核心对 FineSub 零引用，删掉任何一个包或两个都删，其余照常运行；两个包各跑各的进程；`stable.json` 等原生产物是转写包的私有附件；知识库归纠错包；云端只留转写包并重做。规则见第 4 节第 27–35 条，**与前文冲突的地方以这几条为准**。

## 0. 质量红线（高于本文档其余一切）

**FineSub 原先的转写、纠错、翻译质量必须保住。** 编排可以拆、可以重组，但结果不能变。

落到可检查的规则：

1. **算法文件上锁。** `third_party/finesub/src/finesub/` 下默认全部文件不许改，包括识别、分离、稳定化、说话人分离、纠错翻译的全部 LLM 代码、提示词模板、配置默认值。运行时锁（`pylock.*.toml`）、运行时清单和模型清单原在 `finesub_bootstrap` 里，随它解锁后**按内容**上锁：钉住的每一项记在 `src/nonoka_x/resources/RUNTIME_PINS.json`，由 `tests/test_runtime_pins.py` 看住，放在哪个文件里都一样。
2. **只解锁本计划必须动的四处**，它们是编排、入口和安装设施，不是算法：
   - `finesub/stages.py`（`run_pipeline` 所在，要拆）
   - `finesub/pipeline.py`（命令行前端，跟着删选项）
   - `finesub/speech/joint/`（联合 ASR 后端，整个迁走）
   - `finesub_bootstrap/`（安装、配置、运行时读取设施，整个删除，由 Nonoka 提供，见第 4 节第 16 条；用户 2026-11-11 决定）
3. **解锁的地方改完，FineSub 路径的结果必须和改之前一致**，由回归闸门证明（第 6 节 WP-Q）。「一致」默认是逐字节；WP-Q 用未改动的代码连跑两次查明以下产物本身就不能逐字节复现，用户 2026-11-11 批准按下面的标准比：
   - `-stable.json`、`-aligned.json`：顶层 `metadata` 以外逐项相同（`metadata` 里是各阶段的耗时）；
   - `-vocal.ogg`：解码出的采样相同（Ogg 流序列号是随机的）；
   - `-vad.json`：去掉 `source.mtime_ns`（人声文件每次重写）、顶层 `timing` 和所有以 `_sec` 结尾的耗时字段后逐项相同。
   其余产物（`-raw.srt`、`-diar.json`、`-diar-rows.json`、`-diar-evidence.npz`、`-vad-energy.npz`、LLM 请求序列、`final.srt`、`annotated.csv`）仍是逐字节。工作包里写「与基线逐字节一致」的地方，一律指这条标准。
4. **Nonoka 传给 FineSub 的参数和环境，重构前后等价。** 同一个任务请求最终传进去的每个参数、环境变量、生成的 `finesub.toml` 和模型目录都和现在一样。这里的「现在」指**干净环境下的现在**：机器上没有任何会影响 FineSub 的外部环境变量和外部配置文件时的行为。引擎改成密闭运行之后（第 4 节第 20–26 条），所有机器都得到这个行为。
5. **运行环境的内容等价。** 运行时和配置改由 Nonoka 自己管理（第 4 节第 12–17 条），但装出来的东西不能变：Python 包的名称、版本、wheel 哈希，模型的版本与文件哈希，ffmpeg 等工具的版本，以及决定加载哪个模型的常量，都与 `RUNTIME_PINS.json` 一致。改任何一个钉住的版本都属于第 7 条。目录可以改名和搬动（第 4 节第 17 条），文件内容不能变。
6. **闸门不过就停下来报告**，不许为了过闸门去调参数、改算法文件或放宽比对标准。
7. 要动上锁的文件或钉住的版本（包括升级上游），是单独的、需要用户明确批准的事，不属于本计划。重录闸门基线（`scripts/quality_gate.py --record`）同样要用户批准，并单独提交。

子代理如果发现某个工作包不动上锁文件就做不成，停下来交回，不要自己找变通。

## 1. 目标

**最终目标（用户 2026-11-11）：FineSub 完全独立，成为两个可插拔的部件——「FineSub 转写引擎」和「finesub correction」。FineSub 转写引擎与其他转写引擎地位相同，随时可以替换；finesub correction 接任何转写引擎的结果。** 下面各条都服从这个目标。

1. **去掉整套上游对齐机制。** 不再用同步脚本、补丁栈、「可由基线加补丁重建」来约束 `third_party/finesub`。以后升级上游时只跟三块关键算法：FineSub 的转写引擎、纠错层、翻译层。
2. **转写引擎是可选的一等概念**，FineSub 只是其中一个，没有特权：worker、provider、就绪检查、界面里没有「FineSub 专属」的分支，它和别的引擎走同一个注册表、同一套 `EngineSpec`、同一个 `transcript.json` 出口。换掉它（或将来不装它）不影响其余部分。
3. **MAI-Transcribe-2、Gemini 3.5 Transcribe、MOSS Pro 由 Nonoka X 自己维护**：API 客户端、拼窗、断行、密钥、配额、缓存、就绪检查和运行环境都归 Nonoka，不再是 FineSub 的 `--asr` 后端。
4. **纠错翻译继续用 FineSub 的，单独封装成 finesub correction**，和 FineSub 转写引擎互不依赖：两者各自声明运行环境，各自就绪检查；只用其中一个时不需要另一个的任何东西。
5. **FineSub 的边界收成四个文件**：`transcription/engines/finesub.py`（转写引擎）、`correction/finesub.py`（纠错）、`finesub_bridge.py`（派生 FineSub 要的配置、密钥文件和环境变量）、兼容包 `src/finesub_bootstrap/`（第 4 节第 16 条）。Nonoka 其余代码不 import `finesub`、不知道 `FINESUB_*`、不读写 FineSub 的文件格式，由测试强制。这四个文件是 WP-P1 之前的过渡形态；WP-P1 把它们收进两个引擎包，此后 `src/nonoka_x/` 对 FineSub 零引用，没有例外（第 4 节第 27 条）。云 API 引擎的断行改用 Nonoka 自己的一份句子 DP（第 4 节第 5 条）。
6. **运行时环境和配置文件由 Nonoka 自己管理**，`finesub_bootstrap` 整个删除：装什么、装在哪、怎么下载校验修复、配置和密钥存在哪、是什么格式，都由 Nonoka 决定。FineSub 需要的环境变量和配置文件由 Nonoka 在启动 worker 时派生出来喂给它。
7. **`third_party/finesub` 只留核心文件**：引擎源码 `src/finesub/`、许可证文件（`LICENSE`、`LICENSES.json`、`PROMPT_LICENSE.md`）、`UPSTREAM.json`、`QUALITY_LOCK.json`、`LOCAL_CHANGES.md`。其余（`finesub_bootstrap/`、`FILES.json`、`UPSTREAM_README.md`、`VERSION`、vendor 层的 `pyproject.toml`、`README.md` 等）在确认没人读之后删掉或改由 Nonoka 提供。
8. **数据目录重新组织**：所有数据目录都可以改名和搬动；FineSub 相关的文件（派生配置、模型目录文件、密钥文件、引擎状态、运行时与模型）不再放在主数据目录下。**旧版用户由迁移工具无损搬到新布局**，迁移工具随第一个采用新布局的版本发布。
9. **FineSub 引擎密闭运行**：运行时不接受任何外部影响，只接受 Nonoka 的调用。机器上的环境变量、用户目录或当前目录里的配置文件都不能改变它的行为；要影响它，只能通过 Nonoka 的设置和任务请求。

10. **彻底隔离**（用户 2026-11-11）：FineSub 随时可以删掉而不影响 Nonoka 运行；它需要的运行时和数据全部由 Nonoka 给；它的产物由引擎包转换成 Nonoka 的格式后交给 Nonoka 保存。做法是把 FineSub 分装成两个引擎包，见第 4 节第 27–35 条。上面第 5、7 条描述的是分包之前的过渡形态。

不在本次范围：改 FineSub 的算法、升级 FineSub 版本、改 EditDocument 模型。云端 Modal 的调度原先不在范围内，现在由 WP-CL 重做（第 4 节第 34 条）。

## 2. 已核实的事实

执行前不用再查，直接当前提用。

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
| FineSub 是 GPL-3.0-or-later（提示词模板 CC-BY-SA-4.0），Nonoka X 是 GPL-3.0，直接修改和搬移在许可上可行，声明文件要保留 | `third_party/finesub/LICENSES.json`、`LICENSE` |

### 2.2 `speech/joint/`（约 5300 行）的依赖

- **纯 Python**：`model.py`、`stitch.py`、`cache.py`、`quota.py`、`repair.py` 的文本对齐，三个云后端的请求与解析，Gemini 的时间修复/复读检测/续问，MOSS Pro 的输出上限续问与错文种重问。
- **断行 `lines.py` 调用 `speech/postprocessing/segmentation.py` 的句子 DP**。后者只 import 标准库和 FineSub 的纯 Python 模块（`settings`、`subtitles.metrics`、`text`、`recognition.segments`）。
- **需要托管运行时**：`SpeechSpans`（Silero，空档重问用）、`relink.py`（声纹重连说话人）、`sbd.py`（断句模型）、`aligner.py`（强制对齐，要 GPU）、`moss_timing.py`（用 Silero 帧概率给 MOSS 段内定时）、`live.py`（Gemini Live 修补）、`moss_local/`（本地 MOSS，要 GPU）。

### 2.3 现有的上游对齐机制

- `scripts/sync_finesub.py`（`sync` / `check` / `baseline`，553 行）
- `patches/finesub/0001`–`0008` 加 `BASELINE_FILES.json`。8 个补丁**已经应用在 `third_party/finesub/src` 里**，删掉补丁文件不改变引擎代码。
- `third_party/finesub/FILES.json`：当前每个文件的哈希清单。
- `third_party/finesub/UPSTREAM.json`：上游仓库、tag、commit、版本、`engine_bundle_id`，以及补丁列表和若干汇总哈希。
- `tests/test_finesub_patch_stack.py`、`tests/test_sync_finesub.py`
- CI 的 `Verify generated FineSub snapshot` 步骤（`.github/workflows/ci.yml`）
- `docs/engine.md` 第 1–3 节的规则，`third_party/finesub/README.md`

读 `UPSTREAM.json` 的 `commit`、版本或 `engine_bundle_id` 的地方很多（worker、local_provider、桌面暂存工具、离线包脚本、云端镜像、云端媒体库），所以这个文件和这几个字段保留不动。

`desktop/internal/app/bundled/nonoka_x/` 是构建时暂存出来的副本，不要手改。

### 2.4 运行时与配置现在怎么依赖 FineSub

`finesub_bootstrap`（约 11600 行）同时扮演三个角色：

| 角色 | 谁在用 | 具体内容 |
|---|---|---|
| **安装管理** | Nonoka：`provision.py`、`runtime_install.py`、`model_install.py`、`download_route.py`、`asset_digest.py`、`config_file.py` | 按 `pylock.win-py312*.toml` 建虚拟环境（`environment.RuntimeEnvironment`）；按 `runtime-manifest.json` 装 ffmpeg、tokcount 等工具（`ResourceManager`、`shell.resource_specs`）；按 `model-manifest.json` 下载校验模型（`model_fetch`、`model_manifest`）；断点下载、镜像路由、目录布局（`AppPaths`）、原子写、文件锁、进程树 |
| **配置与密钥** | Nonoka：`settings.py` | 密钥存在 `finesub.env`，由 `finesub_bootstrap.secrets` 加保护读写；配置写在 `finesub.toml`；模型目录写在 `model_catalog.toml`。三者加状态文件、知识库目录通过 `FINESUB_ENV_FILE`、`FINESUB_CONFIG_FILE`、`FINESUB_MODEL_CATALOG`、`FINESUB_STATE_FILE`、`FINESUB_KNOWLEDGE_ROOT` 指给引擎（`settings.py:621`） |
| **引擎运行时读取** | FineSub 自己的上锁文件：`paths.py`、`state.py`、`api_keys.py`、`speech/runtime/hf_weights.py`、`vad_asr_stage.py`、分离器、两个说话人后端、`llm/client.py` 等三十多处 | 解析目录、读密钥、读写状态、确认并在缺失时自行下载钉住版本的模型（`model_ensure`）、子进程不弹窗、token 计数器 |

要点：

- 第三个角色在上锁文件里：上锁的引擎文件运行时要 import `finesub_bootstrap` 里的 **48 个名字**（WP-Q 用 AST 扫出来，清单在 `tests/fixtures/quality/bootstrap-surface.json`），分属 17 个模块。按对结果的影响分三类：
  - 直接决定结果：`model_ensure`（加载哪个 revision 的模型）、`model_caches` / `model_manifest`（分离器用哪个 checkpoint、whisper 用哪个仓库）、`token_counter`（token 计数，决定分窗，进而决定提示词）、`secrets`（读密钥）、`artifacts`（产物文件名常量）；
  - 基础设施：`fsops`、`locks`、`processes`、`logs`、`paths`、`http_client`、`download_routes`、`model_fetch`、`hf_verify`、`agy_records`；
  - 纯数据：两份 pylock、运行时清单、模型清单、下载源（内容已记入 `RUNTIME_PINS.json`）。
  所以 `finesub_bootstrap` 可以删，但删之前 Nonoka 必须以同名包提供这 48 个名字（第 4 节第 16 条）。
- 引擎留了环境变量入口：`FINESUB_MODEL_DIR`、`FINESUB_MODEL_MANIFEST`、`FINESUB_DOWNLOAD_SOURCES`、`FINESUB_HF_ENDPOINT`、`FINESUB_CONFIG_FILE`、`FINESUB_ENV_FILE`、`FINESUB_STATE_FILE`、`FINESUB_KEYRING`。Nonoka 可以把它指向自己管的目录和自己派生的文件。
- worker 的环境变量由 `provision.worker_environment` 组装（`provision.py:968`）；现有安装位于 `<数据目录>/finesub/{runtime,models,cache}`。
- macOS 的运行时清单已经是 Nonoka 自己的（`src/nonoka_x/resources/runtime-manifest.macos-*.json`），有先例。
- 桌面脚本 `desktop/scripts/resource-sources.mjs`、`build-offline-package.mjs`、`publish-github-mirror.mjs` 读 FineSub 的清单来准备离线包和镜像。

### 2.5 外部环境现在从哪里漏进引擎

| 漏点 | 位置 |
|---|---|
| worker 的环境变量以 `os.environ.copy()` 为底，再叠加 Nonoka 的设置。机器上已有的任何变量都原样传给引擎 | `provision.py:968`、`local_provider.py` 的 `_engine_environment` |
| 知识库目录和状态文件用 `setdefault` 设置，机器上已有同名变量时外部值优先 | `settings.py:625`、`:626` |
| `CUDA_VISIBLE_DEVICES` 只在外部没设时才由 Nonoka 设 | `worker.py:593` |
| 三种 worker（任务、LLM、知识库）都用普通的 `python -m` 启动，没有隔离参数，`PYTHONPATH`、用户级 site-packages、`PYTHONSTARTUP` 都生效 | `local_provider.py:921`、`:937`、`:944` |
| sidecar 主进程自己也 import FineSub：读模型路由和模型目录、解析说话人产物、取显卡档位选项。主进程的环境是完全未经处理的 | `settings.py:365`、`:384`、`:476`、`:947`、`:1064`，`projector.py:224`，`gpu_tier.py:48` |
| FineSub 和 `finesub_bootstrap` 自己读四十多个 `FINESUB_*` 变量，其中有直接改行为的（`FINESUB_SEPARATOR_ACCEL`、`FINESUB_REFEREE_ACCEL`、`FINESUB_OVERLAP_CANDIDATES`、`FINESUB_SPEAKER_JUDGE_MARKS`、`FINESUB_TASK_STRATEGY`、`FINESUB_KB_SLIM` 等），还有决定去哪找数据的（`FINESUB_HOME`、`FINESUB_BIG_DATA_DIR`） | 全仓 grep |
| 它依赖的库也读环境变量：Hugging Face、torch、CUDA、OpenMP、numba、Triton、代理、证书等 | 未逐项核实，WP-R0 查 |
| `third_party/finesub` 本身满足 FineSub 的「源码检出」判定，`FINESUB_CHECKOUT_DATA` 不为 `0` 时引擎会去这里找 `.env`、`config.toml`、`model_catalog.toml`。现在靠 `settings.bind_environment` 设成 `0` 挡住 | `finesub/paths.py:140`、`settings.py:624` |
| 没有显式变量时，引擎回落到 `%LOCALAPPDATA%\FineSub`（托管数据根）、`~/.cache/finesub`、`~/.finesub/state` | `finesub_bootstrap/paths.py:127`、`finesub/paths.py:261`、`:316` |

### 2.6 WP-Q 查实的事实（2026-11-11）

- **可复现性**：同一份代码、同一台机器，两次跑到 `raw-srt`：`-raw.srt`、`-diar.json`、`-diar-rows.json`、`-diar-evidence.npz`、`-vad-energy.npz` 逐字节一致；`-stable.json`、`-aligned.json` 只差 `metadata` 里 24–29 个耗时字段；`-vad.json` 只差 `source.mtime_ns` 和耗时；`-vocal.ogg` 字节不同、解码采样相同。比对标准见第 0 节第 3 条。
- **外部变量的影响**：录基线时去掉了本机 43 个可能有影响的变量（`CUDA_PATH`、`CUDA_PATH_V11_8`、`NVTOOLSEXT_PATH`、`INTEL_DEV_REDIST`、`MIC_LD_LIBRARY_PATH`、`ANTHROPIC_*`、`CLAUDE_*` 等）。去掉前后识别结果相同，分离器 AOTI 加速仍生效。
- **已装运行时**：98 个包版本与锁一致，7 个工具都是钉住的版本，管线用到的模型（分离器、whisper、qwen 裁判、nemotron3、speaker-embedding）文件哈希全部匹配。
- **LLM 调用的汇合点**：所有 API 路由的调用都经过 `finesub.llm.llm_runtime.chat_complete(messages, **call_kwargs)`，`call_kwargs` 里是 provider、model、温度、`max_tokens`、thinking、seed 等全部参数。闸门在这里截获。
- **旧版数据布局**（迁移工具的输入，WP-R0 要补全）：
  - 主数据目录 `%APPDATA%\Nonoka X`：`finesub.env`、`finesub.toml`、`model_catalog.toml`、`state`、`knowledge/`、`finesub-data/`（`download-routes.json`、`locations.json`）、`bootstrap/`（启动器自己的 Python 与工具）、`tasks/`、`documents/` 等，以及 `storage.json`（`runtimeDir` 指向大文件根）。
  - 大文件根：默认 `<主数据目录>/finesub`，可由 `storage.json` 改到别的盘（本机是 `D:\AIProject\nonoka-sub-envir\Nonoka X\finesub`），下有 `runtime/{python,ffmpeg,uv,yt-dlp,tokcount,aria2c,node,pot-provider}`、`models/{audio-separator,huggingface,torch}`、`cache/`、`.finesub-store.json`、`register-location.cmd`。
  - `%LOCALAPPDATA%\FineSub`：本地 Agent 的 capsule 目录、`download-routes.json`、`user-data/`。
  - 开发检出：`<仓库>/.local`（`scripts/run_local_sidecar.py`）。

## 3. 新的对齐规则

### 3.1 拆掉什么，留下什么

| 拆掉 | 换成 |
|---|---|
| 同步脚本、补丁栈、基线文件、正反向重放测试 | 无 |
| `FILES.json`（全部文件的哈希，用于证明「可重建」） | `QUALITY_LOCK.json`：上锁文件的哈希，用于证明「没被动过」 |
| 「不 fork、vendor 是生成产物、改动必须整理成补丁」的规则 | 第 0 节的质量红线 |
| CI 里「可由上游加补丁重建」的校验 | CI 里的质量锁测试 |
| `finesub_bootstrap/`（安装、配置、引擎运行时设施） | Nonoka 的 `runtime/`、`config/`、`finesub_bridge.py`，以及兼容包 `src/finesub_bootstrap/`（只提供上锁引擎要的 48 个名字） |
| `pylock.*.toml`、运行时清单、模型清单、下载源（原在 `finesub_bootstrap/`） | `src/nonoka_x/resources/` 下 Nonoka 的清单，内容由 `RUNTIME_PINS.json` 看住 |
| vendor 里的非核心文件 | 删除，见第 1 节第 7 条 |

`UPSTREAM.json` 保留，只留仓库、tag、commit、版本、`engine_bundle_id`。

质量锁由 `tests/test_finesub_quality_lock.py` 检查：`third_party/finesub/src/` 下除第 0 节解锁的四处外，每个文件的 sha256 等于 `QUALITY_LOCK.json` 里记录的值，文件不多不少（现为 550 个）。更新 `QUALITY_LOCK.json` 只能出现在用户批准的、专门改引擎的提交里。

在 WP-A 删掉 `FILES.json` 那套之前，旧的快照校验会把 `QUALITY_LOCK.json` 当成多出来的文件：`tests/test_finesub_patch_stack.py` 1 个失败、`tests/test_vendor_contract.py` 8 个错误。用户 2026-11-11 决定保留这个状态，由 WP-A 解决。

### 3.2 以后升级上游时跟什么

这是给将来用的规则，本次重构不执行升级。

| 类别 | 范围 | 升级时 |
|---|---|---|
| **关键算法** | 转写引擎：`speech/` 下除 `joint/` 外的全部，加 `media/`、`subtitles/`、`text.py`。纠错层与翻译层：`llm/stages/`，以及 `llm/` 里决定提示词、分窗、回答解析、后处理的文件，含 `prompt_templates/` | 跟上游。每次升级过回归闸门并由用户看过质量对比 |
| **其余** | 编排、CLI、配置、`llm/agent/`、`llm/routing/`、LLM 调用设施、`llm/knowledge/` | 默认不跟。要跟也可以，同样受质量红线约束 |
| **不再存在** | `finesub_bootstrap/` | 不跟。上游若改了引擎对它的用法（依赖面多了或少了名字），升级时由 `tests/test_runtime_pins.py` 的依赖面测试发现，在兼容包里补上 |

`scripts/finesub_align.py diff --ref <tag>` 取上游某个版本，只对关键算法的路径出报告：上游改了哪些文件、本地相对当前记录的上游版本有没有改过。它只出报告，不改文件。

`llm/` 顶层和 `llm/knowledge/` 的逐文件归属在 WP-A 里列出来交回确认；它只影响将来升级时看哪些文件，不影响本次重构（本次它们全部上锁）。

## 4. 设计决定

标 ⚠ 的开工前请确认，改了要回头调整对应工作包。

1. **引擎层 `src/nonoka_x/transcription/`，纠错层 `src/nonoka_x/correction/`，两层都是插件式的。** 调 FineSub 编排入口的只有两个文件：`transcription/engines/finesub.py` 和 `correction/finesub.py`，两者互不 import。worker 只认注册表里的接口，按任务请求取引擎和纠错实现；FineSub 转写引擎和其他引擎一样只交出 `transcript.json` 加自己的 `native` 产物，没有 FineSub 专属的编排分支。「FineSub 转写 + finesub correction」恰好能原样复用 stable（5.2），这是纠错层对 `native` 产物的优化，不是 worker 的特殊路径。
2. **`run_pipeline` 拆成两个显式入口**：`run_transcription`（到 stable 为止，含声学说话人分离）和 `run_post_stable`（stable 之后）。后者用参数明说说话人来自声学时间线还是来自转写行标签、裁判听哪条音轨，不再靠产物元数据里的后端名推断。`run_pipeline` 留作两者的薄组合，云端照旧调它。
3. **`speech/joint/` 整个迁出 FineSub**，用 `git mv` 搬进 `nonoka_x/transcription/` 再改造。`stages.py` 和 `pipeline.py` 里的 `asr` 选项与联合 ASR 分支一并删掉，FineSub 从此只有它自己的识别链路。
4. **云 API 引擎在启动器环境里就能跑**，只要 httpx 和 ffmpeg。没有 N 卡、没装 16 GB 运行时的用户也能转写加翻译。
5. **断行沿用迁过来的 `lines.py`，句子 DP 复制一份成 Nonoka 自己的代码**（2026-11-11 改：原先是 import FineSub 的，那样删掉 FineSub 后云 API 引擎就断不了行）。WP1 把 `speech/postprocessing/segmentation.py` 和它用到的纯 Python 部分（`recognition.segments` 的常量、`subtitles.metrics`、`text` 里的几个函数、相关配置默认值）复制到 `nonoka_x/transcription/segmentation/`，注明来源，之后由 Nonoka 维护。FineSub 里的原件不动。复制件与原件在同一批输入上输出相同，由测试证明（原件还在时录成基线）。云 API 引擎从此不 import `finesub` 的任何东西。
6. ⚠ **需要托管运行时的修补作为可选增强保留**（2.2 第三类里除本地 MOSS 外的那些）：运行时装了就启用，没装就跳过并在任务日志里说明。没装时 MOSS Pro 的段内时间按字数插值。
7. ⚠ **密钥由用户自带**，存进现有的受保护密钥文件。经 Nonoka Cloud 代理并扣额度留到后续阶段。
8. ⚠ **MOSS 指 MOSS Pro 云 API。** 本地 MOSS 的代码随 `speech/joint/` 一起迁出，但第一版不注册成引擎、不进界面，后续阶段再接。
9. **两层之间的中间格式是 Nonoka 自己的 `transcript.json`**（5.2）。转成 FineSub 的 stable JSON 是 finesub correction 内部的事。
10. **`provider` 含义不变**（任务由本机 sidecar 还是云端管理）。自研引擎都在本机 provider 下跑；云端 provider 只跑 FineSub。
11. **换引擎等于新任务**。引擎记在任务请求里，重试和续跑沿用原引擎。

运行时与配置自管：

12. **安装管理归 Nonoka。** 新建 `src/nonoka_x/runtime/`：自己的清单、目录布局、下载器、校验、安装、修复、卸载。`src/nonoka_x/` 里没有任何地方 import `finesub_bootstrap`，由测试强制；那个名字只留给第 16 条的兼容包，而且只有上锁的引擎代码 import 它。
13. **清单是 Nonoka 的，内容先与 FineSub 的逐项相同。** 运行时锁、工具清单、模型清单、下载源复制到 `src/nonoka_x/resources/` 成为权威来源。钉住的内容（包版本与哈希、模型版本与哈希、工具版本）记进 `RUNTIME_PINS.json`，由测试看住；改它需要用户批准（红线第 5 条）。
14. **配置和密钥归 Nonoka。** 新建 `src/nonoka_x/config/`：Nonoka 自己的配置文件和密钥存储，自己的格式，是唯一的事实来源。用户和界面只跟它打交道。
15. **FineSub 要的东西由桥接模块派生。** `src/nonoka_x/finesub_bridge.py` 是唯一知道 `FINESUB_*` 环境变量和 FineSub 文件格式的地方：每次启动 worker 时，从 Nonoka 的配置、密钥、目录生成 `finesub.toml`、模型目录文件、密钥文件和环境变量，放在 Nonoka 管的运行目录里。这些是派生产物，可以随时删掉重建。
16. ⚠ **`finesub_bootstrap` 整个删除，由 Nonoka 的兼容包取代**（用户 2026-11-11）。上锁的引擎文件还要 import 这个名字，而它们不许改，所以 Nonoka 在 `src/finesub_bootstrap/` 提供一个同名的真实包，只实现依赖面清单里的 48 个名字，背后转调 `nonoka_x.runtime` 和 `nonoka_x.config`（或者直接读桥接模块派生的文件）。要求：
    - 是放在 worker 模块搜索路径上的**真实包**，不靠运行时往 `sys.modules` 里塞别名，这样 `tests/test_runtime_pins.py` 的可解析性检查能直接查它；
    - 提供的名字与清单**不多不少**，由测试强制；import 链上没有 torch 和 numpy；
    - 决定结果的那几个（`model_ensure` 的 revision、`model_caches` 的 checkpoint 与仓库名、`token_counter` 的计数方式、`secrets` 读出的密钥、`artifacts` 的文件名常量）与原实现**语义相同**，由 `RUNTIME_PINS.json` 的常量、真实样本和 LLM 回放三道闸门证明；
    - 引擎「缺模型时自己下载」的行为保留，但指向 Nonoka 的清单和下载源；
    - 第一版允许把原模块的代码复制过来改，注明来源，之后由 Nonoka 维护。
17. **数据目录可以改名和搬动，FineSub 相关的东西移出主数据目录**（用户 2026-11-11）。新布局由 WP-R0 定稿，原则：
    - 主数据目录只放 Nonoka 自己的东西：设置与密钥存储、文档、任务、媒体库、知识库等；
    - FineSub 的派生文件（`finesub.toml`、模型目录文件、密钥文件、引擎状态）放在 Nonoka 管的引擎运行目录里，可以随时删掉重建；
    - 大文件（Python 运行时、工具、模型、缓存）放在用户选的大文件根下，目录名按 Nonoka 的组件划分，不再叫 `finesub`；
    - 上锁的引擎代码按**目录内部结构**找模型（HF hub 缓存的 `models--org--name/snapshots/<rev>`、分离器子目录等），这部分结构必须保持，或由桥接模块用环境变量指过去；顶层叫什么、放在哪都可以变。
18. **已有用户无损迁移，迁移工具随第一个采用新布局的版本发布**（用户 2026-11-11）。它把 2.6 列的旧布局搬到新布局：设置和密钥读进 Nonoka 的存储（`finesub.env`、`finesub.toml` 里 Nonoka 不认识的键原样透传），运行时、模型、缓存按新目录名搬过去，**不重新下载**。迁移后派生出的 `finesub.toml` 必须与迁移前的语义相同。细节见 WP-M1、WP-M2。
19. **引擎的环境需求向运行时管理器声明。** `EngineSpec.environment` 列出需要的组件：FineSub 引擎要 ASR 运行时加识别、分离模型；云 API 引擎只要 ffmpeg；可选增强要 ASR 运行时加 Silero、声纹、断句模型。就绪检查和「去安装」的引导都从这里来。

引擎密闭运行：

20. **FineSub 的代码只在 Nonoka 拉起的 worker 子进程里跑。** sidecar 主进程不再 import 任何会读环境或配置的 FineSub 模块；它现在要的那几样（模型路由与目录、说话人产物解析、显卡档位选项）要么改成问 worker，要么证明是不读环境的纯数据模块后列入白名单。
21. **worker 的环境变量从零构建，不继承。** 桥接模块按一张分类表生成，表里每个变量属于三类之一：
    - **系统必需**：从父进程原样带过去的最小集合，如 `SystemRoot`、`TEMP`、用户目录相关的几个。逐个列名，不用通配。
    - **Nonoka 设定**：值完全来自 Nonoka 的配置、任务请求和目录布局，无条件写入，不用 `setdefault`。所有 `FINESUB_*`、模型与缓存目录、代理、`CUDA_VISIBLE_DEVICES`、`PATH`（由托管工具目录加系统目录拼出）都在这类。
    - **其余一律不存在。**
22. **Python 解释器隔离启动。** 用隔离模式（`-I`）启动，不读 `PYTHON*` 变量和用户级 site-packages；模块搜索路径由 Nonoka 的启动入口显式设置，不靠 `PYTHONPATH`。
23. **文件发现也封住。** worker 的当前目录是 Nonoka 的任务目录；FineSub 按默认位置找数据和配置的入口（`FINESUB_HOME`、`FINESUB_BIG_DATA_DIR`、`FINESUB_CHECKOUT_DATA` 等）全部指向 Nonoka 管的目录；配置只有桥接模块派生的那一份；自动更新检查关掉。
24. **外部的东西要生效，只能先成为 Nonoka 的设置。** 例如系统代理：由 Nonoka 读取、按自己的代理策略决定，再显式传给 worker。用户以前靠系统环境变量调 FineSub 的做法从此失效，需要的能力在 Nonoka 设置里提供。
25. **密闭有边界，如实列出。** 以下不在「外部影响」之内，因为它们要么是物理事实，要么是 Nonoka 明确传进去的输入：显卡与驱动、MSVC 工具链（AOTI 编译用）、网络本身；Nonoka 管理的目录里的模型、缓存、状态文件、知识库；用户选用的本地 Agent CLI（Codex、Antigravity 等）自己的登录状态和配置，它们是独立程序，需要用户目录相关的变量才能工作。
26. **用三种办法证明密闭**（WP-H）：分类表覆盖扫描、投毒测试、读取审计。

彻底隔离（用户 2026-11-11；与前文冲突处以这几条为准）：

27. **FineSub 分装成两个引擎包，Nonoka 核心对它零引用。** `engines/finesub-transcribe/`（转写包）和 `engines/finesub-correct/`（纠错翻译包）。`src/nonoka_x/` 里不 import `finesub`、`finesub_bootstrap`，不出现 `FINESUB_`，不读写 FineSub 的任何文件格式，没有白名单。第 1 节第 5 条的四个文件（两个适配器、桥接模块、兼容包）在 WP-P1 搬进包里：每个包带自己的适配器、自己的桥接、自己那部分兼容包。`third_party/finesub/` 在分装后删除，两个包就是 FineSub 代码唯一的家。
28. **代码冻结后原样分装。** 冻结点是 WP-B（拆 `stages.py`）和 WP1（迁走 `speech/joint/`）做完之后。分装只搬文件，不改内容：包里每个 FineSub 文件的 sha256 等于 `QUALITY_LOCK.json` 里同一路径的值，由每个包自己的 `QUALITY_LOCK.json` 和测试看住。依据（2026-11-11 查实）：`speech/` 不 import `llm/`；`llm/` 只用到 `speech/diarization/` 的 `labels`、`judgments`、`product`、`assign` 和 `speech/postprocessing/recall_repair` 的一个函数；其余共用的是顶层 16 个文件、`subtitles/`、`media/`。精确的文件清单由 WP-P0 算出。
29. **共用的底座两个包各带一份。** 这样删掉任何一个包，另一个都完整。两份内容相同，各自上锁。`stages.py` 拆出的 `run_transcription` 归转写包、`run_post_stable` 归纠错包；`run_pipeline` 这个薄组合不再需要（云端只跑转写，见第 34 条）。
30. **两个包各跑各的进程。** 两个包的顶层包名都是 `finesub`，文件不许改，所以不能在同一个进程里加载。worker 改成两段：转写进程的模块搜索路径上只有转写包，纠错进程只有纠错包，中间靠落盘的产物交接。核心和包之间是进程协议（请求 JSON 进，事件流出，与现在的 worker 协议同形），核心不 import 包里的任何代码。包的就绪检查、模型路由与目录等需要问引擎的信息，也通过这个协议问，不在 sidecar 主进程里跑。
31. **包靠清单被发现，可以随时删。** 每个包根目录有 `PACKAGE.json`：标识、种类（转写 / 纠错）、入口、能力声明、需要的运行时组件与模型（即 `EngineSpec.environment`）。核心启动时扫描 `engines/`，有哪个包就注册哪个。删掉转写包：其余引擎加 FineSub 纠错照常。删掉纠错包：所有转写引擎照常出原始字幕，纠错翻译、知识库、LLM 模型设置显示「未安装」。两个都删：核心照常启动，云 API 引擎照常转写。为此原始字幕的渲染（`transcript.json` 到 SRT）由 Nonoka 核心自己做，不再走纠错包。删除指代码层面可分离、打包时可以不带；界面不提供卸载。
32. **FineSub 要的一切由 Nonoka 给，FineSub 的格式只有包自己懂。** 核心启动包的进程时给一份中性的启动上下文：运行时与工具的位置、模型目录、这个包的数据目录、任务目录、按名字给的密钥、代理、设备、设置项。把它变成 `finesub.toml`、模型目录文件、`FINESUB_*` 环境变量是包里桥接的事，在包的进程里、import `finesub` 之前完成。密钥经协议传入，不由核心写成 FineSub 的文件；包如果必须落盘才能让引擎读到（WP-R0 第 2 问），**硬性要求**：保护强度不低于 Nonoka 的密钥存储，放在包的私有目录，进程结束即删。
33. **产物由包转换成 Nonoka 的格式后交给核心保存；原生产物是包的私有附件。**
    - 转写包交出 `transcript.json`（Nonoka 的格式）和 Nonoka 格式的说话人信息。它的原生产物（`-stable.json`、`-aligned.json`、`-diar*.json`、`-vad*.json`、`-vocal.ogg` 等）放在任务目录下它自己的私有目录里，核心只当不透明的附件登记，不解析（用户 2026-11-11：`stable.json` 是转写包的私有附件）。
    - 纠错包拿到任务时，如果附件里有 FineSub 的 stable，**原样使用，一个字节都不改写**（红线：FineSub 转写接 FineSub 纠错的输入与现在相同）；没有或被删了，就从 `transcript.json` 构造。纠错包认得这种附件是因为那是它自己的原生输入格式，不构成对转写包的依赖。
    - 纠错包交出字幕和 Nonoka 需要的结构化结果，转换在包的适配器里做；`projector.py` 现在解析 FineSub 说话人产物的部分搬进适配器。
    - 知识库归纠错包：存储格式和读写代码都是它的，数据搬出主数据目录，放进纠错包的数据目录，由迁移工具搬。删掉纠错包后数据留着，装回来继续用。
    - 主数据目录和任务目录里，除包的私有目录外，没有 FineSub 格式的文件。
34. **云端只留转写包，交统一的 `transcript.json`**（用户 2026-11-11）。
    - Modal 容器（`nonoka-sub-cloud/modal_compute/`）只装转写包，跑到 stable 为止，在容器里由转写包的适配器转成 `transcript.json`。结果与 WP-C 的云端基线比对。
    - 云端任务的结果改成与本机引擎同形：必备的 `transcript.json`（Nonoka 的格式）加不透明的私有附件（`stable.json`、`diar.json`）。Worker 里**只改任务结果这一部分**（`src/jobs/` 里登记、存取、下发结果产物的代码，以及随之需要的 `migrations/`）：它把附件当不透明文件存和发，不解析。
    - **不动** LLM 转发：Worker 里的 LLM 转发路由、`tokyo_llm/`（东京的转发服务）。纠错包经 `nonoka-cloud` 路由调 LLM 的方式不变。
    - 桌面拿到云端结果时不需要任何 FineSub 包来转换：`transcript.json` 直接可用，附件交给纠错包原样使用。桌面上两个 FineSub 包都删掉时，云端 FineSub 转写照样能用，出到原始字幕为止。
    - 已经存在云端的旧任务结果只有 `stable_json`、`diar_json`：随第 36 条的一次性转换一起补出 `transcript.json`，原件转成附件。切换之后 Worker 和桌面都不再有旧字段的兼容路径。
36. **媒体库同步改成 Nonoka 的格式，一次做完**（用户 2026-11-11）。现在同步的是四个 FineSub 格式的产物（`stable_json`、`raw_srt`、`annotated_csv`、`final_srt`），名字在桌面上传、Worker 白名单、桌面取回三处写死，取回后的投影还要求必须有 `stable_json`；分包之后这条链会断。
    - **同步的内容**：`transcript.json`、字幕（`raw_srt`、`final_srt`，SRT 本身是中性格式），以及纠错结果的 Nonoka 格式（暂名 `correction.json`，由纠错包的适配器从 `annotated.csv` 转出，承载投影需要的全部信息）。不同步任何 FineSub 格式的文件，私有附件不上云。格式由 WP-L 定稿后写进 `docs/provider-spec.md`。
    - **投影只吃 Nonoka 的格式**：`project_edit_document` 的输入从 stable 加 `annotated.csv` 改成 `transcript.json` 加 `correction.json`。前文 5.1「Projector 不用改」作废。任何转写引擎的任务都能投影、同步、取回，包括不经纠错的。红线：同一个 FineSub 任务，新旧投影出的字幕文档内容相同，由测试证明。
    - **不留兼容期**：不双写，Worker 的白名单直接换成新名字，桌面不保留读旧格式的路径。
    - **云端已有数据一次性转换**：媒体库条目和任务结果里的旧产物，用两个包的适配器里同一份转换代码批量转成新格式；原件挪到备份前缀下保留一段时间，确认无误后再删。转换前后条目数相同，每个条目能投影出字幕文档，抽样与转换前的投影结果对比。
    - **桌面强制升级**：发版时在发布清单里设 `minVersion`（已有机制，`desktop/internal/selfupdate/service.go` 的 `releaseMandatory`，已发布的旧版本会执行）。Worker 侧同时拒绝旧格式的同步请求并返回明确的升级提示，挡住还没升级就来同步的旧版本。
    - **切换顺序**固定：先停写（Worker 进入维护态，拒绝媒体库写入）→ 批量转换并校验 → 部署新 Worker → 发布新桌面版并设 `minVersion` → 解除维护态。任何一步校验不过就回到旧 Worker、旧数据（原件还在）。
37. **「运行环境」页改成「引擎配置」页，全局只选一个转写引擎**（用户 2026-11-11）。重构完成后做（WP-UI），只重做这一块，媒体库、编辑器、导出不动。
    - **全局单选**：用户在引擎配置页选定当前的转写引擎，之后所有新任务都用它。新建任务的对话框不再让选引擎，只显示当前用的是哪个，并给一个去引擎配置页的入口。任务请求里仍然记着引擎（提交时从全局设置填入），所以重试和续跑沿用原引擎，第 11 条不变。
    - **可选项来自注册表**：本机的 FineSub 转写包（装了才出现）、MAI-Transcribe-2、Gemini 3.5 Transcribe、MOSS Pro，以及云端 FineSub。云端 FineSub 是列表里的一项（用户 2026-11-11 确认），不是引擎之外的另一个开关：它不需要安装，就绪条件是已登录且有额度；选中它时任务的 `provider` 是云端，选其余各项时是本机。
    - **选哪个装哪个**：每个引擎在清单里声明自己要的运行时组件和模型（第 19、31 条）。选中一个引擎时，页面列出它缺什么、要下载多少、装在哪，用户确认后只安装缺的那部分。云 API 引擎只要 ffmpeg 和密钥；它们的可选增强（空档重问、声纹、断句要的 ASR 运行时与模型）在该引擎的面板里单独列为可选安装。引擎没装好之前不能设为当前引擎，或设了之后新建任务被拦住并指回这一页。
    - **切换时保留已装的东西**：换引擎只装新引擎缺的，原来的不删，换回去不用重新下载。页面按引擎列出各自占的空间（共用的组件只算一次并标明被谁共用），提供「清理未使用的」：删掉当前引擎和纠错包都用不到的组件。有任务还在用某个组件时不允许清理它。
    - **这一页只管转写引擎**：纠错翻译的模型路由、LLM 密钥、知识库留在现有的设置页和知识库页，不动。转写引擎自己的密钥（OpenRouter STT、Azure Speech、MOSS Pro）放在引擎配置页该引擎的面板里。
    - 安装位置、存储位置搬迁、下载线路这些原「运行环境」页的功能保留，并入新页。
    - 桌面本地已有的任务和文档由 WP-M2 处理：已投影好的字幕文档原样保留；任务目录里的旧产物转出 Nonoka 格式，原件归进私有附件目录。
    - 2026-11-11 查实：Modal 上没有 LLM 容器（镜像只有 control、vocal、gpu_worker、scheduler），云端仓库里也没有任何地方调 `axis.translate_axis`；纠错翻译和日文轴翻译本来就都在桌面跑。前文说「云端 LLM 容器按这个名字调用」是错的，`axis.translate_axis` 的签名不必为云端保留。
35. **分包之前写的东西按「将来要搬进包」来写。** WP4a、WP4b 的适配器、WP-R5 的桥接、WP-R7 的兼容包先按原计划落在 `src/` 下，但除协议类型外不 import `nonoka_x` 的其他模块，WP-P1 搬的时候就不用重写。兼容包拿运行时位置和密钥的来源是第 32 条的启动上下文，不直接调 `nonoka_x.runtime`、`nonoka_x.config`（第 16 条相应调整）。

## 5. 目标结构

```text
src/nonoka_x/
├── transcription/
│   ├── __init__.py        # EngineSpec、注册表、TranscriptionEngine 协议
│   ├── model.py           # Unit / Turn / Transcript，以及 transcript.json 的读写
│   ├── lines.py           # 断行（迁自 speech/joint）
│   ├── stitch.py          # 拼窗、跨窗说话人对齐（迁入）
│   ├── cache.py           # 每次调用的原始回答缓存（迁入）
│   ├── keys.py            # 密钥池解析、按日配额台账（迁自 quota.py）
│   ├── audio.py           # ffmpeg：时长、截取上传用 MP3
│   ├── http.py            # httpx 客户端、重试、代理
│   ├── enhance/           # 需要托管运行时的可选修补：空档重问、声纹、断句、对齐（迁入）
│   └── engines/
│       ├── finesub.py     # 调 run_transcription
│       ├── mai2.py
│       ├── gemini.py
│       └── moss_pro.py
├── correction/
│   ├── __init__.py        # CorrectionSpec、CorrectionEngine 协议、注册表
│   └── finesub.py         # finesub correction：调 run_post_stable
├── runtime/               # 运行时自管
│   ├── __init__.py        # 组件、状态、RuntimeManager 门面
│   ├── paths.py           # 目录布局
│   ├── manifests.py       # 读 resources/ 下的清单
│   ├── fetch.py           # 断点下载、校验、镜像与代理路由
│   ├── python_env.py      # 按锁文件建虚拟环境、装 wheel
│   ├── tools.py           # ffmpeg、tokcount 等工具
│   ├── models.py          # 模型下载与校验
│   └── fsops.py           # 原子写、文件锁、进程树
├── config/                # 配置与密钥自管
│   ├── __init__.py        # ConfigStore、SecretStore 门面
│   ├── store.py           # Nonoka 的配置文件
│   ├── secrets.py         # 密钥存储
│   └── migrate.py         # 设置与密钥：从 finesub.env / finesub.toml 读进 Nonoka 的存储
├── migration/             # 旧版数据布局迁移工具（WP-M1、WP-M2），随第一个新布局版本发布
│   ├── __init__.py        # 探测旧布局、计划、执行、回滚
│   └── layouts.py         # 各历史版本的布局描述
├── finesub_bridge.py      # 唯一知道 FINESUB_* 和 FineSub 文件格式的地方；从零构建 worker 的密闭环境
├── worker_entry.py        # worker 的隔离启动入口：显式设置模块搜索路径后再进各 worker
├── resources/             # Nonoka 的清单：运行时锁、工具、模型、下载源、RUNTIME_PINS.json
└── worker.py              # 只做编排：engine.transcribe() -> correction.run()

src/finesub_bootstrap/     # 兼容包：只提供上锁引擎要的 48 个名字，背后是 nonoka_x 的实现（第 4 节第 16 条）

third_party/finesub/       # 只留核心文件（第 1 节第 7 条）
├── LICENSE、LICENSES.json、PROMPT_LICENSE.md
├── UPSTREAM.json          # 保留，精简
├── QUALITY_LOCK.json      # 取代 FILES.json
├── LOCAL_CHANGES.md       # 本地相对上游的全部改动
└── src/finesub/
    ├── stages.py          # 解锁：拆成 run_transcription / run_post_stable
    ├── pipeline.py        # 解锁：删 --asr 相关选项
    ├── speech/joint/      # 迁走后不存在
    └── …                  # 其余全部上锁（550 个文件）
```

FineSub 的两个部件各自的边界：

| 部件 | Nonoka 侧的文件 | 依赖 | 不依赖 |
|---|---|---|---|
| FineSub 转写引擎 | `transcription/engines/finesub.py` | `finesub.stages.run_transcription`、ASR 运行时与模型、桥接模块 | `nonoka_x.correction` 的任何东西 |
| finesub correction | `correction/finesub.py` | `finesub.stages.run_post_stable`、`finesub.llm`、启动器环境、桥接模块 | 任何转写引擎的实现、ASR 运行时（声学说话人分离属于转写引擎，第 4 节第 2 条） |
| 两者共用 | `finesub_bridge.py`、`src/finesub_bootstrap/`、`third_party/finesub/` | | |

纠错注册表里目前只有 `finesub` 一项。留注册表是为了让边界清楚、能被测试约束，不是为了现在就做第二个。

上面的目录树和边界表是 WP-P1 之前的过渡形态。分包之后（第 4 节第 27–35 条）：

```text
engines/
├── finesub-transcribe/        # FineSub 转写包；删掉不影响其余
│   ├── PACKAGE.json           # 标识、入口、能力、需要的运行时组件与模型
│   ├── QUALITY_LOCK.json      # 包内每个 FineSub 文件的哈希，等于分装前的值
│   ├── LICENSE、LICENSES.json、UPSTREAM.json、LOCAL_CHANGES.md
│   ├── adapter/               # 协议入口、桥接（启动上下文 -> FineSub 的配置与环境）、产物转换
│   └── src/
│       ├── finesub/           # speech/、共用底座、run_transcription
│       └── finesub_bootstrap/ # 兼容包里转写用到的名字
└── finesub-correct/           # FineSub 纠错翻译包；删掉不影响其余
    ├── PACKAGE.json、QUALITY_LOCK.json、LICENSE、LICENSES.json、PROMPT_LICENSE.md、UPSTREAM.json、LOCAL_CHANGES.md
    ├── adapter/               # 协议入口、桥接、产物转换、知识库与模型路由的查询
    └── src/
        ├── finesub/           # llm/、它用到的几个 speech 模块、共用底座、run_post_stable
        └── finesub_bootstrap/ # 兼容包里纠错用到的名字

src/nonoka_x/                  # 核心：对 FineSub 零引用
├── transcription/engines/     # 只剩 mai2、gemini、moss_pro；没有 finesub.py
├── transcription/segmentation/  # Nonoka 自己的句子 DP（第 4 节第 5 条）
├── correction/                # 只有协议与注册表；没有 finesub.py
├── engine_host.py             # 扫描 engines/、读 PACKAGE.json、按协议拉起包的进程
└── …                          # 没有 finesub_bridge.py；src/finesub_bootstrap/ 不存在

third_party/finesub/           # 不存在
```

### 5.1 协议

```python
@dataclass(frozen=True)
class EngineSpec:
    id: str                       # "finesub" | "mai2" | "gemini-transcribe" | "moss-pro"
    label: str
    kind: str                     # "local-gpu" | "cloud-api"
    features: EngineFeatures      # speakers, word_timestamps, separate, diarization_levels, languages
    keys: tuple[str, ...]         # 需要的密钥名
    environment: EngineEnvironment  # 必需与可选的运行时、模型、GPU

class TranscriptionEngine(Protocol):
    spec: EngineSpec
    def readiness(self, context: ReadinessContext) -> Readiness: ...
    def transcribe(self, job: TranscriptionJob, report: Reporter) -> TranscriptionResult: ...

class CorrectionEngine(Protocol):
    spec: CorrectionSpec          # id: "finesub"
    def readiness(self, context: ReadinessContext, request) -> Readiness: ...
    def run(self, job: CorrectionJob, report: Reporter) -> CorrectionResult: ...
```

- `TranscriptionJob`：源文件、语言、任务目录、缓存目录、目标阶段、引擎选项、取消检查。
- `TranscriptionResult`：`transcript_json` 路径、`native` 产物路径表、`metadata`。`native` 是引擎自己格式的产物，键带引擎前缀：FineSub 引擎交出 `finesub.stable_json`、`finesub.diar_json` 等；云端续跑下载到的 stable 也按这两个键进来。
- `CorrectionJob`：`TranscriptionResult`、源媒体路径、目标阶段（`raw-srt` / `translated-srt` / `final-srt`）、纠错选项、知识库选项、说话人级别、任务目录。
- `CorrectionResult`：产物清单。WP-P1 之前键名与现在的 ArtifactManifest 一致；WP-L 之后改为 Nonoka 的格式（`correction.json` 等），Projector 随之改（第 4 节第 36 条）。
- finesub correction 负责 stable 之后的全部：渲染原始字幕、写说话人行标签、LLM 纠错翻译、后处理。目标是 `raw-srt` 时也走它，只是不调 LLM。

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

`words` 和 `speaker` 可缺省。

finesub correction 拿到后：

- 有 `native["finesub.stable_json"]`：**原样使用**这份 stable（连同 `finesub.diar_json`），一个字节都不改写；说话人来源是声学时间线。FineSub 转写加 FineSub 纠错这条路因此和现在走的是同一份输入。
- 否则：把 `lines` 转成 stable JSON，`speaker` 写成 `line_speaker`，调 `run_post_stable` 时指明说话人来自行标签、裁判听源混音。

FineSub 转写引擎也要写 `transcript.json`（从它的 stable 投影出来，只读 stable，不改它），这样任何纠错实现都能读它的结果。

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

`runtime.stages` 里 `raw-srt` 的就绪改为按所选引擎判断：云 API 引擎只要 ffmpeg 和密钥。

## 6. 工作包

```text
引擎线：
WP-Q ──┬─► WP-A ──► WP-B ──┬─► WP1 ──► WP3a / WP3b / WP3c ─┐
       │                   ├─► WP4a ─┐                     │
       │                   └─► WP4b ─┴─► WP4c ─────────────┼─► WP8
       └─► WP0 ───────────────────────► WP5 ──► WP6 ──────┤
                    WP7（文档部分随时，云端部分等 WP-B）───┘
运行时线：
WP-Q ──► WP-R0 ──┬─► WP-R1 ───────────┐
                 └─► WP-R2 ──► WP-M1 ─┼─► WP-R3 ─┐
                               WP-M1 ─┴─► WP-R4 ─┴─► WP-R5 ──► WP-R6 ──► WP-R7 ──┬─► WP-M2 ─► WP8
                                                                                  └─► WP-H ──► WP8
分包线：
WP-B、WP1 ──► WP-P0 ─┐
WP4c、WP-R7 ─────────┴─► WP-P1 ──┬─► WP-H ──► WP8
                                 ├─► WP-L ──┬─► WP-M2 ─► WP8
                                 │          └─► 云端数据转换与切换（WP-L 的最后一步，发版时做）
                                 └─► WP-CL ─► WP8
云端：
WP-Q ──► WP-C（动任何代码之前）──► WP-R7、WP-CL、WP8 各比对一次
交叉：WP0 定门面；WP5 等 WP-R4；WP4c 等 WP-R5；WP-R7 等 WP-A、WP-B 和 WP-R6；WP-P1 等 WP-P0、WP4c、WP5 和 WP-R7；WP-H、WP-M2、WP-CL 都等 WP-P1（包的形态和数据目录定型）。图上 WP-R7 之后原先直接接 WP-H、WP-M2 的两条线，现在都经过 WP-P1。WP-UI（引擎配置页）等 WP-P1、WP-L、WP-H、WP6，排在 WP8 之前。
```

发版约束：改变磁盘布局的工作包（WP-R3、WP-R4、WP-R5、WP-R7）和 WP-M1、WP-M2 必须进同一个版本。不能发一个「布局已改、迁移工具还没有」的版本。引擎线也随这个版本一起发（用户 2026-11-11：一次性发布）。

开工前提：WP-Q 的产物（闸门脚本、harness、基线、`QUALITY_LOCK.json`、`RUNTIME_PINS.json`）先提交，否则在工作树里干活的子代理拿不到闸门。真实样本基线在 `test_data/` 下不进仓库，只在本机有；需要 `--real` 的工作包要在本机的主检出里跑。

WP-Q 已完成。WP1、WP4a、WP4b 还要等 WP0 的类型定义。两个子代理不要同时写同一个文件；每个包列出的文件归它独占。**只有 WP-B、WP1 和 WP-R7 可以写 `third_party/finesub/src/`，且只限第 0 节解锁的四处：WP-B 写 `stages.py`、`pipeline.py`，WP1 搬走 `speech/joint/`，WP-R7 删掉 `finesub_bootstrap/`。WP-P1 把整个目录原样分装进两个引擎包，只搬不改。**

两条线靠 WP0 定的门面解耦：引擎线的新代码取密钥、查运行时一律走 `nonoka_x.config` 和 `nonoka_x.runtime` 的门面，不直接碰 `settings.py`、`provision.py`。门面先由现有实现垫着，运行时线再把底下换掉。

### WP-Q 质量基线与回归闸门（已完成 2026-11-11）

产出后面所有工作包要过的闸门。2026-11-11 复核：不带和带 `--real` 两种都实跑通过。

**用法**

```text
D:\Envir\Python312\python scripts/quality_gate.py          # 不用 GPU 的各项，约 25 秒
D:\Envir\Python312\python scripts/quality_gate.py --real   # 再加真实样本与已装运行时实测，约 3 分钟
```

退出码 0 通过、1 有一项失败、2 没有失败但有「未判定」的产物（现在没有：第 0 节第 3 条已给每个不能逐字节复现的产物定了标准；将来出现新的才会走到这里）。每项失败都会指出哪个场景、哪个算法入口的哪个参数、或哪个文件不一样，以及基线文件的位置。`--record <calls|environment|replay|real>` 用当前代码重录某项基线，只能在用户批准后单独提交（第 0 节第 7 条）。

**各项与基线**

| 检查 | 比什么 | 基线 |
|---|---|---|
| 质量锁 | 550 个上锁文件的 sha256，不多不少 | `third_party/finesub/QUALITY_LOCK.json`；`tests/test_finesub_quality_lock.py` |
| 运行时钉住内容 | 两份 pylock 的 98 个包（版本、wheel/sdist 哈希）、7 个工具（版本、地址、哈希）、11 个模型（revision、文件哈希）、下载源、`model_caches`/`artifacts` 常量；上锁引擎对 `finesub_bootstrap` 的 48 个名字都可解析。`--real` 时再实测已装运行时 | `src/nonoka_x/resources/RUNTIME_PINS.json`、`tests/fixtures/quality/bootstrap-surface.json`；`tests/test_runtime_pins.py` |
| 调用等价 | 32 个请求场景（覆盖全部目标阶段、语言、人声分离开关、四档说话人与 `max_speakers`、纠错各选项、三种知识库模式与知识主体、云端续跑带不带时间线、日文轴，以及中文源加知识库、云端续跑加说话人等组合）。每个场景从 `local_provider.validate_request` 进，用 `LocalProvider._engine_environment()` 构建 worker 环境，在 `nonoka_x.worker.main` 里跑完，记录上锁算法入口收到的参数（按真实签名绑定）、纠错时的模型路由和产物清单 | `tests/fixtures/quality/calls/`；`tests/test_finesub_call_equivalence.py` |
| worker 环境与派生文件 | FineSub 读的环境变量（路径按角色记，目录搬动不影响），`FINESUB_CONFIG_FILE`、`FINESUB_MODEL_CATALOG`、`FINESUB_ENV_FILE` 指向的文件内容。启动器那部分（`PYTHON*`、`PATH` 等）只记录不比对 | `tests/fixtures/quality/environment.json` |
| LLM 回放 | 10 个用例、30 个请求：准备、单窗与多窗纠错、校对、说话人裁判、只纠错不翻译、知识更新。在 `llm_runtime.chat_complete` 截获，比提示词全文、路由与调用参数；用确定性回答跑完，比 `final.srt`、`annotated.csv`、`translated.srt`。机器上有没有 `opencc`、`tokcount` 也记入并比对 | `tests/fixtures/quality/replay/`（仓库内 5 个）、`test_data/quality-baseline/replay/`（真实样本 5 个，不进仓库） |
| 真实样本 | 两个样本跑到 `raw-srt`（多人样本开 `acoustics`），与基线按第 0 节第 3 条比对 | `test_data/quality-baseline/real/`（不进仓库） |

**录制条件**：基线都在干净环境下录（第 2.6 节）。不用 GPU 的检查在子进程里去掉一切可能有影响的变量，用户目录也换成空目录；真实样本只去掉变量，保留用户目录（torch 与 AOTI 编译要用）。

**后续工作包用它时要注意**

- 调用等价和回放的入口是 `validate_request`、`FineSubSettings`（`update_keys`、`set_cloud_account`）、`LocalProvider._engine_environment()`、`nonoka_x.worker.main`。哪个工作包改掉了其中一个，就由它改 harness（`tests/quality_harness/`）去调新的等价入口，**基线不动**；改完闸门仍须全部通过。
- 真实样本的运行方式跟着 `LocalProvider._default_worker_command` 和 `_engine_environment` 走；运行时管理器换掉以后（WP-R3），由 WP-R3 让 `tests/quality_harness/real.py` 找到新的运行时位置。
- `RUNTIME_PINS.json` 现在由 `finesub_bootstrap` 的原件推导；WP-R1 把推导来源换成 Nonoka 的清单，WP-R7 删掉原件之后测试仍须通过。
- 没覆盖到的地方：本地 Agent CLI 与 Gemini 原生路由、媒体上传、联网检索、已装工具的 PATH 派生；真实模型跑到 `final-srt` 的效果（WP8 人工对照）；基线只在 Windows 上录过（云端由 WP-C 补）。
- **回放基线是在没有 `tokcount`、没有 `opencc` 的系统 Python 下录的**，而用户实际运行时两者都有：token 计数走的是估算而不是 `tokcount`，繁简折叠没有生效。用户 2026-11-11 决定保持现状，不补录。所以「`tokcount` 有没有被找到」「`opencc` 有没有生效」闸门看不见，由 WP-H 和 WP8 专门确认。

**遗留事项**（复核时发现，不影响上面的结论；由 WP-A 顺手处理，改 harness 不动基线）：

- 真实样本的回放用例在 `test_data/` 缺失时被静默跳过，闸门仍报 PASS，只在摘要里注一句「N skipped」。改成「未判定」（退出码 2），免得在别的机器或工作树里误以为全过。
- `--real` 对已装运行时的实测偏松：包只比版本（装好的 wheel 不留哈希），模型「缺失」「部分缺失」不算问题。管线用到的模型缺失时由 `real.runtime_issues()` 另行拦住，够用；但风险表里「按哈希比对包」的说法只对清单成立，对已装环境不成立。

### WP-C 云端基线

动任何代码之前做。WP-R7 之后云端容器跑的也是 Nonoka 的兼容包（云端镜像的 `PYTHONPATH` 含本仓库的 `src`，见 `nonoka-sub-cloud/modal_compute/nonoka_x_compute/images.py`），而闸门的基线只在 Windows 上录过。

- 文件：`tests/quality_harness/cloud.py`（新增）、`scripts/quality_gate.py`（加 `--cloud`）；基线放 `test_data/quality-baseline/cloud/`，不进仓库。
- 内容：用**现有镜像、未改动的代码**在 Modal 上把 `sample_ja` 和 `sample_multi_person`（开 `acoustics`）跑到 `stable`，连跑两次查明哪些产物可以逐字节复现；把产物、镜像标识、显卡型号存成基线。比对标准沿用第 0 节第 3 条；云端如果出现那里没列的不可复现产物，交回用户定标准，不自己定。L4 机群 CPU 不一致会让 AOTI 重编，记录每次运行的加速状态。
- 完成标准：`quality_gate.py --cloud` 对未改动的代码通过；交付说明里写明花费、两次运行的差异、用的显卡。
- 之后：WP-R7 和 WP8 各跑一次 `--cloud`。

### WP-A 拆掉上游对齐机制

- 删除：`scripts/sync_finesub.py`、`patches/finesub/`、`third_party/finesub/FILES.json`、`tests/test_finesub_patch_stack.py`、`tests/test_sync_finesub.py`、CI 里的快照重建校验（换成质量锁测试）。
- `UPSTREAM.json`：保留 `repository`、`ref`、`commit`、`engine_version`、`engine_bundle_id`；去掉 `patches` 和已失去意义的汇总哈希字段，先确认 `scripts/build_finesub_bundle.py`、`desktop/build/tools/stage/main.go`、`desktop/scripts/build-offline-package.mjs` 不读它们，读的话一并改。
- 新增：`scripts/finesub_align.py`（3.2，只出报告）；`third_party/finesub/LOCAL_CHANGES.md`，把现有 8 个补丁相对上游改了什么、为什么、何时可以去掉记下来，文字从 `docs/engine.md` 3.1 的表里搬。之后对解锁文件的改动也记在这里。
- 列出 `llm/` 顶层和 `llm/knowledge/` 每个文件在 3.2 里的归属，拿不准的交回确认。
- 改写 `docs/engine.md` 和 `third_party/finesub/README.md`：去掉旧规则，写上第 0 节和第 3 节。
- 其余读 `FILES.json` 或钉快照的测试（`test_vendor_contract.py`、`test_split_stage_contract.py` 等）：钉「可重建」的断言删掉，测行为的断言留下。
- 完成标准：仓库里搜不到 `sync_finesub`、`BASELINE_FILES`、`patches/finesub`；`python -m pytest -q` 全过（WP-Q 留下的 `test_finesub_patch_stack.py` 1 个失败、`test_vendor_contract.py` 8 个错误就此消失；`tests/test_asset_digest.py` 不设 `PYTHONPATH=src` 收集失败是原有问题，顺手修掉）；桌面暂存工具和离线包脚本能跑通；`third_party/finesub/src` 此时与改动前逐字节相同；闸门通过。
- 不在本包：`finesub_bootstrap/` 和 vendor 里其余非核心文件留给 WP-R7 删，因为现在的安装器还在读它们。

### WP-B 拆分 `run_pipeline`

本计划里唯一改 FineSub 编排的包，单独一个子代理做，做完单独评审。

- 文件：`third_party/finesub/src/finesub/stages.py`、`pipeline.py`、`tests/test_split_stage_contract.py`、`third_party/finesub/LOCAL_CHANGES.md`。
- 内容：
  - 按第 4 节第 2 条拆出 `run_transcription` 和 `run_post_stable`，`run_pipeline` 留作薄组合。
  - `run_post_stable` 新增两个显式参数：说话人来源（声学时间线 / 转写行标签）和裁判听的音轨。原来由 `joint` 推出的行为改由它们决定。
  - 删掉 `asr`、`asr_aligner`、`asr_repair` 参数和联合 ASR 分支，`pipeline.py` 里相应的命令行选项一并删。做完后两个文件都不再 import `speech.joint`。
  - **只做搬移和拆分**：每个阶段调用哪个算法函数、传什么参数、按什么条件跳过，一律照原样；不顺手整理、不改默认值、不改日志以外的任何行为。
- 完成标准：
  - 闸门全部通过，含 `--real`：真实样本经 `run_pipeline` 的产物按第 0 节第 3 条与基线一致，LLM 请求序列与基线逐字节一致，调用等价测试通过。
  - 先 `run_transcription` 再 `run_post_stable`，与一次 `run_pipeline` 到同一目标，产物按第 0 节第 3 条一致。
  - `run_post_stable` 以「说话人来自行标签」跑一份只有 `line_speaker` 的 stable 到 `raw-srt`，全程不 import torch 和 numpy，并写出 `-diar-rows.json`。
  - 质量锁测试通过（上锁文件一个没动）。
  - 云端仓库的 `phases.py` 仍调得通 `run_pipeline`。

### WP0 契约与骨架

- 文件：`transcription/__init__.py`、`transcription/model.py`、`correction/__init__.py`、`runtime/__init__.py`、`config/__init__.py`、`tests/test_transcription_contract.py`、`docs/provider-spec.md`（只加 5.3 的字段定义）。
- 内容：5.1–5.3 的类型、两个注册表、`transcript.json` 的读写与校验。不含任何引擎实现。另外定两个门面，先薄薄地转调现有的 `settings.FineSubSettings` 和 `provision.RuntimeProvisioner`：
  - `nonoka_x.config`：按名字取密钥池、读写设置项、登记新的密钥规格。
  - `nonoka_x.runtime`：查某个组件是否就绪、取工具路径（ffmpeg 等）、取 worker 环境变量、判断 ASR 运行时是否可用。
- 完成标准：`transcript.json` 读写往返测试，缺 `words`、缺 `speaker` 都覆盖；测试断言 `nonoka_x.transcription` 对 `finesub` 的 import 只落在第 4 节第 5 条的白名单内（`engines/finesub.py` 除外），`nonoka_x.correction` 里只有 `finesub.py` import `finesub`，两个包互不 import 对方的实现文件。
- 另加 FineSub 边界测试（第 1 节第 5 条）：扫描 `src/nonoka_x/`，import `finesub` 或 `finesub_bootstrap`、出现 `FINESUB_` 字样、读写 `finesub.toml`/`finesub.env`/`model_catalog.toml` 的只能是那四个位置。现有的越界文件列成过渡白名单，写明由哪个工作包划掉；白名单只许变短。

### WP1 迁出 `speech/joint/`

- 文件：`third_party/finesub/src/finesub/speech/joint/**`（迁走后删除）、`transcription/lines.py`、`stitch.py`、`cache.py`、`keys.py`、`audio.py`、`http.py`、`enhance/**` 及对应测试。
- 内容：
  - **搬之前先留行为基线**：这部分代码不在闸门覆盖内。用录制的（或手写的）三个云后端回答，在未改动的代码上跑拼窗、跨窗说话人对齐、断行，把输出存成 `tests/fixtures/transcription/` 下的基线；搬完改完之后同样的输入必须得到同样的输出。
  - `git mv` 保留历史，再改 import。对 FineSub 的 `api_http`、`api_keys`、`reporting`、`settings`、`state` 以及 `speech/runtime` 的依赖换成 Nonoka 自己的；对 FineSub 算法模块的依赖一个不留：句子 DP 按第 4 节第 5 条复制成 `transcription/segmentation/`，先在原件上录好输出基线，复制件必须得到相同的输出。FineSub 里给联合 ASR 留的配置项和密钥池定义在上锁文件里，原样留着不管。
  - `audio.py`：截取上传用 MP3 的参数照 `finesub/media/ffmpeg.py:328`；ffmpeg 路径通过 `nonoka_x.runtime` 门面取。
  - `http.py`：代理沿用 worker 环境里已有的设置，先读 `provision.worker_environment` 和 `desktop/internal/netproxy` 确认传递方式。
  - `keys.py` 取密钥一律走 `nonoka_x.config` 门面。配额台账写到 Nonoka 自己的状态文件，不写 `finesub.state`。
  - `enhance/` 里的模块全部惰性 import，并提供「运行时是否可用」的统一判断。
  - 三个云后端的文件先原样搬到 `engines/` 下，能 import 即可，改造留给 WP3。本地 MOSS 搬到 `engines/moss_local/`，不注册。
- 完成标准：`third_party/finesub/src/finesub/speech/joint/` 不存在；搬迁前录的行为基线全部一致；拼窗、缓存命中与失配、配额换 key、断行各有不联网的单元测试；在只装了启动器依赖的环境里 `import nonoka_x.transcription.lines` 成功；闸门通过。

### WP3a MAI-Transcribe-2

- 文件：`engines/mai2.py`、`tests/test_engine_mai2.py`。
- 内容：包成 `TranscriptionEngine`；OpenRouter 与 Azure 两条路；空档重问接到 `enhance/`，运行时不可用时跳过。
- 完成标准：用录制的两种回答格式做解析测试；多窗口拼接测试；缺密钥时 `readiness` 给出明确 issue；开跑前按时长报预估费用；有无运行时两种情况都有测试。

### WP3b Gemini 3.5 Transcribe

- 文件：`engines/gemini.py`、`tests/test_engine_gemini.py`。
- 内容：包成 `TranscriptionEngine`；密钥复用已配置的 `GEMINI_FREE` 池，不自动动用付费池；断句模型、Live 修补、强制对齐接到 `enhance/`。
- 完成标准：时间修复、复读检测、续问、配额耗尽换 key 各有测试；有无运行时两种情况都有测试。

### WP3c MOSS Pro

- 文件：`engines/moss_pro.py`、`tests/test_engine_moss_pro.py`。
- 内容：包成 `TranscriptionEngine`；段内定时在运行时可用时走 Silero，否则插值；声纹重连接到 `enhance/`，不可用时只靠重叠时间对齐说话人，对不上的记为新说话人。
- 完成标准：段级拼接、上限续问、配额台账各有测试；有无运行时两种情况都有测试。

### WP4a FineSub 转写引擎

- 文件：`transcription/engines/finesub.py`、`tests/test_engine_finesub.py`。
- 内容：调 `run_transcription`。传的参数以 WP-Q 的调用基线为准，包括供 ASR 上下文用的 `extra_info`、`knowledge`。跑完只读 stable 投影出 `transcript.json`，并在 `native` 里交出 `finesub.stable_json`、`finesub.diar_json` 等。
- 完成标准：不 import `nonoka_x.correction`；闸门通过，尤其是 `--real` 下真实样本的 `stable.json` 按第 0 节第 3 条一致；`EngineSpec` 如实声明它要的运行时和模型，就绪检查只从这里来。

### WP4b finesub correction

- 文件：`correction/finesub.py`、`axis.py`、`tests/test_finesub_correction.py`。
- 内容：按 5.2 取得 stable，放到 FineSub 的路径，调 `run_post_stable`，参数以 WP-Q 的调用基线为准。`worker.seed_cloud_stable` 和 `axis.translate_axis` 里调 FineSub 的部分收进来。`axis.translate_axis` 内部改为转调本模块（云端没有地方调它，见第 4 节第 34 条；签名保留只为少改桌面侧的调用点）。
- 完成标准：
  - 闸门通过：原生 stable 输入下，发给 LLM 的请求序列与基线逐字节一致。
  - 一份只有 `lines` 的 `transcript.json` 经它跑到 `raw-srt`，全程不 import torch 和 numpy，并写出带说话人的 `-diar-rows.json`。
  - 不 import `nonoka_x.transcription.engines`；日文轴路径的现有测试全过。

### WP4c worker 编排

- 文件：`worker.py`、`tests/test_worker_adapter.py`。
- 内容：按 `transcription.engine` 取引擎，转写，再按 `correction.engine` 交给纠错层。目标阶段不超过 `stable` 时只跑引擎。云端续跑成为「转写结果已在手」的一种输入：直接构造 `TranscriptionResult`，`native` 里放下载到的 stable。worker 里不再直接 import `finesub.stages`。
- 完成标准：
  - 闸门全部通过；默认引擎下同一输入的产物清单与现在一致；任务日志里不出现重复的「已有结果，跳过」；云端续跑路径的现有测试全过。
  - **可替换性**：worker 里没有按引擎名分支的代码；用一个只交 `transcript.json` 的测试用假引擎替换 FineSub，跑到 `final-srt` 不需要改 worker；不注册 FineSub 转写引擎（模拟没装）时，其他引擎加 finesub correction 照样能跑。

### WP5 请求校验、就绪检查、密钥

等 WP-R4 做完再开工：新增的密钥规格直接登记到 Nonoka 自己的密钥存储，不往 `finesub.env` 那套里加。

- 文件：`local_provider.py`、`tests/test_local_provider.py`；密钥规格通过 `nonoka_x.config` 门面登记。
- 内容：
  - `validate_request` 接受并规范化 `transcription`；引擎不支持的选项（如云 API 引擎的 `separate`、`device`、`gpu_tier`）规范化掉并在任务日志里说明。引擎是 `finesub` 时，规范化结果必须和现在完全一样。
  - 就绪检查按引擎分：`local_provider.py:243` 现在按本地 GPU 写死，拆成由各引擎的 `readiness` 提供；`_start` 里的就绪表（`:1113`）让云 API 引擎走续跑那一档再加上自己的密钥检查。
  - LLM 那几档的就绪检查（`harness`、`final-srt`、`knowledge`、`video-multimodal`）改由 finesub correction 的 `readiness` 提供，判断逻辑不变。
  - `capabilities()` 输出 `transcription_engines` 和 `correction_engines`。
  - 登记 OpenRouter STT、Azure Speech（密钥加端点）、MOSS Pro 池三组密钥规格。这些密钥只给 Nonoka 自己的引擎用，桥接模块不把它们派生给 FineSub。
  - 各引擎的就绪检查按第 4 节第 19 条从 `EngineSpec.environment` 和运行时管理器得出。
- 完成标准：没装运行时、没有 GPU 的环境里，配了 MAI2 密钥就能通过 `final-srt` 的启动检查；没配时报 `missing_key`；闸门通过。

### WP6 桌面界面与 Go

- 文件：`desktop/frontend/src/providers/types.ts`、`home/defaultRequest.ts`、`components/TranscriptionDialog.tsx`（及其 CSS）、`app/App.tsx`、`app/types.ts`、`providers/localProvider.ts`、`test/provider.test.mjs`；Go 侧 `desktop/internal/taskhistory`、`internal/telemetry` 如需记录引擎。
- **2026-11-11 调整**：引擎的选择最终放在引擎配置页、全局单选（第 4 节第 37 条，WP-UI）。本包因此只做管线：类型、请求里带上 `transcription` 字段、任务列表和历史显示所用引擎、Go 侧的记录。下面关于对话框里做四个引擎卡片的内容不做；对话框暂时保留现有的本机 / 云端开关，等 WP-UI 换掉。
- 原内容（仅供 WP-UI 参考）：「转写引擎」卡片由 `transcription_engines` 加云端 FineSub 动态生成，`ExecutionMode` 换成 `{provider, engine}`；云 API 引擎不显示计算设备、显卡档位、人声分离，说话人选项按 `features` 给；显示计费说明和「音频会离开本机」提示，没配密钥时给「前往配置」；任务列表和任务历史显示所用引擎。选 FineSub 时发出的请求除多一个 `transcription` 字段外与现在相同，默认值不变。
- 完成标准：`npm run typecheck`、`npm test` 通过；用预览实际点一遍四个引擎的切换和禁用态。

### WP-R0 运行时与配置的接口调查

只读调查加设计定稿，不改代码。结论写进 `docs/runtime-and-config.md`，后面几个 R 包照它做。

- 要回答的问题：
  1. 上锁的引擎代码运行时到底从 `finesub_bootstrap` 读什么、写什么。逐个列出 2.4 第三个角色里的调用点，标明读的是哪个目录或文件、由哪个环境变量决定。
  2. 引擎读密钥只认 `FINESUB_ENV_FILE` 指的文件，还是也认进程环境变量。密钥文件的保护格式是什么，Nonoka 派生时怎么写才能被它读出来。
  3. 引擎或 `finesub_bootstrap` 会不会在运行时**自己改写**配置文件、密钥文件或做迁移（看 `finesub_bootstrap/migrations/`、`user_config.py`、`secrets.py`）。会的话派生文件可能被改掉，要说明怎么防。
  4. `FINESUB_MODEL_MANIFEST`、`FINESUB_DOWNLOAD_SOURCES`、`FINESUB_MODEL_DIR` 是否覆盖了引擎所有「确认并下载模型」的路径；模型和缓存目录必须保持什么布局。
  5. 现有安装怎么认：Nonoka 自己的安装器（和 WP-M1 的迁移工具）怎么认出一个由 `finesub_bootstrap` 装好的运行时是完整且版本正确的，搬到新布局后怎么不重新下载就判定就绪。
  6. `settings.py` 现在写进 `finesub.toml` 的每一个键、它们的来源，以及用户文件里可能存在而 Nonoka 不管的键。
  7. **环境变量全量清单**：`third_party/finesub/src/` 里读的每一个环境变量（名字、位置、作用、不设时的默认行为）；它依赖的库里会影响结果或行为的变量（Hugging Face、torch、CUDA、CTranslate2、OpenMP/MKL、numba、Triton、uv、代理、证书）；以及 Windows 上 Python、torch、DPAPI、MSVC 工具链探测（AOTI 编译）、本地 Agent CLI 各自必须有的系统变量。给出第 4 节第 21 条那张分类表的初稿。
  8. **默认位置的文件发现**：FineSub 和 `finesub_bootstrap` 在没有环境变量时会去哪些默认位置找配置、数据、密钥（用户目录、当前目录、检出目录、注册表），每一处怎么封。
  9. **主进程里的 FineSub**：2.5 列的那几处 import，各自读不读环境或配置；哪些可以证明是纯数据模块，哪些必须挪进 worker。
  10. **隔离模式的代价**：worker 改用 `-I` 启动后，现在靠 `PYTHONPATH` 生效的东西（比如 `provision.worker_environment` 给 yt-dlp 加的路径）怎么办。
  11. **兼容包的设计**：依赖面清单（`tests/fixtures/quality/bootstrap-surface.json`）的 48 个名字逐个说明引擎拿它做什么、读哪个目录或文件、在 Nonoka 里由谁实现；决定结果的那几个（第 4 节第 16 条）要说明怎么保证语义不变。
  12. **新的数据布局**：按第 4 节第 17 条定下主数据目录、引擎运行目录、大文件根各自放什么、叫什么；上锁引擎在模型目录内部依赖的结构逐条列出。
  13. **旧布局清单**：把 2.6 列的旧布局补全到每个历史版本（含开发检出 `.local`、`storage.json` 改过大文件根的、离线包装的、macOS 的），每个文件或目录标明新位置、搬法（改名、移动、跨盘复制）、是否可以重建。
  15. **引擎包的数据目录**（第 4 节第 31–33 条）：两个包各自的数据目录放哪、叫什么、各放什么（派生配置、引擎状态、知识库、LLM 路由）；任务目录里私有附件目录的位置和命名；启动上下文要带哪些字段才够包内桥接派生出与现在相同的 `finesub.toml` 和环境变量。
  14. **vendor 的核心文件**：`third_party/finesub` 下除第 1 节第 7 条以外的每个文件，谁在读（构建脚本、桌面暂存工具、离线包脚本、安装器、云端镜像），删掉前要改谁。
- 产出：Nonoka 配置文件与密钥存储的格式；运行时组件划分；新数据布局；桥接模块要派生的文件和环境变量清单；兼容包的接口说明；环境变量分类表初稿；迁移规则与旧布局清单；发现的阻碍。
- 完成标准：上面十五个问题各有带文件位置的答案；任何一条答案如果意味着必须改上锁文件，单独标出并交回。

### WP-R1 Nonoka 自己的清单

- 文件：`src/nonoka_x/resources/` 下的运行时锁、工具清单、模型清单、下载源，`runtime/manifests.py`，`tests/test_runtime_pins.py`（WP-Q 建的，补全）。
- 内容：把 `finesub_bootstrap` 里的 `pylock.win-py312.toml`、`pylock.win-py312.cn.toml`、`runtime-manifest.json`、`model-manifest.json`、`download-sources.json` 复制成 Nonoka 的清单；现有补丁 0002 对工具清单的改动（aria2c、node、pot-provider、固定 FFmpeg、去掉托管 Git）已经在里面，一并带过来。清单格式可以按 Nonoka 的需要整理，但钉住的内容不变。
- 完成标准：`tests/test_runtime_pins.py` 的 `derived_pins()` 改为从 Nonoka 的清单推导后仍然通过，证明钉住的每一项（含 `engine_constants`）与 WP-Q 记录的基线相同；`finesub_bootstrap` 里的原件此时还在、没动，由 WP-R7 删除。

### WP-R2 基础设施

- 文件：`runtime/fsops.py`、`runtime/fetch.py`、`runtime/paths.py` 及测试；`download_route.py`、`asset_digest.py`、`config_file.py` 改为用它们。
- 内容：原子写、文件锁、目录链接判断、进程树终止、断点下载与大小/摘要校验、镜像与代理路由、目录布局。以 `finesub_bootstrap` 的 `fsops`、`locks`、`processes`、`downloader`、`http_client`、`download_routes`、`paths` 为起点复制过来，注明来源，之后由 Nonoka 维护。目录布局按 WP-R0 定稿的新布局实现（第 4 节第 17 条）。
- 完成标准：这三个已有文件不再 import `finesub_bootstrap`；`tests/test_download_route.py`、`tests/test_asset_digest.py`、`tests/test_hf_mirror_transfer.py` 全过；下载的断点续传、摘要不符、镜像回退各有测试。

### WP-R3 运行时管理器

- 文件：`runtime/__init__.py`（把门面换成真实现）、`runtime/python_env.py`、`runtime/tools.py`、`runtime/models.py`、`provision.py`、`runtime_install.py`、`model_install.py`、`model_specs.py`、`msvc_runtime.py` 及对应测试。
- 内容：按 Nonoka 的清单建虚拟环境、装工具、下载校验模型；状态、修复、卸载、缓存清理；离线包优先的下载阶梯保留。按新布局安装；旧布局由 WP-M1 迁过来，本包只认新布局。开工前先用 WP-M1 的命令把开发机的安装搬到新布局，`--real` 才能继续跑。`provision.py` 对外的状态结构尽量保持不变，桌面「运行环境」页少改。让 `tests/quality_harness/real.py` 和 `pins.py` 找到新布局下的运行时（只改 harness 的定位，基线不动）。
- 完成标准：
  - 这几个文件不再 import `finesub_bootstrap`。
  - 在一个由 WP-M1 迁过来的安装上启动，状态显示就绪，没有任何下载发生，`quality_gate.py --real` 的实测比对通过。
  - 在空目录上全新安装（可以用小的假清单测流程，真实的完整安装至少跑一次），装出来的包清单和模型哈希与基线一致。
  - `tests/test_provision.py`、`tests/test_runtime_install.py`、`tests/test_model_install.py`、`tests/test_offline_model_install.py` 全过。

### WP-R4 配置与密钥存储

- 文件：`config/__init__.py`（把门面换成真实现）、`config/store.py`、`config/secrets.py`、`config/migrate.py`、`settings.py` 及测试。
- 内容：Nonoka 自己的配置文件和密钥存储（格式按 WP-R0 的定稿）；密钥落盘保护不弱于现在；读旧的 `finesub.env`、`finesub.toml` 的迁移逻辑放在 `config/migrate.py`，由本包接进 WP-M1 的迁移命令（M1 留好了这一步的位置），旧文件留作备份；存储放在新布局的主数据目录里；`settings.py` 的快照接口对界面保持兼容，底下改读新存储。本包**不**负责生成 FineSub 的文件，那是 WP-R5。
- 完成标准：迁移往返测试——拿一组真实形态的旧文件（含 Nonoka 不认识的键、多 key 池、自定义端点、本地 Agent 路由）迁移后，所有设置和密钥都读得回来；密钥不出现在日志和快照里；`tests/test_settings.py` 全过。

### WP-R5 FineSub 桥接

- 文件：`finesub_bridge.py`、`tests/test_finesub_bridge.py`；`provision.worker_environment` 和 `settings.bind_environment` 里与 FineSub 有关的部分收进来。
- 内容：按第 4 节第 15 条，每次启动 worker 时从 Nonoka 的配置、密钥、目录派生 `finesub.toml`、模型目录文件、密钥文件和全部 `FINESUB_*` 环境变量。派生文件放在新布局的引擎运行目录里，不在主数据目录。按 WP-R0 第 3 问的结论防止它们被引擎改写后失去同步。
- 完成标准：
  - 闸门的调用等价与环境检查通过：同样的设置下，派生出的 `finesub.toml`、模型目录文件、密钥文件的内容和 FineSub 读的环境变量与 WP-Q 基线一致（环境基线按角色记路径，目录搬了不影响）。
  - 迁移前后对照：同一份旧配置，迁移前引擎读到的与迁移后派生出的语义相同。
  - `src/nonoka_x/` 里没有任何地方 import `finesub_bootstrap`，由测试断言。
  - `quality_gate.py --real` 通过：真实样本跑到 `raw-srt`，产物按第 0 节第 3 条与基线一致。

### WP-R6 桌面、脚本与文档

- 文件：`desktop/scripts/resource-sources.mjs`、`build-offline-package.mjs`、`publish-github-mirror.mjs`、`desktop/internal/app/python_bootstrap.go` 及其测试、「运行环境」页与设置页相关前端文件、`docs/runtime-and-config.md`、`docs/architecture.md` 4.3 节。
- 内容：离线包和 GitHub 镜像脚本改读 Nonoka 的清单；界面文案里「FineSub 运行时」之类的说法按新归属改；Go 侧如果有读 FineSub 清单或环境变量的地方一并改。
- 完成标准：离线包能构建；其中的运行时、工具、模型按文件哈希与改动前的包一致，目录名和 vendor 文件按新布局与第 1 节第 7 条变化，变化逐项列在交付说明里；`go test ./...`、前端测试通过。

### WP-R7 删除 `finesub_bootstrap`，vendor 只留核心文件

等 WP-A、WP-B、WP-R5、WP-R6 做完再开工。WP-R6 必须在前：`desktop/scripts/resource-sources.mjs` 现在直接读 `finesub_bootstrap` 目录里的清单，要先改读 Nonoka 的清单才能删。

- 文件：`third_party/finesub/src/finesub_bootstrap/**`（删除）、`src/finesub_bootstrap/**`（新建兼容包）、`third_party/finesub/` 下第 1 节第 7 条以外的文件、读它们的构建与暂存脚本、`tests/test_runtime_pins.py`、`third_party/finesub/LOCAL_CHANGES.md`。
- 内容：
  - 按第 4 节第 16 条和 WP-R0 第 11 问建兼容包：只提供依赖面清单里的 48 个名字，背后转调 `nonoka_x.runtime`、`nonoka_x.config` 或读桥接模块派生的文件。
  - 删掉 `third_party/finesub/src/finesub_bootstrap/`。确认 worker 的模块搜索路径里找到的是兼容包。
  - 按 WP-R0 第 14 问删掉 vendor 里的非核心文件，读它们的地方一并改。
  - 兼容包不许 import torch、numpy；Nonoka 其余代码不许 import 兼容包。
- 完成标准：
  - `third_party/finesub/` 下只剩第 1 节第 7 条列的文件；质量锁测试通过（550 个上锁文件一个没动）。
  - `tests/test_runtime_pins.py` 通过：依赖面与清单相同，48 个名字都由兼容包解析，兼容包提供的名字不多不少；`engine_constants` 里被引擎用到的那几个由兼容包给出相同的值。
  - `quality_gate.py --real` 全部通过。
  - 云端镜像（`nonoka-sub-cloud`）照样能构建、能调 `run_pipeline`；需要改时在云端仓库里改。兼容包在云端的 Linux 容器里也要能用：决定模型位置的那几个名字只靠环境变量工作，不假设 Windows 的目录布局。
  - `quality_gate.py --cloud` 通过（WP-C 的云端基线）。

### WP-M1 迁移核心

等 WP-R0（新旧布局清单）和 WP-R2（`runtime/paths.py`）做完就开工，排在 WP-R3、WP-R4 之前：它们只认新布局，没有它开发机上的运行时和设置就读不到，`--real` 也跑不了（用户 2026-11-11 决定拆出来提前做）。

- 文件：`src/nonoka_x/migration/**`、`tests/test_migration.py`。
- 内容：
  - 一个命令行入口，不接进 sidecar 启动，不碰界面。
  - 探测旧布局（WP-R0 第 13 问的清单，含 `storage.json` 指向别的盘的大文件根和开发检出 `.local`）。没有旧布局就什么都不做。
  - 先出迁移计划（搬什么、搬到哪、要多少空间、同盘改名还是跨盘复制），再执行。同盘一律改名，不复制；跨盘复制完校验大小和哈希再删源。
  - 运行时、模型、缓存按新目录名搬，**不重新下载**；任务、文档、知识库的数据内容一字不改。
  - 设置和密钥这一步留好位置，实现是 WP-R4 的 `config/migrate.py`，由 WP-R4 接进来；在那之前这一步跳过并说明。
  - 提供只读的 `--dry-run`，输出计划不动文件。
- 完成标准：
  - 在一份旧布局副本上（含大文件根在另一个盘的情形）跑完，文件按 WP-R0 的清单各就各位，没有任何下载；`--dry-run` 不动任何文件；各有测试。
  - 用它把开发机的安装搬到新布局之后，WP-R3 完成时 `quality_gate.py --real` 通过。

### WP-M2 迁移的启动集成、续跑与回滚

等 WP-R3、WP-R4、WP-R5、WP-R7、WP-P1 把新布局定型再开工。必须和改布局的那几个包进同一个版本（第 6 节开头的发版约束）。知识库从主数据目录搬进纠错包的数据目录、旧任务目录里的 FineSub 产物归进私有附件目录，也在这里做（第 4 节第 33 条）。

- 文件：`src/nonoka_x/migration/**`（在 WP-M1 的基础上）、`tests/test_migration.py`，sidecar 启动段里调用它的地方，桌面上显示迁移进度和结果的地方。
- 内容：
  - 启动时、sidecar 打开任何数据之前调 WP-M1 的探测与执行。
  - 设置和密钥调 `config/migrate.py` 读进 Nonoka 的存储，旧文件改名留作备份。
  - 可中断可续：每一步写进迁移日志，中途断电或被杀，下次启动从断点接着做；失败就回滚到迁移前的样子，并在界面上说明。
  - 只迁一次：迁完写标记；新版本再也不读旧路径。
- 完成标准：
  - 拿一份真实形态的旧数据目录副本（含大文件根在另一个盘、有 Nonoka 不认识的配置键、多 key 池、本地 Agent 路由、做过的任务与知识库）迁移，迁完：设置和密钥都读得回来，任务和文档在界面里照常打开，运行时就绪且没有任何下载，`quality_gate.py --real` 的实测比对通过。
  - 迁移在每一步被杀掉后都能续上或回滚，各有测试。
  - 迁移前后对照：同一份旧配置，迁移前引擎读到的与迁移后桥接模块派生出的语义相同。
  - 迁移后主数据目录里没有 FineSub 相关的文件（第 1 节第 8 条）。

### WP-P0 分包清单

等 WP-B、WP1 做完再开工（冻结点）。只读调查，不改代码，结论写进 `docs/engine-packages.md`。

- 要回答的问题：
  1. 两个包各自的**精确文件清单**：从 `run_transcription`、`run_post_stable` 和各自适配器要用的入口出发，静态 import 图加一次打桩运行时的实际 import 记录，两者取并集。提示词模板、包数据等非 `.py` 文件按谁读它来归。
  2. 共用底座有哪些文件；有没有只被一边用、可以不带的。
  3. 纠错包的清单在只装 httpx、pydantic、opencc 的环境里能不能完整 import，链上有没有 torch、numpy。
  4. `finesub_bootstrap` 依赖面的 48 个名字各归哪个包，哪些两边都要。
  5. 核心现在直接用 FineSub 的每一处（`settings.py` 的模型路由与目录、`projector.py` 的说话人产物解析、`gpu_tier.py` 的档位选项、`axis.py`、`worker.py`、`llm_worker.py`、`knowledge_worker.py`、`local_provider.py`）：各归哪个包的适配器，协议上要加什么查询。
  6. 现有产物清单里每一项：是 Nonoka 格式、是中性格式（SRT），还是 FineSub 格式；FineSub 格式的由哪个包转换成什么，哪些只作私有附件。
  7. 任务续跑和重试现在靠盘上的 FineSub 产物跳过已完成的阶段；产物进了私有目录之后这个行为怎么保持。
  8. 知识库目录搬进纠错包数据目录后，界面上知识库管理的每个操作走协议的哪个查询。
  9. 进程拆成两段后启动开销多少；默认路径（FineSub 转写接 FineSub 纠错）的任务日志和进度事件怎么保持与现在一致。
- 完成标准：九个问题各有带文件位置的答案；两份文件清单可以被脚本直接读；任何一条意味着必须改冻结文件的，单独标出并交回。

### WP-P1 分装成两个引擎包

等 WP-P0、WP4c、WP5、WP-R7 做完再开工。做完之后才做 WP-H、WP-M2、WP-CL。

- 文件：`engines/finesub-transcribe/**`、`engines/finesub-correct/**`（新建）；`third_party/finesub/**`（搬空后删除）；`src/finesub_bootstrap/**`、`src/nonoka_x/finesub_bridge.py`、`transcription/engines/finesub.py`、`correction/finesub.py`（搬进包）；`src/nonoka_x/engine_host.py`（新建）、`worker.py`、`local_provider.py`、`settings.py`、`projector.py`、`gpu_tier.py`、`axis.py`、`llm_worker.py`、`knowledge_worker.py` 里用 FineSub 的地方；桌面暂存工具与离线包脚本；`tests/quality_harness/`（只改定位与入口，基线不动）。
- 内容：
  - 按 WP-P0 的清单用 `git mv` 分装，共用底座复制成两份。每个包生成自己的 `QUALITY_LOCK.json`。
  - 按第 4 节第 30–33 条实现：`PACKAGE.json`、进程协议、`engine_host.py`、worker 拆成两段进程、启动上下文、包内桥接、产物转换、私有附件目录、原始字幕渲染改由核心做。
  - 核心里所有用 FineSub 的地方改成走协议；WP0 的过渡白名单清零。
- 完成标准：
  - **只搬不改**：两个包里每个 FineSub 文件的 sha256 等于分装前 `QUALITY_LOCK.json` 里同一路径的值；分装前上锁的 550 个文件每个至少出现在一个包里，没出现的逐个列出并说明为什么两边都不需要。
  - `src/nonoka_x/` 里搜不到 `finesub`、`FINESUB_`（测试断言，无白名单）；`third_party/finesub/`、`src/finesub_bootstrap/` 不存在。
  - 闸门全部通过，含 `--real`：默认路径下纠错包拿到的 stable 与基线按第 0 节第 3 条一致，LLM 请求序列逐字节一致。
  - **可删性**，各有自动测试：删掉转写包目录、删掉纠错包目录、两个都删，三种情况下核心的测试全过；前两种里留下的那个包照常跑完自己的闸门；第三种里一个云 API 引擎（打桩）能跑到 `raw-srt`。
  - 转写包的进程里 import 不到纠错包的文件，反之亦然（测试断言）。
  - 主数据目录和任务目录里，包的私有目录之外没有 FineSub 格式的文件。

### WP-CL 云端重做：只留转写包

等 WP-P1 做完再开工。改 `nonoka-sub-cloud/modal_compute/` 和 Worker 里任务结果那一部分；LLM 转发路由和 `tokyo_llm/` 一个文件都不动（第 4 节第 34 条）。开工前先列出 Worker 里要改的文件交回确认。

- 文件：云端仓库的 `modal_compute/**`、`src/jobs/` 里与结果产物有关的文件及其测试、需要时的 `migrations/`；本仓库 `desktop/internal/cloud/`（下载与提交的字段白名单）、核心里云端续跑的入口。
- 内容：
  - Modal 镜像只装 `engines/finesub-transcribe/`，经包的入口跑到 stable 并转出 `transcript.json`；不再装纠错相关的代码。
  - 结果契约改为 `transcript.json` 加不透明附件；Worker 存、发，不解析。旧结果按原字段继续可取。
  - 桌面侧云端续跑改成「转写结果已在手」：`transcript.json` 直接进核心，附件登记成私有附件；旧结果走第 4 节第 34 条的兼容路径。
  - `test_source_contract.py` 钉的哈希一并更新。
- 完成标准：
  - `quality_gate.py --cloud` 与 WP-C 的基线一致（比的是附件里的 stable 等原生产物）；云端测试和 Worker 测试全过。
  - 云端仓库的 `git diff` 不涉及 LLM 转发路由和 `tokyo_llm/`。
  - 一个新的云端任务从提交到桌面出 `final-srt` 走通，纠错拿到的 stable 是附件原件，LLM 调用照旧经 Nonoka Cloud 转发。
  - 删掉桌面上两个 FineSub 包，同一个云端任务能出到原始字幕。
  - 一个改动前存下的旧结果仍能在桌面上接着跑。

### WP-L 媒体库同步改用 Nonoka 的格式

等 WP-P1 做完再开工；WP-M2 等它（本地旧任务的转换用同一份转换代码）。规则见第 4 节第 36 条。和 WP-CL 都要改云端仓库，两者不同时写同一个文件：WP-CL 管 `src/jobs/`，本包管 `src/library/`。

- 文件：
  - 本仓库：`src/nonoka_x/projector.py`、`local_provider.py` 的投影入口与产物清单、`desktop/internal/cloud/service.go`（同步与取回）、`relay.go`、`desktop/internal/plugins/engine.go` 的产物顺序、`desktop/frontend/src/providers/types.ts`、两个引擎包适配器里的转换代码、`docs/provider-spec.md`、`docs/architecture.md` 第 5 节。
  - 云端仓库：`src/library/**` 及其测试、需要时的 `migrations/`、一次性转换脚本（放 `modal_compute/nonoka_x_compute/operations/`，在装了两个包的环境里跑）。
- 内容：
  1. 定 `correction.json` 的格式：列出现在投影从 `annotated.csv`、`final.srt`、`correction_mode_json` 里读的每一项，一项不漏。
  2. 纠错包适配器产出 `correction.json`；投影改成只吃 `transcript.json` 加 `correction.json`。
  3. 桌面同步与取回、Worker 的媒体库白名单换成新名字，不留旧名字。Worker 对旧格式的同步请求返回明确的升级提示。
  4. 写一次性转换脚本：读云端每个媒体库条目和任务结果的旧产物，转成新格式，原件挪到备份前缀；可重跑，转过的跳过；出一份逐条目的结果报告。
  5. 写切换手册（第 4 节第 36 条的顺序），含每一步的校验和回退办法。
- 完成标准：
  - **投影等价**：对 WP-Q 回放基线里每个用例的产物，旧投影（stable 加 `annotated.csv`）和新投影（`transcript.json` 加 `correction.json`）得到的字幕文档内容相同；说话人、只纠错不翻译、日文轴各有用例。
  - 云 API 引擎只出原始字幕的任务能同步、能在另一个数据目录里取回并打开。
  - 转换脚本在一份云端数据的副本上跑完：条目数不变，每个条目都能投影，抽样与转换前的投影结果相同；重跑不产生变化。
  - `src/library/` 和桌面的同步代码里搜不到 `stable_json`、`annotated_csv`。
  - `go test ./...`、前端测试、Worker 测试全过；闸门通过。
- 不在本包自动执行：对线上数据的转换和切换。那是发版时由用户按手册执行或明确批准后执行的操作。

### WP-H 引擎密闭运行

等 WP-P1、WP5 都做完再开工：它要封的是分包之后的最终形态（两个包各自的进程），覆盖扫描要扫两个包里的 FineSub 代码和兼容包。下文说的 `finesub_bridge.py`、`src/finesub_bootstrap/` 指搬进包之后的对应文件；中性的那部分环境（系统必需变量、`PATH`、解释器隔离启动）由核心的 `engine_host.py` 构建，`FINESUB_*` 由包内桥接从启动上下文生成。

- 文件：`finesub_bridge.py`（环境构建）、`worker_entry.py`、`local_provider.py` 里拉起三种 worker 的地方和 `_engine_environment`、`worker.py` / `llm_worker.py` / `knowledge_worker.py` 的启动段、`settings.py` / `projector.py` / `gpu_tier.py` 里主进程 import FineSub 的地方、`tests/test_engine_hermetic.py`。
- 内容：
  - 按第 4 节第 20–24 条实现：环境从零构建、无条件写入、解释器隔离启动、文件发现封住、主进程不再碰会读环境的 FineSub 模块。
  - 分类表放在桥接模块里，是唯一的事实来源；每一项写明归类理由。
  - 2.5 列的每个漏点逐条关掉，包括那两个 `setdefault` 和 `CUDA_VISIBLE_DEVICES` 的条件设置。
- 证明密闭的三种测试：
  1. **覆盖扫描**：扫 `third_party/finesub/src/` 和兼容包 `src/finesub_bootstrap/` 里所有读环境变量的地方，每个名字都必须在分类表里。以后上游新增一个变量，这个测试会先失败。
  2. **投毒测试**：把父进程环境里所有已知变量设成垃圾值，在当前目录和假的用户目录里放伪造的配置文件和密钥文件，然后构建 worker 环境、派生配置、跑调用等价测试。结果必须与干净父进程下的完全相同。
  3. **读取审计**：测试里给 worker 装一个记录环境变量读取的钩子，跑一遍打桩的完整管线，引擎读过的每个名字都必须在分类表里，且读到的值都来自 Nonoka。
- 完成标准：
  - 三种测试通过；闸门全部通过。
  - `quality_gate.py --real` 在密闭环境下通过：真实样本跑到 `raw-srt`，产物按第 0 节第 3 条与基线一致。闸门里不比对的「启动器部分」环境（`PYTHON*`、`PATH` 等）正是本包要重建的，不算回归。
  - 确认 AOTI 加速仍然生效（看任务日志和运行元数据里的加速状态，与基线相同），没有因为环境收紧而悄悄退回未加速。
  - **确认 token 计数来源和繁简折叠没有变**：在托管运行时下各跑一次改动前后的纠错，任务产物里记录的 `token_counter_source` 相同（仍是 `tokcount`，没有因为 `PATH` 重建找不到它而退回估算），`opencc` 可 import。回放基线不含这两条路径（WP-Q 的说明），闸门抓不到，只能在这里查。
  - 密钥读取、模型加载、ffmpeg 调用、本地 Agent CLI 路由（如果本机装了）各验证一次；没条件验证的如实标记。
  - 报告里列出最终的分类表，以及第 4 节第 25 条那些边界各自的现状。

### WP-UI 引擎配置页

重构的其余部分做完之后开工：等 WP-P1、WP-L、WP-H、WP6，排在 WP8 之前，随同一个版本发布。规则见第 4 节第 37 条。

- 文件：`desktop/frontend/src/pages/RuntimePage.tsx`（及 CSS，改成引擎配置页）、`components/TranscriptionDialog.tsx`（去掉引擎选择）、`components/InstallLocationCard.tsx`、`StorageLocationsCard.tsx`、`StorageRelocation.tsx`、`runtimeCache.ts`、`bridge/runtime.ts`、`app/App.tsx`（导航与文案）、`pages/SettingsPage.tsx`（只移走转写引擎的密钥，其余不动）、`home/defaultRequest.ts`、`providers/types.ts`；Go 侧 `desktop/internal/preferences`（当前引擎的设置项）；Python 侧 `nonoka_x.runtime` 门面要补的查询：按引擎列出所需组件及状态、按组件算占用、找出未被使用的组件、清理。
- 内容：
  - 引擎列表由 `Capabilities.transcription_engines` 生成，单选；每项显示就绪状态、缺什么、下载量、占用空间、计费说明和「音频会离开本机」提示。
  - 选中未就绪的引擎时走安装流程：只装它缺的组件，进度和失败重试沿用现有的安装界面。
  - 云 API 引擎的面板里放它自己的密钥配置和可选增强的安装开关。
  - 「清理未使用的」：列出将删除的组件和能腾出的空间，确认后执行；被任务占用的组件不可清理。
  - 当前引擎存成一项设置；新建任务时由它填入请求的 `transcription.engine`。对话框显示当前引擎和去配置页的入口，引擎不支持的选项（计算设备、显卡档位、人声分离）不显示，说话人选项按引擎的 `features` 给。
  - 当前引擎被删掉（包被移除）或变得不可用时，页面提示并要求重选，不静默回落到别的引擎。
- 完成标准：
  - `npm run typecheck`、`npm test`、`go test ./...` 通过。
  - 用预览实际走一遍：在各引擎之间切换；选一个未安装的引擎并装完；装到一半取消再继续；清理未使用的组件；删掉转写包后页面的表现；当前引擎不可用时新建任务被拦住。
  - 从 FineSub 换到云 API 引擎再换回来，没有任何重新下载。
  - 设置页里纠错翻译、LLM、知识库相关的部分与改动前逐项相同。
  - 选 FineSub 时发出的请求与 WP6 完成时相同（闸门的调用等价不受界面影响）。

### WP7 云端仓库与文档

- 云端仓库：确认 `modal_compute/nonoka_x_compute/phases.py` 在 WP-B 之后仍调得通（它不传 `asr`，预期不用改）；云端测试全过。云端没有 LLM 容器，也不调 `axis.translate_axis`；Worker 和 `tokyo_llm/` 的 LLM 转发不在本计划范围内，不动。需要改时提交在 nonoka-sub-cloud 仓库；`test_source_contract.py` 钉了哈希的文件动了要一起更新。
- 本仓库文档：`docs/architecture.md`（架构图加引擎层与纠错层，FineSub 画成两个可插拔部件）、`docs/provider-spec.md`、新增 `docs/transcription-engines.md`、`CHANGELOG.md`（写明数据目录的新布局和迁移工具）。

### WP8 集成验证

- 全量测试：`D:\Envir\Python312\python -m pytest -q`（仓库根；WP-A 修好之前要带 `PYTHONPATH=src`，否则 `tests/test_asset_digest.py` 收集失败会让整轮中断）、`cd desktop/frontend && npm test && npm run typecheck`、`cd desktop && go test ./...`、`cd nonoka-sub-cloud/modal_compute && python -m pytest -q tests`。
- 最终闸门：`scripts/quality_gate.py --real` 和 `--cloud` 全部通过；用托管运行时跑的纠错里 `token_counter_source` 仍是 `tokcount`；两个引擎包里的 FineSub 文件相对 `1f14db3` 只有 `stages.py`、`pipeline.py` 变过，`speech/joint/` 和 `finesub_bootstrap/` 原件不在，其余每个文件的哈希等于 WP-Q 的 `QUALITY_LOCK.json`；`third_party/finesub/` 不存在；`src/nonoka_x/` 里搜不到 `finesub`。
- 可删性：实际删掉 `engines/finesub-transcribe/`、删掉 `engines/finesub-correct/`、两个都删，各启动一次应用并跑一个任务，结果符合第 4 节第 31 条。
- 可替换性：不装 FineSub 运行时、不注册 FineSub 转写引擎时，用一个云引擎加 finesub correction 跑到 `final-srt`；反过来 FineSub 转写引擎的结果交给 finesub correction 以外的实现（测试用假实现）也能走完。
- 迁移：在一份真实的旧版用户数据目录副本上装新版，迁移后照常使用，没有重新下载，主数据目录里没有 FineSub 相关文件。
- FineSub 路径端到端：一个真实样本用真实模型跑到 `final-srt`，把字幕交给用户和基线对照着看一遍。闸门证明的是「发给模型的东西没变」，这一步是给人看最终效果。
- 真实调用：三个云引擎各跑一个 `test_data/audio` 样本到 `final-srt`，分别在装了和没装运行时的环境下。需要用户提供密钥，没有密钥的引擎如实标记为未验证。

## 7. 约定

- 提交信息不加 Co-Authored-By，不 push。
- Python 测试用系统解释器 `D:\Envir\Python312\python`。WP-A 修好之前，全量 `pytest` 要带 `PYTHONPATH=src`。`ruff` 现有 13 个旧错误不算回归。
- 每个工作包交付前跑 `scripts/quality_gate.py`；动了识别、运行时、环境或目录布局的包再跑 `--real`。
- 上锁文件不许写。保留 `third_party/finesub/LICENSE`、`LICENSES.json`、`PROMPT_LICENSE.md`。
- 每个工作包的交付说明里写明闸门结果；没跑或没过要如实说。

## 8. 风险与取舍

| 风险 | 影响 | 应对 |
|---|---|---|
| 拆 `run_pipeline` 时某个阶段的参数、跳过条件或默认值被带偏 | FineSub 的转写或纠错结果变化 | WP-B 只做搬移；调用等价测试的记录点在编排之下；真实样本按第 0 节第 3 条比对；单独评审 |
| 转写产物本身不可逐字节复现 | 闸门没法用逐字节比对 | WP-Q 已查实（2.6）；用户批准了替代标准（第 0 节第 3 条） |
| 闸门只覆盖选中的样本和请求组合 | 没覆盖到的组合里可能有差异 | WP-Q 的请求矩阵覆盖所有目标阶段和开关；WP8 再加一次人工对照 |
| 迁出的 `speech/joint/` 从此不跟上游 | 上游对这几个后端的修补要自己看着搬 | 这是本次的明确取舍 |
| 没装运行时的时候云引擎缺少空档重问和声纹重连 | 偶发漏段不会被补；续问后同一人可能被记成两个说话人；MOSS Pro 行内时间是插值的 | 只影响自研引擎，不影响 FineSub 路径；任务日志里说明，装了运行时自动启用 |
| 没有了「可由基线加补丁完整重建」的保证 | 无法机械地还原出纯上游代码 | `LOCAL_CHANGES.md` 记下所有本地改动；质量锁看住算法文件 |
| Nonoka 的安装器装出来的运行时与原来有细微差别（某个包的版本、构建或安装顺序） | 识别结果变化，而且很难察觉 | `RUNTIME_PINS` 按哈希看住清单里的包和模型（已装环境只能比包版本和模型文件哈希）；真实样本按第 0 节第 3 条比对；旧安装由 WP-M1 搬过来，不重装 |
| 重建 worker 的 `PATH` 时丢了 `tokcount`，或运行环境里没了 `opencc` | token 计数退回估算，分窗和提示词变化；繁简折叠失效。回放基线不含这两条路径，闸门抓不到 | WP-H 和 WP8 专门核对 `token_counter_source` 与 `opencc` |
| 派生的 `finesub.toml` 漏了某个键，引擎悄悄用了默认值 | 质量相关的设置失效 | 调用等价测试比对派生文件；迁移时不认识的键原样透传 |
| 引擎或 `finesub_bootstrap` 运行时自己改写配置或密钥文件 | 派生文件与 Nonoka 的存储不同步 | WP-R0 第 3 问先查清；桥接模块每次启动重新派生 |
| 迁移旧配置时丢密钥或丢设置 | 用户升级后要重新配置，或任务失败 | 旧文件留作备份；迁移往返测试；迁移失败时回退到旧文件并提示 |
| 兼容包与原 `finesub_bootstrap` 语义有出入（模型 revision、checkpoint、token 计数、密钥读取、产物文件名） | 识别或纠错结果悄悄变化 | 依赖面清单与可解析性测试；`RUNTIME_PINS` 的 `engine_constants`；真实样本和 LLM 回放（回放里 token 计数方式不同会让分窗和提示词变化，逐字节能抓到） |
| 上游升级改了引擎对 `finesub_bootstrap` 的用法 | 兼容包缺名字，引擎起不来 | 依赖面测试先失败，升级时在兼容包里补 |
| 迁移中断（断电、杀进程、磁盘满） | 数据目录半新半旧，启动不了或丢数据 | 迁移日志可续、失败回滚；同盘只改名；跨盘先复制校验再删源；动手前先算空间 |
| 用户迁移后又装回旧版本 | 旧版本找不到搬走的数据 | 迁移前的配置文件留作备份；更新说明写明不支持降级，或由 WP-M2 提供「还原到迁移前」 |
| 布局改了但迁移工具没跟上同一版本 | 老用户升级后数据「消失」 | 第 6 节开头的发版约束：改布局的包和 WP-M1、WP-M2 同版发布 |
| 环境收得太紧，漏掉某个系统必需的变量 | MSVC 探测失败导致 AOTI 悄悄退回未加速；torch 找不到 DLL；DPAPI 读不出密钥；本地 Agent CLI 起不来 | WP-R0 第 7 问先列清；WP-H 逐项验证并专门检查加速状态 |
| 环境收得不够，分类表漏了某个库读的变量 | 仍有外部影响 | 读取审计测试能抓到 FineSub 自己读的；第三方库在原生代码里读的抓不到，这部分靠「其余一律不存在」的白名单方式兜住 |
| 用户以前靠系统环境变量调 FineSub（代理、HF 镜像、指定显卡、线程数） | 升级后这些设置不再生效 | 这是密闭的本意；常用的几项在 Nonoka 设置里提供，更新说明里写明 |
| 开发机本身不干净，基线带着外部影响 | 闸门以一个被污染的行为为准 | WP-Q 录基线前先清点并去掉外部变量 |
| 运行时线和引擎线同时改 `local_provider.py` 一带 | 冲突 | 靠 WP0 的门面解耦；WP5 排在 WP-R4 之后；WP-H 排在最后 |
| 云端镜像仍按自己的方式装运行时、喂配置 | 本地与云端的环境管理方式不同 | 云端的装法和调度本轮不动；它本来就是 Linux 上另一套装法 |
| `correction.json` 漏了投影原先从 `annotated.csv` 读的某一项 | 取回或重新投影后的字幕文档丢信息（置信度、备注、说话人等） | WP-L 先逐项列出再定格式；新旧投影对同一批产物结果相同 |
| 云端数据批量转换出错或中途失败 | 用户的云端媒体库条目打不开 | 先停写；原件挪到备份前缀不删；脚本可重跑；先在数据副本上跑通；校验不过就回退到旧 Worker 和旧数据 |
| 强制升级期间有用户没升级，或升级失败 | 这些用户的同步被拒绝 | `minVersion` 让旧版本自动安装新版；Worker 对旧格式请求返回明确提示而不是静默失败；本地数据不受影响 |
| 切换之后才发现新格式有缺陷 | 新旧格式都已在线上，回退困难 | 备份前缀下的原件保留到确认无误；切换前在副本上完整演练一遍 |
| 分装时漏了文件，或某个文件只在运行到特定分支时才被 import | 包在特定任务上才报缺模块 | WP-P0 用静态图加运行时记录取并集；WP-P1 要求 550 个文件逐个有去向；闸门的 32 个场景和回放在分装后的包上全跑 |
| 两段进程之间交接的产物与原先同进程时不同 | FineSub 默认路径的纠错输入变化 | stable 作为私有附件原样交接，不经转换；`--real` 和 LLM 回放比对 |
| 共用底座有两份，日后只改了一份 | 两个包行为不一致 | 代码冻结；各自的质量锁；升级上游时由分装脚本从同一来源生成两份 |
| 私有附件被用户或清理工具删掉 | 纠错改从 `transcript.json` 重建 stable，输入与原生的不完全相同 | 任务日志里写明用的是哪种输入；重建路径本来就是其他引擎走的路 |
| WP-R7 之后云端容器跑的是 Nonoka 的兼容包，而不是原来的 `finesub_bootstrap` | 云端识别结果悄悄变化，Windows 上的闸门看不见 | WP-C 在动代码前录云端基线；WP-R7 和 WP8 各跑一次 `--cloud`；兼容包里决定模型位置的名字只靠环境变量工作 |
