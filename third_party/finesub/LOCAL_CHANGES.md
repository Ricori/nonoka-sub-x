# FineSub 本地改动

`third_party/finesub/src` 相对 `UPSTREAM.json` 记录的上游版本（`v0.7.0`，`7123055`）改过的地方都记在这里：改了什么、为什么、何时可以去掉。

规则（见 `docs/transcription-engines-plan.md` 第 0 节与第 3 节）：

- 上锁文件（`QUALITY_LOCK.json` 列出的全部文件）不许改。下面 0001–0008 是质量锁建立之前就已在 vendor 里的改动，落在上锁文件里的部分已经包含在锁的哈希里；0002 全部、0004 与 0006 里 `stages.py` 的部分落在解锁的地方。
- 第 0 节解锁的四处（`finesub/stages.py`、`finesub/pipeline.py`、`finesub/speech/joint/`、`finesub_bootstrap/`）可以按工作包修改，改了就在本文件追加一节。
- 升级上游时，用 `python scripts/finesub_align.py diff --ref <tag>` 查看关键算法路径上上游改了什么、和这里的哪些改动重叠；升级本身要用户单独批准，并通过 `scripts/quality_gate.py`。
- 某条改动被上游收走或不再需要时，删掉对应一节。

编号沿用原补丁栈的编号，测试和代码注释里的「local change 000N」指的就是这里的对应一节。

## 0001 Qwen 复核的收尾入口

原补丁：`0001-qwen-tail-entry-point.patch`（影响模块 `src/finesub`）

**改了什么、为什么**：云端三容器架构把纯 CPU 的收尾与要 GPU 的识别分开调度，而上游的复核是 `run_vad_asr` 内联的一段，没有对既有 aligned 产物补挂证据的入口。`finalize_qwen_verification` 就是这个入口，调用的是上游自己的 `apply_verification`，失败兜底直接用上游的 `contained_verification`；纯增量，本地与 CLI 走的仍是未被触碰的内联路径。0.6.0 在 `write_aligned_json` 与 `annotate_segments_with_vad_energy` 之间插入了 `_finish_segment_timeline`，补丁随之重新定锚，函数体不变（ASR 上下文仍由云端在调用前 `set_context` 注入）

**何时可以去掉**：上游支持对既有 aligned 产物单独跑复核，或不再拆分容器

**文件**：
- `src/finesub/speech/recognition/vad_asr_stage.py`

## 0002 运行时资源清单

原补丁：`0002-runtime-resource-management.patch`（影响模块 `src/finesub_bootstrap`）

**改了什么、为什么**：Nonoka X 没有独立的资源系统（所有托管二进制都在 `finesub/runtime/<id>/` 下由 `managedtools` 解析），可安装什么由这份清单决定，与上游的差异都在这个补丁里，分三部分。**下载工具**：桌面端视频下载器需要 aria2c、node 与 pot-provider，上游清单未声明，只能在此声明才可安装。三者只在一起才有用：YouTube 是逐道设卡的，缺一件会在更早的一道上失败。**移除托管 Git**：知识库已是 SQLite，运行时不再需要 MinGit；保留它只会多一个下载目标、进诊断、并把它的目录塞进每个 worker 的 PATH。**固定 FFmpeg**：上游跟随 BtbN 每天重建的 `latest` tag，摘要在安装时现查。GitHub 文件在 R2 上有一份兜底副本（见 `docs/engine.md` 的「GitHub 文件的兜底副本」），会变的字节无法校验，所以固定到一个月末 autobuild（这类 tag 不随每日构建清理）。换版本时改 `src/finesub_bootstrap/runtime-manifest.json`（钉住的版本受质量红线第 7 条约束），并重新运行 `publish-github-mirror.mjs`。此前装的 `n9.0-latest` 继续可用（`RuntimeProvisioner._unpinned_ffmpeg`），下次安装运行环境时才换成固定版本

