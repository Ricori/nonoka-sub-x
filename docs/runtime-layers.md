# 预制环境层：构建与发布手册

本手册分两部分：**构建**（WP-E1，本节）与**发布**（WP-E2，待写）。设计依据见 `docs/transcription-engines-plan.md` 第 0 节第 5 条、4.2 第 14–20 条、WP-E1；前提实验见 `docs/engine-sandbox-experiment.md`。

## 1. 构建

### 1.1 三层是什么

| 层 | 内容 | 根目录下 |
|---|---|---|
| `python` | python-build-standalone `cpython-3.12.1+20240107-x86_64-pc-windows-msvc-shared-install_only`，锁（`src/nonoka_x/resources/pylock.win-py312.toml`）里的 97 个包直接装进它自己的 `Lib\site-packages`，不建 venv；去掉 pip 与 `Lib\site-packages\README.txt`；全部 `.py` 以 `unchecked-hash` 预编译 | `python.exe`、`Lib\`、`DLLs\`、`Scripts\`…… |
| `models` | 模型清单里 FineSub 默认路径会加载的 5 个模型（用户 2026-11-12 定，附录 C 第 6 项）：`separator`、`whisper`、`qwen-referee`、`nemotron3`、`speaker-embedding` | 即一个 `FINESUB_MODEL_DIR`：`audio-separator\`、`huggingface\hub\models--*\snapshots\<revision>\` |
| `tools` | 工具清单里除 uv 以外的全部：ffmpeg/ffprobe、tokcount、yt-dlp、aria2c、node、pot-provider | 平铺 `<工具目录>\<压缩包原样内容>`（用户 2026-11-12 定），例如 `ffmpeg\ffmpeg-n9.0.2-…-win64-gpl-9.0\bin\ffmpeg.exe` |

没有放进模型层的清单条目：`whisper-ja`（只在显式 `--model` 时用）、`diarizen-ts-cuda`/`-cpu`（只在 `--diarization-backend diarizen` 时用）、`diarizen`、`wespeaker`（运行时没有代码加载，是导出工具的源模型）、`moss-transcribe-diarize`（本地 MOSS，第一版不注册）。云 API 引擎增强要的断句模型和强制对齐模型不在清单里，第一版也不接（4.1 第 6 条）。

### 1.2 怎么跑

```text
D:\Envir\Python312\python scripts/build_layers.py build                 # 三层
D:\Envir\Python312\python scripts/build_layers.py build --layer python  # 只建一层（可重复）
D:\Envir\Python312\python scripts/build_layers.py build --clean         # 先清空构建目录（含全部下载缓存）
D:\Envir\Python312\python scripts/build_layers.py compare <第一次> <第二次>
D:\Envir\Python312\python scripts/build_layers.py stage dist\layers --into "<目录>"   # 解压三层，搭出 --real 用的安装根
D:\Envir\Python312\python scripts/quality_gate.py --only layers --layers dist\layers
```

用层跑 `--real`（WP-E1 完成标准；harness 不进沙箱）：`stage` 把三层解到 `<目录>\layers\<层>\<版本>\` 并逐文件核对，再搭出 `<目录>\install\`：`models` 是指向模型层的目录联接，`runtime\<工具>\<版本>` 是指向工具层里该工具目录的联接，旁边写 `current.json`。它打印三个变量，设上后照常跑：

```text
set NONOKA_QUALITY_PYTHON=<目录>\layers\python\<版本>\python.exe
set NONOKA_QUALITY_INSTALL_DIR=<目录>\install
set NONOKA_QUALITY_DATA_DIR=<目录>\data
D:\Envir\Python312\python scripts/quality_gate.py --real
```

（`tests/quality_harness/real.py` 在 2026-11-12 修过一处：清环境之前先读安装位置，否则后两个变量到不了 worker。）跑完可以用 `--layers <目录>\layers\<层>\<版本>` 原地核对层有没有被写入。

- 构建目录默认是**主检出**的 `.tmp\layer-build\`（在 worktree 里跑也一样，worktree 会被清理，大文件不放那里），产出默认在主检出的 `dist\layers\<层>\<版本>\`。两者都被 git 忽略。`--build-dir`、`--out-dir` 可以改。
- 只用本机已有的：系统 Python（跑脚本本身，要 httpx）和 Windows 自带的 `C:\Windows\System32\tar.exe`（bsdtar，带 zstd）。uv 按工具清单下载、校验后只在构建目录里用。
- 下载走 Nonoka 的网络线路（`nonoka_x.runtime.fetch`：Windows 系统代理、环境代理、直连，依次试）；要用设置页里的自定义代理，先设 `NONOKA_PREFERENCES_FILE` 指向桌面的偏好文件。
- 子进程（uv、编译、tar）的环境从零构建：系统必需变量，`USERPROFILE`/`APPDATA`/`LOCALAPPDATA` 指向构建目录下的 `home\`，`TEMP`/`TMP` 指向 `tmp\`，`UV_CACHE_DIR` 指向 `cache\uv-cache\`，uv 用 `--no-config`。用户级缓存（`~\.cache\huggingface`、uv 的用户缓存）一律不碰。
- 磁盘：一次完整构建峰值约 25 GB（下载缓存约 11 GB、三层的暂存树约 10 GB、压缩包约 5 GB；每层打包后会先解压一遍核对，Python 层这一步再临时占约 5 GB）。

构建目录的布局：

```text
.tmp\layer-build\
  cache\     下载缓存：pbs\、wheels\、tools\、uv\、uv-cache\、models\
  work\      暂存树：python\、models\、tools\（--keep-work 时留下）
  verify\    打包后解压核对用，核对完删
  home\ tmp\ 子进程的用户目录与 TEMP
  logs\      uv-install.log（uv -v 的完整输出）、compileall.log、tar-*.log
