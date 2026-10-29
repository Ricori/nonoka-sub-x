# FineSub 引擎同步、补丁栈与构建规范

本文档记录 FineSub 算法引擎在 Nonoka X 中的集成原则、快照同步流程、补丁栈维护规则、Patched CTranslate2 构建以及本地 Engine Bundle 构建规范。

---

## 1. 引擎集成原则

Nonoka X 不 fork FineSub，也不手工复制零散算法模块：

1. **快照同步**：仓库通过自动化脚本同步官方固定 commit 的完整引擎快照，`third_party/finesub` 视为生成产物。
2. **同源引擎包**：构建产出字节稳定的本地与云端共用 Engine Bundle（`finesub-engine/<version>+<commit>`），杜绝本地与云端算法漂移。
3. **补丁栈约束**：开发调试时可直接修改 vendor，但提交前必须整理为独立的 `patches/finesub/*.patch`，并通过正反向重放契约测试。
4. **外部适配优先**：业务逻辑、任务状态与数据模型统一在外部 Adapter 与 Projector 实现，保持引擎纯粹。

---

## 2. 同步机制与目录规范

### 2.1 目录结构
```text
third_party/finesub/
├── README.md
├── UPSTREAM.json           # 上游源信息、commit 与校验哈希
├── FILES.json              # 当前打补丁后的文件清单
├── LICENSE                 # 上游 GPL-3.0-or-later 开源协议（0.5.0 起，此前为 MIT）
├── PROMPT_LICENSE.md       # Prompt 模板的 CC BY-SA 4.0 协议
├── pyproject.toml
├── VERSION                 # pyproject.toml 读取的动态版本号
└── src/
    ├── finesub/            # 核心转写流水线
    └── finesub_bootstrap/  # 运行时与模型下载管理器
        ├── runtime-manifest.json    # 托管二进制清单
        ├── pylock.win-py312.toml    # Python 运行环境锁
        └── pylock.win-py312.cn.toml # 同上，国内镜像路由

patches/finesub/
├── 0001-*.patch … 0011-*.patch   # 补丁栈，按文件名顺序应用（当前 11 个）
└── BASELINE_FILES.json           # 未打补丁的上游基线文件哈希
```

> `BASELINE_FILES.json` 在 `patches/` 下而不是 vendor 里：它描述的是**撤掉这些补丁之后**
> 的 vendor，放进 vendor 就得描述它自己。`sync` 有意不生成它，否则正反向重放会拿一份
> 东西跟它自己比；改用 `python scripts/sync_finesub.py baseline` 单独重建。

> 上游 0.5.0 把桌面端移出了仓库，托管运行时的三份资产随之成为 `finesub_bootstrap`
> 的包内数据：`resources.PACKAGED_RUNTIME_MANIFEST` 与 `environment.PACKAGED_RUNTIME_LOCK`
> 都用 `Path(__file__).with_name(...)` 解析它们。快照因此**不再**保留
> `resources/` 与 `runtime/` 两个目录——留一份副本只会让补丁打在没人读的那份上，
> 而且失败是静默的。

### 2.2 同步白名单
- **核心代码**：`src/finesub/**`、`src/finesub_bootstrap/**`（含上述三份运行时资产）
- **配置文件与协议**：`pyproject.toml`、`LICENSE`、`PROMPT_LICENSE.md`
- **版本号**：仓库根目录 `VERSION`。0.5.0 起 `pyproject.toml` 的 `project.version`
  是 `dynamic`，同步器改为读这个文件，读不到即中断。
- *排除项*：FineSub CLI 包装、部署脚本、agent 任务说明、实验测试文件。

### 2.3 `UPSTREAM.json` 契约
```json
{
  "repository": "https://github.com/caca2331/finesub",
  "ref": "v0.6.0",
  "commit": "5caf422cbad36f0402c98eb565e4c29aee4ac197",
  "archive_sha256": "...",
  "engine_version": "0.6.0",
  "synced_at": "2026-10-11T20:10:58Z",
  "sync_schema": 1,
  "patches": []
}
```
构建与 CI 必须严格校验 `UPSTREAM.json`，哈希不匹配时立即中断构建。

---

## 3. 补丁栈管理与契约测试