**何时可以去掉**：三部分分别移除，都移除后删掉本节。**下载工具**：上游清单收录这三项工具。**移除托管 Git**：上游移除托管 Git。**固定 FFmpeg**：不再需要自托管 FFmpeg

**文件**：
- `src/finesub_bootstrap/environment.py`
- `src/finesub_bootstrap/models.py`
- `src/finesub_bootstrap/runtime-manifest.json`
- `src/finesub_bootstrap/shell.py`
- `src/finesub_bootstrap/system_tools.py`

## 0003 AOTI 工具链移植

原补丁：`0003-aoti-toolchain-portability.patch`（影响模块 `src/finesub`）

**改了什么、为什么**：让 AOTI 在两种上游没覆盖的工具链上编得过，只决定编不编得成，不影响分离结果。**MSVC `/std:c11`**：Windows 下 Triton 生成的 C11 启动器代码（`__triton_launcher.c`）包含空结构体初始化 `CUlaunchAttribute clusterAttr = {};`，MSVC 报 `error C2059: 语法错误: '}'`，且 Windows SDK `winbase.h` 产生 `warning C5105`；`_patch_triton_windows_driver` monkey-patch Triton 的启动器生成与构建命令，将 `{}` 替换为合法的 `{0}` 并插入 `/wd5105` 屏蔽告警。**POSIX**：上游的工具链探测是**有意** Windows-shaped（`_activate_msvc` / `cxx_toolchain_available`），因为 AOTI 这条路只在 Windows 上跑过。云端容器是 Linux，没有它每次构建都抛「requires MSVC」、被 `vocal_accel.install` 接住降级为 eager，云上每个 ≥270s 的任务都在无加速下跑而无人察觉；`_posix_cxx_driver` 让探测在 POSIX 上认系统 C++ 编译器

**何时可以去掉**：两半分别移除，都移除后删掉本节。MSVC 部分：上游 Triton 修复 C11 下的空结构体初始化或上游 FineSub 内置该适配。POSIX 部分：上游支持 Linux 生产链路（0.5.1 已明确判定这是下游的移植需求，不收）

**文件**：
- `src/finesub/speech/preprocessing/separator/separator_aoti.py`

## 0004 固定知识主体

原补丁：`0004-pinned-knowledge-subjects.patch`（影响模块 `src/finesub/llm`）

**改了什么、为什么**：桌面端允许用户手动多选知识主体，读取与写回共用。上游只在有东西来要时才注入词条（背景信息里的关键词、准备任务的判断、窗口的 `<requested_entries>`），选中的主体仍可能不进任何窗口。本补丁新增 `knowledge_subjects`：0.6.0 把纠错收敛成一个 `CorrectionRequest`，它成为其中一个字段，`run_pipeline` 照样按扁平关键字接收再打包；`llm/knowledge/subjects.py` 按限定名解析（找不到时直接报错），把词条渲染成一个受预算约束的块，与风格块一起放进每个纠错窗口（`read` 与 `update` 都注入）；任务后的知识更新把它们固定（`pinned`）在每个分块 `<kb_entries>` 的最前面。纯增量：不传这个参数时所有路径都与上游一致

**何时可以去掉**：上游支持按任务静态指定知识主体，用于注入与写回

**文件**：
- `src/finesub/llm/correction_request.py`
- `src/finesub/llm/correction_translation.py`
- `src/finesub/llm/knowledge/entries.py`
- `src/finesub/llm/knowledge/subjects.py`（新增）
- `src/finesub/llm/knowledge/update.py`
- `src/finesub/stages.py`

## 0005 agy 兼容

原补丁：`0005-agy-compatibility.patch`（影响模块 `src/finesub/llm/agent`）

