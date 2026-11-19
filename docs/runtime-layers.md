# 预制环境层：构建与发布手册

本手册分两部分：**构建**（第 1 节）与**发布**（第 2 节）。整体架构与引擎包协作见 [architecture.md](architecture.md) 与 [transcription-engines.md](transcription-engines.md)。

## 1. 构建

### 1.1 三层是什么

| 层 | 内容 | 根目录下 |
|---|---|---|
| `python` | python-build-standalone `cpython-3.12.1+20240107-x86_64-pc-windows-msvc-shared-install_only`，锁（`src/nonoka_x/resources/pylock.win-py312.toml`）里的 97 个包直接装进它自己的 `Lib\site-packages`，不建 venv；去掉 pip 与 `Lib\site-packages\README.txt`；全部 `.py` 以 `unchecked-hash` 预编译 | `python.exe`、`Lib\`、`DLLs\`、`Scripts\`…… |
| `models` | 模型清单里 FineSub 默认路径会加载的 5 个模型：`separator`、`whisper`、`qwen-referee`、`nemotron3`、`speaker-embedding` | 即一个 `FINESUB_MODEL_DIR`：`audio-separator\`、`huggingface\hub\models--*\snapshots\<revision>\` |
| `tools` | 工具清单里除 uv 以外的全部：ffmpeg/ffprobe、tokcount、yt-dlp、aria2c、node、pot-provider | 平铺 `<工具目录>\<压缩包原样内容>`，例如 `ffmpeg\ffmpeg-n9.0.2-…-win64-gpl-9.0\bin\ffmpeg.exe` |

没有放进模型层的清单条目：`whisper-ja`（只在显式 `--model` 时用）、`diarizen-ts-cuda`/`-cpu`（只在 `--diarization-backend diarizen` 时用）、`diarizen`、`wespeaker`（运行时没有代码加载，是导出工具的源模型）、`moss-transcribe-diarize`（本地 MOSS，当前不注册）。

### 1.2 怎么跑

```text
D:\Envir\Python312\python scripts/build_layers.py build                 # 三层
D:\Envir\Python312\python scripts/build_layers.py build --layer python  # 只建一层（可重复）
D:\Envir\Python312\python scripts/build_layers.py build --clean         # 先清空构建目录（含全部下载缓存）
D:\Envir\Python312\python scripts/build_layers.py compare <第一次> <第二次>
D:\Envir\Python312\python scripts/quality_gate.py --only layers --layers dist\layers
```

用已安装好的三层运行质量闸门 `--real`（含 `layers/<层>/<版本>` 及 `current.json` 的大文件根，加按 `bootstrap-requirements.py312.txt` 构建的 Python 3.12 启动器）：

```text
set NONOKA_QUALITY_LAUNCHER_PYTHON=<启动器目录>\launcher\Scripts\python.exe
set NONOKA_QUALITY_INSTALL_DIR=<运行环境目录>
set NONOKA_QUALITY_DATA_DIR=<数据目录>
D:\Envir\Python312\python scripts/quality_gate.py --real
```

（`NONOKA_QUALITY_PYTHON` 已废弃；如需针对非默认索引校验，可设置 `NONOKA_QUALITY_LAYERS_INDEX`。）跑完可以用 `--only layers --layers <运行环境目录>\layers\<层>\<版本>` 原地核对层是否未被写入。

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

## 2. 发布

设计依据：计划 4.2 第 18、19 条与 WP-E2；用户 2026-11-13 的决定见附录 B 第 59–61 问。脚本是 `scripts/publish_layers.py`，测试是 `tests/test_publish_layers.py`（假的桶加本地 HTTP 服务，不碰 Cloudflare）。

### 2.1 桶里放什么

桶 `nonoka-x-layers`，经自定义域名 `https://layers.nonoka.online/` 公开读。对象全部按内容哈希命名，上传后不再改：

```text
v1/<层>/<清单 sha256>.json       层的逐文件清单（与压缩包里的 .nonoka-layer.json 同字节）
v1/<层>/<压缩包 sha256>.tar.zst  层的压缩包
```

- **模型文件不单独托管**（用户 2026-11-13）：计划 4.2 第 18 条原先要把模型层的每个文件另按 sha256 存一份 `v1/files/<文件 sha256>`，供迁移时按文件补齐（4.4 第 25 条）；现在不传。`layers.json` 的 `files` 键保留、`files.layers` 为空列表，格式不变；脚本里按文件托管的逻辑还在，由 `layers.json` 决定，以后要恢复只改 `FILE_LAYERS`。

