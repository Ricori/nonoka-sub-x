# FineSub 分包清单（WP-P0；WP-P1 已按它分包）

## WP-P1 之后

WP-P1 按这份清单把 `third_party/finesub/` 分进了 `engines/finesub-transcribe/`、`engines/finesub-correct/`、`engines/finesub-knowledge/`，vendor 已删除。下面第 0–15 节是 WP-P0 的原始调查，按当时的代码写，留作记录；现在的事实以这一节和 `docs/engine-packages/*.json` 为准。

- **清单怎么来的**：`scripts/engine_packages.py` 的入口不再手写，而是从三个包自己的代码（`engines/<包>/adapter/`）读出来：每个 `import finesub…`、经它读到的属性、知识库服务端的 `ALLOWED`。写在 `try:` 里、失败了也接着跑的 import 记为「可选」，只有别处也用到才进包（JSON 的 `optional`）。分包后脚本读三个包 `src/` 的并集（同一路径的文件两个包里逐字节相同，`finesub_bootstrap` 只取转写包的原件），重跑得到的清单与分包时一致，`--check` 守着；`--import-check` 改成在每个包**自己的** `src/` 与 `adapter/` 上跑，兼容包绑定好启动上下文后全部 import 成功（纠错 192 个模块、知识库 68 个，链上没有 torch、numpy）。
- **文件数**：转写包 158 个（115 个上锁 + `stages.py` + `finesub_bootstrap` 原件 42 个），纠错包 464 个（463 个上锁 + `stages.py`），知识库包 344 个（全部上锁）。和 WP-P0 比：转写包少 13 个（`speech/diarization/product.py`、`judgments.py` 等只剩纠错包读，`model_ensure` 的下载子进程 WP-R7 已删）；知识库包多 4 个——静态可达的 +5，减去 `llm/knowledge/entries.py`（它是客户端代码、import 协议的客户端，知识库包里没有客户端也 import 不了；WP-P0 只因为测试进程里客户端与服务端同处一个进程才把它记进运行时记录）。
- **不带的文件**：40 个上锁文件加 `pipeline.py`，共 41 个，逐个写明原因记在 `docs/engine-packages/not-carried.json`（带 sha256）；分包前的锁存为 `docs/engine-packages/vendor-quality-lock.json`，三个包的锁每一条都等于它的对应条目。
- **兼容包**：纠错包 10 个模块（加包文件与 `_launch`），恰好第 5 节那 32 个名字；知识库包 6 个模块，恰好 14 个名字——第 5 节说的 `fsops`（「`locks` 内部要」）实际是原 `paths` 要，兼容包的 `paths` 改成从启动上下文取位置后不再需要，于是没有这个模块。每个模块的 `__all__` 恰为名单；复制来的代码还要用的公开名字（异常类 `SecretUnreadable` 等，改名会改变失败消息里的类型名）保留原名，逐个列在 `tests/test_engine_packages.py` 的 `COMPAT_HELPERS`（用户 2026-11-17）。`paths.default_data_root`、`load_app_paths` 取启动上下文的 `paths.package_data`（`engines\<包>\`，D21），没给就报错，不回落到 `%LOCALAPPDATA%\FineSub`。
- **依赖面**：上锁文件用到的 `finesub_bootstrap` 名字从 48 个变成 45 个：`try_lock`、`holding_activity_barrier`、`remove_project_records_under` 只有不带的 `batch_state.py`、`agent_cleanup.py` 用（`tests/fixtures/quality/bootstrap-surface.json` 已按用户 2026-11-17 的答复删掉这三个）。
- **访问点**：`knowledge-access.json` 现在是 0 个——WP-K 之后两半都经 `finesub_knowledge.client` 访问知识库，静态图里不再有直达存储的调用；76 个访问点的原始清单见第 11 节。

> 基于 `main@98a38f7`（FineSub 代码冻结点：WP-B、WP1 之后）。只读调查：没有改任何代码、测试、基线或上锁文件。
> 权威计划：`docs/transcription-engines-plan.md`（下称「计划」）。本文回答计划第 6 节 WP-P0 的十个问题，以及 WP-K、WP-P1 开工要用的清单。
> **WP4c 正在改 `worker.py`**：第 5、7、9 问里关于 `worker.py` 的结论按 98a38f7 的代码写，可能随 WP4c 变化的地方标了「⟳WP4c」。

## 0. 结论一览

| # | 一句话结论 |
|---|---|
| 1 | 三份清单已算出、脚本可读（`docs/engine-packages/{transcribe,correct,knowledge}.json`）：转写包 171 个文件（FineSub 128 个上锁文件 + `stages.py` + `finesub_bootstrap` 原件 42 个），纠错包 466 个（465 个上锁文件 + `stages.py`；`finesub_bootstrap` 换成 11 个模块的兼容包），知识库包 340 个（全部上锁文件，另需一个 8 个模块的兼容包）。两次真实运行记录到的 FineSub 模块全部落在转写清单里，静态图没有漏掉动态 import。 |
| 2 | 转写包和纠错包共用 83 个上锁文件（顶层 8 个、`subtitles/` 8 个、`media/` 3 个、`speech/` 32 个、`llm/` 的路由与 `correction_*` 12 个、`llm/knowledge` 20 个，后者在 WP-K 之后从转写包里掉出去）；共用底座里没有只被一边用到的文件。转写包另有 45 个文件纠错包用不到，纠错包另有 80 个文件转写包用不到；知识库包的文件是纠错包清单的子集。 |
| 3 | 能：纠错清单（182 个 `finesub` 模块 + 11 个兼容模块）和知识库清单（58 + 8）在只放行标准库、httpx、pydantic、opencc 及其依赖的隔离解释器里逐个 import 全部成功，链上没有 torch、numpy、scipy（`import-check.json`）。 |
| 4 | 48 个名字：纠错包用 32 个，知识库包用 14 个，转写包用 26 个（原件全带）；`agy_records.remove_project_records_under`、`locks.holding_activity_barrier`、`locks.try_lock` 三个只有不进任何包的 CLI 文件在用。`pipeline.py` 不能归纠错包（见第 4 问）。 |
| 5 | 核心直接用 FineSub 的 28 处逐一归了包（第 5 问的表）。协议上要加的查询：包的 `describe`、`readiness`；纠错包的模型选项、本地 Agent 目标、LLM 单次调用（现 `llm_worker`）；知识库包的条目列表、读、存、任务主体准备；GPU 档位改成 `PACKAGE.json` 里的静态声明。 |
| 6 | 现有 15 项产物里，`raw_srt`、`translated_srt`、`final_srt` 是中性格式；其余 12 项是 FineSub 格式：stable 由转写包转出 `transcript.json`（加说话人），`annotated.csv`、`speaker_judge.json`、`correction-mode.json` 由纠错包转出 `correction.json`（WP-L），其余只作私有附件。 |
| 7 | 只要私有目录里保持 FineSub 原来的相对文件名（`<目录>\<title>-stable.json` 等），FineSub 自己的「盘上有就跳过」就原样生效；核心另外落一份 `transcription-result.json`，重试时有它就不再拉起转写进程。 |
| 8 | 界面的知识库操作只有三个（列表、读、存），加上任务提交时的「准备主体」，各对应知识库包的一个查询（第 8 问的表）。 |
| 9 | 实测：PBS 解释器冷启动约 0.15 s；纠错进程 import 链约 1.0 s（首次 1.9 s）；知识库进程约 0.55 s；转写进程的重 import（torch 等）3.2–4.2 s，这部分现在的单进程也要付；沙箱准备是建目录加一个锚点文件，毫秒级。真实样本每次 68–90 s，拆进程多出来的开销约 1–3 %。日志与进度的一致性靠核心把两段事件接成一条流（第 9 问）。 |
| 10 | 两半里直接碰知识库的调用点共 76 个，分布在 19 个文件里（18 个上锁文件加 `stages.py`），另有核心的 `worker.py`、`knowledge_worker.py`。推荐的协议是「原函数远程执行」：知识库包按允许清单执行原来的知识库函数，调用方只换 import 来源；每个转写或纠错进程配一个自己的知识库进程（保住现在写锁和快照的语义）；知识更新的 LLM 调用留在纠错进程，知识库包没有任何 LLM 调用。现有闸门基本没覆盖有内容的知识库，第 10.6 节列了 8 个要补录的用例。 |

**需要用户定的**：第 13 节，共 9 项。**交回的问题**（改冻结文件或超出批准范围的）：第 14 节，共 3 项。

## 1. 方法与产物

### 1.1 怎么算的

脚本 `scripts/engine_packages.py` 只读 `third_party/finesub/src/`，重跑得到同样的清单（`--check` 不一致时退出码 1）。

1. **符号级静态可达图**：每个模块解析成「import 时执行的代码（`<module>`）」「每个顶层函数」「每个顶层类（含全部方法）」三类节点；节点的边是它读的名字，经该模块的 import（含函数里的惰性 import）解析到别的节点；用到某模块的任何符号就到达它的 `<module>` 以及各级父包。字符串里**整串**是模块名（`["-m", "finesub.llm.agent.agent_mcp_server"]`、`importlib.resources.files("finesub.llm")`）或 `"模块:属性"`（`asr_backend.py:91` 的后端登记）也算边；句子里提到的 `python -m …` 是给人看的提示，不算（脚本里逐条核过，唯一会被执行的一条 `agent_transports.py:178` 从任何入口都到不了）。故意偏大：到达一个类就到达它所有方法；`if TYPE_CHECKING:` 不算；有 `from __future__ import annotations` 的模块里注解不算。
2. **入口**：每个包的适配器今天调的 FineSub 符号，每个都带核心里的调用位置（各 JSON 的 `entries`）。知识库包的入口还包括第 10 问找出的全部访问点的被调函数（`knowledge.json` 的 `server_side`）。
3. **不走的边（`not_taken`）**：`stages.py` 一个文件里装着两半，靠 `stage`/`scope` 判断分开，静态图看不出来。脚本里手列了 10 条「这个包的运行永远不走」的边，每条写明守卫所在的行；例如 `_PIPELINE_DEFAULTS` 只读 `run_pipeline` 的签名（`stages.py:1155`），`_prepare_run` 只在 `stage > raw-srt` 时校验 LLM 配置（`:1270`），纠错适配器只在 `-diar.json` 已在盘上时进 `_run_diarization_stage`（`correction/finesub.py:274`），说话人后端的 `ensure`/`activity` 只在 `stage.run_diarization` 里被调（`speech/diarization/stage.py:259`、`:276`），`asr_backend.BACKENDS` 只给契约测试用（`asr_backend.py:89-92`）。
4. **运行时 import 记录**（`docs/engine-packages/runtime-imports/`）：用 `sitecustomize` 在进程退出时记下 `sys.modules` 里的 `finesub*`。
   - 打桩运行（`--record`，系统 Python）：`tests/test_finesub_correction.py`、`test_axis.py`、`test_llm_worker.py`（纠错，142 个模块）；`test_engine_finesub.py`（转写，31 个）；`test_knowledge_worker.py`、`test_knowledge_subjects.py`（知识库，57 个）；`test_split_stage_contract.py`（只作核对）。
   - 真实运行（`scripts/engine_packages_real_record.py`，PBS 3.12.1，GPU，与 `--real` 相同的 worker 命令与环境，两个样本跑到 `raw-srt`，多人样本开 `acoustics`）：95 个、117 个模块，退出码都是 0。只作核对，不比任何基线。
   - 清单 = 静态 ∪ 打桩记录。真实运行的模块全部落在转写清单里（`coverage.json` 的 `uncovered_runtime` 为空），静态图没漏动态 import。打桩记录里静态没覆盖的只有知识库测试 import 的 `knowledge.entries`、`knowledge.feedback`（推荐设计里它们留在纠错进程，见 10.2；按取并集的规则仍列进知识库包，只是没人调用的代码副本）。
5. **包数据**按读它的模块归（`DATA_READERS`）：`llm/prompt_templates/**`（277 个）归 `prompt_tables.py:16`、`prompt_compose.py:130`、`example_builder.py:53`、`agent/channels.py:50`、`agent/local_agent.py:249` 所在的包；`llm/routing/model_catalog.toml`、`model_routes.toml` 归 `model_catalog.py:514`、`model_routes.py:948`；`llm/knowledge/node/presets/**` 归 `node/presets.py:549`。模板目录作为整体随读者走：模板名大多是运行时拼的，逐个模板归属静态算不出来。
6. **固定规则**：`finesub_bootstrap/` 整个原样进转写包（计划 4.2 第 16 条）；纠错包、知识库包里不列原件，只列兼容包要提供的模块（`compat_modules`）。

### 1.2 产物文件（都在 `docs/engine-packages/`）

| 文件 | 内容 |
|---|---|
| `transcribe.json`、`correct.json`、`knowledge.json` | `files[]`：`path`（相对 `third_party/finesub/src`）、`module`、`why`（`static`/`runtime`/`data`/`bootstrap-whole`/`parent-package`）、`sha256`、`locked`、数据文件的 `read_by`；`entries`、`not_taken`、`compat_modules`、`bootstrap_imports`（每个 `finesub_bootstrap` 名字被包里哪些行 import）、`bootstrap_surface_used`；知识库包另有 `server_side` |
| `coverage.json` | 550 个上锁文件的归属统计、出现在两个以上包里的文件、没归进任何包的 40 个及其 import 方、运行时记录摘要 |
| `knowledge-access.json` | 第 10 问的全部访问点：调用方节点、文件、行号、被调函数、它最终碰到的原语 |
| `import-check.json` | 第 3 问（`--import-check`，PBS 解释器） |
| `runtime-imports/*.json` | 第 1 问的运行时记录 |

WP-P1 开工时（WP-K 改完访问点之后）**重跑一次脚本**：它从代码算，WP-K 换掉的 import 会自动反映进清单。下面给的数字是冻结代码的；WP-K 之后可预见的变化在第 1 问末尾。

## 2. 第 1 问：三个包的精确文件清单

| 包 | 文件 | `.py` | 数据 | 其中上锁 | 组成 |
|---|---|---|---|---|---|
| 转写 `engines/finesub-transcribe/` | 171 | 159 | 12 | 128 | `speech/` 76、`llm/` 25 个 `.py`（见下）加路由数据 2、预设 5、`subtitles/` 8、`media/` 3、顶层 10（含解锁的 `stages.py`）、`finesub_bootstrap/` 42（37 个 `.py` + 5 个数据文件，原件） |
| 纠错 `engines/finesub-correct/` | 466 | 182 | 284 | 465 | `llm/` 125 个 `.py`（含 `llm/knowledge` 28 个的代码副本）+ 模板 277 + 路由数据 2 + 预设 5、`speech/` 33、`subtitles/` 8、`media/` 4、顶层 12（含 `stages.py`）；兼容包 11 个模块 |
| 知识库 `engines/finesub-knowledge/` | 340 | 58 | 282 | 340 | `llm/knowledge` 24、其余 `llm/` 19（提示词表、token 计数、路由配置等纯代码）、顶层 8、`subtitles/` 4、`media/` 3；模板 277 + 预设 5；兼容包 8 个模块 |

每个包的入口（`entries`，带核心里的调用位置）：

- **转写**：`stages.run_transcription`（`transcription/engines/finesub.py:447`）、`reporting.quieted_libraries`/`reporting_to`（`:446`）、`speech.runtime.resources.gpu_tier_cli_choices`（`gpu_tier.py:48`）、`speech.diarization.product.read_product`（`projector.py:224`；转写包要交出「Nonoka 格式的说话人信息」，计划 4.6 第 34 条，而 WP4a 的 `transcript.json` 不带说话人）、`finesub_bootstrap.model_ensure` 的 `-m` 子进程（WP-R7 已删掉这个下载子进程，引擎不再自己下载模型）。
- **纠错**：`stages.run_post_stable`、`default_pipeline_paths`、`_run_diarization_stage`、`resolve_llm_media_for_source`（`correction/finesub.py:265`、`:106`、`:336`、`:450`）、`speech.diarization.evidence.evidence_path`（`:113`）、`subtitles.line_groups.in_order`（`:159`）、`reporting.*`（`:258`、`worker.py:585`）、`llm.correction_request.CorrectionRequest`、`correction_translation.run_correction_task`、`routing.profiles.resolve_profile`（`:436-438`）、`correction_mode.recorded_choice`/`started`（`worker.py:407`）、`routing.model_routes`（`worker.py:491`、`settings.py:365`、`:384`）、`routing.model_catalog.default_model_catalog`（`settings.py:476`）、`config.clear_config_cache`（`settings.py:1071`）、`agent.agent_paths.vendor_error_text`（`worker.py:531`）、`llm.client.RoleClient`/`is_quota_or_rate_limit_error`、`rate_limit.ModelRateLimiter`、`routing.config.LLMRole`（`llm_worker.py:48`、`:63`、`:79`、`:115`）、`api_http`（`finesub_bridge.py:87`）、`read_product`（`projector.py:224`，WP-L 的 `correction.json` 要它）、MCP 子进程 `llm.agent.agent_mcp_server`（`client.py:2504`、`agent_session_host.py:658`）。
- **知识库**：`knowledge_worker.py:10-12` 的 `knowledge_root_path`、`knowledge_write_lock`、`node.edit.edit_subject`、`node.repo.KnowledgeRepo`；`worker.py:231` 的 `node.proposals.apply_model_proposals`；加上第 10 问的 24 个被调函数。

**转写包里的 25 个 `llm/` 模块**（第 2 问的要点）：`stages._prepare_run` 对每个入口都无条件调 `resolve_llm_switches`（`stages.py:1265` → `llm.correction_request`、`llm.routing.profiles`）和 `task_diarize`/`default_diarize`（`:1328` → `llm.correction_mode`），所以转写进程确实会 import `llm.correction_mode`、`correction_request`、`output_tags`、`routing/*` 这 10 个模块（真实运行记录证实）；另外 15 个是 `build_asr_context`（`stages.py:1736`）要的 `llm/knowledge/*`。**WP-K 之后**，如果 `build_asr_context` 那 5 个名字都改从协议客户端取（第 10.4 节的推荐），这 15 个 `llm/knowledge` 模块和 `llm/output_tags.py` 会从转写包掉出去（脚本模拟：138 → 122 个模块），只剩 9 个 `llm/` 模块（含 `llm` 包本身）。连带一点：这些模块里有 `routing.model_routes` 和 `model_catalog`，`resolve_profile` 会读路由配置，所以**转写包的桥接也要派生 `finesub.toml` 里与路由预设有关的部分**，与现在 worker 读到的相同（环境基线 `environment.json` 覆盖这一项）。

## 3. 第 2 问：共用底座

转写包和纠错包共用 **83 个**上锁文件（`coverage.json` 的 `in_two_or_more` 里同时在两边的那些），其中 38 个三个包都有：

- 顶层：`__init__.py`、`config.py`、`jsonl.py`、`paths.py`、`reporting.py`、`run_metadata.py`、`settings.py`、`text.py`，以及解锁的 `stages.py`（不在 550 个之内，两边都带）；
- `subtitles/`：`line_groups`、`metrics`、`model`、`postprocess`、`rendering`、`stable_rows`、`time_order` 与包文件；
- `media/`：`ffmpeg`、`source` 与包文件；
- `speech/`（32 个）：`diarization/` 下的 `annotate`、`assign`、`backend`、`evidence`、`judgments`、`labels`、`marks`、`product`、`row_split`、`segments`、`stage`，三个后端模块 `diarizen_backend`、`nemotron3_backend`、`sortformer_backend` 和它们 import 时就要的 `diarizen_assemble`、`diarizen_runner`、`diarizen_runtime`（import 时不碰 torch，第 3 问实测）；`postprocessing/` 的 `recall_repair`、`recall_sounds`、`segmentation`、`squeeze_repair`、`stabilization`；`recognition/` 的 `asr_backend`、`segments`；`runtime/` 的 `native_paths`、`resources`；`languages.py`；
- `llm/`（12 个）：包文件、`correction_mode`、`correction_request`、`output_tags`、`routing/{__init__,config,migrate_catalog,model_catalog,model_routes,profiles}` 与两份 toml；`llm/knowledge` 的 20 个（15 个 `.py` 加 5 份预设，WP-K 之后不再共用）。

**有没有只被一边用、可以不带的**：共用底座按定义两边都到达，没有。值得注意的是三处「看起来只一边用」其实两边都要：
- `speech/postprocessing/stabilization.py`、`speech/recognition/asr_backend.py`、`segments.py`、`speech/runtime/resources.py`：`stages.py` 在 import 时就要（`stages.py:38-48`），纠错包也得带；
- `speech/diarization/stage.py` 连同三个后端模块：纠错包用它从时间线写行标签（`_run_line_label_rows`、`_label_rows_from_timeline`），`stage.py:29` 在 import 时就 import 三个后端模块；
- `llm/correction_mode` 等：转写包也要（见第 1 问）。

**只在一边的**：转写包独有 45 个（分离、VAD、识别、qwen 裁判、说话人模型的运行代码、`speech/runtime` 的设备与资源调度、`doc_links.py`）；纠错包独有 80 个（`llm/` 的纠错、翻译、研究、Agent、提示词全部，`api_keys.py`、`api_http.py`、`state.py`，`speech/diarization/export.py`、`media/clips.py`）。

## 4. 第 3 问：纠错包、知识库包能否在启动器环境里完整 import

`--import-check`：PBS 3.12.1 以 `-I -B` 启动，在所有 finder 前装一道闸，只放行标准库、httpx、pydantic、opencc-python-reimplemented、tzdata 这几个发行版及其依赖（实际放行 13 个发行版，见 `import-check.json`），以及**本包清单里的** `finesub` 模块和兼容包的模块；然后逐个 import 清单里的每个模块。

| 包 | 模块数 | 失败 | torch/numpy/scipy 等 | 被挡下的尝试 |
|---|---|---|---|---|
| 纠错 | 193（182 + 兼容 11） | 0 | 无 | `brotli`、`brotlicffi`、`click`、`zstandard`：httpx 的可选依赖，它自己 try/except |
| 知识库 | 66（58 + 兼容 8） | 0 | 无 | 同上 |

两点说明：
- 这个检查一开始抓到纠错清单里有 4 个模块 import 时就要 numpy/torch（`fw_refine_backend` 等），来源是 `asr_backend.BACKENDS` 的字符串登记；查实那份登记「只给契约测试用」后列为不走的边。也就是说**这道检查本身可以当 WP-P1 的测试**：包目录里只放清单文件、按同样的闸 import 全部模块。
- 读音提示要的 `pyopenjtalk`、`pykakasi`、`rapidfuzz` 在启动器环境里本来就没有，`kb_phonetic_hints.py:71` 在用到时才 import、缺了降级，和现在相同（闸门回放基线也是在没有它们的环境下录的）。

## 5. 第 4 问：`finesub_bootstrap` 依赖面的 48 个名字

T = 转写包里的上锁文件 import 它（转写包带原件），C = 纠错包，K = 知识库包。调用点见各 JSON 的 `bootstrap_imports`。

| 模块 | 名字 | T | C | K | 主要调用点 |
|---|---|---|---|---|---|
| `agy_records` | `agy_project_records_dir` | | C | | `llm/agent/local_agent.py:7496` |
| | `remove_project_records_under` | | | | 只有 `llm/agent/agent_cleanup.py:11`（不进任何包） |
| `artifacts` | `ARTIFACT_DIR_SUFFIX`、`RESOLVED_SWITCHES_FILENAME` | | C | | `llm/correction_translation.py:32`、`stages/correction/run.py:36` |
| `download_routes` | `resolve_region` | T | | | `separator/separation.py:296` |
| `fsops` | `RECORD_REPLACE`、`replace_path` | T | C | | `media/source.py:10`、`state.py:21` |
| | `write_atomic` | T | C | | 同上，另 `correction_mode.py:16`、`research.py:53` 等 |
| | `is_directory_link`、`remove_tree` | | C | | `agent_task_runtime.py:25`、`local_agent.py:33`、`client.py:2110` |
| `hf_verify` | `VerificationMismatch` | T | | | `speech/runtime/hf_weights.py:84` |
| `http_client` | `in_tunnel_setup`、`watch_tunnel` | | C | K | `api_http.py:34` |
| `locks` | 5 个 `AGENT_*` 常量 | | C | | `agent/agent_paths.py:22` |
| | `LockUnavailable`、`holding_lock` | | C | K | `local_agent.py:34`、`knowledge/base.py:840`、`state.py:22` |
| | `holding_activity` | | C | | `agent_session_host.py:59`、`local_agent.py:34` |
| | `holding_activity_barrier` | | | | 只有 `agent_cleanup.py:13`（不进任何包） |
| | `try_lock` | | | | 只有 `batch_state.py:307`、`agent_cleanup.py`（不进任何包） |
| `logs` | `log_directory` | T | C | | `paths.py:242` |
| | `prune` | | C | K | `llm/http.py:33` |
| `model_caches` | 4 个 | T | | | `paths.py:349`、`separation.py:26`、`vad_asr_stage.py:1028` |
| `model_ensure` | 4 个 | T | | | `speech/runtime/hf_weights.py:73-131`、说话人后端 |
| `model_fetch` | 模块与 `fetch_fixed_files` | T | | | `separation.py:295` |
| `model_manifest` | `entry_for` | T | | | `separation.py:297` |
| `paths` | `default_data_root`、`load_app_paths`、`packaged_app_paths`、`packaged_app_root` | T | C | K | `finesub/paths.py:35`、`:54`、`:72` |
| `processes` | `no_window` | T | C | K | `media/ffmpeg.py:10`、`token_budget.py:33`、`local_agent.py:35`、`knowledge/node/importer.py:20` |
| `secrets` | 模块、`ensure_protected`、`read_env_file` | | C | | `api_keys.py:17` |
| `token_counter` | 模块、`CHECKOUT_RELATIVE_PATH`、`configured_path`、`find_on_path` | T | C | K | `finesub/paths.py:9`、`llm/token_budget.py:32` |

**纠错兼容包的精确名单（32 个）**：`agy_records:agy_project_records_dir`；`artifacts:ARTIFACT_DIR_SUFFIX`、`RESOLVED_SWITCHES_FILENAME`；`fsops:RECORD_REPLACE`、`is_directory_link`、`remove_tree`、`replace_path`、`write_atomic`；`http_client:in_tunnel_setup`、`watch_tunnel`；`locks:AGENT_ACTIVITY_ROOT_VARIABLE`、`AGENT_CAPSULE_ROOT_VARIABLE`、`AGENT_IDENTITY_ANCHOR_VARIABLE`、`AGENT_LOCATOR_KIND_MANAGED`、`AGENT_LOCATOR_KIND_VARIABLE`、`LockUnavailable`、`holding_activity`、`holding_lock`；`logs:log_directory`、`prune`；`paths:default_data_root`、`load_app_paths`、`packaged_app_paths`、`packaged_app_root`；`processes:no_window`；`secrets`（模块）、`secrets:ensure_protected`、`read_env_file`；`token_counter`（模块）、`token_counter:CHECKOUT_RELATIVE_PATH`、`configured_path`、`find_on_path`。兼容包要有的模块：`agy_records`、`artifacts`、`fsops`、`http_client`、`locks`、`logs`、`paths`、`processes`、`secrets`、`token_counter`（`correct.json` 的 `compat_modules`）。与计划 4.2 第 16 条的预期一致：没有 `model_*`、`hf_verify`、`model_fetch`、`download_routes`。

**知识库包也需要兼容包**，名单 14 个：`http_client:in_tunnel_setup`、`watch_tunnel`；`locks:LockUnavailable`、`holding_lock`；`logs:prune`；`paths` 的 4 个；`processes:no_window`；`token_counter`（模块）与它的 3 个。模块 8 个：`fsops`（`locks` 内部要）、`http_client`、`locks`、`logs`、`paths`、`processes`、`token_counter` 与包文件。其中 `locks.holding_lock` 实现知识库写锁（`knowledge/base.py:840`），决定结果的是 `token_counter`（知识库包里只有 `HeuristicTokenCounter` 和截断用到它，见 10.2）。

**`pipeline.py` 归纠错包？做不到。** WP-B 交回的 `locks.holding_lock`、`LockUnavailable` 本身已在纠错名单里，但 `pipeline.py` 是 FineSub 的批处理命令行：它在 import 时就要 `speech/preprocessing/separator/rates`、`speech/runtime/native_paths` 等，跑起来走 `run_pipeline` 两半（`pipeline.py:71-93`、`:1707`），连同 `batch_state.py`、`scheduler.py` 一起会把 41 个转写模块和 `model_caches`、`model_ensure`、`model_fetch`、`model_manifest`、`hf_verify`、`download_routes`、`locks.try_lock` 带进纠错包，违反兼容包「不含 `model_*`」的约定。Nonoka 不跑这个命令行（只有 `tests/test_split_stage_contract.py:201` import 它的 `build_parser`）。推荐三个文件都不进任何包（第 13 节第 2 项）。

## 6. 第 5 问：核心现在直接用 FineSub 的每一处

「协议」一列是要加的查询；没写的表示只是搬进适配器，协议不变。

| 位置 | 用的东西 | 归属 | 协议上要加什么 |
|---|---|---|---|
| `transcription/engines/finesub.py:446-447` | `run_transcription`、`reporting` | 转写包适配器（WP-P1 搬） | 转写任务（现 worker 协议同形） |
| `correction/finesub.py:106-450` | `run_post_stable`、`_run_diarization_stage`、`default_pipeline_paths`、`resolve_llm_media_for_source`、`CorrectionRequest`、`run_correction_task`、`resolve_profile`、`evidence_path`、`in_order`、`reporting` | 纠错包适配器 | 纠错任务 |
| `axis.py:122`、`:167` | 经 `correction.finesub.stable_from_source_rows`、`run_source_text` | 纠错包适配器 | 纠错任务的「源文本轴」输入（`TranscriptionResult` 之外的一种输入） |
| `worker.py:407` ⟳WP4c | `correction_mode.recorded_choice`/`started`（中文任务续跑时判断走「只纠错」） | 纠错包适配器：读的 `llm-artifacts/correction-mode.json` 在纠错包的私有目录里，判断必须挪进包 | 无（包内做） |
| `worker.py:491` ⟳WP4c | `model_routes.install_runtime_preferred`、`parse_llm_model_args`（请求里的 `llm_model` 覆盖） | 纠错包适配器 | 无 |
| `worker.py:531` ⟳WP4c | `agent_paths.vendor_error_text`（失败事件的厂商报错） | 纠错包适配器 | 失败事件里带 `detail` |
| `worker.py:585`、`:608` ⟳WP4c | `reporting`、`run_pipeline` | WP4c 换成两个适配器 | — |
| `worker.py:231`、`:284` ⟳WP4c | `prepare_knowledge_subjects`、`_create_knowledge_subject`（`KnowledgeRepo`、`apply_model_proposals`） | 知识库包 | `subjects.prepare`（第 8 问） |
| `worker.py:676` ⟳WP4c | 读 vendor 的 `UPSTREAM.json` 填产物清单的 `engine_commit` | 各包 | `describe`：返回 `PACKAGE.json` 的标识、版本、上游 commit |
| `settings.py:343` | `finesub_bootstrap.secrets` | 核心自己的密钥存储（WP-R4），与包无关 | — |
| `settings.py:365` | `default_model_routes`（`correction_proofread`：Nonoka Cloud 路由时关校对） | 纠错包适配器（任务开始时在包内解析） | 无；若界面要显示，`llm.proofread_default` |
| `settings.py:384` | `load_model_routes`（本地 Agent 的模型目标） | 纠错包 | `llm.local_agent_targets(provider)` |
| `settings.py:476`、`:954` | `default_model_catalog`（Gemini 内置模型、目标 id 映射） | 纠错包；映射本身是派生 `[llm.preferred_targets]` 的一部分，进纠错包桥接 | `llm.model_options` |
| `settings.py:615-632`、`:935` | `finesub.env`/`finesub.toml` 读写、`FINESUB_*` 写进 `os.environ` | WP-R4、WP-R5 → 两个包的桥接 | 启动上下文 |
| `settings.py:1071-1073` | 清缓存 | 删除（上两项挪走后不需要） | — |
| `projector.py:224` | `speech.diarization.product.read_product` | 两边都要：转写包适配器把 `-diar-rows.json` 的标签转进 `transcript.json`（计划 4.6 第 34 条的「Nonoka 格式的说话人信息」）；纠错包适配器把判定后的说话人转进 `correction.json`（WP-L）。两个包的清单里都有 `product.py` | 无（产物转换在包内） |
| `gpu_tier.py:48` | `gpu_tier_cli_choices` | 转写包 | 推荐写成 `PACKAGE.json` 的静态能力声明（纯数据，零次环境读取，R0 文档 1.9），由测试对照原函数；不必每次起进程问 |
| `llm_worker.py:48-115` | `RoleClient`、`ModelRateLimiter`、`LLMRole`、`is_quota_or_rate_limit_error` | 纠错包的常驻 LLM 进程 | `llm.call`（与现在 `llm_worker` 的 NDJSON 请求同形） |
| `knowledge_worker.py:10-12` | 列表、读、存 | 知识库包 | 第 8 问 |
| `local_provider.py:50`、`:1336`、`:1680` | 产物名白名单与投影 | WP-L | — |
| `local_provider.py:120`、`:402` | `UPSTREAM.json`（能力里的 engine 信息） | 各包 | `describe` |
| `local_provider.py:141` | `FINESUB_MODEL_DIR` 下 `accel/*/probe.json` 的旧探针清理 | 加速已关，WP-H 删 | — |
| `local_provider.py:243`、`:1113` | 就绪检查（WP5） | 各包 | `readiness(request)` |
| `local_provider.py:921-985` | 三种 worker 的启动命令、`PYTHONPATH` 含 vendor、`_engine_environment` | `engine_host.py` | — |
| `finesub_bridge.py:73-279` | `finesub_bootstrap` 的下载线路补丁、`api_http` 的代理策略 | 拆成两份：下载那部分随转写包（WP-R7 删自下载后大多失效），`api_http` 代理策略随纠错包（知识库包也 import `api_http`，但不发请求） | — |
| `provision.py`、`runtime_install.py`、`model_install.py` | `finesub_bootstrap` 的安装设施 | 核心（WP-E3 换成层管理器），与包无关 | — |
| `sidecar.py:182` | `FineSubSettings` | WP-R4 | — |

## 7. 第 6 问：产物清单逐项

现在的产物清单（`worker.py:647-668`、`correction/finesub.py:355-367`）：

| 产物 | 格式 | 归属与去向 |
|---|---|---|
| `transcript.json`（WP4a 新增） | Nonoka | 转写包适配器写；核心保存 |
| `vocal_audio`（`-vocal.ogg`） | FineSub 中间产物（音频本身是通用格式，但只有 FineSub 用） | 转写包私有附件；纠错包的说话人裁判 `judge_audio=vocal` 要听它，经附件交给纠错包 |
| `vad_json`、`vad_energy_npz`、`aligned_json` | FineSub | 转写包私有附件，只给续跑用 |
| `stable_json` | FineSub | 转写包私有附件；转写包转出 `transcript.json`；原样交纠错包（计划 5.2） |
| `diar_json`、`diar_rows_json`、`diar_evidence_npz` | FineSub | 转写包私有附件；`diar_rows` 的标签由转写包转进 `transcript.json`；三者原样交纠错包（WP4b 的键名） |
| `metadata_json`（运行元数据） | FineSub | 写它的包的私有附件（两半各自 `update_run_metadata` 同一个文件，拆开后各一份，见第 7 问） |
| `raw_srt` | 中性（SRT） | 默认路径（FineSub 转写 + FineSub 纠错）仍由纠错包的 `run_post_stable` 写，与基线逐字节；纠错包不在时由核心从 `transcript.json` 渲染（计划 4.6 第 33 条）。两者字节未必相同，见第 13 节第 6 项 |
| `translated_srt`、`final_srt` | 中性（SRT） | 纠错包产出，核心保存 |
| `speaker_judge_json` | FineSub | 纠错包私有附件；内容由纠错包转进 `correction.json`（WP-L） |
| `annotated_csv` | FineSub | 纠错包私有附件；纠错包转出 `correction.json`（WP-L，计划 4.8 第 38 条） |
| `correction_mode_json`（`llm-artifacts/correction-mode.json`） | FineSub | 纠错包私有附件；投影要的「译文还是润色」进 `correction.json` |
| `llm-artifacts/**`（交换日志、`task-artifacts.jsonl`、研究上下文、`resolved-switches.json`、任务报告） | FineSub | 纠错包私有附件，不进清单 |
| 云端的 `stable_json`、`diar_json`（`source.kind=cloud_stable`） | FineSub | 不透明附件，键 `finesub.stable_json`/`finesub.diar_json`，交纠错包原样使用（计划 4.7 第 36 条） |
| `request.json`、事件、`telemetry-run.json` | Nonoka | 核心 |

## 8. 第 7 问：续跑与重试怎么保持

现状：`retry`/`resume` 都是在**同一个任务目录**重新拉起 worker（`local_provider.py:1514-1544`）；worker 以 `resume=True` 调 `run_pipeline`，FineSub 在每一阶段先看 `<task>\workspace\<title>-*.json` 是否已在盘上（`stages.py` 的 `_use_or_create`、`_run_transcription_stages:1428` 起各分支）；云端续跑把 stable 复制成同名文件（`worker.py:343` → `place_native`，「已存在就不拷」）。

推荐：
1. **私有目录里保持 FineSub 的相对布局**。转写包适配器把 `output_path` 设为 `tasks\<id>\native\finesub-transcribe\<title>.srt`、`task_artifact_dir` 设在同目录下，FineSub 的产物名由 `default_pipeline_paths` 从 `output_path` 推出，跳过逻辑一行不用改。纠错包同理用 `native\finesub-correct\`，它的 `place_native` 已经是「目标已在就不拷」（WP4b），重试时用的是第一次拷进来的字节，与现在「重试接着用第一次渲染的 stable」相同。
2. **核心记一份转写结果**：转写成功后核心写 `tasks\<id>\transcription-result.json`（`TranscriptionResult.to_dict()`：`transcript.json` 路径、`native` 附件表、引擎、元数据）。重试时有它且文件都在，就不再拉起转写进程（省掉沙箱和 torch import；转写包被删了也能续跑纠错）。没有它就照常拉起，FineSub 自己跳过已完成的阶段。
3. **两半各一份运行元数据**：现在两半写同一个 `-metadata.json`（阶段耗时等）。拆开后各在自己目录里写，比对标准本来就排除了耗时（计划第 0 节第 3 条），不影响闸门。
4. **中文任务「只纠错」的续跑判断**（`worker.py:396-417` ⟳WP4c）读的是纠错包私有目录里的 `correction-mode.json`，必须挪进纠错包适配器。
5. **旧任务**（WP-M2）：现在一个 `workspace\` 里混着两半的产物，迁移时按第 6 问的表分到 `native\finesub-transcribe\` 和 `native\finesub-correct\`（`stable`、`diar*` 两边都要的放转写目录、纠错目录按「已在就不拷」第一次续跑时自己拷），并补写 `transcription-result.json`。

## 9. 第 8 问：知识库管理的每个操作

界面只有这四处入口，全部经 `POST /v1/knowledge`（`sidecar.py:94`、`desktop/internal/provider/service.go:160`）或任务提交：

| 操作 | 界面位置 | 现在的实现 | 知识库包的查询 |
|---|---|---|---|
| 列出条目 | `KnowledgePage.tsx:34`；新建任务时选主体 `TranscriptionDialog.tsx:376` | `knowledge_worker.py:23-27`：`repo.subjects(rev)` 的 id、名、类、简介与 rev | `subjects.list` → 原样返回 |
| 读一条 | `KnowledgePage.tsx:53` | `:35-40`：`entry_text` 加内容哈希作版本 | `subject.read(id)` |
| 保存 | `KnowledgePage.tsx:62` | `:41-53`：加写锁、按版本 CAS、`edit_subject`、返回新内容与拒绝项 | `subject.save(id, version, content)`；写锁在知识库进程里加 |
| 任务前准备主体 | 提交任务（`worker.py:267-307` ⟳WP4c） | 按 id 或名解析主体，`update` 模式下不存在就 `apply_model_proposals` 新建 | `subjects.prepare(subjects, mode, task_id)` → `{names, created, rev}` |
| 就绪 | 知识库页「未安装」、开了知识库的请求被拦下（计划 4.6 第 35a 条） | 无 | `describe` / `readiness`：包在不在、数据目录在不在、条目数 |

UI 操作每次一个短命的知识库进程（与现在 `knowledge_worker` 的 `subprocess.run` 相同），不需要常驻。

## 10. 第 9 问：启动开销，日志与进度

**实测**（本机，PBS 3.12.1 `-I`，已有 `.pyc`，各跑三次）：

| 进程 | 内容 | 耗时 |
|---|---|---|
| 解释器本身 | `-I -c pass` | 0.14–0.15 s |
| 转写（轻） | `import finesub.stages` | 0.21–0.30 s |
| 转写（重） | 再加 `vad_asr_stage`、`separation`、`diarization.stage`、`torch` | 3.2–4.2 s（现在的单进程同样要付） |
| 纠错 | `stages`、`correction_translation`、`llm.client` | 1.0–1.1 s（首次 1.9 s） |
| 知识库 | 知识库服务要的模块 | 0.52–0.61 s |
| 协议往返 | 本机回环 TCP、NDJSON、2 KB 结果，不含服务端工作 | 约 0.12 ms/次 |

沙箱准备（建 `sandbox\temp\<进程>\`、写并持有锚点文件、构建环境变量）是几个文件系统操作，毫秒级，没单独测。新增开销合计：纠错进程约 1–2 s，知识库进程约 0.5 s，与真实样本每次 68–90 s 比约 1–3 %。`.pyc` 按计划预编译（4.2 第 17 条）；没预编译时第一次会慢约 40 s（实验报告 2.7），那是层的问题，不是拆进程的。

**日志与进度保持一致**（⟳WP4c）：
- 事件仍走 FineSub 的 reporter：每个包进程里由适配器绑 `NonokaXReporter` 那一套（去重、过滤 API 调用），把 `log`、`stage_started`、`planned`、`warning` 原样写成 NDJSON 事件；核心把两段进程的事件按顺序接成一条任务事件流。WP4b 已交回「没绑 FineSub reporter 时没有去重与过滤」，所以**两个包的适配器都必须绑同一个 reporter 实现**，否则日志会变。
- 进度的分母：`_prepare_run` 每个入口各报一次 `planned`（`stages.py:1406-1412`：转写段报到 stable 为止的阶段，纠错段报 `raw-srt` 之后的）。现在 `run_pipeline` 一次报全部。核心要把两段的 `planned` 合成一个（第一段开始时就知道整个任务的目标，可以先发完整的计划、忽略第二段的 `planned`，或累加）。这和 WP4c 在单进程里要解决的是同一件事（它的完成标准「任务日志里不出现重复的『已有结果，跳过』」），拆进程只是把它的做法搬到核心。
- 知识库进程在服务端发的 reporter 事件（例如 `knowledge_signals_enabled` 的「当前在 worktree 中运行」警告 `base.py:798`、`harvest_rendered_edits` 的降级警告）要随响应带回客户端，由客户端用自己的 `current_reporter()` 再发一次，否则任务日志会少行（10.3 第 6 条）。

## 11. 第 10 问：知识库的访问点

### 11.1 调用点清单

「访问点」= 不属于知识库数据层的代码，调用了会打开知识库存储（`KnowledgeRepo`/`KnowledgeStore`/SQLite）、读写知识库目录或加写锁的函数。由脚本按符号图找出（`knowledge-access.json`：调用方、行、被调函数、最终碰到的原语），被调函数的读写性质逐个读代码确认。推荐设计里 `knowledge/update.py`、`knowledge/entries.py` 留在纠错进程（10.2），所以它们内部的调用也算访问点。

读写性质：**读** = 只读存储；**信号** = 写命中/曝光/落地等遥测事件（幂等插入，不加写锁，失败吞掉，`base.py:787-815`）；**写锁** = 跨进程写锁（`base.py:830`，`<知识库>.lock`）；**pin** = 读当前 rev 并设本进程的快照 ContextVar（`base.py:322-357`）。

| 文件 | 点数 | 调用（行：调用方 → 被调，性质） |
|---|---|---|
| `finesub/stages.py`（解锁但冻结） | 4 | 459：`_validate_llm_configuration` → `style.resolve_style_keys`（读）；463：→ `subjects.resolve_subject_keys`（读）；1746：`build_asr_context` → `base.load_preinjected_entries`（读）；1747：→ `base.match_terms`（读）。另用 `DEFAULT_KNOWLEDGE_ROOT`（`:446`、`:1737`） |
| `finesub/llm/correction_translation.py` | 9 | 314 `_auto_style_catalog` → `load_style_catalog`（读）；469 `_record_named_style` → `resolve_style_keys`（读）；544 `_renderable_style_rev` → `load_style_entries`（读）；1167 `_run_task` → `record_style_injected`（信号）；1207 → `render_style_block`（读）；1209 → `subjects.render_subject_block`（读）；1420 `_split_tagged` → `load_style_entries`（读）；1579 `_generation_pin` → `node.edit.harvest_rendered_edits_at_run_start`（写，内部加写锁，`edit.py:22`）；1582 → `base.pinned_generation_rev`（pin）。另 `run_post_correction_knowledge_update`（`:186`）调知识更新 |
| `finesub/llm/knowledge/update.py` | 11 | 392 `repair_knowledge_proposals` → `KnowledgeRepo`（rev，之后 `:491` `apply_model_proposals` 写）；680 `_revision_with_proposal` → `store.conn.execute` 原始 SQL（读 revisions 表）；790 → `worktree_blocks_knowledge_write`（闸）；887、896 → `resolve_style_keys`、`resolve_subject_keys`（读）；1016、1281、1359 → `KnowledgeRepo`（rev；1281 传给 `apply_model_proposals` 写）；1033 → `load_index_texts`（读）；1244 → `knowledge_write_lock`（写锁，包住 1279 的应用）；1402 `_book_refined_alignment_evidence`、1457 `_book_style_marks` → `store`（信号）；1430 `_style_marks_target` → `repo.resolve`（读） |
| `finesub/llm/knowledge/entries.py` | 2 | 83 `select_kb_entries`、244 `render_kb_entry_excerpt` → `KnowledgeRepo`（`resolve`、`rev`、`store.node`，读） |
| `finesub/llm/research.py` | 9 | 222、230 `resolve_round1_entries` → `load_entry_texts`；362、372 `render_preinjected_entries` → `load_preinjected_entries`、`match_terms`；874 → `load_index_entries`；886 → `load_index_texts`；902、903、907 `research_knowledge_inputs_hash` → 同上三个（都是读）；1436 `_run_chunked_research` → `style.style_vote_facts`（读信号表） |
| `finesub/llm/stages/correction/run.py` | 11 | 139、142 `_shadow_scan_windows` → `knowledge_signals_enabled`、`KnowledgeRepo.open(...).store` → `matching.shadow_scan`（信号）；178、192 `_log_landed_windows` → 同上 → `signals.log_landed_windows`（信号）；815 → `load_index_texts`、835 → `render_style_block`、859 → `knowledge_version`（读）；1180 `_settled_style_block` → `render_style_block`（读）；1262 `_window_terms` → `load_entry_texts`（读） |
| `finesub/llm/stages/correction/attempts.py` | 6 | 273、277 `_log_window_exposures` → `knowledge_signals_enabled`、`KnowledgeRepo.open` → `signals.log_exposed_entries`（信号）；561 `_pin_style_pick` → `render_style_block`（读）；564 → `record_style_injected`（信号）；1010 `run_window_attempts` → `load_entry_texts`（读） |
| `finesub/llm/stages/correction/request_turn.py` | 3 | 421 → `render_style_block`（读）；427 → `record_style_injected`（信号）；443 → `load_entry_texts`（读） |
| `finesub/llm/stages/correction/parallel.py` | 2 | 287 → `repin_generation_rev`（pin）；313、338 → `load_entry_texts`（读） |
| `finesub/llm/stages/correction/serial.py` | 2 | 76 `_carried_view` → `knowledge_root_path`（路径）；256 → `load_entry_texts`（读） |
| `finesub/llm/stages/correction/context.py` | 1 | 109 `CarriedContext` → `load_entry_texts`（读） |
| `finesub/llm/stages/prepare_task.py` | 2 | 408 → `load_index_texts`；694 `_payload` → `load_entry_texts`（读） |
| `finesub/llm/stages/plan.py` | 1 | 317 `_opening_input_estimate` → `load_index_texts`（读） |
| `finesub/llm/stages/speaker_judge/roster.py` | 2 | 26 → `match_index_keywords`；30 → `load_index_entries`（读） |
| `finesub/llm/prompt_artifacts.py` | 2 | 138 → `load_index_texts`；180 → `render_style_block`（读） |
| `finesub/llm/length_tier.py` | 1 | 102 `styles_tier` → `load_style_entries`（读） |
| `finesub/llm/kb_phonetic_hints.py` | 2 | 196 `library_terms` → `KnowledgeRepo.open(...).store`（`node`、`parents`）；240 `window_terms` → `repo.resolve`（读） |
| `finesub/llm/client.py` | 4 | 257 `_agent_task_inputs` → `knowledge_signals_enabled`（闸）；331、348 `_agent_knowledge_binding` → `knowledge_root_path`、`kb_index_block_text`（读）；627 `_claim_for_opening` → `kb_index_block_text`（读） |
| `finesub/llm/agent/agent_mcp_server.py`（MCP 子进程） | 2 | 1260、1288 `HarnessToolServer`：`KnowledgeRepo`（`resolve`、`resolve_qualified`、`store.node`、`store.parents`）、`kb_index_block_text`，加上 1248-1563 一串只在有仓库对象时调用的 `render_subject`、`ExactIndex.build`、`log_exposed_nodes`（信号）、`proposals.preview` 等（读） |
| 核心 `src/nonoka_x/worker.py` ⟳WP4c | — | 284 `prepare_knowledge_subjects` → `KnowledgeRepo`（`subjects`、`resolve`、`rev`，读）；231 `_create_knowledge_subject` → `apply_model_proposals`（写，**不另加写锁**，靠存储的 CAS） |
| 核心 `src/nonoka_x/knowledge_worker.py` | — | 第 8 问的三个操作（读；写锁 + `edit_subject`） |

转写那一半只有 `build_asr_context` 一处（读，不加锁）；其余都在纠错那一半。

### 11.2 协议设计（推荐）

**形状：「原函数远程执行」。** 知识库包的服务端持有一张**允许清单**，项目就是上表里的被调函数（`knowledge.json` 的 `server_side`，24 个函数）加若干「仓库方法」（`KnowledgeRepo` 的 `rev`、`subjects`、`resolve`、`resolve_qualified`、`entry_text`、`store.node`、`store.parents`，以及把仓库对象作第一个参数的 `shadow_scan`、`log_landed_windows`、`log_exposed_entries`、`log_exposed_nodes`、`apply_model_proposals`、`edit_subject`、`book_style_marks`、`refined_alignment_evidence`、`preview`，再加 `update.py:680` 那条 SQL 作为一个具名查询）。服务端收到调用就用**原来的知识库代码**执行，所以选哪些条目、怎么匹配、怎么渲染都不变。调用方的改动只是「这个名字从哪里来」：上锁文件里的 `from ...knowledge.base import load_entry_texts` 改成从协议客户端取同名同签名的函数；以仓库对象为参数的地方，客户端给一个仓库句柄代替真实对象。

- **请求**：`{id, op, args, kwargs, pins, repo_handles}`。`pins` 是客户端当前的 `active_generation_pins()`，服务端执行前 `bind_generation_pins(pins)`，所以 `_effective_rev` 读到的快照与现在相同。`pinned_generation_rev`/`repin_generation_rev` 在客户端实现成「远程读 rev + 本地设 ContextVar」（ContextVar 留在客户端进程的 `base.py` 里）。
- **响应**：`{id, result | error, reporter_events}`。结果里的 frozen dataclass（`TermMatch`、`KeywordMatch`、`IndexEntry`、`ResolvedEntry`、`NodeVersion`、`StyleCatalog`、报告对象等）用一个按类名编解码的 codec 往返，客户端还原成同一个类（客户端进程里有这些类的代码，见 10.5）。异常按类型还原（`AmbiguousName`、`StaleWriteError`、`NotFoundError`、`LockUnavailable`、`StyleContractError` 等），因为调用方会按类型捕获。服务端执行期间的 reporter 事件随响应带回，客户端再发一次。
- **不能远程的**：参数里有可调用对象的函数，例如 `entries.select_kb_entries(count_tokens=...)`（`entries.py:215`）。这类函数留在客户端执行，只把它们内部的仓库读取换成句柄调用。这就是 `entries.py`、`update.py` 留在纠错进程的原因之一。
- **写锁**：`knowledge_write_lock` 是客户端里包住一段代码的 context manager（`update.py:1243-1279`：先锁、再应用提案）。协议给 `lock.acquire(root) → token`、`lock.release(token)`，服务端持有真实的 OS 锁；客户端连接断开时服务端释放（进程被杀不留死锁）。
- **进程拓扑（推荐「每个客户端进程配一个知识库进程」）**：核心拉起转写进程或纠错进程时，同时拉起一个只服务它的知识库进程，任务结束一起收掉；界面操作各起一个短命的。理由：现在的写锁语义是「同一进程的线程共享一把 OS 锁、不同进程互斥」（`base.py:818-828`），快照 pin、`_SIGNAL_SKIP_WARNED` 等也是进程级状态。一对一配对后，「一个纠错进程 + 它的知识库进程」在这些语义上等于现在的一个 worker 进程；多个任务之间仍然跨进程互斥。如果只起一个全局的知识库服务，所有任务的写入都成了「同一进程」，现在任务之间的互斥会变成共享，必须改 `base.py` 的锁实现，超出批准范围。代价是每个任务多约 0.5 s。
- **连接方式**：包进程之间**不经核心转发**，由核心居间分配。核心起知识库进程时给它一个本机回环端口（或命名管道）和一次性令牌，再把端点写进客户端进程的启动上下文；客户端直接连。不转发的理由：知识库调用是深在 FineSub 代码里的同步调用、并行窗口会多线程同时发，而现在任务进程的标准输出是事件流、标准输入只送一次请求，借它转发要把 worker 协议改成双向多路复用；直连每次约 0.12 ms，一个任务最多几千次，合计不到 1 s。备选方案「经核心转发」见第 13 节第 4 项。
- **沙箱里的转写进程**：沙箱只改向用户目录和临时目录的环境变量（计划 4.5 第 28 条），不隔离网络，本机回环照常能连；端点由启动上下文给。转写进程只做两次读（`build_asr_context`），不需要写锁。
- **MCP 子进程**：`agent_mcp_server` 由 Agent CLI 拉起，环境是 FineSub 自己拼的 dict（`client.py:2475-2492`、`agent_session_host.py:640-650`，只有 `FINESUB_MCP_*` 和 `PYTHONPATH`），能不能继承纠错进程的其他环境变量取决于各家 CLI。推荐：知识库进程启动时在**自己的** `engines\finesub-knowledge\run\<客户端进程>\endpoint.json` 写端点和令牌；客户端库拿到知识库根路径（`FINESUB_MCP_KNOWLEDGE_ROOT` 本来就传）就按约定找到这个文件。这样 `client.py`、`agent_session_host.py` 不用改。备选见第 14 节第 2 项。
- **知识库包自己的 LLM 调用**：推荐**没有**。知识更新（`update.py`）是嵌在纠错运行里的 LLM 任务：同一个 `RoleClient`、限流器、token 计数器、任务槽预约（`update.py:728-738`：「嵌在纠错运行里时沿用已开的账户，一个任务一份预约」）、同一套交换日志和 `task_artifact_dir`，回放闸门也是在纠错进程的 `chat_complete` 截获它的请求。所以更新的编排（`update.py`、`entries.py`）留在纠错进程、用纠错包的 LLM 设施，只有它读写存储的那些调用经协议。由此知识库包的清单里没有 `llm/client.py` 及以下的 LLM 调用栈（`knowledge.json` 可查），不需要 LLM 密钥和路由。`maintain.py`（另一个调 LLM 的知识库模块）只是命令行，不进任何包。
- **token 计数**：知识库进程里会被执行的计数只有 `style.render_style_block` 用的 `HeuristicTokenCounter`（`style.py:425`，确定性的，不依赖 tokcount）。依赖 tokcount 的 `select_kb_entries`/`render_kb_entry_excerpt` 留在纠错进程，所以 `token_counter_source` 的核对（WP-H）仍只看纠错进程。

### 11.3 每个要改的冻结文件怎么改（只换访问方式）

共 19 个文件：18 个上锁文件加 `stages.py`。通则：被调函数和仓库方法改从协议客户端模块取（客户端是 Nonoka 自己的代码，按「将来要搬进包」写，WP-P1 放进转写包和纠错包）；调用表达式、参数、返回值的用法、`try/except` 的降级一律不动；纯函数和常量（`CATEGORY_SEPARATOR`、`STYLE_CATEGORY`、`term_lines_only`、`_match_normalize` 等）照旧从原模块 import。

1. **`stages.py`**：`build_asr_context` 的 import（`:1736-1742`）里 `DEFAULT_KNOWLEDGE_ROOT`、`load_preinjected_entries`、`match_terms` 改从客户端取。推荐 `render_term_matches`、`term_lines_only` 也从客户端取（远程执行原函数），这样转写进程里一个 `llm/knowledge` 模块都不用（第 1 问末尾：WP-K 后 138 → 122 个模块）。`_validate_llm_configuration`（`:446-463`）的 `resolve_style_keys`、`resolve_subject_keys` 同样换来源；`resolve_style_selection` 不碰存储，不动。
2. **`correction_translation.py`**：模块级 import（`:43-68`）里的 `load_style_catalog`、`resolve_style_keys`、`load_style_entries`、`render_style_block`、`record_style_injected`、`render_subject_block` 换来源；`_generation_pin`（`:1577-1582`）的 `harvest_rendered_edits_at_run_start`、`pinned_generation_rev` 换来源。
3. **`research.py`、`prompt_artifacts.py`、`length_tier.py`、`stages/plan.py`、`stages/prepare_task.py`、`stages/speaker_judge/roster.py`、`stages/correction/{context,serial,parallel,request_turn}.py`**：只换 import 来源（模块级或函数里的惰性 import），调用不动。
4. **`stages/correction/attempts.py`、`run.py`**：信号三处（`_log_window_exposures`、`_shadow_scan_windows`、`_log_landed_windows`）的 `KnowledgeRepo.open(knowledge_root_path(root))` 和 `.store` 换成客户端的仓库句柄，`log_exposed_entries`/`shadow_scan`/`log_landed_windows` 换来源；其余读调用只换来源。
5. **`kb_phonetic_hints.py`**、**`client.py`**、**`agent/agent_mcp_server.py`**：`KnowledgeRepo` 和 `kb_index_block_text`、`knowledge_signals_enabled` 换来源；MCP 服务里一串依赖仓库对象的渲染调用（`render_subject`、`ExactIndex.build` 等）随仓库句柄远程执行。
6. **`knowledge/update.py`、`knowledge/entries.py`**（推荐留在纠错进程）：`KnowledgeRepo.open`、`knowledge_write_lock`、`load_index_texts`、`resolve_*_keys`、`apply_model_proposals` 换成客户端；`_revision_with_proposal` 的原始 SQL 换成具名查询；写锁换成 acquire/release。这两个文件是知识库代码，改它们属于第 0.1 节第 5 条的「纠错那一半读写知识库的模块」。
7. 核心 **`worker.py`**、**`knowledge_worker.py`** 不是冻结文件，WP-K 直接改成调协议。

`QUALITY_LOCK.json` 只变这 18 个上锁文件（`stages.py` 不在锁里）。纠错包里其余 `llm/knowledge` 模块原样作代码副本（纯函数和类定义要用），但它们碰存储的函数在纠错进程里不再被调用；WP-K 的「两半进程不再直接打开知识库目录」测试负责证明（判据见第 13 节第 8 项）。

### 11.4 闸门现有基线覆盖了什么

| 路径 | 覆盖情况 |
|---|---|
| 纠错窗口读条目、预注入、术语匹配、风格块、索引文本（研究、准备任务、开场估算） | **没覆盖**：回放 10 个用例里 9 个 `knowledge: none`（`tests/quality_harness/replay.py:67-168`），唯一的 `sample-stable-knowledge-update` 写进一个**新建的空库** |
| 知识更新写回 | **基本没覆盖**：那个用例的确定性回答是空提案（`replay.py:219-221`「nothing worth writing back」），只覆盖了更新的提示词请求和「新建主体」；回放只比 `final.srt`、`annotated.csv`、`translated.srt`（`replay.py:385-404`），不比知识库写出的内容 |
| 信号（命中、曝光、落地、风格注入）写入 | 没覆盖（不比库） |
| `harvest_rendered_edits_at_run_start` | 没覆盖（空库没有渲染文件） |
| ASR 上下文文本 | **没覆盖**：调用等价里 ASR 上下文恒为空（WP4a 交付说明已指出） |
| 说话人裁判的名册（`roster.py`） | 没覆盖（用例里没有有内容的库） |
| 主体解析与新建 | 部分：调用等价的 `final-srt-auto-read-existing-subject`、`*-update-new-subject` 等记录了传给算法入口的主体名，不比库的内容 |
| MCP 的 `kb_*` 工具、本地 Agent 路由 | 没覆盖（闸门本来就不覆盖 Agent CLI，计划 WP-Q「没覆盖到的地方」） |
| 界面的列表、读、存 | 闸门没有；`tests/test_knowledge_worker.py` 有单元测试 |

### 11.5 要补录的用例（WP-K 在未改动的代码上录，录前经用户批准）

共用一个**有内容的知识库夹具**（建议放 `tests/fixtures/quality/knowledge/`）：一个带别名和若干术语子条目的主播主体、一个带术语的普通主体、一个风格条目、一份手改过的渲染文件；所有比对都去掉时间戳类字段。

| # | 用例 | 跑什么 | 比什么 |
|---|---|---|---|
| K1 | 读库纠错 | 回放：多人样本 stable，`final-srt`，`knowledge: read`，带主体和风格，`judge-text` | 全部 LLM 请求逐字节（覆盖读条目、预注入、术语、风格块、索引、研究、准备任务、名册） |
| K2 | 写回 | 回放：`knowledge: update`，确定性回答给非空提案（新增条目、改条目、风格约定） | LLM 请求；更新后知识库的导出（渲染文件逐字节 + 存储各表去时间戳后的行）；`task-artifacts.jsonl` |
| K3 | 信号 | K1、K2 跑完后 | 信号表的行（去时间戳） |
| K4 | ASR 上下文 | 调用等价：`raw-srt`，`extra_info` 里提到夹具的主体和术语 | 传给 `run_vad_asr` 的 `asr_context` 文本与 `-metadata.json` 里的 `asr_context` 报告 |
| K5 | 界面操作 | `knowledge_worker.handle` 的列表、读、存（含版本冲突、库忙） | 响应与存后的条目文本 |
| K6 | 开跑时收编手改 | K1 的库里有一份手改过的渲染文件 | 收编后的存储导出与 reporter 警告 |
| K7 | MCP 知识库工具 | 进程内直接调 `HarnessToolServer` 的 `kb_index`、`kb_search`、`kb_read`（或对应工具名） | 回复全文 |
| K8 | 任务前新建主体 | `prepare_knowledge_subjects` 在空库和已有同名主体两种情况 | 返回值与存储导出 |

## 12. 550 个上锁文件的归属

`coverage.json`：转写包 128 个、纠错包 465 个、知识库包 340 个，出现在两个以上包里的 385 个，**没归进任何包的 40 个**，逐个原因：

| 文件 | 为什么三个包都不需要 |
|---|---|
| `batch_state.py`、`scheduler.py` | FineSub 的批处理命令行（只有解锁的 `pipeline.py` 和 `scheduler.py` 自己 import；`finesub_bootstrap/shell.py:202` 的启动器）。见第 4 问 |
| `llm/agent/agent_cleanup.py`、`agent_join.py`、`agent_ping.py`、`agent_task_control.py` | FineSub 自己的安装前端 `finesub_bootstrap/shell.py:238-346` 以 `-m` 启动的命令行；Nonoka 不用。`agent_task_control` 另被 `agent_transports.py:178` 的对话式引导写进给 Agent 的命令，但那条路（`conversational_bootstrap`）从任何入口都到不了 |
| `llm/knowledge/__main__.py`、`maintain.py`、`report.py`、`verify.py`、`snapshot.py`、`node/candidates.py`、`node/draft.py`、`node/history.py`、`node/migrate.py`、`node/phase_b.py`、`node/repair.py`、`node/scan.py` | 知识库的维护命令行（`python -m finesub.llm.knowledge …`）及只有它用的模块；Nonoka 的界面只有列表、读、存 |
| `llm/knowledge/share/`（14 个） | 知识库共享的命令行与服务（`python -m finesub.llm.knowledge.share`，`shell.py:294`）；Nonoka 没有这个功能 |
| `workflows/__init__.py`、`workflows/reference_ingest.py` | 参考资料导入工作流，只由 `scheduler.py` 调 |
| `speech/recognition/cli/`（3 个） | 识别的单独命令行 |
| `subtitles/alignment.py`、`subtitles/compliance.py`、`llm/session_contract.py`、`llm/token_measure.py` | 树里没有任何地方 import（只在文档字符串里被提到） |

另外两个**解锁**文件：`stages.py` 两个包都带；`pipeline.py` 不进任何包（第 4 问）。`finesub_bootstrap/` 整个进转写包。

## 13. 需要用户定的点（带推荐）

1. **知识库代码在纠错包里的副本**：纠错那一半的上锁文件在模块级 import 了 `llm/knowledge` 里的纯函数、常量和类（`CATEGORY_SEPARATOR`、`STYLE_CATEGORY`、`StyleCatalog`、`LineGate`、`_match_normalize`……），不改这些 import 就得带上那 28 个知识库模块的代码，它们只是不再碰数据。计划 4.6 第 35a 条写「知识库包持有……全部知识库代码」。**推荐**：理解为「只有知识库包**执行**碰数据的知识库代码」，纠错包带原样副本，由测试证明纠错进程不打开知识库。另一条路是把这些纯 import 也改掉，等于改大半个纠错层，超出第 0.1 节第 5 条。
2. **`pipeline.py`、`batch_state.py`、`scheduler.py` 以及第 12 节的 37 个上锁文件不进任何包**：**推荐**不带（FineSub 的命令行与死代码，Nonoka 不用；需要时从 git 历史取回）；`tests/test_split_stage_contract.py:201` 对 `build_parser` 的那条断言随 WP-P1 删。另一条路是把命令行与维护工具放进知识库包或转写包备用，但它们要两半在同一进程，放哪个包都跑不起来。
3. **知识更新的 LLM 调用跑在哪**：**推荐**留在纠错进程、用纠错包的 LLM 设施，只有存储读写走协议（11.2）。另一条路是整个更新放进知识库包跑，知识库包就要带完整的 LLM 栈、密钥和路由，任务槽预约从「一个任务一份」变成两份，回放闸门也截不到它的请求。
4. **包进程之间的连接**：**推荐**核心分配端点、包进程直连（11.2）。另一条路是全部经核心转发（核心是唯一的枢纽，审计面更小），要把 worker 协议改成双向多路复用。
5. **知识库进程的拓扑**：**推荐**每个转写或纠错进程配一个（保住写锁、快照、一次性警告这些进程级语义，每任务约多 0.5 s）。另一条路是全局一个常驻服务，要改 `base.py` 的写锁实现，超出批准范围。
6. **原始字幕谁渲染**：默认路径（FineSub 转写 + FineSub 纠错）**推荐**仍由纠错包的 `run_post_stable` 写 `raw.srt`（与基线逐字节）；核心的渲染器只在纠错包不在或换了转写引擎时用。核心渲染的 `raw.srt` 与 FineSub 的（后处理、空行、断行）不保证字节相同；如果希望两者相同，WP-P1 要另做一份等价测试。
7. **知识库包的兼容包**：**推荐**单独一个，名字正好 14 个（同一份 Nonoka 源码生成，各包「名字不多不少」）。另一条路是直接复用纠错包那份（32 个名字），违反「不多不少」的规则。
8. **WP-K「两半进程不再直接打开知识库目录」测试的判据**：纠错进程仍会拿着知识库路径字符串并 `Path.resolve()` 它（`finesub/paths.py` 的 `resolve_knowledge_root`，上锁、不属于知识库访问点）。**推荐**判据是「没有打开知识库目录下的任何文件、没有 `sqlite3.connect`」（审计钩子看 `open` 与 `sqlite3.connect` 事件），路径解析不算。
9. **GPU 档位选项**：**推荐**写成转写包 `PACKAGE.json` 的静态声明，加一个测试对照原函数；另一条路是每次起进程查询。

## 14. 交回的问题（可能要改冻结文件、或在批准范围边上）

1. **`knowledge/update.py`、`knowledge/entries.py` 要改**（11.3 第 6 条）：它们是知识库代码本身。按推荐设计（更新的编排留在纠错进程）这两个文件里的存储调用就是纠错那一半的访问点，属于第 0.1 节第 5 条的「纠错那一半读写知识库的模块」。请用户确认这个理解；如果不认，只能选第 13 节第 3 项的另一条路。
2. **MCP 子进程找端点**：推荐方案（11.2 端点文件）不需要改 `client.py:2475-2492`、`agent_session_host.py:640-650`。如果改用「往 MCP 环境里加一个端点变量」，就要改这两个上锁文件里拼环境的地方：这算不算「访问方式」，需要用户定。
3. **写锁与 rev 快照的语义**：只有选「每个客户端进程配一个知识库进程」时才不用改 `base.py:818-880`（写锁的进程内共享）和 `:322-382`（快照 pin）；选全局服务就必须改它们，那不在批准范围内。

没有发现其他必须改冻结文件的地方：第 1–9 问的结论都能靠适配器、桥接、核心和清单完成。

## 15. 可能随 WP4c 变化的结论

- 第 5 问表里 `worker.py` 的各行（行号、`run_pipeline` 一行会消失、`prepare_knowledge_subjects` 等的归属不变但调用位置会移动）。
- 第 7 问第 4 条、第 9 问的日志与进度：WP4c 在单进程里怎么合并两段的 `planned` 和去重「已有结果，跳过」，WP-P1 就把同样的做法搬到核心。
- 第 1 问的入口：WP4c 之后 worker 不再 import `finesub.stages`，`worker.py` 里的几个入口会转移到两个适配器里，清单内容不变（入口符号不变，只是 `used_at` 变）。重跑脚本即可。