**改了什么、为什么**：agy 1.3 起，`--project` 指向一个它已不认识的 id（`~/.gemini/config/projects` 下的记录没了）时会静默改开 `default-cli-project`，退出码仍为 0、仍列出 cwd 的 hooks。引擎的 `/hooks` 项目检查因此一直通过，缓存的 `finesub-project.json` 永不失效，每次工具调用都死在 `_grant_permissions`。本补丁让 `_check_project` 从本次日志读出 agy 实际打开的项目（`Conversation using project ID:`），不一致即视为不可用，交给既有的快速路径重新注册；刚注册完仍不一致则直接失败。agy 1.4 的 `/hooks` 日志不再输出这一行，所以检查还要求缓存 id 在 `~/.gemini/config/projects` 下有记录，缺失同样重新注册；记录目录整个不存在时不做判断。守卫编码部分上游 0.7.0 已移植

**何时可以去掉**：上游自行核对实际打开的项目，或 agy 对未知 `--project` 改为报错

**文件**：
- `src/finesub/llm/agent/local_agent.py`

## 0006 续跑路径的延迟导入

原补丁：`0006-lazy-asr-import-in-stages.patch`（影响模块 `src/finesub`）

**改了什么、为什么**：桌面端从云端交付的 `stable.json` 续跑时，人声与识别阶段都不会启动，启动环境也只装了 `[harness]`。上游 0.7.0 已把这些 import 从模块顶层移走，但 `run_pipeline` 和 `_run_vocal_stage` 的函数体开头仍无条件 import 识别与分离（要 numpy、torch），`marks.row_marks` 也在判断证据可用之前就 import numpy。本补丁把这三处挪到真正要用的分支里；需要识别的运行导入同样的模块，算出来的东西不变；云端共用无妨

**何时可以去掉**：上游的续跑路径不再依赖 numpy、torch

**文件**：
- `src/finesub/speech/diarization/marks.py`
- `src/finesub/stages.py`

## 0007 LLM 等待可见性

原补丁：`0007-llm-wait-visibility.patch`（影响模块 `src/finesub/llm`）

**改了什么、为什么**：2026-10-29 一个桌面任务在路由提示和第一个纠错窗口之间 29 分钟没有任何日志。本补丁让 `_call_and_parse` 在每轮研究开始等待时、以及回复到达后各记一行 `debug`；解析失败且还要重试时发 `research-parse-retry` 警告；`hedge_on_silence` 在调用未返回期间每 5 分钟记一行 `llm api call still waiting`，带上流式进度（`StreamProgress.describe`）。调用本身、超时与结果都不变；云端共用无妨。桌面端另外放行耗时 ≥ 60 秒的成功 `llm api call`

**何时可以去掉**：上游自己报告进行中的调用与研究轮次

**文件**：
- `src/finesub/llm/http.py`
- `src/finesub/llm/llm_runtime.py`
- `src/finesub/llm/research.py`

## 0008 LLM 流式回复超时

原补丁：`0008-llm-stream-reply-timeout.patch`（影响模块 `src/finesub/llm`）

**改了什么、为什么**：上游只按「有没有收到字节」判断流式调用，keep-alive 也会清零计时，中转挂住时要等到硬超时（默认预算约 46 分钟）；而硬超时又是总时长上限，会切断还在正常生成的调用。本补丁改成看真回复：答案文本和思考内容算回复，keep-alive 注释、空 delta、只带 usage 的事件不算。10 分钟没有回复发备份请求（`silence_seconds=min(soft, 600)`）；备份发出后两条都连续 10 分钟没有回复就一起断开并抛 `StreamInterrupted`（上游按类型重试），任何一条收到回复这 10 分钟重新计；回复还在到达时顺延硬截止。只作用于 LLM 生成调用，网页检索和本地 agent CLI 不变；云端不走这条路径

**何时可以去掉**：上游按真回复判断流式调用是否挂住，且不因总时长切断仍在回复的调用

**文件**：
- `src/finesub/llm/http.py`
- `src/finesub/llm/llm_runtime.py`
- `src/finesub/llm/provider_transports.py`
- `src/finesub/llm/streaming.py`