- 每个对象带 `Cache-Control: public, max-age=31536000, immutable` 和元数据 `x-amz-meta-sha256`（脚本据此判断「已经在桶里、内容相同」）。
- 这个桌面版本要哪几层写在 `src/nonoka_x/resources/layers.json`，格式见计划 WP-E2 一节开头（WP-E3 按同一份格式读）。它随桌面安装包发布，**不上传**。
- 2026-11-13 的这一版：6 个对象，共 5.73 GiB（3 个压缩包 5.72 GiB、3 份清单 10.33 MiB）。

### 2.2 要在 Cloudflare 上建的东西（用户自己建）

账号、令牌都只在用户本机，不进仓库、不进日志。会话只往这个桶里上传，不碰其他资源（Worker、`nonoka-x-cloud` 桶等）。

1. **桶**：R2 object storage → Create bucket，名字 `nonoka-x-layers`，位置 Automatic，存储类 **Standard**（Infrequent Access 有取回费和 30 天最短存储期，不适合）。
2. **公开读，只经自定义域名**：桶 → Settings → Custom Domains → Add，填 `layers.nonoka.online` → Connect Domain，等状态从 Initializing 变成 Active。域名所在的 zone `nonoka.online` 必须在同一个账号里。**`r2.dev` 公共地址保持关闭**：速率限制和 WAF 只作用在自定义域名上，`r2.dev` 开着就能绕过去。
3. **速率限制规则**（挡盗链；用户 2026-11-13 定）：Security → WAF → Rate limiting rules → Create rule。
   - Free 计划只有 1 条规则，表达式只能用路径（Path），不能用主机名；而 `cloud.nonoka.online` 的 Worker 也用 `/v1/…` 路径（`/v1/tasks`、`/v1/uploads`、`/v1/library`、`/v1/session`、`/v1/capabilities`、`/v1/admin`），所以规则只匹配层用到的三个前缀，和它不重叠：

     ```text
     (starts_with(http.request.uri.path, "/v1/python/") or starts_with(http.request.uri.path, "/v1/models/")
      or starts_with(http.request.uri.path, "/v1/tools/"))
     ```

     Pro 及以上再加 `and http.host eq "layers.nonoka.online"`。以后云端 Worker 若要加以这三个前缀开头的路径（或者恢复按文件托管、用到 `/v1/files/`），先改这条规则。
   - 计数特征：IP；周期：10 秒；阈值：**50 次请求**；动作：Block；封禁时长：10 秒（Free 计划只有这一档）。
   - 阈值的依据：一个新用户装三层是 3 份清单 + 3 个压缩包，断点续传每次再多一个请求。50 次/10 秒对正常下载绰绰有余，经同一个代理出口的多个用户同时装也够用。
   - 验证步骤见 2.4。
4. **API 令牌**：R2 object storage → Manage R2 API Tokens → Create API token。权限 **Object Read & Write**，范围 **只限桶 `nonoka-x-layers`**（Apply to specific buckets only），有效期可以设短一点（发布完就到期或手动撤销）。创建后页面只显示一次 Access Key ID 和 Secret Access Key；账号 ID 在 R2 概览页右侧（Account ID）。
5. **凭据放哪**（用户 2026-11-13 定）：主检出的 `scripts\.env`，即 `D:\Development\nonoka-sub-x\scripts\.env`（已在 `.gitignore` 里；脚本在 worktree 里跑时也读这一份，先找脚本旁边的 `.env`）。主检出要包含了 `.gitignore` 里这一行才算忽略，在那之前只能放在 worktree 自己的 `scripts\.env`。内容：

   ```text
   R2_ACCOUNT_ID=<32 位十六进制的账号 ID>
   R2_ACCESS_KEY_ID=<令牌的 Access Key ID>
   R2_SECRET_ACCESS_KEY=<令牌的 Secret Access Key>
   ```

   脚本只从这个文件读；文件没被 git 忽略（比如被 `git add -f` 进了仓库）就拒绝读。脚本从不打印、记录这三个值，出错时只报文件名和键名；S3 的错误回复里可能带 Access Key ID，脚本只取其中的错误码和说明。
6. **缓存**：不用配。Free 计划单个文件超过 512 MB 不进 CDN 缓存（三个压缩包都超过），`.json` 默认也不缓存，所以下载基本都回源到 R2；R2 不收流量费，只按请求计费（2.6）。不要加 Cache Everything 规则。

### 2.3 发布步骤

