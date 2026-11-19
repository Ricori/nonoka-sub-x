# FineSub 引擎集成、质量锁与上游对齐

本文档记录 FineSub 算法引擎在 Nonoka X 中的集成规则：质量红线与质量锁、目录与元数据、密闭启动规范、本地改动的记录方式、以后升级上游时跟什么，以及 Patched CTranslate2 的构建。

---

## 1. 质量红线

**FineSub 原先的转写、纠错、翻译质量必须保住。** 编排可以拆、可以重组，但结果不能变。

1. **算法文件上锁。** FineSub 分装在三个引擎包里（见 2.1）。每个包 `src/` 下除第 2 条的两处外，每个文件的 sha256 都记在该包的 `QUALITY_LOCK.json`，由 `tests/test_finesub_quality_lock.py` 检查：哈希相同，文件不多不少，而且每一条都等于分包前基准锁（`tests/engine-packages/vendor-quality-lock.json`）里同一路径的那一条。分包前的 550 个上锁文件，每个要么在某个包里，要么列在 `tests/engine-packages/not-carried.json` 并写明不带原因（`tests/test_engine_packages.py`）。运行时锁、运行时清单和模型清单按内容钉在 `src/nonoka_x/resources/RUNTIME_PINS.json`，由 `tests/test_runtime_pins.py` 看住。
2. **只解锁两处**，它们是编排和安装设施，不是算法：`finesub/stages.py`、`finesub_bootstrap/`（知识库包的 18 处知识库访问点按协议隔离，仍受 `QUALITY_LOCK.json` 看住）。
3. **解锁的地方改完，FineSub 路径的结果必须和改之前一致**，由回归闸门 `scripts/quality_gate.py` 证明。默认逐字节比对；仅以下本身含时间戳/随机流号的产物按内容比对：`-stable.json` / `-aligned.json`（除顶层 `metadata` 外逐项一致）、`-vocal.ogg`（解码采样一致）、`-vad.json`（去掉 `source.mtime_ns`、顶层 `timing` 与 `*_sec` 后逐项一致）。
4. **Nonoka 传给 FineSub 的参数和环境、装出来的运行环境内容，都与基线等价。**
5. **闸门不过就停下来报告**，不许为了过闸门去调参数、改算法文件或放宽比对标准。
6. 改上锁文件、改钉住的版本（包括升级上游）、更新 `QUALITY_LOCK.json`、重录闸门基线（`scripts/quality_gate.py --record`），都是单独的、需要用户明确批准的提交。

业务逻辑、任务状态与数据模型放在 Nonoka 侧的适配层与 Projector 里实现，不进引擎。

`.gitattributes` 把 `engines/*/src/**` 标为 `-text`，任何平台检出的都是提交时的字节，哈希才可比。

---

## 2. 目录与元数据

### 2.1 目录结构

FineSub 按 `tests/engine-packages/*.json` 清单分装为三个独立引擎包，每个包在各自的隔离进程中运行：

```text
engines/
├── finesub-transcribe/       # FineSub 转写引擎（Python 层解释器）
│   ├── PACKAGE.json          # 标识、种类、入口、就绪声明、要的密钥名、GPU 档位
│   ├── adapter/              # 包自己的代码：finesub_transcribe/（入口、桥接、协议）、finesub_knowledge/（知识库协议的客户端）
│   ├── src/
│   │   ├── finesub/          # 转写那一半的 FineSub 文件
│   │   └── finesub_bootstrap/ # 原件；缺模型时报错、不再自己下载
│   ├── UPSTREAM.json         # 对应的上游仓库、tag、commit、版本与 engine_bundle_id
│   ├── QUALITY_LOCK.json     # 本包上锁文件的 sha256
│   ├── LOCAL_CHANGES.md      # 相对上游改过什么、为什么、何时可以去掉
│   ├── LICENSE               # 上游 GPL-3.0-or-later 开源协议（0.5.0 起，此前为 MIT）
│   └── LICENSES.json
├── finesub-correct/          # finesub correction（启动器解释器）
│   ├── …                     # 同上，另有 PROMPT_LICENSE.md（Prompt 模板的 CC BY-SA 4.0 协议）
│   ├── adapter/              # finesub_correct/、finesub_knowledge/（客户端）
│   └── src/finesub_bootstrap/ # 兼容包：只有纠错那一半 import 的名字，位置取自启动上下文
└── finesub-knowledge/        # 知识库包（启动器解释器）
    ├── …                     # 同上，另有 PROMPT_LICENSE.md
    ├── adapter/              # finesub_knowledge_host/、finesub_knowledge/（服务端与管理）
    └── src/finesub_bootstrap/ # 兼容包：知识库代码 import 的名字
```

