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
├── 0001-*.patch … 0008-*.patch   # 补丁栈，按文件名顺序应用（当前 8 个，编号连续）
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
  "ref": "v0.7.0",
  "commit": "7123055da0cd6e037d8e37feb53b82f9e428c5cb",
  "archive_sha256": "...",
  "engine_version": "0.7.0",
  "synced_at": "2026-11-09T14:04:41Z",
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
| `0002-runtime-resource-management.patch` | `src/finesub_bootstrap` | Nonoka X 没有独立的资源系统（所有托管二进制都在 `finesub/runtime/<id>/` 下由 `managedtools` 解析），可安装什么由这份清单决定，与上游的差异都在这个补丁里，分三部分。**下载工具**：桌面端视频下载器需要 aria2c、node 与 pot-provider，上游清单未声明，只能在此声明才可安装。三者只在一起才有用：YouTube 是逐道设卡的，缺一件会在更早的一道上失败。**移除托管 Git**：知识库已是 SQLite，运行时不再需要 MinGit；保留它只会多一个下载目标、进诊断、并把它的目录塞进每个 worker 的 PATH。**固定 FFmpeg**：上游跟随 BtbN 每天重建的 `latest` tag，摘要在安装时现查。GitHub 文件在 R2 上有一份兜底副本（见本表后的说明），会变的字节无法校验，所以固定到一个月末 autobuild（这类 tag 不随每日构建清理）。换版本时改这个补丁，并重新运行 `publish-github-mirror.mjs`。此前装的 `n9.0-latest` 继续可用（`RuntimeProvisioner._unpinned_ffmpeg`），下次安装运行环境时才换成固定版本 | 三部分分别移除，都移除后删掉补丁。**下载工具**：上游清单收录这三项工具。**移除托管 Git**：上游移除托管 Git。**固定 FFmpeg**：不再需要自托管 FFmpeg |
| `0003-aoti-toolchain-portability.patch` | `src/finesub` | 让 AOTI 在两种上游没覆盖的工具链上编得过，只决定编不编得成，不影响分离结果。**MSVC `/std:c11`**：Windows 下 Triton 生成的 C11 启动器代码（`__triton_launcher.c`）包含空结构体初始化 `CUlaunchAttribute clusterAttr = {};`，MSVC 报 `error C2059: 语法错误: '}'`，且 Windows SDK `winbase.h` 产生 `warning C5105`；`_patch_triton_windows_driver` monkey-patch Triton 的启动器生成与构建命令，将 `{}` 替换为合法的 `{0}` 并插入 `/wd5105` 屏蔽告警。**POSIX**：上游的工具链探测是**有意** Windows-shaped（`_activate_msvc` / `cxx_toolchain_available`），因为 AOTI 这条路只在 Windows 上跑过。云端容器是 Linux，没有它每次构建都抛「requires MSVC」、被 `vocal_accel.install` 接住降级为 eager，云上每个 ≥270s 的任务都在无加速下跑而无人察觉；`_posix_cxx_driver` 让探测在 POSIX 上认系统 C++ 编译器 | 两半分别移除，都移除后删掉补丁。MSVC 部分：上游 Triton 修复 C11 下的空结构体初始化或上游 FineSub 内置该适配。POSIX 部分：上游支持 Linux 生产链路（0.5.1 已明确判定这是下游的移植需求，不收） |
| `0004-pinned-knowledge-subjects.patch` | `src/finesub/llm` | 桌面端允许用户手动多选知识主体，读取与写回共用。上游只在有东西来要时才注入词条（背景信息里的关键词、准备任务的判断、窗口的 `<requested_entries>`），选中的主体仍可能不进任何窗口。本补丁新增 `knowledge_subjects`：0.6.0 把纠错收敛成一个 `CorrectionRequest`，它成为其中一个字段，`run_pipeline` 照样按扁平关键字接收再打包；`llm/knowledge/subjects.py` 按限定名解析（找不到时直接报错），把词条渲染成一个受预算约束的块，与风格块一起放进每个纠错窗口（`read` 与 `update` 都注入）；任务后的知识更新把它们固定（`pinned`）在每个分块 `<kb_entries>` 的最前面。纯增量：不传这个参数时所有路径都与上游一致 | 上游支持按任务静态指定知识主体，用于注入与写回 |
| `0005-agy-compatibility.patch` | `src/finesub/llm/agent` | agy 1.3 起，`--project` 指向一个它已不认识的 id（`~/.gemini/config/projects` 下的记录没了）时会静默改开 `default-cli-project`，退出码仍为 0、仍列出 cwd 的 hooks。引擎的 `/hooks` 项目检查因此一直通过，缓存的 `finesub-project.json` 永不失效，每次工具调用都死在 `_grant_permissions`。本补丁让 `_check_project` 从本次日志读出 agy 实际打开的项目（`Conversation using project ID:`），不一致即视为不可用，交给既有的快速路径重新注册；刚注册完仍不一致则直接失败。agy 1.4 的 `/hooks` 日志不再输出这一行，所以检查还要求缓存 id 在 `~/.gemini/config/projects` 下有记录，缺失同样重新注册；记录目录整个不存在时不做判断。守卫编码部分上游 0.7.0 已移植 | 上游自行核对实际打开的项目，或 agy 对未知 `--project` 改为报错 |
| `0006-lazy-asr-import-in-stages.patch` | `src/finesub` | 桌面端从云端交付的 `stable.json` 续跑时，人声与识别阶段都不会启动，启动环境也只装了 `[harness]`。上游 0.7.0 已把这些 import 从模块顶层移走，但 `run_pipeline` 和 `_run_vocal_stage` 的函数体开头仍无条件 import 识别与分离（要 numpy、torch），`marks.row_marks` 也在判断证据可用之前就 import numpy。本补丁把这三处挪到真正要用的分支里；需要识别的运行导入同样的模块，算出来的东西不变；云端共用无妨 | 上游的续跑路径不再依赖 numpy、torch |
| `0007-llm-wait-visibility.patch` | `src/finesub/llm` | 2026-10-29 一个桌面任务在路由提示和第一个纠错窗口之间 29 分钟没有任何日志。本补丁让 `_call_and_parse` 在每轮研究开始等待时、以及回复到达后各记一行 `debug`；解析失败且还要重试时发 `research-parse-retry` 警告；`hedge_on_silence` 在调用未返回期间每 5 分钟记一行 `llm api call still waiting`，带上流式进度（`StreamProgress.describe`）。调用本身、超时与结果都不变；云端共用无妨。桌面端另外放行耗时 ≥ 60 秒的成功 `llm api call` | 上游自己报告进行中的调用与研究轮次 |
| `0008-llm-stream-reply-timeout.patch` | `src/finesub/llm` | 上游只按「有没有收到字节」判断流式调用，keep-alive 也会清零计时，中转挂住时要等到硬超时（默认预算约 46 分钟）；而硬超时又是总时长上限，会切断还在正常生成的调用。本补丁改成看真回复：答案文本和思考内容算回复，keep-alive 注释、空 delta、只带 usage 的事件不算。10 分钟没有回复发备份请求（`silence_seconds=min(soft, 600)`）；备份发出后两条都连续 10 分钟没有回复就一起断开并抛 `StreamInterrupted`（上游按类型重试），任何一条收到回复这 10 分钟重新计；回复还在到达时顺延硬截止。只作用于 LLM 生成调用，网页检索和本地 agent CLI 不变；云端不走这条路径 | 上游按真回复判断流式调用是否挂住，且不因总时长切断仍在回复的调用 |