```text
D:\Envir\Python312\python scripts/publish_layers.py index                 # dist\layers -> layers.json（只读 dist，每个压缩包重算 sha256）
D:\Envir\Python312\python scripts/publish_layers.py plan --list --remote  # 要传的对象、数量、总大小，哪些已在桶里
D:\Envir\Python312\python scripts/publish_layers.py upload [--proxy http://127.0.0.1:7890 | --direct]
D:\Envir\Python312\python scripts/publish_layers.py upload                # 再跑一次：应当全部 skipped
D:\Envir\Python312\python scripts/publish_layers.py verify --direct
D:\Envir\Python312\python scripts/publish_layers.py verify --proxy http://127.0.0.1:7890
D:\Envir\Python312\python scripts/publish_layers.py ratelimit --direct
```

- `index` 先核对每层：目录名是清单的版本、`summary.json` 与文件相符、压缩包 sha256 与 summary 相同、压缩包里的 `.nonoka-layer.json` 与 `manifest.json` 同字节。`dist\layers\<层>\` 下有多个版本时用 `--version <层>=<版本>` 指定。生成的 `layers.json` 要提交。
- `files.layers` 为空，`plan`/`upload` 不解压任何层；只有按文件托管时才会把那一层解到主检出的 `.tmp\layer-publish\`。
- **上传幂等、绝不覆盖**：先 HEAD；已存在且大小和 `x-amz-meta-sha256` 都对得上就跳过；名字被占用但内容不同（或没有记录 sha256）就停下报错，不动它。写入一律带 `If-None-Match: *`，所以 HEAD 与写入之间被别人抢先也不会覆盖。64 MiB 以上分片上传（每片 64 MiB），每个请求都带载荷的 sha256 由 R2 校验，整对象的 sha256 在完成上传前再核一遍，对不上就放弃这次分片上传。传完再 HEAD 一次确认。中途被杀留下的未完成分片上传，R2 默认 7 天后自动清掉。
- 上传走 R2 的 S3 接口 `https://<账号 ID>.r2.cloudflarestorage.com`，不经自定义域名；默认按 Nonoka 的网络线路选代理或直连，也可以 `--proxy`/`--direct` 指定。
- **gzip**：客户端声明接受 gzip 时，R2（S3 接口和自定义域名都是）会把 JSON 对象压缩后发出，回包没有 `Content-Length`；带 `Range` 的请求则拿到原始字节。脚本发给 R2 的请求一律带 `Accept-Encoding: identity`（2026-11-13 第一次真实上传就因为 HEAD 读不到长度停过一次）。层管理器下载清单时也应带 `Accept-Encoding: identity`，大小核对和断点续传才可靠。
- **外网校验**（发版前必做）：`verify` 从 `layers.json` 的 `baseUrl` 下载全部对象，用 Nonoka 自己的下载器按大小和 sha256 校验，核对清单的版本，把压缩包解开、核对它带的清单与下载的清单同字节、逐文件比 sha256（`files.layers` 列了层时再逐个下载它的单文件校验）。经代理和不经代理各跑一次。每次下载约 5.7 GiB；临时目录默认是主检出的 `.tmp\layer-verify\`，一层一层解开核对、核完就删，最多同时占约 8 GiB（Python 层的压缩包加解开的 5.3 GiB），`--keep` 全部保留。
- 桶里已经有的对象不会因为重新跑 `index` 而改变：内容不变，名字就不变。

### 2.4 验证速率限制

`ratelimit --direct`（或 `--proxy …`）：挑 `layers.json` 里最小的对象（模型层清单，14812 字节），8 个线程连发 120 个 GET，统计状态码；然后等 15 秒再发 1 个。期望：前一批里有 429（被规则拦下），等过封禁时长后的那一个是 200。脚本打印各状态码的次数，不符合期望时退出码为 1。另外：

- Cloudflare 面板 Security → Events 里能看到这条规则的命中记录（来源 IP、路径）。
- 确认没误伤云端：同一时间 `cloud.nonoka.online` 的请求不受影响（路径不在规则的三个前缀里）。
- 这一步本身只产生约 120 次 B 类操作，可以忽略。

### 2.5 旧版本

不自动删。`unreferenced` 列出桶里没被任何已发布版本引用的对象和各自大小：已发布版本 = 每个 `v*` 标签上的 `src/nonoka_x/resources/layers.json`，加上当前工作区这份；按文件托管的层（当前没有）的单文件，是否被引用按这些 `layers.json` 指向的清单算。脚本没有删除命令；要删哪些，由维护者确认后单独执行（R2 面板里删，或另写一次性的命令，删除本身不收费）。

```text
D:\Envir\Python312\python scripts/publish_layers.py unreferenced
```

### 2.6 流量与请求费用的估算

R2 的价格（2026-11-13 查 <https://developers.cloudflare.com/r2/pricing/>）：Standard 存储 $0.015 / GB·月；A 类操作（PutObject、CreateMultipartUpload、UploadPart、CompleteMultipartUpload、ListObjects 等）$4.50 / 百万次；B 类操作（GetObject、HeadObject 等）$0.36 / 百万次；**出口流量免费**。每月免费额度：存储 10 GB·月、A 类 100 万次、B 类 1000 万次。

估算方法：

```text
每月费用 ≈ max(0, 存储 GB − 10) × 0.015
         + max(0, A 类次数 − 1,000,000) / 1e6 × 4.50
         + max(0, B 类次数 − 10,000,000) / 1e6 × 0.36
