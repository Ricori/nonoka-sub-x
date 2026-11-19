# 转写引擎重构：已完成的工作包（归档）

从 `docs/transcription-engines-plan.md` 移出来的内容（2026-11-18）：第 6 节里已完成工作包的交付摘要与原始说明，以及附录 A。计划正文里每个包只留标题；问题与答复仍在计划的附录 B。这里是历史记录，写的时候的文件路径（如 `third_party/finesub/`）可能已经不存在。

## 整理前的状态行（2026-11-18 之前，逐次追加）

> 状态（2026-11-11）：WP-Q、WP-A、WP0、WP-R0 已完成并合进 main；WP-C 的 harness 已合进 main，云端基线还没录成（见 WP-C）；编译加速已在 `85ae2b9` 关闭；WP-R2、WP-B、WP-R1、WP1、WP4a、WP4b、WP4c、WP3a/b/c、WP-C、WP-P0、WP-M1（最后一条完成标准除外）、WP-R4、WP-K、WP-E1、WP-CL0、WP5、WP-E2、WP-E3、WP-R5、WP-R6、WP6、WP-R7、WP-P1 已完成并合进 main（WP-CL0 的预热脚本合进云端仓库的 main；各包按附录 B 的答复调整）；2026-11-12 用户决定知识库独立成第三个包、全部经协议访问（第 0.1 节第 5 条、4.6 第 35a 条、WP-K）；第五批（WP5、WP-E2、WP-E3，2026-11-13）已全部完成；第六批（附录 B 第 69 问）的 WP-R5、WP-R6 与 WP6 第一阶段已于 2026-11-16 合进 main；用户 2026-11-13 决定去掉「运行环境」页、引擎与环境都放进设置页（4.9 第 39 条，附录 B 第 73 问），这部分界面已作为 WP6 第二阶段完成（2026-11-16），依赖 WP-P1 / WP-H 的部分留给 WP-UI；第七批只开 WP-R7（2026-11-16，附录 B 第 75 问；`--cloud` 花费批准上限 $1.5，第 76 问），已于 2026-11-17 合进两个仓库的 main（`--cloud` 共花 $0.448），第八批只开 WP-P1（2026-11-17，附录 B 第 80 问，前提满足的只有它），已于 2026-11-18 合进两个仓库的 main（vendor 已删，FineSub 在 `engines/` 下的三个包里；云端在 WP-CL 之前只改了挂载、没实测），第九批开 WP-H 与 WP-L 并行、WP-CL 等 WP-L 合并后作为第十批（2026-11-18，附录 B 第 83 问），WP7 留到 WP-P1、WP-H 之后、WP8 之前；WP-M1 的最后一条完成标准先不做（第 70 问）；三层已传上 R2（经代理的下载校验和速率限制规则用户决定先不做，见 WP-E2）；2026-11-13 用户决定模型层不再按单文件放 R2，迁移时模型补齐改为下整个模型层压缩包（附录 B 第 63、64 问）；其余未开工。本文档是交给子代理执行的工作说明，每个工作包可以单独领走。

## 第 6 节：已完成的工作包

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

### WP6 桌面界面与 Go（已完成 2026-11-16，含第二阶段设置页重组）

> 第二阶段交付（按 4.9 第 39 条 2026-11-13 的改法，问题与答复见附录 B 第 74 问）：设置页改成一页长滚动——转写引擎 / 运行环境 / 纠错与翻译 / 存储与网络；运行环境页删掉（`RuntimePage` 搬成 `components/RuntimeSection`），导航去掉「运行环境」，原来跳运行环境页的地方改成跳设置页对应段。转写引擎段：`Capabilities.transcription_engines` 加上合成的「FineSub（云端）」单选，每项显示就绪、运行位置、音频去向、计费；选中云 API 引擎时展开它的密钥（`transcription_engines[].keys` 与设置快照新增的 `engineKeys`，原 12 个 `keys` 不变、不派生给 FineSub；Gemini 的 `GEMINI_FREE` 与纠错共用）；当前引擎不在列表里时提示重选、不静默回落。当前引擎存在 Go 桌面偏好 `preferences.json` 的 `transcriptionEngine`，缺省或非法值回到 `finesub`。纠错与翻译段：「当前纠错引擎：finesub correction」卡片，下面是原样的模型配置。新建任务对话框去掉本机 / 云端开关，只显示当前引擎和「更换」；选项按引擎藏（设备 / 显卡档位只给本机 GPU 引擎，人声分离按 `features.separate`，说话人档与语言按声明）；未就绪的引擎可选，但新建任务被拦、指回设置页；日文轴无论选哪个引擎都走本机 FineSub；选 FineSub 时请求与第一阶段逐字节相同（有测试）。运行环境段加「完整核对」（`repair`）和「清理未使用的」（新路由 `DELETE /v1/runtime/provision/unused`、Go `RemoveUnusedRuntime`）；卸载视频工具的死代码（前端、Go `RemoveRuntimeGroup`、sidecar 路由、`remove_tool_group`）删掉；numba 编译缓存纳入「清理缓存」（`finesub_bridge.COMPILE_CACHES`）。`docs/provider-spec.md` 跟上。预览里用假后端走过一遍（四段显示、配假密钥后就绪、清理两步确认、完整核对、对话框按引擎藏选项、FineSub 的请求、云端未登录与引擎被移除时拦下、窄窗口）。主会话复核：pytest 935 通过，`go test ./...`、前端 `typecheck` 与 160 个测试通过，默认闸门通过（Python 侧没动派生，没跑 `--real`），没重录基线。交回 WP-UI：见 WP-UI 一节开头的说明。下面是第一阶段的交付和原始说明。

> 第一阶段交付（管线）：前端 `providers/types.ts`（`Capabilities.transcription_engines` / `correction_engines`、`TaskRequest.transcription`、`TaskSnapshot.transcription_engine`）、`home/defaultRequest.ts`（本机带 `{"engine":"finesub","options":{}}`，云端带 `finesub-cloud`）、`app/format.ts` 与 `pages/TasksPage.tsx`（任务行显示「本机 · FineSub」「☁ 云端 · FineSub」，旧快照按 provider 推断）、`local_provider.py`（任务快照加 `transcription_engine`）、`docs/provider-spec.md`（`transcription` 的校验与丢字段日志、快照字段、三个新错误码）。Go 不改代码：任务历史原样存，云端 relay 不转发 `transcription`，加了两条测试证明。清单外的文件经用户批准（附录 B 第 73 问）。本机 FineSub 请求去掉 `transcription` 后与改前逐字节相同（有测试）。主会话复核：与 WP-R5、WP-R6 一起试合并，pytest 935 通过，`go test ./...`、前端 `typecheck` 与 156 个测试通过，闸门含 `--real` 通过，没重录基线。没做：遥测记引擎（要改服务端格式）；插件发起的任务不带 `transcription`，按缺省 FineSub 跑。下面是原始说明。

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