> **GitHub 文件的兜底副本**：下载阶梯的最后一级是 `cloudfile.nonoka.online/github-mirror/<host>/<path>`（R2），覆盖运行时清单、模型清单、lock 与启动器用到的全部 GitHub 文件，以及 uv 下载的 Python。任何改变这些文件的操作（引擎同步、升级 uv、换 FFmpeg）之后，都要在 `desktop/scripts` 下运行一次：
> `node publish-github-mirror.mjs --uv <对应版本的 uv>`（凭据读 `desktop/.env.release`，已存在且大小相同的文件会跳过）。

> **离线版**：exe 旁边的 `offline-pack/`（或 `NONOKA_OFFLINE_PACK` 指向的目录）排在下载阶梯最前面，缺的文件照常在线下载。文件按内容存放，不绑定版本：`files/<sha256>/`（工具、wheel、separator）、`python/<tag>/`（uv 的镜像结构）、`launcher/`（启动器 wheel）、`huggingface/<repo>/<revision>/`（整个快照）、`unverified/`（没有摘要的小文件）。在 `desktop/scripts` 下构建（`--models` 指向一份已装好模型的 `finesub/models`，产物默认在 `desktop/bin/offline/`）：
> `node build-offline-package.mjs --uv <对应版本的 uv> --models <models 目录> --exe "../bin/Nonoka Sub X.exe"`
> 版本更新时加 `--since <旧的 offline-pack>` 只输出新增文件，合并进旧包并换上新 exe 即可。

> 上游 **0.7.0** 收走的补丁如下（这一段用的是当时的旧编号；之后补丁栈重排为 0001–0008）：**0009**（占位答案，`output_protocol._with_placeholder`）；**0012**（顺序读 ASR 交付，`separation.stream_asr_frames`；上游称 48 kHz 素材只差浮点末位）；**0007 的守卫编码**（`sys.stdin.buffer`）；**旧 0013 的流式读取与网络错误重试**（所有 OpenAI 兼容调用走流式，`api_http.RETRYABLE_TRANSPORT` 按类型重试）；**0016**（tokcount → countTokens，连接 5 秒、读取 15 秒 → 估算并告警，连续 30 秒不可达后直接估算；桌面端 tokcount 仍必装）。**旧 0013 的冷却等待**有意对齐上游而删除，回到 3 次粘性重试 + 20 分钟跳过期：只配了单 key 的用户在 key 冷却或主机被判不可达时任务会直接失败，需要手动续跑。
>
> 0.7.0 带来的适配层变化（不是补丁）：模型目录由 `model_catalog.psv` 改为 `model_catalog.toml`（桌面端生成，启动时清掉旧文件）；环境变量 `FINESUB_STATE_DIR` 改名 `FINESUB_STATE_FILE`；每个模型只有一个 target，联网搜索由模型决定，target 名去掉 `-completion`、`-media`、`-native` 中缀；agy 的 Gemini 只留 3.8 Flash；说话人标签写进 `-diar-rows.json`，`projector` 改走 `speech.diarization.product.read_product`，切行（`diarize_split`，默认开）与 `judge-audio` 的 `<id>b`、`<id>i` 由此对上；`quality` 档不写 `--diarize` 时默认开裁判，桌面端显式传 `off`；校对（pass 3）跟随上游，纠错路由是 Nonoka Cloud 时关闭（`settings.correction_proofread`）；代理策略改挂 `finesub.api_http`，引擎拒绝 SOCKS，自定义 SOCKS5 时引擎 API 改走直连并告警；识别后补救（`squeeze_repair`、`gap_fill`）本地默认开、云端关闭。后续接入见 [finesub-0.7-followups.md](finesub-0.7-followups.md)。

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
python scripts/sync_finesub.py sync --ref v0.7.0
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