两个包都带的 FineSub 文件逐字节相同，三个包各带一份的 `bridge.py`、`wire.py`、`failures.py` 也逐字相同（`tests/test_engine_packages.py`）。每个包只留核心代码与元数据：没有 `pyproject.toml`、`VERSION`、README，版本号以 `UPSTREAM.json` 为准；包目录里不同时有 `pyproject.toml` 和 `src/finesub`，引擎也就不会把它当成源码检出。各包文件清单由 `scripts/engine_packages.py` 生成并保存在 `tests/engine-packages/*.json`。

不要把机器上安装的 `site-packages/finesub` 复制到这里。

### 2.1a 包进程怎么启动（密闭运行）

核心的 `nonoka_x.engine_host` 拉起每个包的进程：

- **环境从零构建，不继承**。分类表分两半：中性的一半在 `engine_host.CLASSIFICATION`（系统变量、`PATH`、`PYTHON*`、代理、沙箱目录），引擎自己读的一半在各包 `bridge.py` 的 `CLASSIFICATION`（`FINESUB_*`、模型库的变量、密钥）。每个名字归三类之一：「系统必需」按名从父进程原样带；「Nonoka 设定」由核心或桥接无条件写；其余一律不存在。`PATH` 只有工具层里 ffmpeg / ffprobe / tokcount 的目录、纠错进程另加本机探测到的本地 Agent CLI 目录（npm 装的 CLI 再加 node 的目录，带 Python hook 的 `agy` 再加启动器 Python 目录），再加由 `SystemRoot` 推出的系统目录；就绪检查也只认工具层。代理总是显式写：有代理写 `HTTP(S)_PROXY`，没有写 `NO_PROXY=*`。
- **解释器隔离启动**：`python -I`（转写进程另加 `-B`）`-c <引导>`，引导代码先把 `sys.path` 设成包自己的两个根（`adapter`、`src`）加解释器自己的库、把三个标准流改成 UTF-8，再以 `__main__` 运行入口模块。`PYTHONPATH`（同样只有包的两个根）与 `PYTHONNOUSERSITE=1` 只为引擎自己拉起的孙进程（MCP 服务）而设。
- **转写进程在沙箱里**：`USERPROFILE`、`HOME`、`APPDATA`、`LOCALAPPDATA` 指向 `<数据目录>\engines\finesub-transcribe\sandbox\home\`（跨运行共用；`HOMEDRIVE`、`HOMEPATH` 也拆成这个目录的驱动器与路径，用户 2026-11-18 定），`TEMP`、`TMP` 指向 `sandbox\temp\<进程>\`（每个进程一份，里面的锚点文件 `!!nonoka-temp-anchor` 由启动方加锁开着，进程结束后整个删掉；启动时清掉没有活进程的残留）。当前目录是任务目录；纠错和知识库进程的当前目录是它自己的 run 目录。纠错、知识库进程不进沙箱，用真实的用户目录；本地 Agent CLI 的配置与证书变量只带给纠错进程（D16）。
- `CUDA_VISIBLE_DEVICES`：请求是 `cuda:N` 时由转写适配器在调 `run_transcription` 之前无条件设成 `N`，其余情况不存在；父进程的值从不带进来。

证明在 `tests/test_engine_hermetic.py`（覆盖扫描、投毒、读取审计）、`tests/test_engine_sandbox.py`（沙箱、锚点、残留、两进程并发、`-I -B`），以及 `scripts/quality_gate.py --real` 的 `sandbox` 一项（运行前后对沙箱外的受保护位置做快照比对）。

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
只有这五个字段，三个包里的相同。它说明包里的 FineSub 基于哪个上游版本，不再承载任何校验哈希（「没被动过」由质量锁证明）；核心读的是 `PACKAGE.json` 的 `upstream`（与它一致）。

---

## 3. 本地改动与上游对齐

### 3.1 本地改动
包里的 FineSub 相对 `UPSTREAM.json` 记录的上游版本改过的地方，全部记在每个包的 `LOCAL_CHANGES.md`（三份相同，例如 [`engines/finesub-correct/LOCAL_CHANGES.md`](../engines/finesub-correct/LOCAL_CHANGES.md)）：改了什么、为什么、何时可以去掉、动到哪些文件。之后对解锁文件的改动也记在那里。

### 3.2 以后升级上游时跟什么

| 类别 | 范围 | 升级时 |
|---|---|---|
| **关键算法** | 转写引擎：`speech/` 下除 `joint/` 外的全部，加 `media/`、`subtitles/`、`text.py`。纠错层与翻译层：`llm/stages/`、`llm/prompt_templates/`，以及 `llm/` 里决定提示词、分窗、回答解析、后处理的文件（逐文件清单见 `scripts/finesub_align.py` 的 `KEY_FILES`） | 跟上游。每次升级过回归闸门并由用户看过质量对比 |
| **其余** | 编排、CLI、配置、`llm/agent/`、`llm/routing/`、LLM 调用设施、`llm/knowledge/`（渲染进纠错窗口的几个文件除外，见 `KEY_FILES`） | 默认不跟。要跟也可以，同样受质量红线约束 |
| **安装设施** | `finesub_bootstrap/`：转写包带原件（已删自下载），纠错包与知识库包带兼容包 | 不跟。上游若改了引擎对它的用法，由 `tests/test_runtime_pins.py` 的依赖面测试与 `tests/test_engine_packages.py` 的名单测试发现，在原件或兼容包里补上 |

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

### 3.4 下载线路与离线版

> **下载线路**：桌面端和本地服务的每个下载都走同一条阶梯：离线包（有就用）→ 经代理（设置页的自定义代理，没有则系统代理）从官方源 → 直连官方源 → 报错提示开代理。**没有镜像、不按地区选线路**：官方源慢的地方靠用户自己的本地代理。Go 侧是 `desktop/internal/app/download_route.go`（启动器 Python），Python 侧是 `nonoka_x.runtime.fetch` 与 `nonoka_x.download_route`。运行环境的三层按 `src/nonoka_x/resources/layers.json` 下载与校验。

> **离线版**：exe 旁边的 `offline-pack/`（或 `NONOKA_OFFLINE_PACK` 指向的目录，里面有 `pack.json`）排在下载阶梯最前面，缺的文件照常在线下载。内容：
> - 三层的清单与压缩包，路径与 R2 上相同：`v1/<层>/<清单 sha256>.json`、`v1/<层>/<压缩包 sha256>.tar.zst`。层管理器（`nonoka_x.runtime.layers`）按 `layers.json` 找它们，大小对上就原地读、不复制、不删，装完照常按清单核对。
> - 启动器 Python 要的三样（断网机器要先有它才能跑本地服务，层是本地服务装的）：`files/<sha256>/`（uv）、`python/<tag>/`（uv 安装的解释器，uv 的镜像结构）、`launcher/`（启动器 wheel）。
>
> 文件按内容存放，不绑定版本，新文件可以直接合并进旧包。在 `desktop/scripts` 下构建，产物默认在 `desktop/bin/offline/`：
> `node build-offline-package.mjs --uv <对应版本的 uv> --exe "../bin/Nonoka Sub X.exe"`
> 层默认按 `src/nonoka_x/resources/layers.json` 从 R2 下载；`--layers <dist\layers>` 改从本机构建产出（`scripts/build_layers.py`）复制。两种来源都按 `layers.json` 的大小和 sha256 校验，所以包里的层文件与 R2 上同版本的逐字节相同。`--layers-only` 只放三层；不给 `--exe` 只写 `offline-pack/`。版本更新时加 `--since <旧的 offline-pack>` 只输出新增文件，合并进旧包并换上新 exe 即可。约 5.8 GB。

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