### 3.1 当前维护的补丁清单
| 补丁文件 | 影响模块 | 存在原因 | 移除条件 |
| :--- | :--- | :--- | :--- |
| `0001-qwen-tail-entry-point.patch` | `src/finesub` | 云端三容器架构把纯 CPU 的收尾与要 GPU 的识别分开调度，而上游的复核是 `run_vad_asr` 内联的一段，没有对既有 aligned 产物补挂证据的入口。`finalize_qwen_verification` 就是这个入口，调用的是上游自己的 `apply_verification`，失败兜底直接用上游的 `contained_verification`；纯增量，本地与 CLI 走的仍是未被触碰的内联路径。0.6.0 在 `write_aligned_json` 与 `annotate_segments_with_vad_energy` 之间插入了 `_finish_segment_timeline`，补丁随之重新定锚，函数体不变（ASR 上下文仍由云端在调用前 `set_context` 注入） | 上游支持对既有 aligned 产物单独跑复核，或不再拆分容器 |
| `0002-runtime-tool-manifest.patch` | `src/finesub_bootstrap/runtime-manifest.json` | 桌面端视频下载器需要 aria2c、node 与 pot-provider，上游清单未声明；Nonoka X 没有独立的资源系统（所有托管二进制都在 `finesub/runtime/<id>/` 下由 `managedtools` 解析），只能在此声明才可安装。三者只在一起才有用：YouTube 是逐道设卡的，缺一件会在更早的一道上失败 | 上游清单收录这三项工具 |
| `0003-triton-msvc-c11-empty-struct.patch` | `src/finesub` | Windows 下 Triton 生成的 C11 启动器代码（`__triton_launcher.c`）包含空结构体初始化 `CUlaunchAttribute clusterAttr = {};`。MSVC 在 `/std:c11` 严格模式下报 `error C2059: 语法错误: '}'`，且 Windows SDK `winbase.h` 产生 `warning C5105`。本补丁 monkey-patch Triton 的启动器生成与构建命令，将 `{}` 替换为合法的 `{0}` 并插入 `/wd5105` 屏蔽告警 | 上游 Triton 修复 C11 下的空结构体初始化或上游 FineSub 内置该适配 |
| `0004-aoti-posix-compiler.patch` | `src/finesub` | 上游的工具链探测是**有意** Windows-shaped（`_activate_msvc` / `cxx_toolchain_available`），因为 AOTI 这条路只在 Windows 上跑过。云端容器是 Linux，没有它每次构建都抛「requires MSVC」、被 `vocal_accel.install` 接住降级为 eager，云上每个 ≥270s 的任务都在无加速下跑而无人察觉。本补丁让探测在 POSIX 上认系统 C++ 编译器。⚠ 必须排在 0003 之后：两条改的是同一个文件 | 上游支持 Linux 生产链路（0.5.1 已明确判定这是下游的移植需求，不收） |
| `0005-remove-managed-git.patch` | `src/finesub_bootstrap` | 知识库已是 SQLite，运行时不再需要 MinGit；保留它只会多一个下载目标、进诊断、并把它的目录塞进每个 worker 的 PATH | 上游移除托管 Git |
| `0006-pinned-knowledge-subjects.patch` | `src/finesub/llm` | 桌面端允许用户手动多选知识主体，读取与写回共用。上游只在有东西来要时才注入词条（背景信息里的关键词、准备任务的判断、窗口的 `<requested_entries>`），选中的主体仍可能不进任何窗口。本补丁新增 `knowledge_subjects`：0.6.0 把纠错收敛成一个 `CorrectionRequest`，它成为其中一个字段，`run_pipeline` 照样按扁平关键字接收再打包；`llm/knowledge/subjects.py` 按限定名解析（找不到时直接报错），把词条渲染成一个受预算约束的块，与风格块一起放进每个纠错窗口（`read` 与 `update` 都注入）；任务后的知识更新把它们固定（`pinned`）在每个分块 `<kb_entries>` 的最前面。纯增量：不传这个参数时所有路径都与上游一致 | 上游支持按任务静态指定知识主体，用于注入与写回 |
| `0007-agy-project-fallback-reregister.patch` | `src/finesub/llm/agent` | agy 1.3 起，`--project` 指向一个它已不认识的 id（`~/.gemini/config/projects` 下的记录没了）时会静默改开 `default-cli-project`，退出码仍为 0、仍列出 cwd 的 hooks。引擎的 `/hooks` 项目检查因此一直通过，缓存的 `finesub-project.json` 永不失效，每次工具调用都死在 `_grant_permissions`（"project record ... is not readable"）。本补丁让 `_check_project` 从本次日志读出 agy 实际打开的项目（`Conversation using project ID:`），不一致即视为不可用，交给既有的快速路径重新注册；刚注册完仍不一致则直接失败，不会每次调用都注册一个新项目。日志里没有这一行时不做判断，行为与上游一致。agy 1.4 的 `/hooks` 日志不再输出这一行，检查又形同虚设：2026-10-25 agy 1.4.5 迁移项目记录时丢掉了 tool 项目的记录，工具调用再次全部死在同一处。因此检查还要求缓存 id 在 `~/.gemini/config/projects` 下有记录，缺失同样视为不可用、重新注册；记录目录整个不存在时不做判断（agy 可能挪了位置，且只有工具路径需要它） | 上游自行核对实际打开的项目，或 agy 对未知 `--project` 改为报错 |
| `0008-agy-guard-utf8-stdin.patch` | `src/finesub/llm/agent` | agy 的 PreToolUse 守卫脚本（`guard_view_file.py`，三个模板）用 `json.load(sys.stdin)` 读载荷。agy 写的是 UTF-8，文本 stdin 却按系统代码页解码，中文 Windows 上是 cp936：转义引号前若是奇数个 UTF-8 字节的中日文，最后一个字节会和反斜杠拼成一个 GBK 字符，`\"` 失去转义，JSON 解析失败，钩子崩溃即被 agy 当作拒绝。纠错 `submit` 的内容里一旦有 `…不明确\"` 这类片段就会被拒，模型只能整份重写（2026-10-13 一个窗口被拒 5 次，耗时 625 秒；更早一次只能靠全 ASCII 的 `text` 占位答案通过）。9 月起 agy 1.2.2 的日志里就有同样的崩溃，与 agy 版本无关。改为 `json.loads(sys.stdin.buffer.read())`，由 `json` 自行识别 UTF-8。脚本内容变化会让 hook digest 改变，FineSub 的 agy 项目各重新注册一次 | 上游守卫改为按字节读取 |
| `0009-reject-placeholder-correction.patch` | `src/finesub/llm` | 纠错校验只看结构（覆盖、位置、列数、单元格非空）。2026-10-13 一个 agy 窗口在真答案被守卫拒绝后（见 0008），提交了 52 行全是 `text|text` 的试探答案并被采纳，最终字幕每行都是 "text"。`validate_correction_output_text` 在结构通过后再整窗判断一次：5 行及以上每行完全相同，或过半行是占位词（`text`、`test`、`placeholder`、`todo`、`tbd`），即作为可修复错误打回重写。单独一行短句、或短窗口里重复的「うん」不受影响。续跑与回放走同一个函数，缓存里的占位答案不会再被复用。只会更严，云端共用无妨 | 上游拒绝占位式纠错答复 |
| `0010-lazy-asr-imports-in-stages.patch` | `src/finesub` | 桌面端从云端交付的 `stable.json` 续跑时，`run_pipeline` 的 `aligned_required` 为假、人声与识别阶段都不会启动，但 `finesub.stages` 在模块顶层导入了 `transcribe`、`vad_asr_stage` 与分离器，三者一导入就加载 numpy 和 torch，于是只装了 `[harness]` 的启动环境连 `run_pipeline` 都导入不了。本补丁把这三个模块挪到各自阶段内部导入；`model_name` 与 `gap_sec` 原本以 `transcribe` 的常量为默认值（定义时就要导入它），改为默认 `None`、在识别阶段内解析成同样的常量，仍然只有一处定义。需要识别的运行导入同样的模块，只是晚一个阶段，算出来的东西不变；云端共用无妨 | 上游 `finesub.stages` 不再在导入时依赖 torch |
| `0011-llm-wait-visibility.patch` | `src/finesub/llm` | 2026-10-29 一个桌面任务在路由提示和第一个纠错窗口之间 29 分钟没有任何日志：准备任务在等一个慢的 OpenAI 兼容模型，成功调用只记 `debug`（桌面端会滤掉），解析重试只写 artifacts，软超时按 64k 输出算要约 47 分钟才到，看上去和卡死没区别。本补丁让 `_call_and_parse` 在每轮研究开始等待时、以及回复到达后（哪个模型、用了多久）各记一行 `debug`；解析失败且还要重试时发 `research-parse-retry` 警告（每次重试都是整次重新生成）；`_SoftDeadline` 在调用未返回期间每 5 分钟记一行 `llm api call still waiting`，软超时前后都记，软超时警告本身不变。调用本身、超时与结果都不变，只多几行日志；云端共用无妨。桌面端另外放行耗时 ≥ 60 秒的成功 `llm api call` | 上游自己报告进行中的调用与研究轮次 |

