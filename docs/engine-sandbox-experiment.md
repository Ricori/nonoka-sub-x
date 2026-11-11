# 预制环境包 + 进程级沙箱：两个前提的实验报告

日期：2026-11-11。机器：本机（RTX 3070，sm86，torch 2.11.0+cu128）。
实验脚本和产物都在 `.tmp/sandbox-experiment/`（不入库），下文的路径都相对于它，除非另有说明。
没有改动仓库里任何已有文件，也没有改动已装的运行时、模型、`%APPDATA%\Nonoka X`、`%LOCALAPPDATA%\FineSub`（见 1.3 的快照证据）。
本报告里没有任何真实密钥，密钥实验只用了假值。

## 结论速览

| 前提 | 结论 | 一句话 |
|---|---|---|
| 一：沙箱里原样带未改动的 `finesub_bootstrap`，引擎能跑、结果与基线一致 | **有条件成立** | 两个样本在沙箱里跑到 `raw-srt`，全部产物按第 0 节第 3 条与基线一致；引擎和 `finesub_bootstrap` 的写入没有出沙箱。条件有三：要绕过 torch 2.11 在 Windows 上「清空 `%TEMP%`」的缺陷（见 1.2.1）；要有现成的 AOTI 产物（本机没有 MSVC，没法验证重新编译，没有 AOTI 时结果与基线**不同**，见 1.2.2）；引擎源码目录要么可写，要么预先编译好 `.pyc` |
| 二：可重定位的 Python 装出的包与现在的 venv 逐文件相同，搬目录后仍能用 | **有条件成立** | 28286 个文件逐字节相同。实质内容的差异只有一处：锁里的 `onnx` 和 `onnx-weekly` 往同一个 `onnx/` 目录写文件，最后留下谁的文件取决于安装顺序，现在的 venv 本身也只是其中一种随机结果。搬到带空格的新路径后不做任何修补，import 和两个样本都通过；两个补丁版本（3.12.1、3.12.16）的结果都与基线一致 |

用户在实验过程中定了一条（2026-11-11）：**以后直接删掉 `finesub_bootstrap` 的自下载行为。** 所以「缺模型时它会怎么做」没有实测（见第 4 节）。

---

## 1. 前提一：沙箱里原样带 `finesub_bootstrap`

### 1.0 做法

- 驱动脚本 `scripts/sandbox_run.py`：import `tests/quality_harness/real.py`（没有改它），沿用它的请求参数（`SAMPLES`）、`_worker_launch` 构建的命令与环境（死代理、`HF_HUB_OFFLINE=1`、新建的配置目录），以及 `compare_runs` 比对与 `ACCEPTED_STANDARDS` 判定。在这之上做了这些改动：
  - worker 环境里的 `USERPROFILE`、`HOME`、`APPDATA`、`LOCALAPPDATA`、`TEMP`、`TMP` 指向 `sandbox/<轮次>/user/...` 下新建的空目录；
  - `FINESUB_MODEL_DIR`、`HF_HOME`、`TORCH_HOME` 指向模型副本 `models/copy`（用户选的方案：完整复制已装模型，保留 HF 缓存的相对符号链接和修改时间，`scripts/copy_tree.py`）。这样已装的模型目录一个字节都不碰；
  - MSVC 探测（`local_provider._prepare_msvc_environment`）改在沙箱化之后的环境上运行，不用真实用户目录；
  - 可选：换引擎源码目录（`--vendor`）、换解释器（`--python`）、TEMP 锚点（`--anchor`）、TEMP 用真实目录（`--real-temp`）。
- 写入位置用快照比对：每次运行前后各做一次文件清单（大小 + 修改时间）快照，`scripts/snap.sh`、`scripts/snapshot.py`。完整遍历的位置有：`%LOCALAPPDATA%\FineSub`、`~\.cache`、`~\.finesub`、`~\.gemini`、`%APPDATA%\Nonoka X`、`%APPDATA%\NVIDIA`、`%LOCALAPPDATA%\NVIDIA`、真实 `%TEMP%`、`%APPDATA%\Python`、`third_party/`、`src/`、已装运行时与模型的根 `D:\AIProject\nonoka-sub-envir\Nonoka X\finesub`、基础解释器 `D:\Envir\Python312`。另外两层深地列出用户目录、`%APPDATA%`、`%LOCALAPPDATA%`、`C:\ProgramData`，用来发现别处冒出来的新目录。一次快照约 23 万个文件、9 秒。
- 基线是 `test_data/quality-baseline/real/<sample>/run-1`（WP-Q 录的，aoti，sample_ja 57–60 秒，sample_multi_person 51–53 秒）。