```

- **存储**：一整套层 5.73 GiB ≈ 6.15 GB，在免费额度内，$0/月。再多保留一整套旧版本共约 12.3 GB，超出约 2.3 GB，约 $0.035/月；之后每多一整套约 $0.09/月。只换一层时按那一层算（Python 层 2.5 GiB）。
- **每个新用户**：下载 3 份清单 + 3 个压缩包 ≈ **5.73 GiB**，流量免费；请求是 6 次 GET，加上断点续传时每续一次多 1 次，按 10 次算。即使每月 1 万个新用户也只有约 10 万次 B 类操作，在免费额度内。
- **迁移补齐**：模型文件不在 R2 上单独托管，迁移时缺失模型改为下载整层模型包。
- **一次发布**：A 类约 100 次（3 次 PUT 清单 + 3 个压缩包的分片上传：Python 40 片、模型 48 片、工具 4 片，各加创建与完成），B 类十几次 HEAD；重复上传只有 6 次 HEAD。
- **盗链的上限**：流量不要钱，能推高费用的只有请求次数。速率限制把每个 IP 压在 10 秒 50 次以内，一个 IP 持续打满一个月约 1300 万次 B 类操作（约 $1，超出免费额度的部分）；靠规则挡住突发、看用量发现持续的异常 IP（2.7），必要时再加规则封掉。

### 2.7 看用量的地方

- **R2 → 桶 `nonoka-x-layers` → Metrics**：存储量、对象数、A 类与 B 类操作次数（默认看最近 24 小时，可以改时间范围）。
- **账号的 Billing 页（计费用量）**：本月 R2 存储与操作的计费用量，和免费额度的对照（菜单位置以面板为准）。
- **zone `nonoka.online` → Analytics & Logs → Traffic**：按主机名筛 `layers.nonoka.online`，看请求数、流量、来源国家。
- **Security → Events**：速率限制规则的命中记录，找盗链的 IP。
- 需要程序化查询时用 GraphQL Analytics API 的 `r2OperationsAdaptiveGroups`、`r2StorageAdaptiveGroups` 数据集（最多 31 天范围）。

### 2.8 第一次发布（2026-11-13）

- **建了什么**：桶 `nonoka-x-layers` 由会话用用户本机已登录的 wrangler 建（用户同意；Standard，位置自动分到 APAC，`r2.dev` 关闭）；自定义域名 `layers.nonoka.online` 由用户自己在面板里绑定（zone `nonoka.online`，最低 TLS 1.0，所有权与 SSL 均 active）；R2 API 令牌由用户建，写进 worktree 的 `scripts\.env`。**速率限制规则没建**（用户决定先不做）。
- **上传**（直连，约 10 MiB/s）：6 个对象 5.73 GiB 全部在桶里。第一次在 Python 层清单处停下（上面 gzip 一条），修好后续传，已传的 2 个跳过、其余 4 个上传。再跑一次：6 个全部 skipped，只发了 HEAD，桶里每个对象的 etag、修改时间、`x-amz-meta-sha256` 前后完全相同。
- **外网校验，不经代理**：通过。从 `https://layers.nonoka.online/` 下载 5.73 GiB，用时 13 分 03 秒；三层的清单、压缩包、版本都对，解开后逐文件比 sha256：python 45563、models 33、tools 9225 个文件全部相符。
- **外网校验，经代理**：**待补，发版前必须补上**（用户 2026-11-13 决定这次不跑）。当时本机 `127.0.0.1:7890` 没有代理在监听。
- **速率限制**：**待补**。`ratelimit --direct` 的 120 个请求全部 200，即规则不存在；建好规则后重跑，期望出现 429。