```

产出：

```text
dist\layers\<层>\<版本>\
  layer.tar.zst   压缩包：tar + zstd -3，Windows 自带 tar.exe 能解（tar -x -f layer.tar.zst -C <目标>）
  manifest.json   逐文件清单，与压缩包根目录里的 .nonoka-layer.json 同字节
  summary.json    版本、清单的完整 sha256、压缩包大小与 sha256、原始大小、文件数、各步耗时（不属于版本）
```

### 1.3 输入与校验

输入只有 Nonoka 的清单（`nonoka_x.runtime.manifests`）和 `RUNTIME_PINS.json`。开工前逐项对照：解释器的构建名、地址、大小、sha256；锁里每个包的版本、每个 wheel 的地址与 sha256；每个工具的版本、地址、大小、sha256、`required_files`；模型层每个模型的 repo、revision、`listedOnly`、每个文件的名字、大小、sha256。有一处不一致就不构建。

- **解释器**：按地址下载、校验大小与 sha256，解开后逐文件记下（路径、大小、sha256，写进清单的 `sources.interpreter.files`）。
- **Python 包**（用户 2026-11-12 定）：先用 Nonoka 的下载器把锁里的每个 wheel（含 ctranslate2 的 `archive`）下到 `cache\wheels\`，逐个按锁里的 sha256 校验（锁里没写大小的，先 HEAD 取大小）；再写一份**本地锁**（`tmp\pylock.local.toml`：名字、版本、marker、sha256 原样，地址换成本地文件，不带 sdist），执行

  ```text
  uv --no-config -v pip install --offline --link-mode copy --python work\python\python.exe --requirement tmp\pylock.local.toml
  uv --no-config pip uninstall --python work\python\python.exe pip
  ```

  uv 按本地锁里的哈希再校验一遍。从本地文件安装时，uv 会在每个 `*.dist-info` 里多写一个 `uv_cache.json`（在 `RECORD` 里），记着 wheel 文件的修改时间；交给 uv 之前把下载好的 wheel 的修改时间设成 `FIXED_MTIME`，这个文件的内容就固定了。uv 偶发写入口启动器时「拒绝访问」（本机见过一次，像是扫描程序占着刚写的 exe），脚本会原样再装一次。装完逐个核对：每个 `*.dist-info` 的名字和版本在锁里；按它 `WHEEL` 里的标签认出装的是锁里的哪一个 wheel，文件名、地址、sha256 写进清单的 `sources.packages`；`RECORD` 列的每个文件都在、哈希和大小相符；锁里在 Windows 上生效的包一个不少。
- **字节码**：删掉所有 `__pycache__`（PBS 自带的和 uv 探测解释器时生成的），再用层自己的解释器统一编译：`python -I -B -m compileall -f -q -j 1 --invalidation-mode unchecked-hash -s <层根> <层根>`。`-s` 去掉源码路径前缀，字节码里不留构建机路径（加载时解释器会把 `co_filename` 改成实际位置）。个别故意写坏的 `.py`（测试数据、模板）编译失败是正常的，记在 `logs\compileall.log`。
- **每个文件都有来历**：Python 层里的每个文件，要么属于某个包的 `RECORD`，要么是解释器压缩包里原样的文件，要么是由层里的某个 `.py` 编译出的字节码，要么是列明的例外。
- **写死路径的例外**：扫描每个文件里有没有暂存路径（UTF-8 与 UTF-16，大小写和斜杠的几种写法）。只允许出现在 `Scripts\*.exe`（uv 生成的入口启动器）和 `Scripts\numba`（shebang），列进清单的 `exceptions`；出现在别处就停下。这些文件在各自的 `RECORD` 里，删掉会让内容对不上 `RECORD`，所以保留；搬目录后它们不能用，引擎走 `python -m`，不用它们（4.2 第 17 条）。
- **模型**：分离器的三个文件按清单地址下载并校验（`download_checks.json` 已钉到上游 commit `8a65b784`，用户 2026-11-12 批准）。Hugging Face 的模型不经 `huggingface_hub`、不经引擎：先从 Hub 的 tree API 取钉住 revision 下的文件列表，`listedOnly` 的只取清单列的文件，其余取整个仓库（与引擎自己下载时的范围相同）；逐个从 `https://huggingface.co/<repo>/resolve/<revision>/<文件>` 下载。清单列的文件按清单的大小与 sha256 校验，其余 LFS 文件按 Hub 给的 sha256，其余小文件按 Hub 给的 git blob 哈希，校验方式写进 `sources.models[].files[].checked`。
- **工具**：按清单地址下载并校验，压缩包原样解进 `<工具目录>\`，检查 `required_files` 都在。

### 1.4 层的样子与引擎要的标记

- 只有普通文件：没有符号链接、目录联接或其他重解析点（构建时扫描，遇到就停）。HF 缓存的 `snapshots\<revision>\` 里直接是实文件，没有 `blobs\`、`refs\`、`.no_exist\`、`.locks\`；分离器没有 `accel\`。
- 每个 HF 仓库目录旁有引擎的校验标记 `huggingface\hub\models--*\.finesub-verified.json`，内容与 `finesub_bootstrap.hf_verify.write_marker` 写的逐字节相同（摘要按引擎自己的模型清单条目算，和 Nonoka 清单里这几条相同）。引擎把它读成 `current`。
- 分离器的 `.ckpt` 与 `.yaml` 旁有引擎的 `.verified` 戳记（与 `finesub_bootstrap.model_fetch._write_verified_stamp` 写的逐字节相同），里面记着 sha256、大小和 `mtime_ns`。`download_checks.json` 在引擎自己的清单里没钉哈希，引擎既不校验也不读它的戳记，所以没有戳记。
- **修改时间**（用户 2026-11-12 定）：每个文件和目录的修改时间都设成 `FIXED_MTIME`（2026-01-01 00:00:00 UTC），戳记里的 `mtime_ns` 就是它。tar 解压会还原修改时间；**层管理器解压时必须保留**，否则引擎会认为戳记过期、重算 610 MB 的哈希并把新戳记写进模型层。闸门在解压出的模型层上检查这一点。逐文件清单不记修改时间。

### 1.5 逐文件清单与版本

格式由 `src/nonoka_x/runtime/layer_manifest.py` 定义，构建、闸门、层管理器共用：

```json
{
 "schema": 1, "layer": "python", "platform": "windows-x64",
 "sources": {"interpreter": {...}, "packages": [...], "installer": {...}, "bytecode": {...}},
 "exceptions": [{"path": "Scripts/audio-separator.exe", "reason": "..."}],
 "empty_dirs": ["..."],
 "files": [{"path": "Lib/site-packages/torch/__init__.py", "size": 0, "sha256": "..."}]
}
```

- 路径是相对层根的 POSIX 路径；`files` 按路径排序；字节是规范的（键排序、一个空格缩进、LF、末尾换行），同样的输入得到同样的字节。不含构建机的绝对路径，不含时间。
- `sources` 只记钉住的输入：模型层是每个模型的 repo、revision、文件与校验方式；工具层是每个工具的版本、地址、大小、sha256、目录与文件数。
- 清单放两份：压缩包根目录的 `.nonoka-layer.json`（不列在自己的 `files` 里，解压出的目录因此自带清单）和压缩包旁边的 `manifest.json`（用户 2026-11-12 定）。
- **版本号** = 清单字节 sha256 的前 16 位十六进制（用户 2026-11-12 定）；完整 sha256 在 `summary.json` 的 `manifest_sha256`。

### 1.6 可重复构建

规则（4.2 第 17 条、附录 B 第 13 问）：同一份输入在干净目录里构建两次，三层的逐文件清单完全相同；发布前必须做第二次构建并比对。

入口启动器里写着安装时解释器的绝对路径，所以两次构建都在**同一个位置**（默认的 `.tmp\layer-build\`），第二次之前用 `--clean` 把构建目录连同全部下载缓存清空（用户 2026-11-12 定）。做法：

```text
D:\Envir\Python312\python scripts/build_layers.py build --clean
move dist\layers dist\layers-first      （或用 --out-dir 把第二次放到别处）
D:\Envir\Python312\python scripts/build_layers.py build --clean --out-dir dist\layers-second
D:\Envir\Python312\python scripts/build_layers.py compare dist\layers-first dist\layers-second
```

`compare` 逐层比清单字节，不同时列出哪些文件不同。

### 1.7 闸门的「预制层」检查

`scripts/quality_gate.py --layers <路径>…`（实现在 `tests/quality_harness/layers.py`）。路径可以是构建产出目录（`dist\layers`，每个压缩包先解到主检出的 `.tmp\layer-gate\` 再逐文件核对，核对完删掉）、某一层的版本目录、一个已解压的层目录（自带 `.nonoka-layer.json`，就地逐文件重算 sha256），或单独一份清单（只核对清单本身，结果标为「未判定」）。核对的内容：

- 每层：版本号与目录名相符；有文件时，目录与清单完全一致（不多不少、大小、sha256、没有链接）。
- Python 层：解释器的构建与 sha256 对 `RUNTIME_PINS.json`；锁里在 Windows 上生效的每个包都装了，装的是锁里那个 wheel（`sources.packages` 的地址与 sha256 对 `RUNTIME_PINS.json`）；每个包装出的文件对它的 `RECORD`；其余文件是解释器压缩包里原样的、由层里的源码编译的字节码，或列明的例外；例外只能是 `Scripts\*.exe` 和 `Scripts\numba`。
- 模型层：正好是那 5 个模型，repo、revision、每个钉住的文件（大小、sha256）对 `RUNTIME_PINS.json`；除了快照文件、引擎的两种标记和分离器文件，没有别的（没有 `accel\`、`blobs\`）；标记的内容是引擎要的；带戳记的文件修改时间是 `FIXED_MTIME`。
- 工具层：除 uv 外每个钉住的工具都在，版本、地址、大小、sha256 对 `RUNTIME_PINS.json`，`required_files` 都在，每个文件都属于某个工具的目录。

### 1.8 本机实测（2026-11-12）

| 层 | 版本 | 文件数 | 原始 | 压缩后 | 单层耗时 |
|---|---|---|---|---|---|
| python | `8cc0ee612a5db785` | 45563（例外 38：37 个入口 exe + `Scripts\numba`） | 5.30 GiB | 2.50 GiB | 约 20.5 分钟（下载 wheel 6–7 分钟、安装 1 分钟、编译 1 分钟、核对 5 分钟、清单 2 分钟、打包 1 分钟、解压核对 3 分钟） |
| models | `f2a2b753498be37c` | 33 | 3.99 GiB | 2.97 GiB | 约 10 分钟（下载 9 分钟） |
| tools | `b2e46916329ea7c4` | 9225 | 0.68 GiB | 0.24 GiB | 约 2 分钟 |

- 完整的 `build --clean`（三层、全部重新下载）：第一次 33 分 38 秒，第二次 33 分 29 秒。下载速度约 5 MB/s，耗时大半在下载。
- 两次构建（同一位置、各自 `--clean`）：三层的清单逐字节相同，连压缩包的 sha256 也相同。
- `quality_gate.py --layers dist\layers`：三层通过。
- Python 层解压到带空格的路径（Windows `tar.exe`，34 秒）：`python -I -B -c "import torch, ctranslate2, faster_whisper, audio_separator, onnxruntime, transformers"` 成功，`torch.cuda.is_available()` 为真（RTX 3070），ctranslate2 看到 1 块 CUDA 设备，运行后层目录没有新文件。
- 模型层：`hf_verify` 的标记读成 `current`，`pinned_snapshot_loadable` 为真；分离器戳记有效；whisper、qwen（含按 repo + revision 离线解析）、wespeaker、nemotron3 都能加载；运行后模型层没有新文件。
- 用层里的解释器、模型、工具跑 `quality_gate.py --real`：通过（7 个逐字节相同、8 个按批准标准相同，eager）。之后模型层、工具层原地逐文件核对通过。

### 1.9 已知问题

- **numba 在运行时往 Python 层里写缓存**：跑过 `--real` 后，Python 层多出 46 个文件 `Lib\site-packages\librosa\{core,util}\__pycache__\*.nbi|*.nbc`（librosa 里 `cache=True` 的 numba 函数；没有 `NUMBA_CACHE_DIR` 时 numba 把缓存写在源码旁边，`-B` 管不到）。不影响结果，但层因此不再只读、层管理器的快速检查会认为它与清单不符。要在转写进程的环境里设 `NUMBA_CACHE_DIR`（指向沙箱里的缓存目录），属于 4.5 第 27 条的变量分类，交给 WP-R5 / WP-H；本包没有改。