### 1.1 各阶段是否正常，产物是否与基线一致

所有轮次的 worker 都正常退出（exit 0）。torch 加载、模型加载、人声分离、识别、声学说话人分离（`sample_multi_person`）都跑完了。

| 轮次 | 配置 | 加速 | 耗时 ja / multi（秒） | 产物判定 |
|---|---|---|---|---|
| s1 | 沙箱，模型副本**不带** accel | **eager** | 76.4 / — | **全部不同**（见 1.2.2） |
| s2 | 沙箱，带 accel，TEMP 在沙箱、无锚点 | aoti 加载失败 | 卡死 | 无产物（见 1.2.1） |
| s3 | 沙箱 + TEMP 锚点（方案 A） | aoti | 73.9 / 61.9 | 一致 |
| s4 | 沙箱，只有 TEMP/TMP 用真实目录（方案 B） | aoti | 64.7 / 55.6 | 一致 |
| s5 | 同 s3，引擎源码换成不含 `pyproject.toml` 的副本 | aoti | 71.5 / 61.3 | 一致 |
| s6 | 同 s3，解释器换成 PBS 3.12.1 环境（前提二） | aoti | 109.8 / 58.5 | 一致 |
| s7 | 同 s3，解释器换成 PBS 3.12.16 环境（前提二） | aoti | 100.2 / 54.3 | 一致 |
| s8 | 同 s3，解释器换成搬动并改名后的 PBS 3.12.1 环境（前提二） | aoti | 64.1 / 55.1 | 一致 |

「一致」的逐产物明细（s3–s8 每一轮、每个样本都一样，汇总见 `runs/<轮次>-<sample>/summary.json` 的 `comparison`）：

| 产物 | 判定 |
|---|---|
| `-raw.srt` | 逐字节相同 |
| `-vad-energy.npz` | 逐字节相同 |
| `-diar.json`、`-diar-rows.json`、`-diar-evidence.npz`（只有 multi_person） | 逐字节相同 |
| `-stable.json`、`-aligned.json` | 字节不同，顶层 `metadata` 以外相同（已批准的标准） |
| `-vad.json` | 字节不同，去掉 `source.mtime_ns` 和耗时后相同（已批准的标准） |
| `-vocal.ogg` | 字节不同，解码出的采样相同（已批准的标准） |

没有任何产物落在已批准标准之外。s6、s7 的 ja 慢了约 40 秒，原因是新环境第一次运行时要编译 `.pyc`；s8 是同一个 3.12.1 环境的第二次运行，回到 64 秒。

### 1.2 AOTI 加速

#### 1.2.1 torch 2.11 在 Windows 上会清空 `%TEMP%`（新发现，决定沙箱怎么做 TEMP）

- 现象（s2）：沙箱 TEMP 是空目录，人声分离一开始就报 `Error: CUDA driver error: file not found`，worker 停在那里不动（CPU 时间不再增长），只能杀掉。沙箱 TEMP 里有两个 AOTI 包只解压了一部分：`frequency_b0` 22 个文件剩 5 个，`band_split_single` 76 个剩 13 个，剩下的恰好是已经加载进进程、被占用的 `.pyd` 和部分 cubin。
- 定位（`scripts/aoti_temp_sentinel.py`，只用已装运行时的 torch，脱离引擎单独测）：在一个新 TEMP 里放几个哨兵文件，加载一个 AOTI 包，再销毁加载器对象。结果是 **TEMP 下所有能删的文件都被删了**，删除碰到第一个被占用的文件就停下（按目录顺序：放一个被打开着的 `m_locked.txt` 时，排在它前面的 `a_dir/` 被删了，它和排在后面的都还在）。也就是说，`AOTIModelPackageLoader` 在 Windows 上把 `%TEMP%` 本身当成自己的临时目录，析构时对它做 `remove_all`。引擎的 `load_packages`（`finesub/speech/preprocessing/separator/separator_aoti.py:670`）注释里说加载器「按归档的顶层前缀解压」，解压目录就是 `%TEMP%\time_b0`、`%TEMP%\frequency_b0` 这些固定名字。
- 为什么基线没出事：真实 `%TEMP%` 里有很多被其他程序占用的文件，`remove_all` 很早就停住了。代价是**现在每次真实运行都可能删掉用户 TEMP 里排在前面、恰好没被占用的文件**。s4（TEMP 用真实目录）这一轮快照里真实 TEMP 没有文件被删，只改写了约 700 个 AOTI 解压文件；能不能删到东西取决于当时 TEMP 里的内容。
- 两种绕过办法都实测过（用户选了两种都跑）：
  - **方案 A：锚点文件（s3、s5–s8）**。启动进程在沙箱 TEMP 里先建一个排序最前的 `!!nonoka-temp-anchor`，并在 worker 运行期间一直开着。Python 在 Windows 上 `open()` 不给删除共享，所以 worker 删不掉它，`remove_all` 从第一项就停住。全部通过，沙箱之外零写入。
  - **方案 B：只有 TEMP/TMP 用真实目录（s4）**。结果一致，但真实 TEMP 里会留下或改写约 700 个 AOTI 解压文件（20 MB 左右），而且「清空 TEMP」的风险原样保留。