> 交付：`runtime/layers.py`（读 `layers.json` 并核对；装一层：清单 → 路径深度 → 磁盘空间 → 压缩包（离线包优先、从不删；否则断点续传、走 Nonoka 的代理设置）→ 系统 `tar.exe` 解到 `<版本>.unpack\`（保留修改时间）→ 按清单核对 → 改名 → 才切 `current.json` → 删下载的压缩包 → 没有未结束任务就删旧版本；被杀在每一步后都能续上；就绪 `ready/missing/outdated/damaged`，快速检查单趟扫描，真 Python 层 45563 个文件 0.46 s；`repair`；`unused()`/`remove_unused()`/`overview()`，删之前先改名到 `.retired`、被占用就整份留下；迁移用的 `complete_models()`），`provision.py` 重写在层上（对外状态结构不变，新增 `layers`、`unused_layers`、`unused_bytes`、`repair` 目标、`install_path_too_deep`、`runtime_in_use`；不建 venv、不调 uv、不从 HF 下载），`runtime/__init__.py` 门面（WP5 要的 `status()`、`tool_path()` 保住；`model_dir` 从模型层答），`runtime_install.py`、`model_install.py`，迁移钩子 `ModelCompletionHook`、`LayerDownloadHook` 登记进 `migration/hooks.py`；harness 只改定位（`real.py` 的 `provisioner_for`、`NONOKA_QUALITY_LAYERS_INDEX`，`pins.py` 量模型层与工具层）。`model_specs.py`、`msvc_runtime.py` 没改。`test_finesub_boundary.py` 去掉三条 WP-R3 过渡项。按附录 B 第 66、67 问的答复做。实测：从本地服务装真实三层约 3.6 分钟；从 R2 直连装真实三层（工具 55 s、Python 442 s、模型 331 s），完整核对全部就绪、下载缓存为空，`--real` 通过；整个大文件根搬到另一个盘、带空格的路径，不修补，就绪与 `--real` 都通过；真实三层最长相对路径 python 144、models 145 字符，默认位置算下来约 232。主会话复核：与 WP-E2 一起试合并，pytest 925 通过，闸门质量锁、钉住内容（installed runtime matches）、调用等价、环境、LLM 回放、知识库、`--real`（7 个逐字节、8 个按认可标准）通过，没重录基线；`--layers` 只报跑过 `--real` 后 Python 层多出的 46 个 numba 缓存文件，模型层与工具层干净。小改进（不挡合并）：层的下载走 `runtime.fetch.download_file`，没带 `Accept-Encoding: identity`，现在靠 httpx 自动解 gzip 才对，以后补上更稳。交回：WP-R5 / WP-H——`NUMBA_CACHE_DIR`；`local_provider.py` 的 `_prepare_msvc_environment`、`_clear_legacy_separator_decode_probes` 随「环境从零构建」删掉；`worker_environment` 仍是父环境加层路径。WP-R6——运行环境页文案（前端仍写「FineSub bootstrap 不可用」）、`remove_tool_group` 按钮（视频工具已并入工具层，不再单独卸载）、新的 `repair` 目标与字段；`download_route.py` 里 `offline_snapshot`、`offline_python`、`ensure_fast_enough`、`stage_environment`、`SpeedWatch` 已无调用方（WP-R6 更正：`stage_environment`、`SpeedWatch` 仍有调用方，留着；其余已删）；Go 传给 sidecar 的 `--install-dir` 现在当作大文件根。WP-UI——「清理未使用的」用 `unused_layers()`/`remove_unused_layers()` 或门面 `layer_overview()`，sidecar 还没有对应路由。WP-M2——两个钩子已登记，迁移命令现在会真做模型补齐、下 Python 层和工具层。macOS 没有工具层，运行环境页在 mac 上显示不支持。下面是原始说明。

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

### WP-R5 FineSub 桥接（已完成 2026-11-16）

> 交付：`finesub_bridge.py` 按进程派生（附录 B 第 71 问，8 条都是推荐项）：核心在拉起 worker 前组一份中性启动上下文，桥接分转写、纠错两份——转写只给模型目录变量、两个加速开关、`NUMBA_CACHE_DIR=<大文件根>\cache\finesub-transcribe\numba`、`HF_TOKEN`（配了才有）、`FINESUB_STATE_FILE`，不写文件；纠错在 `engines\finesub-correct\run\<pid>-<随机>\` 写 `model_catalog.toml`、`finesub.toml`（有内容才写）、`.env`（只在改过 `GEMINI_BASE_URL` 时写这一行），LLM 与检索密钥、`NONOKA_CLOUD_KEY` 走进程变量，`FINESUB_ENV_PROTECT=0`，`FINESUB_KNOWLEDGE_ROOT`、`FINESUB_STATE_FILE` 无条件写（位置等 WP-M2）。task worker 两份合用；run 目录持锁、进程结束即删，sidecar 启动时清残留；继承来的 `FINESUB_*` 一律去掉。sidecar 自己只钉住 FineSub 读打包默认值，并删掉 `engines\finesub-shared\`。`settings.py` 不再写派生文件，`config_file.py` 删除，`provision.worker_environment` 不再设模型变量；`local_provider.py`（用户批准）删 `_prepare_msvc_environment`、`_clear_legacy_separator_decode_probes`，加 `_engine_launch`。WP-R2 搬来的运行时补丁原样保留。harness：`environment.py` 扩大比对范围（13 个密钥名、6 个 Base URL 名、`NUMBA_CACHE_DIR`、`FINESUB_ENV_PROTECT`）。环境基线单独重录（`23d59c4`），差异：`.env` 不再有（密钥改成进程变量，值不变）、新增 `FINESUB_ENV_PROTECT=0` 与 `NUMBA_CACHE_DIR`、`UV_CACHE_DIR` 随 uv 消失；`finesub.toml`、`model_catalog.toml` 内容不变。跑 `--real` 后 Python 层不再新增 numba 缓存。主会话复核：与 WP-R6、WP6 一起试合并，pytest 935 通过，闸门含 `--real` 通过，基线差异与说明一致、里面只有闸门的假密钥。行为上的小变化：任务进行中改密钥，正在跑的 task worker 看不到新值（LLM worker 照旧保存后重启）；外部的 `FINESUB_*` 不再漏进引擎。交回：WP-P1——`_engine_launch` 里对桥接的调用挪进包进程，桥接仍 import `download_route`、`runtime.fetch`、`runtime.fsops`，sidecar 进程内仍 import FineSub；WP-H——环境仍以父环境为底，白名单、`-I -B`、沙箱、`HF_HUB_OFFLINE=1` 没做；WP-M2——搬知识库与限流状态时改两份上下文里的 `knowledge_root`、`state_file`；WP-UI——numba 缓存目录要不要进「清理」。下面是原始说明。

- 文件：`finesub_bridge.py`、`tests/test_finesub_bridge.py`；`provision.worker_environment` 和 `settings.bind_environment` 里与 FineSub 有关的部分收进来（含 `85ae2b9` 加的两个加速开关）。
- 内容：按 4.3 第 22、23 条，每次启动 worker 时从 Nonoka 的配置、密钥、目录（层的当前版本目录）派生 `finesub.toml`、模型目录文件和全部 `FINESUB_*` 环境变量；派生文件放 `engines\<包>\run\<进程标识>\`；密钥只走环境变量。按 4.6 第 35 条写成将来要搬进两个包的样子：转写那一份和纠错那一份分开（各自只派生自己要的东西）。
- 完成标准：
  - 闸门的调用等价通过；派生出的 `finesub.toml`（含 `[nonoka_models]`，与基线同字节）和模型目录文件的内容与 WP-Q 基线一致。
  - **环境基线重录**（第 0.1 节第 4 条，D19，用户已批准）：**单独提交**，交付说明里逐项列出新旧基线的差异并说明每一项为什么是交付方式的变化而不是引擎行为的变化。只重录 `environment`，其余基线不动。
  - 迁移前后对照：同一份旧配置，迁移前引擎读到的与迁移后派生出的语义相同（只对 Nonoka 认识的键）。
  - `src/nonoka_x/` 里只有本文件和两个适配器知道 `FINESUB_*`（WP0 的边界测试，白名单变短）。
  - `quality_gate.py --real` 通过。

### WP-R6 桌面、脚本、离线包与文档（已完成 2026-11-16）

> 交付：启动器 Python 的下载去掉所有镜像与 R2 兜底，阶梯为「离线包 → 代理 → 直连 → 报错」（`python_bootstrap.go`、`download_route.go`）；`runtimeName` = `runtime`（`locations.go`），搬迁本来就整目录搬、没改；桌面端自己找 ffmpeg / ffprobe / tokcount / yt-dlp / PO Token 改到 `layers\tools\<版本>\<工具 id>\`，版本取 `current.json`、不核对是不是这个版本要的（`managedtools/resolve.go`）；预估占用 16 → 14 GiB；离线包＝三层（`v1/<层>/<sha256>.json|.tar.zst`，与 R2 同路径，层管理器原地读）加启动器三样，默认按 `layers.json` 从 R2 下、`--layers` 从本机构建复制，都按索引核大小与 sha256，`pack.json` schema 2；`publish-github-mirror.mjs` 删除；运行环境页、存储卡片的文案按层改（「运行环境层不可用」、macOS 没有预制层、层损坏时显示「修复」、删视频工具卸载按钮），类型加 `repair` 与三个层字段；`download_route.py` 删 `offline_python`、`offline_snapshot`、`ensure_fast_enough` 与 4 个测速测试（`stage_environment`、`SpeedWatch` 仍有调用方，留着）；`docs/architecture.md` 4.3、`docs/engine.md` 3.4、`LOCAL_CHANGES.md` 一句。计划外的文件与两项非推荐选择见附录 B 第 72 问。实测：离线包从 R2 直连构建 626 秒，21 个文件 5.77 GiB，层对象与 R2 同版本逐字节相同；进程级断网（审计钩子拒绝一切连接与名字解析）从离线包装三层 112 秒、零次联网尝试，完整核对三层就绪；`--layers` 对离线装好的三层通过；`--real` 在这份层上通过。测试用的约 16 GB 文件按用户选择做完就删了。没做到：断网是进程级的不是整机；Go 启动器的离线安装只由单元测试覆盖；没给 `--exe`（本机没有桌面 exe）。主会话复核：与 WP-R5、WP6 一起试合并，pytest 935 通过，`go test ./...`、前端测试通过，闸门含 `--real` 通过。交回：WP-UI——见 WP-UI 一节的说明；WP-R7——删自下载后 `download_route.offline_file`、`_copy_into_place` 和离线包 `files/<sha>/` 里除 uv 以外的用途会消失；WP-M2 / 发版——老用户的大文件根从 `finesub` 变成 `runtime` 靠迁移改名，迁移接进启动之前没迁移的数据目录在新版里看起来是「没装」；macOS 没有工具层，`managedtools` 在 mac 上只能用系统工具。下面是原始说明。

- 文件：`desktop/scripts/resource-sources.mjs`、`build-offline-package.mjs`、`publish-github-mirror.mjs`（删除）、`desktop/internal/app/python_bootstrap.go` 及其测试、`desktop/internal/storage/locations.go`、「运行环境」页与设置页相关前端文件、`docs/architecture.md` 4.3 节。
- 内容：
  - 离线包改成装 `layers.json` 列的层文件（压缩包加清单）；`resource-sources.mjs` 改读 Nonoka 的清单与 `layers.json`。
  - 删掉 GitHub 镜像脚本和界面里的国内镜像、下载线路选项（4.2 第 18 条）。
  - 大文件根改名 `runtime`（Go 的常量 `runtimeName`，`locations.go:30`）；「存储位置搬迁」整体搬 `runtime\`，不需要任何修补（层不含绝对路径）。
  - 界面文案里「FineSub 运行时」之类的说法按新归属改；Go 侧如果有读 FineSub 清单或环境变量的地方一并改。
- 完成标准：离线包能构建，其中的层文件与 R2 上同版本的逐字节相同；用离线包在断网的机器（或断网的虚拟机）上装好三层、`--real` 通过；`go test ./...`、前端测试通过；仓库里搜不到 `publish-github-mirror`。

### WP-R7 删自下载，vendor 只留核心文件（已完成 2026-11-17）

> 交付（问题与答复见附录 B 第 77–79 问）：vendor 删 `pyproject.toml`、`VERSION`、`README.md`、`UPSTREAM_README.md`，留 `LICENSE`、`LICENSES.json`、`LOCAL_CHANGES.md`、`PROMPT_LICENSE.md`、`QUALITY_LOCK.json`、`UPSTREAM.json`、`src/`；没了 `pyproject.toml`，`finesub.paths._is_checkout_root` 为假，worker 早已设 `FINESUB_CHECKOUT_DATA=0`，行为不变。引擎不再自己下载（上锁的 550 个文件没动，改的两个不在锁里）：`model_ensure.py` 新增 `ModelUnavailable(VerificationMismatch)`——用子类是因为上锁的 `hf_weights.prepare` 只放过这一种异常，别的吞掉交给加载器懒下载；模型缺失、残缺或标记为 failed / stale 时抛它，消息写明哪个模型、哪个目录、什么原因，并提示在设置里安装或修复模型层，不写标记、不删文件、不联网；删 `_download`、下载子进程 `main` / `__main__`、`_token_for`、`OFFICIAL_HF_HOST`、`verify_downloaded`；模型齐全时走原来的快路径。`model_fetch.py` 删 `fetch_with_fallback`、`_fetch_unverified`，分离器的 `fetch_fixed_files` 只校验盖戳，缺文件或校验不过抛 `ModelUnavailable`。`LOCAL_CHANGES.md` 第 0012 节写明还原方法。死代码：`finesub_bridge.py` 删下载阶梯和云端摘要源，只留 `resolve_region` 钉成 `global`（上锁的 `place_separator_files` 总会先调它，原函数会联网探测）和代理策略；`download_route.py` 只剩代理部分；删 `asset_digest.py`、`tests/test_asset_digest.py`、`tests/test_hf_mirror_transfer.py`。桌面暂存（`desktop/build/tools/stage`）加 `-python`，版本必须等于运行时清单钉住的 3.12.1，只用 `compileall --invalidation-mode unchecked-hash -d third_party/finesub/src` 预编译引擎代码，`main_test.go` 证明两次暂存逐字节相同、只编引擎、版本不对时拒绝；`Taskfile.yml` 去掉 pyproject / VERSION、补上 `src/finesub_knowledge`；`release.yml` 加 `setup-python 3.12.1`。新测试 `tests/test_engine_no_self_download.py`（22 项，审计钩子记录一切连接、域名解析和子进程）：齐全时快路径不变；五种损坏都报明确错误、零记录、目录不变；真实清单里每个模型目录删掉后 `hf_weights.prepare` 与 `prepare_directory` 都报明确错误、零网络请求；下载子进程已不存在；分离器文件三种情况；钩子自检。文档：`docs/engine.md` 目录树、`docs/engine-packages.md`、`scripts/engine_packages.py`。云端仓库只改 `model_prime.py` 注释（Vocal 容器用烤进镜像的分离器，HF 模型由 WP-CL0 预热）。子会话的 `--cloud` 起了三次：L4 跑一半用户叫停；A10 不过（sample_ja text_distance 0.0109 > 0.005，sample_multi_person 0.0298、coverage 0.0032 > 0.002，基线在 L4 上录，跨卡本来不可比）；L4 第二次通过、各项与基线差值为 0，耗时 3920 秒；合计 $0.448，三个临时应用已停，没碰生产 Volume 与生产应用。主会话复核：两个仓库在 main 上试合并，pytest 916 通过 1 跳过，`go test ./...`（暂存预编译测试在 3.12.1 上实跑）、前端 typecheck 与 160 个测试、云端仓库 156 个测试通过，默认闸门与 `--real`（7 个逐字节、8 个按认可标准）通过，`--cloud` 没重跑；没重录基线。交回：WP-P1——`src/nonoka_x/cloud_route.py` 已没有生产调用方，可以删；WP-H——`HF_HUB_OFFLINE=1` 仍只有闸门 `--real` 设；WP-UI——nemotron 说话人后端在模型缺失时仍会先打「首次使用：正在下载…」再报错，文件上锁，只能在界面或日志层面处理；发版——在 mac 上本地构建要自己装 3.12.1（`release.yml` 两个平台都装了）；`docs/engine-packages/*.json` 本来就是冻结快照、与脚本不同步，没重新生成；一次 L4 `--cloud` 约 65 分钟，基线绑在 L4 上不能换卡。下面是原始说明。

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

### WP-P1 分装成两个引擎包（已完成 2026-11-18，实为三个包）

> 交付（问题与答复见附录 B 第 81、82 问；分两阶段在同一分支上提交：先拆进程、再分装，各过一次闸门）：三个包 `engines/finesub-transcribe/`（Python 环境层的解释器）、`engines/finesub-correct/`、`engines/finesub-knowledge/`（启动器的解释器），各有 `PACKAGE.json`（`paths` 为 `adapter`、`src`）、`adapter/`（包自己的代码）、`src/`（FineSub 文件）、许可文件、`UPSTREAM.json`、自己的 `QUALITY_LOCK.json`、`LOCAL_CHANGES.md`（新增 0013 节），没有 `pyproject.toml`；`.gitattributes` 改成 `engines/*/src/** -text`。文件先 `git mv` 进第一个要它的包，其余包放逐字节相同的副本：转写包 115 个上锁文件加 `stages.py` 与 `finesub_bootstrap` 原件 42 个，纠错包 463 个加 `stages.py` 与兼容包，知识库包 344 个加兼容包；分包前上锁的 550 个里 510 个进了包，40 个和 `pipeline.py` 不带，逐个理由在 `docs/engine-packages/not-carried.json`，分包前的锁存为 `docs/engine-packages/vendor-quality-lock.json`。清单（`scripts/engine_packages.py`）改从各包 adapter 自己的 import 读出，`--import-check` 在每个包自己的路径上跑（纠错 192、知识库 68 个模块，无 torch / numpy）。兼容包按名单从原件逐段复制，`__all__` 恰为 WP-P0 名单（纠错 32 个名字、知识库 14 个），复制来的代码还要的公开名字保留原名并列在 `tests/test_engine_packages.py` 的 `COMPAT_HELPERS`；`paths` 的位置取自启动上下文的 `paths.package_data`（`<数据目录>\engines\<包>\`，D21），未绑定就报错、不回落 `%LOCALAPPDATA%\FineSub`。上锁引擎用到的 `finesub_bootstrap` 名字从 48 个变成 45 个，`tests/fixtures/quality/bootstrap-surface.json` 删了这 3 个（唯一改动的闸门文件，第 82 问）。核心：sidecar 里要问 FineSub 的事交给纠错包的常驻进程（原 LLM worker 扩大职责），就绪按 `PACKAGE.json` 的声明由核心判断；「要不要知识库」由纠错包回答，provider 与 worker 调同一个判断；FineSub 产物进 `tasks\<id>\native\<包>\`，harness 比对时映射回基线的 `workspace\`（基线不动）；没装纠错包时核心用 `nonoka_x/transcription/srt.py` 从 `transcript.json` 渲染 `raw.srt`，装了时照旧由纠错包写；共用桥接的代理策略拆成两半各钉本包有的；`cloud_route.py` 删除。桌面：暂存工具暂存三个包并用 3.12.1 `unchecked-hash` 预编译每个包的 `adapter/`、`src/`；`runtime.go` 的 `NONOKA_VENDOR_DIR` → `NONOKA_ENGINES_DIR`、sidecar `--vendor` → `--engines`，目录不在也能启动；顺带修好阶段一后暂存工具还列着已搬走的 `src/finesub_knowledge`。闸门 harness：`union.py` 按名字排包的先后，`lock.py` 按包核对并比对分包前的锁，`--real` 的纠错与知识库进程用新变量 `NONOKA_QUALITY_LAUNCHER_PYTHON` 指定的启动器（主检出 `.tmp\launcher-py3.12\launcher\Scripts\python.exe`，照 `python_bootstrap.go` 用 uv 建，实际解析出 3.12.13 而不是 3.12.9），`NONOKA_QUALITY_PYTHON` 不再生效。云端仓库：`images.py` 三处挂载改成 `engines/finesub-transcribe`（挂载点与 `PYTHONPATH` 不变），`phases.py` 把档位名单传给 `resolve_request_gpu_tier`，测试跟上；没跑 `--cloud`。新测试 `tests/test_engine_packages.py`（18 项，含包形状、550 个去向、兼容包名单、互相 import 不到、三种可删性、包私有目录之外没有 FineSub 格式文件）。主会话复核：两个仓库在 main 上试合并；另写脚本核对 510 个进包文件的 sha256 全部等于分包前的锁、40 个不带的都有理由、转写包的 `finesub_bootstrap` 原件 42 个与 main 逐字节相同；pytest 926 通过 1 跳过，`go test ./...`、前端 typecheck 与 160 个测试、云端 156 个测试通过；默认闸门与 `--real`（7 个逐字节、8 个按认可标准）通过；云端代码 import 的 FineSub 模块在只有转写包与核心的路径上都能 import（静态核对，不是实跑）。交回：WP-H——`HF_HUB_OFFLINE=1` 仍只有闸门 `--real` 设；纠错进程的启动器是 3.12.13，idna、typing-inspection、tzdata 版本与层里不同；兼容包的 `secrets` / `token_counter` 由 WP-H 的 `token_counter_source` 核对。WP-L——`transcript.json` 还没有说话人，核心渲染的 `raw.srt` 不带说话人。WP-CL——云端只改了挂载和档位名单、没实测；云端 `PYTHONPATH` 没有包的 `adapter/`，以后给 ASR 传 extra info 前要加 `/opt/finesub/adapter`；worker 的 `CLOUD_NATIVE` 键还在。WP-M2——状态文件和知识库仍在主数据目录；兼容包的 `user-data`、`agent-capsules` 已落到 `engines\finesub-correct\`，旧的 `%LOCALAPPDATA%\FineSub` 不搬不碰（D8）。WP-UI——没装知识库包时 FineSub 纠错的 `--style auto` 会跑到一半失败，要不要在就绪里拦由用户定；没装纠错包时云端续跑明确报错；nemotron 缺模型时仍先打「首次使用：正在下载…」。WP7 / 其他——`scripts/engine_packages_real_record.py` 要重跑得先改成每个进程各记一份；代理改为启动时解析一次；`desktop/examples/plugins/clip-picker/docs/llm-engine.md` 和几处来源注释还写着 `third_party/finesub`。下面是原始说明（写「两个包」的地方按三个包理解）。

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

### WP-CL0 云端模型预热脚本（已完成 2026-11-13）

> 交付：云端仓库 `modal_compute/nonoka_x_compute/operations/model_prime.py`（只读 Nonoka 的模型清单，不 import finesub、finesub_bootstrap；缓存布局与 `.finesub-verified.json` 标记的格式和引擎相同，测试把两边对上）与 `prime.py` 的 `check_models`（Volume 只读挂载，只检查）、`prime_models`（补齐缺的）、`prime_diarization_models`（改为只补说话人分离的两个）；`NONOKA_X_PRIME_VOLUME` 把它们指向另一个已存在的 Volume；`modal_compute/docs/compute.md` 更新。按附录 B 第 56 问：只放云端实际加载的 4 个（whisper、qwen-referee、nemotron3、speaker-embedding，分离器烤在 Vocal 镜像里）；和引擎用同一个库、同样的参数下载，脚本自己逐文件校验；已在但校验不过就报错、不动文件；没有哈希的小文件只看在不在；只有钉住的快照整个不在时才下载，下载后校验不过照引擎的规矩写失败标记。本仓库 `tests/quality_harness/cloud.py` 加测量用变量 `NONOKA_QUALITY_CLOUD_MODELS_VOLUME`（不设时行为不变，第 57 问）。实测：生产 Volume 只读检查 4 个全部在、校验通过，没有改动；空的临时 Volume 预热约 1 分钟放齐，之后 `--cloud` 通过、各项统计为 0，前后 Volume 上 149 项完全相同，日志里没有下载；云端测试 156 个通过。花费：已出账约 $0.28，第二次闸门那一小时未出账，估计合计 $0.33–0.43（用户批过中途重跑）。临时 Volume `nonoka-x-models-wpcl0-tmp` 经用户同意已删。交回：生产 Volume 上还有 6 个没人读的旧分离器文件，没动，清不清理由用户定。下面是原始说明。

- 文件：云端仓库 `modal_compute/nonoka_x_compute/operations/prime.py`（或新文件）及其测试。
- 内容：按 4.7 第 37 条写一个不经引擎的预热脚本：读本仓库的模型清单（钉住的 revision 与文件哈希），把云端要的模型放进 `/models` Volume 并逐文件校验；已经在的、校验通过的跳过。`prime_diarization_models` 改用它。不改云端的调度、镜像的其余部分和加速选择。
- 完成标准：在当前 Volume 上跑一遍，结果是「全部已在、校验通过」，没有改动任何文件；在一个空的临时 Volume 上跑一遍，放齐后引擎（还带着自下载）跑 `--cloud` 不再触发任何下载（看日志）；云端测试全过。交付说明里写明花费。

### WP-H 密闭运行与沙箱（已完成 2026-11-18）

> 交付（2026-11-18，问题与答复见附录 B 第 85、87 问）：三个包的进程都从零构建环境、`-I` 启动，转写进程 `-I -B` 并在沙箱里跑。分类表一分为二：中性的一半在 `src/nonoka_x/engine_host.py` 的 `CLASSIFICATION`（系统必需按名带、Nonoka 设定、不存在三类，按进程与平台分，每行有理由），包内的一半在三份逐字相同的 `engines/*/adapter/*/bridge.py`（`FINESUB_*`、模型库变量、密钥，含真实管线读取审计补进的 353 个库变量）。`package_environment` 从空字典开始，PATH 只有工具层与系统目录（纠错另加本地 Agent CLI 的目录与 node），代理总是显式写、不在桌面策略下按直连；`HF_HUB_OFFLINE=1` 由转写包桥接设；`cuda:N` 时适配器无条件设 `CUDA_VISIBLE_DEVICES`。沙箱：`<数据目录>\engines\finesub-transcribe\sandbox\home\` 给 USERPROFILE、HOME、APPDATA、LOCALAPPDATA、HOMEDRIVE+HOMEPATH（附录 C 第 2 项定为改向），每进程 `sandbox\temp\<pid>-<随机>\` 给 TEMP/TMP 并锁住锚点，进程结束删、启动时清残留。就绪的工具只认工具层（非推荐项，用户选的）。闸门加 `sandbox` 一项（`--real` 时：严格区零写入、eager、有沙箱且无残留；只有观察区变化记「未判定」）。新测试 `tests/test_engine_hermetic.py`（覆盖扫描、投毒、读取审计）与 `tests/test_engine_sandbox.py`；`docs/engine.md` 加 2.1a。桌面在用户机器上装的启动器是 uv 解析出的 3.12.13（3.12.9 只是测速样本的版本）。`token_counter_source` 改动前后都是 `tokcount-local`，opencc 可 import。环境基线按用户批准重录（`fcea2b2`，比对部分只多两行 `HF_HUB_OFFLINE`，其余是不比对的启动器部分）。没条件验证：本地 Agent CLI 的真实调用、macOS、注册表与 Known Folder 写入、桌面 exe 实跑。交回：WP-UI——包引擎就绪只认工具层而核心 media 档仍认用户 PATH 上的 ffmpeg，口径要不要统一由用户定；沙箱 home（NVIDIA ComputeCache）要不要进「清理」；macOS 没有工具层前纠错报缺 FFmpeg；WP7——`CHANGELOG.md` 写明外部环境变量（代理、HF 镜像、显卡、线程数）不再影响引擎、工具只用工具层、本地 Agent CLI 只看得到自己的目录；WP8——有条件时真实调用一次本地 Agent CLI，在干净 Windows 上看没有系统 ffmpeg 时的就绪。


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

### WP-L 媒体库同步改用 Nonoka 的格式（已完成 2026-11-18）

> 交付（2026-11-18，问题与答复见附录 B 第 84、86 问）：`correction.json`（schema 1）存对好的字幕行——`mode`（stable / final）、`correct_only`、每行 start/end/source/translation/speaker/low_confidence/words，词对象原样保留；原投影的对齐逻辑整段搬进纠错包适配器（`engines/finesub-correct/adapter/finesub_correct/document.py`），核心投影（`src/nonoka_x/correction/document.py`、`projector.py`）只吃 `transcript.json` 加 `correction.json` 逐行照抄，轴对齐、分轨、隐藏轨仍在核心。说话人只放 `correction.json`（纠错包每个目标都写），`transcript.json` 不动；没装纠错包时也能从 `transcript.json` 投影和同步。产物清单加 `transcript_json`、`correction_json`（排在 `stable_json` 之后、`raw_srt` 之前），调用等价比清单时跳过这两项、基线不动。同步白名单换成 `transcript_json`、`correction_json`、`raw_srt`、`final_srt`；Worker 收到旧名字回 426 并提示更新；只有 SRT 的旧本地任务同步时提示先迁移。两个包的适配器各加一个转换协议操作；一次性转换脚本 `modal_compute/nonoka_x_compute/operations/library_convert.py` 是本机命令行（survey / copy / convert，可重跑、原件进备份前缀），云端仓库还加了媒体库只读开关（转换期间用）。线上只读副本（按批准的做法）：137 条、526 个对象、约 177 MiB；转换 136 条、1 条本来没有产物打不开；新旧投影 136 条全部相同，抽 20 条复核相同，重跑无变化；副本与含 id 的报告已删。切换手册在交付里（见云端仓库与 `docs/`）。新测试 `tests/test_projection_equivalence.py`、`tests/test_library_roundtrip.py`。主会话复核（与 WP-H 一起）：两个仓库在 main 上试合并无冲突，pytest 964 通过 1 跳过（唯一失败是待重录的环境基线，重录后通过），`go test ./...`、前端 160 个、Worker 77 个、`modal_compute` 161 个测试通过；默认闸门与 `--real`（7 个逐字节、8 个按认可标准；sandbox 严格区零写入，观察区只有别的程序在 `%TEMP%` 的 `.tmp`）通过。交回：WP-CL——`relay.go` 里从云端作业取 stable 的两处、任务结果的新格式与转换脚本里任务结果那一步；WP-M2——本地旧任务的转换调同一个协议操作；发版——对线上数据的转换和切换按手册由用户执行或明确批准后执行。


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

### WP-CL 云端重做：只留转写包（已完成 2026-11-18）

> 交付（2026-11-18，问题与答复见附录 B 第 88、92 问）：本仓库 `8a36a94`、云端仓库 `5c8fbb7`（合并 本仓库 429ee9e、云端 379f86a）。Modal 镜像只装转写包；任务结果改为 `transcript.json` 加不透明附件，Worker 存、发不解析，对旧名字回 426；桌面云端续跑改成「转写结果已在手」，附件登记为私有附件；provider 校验不再要求装了 finesub 引擎；`job_convert` 做旧作业结果的一次性转换。`--cloud` 跑 1 次 PASS（约 18 分钟，花费在 $1 上限内）。端到端：本机 Worker（`wrangler dev --local`，密钥全用假值）加真实 relay 与 sidecar 走通到 `final-srt`，纠错拿到的 stable 与附件原件逐字节相同；删掉两个 FineSub 包后出到 `raw.srt`；改动前的旧结果转换后接着跑通。4 个云端调用基线（`tests/fixtures/quality/calls/cloud-*.json`，只差 `input`、`request` 两项）经用户批准重录，单独提交 `8ea28e2`。交回：WP8 / 发版——线上 LLM 那一程没实跑（东京中继按生产 Worker 的 `/v1/session` 验 key），集中到 WP8 或发版时按 J5 实测；线上转换和部署（Worker、Modal 应用、D1、R2）按手册 J1–J5 由用户执行或明确批准后执行。WP-M2——本机旧任务 `request.json` 的 `cloud_stable` 要转成 `cloud_transcript`（已转给阶段 2）。WP-UI——日文轴请求的校验要求装了 finesub 引擎；没装纠错包时云 API 引擎做 `raw-srt` 被 harness 档拦下。下面是原始说明。

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

### WP-M2 迁移的启动集成、续跑与回滚（已完成 2026-11-19）

> 交付（附录 B 第 89–92 问；分两阶段、两个会话）：阶段 1 `6cf0129`（合并 `2b8faf0`），摘要见下面原始说明开头的引用段。阶段 2 `0cc3bcd`、`47cf323`（合并 `01ff48b`）：桌面迁移界面（调 `GET /v1/migration`，进度、结果、重试，`migrating` 503 不当成 sidecar 故障）；新步骤 `leftovers` 过了回滚点就删主数据目录里的 `*.pre-migration` 与整个 `finesub-data\`（含不认识的文件、含本机打不开的旧密钥的备份），删了什么写进报告；旧云端续跑请求的 `cloud_stable` 改写成 `cloud_transcript`（附件键 `finesub.stable_json`、`finesub.diar_json`，随回滚写回），转不成的列进报告。验收：真实安装的新副本上 17/17（设置与密钥读回、不认识的键进报告、16 个任务与 20 个文档照常打开、模型 kept 27 fetched 0、三层 ready、主数据目录除 `engines\` 外没有 FineSub 文件，没有例外），过程记在笔记目录 `wp-m2-delivery.md` 的「验收」一节，WP8 直接沿用；`--real` 按第 92 问移到 WP8。主会话复核：与 WP7 一起在 main 上试合并，pytest 1004 通过、`go test`、前端 typecheck 与测试、默认闸门通过，wails 绑定重新生成与提交一致。交回：WP-UI——桌面中继（`desktop/internal/cloud/relay.go` 的 `startRelayLocal`）遇到 503 `migrating` 当成 refused 失败，应按 sidecar 未就绪等待（已有 `sidecar.IsMigrating`）；WP8——改写后的续跑请求只验过 main 的校验器，挑一个改写过的任务点重试即可端到端；发版——`.nonoka-partial` 半截副本已写进 CHANGELOG。下面是原始说明。

> 阶段 1（Python 核心）已于 2026-11-18 合进 main（`6cf0129`）：sidecar 启动时迁移，未完成时除 `GET /v1/migration`、`POST /v1/migration/retry` 外一律 503 `migrating`；知识库进 `engines\finesub-knowledge\knowledge\`、限流状态进 `engines\finesub-correct\state`；旧任务归位并转换；回滚、续跑、只迁一次；去掉 `bind_environment` 的自动迁移。用户决定见附录 B 第 89 问。阶段 2 还剩：桌面的迁移进度与结果显示、迁移完成后删掉没用的旧文件（第 89 问）、真实形态旧数据副本上的验收与 `--real`、前端测试。

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

### WP7 云端仓库与文档（已完成 2026-11-19）

> 交付（附录 B 第 93 问）：`c4f190b`（合并 `c82364d`）。`docs/architecture.md` 重画（引擎层与纠错层、三个包、三层预制包与沙箱、主数据目录新布局）；`docs/provider-spec.md` 按代码更正（`migrating` 503 与 `/v1/migration`、`/v1/migration/retry`，`missing_engine` 改指引擎包没装，去掉代码里已没有的 `invalid_engine`，日文轴只在本机）；新增 `docs/transcription-engines.md`；`CHANGELOG.md` 写进未发版小节，用户定为 `0.8.0`、多写的 4 条保留（`442bd4e`）；清掉过时的 `third_party/finesub` 引用与来源注释。云端仓库没改，测试全过（Worker 156、`modal_compute` 171）。`scripts/engine_packages_real_record.py` 在 WP-H 之后录不到包进程，按用户决定删除（`442bd4e`）。交回：WP-UI——`engine_host.py` 的 `missing_harness` 提示仍写「运行环境」页；没装纠错包时云 API 引擎做 `raw-srt` 被 harness 档拦下（文档已按现状写成例外）。WP8——`scripts/build_layers.py stage` 过时（仍打印已失效的 `NONOKA_QUALITY_PYTHON`，搭 WP-E3 之前的安装根形状），`--real` 现在要含 `layers\<层>\current.json` 的大文件根；`desktop/docs/PLUGINS.md` 的阶段表没改。下面是原始说明。

- 云端仓库：确认 `modal_compute/nonoka_x_compute/phases.py` 在 WP-B 之后仍调得通（它不传 `asr`，预期不用改）；云端测试全过。需要改时提交在 nonoka-sub-cloud 仓库；`test_source_contract.py` 钉了哈希的文件动了要一起更新。
- 本仓库文档：`docs/architecture.md`（架构图加引擎层与纠错层，FineSub 画成两个可插拔部件，运行环境画成三层预制包与沙箱）、`docs/provider-spec.md`、新增 `docs/transcription-engines.md`、`CHANGELOG.md`（写明数据目录的新布局、迁移会清空旧环境并重新下载、关闭加速后变慢）。

### WP-UI 引擎配置页（已完成 2026-11-19，界面由用户自验）

> 交付（附录 B 第 94、96–98 问）：子代理做阶段 1，`7d4518d`（合并 `a3cc842`）与补改 `49a10e4`（合并 `71edf0a`）。设置页引擎列表显示就绪、缺哪几层、下载量、占用；选中未装的引擎只装它缺的层（沿用运行环境段的位置确认、进度、取消、重试）；按引擎列占用，共用层只算一次；「清理未使用的」新增 `GET`/`DELETE /v1/runtime/provision/unused?engine=`，默认只删没人用的旧版本，当前引擎用不到的整层要逐层勾选，未结束（含被中断）任务占用的层不可清；当前引擎被删或未就绪时引擎段、首页横幅、新建任务对话框都提示重选，不静默回落；就绪文案改指向「设置 → 运行环境」。交回各条：没装知识库包时翻译、纠错、日文轴在启动前拦下；缺说话人分离模型的请求在新建时拦下（`missing_diarization_model`，重试与续跑不判，第 97 问）；核心 media 档只认工具层的 ffmpeg；日文轴校验不再要求装了转写包；没装纠错包时云 API 引擎照常出原始字幕；中继在迁移期间一直等、迁移失败报 `migration_failed`；顺带修好 Go 拒绝 `repair` 目标（「完整核对」按钮）。主会话精简验收（第 98 问）：在 WP-M2 验收副本的复制上用 sidecar 接口验了四种引擎包变体的就绪、清理计划与拦截，FineSub 请求与 WP6 相同，记录在笔记目录 `wp-ui-delivery.md` 的「阶段 2：精简验收」；界面的逐项点验由用户自己做（装未装的引擎、取消再继续、真删层也在其中）。交回：WP8 / 发版——用户自己的界面验收；PATH 上的 `wails3` 是 alpha2.119，仓库已是 3.1.0，重生成绑定会改动几乎所有文件，只能按需挑文件提交。下面是原始说明。

> 收到的交回（2026-11-19）：归档里 WP-E3、WP-P1、WP-H、WP-CL、WP-M2、WP7 各节「交回」中写给 WP-UI 的条目都归本包，其中 WP-M2 的「中继遇到 503 `migrating` 当成失败」一条在 `desktop/internal/cloud/relay.go`，WP-CL 的「日文轴请求的校验要求装了 finesub 引擎」一条在核心的请求校验，一并并入本包的文件。

重构的其余部分做完之后开工：等 WP-P1、WP-L、WP-H、WP6、WP-E3，排在 WP8 之前，随同一个版本发布。规则见 4.9 第 39 条。

> 2026-11-13 用户改了方向（4.9 第 39 条第一个子条目，附录 B 第 73 问）：没有单独的引擎配置页，「运行环境」页去掉，下面写到引擎配置页的内容都放进设置页；界面主体已由 WP6 第二阶段做完（见 WP6 一节），本包只剩依赖 WP-P1、WP-L、WP-H 的部分和完成标准里的整体验收：删掉转写包后的表现、按包列占用空间、选中未装的引擎时只装它缺的层（现在仍是运行环境段的「一键准备全部」与分组安装）、从 FineSub 换到云 API 引擎再换回来不重下的实测；Python 侧有些就绪文案还写「在『运行环境』页……」（如 `missing_harness`），要改成指向设置页。WP-R6、WP-R5 交来的界面事项（卸载视频工具的死代码、`repair` 的入口、「清理未使用的」、numba 缓存进不进「清理」）已由 WP6 第二阶段处理（附录 B 第 74 问）。

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

### WP7 云端仓库与文档（已完成 2026-11-19）

已完成。交付摘要与原始说明移到 `docs/transcription-engines-plan-done.md` 的同名一节。

### WP8 集成验证（已完成 2026-11-19）

> 交付（附录 B 第 92、95、99–101 问；完整报告见主会话笔记目录 `wp8-delivery.md`；期间修复与设置页重构提交在 `22daa69`）：
> - **静态核对**：`wp8-tools/w8_finesub_diff.py` 核对引擎包内 FineSub 文件相对 `1f14db3` 942 个逐字节相同，34 处差异全在批准范围；`speech/joint/` 21 个文件全不存在；`third_party/finesub/` 已清除；`src/nonoka_x/` 无 `import finesub`（第 81、99 问）。
> - **全量测试**：`nonoka-sub-x` pytest 全过（1020 passed, 1 skipped，清除 11 处锁死引擎中文文案原文的脆弱断言后复测通过）；`desktop/frontend` `typecheck`、`npm test`（173/173）、`npm run build` 全过；`desktop` `go test ./...` 15 个包在干净检出全过；`nonoka-sub-cloud` Worker `typecheck` + `npm test`（78/78）与 `modal_compute` pytest（171/171）全过。
> - **最终闸门**：默认 `scripts/quality_gate.py` 6/6 全过（510 上锁文件、97 包/7 工具/11 模型、45 bootstrap 名字、32 调用场景、worker 环境、10 组 LLM 回放、7 组知识库）；`--only layers` 对从 R2（`layers.nonoka.online`）下载解包的三层（`tools` `b2e46916329ea7c4`、`python` `8cc0ee612a5db785`、`models` `f2a2b753498be37c`）全部一致（45,563 + 33 + 9,225 文件，148.1s）；`--real` 15/15 产物与基线一致（7 逐字节、8 按批准标准，209.6s；`sandbox` 仅因并发运行中的桌面端写 `%APPDATA%\Nonoka X` 与新启动器首次生成 `.pyc` 触发）；`--only cloud --cloud` 在 Modal L4 实跑通过（1295.7s，约 \$0.3 在 \$1 上限内，两个样本四项指标均零偏差）。
> - **可删性**：目录联接三种变体（`no-correct`、`no-transcribe`、`no-both`）全按 4.6 第 33 条通过；过程中发现并修好 `local_provider.py` 无转写包时启动 `cloud_transcript` 抛 `KeyError: 'finesub'` 的回归缺陷。
> - **可替换性**：6a 仅留 `tools` 层、无 `python`/`models`/转写包时，用 `cloud_transcript` + `finesub-correct` 跑通到 `final-srt`；6b 真实 GPU `finesub-transcribe` + `FakeCorrection` 跑通生成 `final.srt` 与 `correction.json`（109 行）。
> - **迁移与端到端**：迁移沿用 `wp-m2-delivery.md` 真实副本实测，跳过旧云端任务重试（第 99 问）；FineSub 端到端用 `sample_ja.m4a` + R2 三层 + Nonoka Cloud LLM（`gpt-6.1-terra`）完整跑通 `final-srt`（109 行，8,032 字节），探针确认 `token_counter_source=tokcount+api+heuristic` 与 `opencc=opencc`。
> - **界面自验与缺陷修复**（`22daa69`，第 100–101 问）：修好外部 PATH Python 缺少 `opencc`/Windows `tzdata` 导致模型测试 HTTP 502（`desktop/internal/app/runtime.go`）；完成设置页重构（引擎凭据弹窗、运行环境拍平三行与单删 `models` 层、首启 A1 位置确认、全局模型外置与「其他配置」弹窗隐藏 `HF_TOKEN`、Portal `InfoTip`、合并存储与网络卡、LLM 测试底栏状态与宽度修复）。
> - **未验证/未做项**（第 95 问）：三个云 API 引擎真实调用标为未验证；干净 Windows 全新安装本次不做。发版前按 `docs/library-cutover.md` 割接并向用户确认 `nonoka-sub-cloud` 的 `cursor/hotfix-cpu-limit-fe90` 是否合入 `main`。下面是原始说明。

- 全量测试：`D:\Envir\Python312\python -m pytest -q`（仓库根）、`cd desktop/frontend && npm test && npm run typecheck`、`cd desktop && go test ./...`、`cd nonoka-sub-cloud/modal_compute && python -m pytest -q tests`。
- 最终闸门：`scripts/quality_gate.py --real`、`--layers`（对 R2 上要发布的层）和 `--cloud` 全部通过；用纠错进程跑的纠错里 `token_counter_source` 仍是 `tokcount`；两个引擎包里的 FineSub 文件相对 `1f14db3` 只有 `stages.py`、`pipeline.py`、`finesub_bootstrap` 的自下载部分变过，`speech/joint/` 不在，其余每个文件的哈希等于 WP-Q 的 `QUALITY_LOCK.json`；`third_party/finesub/` 不存在；`src/nonoka_x/` 里搜不到 `finesub`。
- 可删性：实际删掉 `engines/finesub-transcribe/`、删掉 `engines/finesub-correct/`、两个都删，各启动一次应用并跑一个任务，结果符合 4.6 第 33 条。
- 可替换性：不装 Python 环境层、不注册 FineSub 转写引擎时，用一个云引擎加 finesub correction 跑到 `final-srt`；反过来 FineSub 转写引擎的结果交给 finesub correction 以外的实现（测试用假实现）也能走完。
- 迁移：在一份真实的旧版用户数据目录副本上装新版，迁移后照常使用，模型没有重下，三层从 R2 下载装好，主数据目录里除 `engines\` 外没有 FineSub 相关文件。
- 新用户：在一台干净的 Windows（没有系统 Python、没有开发者模式）上全新安装，从 R2 装好三层（经不经代理不限，附录 B 第 68 问），跑一个任务到 `final-srt`。
- FineSub 路径端到端：一个真实样本用真实模型跑到 `final-srt`，把字幕交给用户和基线对照着看一遍。闸门证明的是「发给模型的东西没变」，这一步是给人看最终效果。
- 真实调用：三个云引擎各跑一个 `test_data/audio` 样本到 `final-srt`，分别在装了和没装 Python 环境层的环境下。需要用户提供密钥，没有密钥的引擎如实标记为未验证。

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