> 上游 **0.6.0** 又收走了四条，本仓库删掉它们并把编号重排为 0001–0006：
> **旧 0003**（agy 工作区读取授权）——上游给自己的 agy project 授予 `read_file(*)`
> （`local_agent.AGY_READ_PERMISSION_RULES`），并要求 agy ≥ 1.2.16；**旧 0004**（复用
> Nonoka X 0.4.2 运行时的锁哈希）——上游删掉了 `_LEGACY_LOCK_FILE_DIGESTS`，而且 0.6.0
> 的锁换了补丁版 CTranslate2、新增 `cryptography`，托管运行时无论如何都要重建一次；
> **旧 0008**（Silero 非 ASCII 路径）——上游改由 Python 打开 TorchScript 再交给 torch
> （`speech/runtime/native_paths.load_torchscript`）；**旧 0009**（Codex GPT-6 目录行）——
> 上游自带 GPT-6 Luna / GPT-6.1 Sol，桌面端改为与上游对齐，不再额外提供 GPT-6 Sol / Astra
> （Codex CLI 需 ≥ 0.160.0）。
>
> 0.6.0 带来的适配层变化（不是补丁，记在这里方便对照）：`run_full_correction` 被删，
> 日文轴走 `run_correction_task(CorrectionRequest(...))`；任务组只剩 `correction-mm`、
> `correction-text`、`research`、`knowledge`（`planning-*` 与 `search_judge` 并入准备任务，
> 桌面设置随之迁移）；知识库 `collect` 改名 `read`；WorkBuddy 改为独立 CodeBuddy CLI
> （`LOCAL_CODEBUDDY`）；`LLMRole` 去掉了 `lightweight`（插件 API 保留这个名字，映射到
> `general_capable`）；`annotated.csv` 增加表头与 `spk` 列。