- 另外两个和 TEMP 有关的事实：解压目录名是固定的（`time_b0` 等），所以**两个引擎进程共用同一个 TEMP 会互相覆盖**，每个进程应该有自己的 TEMP；解压出来的约 20 MB 在进程结束后留在 TEMP 里，不会被清掉。

#### 1.2.2 本机没有 MSVC，重新编译没法验证；没有 AOTI 时结果与基线不同

- 本机没有 Visual Studio：`C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe` 不存在，`2022\` 下是空目录。系统 `PATH` 里还留着 `...\2022\BuildTools\VC\Tools\MSVC\14.33.31629\bin\Hostx86\x64`，但这个目录已经不在了。现有的 AOTI 产物（`models\audio-separator\accel\v2-2.11.0+cu128-cuda12.8-sm86-rtx3070-6a790594\aoti\manifest.json`）的 `compiler` 字段指向 BuildTools 14.33，是以前编的，编译器后来被卸了。
- s1（模型副本不带 `accel\`）：引擎的 `select_backend`（`separator/accel.py:530`）看到没有现成的包、`aoti_buildable()` 也找不到工具链，直接选了 **eager**（`accel_requested` 就是 eager，没有尝试编译）。产物**全部与基线不同**，例如 `-raw.srt` 第 170 行的时间戳 `00:02:15,057` 变成了 `00:02:15,077`。所以「用户机器上没有 AOTI」不只是慢，而是**质量红线意义上的结果改变**。
- 按用户的决定，后面的轮次把已装的 `accel\` 目录复制进模型副本，测「能加载、结果一致」。结论：在沙箱里（方案 A/B）AOTI 产物能正常加载，`real.acceleration` 全部是 `{"accel": "aoti", "accel_requested": "aoti", "device": "cuda"}`。
- **没验证的**：重新编译能不能成功、首次编译要多久、第二次是否复用（需要装 VS 2022 Build Tools 的 C++ 工作负载后补测）。
- 编译缓存的位置（读代码与实测）：
  - AOTI 包：`<FINESUB_MODEL_DIR>\audio-separator\accel\<键>\aoti\`，键里含 torch、CUDA、sm、显卡型号（`separator/accel.py:166`）。**在模型目录里，不在用户目录里**，所以沙箱不影响它；预制模型包要么带上对应显卡的产物，要么接受在用户机器上编译（需要 MSVC）。
  - JIT 层的 inductor 缓存：引擎在 `TORCHINDUCTOR_CACHE_DIR` 没设时自己设到 `<accel键>\inductor`（`accel.py:369-374`）。本次是 aoti 层，没走到。
  - 每次运行引擎都会改写 `<accel键>\probe.json`（`aoti_checked_at`）。实测模型副本里只有这一个文件被改。**这说明用已装的模型目录跑会写进模型目录。**顺带：实验期间，已装模型目录里的这个文件在我第一次运行之前（2026-11-11 17:2x）被别的进程改过，不是本实验写的。
  - qwen 裁判的编译缓存走 `resolve_cache_dir()`（`finesub/paths.py:247`）：在检出模式下是 `<vendor>\cache`，非检出时是 `%LOCALAPPDATA%\FineSub\cache`（沙箱里就落进沙箱）。跑到 `raw-srt` 这两个样本时没走到，`third_party/finesub/cache` 一直不存在。
- MSVC 探测与用户目录：
  - Nonoka 的 `_prepare_msvc_environment`（`src/nonoka_x/local_provider.py:167`）在父进程里运行，按 `ProgramFiles(x86)` 找 vswhere，再跑 `vcvars64.bat`；引擎自己的 `_find_vcvars`（`separator_aoti.py:83`）用的是固定路径 `_VSWHERE`。两者定位工具链都**不看** `USERPROFILE`、`APPDATA`。
  - 本机在沙箱环境上运行探测，0 秒就返回了（vswhere 不存在），`msvc.json` 里 `found` 是空的。`vcvars64.bat` 在沙箱化的用户目录下能不能正常激活（它会碰 `%LOCALAPPDATA%` 等位置），本机没法测。

### 1.3 运行期间写到了哪里

**沙箱里出现的东西**（s3 的典型内容，每轮约 20 MB）：

| 位置 | 内容 | 由谁写、靠哪个变量 |
|---|---|---|
| `user\AppData\Roaming\NVIDIA\ComputeCache\` | CUDA 驱动的 JIT 缓存 | NVIDIA 驱动，跟随 `APPDATA` |
| `user\AppData\Local\Temp\{time_b*,frequency_b*,band_split_single,mask_estimator_single}\` | AOTI 包解压（约 700 个文件） | torch AOTI 加载器，`TEMP` |
| `user\AppData\Local\Temp\torch\kernels\` | NVRTC 编译的内核缓存 | torch，`TEMP` |
| `user\AppData\Local\Temp\torchinductor_<用户名>\` | 空目录 | torch inductor，`TEMP`；目录名取自 `USERNAME` |
| `user\AppData\Local\Temp\vocal_*` 等 | 人声分离的临时目录，空 | 引擎，`tempfile` |

沙箱里**没有**出现 `%LOCALAPPDATA%\FineSub`、`~\.cache`、`~\.finesub`：模型都在、`HF_HUB_OFFLINE=1`、`FINESUB_*` 路径都由 harness 显式给出时，跑到 `raw-srt` 用不到托管数据根。

**沙箱之外**：13 次运行（s1、s3–s8）的逐次快照比对里，上面列出的保护位置（`%LOCALAPPDATA%\FineSub`、`~\.cache`、`%APPDATA%`（含 `Nonoka X`、`NVIDIA`、`Python`）、`third_party/`、`src/`、已装运行时与模型、基础解释器）**新增 0、删除 0、修改 0**。从第一次运行前到最后一次运行后，整段时间的总比对也是 0（`snapshots/all-diff.json`，按上述位置过滤）。快照里出现的其他变化都是别的程序：Claude 桌面应用（`%APPDATA%\Claude`、`%TEMP%\claude\...`，包括 s6 期间它自己清掉的 1615 个 `bash-edit-diff` 文件）、`~\.grokbot`、QQ 电脑管家、雷神加速器，以及运行间隙里的 NVIDIA `DXCache`/`GLCache`（图形着色器缓存，不在任何一次运行的时间窗里）。

仍然写到沙箱之外的，逐个列出（都是预期的或可改向的）：

| 写入 | 谁写的 | 能否改向 |
|---|---|---|
| 任务目录 `runs/<轮次>/tasks/...`、派生配置 `runs/<轮次>/config/` | worker / harness | 本来就该在 Nonoka 管的目录里 |
| `<FINESUB_MODEL_DIR>\audio-separator\accel\<键>\probe.json` 每次改写 | `separator/accel.py`（读写 probe） | 由 `FINESUB_MODEL_DIR` 决定；预制模型包装上后会被改写，模型层不能当只读，或者在包里接受这个文件会变 |
| 引擎源码目录里的 `__pycache__\*.pyc` | Python 解释器 | 源码目录可写、pyc 过期或不存在时就写。s5（新复制的源码）写了 126 个 pyc 进副本目录；真实 vendor 里 pyc 都是新的，没写。改向：`PYTHONPYCACHEPREFIX`（`-I` 下不认，要用 `-X pycache_prefix=`），或者 `PYTHONDONTWRITEBYTECODE`，或者打包时预编译 |
| 方案 B 下：真实 `%TEMP%` 里的 AOTI 解压文件，外加「清空 TEMP」的风险 | torch | 用方案 A（TEMP 进沙箱 + 锚点）消除 |

**不在快照覆盖范围内、没有验证的**：注册表；`C:\Windows\Temp`；用 Known Folder API（`SHGetKnownFolderPath`，不看环境变量）找目录的原生库（这次的快照里没有看到这类写入，但两层深的列表只能发现新目录，发现不了已有目录深处被改的文件）。

### 1.4 密钥读取（假密钥，`scripts/secrets_probe.py`）

用已装运行时与未改动的 vendor，在沙箱用户目录下（`sandbox/sec`）测试：

| 测试 | 结果 |
|---|---|
| 写一个明文 `.env`（三个假值：`GEMINI_PAID`、`DEEPSEEK_API_KEY`、`OPENROUTER_API_KEY`），让引擎自己的读取路径 `api_keys.read_dotenv()` 去读 | 后端是 `DpapiBackend`；`ensure_protected` 把文件就地加密改写：出现 `FINESUB_KEYRING=finesub$kek$v1$...`，三个值都变成 `fs$` 令牌，文件里不再有明文；旁边多了 `finesub.env.lock`；读回的值和假值全部相等 |
| 另起一个进程，仍用沙箱用户目录读回 | 全部相等 |
| 另起一个进程，用**真实**用户目录读同一个文件 | 全部相等。DPAPI 绑定的是 Windows 账户（靠已加载的用户配置文件），与 `USERPROFILE`、`APPDATA` 变量无关 |
| 不写任何文件（`FINESUB_ENV_FILE` 指向不存在的文件），密钥只放在进程环境变量里 | `read_dotenv()` 返回空；`api_keys.key_entries()` 对三个名字都读到了假值（含经密钥池解析的 `GEMINI_PAID`）。证实调查文档 1.2 的说法 |

结果在 `reports/secrets-*.json`。注意：只要 `FINESUB_ENV_PROTECT` 没设成 `0`、文件里有明文，引擎就会改写这个文件。计划里「只走环境变量 + `FINESUB_ENV_PROTECT=0`」（D3、D22）在沙箱下照样成立。

### 1.5 引擎源码放在不含 `pyproject.toml` 的目录里

- 把 `third_party/finesub` 复制到 `vendor-nocheckout/`，去掉 `pyproject.toml` 和 `__pycache__`。`src/` 下 615 个文件的 sha256 与原件逐一相同。
- 检出判定：原 vendor 的 `paths.own_checkout_root()` 是 `D:\Development\nonoka-sub-x\third_party\finesub`，`resolve_cache_dir()` 是 `<vendor>\cache`；副本的分别是 `None` 和 `%LOCALAPPDATA%\FineSub\cache`（沙箱里就落进沙箱）。也就是说，检出模式的回落（`<检出>\cache`、`<检出>\bin\windows-amd64\tokcount.exe`、分离器加速缓存的检出位置）都不会再触发。
- s5 两个样本与基线一致，沙箱之外零写入。唯一的副作用是往副本源码目录里写了 126 个 `.pyc`（见 1.3）。

### 1.6 沙箱需要重定向的变量（实测有效的最小集合）

| 变量 | 指向 | 依据 |
|---|---|---|
| `USERPROFILE`、`HOME` | `<沙箱>\user` | Python 在 Windows 上的 `Path.home()` 用 `USERPROFILE`；`HOME` 没人读，一并设上无害 |
| `APPDATA` | `<沙箱>\user\AppData\Roaming` | NVIDIA ComputeCache 跟着它走 |
| `LOCALAPPDATA` | `<沙箱>\user\AppData\Local` | `finesub_bootstrap.paths.default_data_root()`（托管数据根、下载线路状态、日志、capsule）只看它 |
| `TEMP`、`TMP` | `<沙箱>\user\AppData\Local\Temp`，**每个进程一份**，并放锚点文件 | 见 1.2.1 |
| 模型变量 `FINESUB_MODEL_DIR`、`HF_HOME`、`TORCH_HOME` | 预制模型层 | 沿用现有做法 |

`HOMEDRIVE`/`HOMEPATH` 这次保留了真实值：Python 在有 `USERPROFILE` 时不看它们，快照里也没有因此漏出去的写入，但没有单独验证把它们也改向的效果（驱动脚本有 `--homepath` 开关，没用）。`USERNAME` 只决定 `torchinductor_<用户名>` 这个目录名。

---

## 2. 前提二：可重定位的 Python

### 2.0 做法（用户批准的方案）

- 解释器：python-build-standalone 两个构建，从 GitHub `astral-sh/python-build-standalone` 发布页下载，sha256 与发布方给的一致：
  - `cpython-3.12.1+20240107-x86_64-pc-windows-msvc-shared-install_only.tar.gz`（`fd5a9e0f...`，与现在的基础解释器同为 3.12.1；MSC v.1929）
  - `cpython-3.12.16+20261110-x86_64-pc-windows-msvc-install_only.tar.gz`（`e23fe5b1...`；MSC v.1944）
- 安装：用已装运行时里的 uv 0.11.32，`uv --no-config pip install --link-mode copy --python <pbs>\python.exe --requirement third_party/finesub/src/finesub_bootstrap/pylock.win-py312.toml`，**直接装进解释器自己的 `Lib\site-packages`**，不建 venv。`UV_CACHE_DIR` 设在 `downloads/uv-cache`。uv 按锁里的 sha256 校验每个 wheel。
  - 3.12.1：下载并准备 98 个包用了 6 分 57 秒，安装 28 秒（`logs/uv-install-3.12.1.log`）。PBS 自带的 setuptools 69.0.3 被换成锁里的 81.0.0。
  - 3.12.16：缓存全部命中，26 秒装完。
  - 下载来源：files.pythonhosted.org、download-r2.pytorch.org（torch/torchaudio/torchvision）、github.com/caca2331（ctranslate2），缓存共 5.1 GB。
- 逐文件比对：`scripts/compare_site.py`。在新环境**还没运行过**的时候，对两边所有文件做 sha256（结果在 `reports/site-3.12.1.json`、`reports/site-3.12.16.json`）。

### 2.1 `site-packages` 逐文件比对

左边是已装的 venv（33007 个文件），右边是 PBS 3.12.1（29340 个）：

| 类别 | 数量 | 性质 |
|---|---|---|
| 逐字节相同 | 28286 | — |
| 内容不同：`onnx/` 下的文件 | 3.12.1：7 个；3.12.16：114 个 | **实质差异**，见下 |
| 内容不同：`*.dist-info/RECORD` | 24 | 只差 `../../Scripts/*.exe` 那一行的哈希（入口启动器里写着解释器路径） |
| 只在 venv 有：`.pyc` | 4686 | venv 用过以后积累的字节码 |
| 只在 venv 有：`_virtualenv.pth`、`_virtualenv.py` | 2 | uv venv 自带 |
| 只在 venv 有：`torch-2.11.0+cu128.dist-info/direct_url.json`、`uv_cache.json` | 2 | 当时是从本地 wheel 缓存装的 torch（路径在一个旧的 `Finoka` 数据目录下），文件名里的 sha256 与锁一致，同一个 wheel |
| 只在 PBS 有：pip 及其元数据、`README.txt` | 约 520 | PBS 自带 pip（3.12.1 是 23.3.2，3.12.16 是 26.3）。打包时可以删掉 |
| 只在 PBS 有：`.pyc` | 495 / 441 | uv 探测解释器时生成的 |

**`onnx` 的差异是锁本身的问题，不是可重定位 Python 的问题**：锁里同时有 `onnx` 1.23.2 和 `onnx-weekly` 1.23.0.dev20260805，两个 wheel 都往 `onnx/` 目录写文件，两个 `RECORD` 都声称拥有这些文件。uv 并行安装，最后留下谁的文件取决于安装顺序：同一个 uv、同一份锁，3.12.1 装出来 7 个文件和 venv 不同，3.12.16 装出来 114 个不同。例如 `onnx/__init__.py` 里，一边找 `onnx.libs`，另一边找 `onnx_weekly.libs`。现在的 venv 也只是其中一种随机结果。
- 影响面：引擎里 import `onnx` 的只有 `finesub/speech/joint/sbd.py`（联合 ASR，要整个迁出），以及 audio-separator 的 MDX 架构代码；这次的管线用的是 Roformer。几种结果下两个样本都与基线一致。
- 这是「装出来的东西逐文件相同」做不到的唯一实质原因。预制环境包恰好能把它变成**确定的**：包里只有一份 `onnx/`，所有用户拿到的都一样。代价是包里那份不一定等于某台用户机器上现在的那份。是否需要处理（例如固定安装顺序，或者去掉其中一个包），要用户定，可能还会碰到钉住的版本（红线第 5、7 条）。

### 2.2 基础解释器补丁版本的影响

3.12.1 和 3.12.16 两个环境，两个样本都与基线一致（s6、s7）。`site-packages` 的差异只多了 pip 版本不同、`onnx/` 冲突的结果不同，没有看到解释器版本带来的其他差别。首次运行慢约 40 秒，是生成 `.pyc` 的开销（s8 同一环境的第二次运行恢复到 64 秒）；3.12.16 没有跑第二次。

### 2.3 搬动目录

- 把 `envs/pbs-3.12.1` 直接改名并挪到 `envs/moved/other-root/py env relocated`（新路径带空格），**不做任何修补**：
  - `python -I -c "import torch, ctranslate2, faster_whisper, audio_separator, onnxruntime, transformers"` 成功；`sys.prefix` 自动是新路径；`torch.cuda.is_available()` 为 True，在 RTX 3070 上算矩阵乘法正常；ctranslate2 看到 1 块 CUDA 设备；
  - 两个样本到 `raw-srt` 与基线一致（s8）。
- 写死绝对路径的文件（在新环境里 grep 安装路径得到）：
  - `Scripts\*.exe` 37 个入口启动器（`audio-separator.exe`、`hf.exe`、`tqdm.exe` 等）。搬动后运行会报 `uv trampoline failed to canonicalize script path`。引擎走 `python -m`，用不到它们；
  - `Scripts\numba`（文本脚本的 shebang）；
  - `Lib\encodings\__pycache__\` 里的 4 个 `.pyc`（只是 `co_filename`，影响回溯里显示的路径，不影响运行）；
  - `site-packages` 里**一个也没有**。
- 对照：已装的 venv 有 `pyvenv.cfg` 的 `home = D:\Envir\Python312`（绝对路径，基础解释器在 venv 之外），`site-packages` 里有 93 个 `.pyc` 含旧路径，`Scripts\*.exe` 同样写死了路径。

### 2.4 代替 venv 跑两个样本

见 1.1 表里的 s6（3.12.1）、s7（3.12.16）、s8（搬动后的 3.12.1）：都在沙箱 + 锚点下跑，AOTI 生效，全部产物与基线一致。

### 2.5 体积

原始大小（`du -sb`）：

| 层 | 内容 | 原始 | zstd -3 |
|---|---|---|---|
| Python 环境 | PBS 3.12.1 + 98 个包 | 5.04 GiB | 2.45 GiB |
| 模型 | `audio-separator`（含 AOTI 产物，0.6 GiB）+ `huggingface`（3.4 GiB） | 4.00 GiB | 2.98 GiB |
| 工具 | 引擎实际用到的 ffmpeg/ffprobe + tokcount | 0.92 GiB | 0.35 GiB |
| （工具，其余） | aria2c、node、pot-provider、yt-dlp、uv | 0.28 GiB | 没测 |

Python 环境层的几种压缩（`archives/`）：

| 方式 | 大小 | 压缩耗时 |
|---|---|---|
| tar + zstd -3（Windows 自带 `tar.exe --zstd`） | 2.45 GiB | 47 秒 |
| tar + zstd -19（多线程） | 2.02 GiB | 283 秒 |
| tar + xz -6（多线程） | 1.95 GiB | 303 秒 |

zstd -3 的包用 Windows 自带 `tar.exe` 解压到新目录用了 28 秒，解压出来的环境能直接 `import torch` 并看到 CUDA。HF 缓存里有 24 个相对符号链接：Windows 上解压符号链接需要开发者模式或管理员权限，模型层打包时要么去掉符号链接，要么确认解压端有这个权限。

---

## 3. 「不需要兼容包」成立到什么程度

**成立的部分**：未改动的 `finesub_bootstrap` 放在沙箱里，引擎读写的默认位置全部跟着 `LOCALAPPDATA`、`USERPROFILE` 等变量进了沙箱目录，没有一处写到沙箱之外的用户位置；结果与基线一致。调查文档 1.8 里「没有变量能改」的那几处（托管数据根 `%LOCALAPPDATA%\FineSub`、日志、参考数据、capsule、会话台账、`~\.cache\finesub`、`~\.cache\audio-separator`），在沙箱里都被 `LOCALAPPDATA`/`USERPROFILE` 改向了，不需要兼容包去改 `paths.default_data_root` 和 `model_caches`。检出判定靠「引擎目录里不放 `pyproject.toml`」就消除了。所以对**转写引擎**来说，计划 WP-R7「以同名包提供 48 个名字」的主要理由（改向默认位置）由沙箱边界代替，实测成立。

**仍然需要 Nonoka 另外处理的 `finesub_bootstrap` 和环境行为**：

1. **自下载**：用户 2026-11-11 决定直接删掉。注意这意味着 `finesub_bootstrap` **不再是原样**：要改的是 `model_ensure.ensure_hf_model`（缺模型时开子进程下载）和 `model_fetch`（分离器固定文件，被上锁的 `separation.place_separator_files` 调用）。`finesub_bootstrap` 已经解锁，所以可以改；上锁文件不能动，所以改法只能是让这些函数在缺模型时报错而不是下载。另外两条不经 `finesub_bootstrap` 的下载路径也要堵住：audio-separator 库自己从 GitHub 下（`separation.py:285-290`），以及加载器在 `pinned_snapshot_loadable` 为假时按 `HF_ENDPOINT` 懒下载。后一条可以用 `HF_HUB_OFFLINE=1`（这次实验一直开着）。
2. **下载线路的联网探测**：`download_routes.resolve_region` 在缓存过期时会去探测公网国家端点（`download_routes.py:147`、`:172`），状态写在 `<LOCALAPPDATA>\FineSub\download-routes.json`（沙箱里）。它只在下载路径上被调用；删掉自下载后基本走不到。要彻底不联网，可以设 `FINESUB_DOWNLOAD_REGION`（`forced_region`，跳过探测）。这次的运行都没走到这条路径（沙箱里没出现这个文件）。
3. **`.env` 的就地加密改写**：照计划 D3/D22 处理（密钥只走环境变量，必要时 `FINESUB_ENV_PROTECT=0`）。沙箱里实测都正常（1.4）。
4. **TEMP**：每个引擎进程一份沙箱 TEMP，并由启动方持有一个排序最前的锚点文件（1.2.1）。这是 torch 的问题，与 `finesub_bootstrap` 无关，但不处理的话，沙箱 TEMP 恰恰会让 AOTI 失败。
5. **AOTI 产物**：编译缓存在模型目录里，不在用户目录里。用户机器上没有 MSVC 时，引擎直接回落 eager，**结果与基线不同**（1.2.2）。所以「接受在用户机器上重新编译 AOTI」的前提是用户装了 MSVC；没装的机器要么由预制模型包按显卡架构带上 AOTI 产物，要么接受质量红线上的偏差。这需要用户决定。
6. **`.pyc` 写进引擎源码目录**：预制包要么预编译，要么用 `-X pycache_prefix=` 指到沙箱里。
7. **模型层不是只读的**：每次运行会改写 `probe.json`。
8. **`finesub_bootstrap` 的其他名字**：`token_counter`（tokcount 现在靠 `PATH` 找到）、`processes`、`fsops`、`locks`、`secrets`、`artifacts` 等，原样用，实测没问题。

不在本实验范围内的：纠错引擎（用户已定不进沙箱），它用到的 `agy_records`（`~\.gemini`）、capsule、本地 Agent CLI 这些仍要按原计划处理。

## 4. 没做成或没条件验证的

| 项 | 原因 |
|---|---|
| AOTI 重新编译、首次编译耗时、第二次复用缓存 | 本机没有 MSVC（用户选择先测一致性）。装 VS 2022 Build Tools（C++ 工作负载）后，用不带 `accel\` 的模型副本连跑两次即可补测 |
| `vcvars64.bat` 在沙箱用户目录下能否激活 | 同上 |
| 缺模型时 `finesub_bootstrap` 的自下载行为与写入位置 | 用户决定直接删掉这个行为，所以没测；已经准备好的缺声纹模型副本 `models/missing-emb` 没有用到 |
| 注册表、`C:\Windows\Temp`、Known Folder API 的写入 | 快照不覆盖 |
| 3.12.16 环境的第二次运行耗时、搬动 3.12.16 | 没做（3.12.1 已经验证了搬动） |
| `HOMEDRIVE`/`HOMEPATH` 一并改向 | 没单独验证（没观察到需要） |

## 5. 实验产物（`.tmp/sandbox-experiment/`）

**已按用户要求于 2026-11-11 整个删除**（含脚本和运行记录）。上文引用的 `scripts/`、`runs/`、`snapshots/`、`reports/` 等路径都已不存在；要复现，需要按第 1.0、2.0 节的做法重新搭建。删除前的内容如下：

| 目录 | 内容 | 大小 |
|---|---|---|
| `scripts/` | 全部实验脚本 | 小 |
| `runs/` | 每轮的请求、命令、环境（密钥类变量已打码）、事件、产物、`summary.json` | 小 |
| `snapshots/` | 文件清单快照与差异 | 1.1 GB |
| `reports/` | site-packages 比对、密钥实验结果 | 小 |
| `sandbox/` | 各轮沙箱目录 | 130 MB |
| `models/copy`、`models/missing-emb` | 模型副本 | 各 4.0 GB |
| `envs/pbs-3.12.16`、`envs/moved/...`（3.12.1） | 两个可重定位环境 | 各 5.3 GB |
| `downloads/uv-cache`、`downloads/pbs` | wheel 缓存、PBS 压缩包 | 5.1 GB、85 MB |
| `archives/` | 压缩测试包 | 9.8 GB |
| `extract-test/` | 解压测试 | 5.3 GB |
