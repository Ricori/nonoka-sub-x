# FineSub 引擎集成、质量锁与上游对齐

本文档记录 FineSub 算法引擎在 Nonoka X 中的集成规则：质量红线与质量锁、目录与元数据、本地改动的记录方式、以后升级上游时跟什么，以及 Patched CTranslate2 的构建。完整背景见 [转写引擎重构计划](transcription-engines-plan.md) 第 0 节与第 3 节。

---

## 1. 质量红线

**FineSub 原先的转写、纠错、翻译质量必须保住。** 编排可以拆、可以重组，但结果不能变。

1. **算法文件上锁。** `third_party/finesub/src/` 下除第 2 条的四处外，每个文件的 sha256 都记在 `third_party/finesub/QUALITY_LOCK.json`，由 `tests/test_finesub_quality_lock.py` 检查：哈希相同，文件不多不少。运行时锁、运行时清单和模型清单按内容钉在 `src/nonoka_x/resources/RUNTIME_PINS.json`，由 `tests/test_runtime_pins.py` 看住。
2. **只解锁四处**，它们是编排、入口和安装设施，不是算法：`finesub/stages.py`、`finesub/pipeline.py`、`finesub/speech/joint/`、`finesub_bootstrap/`。
3. **解锁的地方改完，FineSub 路径的结果必须和改之前一致**，由回归闸门 `scripts/quality_gate.py` 证明。比对标准（哪些产物逐字节、哪些按内容比）见计划第 0 节第 3 条。
4. **Nonoka 传给 FineSub 的参数和环境、装出来的运行环境内容，都与基线等价。**
5. **闸门不过就停下来报告**，不许为了过闸门去调参数、改算法文件或放宽比对标准。
6. 改上锁文件、改钉住的版本（包括升级上游）、更新 `QUALITY_LOCK.json`、重录闸门基线（`scripts/quality_gate.py --record`），都是单独的、需要用户明确批准的提交。

业务逻辑、任务状态与数据模型放在 Nonoka 侧的适配层与 Projector 里实现，不进引擎。

`.gitattributes` 把 `third_party/finesub/**` 标为 `-text`，任何平台检出的都是提交时的字节，哈希才可比。

---

## 2. 目录与元数据

### 2.1 目录结构
```text
third_party/finesub/
├── README.md
├── UPSTREAM.json           # 对应的上游仓库、tag、commit、版本与 engine_bundle_id
├── QUALITY_LOCK.json       # 上锁文件的 sha256
├── LOCAL_CHANGES.md        # 相对上游改过什么、为什么、何时可以去掉
├── LICENSE                 # 上游 GPL-3.0-or-later 开源协议（0.5.0 起，此前为 MIT）
├── LICENSES.json
├── PROMPT_LICENSE.md       # Prompt 模板的 CC BY-SA 4.0 协议
├── UPSTREAM_README.md      # 上游原始说明
├── pyproject.toml
├── VERSION                 # pyproject.toml 读取的动态版本号
└── src/
    ├── finesub/            # 核心转写流水线
    └── finesub_bootstrap/  # 运行时与模型下载管理器（计划 WP-R7 删除）
```

不要把机器上安装的 `site-packages/finesub` 复制到这里。

### 2.2 `UPSTREAM.json`
```json
{
  "commit": "7123055da0cd6e037d8e37feb53b82f9e428c5cb",
  "engine_bundle_id": "finesub-0.7.0+7123055da0cd",
  "engine_version": "0.7.0",
  "ref": "v0.7.0",
  "repository": "https://github.com/caca2331/finesub"
}
```
只有这五个字段。`commit`、`engine_version`、`engine_bundle_id` 被 worker、local_provider、桌面暂存工具和离线包脚本读取；它说明 vendor 基于哪个上游版本，不再承载任何校验哈希（「没被动过」由质量锁证明）。

---

## 3. 本地改动与上游对齐

### 3.1 本地改动
vendor 相对 `UPSTREAM.json` 记录的上游版本改过的地方，全部记在 [`third_party/finesub/LOCAL_CHANGES.md`](../third_party/finesub/LOCAL_CHANGES.md)：改了什么、为什么、何时可以去掉、动到哪些文件。之后对解锁文件的改动也记在那里。

### 3.2 以后升级上游时跟什么

| 类别 | 范围 | 升级时 |
|---|---|---|
| **关键算法** | 转写引擎：`speech/` 下除 `joint/` 外的全部，加 `media/`、`subtitles/`、`text.py`。纠错层与翻译层：`llm/stages/`、`llm/prompt_templates/`，以及 `llm/` 里决定提示词、分窗、回答解析、后处理的文件（逐文件清单见 `scripts/finesub_align.py` 的 `KEY_FILES`） | 跟上游。每次升级过回归闸门并由用户看过质量对比 |
| **其余** | 编排、CLI、配置、`llm/agent/`、`llm/routing/`、LLM 调用设施、`llm/knowledge/`（渲染进纠错窗口的几个文件除外，见 `KEY_FILES`） | 默认不跟。要跟也可以，同样受质量红线约束 |
| **不再存在** | `finesub_bootstrap/`（WP-R7 之后） | 不跟。上游若改了引擎对它的用法，由 `tests/test_runtime_pins.py` 的依赖面测试发现，在兼容包里补上 |

`scripts/finesub_align.py diff --ref <tag>` 取上游 `<tag>` 和 `UPSTREAM.json` 记录的 commit，只在关键算法路径上报告三类文件：上游在两个版本之间改了的；本地相对记录版本改过的；两边都改了、升级时要手工合的。它只出报告，不改任何文件。两份上游源码默认从 GitHub 下载源码包；`--base-dir`、`--upstream-dir` 可以指向已解压的上游仓库根目录（含 `src/finesub`），离线也能跑。

### 3.3 常用命令
```bash
# 上锁文件是否被动过
python -m pytest -q tests/test_finesub_quality_lock.py

# 回归闸门（真实样本加 --real，只能在有 test_data/ 的主检出里跑）
python scripts/quality_gate.py

# 某个上游版本在关键算法路径上改了什么
python scripts/finesub_align.py diff --ref v0.8.0
```

### 3.4 下载兜底与离线版

> **GitHub 文件的兜底副本**：下载阶梯的最后一级是 `cloudfile.nonoka.online/github-mirror/<host>/<path>`（R2），覆盖运行时清单、模型清单、lock 与启动器用到的全部 GitHub 文件，以及 uv 下载的 Python。任何改变这些文件的操作（升级上游、升级 uv、换 FFmpeg）之后，都要在 `desktop/scripts` 下运行一次：
> `node publish-github-mirror.mjs --uv <对应版本的 uv>`（凭据读 `desktop/.env.release`，已存在且大小相同的文件会跳过）。

> **离线版**：exe 旁边的 `offline-pack/`（或 `NONOKA_OFFLINE_PACK` 指向的目录）排在下载阶梯最前面，缺的文件照常在线下载。文件按内容存放，不绑定版本：`files/<sha256>/`（工具、wheel、separator）、`python/<tag>/`（uv 的镜像结构）、`launcher/`（启动器 wheel）、`huggingface/<repo>/<revision>/`（整个快照）、`unverified/`（没有摘要的小文件）。在 `desktop/scripts` 下构建（`--models` 指向一份已装好模型的 `finesub/models`，产物默认在 `desktop/bin/offline/`）：
> `node build-offline-package.mjs --uv <对应版本的 uv> --models <models 目录> --exe "../bin/Nonoka Sub X.exe"`
> 版本更新时加 `--since <旧的 offline-pack>` 只输出新增文件，合并进旧包并换上新 exe 即可。

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