### 3.2 补丁栈测试约束
- **正反向重放**：`tests/test_finesub_patch_stack.py` 会从当前 vendor 逆序撤销所有 patch，比对是否与 `BASELINE_FILES.json` 一致；随后重新正序应用，比对是否与 `FILES.json` 完全一致。
- **边界保护**：除特定拆分与路由补丁外，严禁补丁触碰未经测试证明的算法核心。

### 3.3 同步与校验命令
```bash
# 离线校验当前快照与补丁完整性
python scripts/sync_finesub.py check

# 同步之后重建基线（逆序撤销补丁栈再取哈希；sync 不做这一步）
python scripts/sync_finesub.py baseline

# 运行补丁栈与引擎契约测试
python -m pytest tests/test_split_stage_contract.py tests/test_finesub_patch_stack.py tests/test_referee_placement.py tests/test_agent_tool_arg_encoding.py tests/test_resource_activation_retry.py -q

# 构建本地/云端 Engine Bundle
python -m scripts.build_finesub_bundle

# 同步指定 upstream 版本
python scripts/sync_finesub.py sync --ref v0.6.0
```

---

## 4. Patched CTranslate2 构建

FineSub `0.6.0` 的 `fw-refine` 后端深度依赖 CTranslate2 解码器内部的 attention、beam winner lineage、终止事件与 chosen logprob 接口，原生 CTranslate2 不具备此能力。桌面端的托管运行时直接安装上游 CI 构建的 Windows wheel（`ct2-4.8.1+finesub0.6.0`，见运行时锁）；云端镜像从源码编译同一套补丁。

### 4.1 补丁序列
补丁源码位于 nonoka-sub-cloud 仓库的 `modal_compute/ct2_patches/`，基于上游 `v4.8.1 / 0d8bcd36`：
- `0001`–`0004`：WT alignment 控制与 attention 后处理。
- `0005`–`0008`：Decoded prefix、单遍 trace/refine path 与 unfinished span。
- `0009`–`0010`：Beam winner lineage 与 disfluency weights。
- `0011`：逐样本 `real_audio_frames` 支持。
- `0012`–`0013`（FineSub 0.6.0）：flash attention 只作用于 Whisper encoder，并暴露 `flash_attention_available()`；构建只编 encoder 用得到的那一份 kernel。上游原样复制，哈希由 nonoka-sub-cloud 仓库的 `modal_compute/tests/test_ct2_source_build.py` 钉住。

### 4.2 镜像构建与验证
构建环境拉取源码、应用 13 个补丁并以 `-DWITH_FLASH_ATTN=ON` 编译 Linux CUDA wheel。构建末尾断言 `return_refine_paths`、`return_refine_weights` 与 `flash_attention_available()`，确保运行时能力完备。

flash attention 只在 compute capability ≥ 8.0 的卡上生效。
