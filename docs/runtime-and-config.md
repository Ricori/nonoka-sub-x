# 运行时与配置：接口调查与设计定稿（WP-R0）

> 状态：定稿（2026-11-11）。基于 `main@989eef7`、FineSub `v0.7.0@7123055`。
> 本文是 `docs/transcription-engines-plan.md` 第 6 节 WP-R0 的产出，后面的 R、M、P、H 包照它做。
> **2026-11-11 标注**：D5、D10、D12、D23 已被计划的「预制环境包 + 进程级沙箱」方案取代，D18、D21 部分取代（用户确认，见 `docs/transcription-engines-plan.md` 附录 A.3）。正文里依赖这几条的段落（1.5 的 venv 就绪判定与搬家、1.11 里转写包用到的名字、1.12 的大文件根树、1.13 里 `python`、`python-builds`、工具各行的搬法、1.15 的 `runtime.manifests` 与 `download` 字段、2.7 第 2 条、3.2 第 2、6、7 条）按计划为准，原文保留作记录。
> 方法：只读代码和本机旧数据目录（只列目录、读非密钥文件；密钥文件只取变量名，不取值），不改任何代码。
> 文中 `finesub/…`、`finesub_bootstrap/…` 指 `third_party/finesub/src/` 下的文件；`src/…` 指本仓库。

## 0. 用户答复记录

设计类的每一项都来自下面这些答复，正文里用编号引用（如「D3」）。全部答复于 2026-11-11 在本会话里给出。

| 编号 | 批次 | 问题 | 用户的选择 |
|---|---|---|---|
| D1 | 1 | Nonoka 配置文件格式 | JSON：主数据目录下的 `settings.json` |
| D2 | 1 | 密钥存储方案 | 新文件 `secrets.json`，沿用 FineSub 现行的信封方案（Windows DPAPI 绑账户、macOS 混淆），代码复制过来注明来源 |
| D3 | 1 | 密钥怎么交给引擎 | 只走进程环境变量，不落盘；只在用户改过 `GEMINI_BASE_URL` 时写一个只含这一项的 `.env`（无密钥） |
| D4 | 1 | 引擎包的数据目录 | 主数据目录下 `engines\<包 id>\` |
| D5 | 2 | 大文件根的名字与内部划分 | 根叫 `runtime`（默认 `<主数据目录>\runtime`，搬过的为 `<所选位置>\Nonoka X\runtime`），内部 `python\`、`tools\<工具>\<版本>\`、`models\{huggingface,audio-separator,torch}\`、`cache\` **〔2026-11-11 已被取代：大文件根仍叫 `runtime`，内部改为 `layers\<层>\<版本>\` 加 `current.json`（预制环境包），见计划 4.2 第 19 条、附录 A.3〕** |
| D6 | 2 | 派生配置文件放哪 | `engines\<包>\run\<进程标识>\`，每个进程一份，进程结束删，启动时清残留 |
| D7 | 2 | 任务目录里私有附件目录的名字 | `tasks\<id>\native\<包 id>\` |
| D8 | 2 | 引擎在 `%LOCALAPPDATA%\FineSub` 下写的东西 | 原地不动：新版不读不写也不碰，会话台账从零开始 |
| D9 | 3 | `-I` 启动后孙进程怎么找到包 | 入口设 `sys.path`；`PYTHONPATH`（只含该包、它的兼容包、yt-dlp）和 `PYTHONNOUSERSITE=1` 列为「Nonoka 设定」变量供孙进程用 |
| D10 | 3 | venv 的基础解释器是系统 Python | 列为密闭边界并登记：运行时记录里写明基础解释器路径和版本，就绪检查核对；迁移不搬它；新装仍允许复用系统 Python **〔2026-11-11 已被取代：不再用系统 Python，解释器随 Python 环境层下发（PBS 3.12.1），见计划 4.2 第 14 条〕** |
| D11 | 3 | 旧 `finesub.toml` 里 Nonoka 不认识的键 | 丢弃 |
| D11c | 4 | 确认 D11 与计划第 18 条冲突 | 确认丢弃，迁移报告列出被丢弃的表与键名；计划第 18 条和 WP-R4、WP-M2 的完成标准改掉（见第 3 节） |
| D12 | 3 | 运行时组件粒度 | 与清单条目一一对应 **〔2026-11-11 已被取代：组件改为三层（Python 环境、模型、工具），见计划 4.2 第 14、15 条〕** |
| D13 | 4 | 旧 `finesub.env` 里 Nonoka 不认识的变量 | 同样丢弃，迁移报告只列变量名不列值，旧文件改名留作备份 |
| D14 | 4 | `bootstrap\`、`app-resources\` 放哪 | 留在主数据目录，不动 |
| D15 | 4 | 自定义模型的事实（token 上限等）谁给 | 核心按 `model_specs.py` 算好，放进启动上下文的「LLM 端点列表」 |
| D16 | 5 | 本地 Agent CLI 的配置与证书变量 | 列入「系统必需」，逐个列名从父进程原样带给纠错包进程（转写包不带） |
| D17 | 5 | Windows 注册表里的系统代理 | 总是显式写代理变量：用代理就写 `HTTP(S)_PROXY` 与 `NO_PROXY`，不用就写 `NO_PROXY=*` |
| D18 | 5 | 安装器自己的小状态 | `<主数据目录>\runtime-state\`；`locations.json` 不要了，大文件根只认 `storage.json` **〔2026-11-11 部分取代：`runtime-state\` 与不要 `locations.json` 仍有效；不做国内镜像后不再有下载线路状态（`download-routes.json`），见计划 4.4 第 24 条〕** |
| D19 | 6 | D3 让闸门的环境基线必然不过 | WP-R5 时按新的交付方式重录 `tests/fixtures/quality/environment.json`，单独提交（用户已批准，红线第 7 条） |
| D20 | 6 | 派生 `finesub.toml` 是否照写 `[nonoka_models]` | 照写，与闸门基线同字节 |
| D21 | 6 | 兼容包里「托管安装布局」映射到哪 | `data_root`、`user_data`、`agent_capsules` 放 `engines\<包>\` 下；`models` 指 `runtime\models`；`cache` 指 `runtime\cache\<包>\`；都来自启动上下文 **〔2026-11-11 部分取代：只对纠错包的兼容包有效，`models`、`cache` 两项不再需要；转写包原样带 `finesub_bootstrap`，默认位置由进程级沙箱改向，见计划 4.2 第 16 条、4.5 第 28 条〕** |
| D22 | 6 | 派生 `.env` 被引擎就地加密改写 | 给引擎设 `FINESUB_ENV_PROTECT=0`，引擎对派生文件只读不写 |
| D23 | 7 | venv 的基础解释器是旧根里的 `runtime\python-builds\` 时怎么迁 | 搬到 `<新根>\tools\python-builds\`，并把 `pyvenv.cfg` 的 `home` 一行改成新路径（只改路径，不动任何包）；迁移后实测导入 torch 与 `--real` **〔2026-11-11 已被取代：没有 venv，迁移时旧 Python 环境清空后重新下载，见计划 4.4 第 25 条〕** |

## 1. 十五个问题的答案

### 1.1 上锁引擎运行时从 `finesub_bootstrap` 读写什么（第 1 问）

上锁文件里对 `finesub_bootstrap` 的 import 共 60 处，解析成 48 个名字（与 `tests/fixtures/quality/bootstrap-surface.json` 一致；`speech/joint/` 和 `stages.py` 没有直接 import）。下表按「它让引擎碰哪个目录或文件、由什么决定」逐个调用点列出。每个名字在兼容包里的实现见 1.11。

| 调用点 | 用到的名字 | 读 / 写什么 | 由什么决定 |
|---|---|---|---|
| `finesub/paths.py:9`、`:324-332`；`finesub/llm/token_budget.py:32`、`:187-196` | `token_counter`（`configured_path`、`CHECKOUT_RELATIVE_PATH`、`find_on_path`） | 找 `tokcount` 可执行文件：先 `GEMINI_TOKEN_COUNTER_EXE`，再 `<检出根>\bin\windows-amd64\tokcount.exe`，再 `PATH` | `GEMINI_TOKEN_COUNTER_EXE`（`finesub_bootstrap/token_counter.py:25`）；检出根判定见 1.8；`PATH`。现在 Nonoka 靠把 tokcount 目录放进 `PATH` 让它找到（`src/nonoka_x/provision.py:979-984`） |
| `finesub/paths.py:35`、`:72` | `paths.packaged_app_root`、`paths.packaged_app_paths` | 判断代码是否装在 `<根>\app\versions\<版本>\` 里；是则用该安装的布局 | 只看 `__file__` 的位置（`finesub_bootstrap/paths.py:434-458`）。Nonoka 的布局里永远不成立 |
| `finesub/paths.py:54-58` | `paths.default_data_root`、`paths.load_app_paths` | 「托管安装」布局：`%LOCALAPPDATA%\FineSub`（macOS `~/Library/Application Support/FineSub`），其 `locations.json` 的 `bigData` 决定 models/cache/agent-capsules 的根 | `LOCALAPPDATA`（`finesub_bootstrap/paths.py:131`）。**没有任何 `FINESUB_*` 变量能改它** |
| `finesub/paths.py:242` | `logs.log_directory` | 运行日志目录 `<user_data>\logs`；`<user_data>` 是 `%LOCALAPPDATA%\FineSub\user-data`（`FINESUB_CHECKOUT_DATA=0` 时） | 同上 |
| `finesub/llm/http.py:33`、`:604` | `logs.prune` | 删旧日志，目录同上 | 同上 |
| `finesub/paths.py:349-354` | `model_caches.SEPARATOR_CHECKPOINT`、`existing_separator_dir` | 分离器权重目录：`<FINESUB_MODEL_DIR>\audio-separator` 有 ckpt 就用，否则看 `~\.cache\audio-separator`（`finesub_bootstrap/model_caches.py:145-153`） | `FINESUB_MODEL_DIR`；没有时直接用 `~\.cache\audio-separator`（`finesub/paths.py:346`） |
| `finesub/speech/preprocessing/separator/separation.py:26`、`:295-310` | `model_caches.SEPARATOR_CHECKPOINT`、`model_fetch`、`download_routes.resolve_region`、`model_manifest.entry_for`、`paths.default_data_root` | 按模型清单把分离器的固定文件预先下载进分离器目录；下载线路状态写在 `default_data_root()\download-routes.json` | 清单：`FINESUB_MODEL_MANIFEST`（`finesub_bootstrap/model_manifest.py:26`、`:70-74`）；线路：`FINESUB_DOWNLOAD_REGION`、`FINESUB_GITHUB_FILE_PROXY` 等（`finesub_bootstrap/download_routes.py:58-62`）、`FINESUB_DOWNLOAD_SOURCES`（`finesub_bootstrap/download_sources.py:28`）；状态位置：`LOCALAPPDATA` |
| `finesub/speech/recognition/vad_asr_stage.py:1028` | `model_caches.WHISPER_REPO_ID`、`WHISPER_JA_REPO_ID` | 把模型名映射到清单 id（`whisper` / `whisper-ja`），再交给 `hf_weights.prepare` | 常量（钉在 `RUNTIME_PINS.json`） |
| `finesub/speech/runtime/hf_weights.py:73`、`:84`、`:88-96`、`:109`、`:128-131` | `model_ensure.pinned_revision`、`ensure_hf_model`、`pinned_snapshot_loadable`、`pinned_snapshot_dir`、`hf_verify.VerificationMismatch` | 确认 HF 模型在盘上且是钉住的 revision，不在就开子进程下载并校验；判断能否离线加载；返回快照目录 | hub 目录：`HF_HUB_CACHE` > `HUGGINGFACE_HUB_CACHE` > `HF_HOME\hub` > `<models_root>\huggingface\hub`（`finesub_bootstrap/model_ensure.py:75-85`）；`models_root` 与线路状态的 `data_root` 都取自 `resolve_managed_app_paths()`，即 `%LOCALAPPDATA%\FineSub`；清单与线路同上 |
| `finesub/speech/diarization/diarizen_backend.py:53`、`:116`；`nemotron3_backend.py:79`、`:113` | `model_ensure.pinned_snapshot_dir`、`hf_verify.VerificationMismatch` | 说话人分离后端按路径打开 TorchScript 快照 | 同上 |
| `finesub/speech/preprocessing/separator/separator_aoti.py:26`；`finesub/media/ffmpeg.py:10`；`finesub/llm/token_budget.py:33`；`finesub/llm/agent/local_agent.py:35`；`finesub/llm/knowledge/node/importer.py:20` | `processes.no_window` | 子进程创建标志（不弹窗） | 无 |
| `finesub/api_keys.py:17`、`:314-327` | `secrets`（`ensure_protected`、`read_env_file`） | 读 `.env`；**首次读到明文值时就地加密改写该文件**（见 1.3） | `FINESUB_ENV_FILE`（`finesub/paths.py:203`）；`FINESUB_ENV_PROTECT`（`finesub_bootstrap/secrets.py:281`） |
| `finesub/state.py:21-22`、`:72`、`:88` | `fsops.RECORD_REPLACE`、`replace_path`、`locks.holding_lock` | 读改写限流状态文件（JSON），旁边加 `.lock` | `FINESUB_STATE_FILE`（`finesub/paths.py:298`）；没有时见 1.8 |
| `finesub/batch_state.py:32`、`:84`、`:307` | `fsops.RECORD_REPLACE`、`replace_path`、`locks.holding_lock`、`try_lock` | 批处理状态写进日志目录 | 日志目录（同 `logs`） |
| `finesub/media/source.py:10` | `fsops.RECORD_REPLACE`、`replace_path`、`write_atomic` | 下载的源媒体与 URL→id 映射写在参考数据目录 `<user_data>\reference`（`finesub/paths.py:407-413`） | `LOCALAPPDATA`（经托管布局） |
| `finesub/llm/correction_mode.py:16`、`research.py:53`、`task_report.py:11`、`agent/agent_task_runtime.py:25`、`agent/local_agent.py:33`；`client.py:2110`、`agent/agent_session_host.py:392`、`agent/agent_cleanup.py:12` | `fsops.write_atomic`、`is_directory_link`、`remove_tree` | 原子写任务产物、删 capsule 等 | 调用方给的路径（任务目录或 capsule） |
| `finesub/llm/agent/agent_paths.py:22`；`agent_session_host.py:59`；`agent_task_runtime.py:26`；`agent_cleanup.py:13`；`local_agent.py:34`；`finesub/llm/knowledge/base.py:840` | `locks.AGENT_*_VARIABLE`、`AGENT_LOCATOR_KIND_MANAGED`、`LockUnavailable`、`holding_activity`、`holding_activity_barrier`、`holding_lock`、`try_lock` | 本地 Agent 的 capsule 目录与活动租约：`FINESUB_AGENT_*` 四个变量全给时用它们，否则检出模式用临时目录，否则用托管布局的 `agent_capsules` 和 `user_data`（`finesub/llm/agent/agent_paths.py:99`、`:106-160`），会话台账写在 `<user_data>\agent-sessions.jsonl` | `FINESUB_AGENT_CAPSULE_ROOT`、`…_ACTIVITY_ROOT`、`…_IDENTITY_ANCHOR`、`…_LOCATOR_KIND`（`finesub_bootstrap/locks.py:49-52`）；Nonoka 现在不设，所以落在 `%LOCALAPPDATA%\FineSub` |
| `finesub/llm/agent/agent_cleanup.py:11`；`local_agent.py:7496` | `agy_records.remove_project_records_under`、`agy_project_records_dir` | 读写 Antigravity CLI 自己的项目记录 `~\.gemini\config\projects`（`finesub_bootstrap/agy_records.py:24-25`），删指向已删 capsule 的记录 | 用户目录 |
| `finesub/api_http.py:34` | `http_client.in_tunnel_setup`、`watch_tunnel` | httpx 请求钩子，识别代理 CONNECT 超时 | 无 |
| `finesub/llm/correction_translation.py:32`；`llm/stages/correction/run.py:36` | `artifacts.ARTIFACT_DIR_SUFFIX`（`.llm-artifacts`）、`RESOLVED_SWITCHES_FILENAME`（`resolved-switches.json`） | 产物文件名常量（`finesub_bootstrap/artifacts.py:112`、`:116`） | 常量 |

另外两处不在 48 个名字里、但 WP-B/WP-R7 要知道：解锁文件 `finesub/pipeline.py:674`、`:691`、`:1737` 还 import `logs.prune`、`logs.open_log`、`locks.LockUnavailable`/`holding_lock`。依赖面扫描只扫上锁文件，所以 `open_log` 不在清单里——兼容包要么也提供它，要么 WP-B 拆 `pipeline.py` 时去掉这几处（`pipeline.py` 本来就解锁）。

### 1.2 密钥怎么读，保护格式是什么（第 2 问）

- **进程环境变量优先于文件。** 每个 key 池先 `os.getenv(<池的变量名>)`，没有才读 `.env` 解析结果（`finesub/api_keys.py:203-205`）；其他按名取的密钥同样环境优先（`finesub/api_keys.py:340-342`）。自定义提供商的密钥名来自模型目录的 `key_env` 列（`finesub/llm/llm_runtime.py:785-788`），走的也是这条路。所以**引擎不落盘就能读到全部密钥**，D3 据此定了只走环境变量。
- **例外**：`GEMINI_BASE_URL` 只从 `.env` 读（`finesub/llm/llm_runtime.py:402`，`media_upload.py:312` 同样）。它不是密钥，D3 规定只在用户改过它时写一个只含它的 `.env`。
- **本地 Agent CLI 拿不到这些变量**：引擎给 CLI 的环境是白名单（`finesub/llm/agent/local_agent.py:93` 起的 `ENV_ALLOWLIST`），并显式剥掉 `ANTHROPIC_API_KEY`、`OPENAI_API_KEY`、`GEMINI_FREE`、`GEMINI_PAID`、`EXA_KEYS`、`TAVILY_KEYS` 等（`:81-92`）。代价：ffmpeg、tokcount、模型下载子进程会继承这些变量（`finesub_bootstrap/model_ensure.py:248` 用 `{**os.environ, …}`）。
- **保护格式**（`finesub_bootstrap/secrets.py:1-37`、`:291-366`）：`.env` 里一行 `FINESUB_KEYRING=` 存主密钥。Windows 上是 `finesub$kek$v1$` + base64(16 字节盐 + 混淆后的 DPAPI 密文)，DPAPI 绑当前 Windows 账户、熵为 `b"finesub"`，混淆是 `shake_256(b"finesub"+盐)` 异或；其他平台是 `finesub$obf$v1$` 前缀、只混淆不包裹。每个值是 `fs$` + base64(12 字节盐 + 8 字节 blake2b 标签 + 密文)，密文 = 明文异或 `shake_256(master+"|v1|"+盐)`。名字、引号、逗号、注释原样保留。读取 `read_env_file` 是纯读（`:537-579`），能解开就返回明文，解不开的跳过并警告。
- **Nonoka 怎么写才能被它读出来**：按 D3 不再写密钥文件。若将来需要，写明文即可被读（`_TOKEN_RE` 不匹配的值按明文返回，`:561-563`），或用复制过来的 `seal`/`wrap_master` 写成同格式。D2 定的 `secrets.json` 用同一套信封算法，只是容器换成 JSON。

### 1.3 引擎会不会自己改写配置、密钥或做迁移（第 3 问）

- **会改写密钥文件**：`finesub/api_keys.py:317` 每进程首次读 `.env` 前调 `secrets.ensure_protected`，它在 `protection_enabled()` 为真且文件里有明文值时，加锁后把明文就地加密重写（`finesub_bootstrap/secrets.py:820-857`）。关法：`FINESUB_ENV_PROTECT=0`（`:281`）。D22 定了给引擎设 `0`；而且按 D3 派生文件里本来就没有密钥。
- **不改写配置文件**：引擎只读 `config.toml` 和 `model_catalog.toml`（`finesub/config.py:89`、`finesub/llm/routing/model_catalog.py:931-933`）。`finesub/llm/routing/migrate_catalog.py:365-388` 会转换旧格式目录，但只由它自己的命令行调用，引擎运行路径不调。
- **不做迁移**：`finesub_bootstrap/migrations/`、`user_config.py`、`update_check.py` 在 `finesub/` 里没有任何 import（全仓 grep）；它们只被 FineSub 自己的启动器 `shell.py` 用，Nonoka 也不调。
- **会写的其他东西**：限流状态（`FINESUB_STATE_FILE`）、下载线路状态和失败计数（托管 `data_root\download-routes.json`）、日志、capsule 与会话台账、编译缓存、模型目录。这些都是引擎状态，不是派生配置；D4/D21 把它们放进包的数据目录和 `runtime\cache\<包>\`。
- 结论：按 D6 每个进程一份派生文件、进程结束即删，再加 D22，派生文件不会被改掉后失去同步。

### 1.4 模型「确认并下载」的路径是否都被覆盖，模型目录必须保持的结构（第 4 问）

**不覆盖。** `FINESUB_MODEL_MANIFEST`、`FINESUB_DOWNLOAD_SOURCES`、`FINESUB_MODEL_DIR` 只覆盖「用哪份清单、哪份下载源、分离器放哪」。引擎确认和下载模型的入口有五条：

1. **HF 模型**（whisper、whisper-ja、qwen 裁判、说话人分离与声纹的 TorchScript）：`hf_weights.prepare` → `model_ensure.ensure_hf_model`（`finesub/speech/runtime/hf_weights.py:88-96`）。下载写到 `_hub_dir`，环境变量优先，Nonoka 设的 `HF_HOME` 生效。但传进去的 `data_root`（线路状态、失败计数）和 `models_root` 来自 `resolve_managed_app_paths()`，即 `%LOCALAPPDATA%\FineSub`，**没有变量能改**，只有兼容包的 `paths.default_data_root`/`load_app_paths` 能改（D21）。
2. **分离器固定文件**：`separation.place_separator_files`（`separation.py:295-310`），目录由 `FINESUB_MODEL_DIR` 决定，`data_root` 用 `paths.default_data_root()`，同上。
3. **分离器库自己下载**：上一步失败时 `audio-separator` 自己从 GitHub 下（`separation.py:285-290` 的说明），不经线路和校验。目录同 `FINESUB_MODEL_DIR`。
4. **加载器懒下载**：`pinned_snapshot_loadable` 为假时 faster-whisper / transformers 自己按 `HF_ENDPOINT`、`HF_HOME` 拉（`hf_weights.py:109-114`、`:140` 的 `offline_first`）。
5. **线路判定会联网**：`download_routes.resolve_region` 缓存过期时探测国家端点（`finesub_bootstrap/download_routes.py:147` 的 `resolve_region`、`:172` 的 `probe_region`）。兼容包改为直接返回启动上下文给的线路，不探测。

Silero VAD 从 `silero_vad` 包数据里加载，不下载（`finesub/speech/preprocessing/silero_ghost.py:74`）。

**模型目录必须保持的内部结构**（顶层叫什么、放在哪可以变，由 `HF_HOME`、`FINESUB_MODEL_DIR` 指过去）：

- HF：`<HF_HOME>\hub\models--<org>--<name>\{blobs,snapshots\<钉住的 revision>}`，加引擎的校验标记 `.finesub-verified.json`（`finesub_bootstrap/hf_verify.py:40`、`:58`）；本机没有 `refs\`，靠钉住的 revision 直接定位。`<HF_HOME>\xet\` 是 hf_xet 的缓存。目录名常量见 `finesub_bootstrap/model_caches.py:27-64`。
- 分离器：`<FINESUB_MODEL_DIR>\audio-separator\` 下 `model_bs_roformer_ep_317_sdr_12.9755.ckpt`、同名 `.yaml`、`download_checks.json`，各自的 `.lock`、`.verified`；编译产物在 `audio-separator\accel\<key>\`（`finesub/speech/preprocessing/separator/accel.py:166-176`）。
- `TORCH_HOME`：`<models>\torch`（本机不存在，引擎当前路径不写）。

### 1.5 怎么认出已装好的运行时，搬家后怎么不下载就判定就绪（第 5 问）

- **Python 运行时**是 `runtime\python\` 下的 uv venv。就绪 = `Scripts\python.exe` 存在 + 标记 `finesub-runtime.json` 等于 `{"schemaVersion": 2, "pythonVersion": "3.12", "runtimeLockHash": <锁的内容摘要>}` + 快速健康检查通过（`finesub_bootstrap/environment.py:334-372`、`:406-446`、`:1191-1206`）。摘要是去掉 `#` 注释行、统一换行后的 sha256，且**总是按标准锁算**，不按区域锁（`:278-290`）。标记只说明「装的是哪份锁」，不含路径。
- **工具**：`runtime\<工具>\<版本>\` + `current.json`（`{"current": "<版本>"}`），就绪 = 指针版本等于清单版本且必需文件都在（`finesub_bootstrap/resources.py:67-106`）。指针不含路径。
- **模型**：按清单逐个文件看大小，有摘要的看校验标记（`src/nonoka_x/model_install.py` 的 `missing_managed_models`，`finesub_bootstrap/model_ensure.py:296-302`）。
- **搬家**：三者的判定都不含绝对路径，按新目录名改名后用同样的规则判定即可就绪。要注意：
  - venv 的 `pyvenv.cfg` 的 `home` 是**基础解释器的绝对路径**。本机是 `D:\Envir\Python312`（安装器发现系统 Python 3.12 就复用，`environment.py:429-438`）。搬 venv 不动基础解释器；D10 定了把它登记为边界并在就绪检查里核对。
  - `Scripts\python.exe` 靠旁边的 `pyvenv.cfg` 定位，搬目录后能用；但 `Scripts\*.exe` 入口脚本（`audio-separator.exe`、`hf.exe` 等）和 `activate*` 写死了旧路径，搬后失效。引擎走 `python -m`，不用它们；WP-M1 搬完要实测一次 `python -c "import torch, ctranslate2"` 和一次 `--real`。
  - Nonoka 新的运行时管理器（WP-R3）可以直接认上面三种标记；也可以写自己的标记，但判定规则不能比这三种松。

### 1.6 `finesub.toml` 里的键、来源，以及 Nonoka 不管的键（第 6 问）

Nonoka 写的三个文件都由 `src/nonoka_x/settings.py` 生成，**`finesub.toml` 是就地合并写**（`src/nonoka_x/config_file.py:121` 的 `update_config_file`），所以用户手写的其他表一直留着。

| 文件 | 键 | 来源 |
|---|---|---|
| `finesub.toml` | `[nonoka_models]` 的 `default_provider`/`default_model`、`correction_*`、`research_*`、`knowledge_*` | 设置页的模型路由（`settings.py:282-300`、`:848-888`）；旧的 `planning_*`、`search_judge_*` 迁移时清掉（`:524-567`） |
| `finesub.toml` | `[llm.preferred_targets]` 的 `default`、`correction-mm`、`correction-text`、`research`、`knowledge`；旧组 `planning-mm`、`planning-text`、`search_judge` 写 `None`（即删除） | 由路由算出的目标 id（`settings.py:1020-1049`）：Gemini 用包内目录的 fact id，本地 Agent 用包内路由表的 target id，HTTP 提供商用 `nonoka-<provider>-<sha256 前 12 位>` |
| `finesub.toml` | `[llm]` 的 `default_target`、`task_route_*` | 一律写 `None` 删除（`settings.py:1050-1062`） |
| `model_catalog.toml` | 头注释一行 + 每个 HTTP 路由一行 `[models."<target>"]`：`provider_tier`、`provider_kind`、`base_url`、`key_env`、`display_name`、`api_model_id`、`max_input_tokens`、`max_output_tokens`、`supports_audio/video/native_search`（恒 false）、`thinking`、`token_scale`、`capability` | 路由 + `model_specs.spec_for` 的事实 + Base URL（`settings.py:953-1018`）；整文件重写 |
| `finesub.env` | `KEY_SPECS` 的 12 个密钥名、`BASE_URL_SPECS` 的 5 个地址、`NONOKA_CLOUD_KEY`、`NONOKA_CLOUD_BASE_URL`、引擎写的 `FINESUB_KEYRING` | 设置页与云端登录（`settings.py:18-60`、`:228-229`、`:796-846`、`:890-920`） |

Nonoka 不管、但引擎会读的 `config.toml` 表：`[providers]`、`[pools]`（`finesub/api_keys.py:132-157`）、`[segmentation]`、`[asr]`、`[vad]` 等 `finesub/settings.py` 声明的全部配置项、`[llm]` 下除上面两项外的其他表（如 `model_groups`）。本机的 `finesub.toml` 里就有一张历史遗留的 `[finoka_models]` 表和一个空的 `[llm]`。按 D11、D11c，迁移时这些都丢弃并在报告里列名。

另：每个任务的 `llm_model` 覆盖不写配置文件，由 worker 在进程内装上（`src/nonoka_x/worker.py:483` 起）。

### 1.7 环境变量全量清单与分类表初稿（第 7 问）

#### 1.7.1 FineSub 与 `finesub_bootstrap` 自己读的变量

全仓 grep 字面量（`finesub/`、`finesub_bootstrap/`，不含要迁走的 `speech/joint/`）：

| 变量 | 位置 | 作用；不设时 |
|---|---|---|
| `FINESUB_CONFIG_FILE` | `finesub/paths.py:217`、`config.py:48` | 配置文件；不设走默认发现（1.8） |
| `FINESUB_ENV_FILE` | `finesub/paths.py:203` | `.env`；同上 |
| `FINESUB_MODEL_CATALOG` | `finesub/paths.py:275` | 模型目录覆盖；同上 |
| `FINESUB_STATE_FILE` | `finesub/paths.py:298` | 限流状态；同上 |
| `FINESUB_KNOWLEDGE_ROOT` | `finesub/paths.py:423` | 知识库；同上 |
| `FINESUB_CHECKOUT_DATA` | `finesub/paths.py:143` | 检出模式是否用检出目录里的个人数据；默认开 |
| `FINESUB_ROOT` | `finesub/paths.py:150`、`:187`；`config.py:49` | 指定检出根；会让引擎把它当自己的检出 |
| `FINESUB_MODEL_DIR` | `finesub/paths.py:368` | 分离器目录与加速缓存的根；不设用 `~\.cache\audio-separator` |
| `FINESUB_SEPARATOR_ACCEL` | `separator/accel.py:48` | 关分离器加速 |
| `FINESUB_REFEREE_ACCEL` | `verification/qwen_referee.py:133` | 关 qwen 裁判编译 |
| `FINESUB_CT2_FLASH_ATTENTION` | `speech/runtime/device.py:518` | CTranslate2 flash attention |
| `FINESUB_DIARIZATION_ARTIFACTS` | `speech/diarization/backend.py:25` | 说话人分离调试产物 |
| `FINESUB_OVERLAP_CANDIDATES` | `llm/overlap_candidates.py:31` | 重叠候选开关 |
| `FINESUB_SPEAKER_JUDGE_MARKS` | `llm/stages/speaker_judge/run.py:117` | 说话人裁判标记 |
| `FINESUB_TASK_STRATEGY` | `llm/stages/task_graph.py:375` | 任务图策略 |
| `FINESUB_KB_SLIM` | `llm/kb_phonetic_hints.py:40` | 知识库读音提示精简 |
| `FINESUB_KNOWLEDGE_WRITE` | `llm/knowledge/base.py:778` | 工作树里允许写共享知识库 |
| `FINESUB_LOG_LEVEL` | `finesub/reporting.py:317` | 日志级别 |
| `FINESUB_AGENT_TRANSPORT` | `llm/agent/agent_transports.py:90` | 本地 Agent 传输方式 |
| `FINESUB_CODEX_FORK_ROLLOUT` | `llm/agent/local_agent.py:1756` | Codex fork 行为 |
| `FINESUB_CODEX_PROVIDER_TOKEN` | `llm/agent/codex_connection.py:47` | Codex 提供商令牌 |
| `FINESUB_TOOL_SESSION_OPENING` | `llm/agent/agent_session_host.py:514` | 工具会话开场 |
| `FINESUB_SHARE_MAINTAINER_TOKEN` | `llm/knowledge/share/server.py:66` | 知识库分享服务（不在任务路径上） |
| `FINESUB_MCP_*`（`ROOT`、`ASSIGNMENT`、`WORKER`、`SESSION`、`LOG`、`KNOWLEDGE_ROOT`、`TOOLS`、`WAIT_SECONDS`、`CHANNEL`、`REPLY_LIMIT`、`HOLD_ACCEPTED_SECONDS`、`PREDELIVERED`） | `llm/agent/agent_mcp_server.py`、`channels.py`、`agent_session_host.py:631-655`、`client.py:2476-2499` | 引擎给自己的 MCP 服务子进程设的；`FINESUB_MCP_WAIT_SECONDS` 父进程有就沿用（`agent_session_host.py:647`） |
| `FINESUB_AGENT_CAPSULE_ROOT`、`…ACTIVITY_ROOT`、`…IDENTITY_ANCHOR`、`…LOCATOR_KIND` | `finesub_bootstrap/locks.py:49-52`，读在 `finesub/llm/agent/agent_paths.py:106-111` | capsule 位置；不设用托管布局 |
| `FINESUB_TASK_ACTIVITY_ROOT` | `finesub_bootstrap/locks.py:43` | 只在 bootstrap 内用 |
| `FINESUB_ENV_PROTECT` | `finesub_bootstrap/secrets.py:281` | `.env` 自动加密；默认开 |
| `FINESUB_MODEL_MANIFEST` | `finesub_bootstrap/model_manifest.py:26` | 模型清单 |
| `FINESUB_DOWNLOAD_SOURCES` | `finesub_bootstrap/download_sources.py:28` | 下载源表 |
| `FINESUB_DOWNLOAD_REGION`、`FINESUB_PYPI_INDEX`、`FINESUB_HF_ENDPOINT`、`FINESUB_GITHUB_FILE_PROXY` | `finesub_bootstrap/download_routes.py:58-62` | 线路覆盖 |
| `FINESUB_HOME`、`FINESUB_BIG_DATA_DIR`、`FINESUB_EXTRA_PYTHONPATH`、`FINESUB_NO_UPDATE_CHECK`、`CI` | `runtime_health.py:595`、`shell.py:778`、`environment.py:223`、`update_check.py:54`、`:188` | 只在 FineSub 自己的启动器与安装器里读，引擎运行路径不读；随 `finesub_bootstrap` 删除消失 |
| `FINESUB_KEY_AZURE_SPEECH`、`FINESUB_KEY_OPENROUTER_STT`、`FINESUB_AZURE_SPEECH_ENDPOINT` | `speech/joint/stage.py:34-35`、`mai2.py:52` | 随 `speech/joint/` 迁出，归 Nonoka 自己的引擎 |
| `GEMINI_TOKEN_COUNTER_EXE` | `finesub_bootstrap/token_counter.py:25` | tokcount 路径；不设走检出路径与 `PATH` |
| `GEMINI_BASE_URL` | `llm/llm_runtime.py:390-402` | 只从 `.env` 读 |
| 密钥池与密钥：`GEMINI_FREE`、`GEMINI_PAID`、`EXA_KEYS`、`TAVILY_KEYS`、`MOSS_PRO`，以及目录 `key_env` 列出的名字 | `finesub/api_keys.py:55-61`、`:203`、`:340` | 环境优先，再 `.env` |
| `TORCHINDUCTOR_CACHE_DIR` | `separator/accel.py:54`、`:369`；`qwen_referee.py:805-807` | 父进程有值就不覆盖；没有时引擎自己设到它的缓存目录 |
| `INCLUDE` | `separator/separator_aoti.py:136` | 找 MSVC 头文件 |
| `HSA_OVERRIDE_GFX_VERSION` | `speech/runtime/device.py:159` | AMD GPU |
| `APPDATA`、`PATH` | `llm/agent/local_agent.py:2204-2206` | 找本地 Agent CLI |
| `CODEX_HOME` | `llm/agent/codex_connection.py:93` | Codex 配置目录 |
| `PYTHONPATH` | `client.py:2490`、`agent_session_host.py:645` | 原样（转绝对路径）传给 MCP 服务子进程 |
| `XDG_STATE_HOME` | `finesub/paths.py:314` | 只在没有状态文件变量且没有托管布局时 |
| `EDITOR`、`VISUAL` | `llm/knowledge/maintain.py:121` | 知识库维护命令行，不在任务路径上 |
| `LOCALAPPDATA` | `finesub_bootstrap/paths.py:131` | 托管数据根 |
| `PATHEXT`、`NoDefaultCurrentDirectoryInExePath` | `processes.py:58`、`shell.py:619` | 找可执行文件 |

#### 1.7.2 依赖库与系统需要的变量

以下按库源码与文档列出，`huggingface_hub` 已在本机运行时 `Lib\site-packages\huggingface_hub\constants.py` 核实；其余**由 WP-H 的读取审计实测补全**（计划第 4 节第 26 条）。

- Hugging Face：`HF_HOME`、`HF_HUB_CACHE`、`HUGGINGFACE_HUB_CACHE`、`HF_ENDPOINT`、`HF_TOKEN`、`HF_TOKEN_PATH`、`HF_HUB_OFFLINE`、`HF_HUB_DISABLE_*`（进度条、符号链接、遥测、隐式令牌、更新检查、xet）、`HF_HUB_ENABLE_HF_TRANSFER`、`HF_HUB_DOWNLOAD_TIMEOUT`、`HF_HUB_ETAG_TIMEOUT`、`HF_XET_CACHE`、`HF_XET_HIGH_PERFORMANCE`、`HF_DEBUG`；transformers 的 `TRANSFORMERS_CACHE`、`TRANSFORMERS_OFFLINE`。
- torch / CUDA：`CUDA_VISIBLE_DEVICES`、`TORCH_HOME`、`TORCHINDUCTOR_*`（含 `TORCHINDUCTOR_CACHE_DIR`）、`PYTORCH_CUDA_ALLOC_CONF`、`CUDA_LAUNCH_BLOCKING`、`TORCH_CUDA_ARCH_LIST`、`CUDA_PATH*`、`NVTOOLSEXT_PATH`（WP-Q 去掉后结果不变）。
- CTranslate2：`CT2_VERBOSE`、`CT2_CUDA_ALLOCATOR`、`CT2_FORCE_CPU_ISA`、`CT2_USE_EXPERIMENTAL_PACKED_GEMM`。
- OpenMP / MKL：`OMP_NUM_THREADS`、`MKL_NUM_THREADS`、`KMP_DUPLICATE_LIB_OK`；numba：`NUMBA_CACHE_DIR`、`NUMBA_NUM_THREADS`、`NUMBA_DISABLE_JIT`；Triton：`TRITON_CACHE_DIR`。
- uv：`UV_CACHE_DIR`、`UV_INDEX_URL`、`UV_PYTHON*`（只在安装时）。
- 代理与证书：`HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY`、`NO_PROXY` 及小写形式；`SSL_CERT_FILE`、`SSL_CERT_DIR`、`REQUESTS_CA_BUNDLE`、`CURL_CA_BUNDLE`。**没有代理变量时 Python 在 Windows 上会去注册表读系统代理**（`src/nonoka_x/download_route.py:539-541` 的说明），D17 定了总是显式写。
- Python：`PYTHONPATH`、`PYTHONHOME`、`PYTHONSTARTUP`、`PYTHONUTF8`、`PYTHONIOENCODING`、`PYTHONNOUSERSITE`、`PYTHONDONTWRITEBYTECODE`。`-I` 让解释器忽略全部 `PYTHON*`，但 venv 的 `distutils-precedence.pth` 照样执行并读 `SETUPTOOLS_USE_DISTUTILS`（本机 `runtime\python\Lib\site-packages\distutils-precedence.pth`）。
- Windows 系统：`SystemRoot`、`windir`、`SystemDrive`、`TEMP`、`TMP`、`USERPROFILE`、`HOMEDRIVE`、`HOMEPATH`、`APPDATA`、`LOCALAPPDATA`、`ProgramData`、`ProgramFiles`、`ProgramFiles(x86)`、`ProgramW6432`、`CommonProgramFiles*`、`ComSpec`、`PATHEXT`、`NUMBER_OF_PROCESSORS`、`PROCESSOR_ARCHITECTURE`、`PROCESSOR_IDENTIFIER`、`OS`、`USERNAME`、`USERDOMAIN`、`COMPUTERNAME`。DPAPI 不靠环境变量，靠已加载的用户配置文件。
- MSVC（AOTI 编译）：`INCLUDE`、`LIB`、`LIBPATH`、`VSINSTALLDIR`、`VCToolsInstallDir`、`WindowsSdkDir` 等，由 `src/nonoka_x/local_provider.py:167` 的 `_prepare_msvc_environment` 用 vswhere 探测后写入；它在外部 `INCLUDE` 已含 `<array>` 时直接返回（`:177-179`），即外部值会漏进来。
- 本地 Agent CLI：引擎白名单 `local_agent.py:93` 起：`APPDATA`、`CLAUDE_CONFIG_DIR`、`CODEBUDDY_CONFIG_DIR`、`CODEBUDDY_INTERNET_ENVIRONMENT`（及拼错的 `…ENVIROMENT`）、`CODEX_HOME`、`ALL_PROXY`、`CODEX_CA_CERTIFICATE`、`HTTP_PROXY`、`HTTPS_PROXY`、`NODE_EXTRA_CA_CERTS`、`NO_PROXY`、`SSL_CERT_DIR`、`SSL_CERT_FILE`、`COMSPEC`、`DSH_HOME`、`LOCALAPPDATA`、`PATH`、`PATHEXT`、`PROGRAMDATA`、`PROGRAMFILES`、`PROGRAMFILES(X86)`、`SYSTEMDRIVE`、`SYSTEMROOT`、`TEMP`、`TMP`、`USERDOMAIN`、`USERNAME`、`USERPROFILE`、`WINDIR`。

#### 1.7.3 分类表初稿（第 4 节第 21 条）

「系统必需」从父进程按名原样带；「Nonoka 设定」由核心或包内桥接无条件写入；表外一律不存在。WP-H 把它落成桥接模块里的唯一事实来源，覆盖扫描以它为准。

| 类 | 变量 | 谁写 | 依据 |
|---|---|---|---|
| 系统必需 | `SystemRoot`、`windir`、`SystemDrive`、`TEMP`、`TMP`、`USERPROFILE`、`HOMEDRIVE`、`HOMEPATH`、`APPDATA`、`LOCALAPPDATA`、`ProgramData`、`ProgramFiles`、`ProgramFiles(x86)`、`ProgramW6432`、`CommonProgramFiles`、`CommonProgramFiles(x86)`、`ComSpec`、`PATHEXT`、`NUMBER_OF_PROCESSORS`、`PROCESSOR_ARCHITECTURE`、`PROCESSOR_IDENTIFIER`、`OS`、`USERNAME`、`USERDOMAIN`、`COMPUTERNAME`；macOS 为 `HOME`、`USER`、`TMPDIR`、`LANG` | `engine_host` | Python、torch、DPAPI、子进程的基本需要 |
| 系统必需（只给纠错包） | `CLAUDE_CONFIG_DIR`、`CODEBUDDY_CONFIG_DIR`、`CODEBUDDY_INTERNET_ENVIRONMENT`、`CODEBUDDY_INTERNET_ENVIROMENT`、`CODEX_HOME`、`DSH_HOME`、`CODEX_CA_CERTIFICATE`、`NODE_EXTRA_CA_CERTS`、`SSL_CERT_FILE`、`SSL_CERT_DIR` | `engine_host` | D16；第 25 条边界 |
| Nonoka 设定（中性） | `PATH`（托管工具目录 + 本地 Agent CLI 目录 + `SystemRoot\System32` 等系统目录，拼法由 WP-H 定） | `engine_host` | 第 21 条；tokcount 继续靠 `PATH` 被找到 |
| Nonoka 设定（中性） | `PYTHONPATH`（该包、其兼容包、yt-dlp）、`PYTHONNOUSERSITE=1`、`PYTHONIOENCODING=utf-8`、`PYTHONUTF8=0` | `engine_host` | D9；后两项 worker 自己在 `-I` 下不认，只给孙进程（见 1.10） |
| Nonoka 设定（中性） | `HTTP_PROXY`、`HTTPS_PROXY`、`NO_PROXY` 及小写形式 | `engine_host`（值来自启动上下文） | D17 |
| Nonoka 设定（中性） | `CUDA_VISIBLE_DEVICES` | `engine_host`（值来自任务请求的设备） | 第 21 条；去掉 `worker.py:593` 的条件设置 |
| Nonoka 设定（中性） | MSVC 的 `INCLUDE`、`LIB`、`LIBPATH` 等 | `engine_host`，总是由 vswhere 探测得出，不看父进程 | 第 25 条边界（MSVC 工具链） |
| Nonoka 设定（包内） | `FINESUB_CONFIG_FILE`、`FINESUB_ENV_FILE`、`FINESUB_MODEL_CATALOG`（都指 `run\<进程>\` 下的派生文件，`.env` 可以不存在）、`FINESUB_CHECKOUT_DATA=0`、`FINESUB_STATE_FILE`、`FINESUB_ENV_PROTECT=0` | 包内桥接 | D6、D22 |
| Nonoka 设定（包内，只纠错包） | `FINESUB_KNOWLEDGE_ROOT`、密钥（按名，来自启动上下文） | 包内桥接 | D3、D4 |
| Nonoka 设定（包内，只转写包） | `FINESUB_MODEL_DIR`、`HF_HOME`、`TORCH_HOME`、`HF_TOKEN`（用户配了才有） | 包内桥接 | D5 |
| 不存在 | 其余全部，含 `FINESUB_ROOT`、全部行为开关类 `FINESUB_*`、`FINESUB_AGENT_*`、`FINESUB_MCP_*`（引擎自己给孙进程设）、`FINESUB_MODEL_MANIFEST`、`FINESUB_DOWNLOAD_*`、`FINESUB_HF_ENDPOINT`、`FINESUB_PYPI_INDEX`、`FINESUB_GITHUB_FILE_PROXY`、`GEMINI_TOKEN_COUNTER_EXE`、`TORCHINDUCTOR_CACHE_DIR`（必须不存在，引擎才会用自己的缓存目录）、`HF_ENDPOINT`（线路由兼容包按上下文给下载子进程）、`HF_HUB_CACHE`、`UV_*`、`CUDA_PATH*`、`XDG_STATE_HOME`、`SETUPTOOLS_USE_DISTUTILS`、`HSA_OVERRIDE_GFX_VERSION`、`EDITOR`、`VISUAL` | — | 第 21 条 |

### 1.8 默认位置的文件发现，每一处怎么封（第 8 问）

| 默认位置 | 位置 | 怎么封 |
|---|---|---|
| 检出目录里的 `.env`、`config.toml`、`model_catalog.toml`、`.state`、`knowledge\`、日志、`data\reference` | `finesub/paths.py:194-197`、`:207`、`:221`、`:279`、`:301-303`、`:426-428`、`:407-409` | `FINESUB_CHECKOUT_DATA=0`，并把前五项都用变量显式指定 |
| **检出根判定**：目录里同时有 `pyproject.toml` 和 `src\finesub` 就算检出（`finesub/paths.py:24-31`），从 `__file__` 向上找（`:187-191`）。vendor 目录、`app-resources\<版本>\nonoka_x\third_party\finesub` 都满足 | `finesub/paths.py:172-191` | `FINESUB_CHECKOUT_DATA` **挡不住**下面三项（它们直接调 `own_checkout_root()`）：编译缓存 `<检出>\cache`（`:253-255`，qwen 裁判加速 `qwen_referee.py:341-343`）、tokcount 候选 `<检出>\bin\windows-amd64\tokcount.exe`（`:324-332`）、分离器加速缓存（`accel.py:177-180`，有 `FINESUB_MODEL_DIR` 时不走）。封法：引擎包的目录**不能**同时含 `pyproject.toml` 和 `src\finesub`（WP-P1 的布局约束）；在那之前 WP-R7 删掉 vendor 的 `pyproject.toml` 也就封住了 |
| `FINESUB_ROOT` 指定的检出 | `finesub/paths.py:150`、`:187`；`config.py:49` | 分类表「不存在」 |
| 当前目录 | `resolve_checkout_root` 先从 `Path.cwd()` 向上找（`finesub/paths.py:161`），`config.py` 的缓存键含 `os.getcwd()` | `own_checkout_root` 先看 `__file__`，`__file__` 不在检出里就返回 None（`:187-190`），当前目录不起作用；worker 的当前目录设成任务目录 |
| 托管布局 `%LOCALAPPDATA%\FineSub`（下载线路状态、日志、参考数据、capsule、会话台账、`.env`/`config.toml` 的最后回落） | `finesub_bootstrap/paths.py:124-136`、`finesub/paths.py:40-58` | 兼容包的 `paths.default_data_root`/`load_app_paths` 返回启动上下文给的目录（D21）；D8 定了不碰旧目录 |
| `locations.json` 的 `bigData` 记录 | `finesub_bootstrap/paths.py:171-173`、`:223-253` | 兼容包的 `load_app_paths` 不读记录 |
| `~\.cache\audio-separator` | `finesub/paths.py:346`、`finesub_bootstrap/model_caches.py:145-153` | `FINESUB_MODEL_DIR` 总是设置；兼容包的 `existing_separator_dir` 只返回托管目录 |
| `~\.cache\huggingface` | `finesub_bootstrap/model_caches.py:114-142` | `HF_HOME` 总是设置（`existing_hf_home` 在任一 HF 变量存在时直接用托管目录） |
| `~\.cache\finesub`（编译缓存最后回落） | `finesub/paths.py:261` | 兼容包让托管布局永远存在 |
| `~\.finesub\state`、`$XDG_STATE_HOME\finesub` | `finesub/paths.py:314-316` | `FINESUB_STATE_FILE` 总是设置 |
| `~\.gemini\config\projects`（agy 自己的记录） | `finesub_bootstrap/agy_records.py:24-25` | 不封：属于第 25 条「本地 Agent CLI 自己的状态」 |
| 注册表系统代理 | Python 标准库 `urllib.request.getproxies`（`src/nonoka_x/download_route.py:539-541`） | D17 |
| 编译缓存 `TORCHINDUCTOR_CACHE_DIR` 的系统临时目录默认 | `separator/accel.py:369-374` | 引擎自己设到它的缓存目录，只要父进程不带这个变量 |
| 自动更新检查 | `finesub_bootstrap/update_check.py` | 引擎不调；兼容包不提供该模块 |

### 1.9 主进程里的 FineSub（第 9 问）

位置与计划第 2.5 节一致。实测（隔离解释器里 import 并钩住 `os.environ.get`/`os.getenv`）：

| 位置 | 用的东西 | 读不读环境或配置 | 结论 |
|---|---|---|---|
| `src/nonoka_x/settings.py:365`、`:384` | `finesub.llm.routing.model_routes`（`default_model_routes`、`load_model_routes`） | `default_model_routes` 经 `finesub.config.read_config_with_path` 读 `FINESUB_CONFIG_FILE`/`FINESUB_ROOT`/当前目录指向的配置（`model_routes.py:1680-1685`、`config.py:40-48`）；import 时还拉进 `finesub_bootstrap.token_counter` | 必须挪进纠错包进程，走协议查询 |
| `src/nonoka_x/settings.py:476`、`:947` | `finesub.llm.routing.model_catalog.default_model_catalog` | 读 `FINESUB_MODEL_CATALOG` 等（`model_catalog.py:928-933`） | 必须挪进纠错包 |
| `src/nonoka_x/settings.py:1064-1066` | `finesub.config.clear_config_cache` 等 | 清进程内缓存；挪走上两项后不再需要 | 删 |
| `src/nonoka_x/projector.py:224` | `finesub.speech.diarization.product.read_product` | import 链只有 `finesub.reporting`、`text`、`subtitles.stable_rows`、`speech.diarization.{labels,judgments,product}`；import 时零次环境读取，无 torch、numpy；`reporting` 只在配置日志时读 `FINESUB_LOG_LEVEL`（`reporting.py:317`） | 可证明是纯数据模块，WP-P1 前可列白名单；最终随 `projector` 的说话人解析一起搬进转写包适配器（第 33 条） |
| `src/nonoka_x/gpu_tier.py:48` | `finesub.speech.runtime.resources.gpu_tier_cli_choices` | import 与调用都零次环境读取，无 torch | 同上，纯数据；最终改为协议查询 |

另外，sidecar 主进程自己把 `FINESUB_*` 写进 `os.environ`（`src/nonoka_x/sidecar.py:183` → `settings.py:620-628`），worker 再继承，这正是第 2.5 节的 `setdefault` 漏点；Nonoka 自己的 `download_route.py:366` 也直接读 `FINESUB_GITHUB_FILE_PROXY`，违反第 27 条，WP-R2 要改。

### 1.10 隔离模式的代价（第 10 问）

`-I` = 忽略全部 `PYTHON*` 变量 + 不加用户级 site-packages + 不把脚本目录放进 `sys.path`。现在靠这些生效的东西：

1. **`PYTHONPATH` 里的三段**：`src`（Nonoka）、`<vendor>\src`（FineSub 与 `finesub_bootstrap`）、yt-dlp 目录（`src/nonoka_x/local_provider.py:972-976`、`provision.py:985-989`）。改由 Nonoka 的启动入口在 import 任何东西之前设 `sys.path`。
2. **引擎拉起的孙进程**：`sys.executable -m finesub.llm.agent.agent_mcp_server`（`finesub/llm/client.py:2503`、`agent_session_host.py:657`）和 `sys.executable -m finesub_bootstrap.model_ensure`（`finesub_bootstrap/model_ensure.py:241`）不带 `-I`，靠继承的 `PYTHONPATH` 找包（MCP 服务的 `PYTHONPATH` 从父进程环境转成绝对路径传，`client.py:2490-2491`）。上锁文件不能改，所以按 D9：`PYTHONPATH` 和 `PYTHONNOUSERSITE=1` 作为「Nonoka 设定」写进环境。注意 MCP 服务是由 Agent CLI 拉起的，CLI 只转交引擎给的那份 `env`，`PYTHONNOUSERSITE` 不在其中，所以 MCP 服务里用户级 site-packages 仍然生效——这是 D9 留下的一个边界，WP-H 报告里列出。
3. **`PYTHONIOENCODING=utf-8`、`PYTHONUTF8=0`**（`local_provider.py:983-984`）：`-I` 下 worker 自己不再认它们。worker 的 NDJSON 标准输出要是 UTF-8，改由入口 `sys.stdout.reconfigure(encoding="utf-8")`；UTF-8 模式在 3.12 默认关，等同 `PYTHONUTF8=0`，MSVC 的 GBK 输出解码不受影响。孙进程仍需要这两个变量，留在「Nonoka 设定」里。
4. **venv 里的 `.pth`**：`-I` 不影响 site-packages 的 `.pth`（`_virtualenv.pth`、`distutils-precedence.pth`），照常执行。
5. **知识库 worker** 用 `subprocess.run` 同样拿 `_engine_environment()`（`local_provider.py:939-946`），一并改。

### 1.11 兼容包的接口（第 11 问）

总原则（第 4 节第 16、35 条）：真实包、名字不多不少、不 import torch/numpy、不 import `nonoka_x`；拿运行时位置、模型目录、下载线路、包数据目录的来源是**启动上下文**（由包内桥接在 import `finesub` 之前装进兼容包的一个模块级对象，WP-R7/WP-P1 定具体形式）。实现来源：「复制」= 复制原模块代码、注明来源；「上下文」= 行为保留，但位置与线路取自启动上下文。

| 名字 | 引擎拿它做什么 | 读哪个目录或文件 | 兼容包里的实现 |
|---|---|---|---|
| `agy_records:agy_project_records_dir`、`remove_project_records_under` | 清理 agy CLI 指向已删 capsule 的项目记录 | `~\.gemini\config\projects` | 复制 |
| `artifacts:ARTIFACT_DIR_SUFFIX`、`RESOLVED_SWITCHES_FILENAME` | 产物文件名 | — | 复制常量；**决定结果**，值由 `RUNTIME_PINS.json` 的常量与 LLM 回放证明 |
| `download_routes:resolve_region` | 下载线路 | 原：`<data_root>\download-routes.json` + 联网探测 | 上下文：直接返回上下文的线路，不读不写不探测 |
| `fsops:RECORD_REPLACE`、`is_directory_link`、`remove_tree`、`replace_path`、`write_atomic` | 原子写、删目录 | 调用方给的路径 | 复制 |
| `hf_verify:VerificationMismatch` | 校验失败的异常类 | — | 复制（`model_ensure` 也要用 `hf_verify` 的其余函数，模块内部一起复制） |
| `http_client:in_tunnel_setup`、`watch_tunnel` | httpx 钩子 | — | 复制 |
| `locks:AGENT_ACTIVITY_ROOT_VARIABLE`、`AGENT_CAPSULE_ROOT_VARIABLE`、`AGENT_IDENTITY_ANCHOR_VARIABLE`、`AGENT_LOCATOR_KIND_MANAGED`、`AGENT_LOCATOR_KIND_VARIABLE` | 变量名与定位种类常量 | — | 复制常量 |
| `locks:LockUnavailable`、`holding_activity`、`holding_activity_barrier`、`holding_lock`、`try_lock` | 文件锁与活动租约 | 调用方给的路径 | 复制 |
| `logs:log_directory`、`prune` | 日志目录与清理 | `<user_data>\logs` | 复制；`user_data` 由 D21 落到 `engines\<包>\user-data\` |
| `model_caches:SEPARATOR_CHECKPOINT`、`WHISPER_REPO_ID`、`WHISPER_JA_REPO_ID` | 用哪个 checkpoint、哪个仓库 | — | 复制常量；**决定结果**，值由 `RUNTIME_PINS.json` 的 `engine_constants` 证明 |
| `model_caches:existing_separator_dir` | 分离器目录 | 原：托管目录，否则 `~\.cache\audio-separator` | 上下文：只返回托管目录（第 1.8 节） |
| `model_ensure:pinned_revision`、`pinned_snapshot_dir`、`pinned_snapshot_loadable`、`ensure_hf_model` | 钉住的 revision、快照目录、能否离线加载、缺了就下载 | `HF_HOME\hub`；清单 | 复制算法；清单读 Nonoka 的模型清单（路径来自上下文，不读 `FINESUB_MODEL_MANIFEST`）；下载子进程的线路取上下文。**决定结果**（revision），由 `RUNTIME_PINS.json` 与 `--real` 证明。「缺了自己下载」保留（第 16 条）。下载子进程是 `sys.executable -m finesub_bootstrap.model_ensure`，兼容包必须保留这个模块名可执行（D9 的 `PYTHONPATH` 让它找得到） |
| `model_fetch`（模块）与 `model_fetch:fetch_fixed_files` | 分离器固定文件下载、镜像回退、失败计数 | 目标目录；`<data_root>` 下的失败计数 | 复制算法，线路与 `data_root` 取上下文 |
| `model_manifest:entry_for` | 清单条目 | 原：`FINESUB_MODEL_MANIFEST` 或包内 `model-manifest.json` | 上下文：读 Nonoka 的清单 |
| `paths:default_data_root` | 托管数据根 | 原：`%LOCALAPPDATA%\FineSub` | 上下文：`engines\<包>\`（D21） |
| `paths:load_app_paths` | 托管布局 | 原：数据根 + `locations.json` | 上下文：`data_root` = `engines\<包>\`，`user_data` = `engines\<包>\user-data\`，`agent_capsules` = `engines\<包>\agent-capsules\`，`models` = `runtime\models\`，`cache` = `runtime\cache\<包>\`；返回同字段的 `AppPaths`（D21） |
| `paths:packaged_app_root`、`packaged_app_paths` | 是否装在 `app\versions\` 里 | `__file__` | 复制（Nonoka 布局里恒为 None） |
| `processes:no_window` | 创建标志 | — | 复制 |
| `secrets`（模块）与 `secrets:ensure_protected`、`read_env_file` | 读 `.env` | `FINESUB_ENV_FILE` | 复制。**决定结果**（读出的值），由调用等价闸门证明 |
| `token_counter`（模块）与 `token_counter:CHECKOUT_RELATIVE_PATH`、`configured_path`、`find_on_path` | 找 tokcount | `GEMINI_TOKEN_COUNTER_EXE`、`PATH` | 复制。**决定结果**（计数方式），由 WP-H 的 `token_counter_source` 核对证明 |

两个包各需要哪些名字由 WP-P0 第 4 问定；`model_*`、`hf_verify` 只有转写包用，`secrets`、`agy_records`、`artifacts` 只有纠错包用（按上表的调用点推断，WP-P0 核实）。

### 1.12 新的数据布局（第 12 问）

**主数据目录**（名字与位置不变，由 Go 决定：Windows `%APPDATA%\Nonoka X`，macOS `~/Library/Application Support/Nonoka X`，`desktop/internal/app/runtime.go:245-260`）只放 Nonoka 自己的东西：

```text
<主数据目录>\
├── settings.json            # D1：模型路由、Base URL、云端账号地址等非密钥设置
├── secrets.json             # D2：全部密钥，信封加密
├── preferences.json, storage.json, library.json, task-history.json, cloud-*.json, …   # 现有 Nonoka 文件，不动
├── runtime-state\           # D18：运行时管理器的小状态
│   └── download-routes.json
├── engines\                 # D4：引擎包的私有目录（第 33 条允许）
│   ├── finesub-transcribe\
│   │   ├── run\<进程>\      # D6：派生 finesub.toml、model_catalog.toml（及按 D3 可能有的 .env）
│   │   ├── user-data\       # D21：日志、参考数据
│   │   └── state            # FINESUB_STATE_FILE
│   └── finesub-correct\
│       ├── knowledge\       # 知识库（第 33 条）
│       ├── state            # 限流状态（原 <主数据目录>\state）
│       ├── run\<进程>\
│       ├── user-data\       # 日志、agent-sessions.jsonl、.task-activity
│       └── agent-capsules\
├── tasks\<id>\
│   └── native\<包 id>\      # D7：引擎原生产物
├── documents\, videos\, thumbnails\, plugins\, …          # 不动
├── bootstrap\, app-resources\                             # D14：启动器自己的，不动
```

**大文件根**（D5；默认 `<主数据目录>\runtime`，由 `storage.json` 的 `runtimeDir` 记录搬过的位置；Go 的常量 `runtimeName` 从 `finesub` 改成 `runtime`，`desktop/internal/storage/locations.go:30`）：

```text
<大文件根>\
├── python\                  # ASR 运行时 venv（组件 asr-python）
├── tools\<工具>\<版本>\ + tools\<工具>\current.json   # ffmpeg、uv、tokcount、yt-dlp、aria2c、node、pot-provider
├── models\
│   ├── huggingface\{hub,xet}\   # HF_HOME；内部结构见 1.4
│   ├── audio-separator\         # FINESUB_MODEL_DIR\audio-separator；含 accel\
│   └── torch\                   # TORCH_HOME
└── cache\
    ├── uv\                      # 只在安装时用
    └── <包 id>\                 # D21：引擎编译缓存（qwen 裁判加速等）
```

D18 不要 `locations.json`，所以 FineSub 的 `.finesub-store.json` 标记和 `register-location.cmd`（后者往 `%LOCALAPPDATA%\FineSub` 写记录）也不再需要。

**运行时组件**（D12，与清单条目一一对应，`EngineSpec.environment` 和 `PACKAGE.json` 用这些 id）：`asr-python`；工具 `ffmpeg`（含 ffprobe）、`uv`、`tokcount`、`yt-dlp`、`aria2c`、`node`、`pot-provider`；模型按模型清单的 id 各一个（`separator`、`whisper`、`whisper-ja`、`qwen-referee`、说话人分离与声纹的各条目，以清单为准）。共用组件（如 `ffmpeg`、`asr-python`）只算一次。

**上锁引擎在模型目录内部依赖的结构**：见 1.4。

### 1.13 旧布局清单（第 13 问）

各历史形态：

- **Windows 安装版**：主数据目录 `%APPDATA%\Nonoka X`；更早的 `%APPDATA%\Finoka` 由桌面端启动时整体改名过来（`desktop/internal/app/runtime.go:196-235`，先于本迁移执行）。
- **macOS**：`~/Library/Application Support/Nonoka X`（同样从 `…/Finoka` 改名）。只有媒体工具，没有 Python 运行时（`src/nonoka_x/provision.py:233`），清单是 Nonoka 自己的 `runtime-manifest.macos-*.json`；密钥是 `finesub$obf$v1$` 混淆；引擎托管根是 `~/Library/Application Support/FineSub`。
- **`storage.json` 改过大文件根的**：`runtimeDir` = `<所选位置>\Nonoka X\finesub`（`desktop/internal/storage/locations.go:28-36`）；本机即 `D:\AIProject\nonoka-sub-envir\Nonoka X\finesub`。桌面端搬家的半途标记见 `desktop/internal/storage/move.go:49`，迁移前先确认没有未完成的搬家。
- **离线包**：`offline-pack\` 在可执行文件旁边，只是下载来源（`desktop/internal/app/offline_pack.go` 头注释），装出来的布局与普通安装相同，不迁移。
- **开发检出**：`scripts/run_local_sidecar.py:18` 用 `<仓库>\.local` 作主数据目录，大文件根默认 `<仓库>\.local\finesub`；vendor 是 `<仓库>\third_party\finesub`，是检出根，qwen 裁判编译缓存在 `<仓库>\third_party\finesub\cache\`。本机没有 `.local`。

逐项（「重建」= 可以不搬，删掉后自动重建；迁移规则见第 2.7 节）：

| 旧位置 | 新位置 | 搬法 | 可否重建 |
|---|---|---|---|
| `<主>\finesub.env`（+`.lock`） | 认识的密钥 → `secrets.json`；认识的 Base URL 与 `NONOKA_CLOUD_BASE_URL` → `settings.json`；`NONOKA_CLOUD_KEY` → `secrets.json`；`FINESUB_KEYRING` 不迁；不认识的丢弃并列名（D13） | 解密后重新封装；原文件改名备份 | 否 |
| `<主>\finesub.toml`（+`.lock`） | `[nonoka_models]` → `settings.json`；`[llm.preferred_targets]`、`[llm]` 的两类键是派生的，不迁；其余丢弃并列名（D11、D11c） | 转换；原文件改名备份 | 路由部分否 |
| `<主>\model_catalog.toml`、`model_catalog.psv` | 不迁（每进程派生） | 删除 | 是 |
| `<主>\state`（+`state.lock`） | `engines\finesub-correct\state` | 同盘改名 | 是（限流计数） |
| `<主>\knowledge\`（+`knowledge.lock`） | `engines\finesub-correct\knowledge\` | 同盘改名，内容一字不改 | 否 |
| `<主>\finesub-data\download-routes.json` | `runtime-state\download-routes.json` | 改名 | 是 |
| `<主>\finesub-data\locations.json`（+`.lock`） | 不要了（D18） | 删除 | — |
| `<主>\tasks\<id>\` 里的 FineSub 产物 | `tasks\<id>\native\<包 id>\` | WP-M2 做，归属由 WP-P0 第 6 问定 | 否 |
| `<主>\bootstrap\`、`app-resources\` | 不动（D14） | — | 是 |
| `<主>\app-resources\<版本>\…\third_party\finesub\cache\`（若有） | 不迁 | 随版本清理 | 是 |
| 大文件根 `<旧根>`（默认 `<主>\finesub`，或 `storage.json` 记录的位置） | `<旧根的父目录>\runtime`；`storage.json` 有记录时改写记录 | 同盘整体改名；不同盘不可能（新根与旧根同父目录） | 否（数 GB） |
| `<旧根>\runtime\python\` | `<新根>\python\` | 改名；`pyvenv.cfg` 的 `home` 不动（D10） | 否 |
| `<旧根>\runtime\<工具>\` | `<新根>\tools\<工具>\` | 改名 | 否 |
| `<旧根>\runtime\python-builds\`（Nonoka 托管的基础 Python，若有） | `<新根>\tools\python-builds\` | 改名，并把 `python\pyvenv.cfg` 的 `home` 改成新路径（D23） | 否 |
| `<旧根>\models\` | `<新根>\models\` | 随根改名，内部不动 | 否 |
| `<旧根>\cache\`（`uv\` 等） | `<新根>\cache\` | 随根改名 | 是 |
| `<旧根>\.finesub-store.json`、`register-location.cmd` | 不要了 | 删除 | — |
| `%LOCALAPPDATA%\FineSub\`（`agent-capsules\`、`user-data\{agent-sessions.jsonl,.task-activity,…}`、`download-routes.json`） | 不动（D8） | — | — |
| 开发检出 `<仓库>\.local\…` | 同上各行，以 `.local` 为主数据目录 | 同上 | 同上 |

### 1.15 引擎包的数据目录（第 15 问）

- **位置与命名**：`<主数据目录>\engines\finesub-transcribe\`、`…\engines\finesub-correct\`（D4；包 id 与计划第 4 节第 27 条的目录名一致）。删掉包的代码，数据目录留着（第 33 条）。
- **各放什么**：见 1.12 的树。转写包：`run\`、`user-data\`、`state`（几乎不写）；纠错包：`knowledge\`、`state`、`run\`、`user-data\`（含 `agent-sessions.jsonl`，D8 定了从零开始）、`agent-capsules\`。**LLM 路由不在包里**：它是用户设置，存在 `settings.json`（D1、第 4 节第 14 条），经启动上下文给包。编译缓存在大文件根 `runtime\cache\<包 id>\`（D21）。
- **私有附件**：`tasks\<id>\native\<包 id>\`（D7）。
- **启动上下文的字段**（够包内桥接派生出与现在相同的文件和环境变量；传输形式由 WP-P1 的进程协议定）：

| 字段 | 内容 | 派生出什么 |
|---|---|---|
| `schema`、`package_id`、`purpose` | 版本；`finesub-transcribe`/`finesub-correct`；`task`/`llm`/`knowledge` | — |
| `paths.package_data` | `engines\<包>\` | 兼容包托管布局（D21）、`FINESUB_STATE_FILE`、`FINESUB_KNOWLEDGE_ROOT`（纠错包） |
| `paths.run` | `engines\<包>\run\<进程>\` | `FINESUB_CONFIG_FILE`、`FINESUB_MODEL_CATALOG`、`FINESUB_ENV_FILE` 指向的派生文件 |
| `paths.task`、`paths.native` | 任务目录与 `native\<包 id>\`（`purpose=task` 时） | worker 的当前目录、产物位置 |
| `runtime.models`、`runtime.cache` | `runtime\models\`、`runtime\cache\<包>\` | `FINESUB_MODEL_DIR`、`HF_HOME`、`TORCH_HOME`；兼容包的 `models`/`cache` |
| `runtime.tools` | 各工具可执行文件或目录（ffmpeg、ffprobe、tokcount、yt-dlp 等） | 由 `engine_host` 拼进 `PATH`、`PYTHONPATH`（不是包内桥接） |
| `runtime.manifests` | Nonoka 的模型清单、下载源路径 | 兼容包的 `model_manifest`、`model_fetch` |
| `download` | 线路：`region`、HF 端点、GitHub 代理前缀（由 Nonoka 的线路策略算好） | 兼容包的 `resolve_region` 与下载子进程的 `HF_ENDPOINT` |
| `network.proxy` | `http`、`https`、`no_proxy`（D17） | `engine_host` 写代理变量 |
| `device` | `cuda_visible_devices` | `engine_host` 写 `CUDA_VISIBLE_DEVICES` |
| `secrets` | 名 → 值，只含该包 `PACKAGE.json` 声明要的名字（纠错包：LLM 与检索密钥、`NONOKA_CLOUD_KEY`；转写包：`HF_TOKEN`） | 同名环境变量（D3） |
| `llm.routes` | `default`、`correction`、`research`、`knowledge` 各自的 `{provider, model}` | `[nonoka_models]`（D20）与 `[llm.preferred_targets]`（桥接在包内用 FineSub 的目录与路由表把 Gemini、本地 Agent 的模型映射到目标 id，算法同 `settings.py:1020-1049`） |
| `llm.endpoints` | 每个 HTTP 路由：`provider`、`kind`（openai_compat / anthropic）、`tier`、`base_url`、`key_name`、`model_id`、`display_name`、`max_input_tokens`、`max_output_tokens`、`thinking`、`token_scale`、`capability`（D15，核心按 `model_specs.py` 算好） | `model_catalog.toml` 的行，目标 id `nonoka-<provider>-<sha256(provider\0model)[:12]>` |
| `llm.gemini_base_url` | 用户改过才有 | 只含它的 `.env`（D3） |
| `agent_cli` | Agent CLI 目录列表（`settings.local_agent_path_entries` 的结果）；D16 的那几个变量的值 | `engine_host` 拼 `PATH`、带系统必需变量 |

### 1.14 vendor 的非核心文件谁在读（第 14 问）

`third_party/finesub/` 下除核心文件（`src/finesub/`、`LICENSE`、`LICENSES.json`、`PROMPT_LICENSE.md`、`UPSTREAM.json`、`QUALITY_LOCK.json`、`LOCAL_CHANGES.md`）外的文件：

| 文件 | 谁在读 | 删前要改谁 |
|---|---|---|
| `src/finesub_bootstrap/**` | 上锁引擎（1.1）；`src/nonoka_x/` 的 `settings.py`、`provision.py`、`runtime_install.py`、`model_install.py`、`download_route.py`、`asset_digest.py`、`config_file.py`；`desktop/scripts/resource-sources.mjs:14`（读其中的清单与锁）；`desktop/scripts/build-offline-package.mjs:165`（经 `engine` 路径找 `UPSTREAM.json`）；云端镜像整个复制 `third_party/finesub`（`nonoka-sub-cloud/modal_compute/nonoka_x_compute/images.py:159`、`:247`、`:264`） | WP-R1～R6、兼容包（WP-R7）、云端镜像（WP-R7 完成标准） |
| `pyproject.toml` | **引擎的检出根判定**（`finesub/paths.py:26`，见 1.8）；桌面暂存 `desktop/build/tools/stage/main.go:43`、`desktop/build/Taskfile.yml:95`；`tests/test_vendor_contract.py:71`；`scripts/sync_finesub.py:47`；安装器只把 vendor 当工作目录（`finesub_bootstrap/environment.py:786`、`:803`、`:813`），不读它 | 暂存清单、测试；删掉会改变 qwen 裁判编译缓存的位置（只影响缓存，见第 3 节） |
| `VERSION` | `pyproject.toml:251` 的动态版本；暂存 `stage/main.go:44`、`Taskfile.yml:96`；`tests/test_vendor_contract.py:74`；`scripts/sync_finesub.py:43`。引擎运行时不读（grep 无结果） | 暂存清单、测试 |
| `FILES.json` | `scripts/build_finesub_bundle.py:61`、`scripts/sync_finesub.py:405`、`tests/test_finesub_patch_stack.py:58` | WP-A |
| `UPSTREAM_README.md` | 只有 `scripts/sync_finesub.py:202` 写它 | WP-A |
| `README.md` | `pyproject.toml:15`（`readme`）、`scripts/sync_finesub.py:387-391` | 随 `pyproject.toml` |

## 2. 设计定稿

### 2.1 Nonoka 配置文件（D1）

- `<主数据目录>\settings.json`，UTF-8 JSON，顶层 `schema` 版本号，原子写（临时文件 + 替换）加旁边的 `.lock` 文件锁。
- 内容（字段名由 WP-R4 定，开工时有拿不准的先问用户）：模型路由（`default` 与三条任务路由的 `provider`/`model`）、各提供商的 Base URL（含 `GEMINI_BASE_URL`）、Nonoka Cloud 的 LLM 地址；以后的「当前转写引擎」（WP-UI）也放这里。
- 只放 Nonoka 认识的设置。不保存任何 FineSub 格式的内容（D11、D11c）。

### 2.2 密钥存储（D2）

- `<主数据目录>\secrets.json`：`{"schema": 1, "keyring": "<finesub$kek$v1$… 或 finesub$obf$v1$…>", "values": {"<名字>": "<原文，其中密钥部分换成 fs$… 密文>"}}`。信封算法逐字复制 `finesub_bootstrap/secrets.py`（注明来源），多 key 池的 `名:值,名:值` 写法保留在值里，按现在的 `iter_entries` 规则只加密密钥部分，这样设置页照样能逐项显示「已保护 / 明文 / 无法解密」与掩码。
- 保护强度与现在相同：Windows 绑当前账户，macOS 混淆并 `0600`。
- 只有 Nonoka 的 `config.secrets` 读写它；密钥经启动上下文按名交给包，包设成环境变量（D3）。

### 2.3 运行时组件划分（D12）

见 1.12。组件 = 清单条目；`asr-python` 一个；`ffmpeg` 含 ffprobe。macOS 只有媒体工具组件。

### 2.4 新数据布局（D4–D8、D14、D18、D21）

见 1.12、1.15。

### 2.5 桥接模块要派生的文件与环境变量

每个包的桥接在自己进程里、import `finesub` 之前，从启动上下文生成：

- `run\<进程>\finesub.toml`：`[nonoka_models]`（D20，与现在同格式同顺序）、`[llm.preferred_targets]`；不再写 `[llm]` 的空表以外的东西。写法与 `src/nonoka_x/config_file.py` 的输出逐字节相同，以通过闸门的文件比对。
- `run\<进程>\model_catalog.toml`：头注释 + 每个端点一行，逐字节同 `settings.py:965-1018`。
- `run\<进程>\.env`：仅当 `llm.gemini_base_url` 存在，内容只有 `GEMINI_BASE_URL=<值>`（D3）。
- 环境变量：1.7.3 表里「Nonoka 设定（包内）」各行。中性的部分由核心 `engine_host` 构建。
- 进程结束删 `run\<进程>\`；每次启动清掉不属于活进程的残留（D6）。

### 2.6 兼容包的接口

见 1.11。

### 2.7 迁移规则

在 WP-M1/WP-M2 里实现，规则：

1. 迁移前先出计划（`--dry-run`），确认：没有正在跑的任务；桌面端没有未完成的存储搬家；新名字的目标（`<旧根的父目录>\runtime`、`<主>\engines\…`、`settings.json`、`secrets.json`）不存在，存在就停下报告，不合并。
2. 大文件根：整体同盘改名为 `runtime`，再在内部把 `runtime\python` 改名为 `python`、`runtime\<工具>` 改名为 `tools\<工具>`；`models\`、`cache\` 原位；有 `runtime\python-builds\` 时搬到 `tools\python-builds\` 并改写 `python\pyvenv.cfg` 的 `home`（D23）。`storage.json` 有 `runtimeDir` 记录时改写成新路径，没有记录（默认位置）就不写。全程不下载，改完按 1.5 的规则判定就绪。
3. 设置与密钥：按 1.13 表格转换。认识的名字清单 = `src/nonoka_x/settings.py` 的 `KEY_SPECS`、`BASE_URL_SPECS`、`CLOUD_KEY_NAME`、`CLOUD_BASE_URL_NAME`、`[nonoka_models]` 的路由键（含要清掉的旧路由键）。其余丢弃，迁移报告只列表名、键名、变量名，不列值（D11c、D13）。旧文件改名留作备份。
4. 知识库、限流状态：同盘改名进 `engines\finesub-correct\`，内容一字不改。
5. 可重建的（`model_catalog.toml`、`locations.json`、store 标记、注册脚本）删除。
6. `%LOCALAPPDATA%\FineSub`、`bootstrap\`、`app-resources\` 不碰（D8、D14）。
7. 任务目录里的 FineSub 产物归进 `native\<包 id>\`：WP-M2 做，按 WP-P0 第 6 问的归属。
8. 迁移后派生出的 `finesub.toml` 与迁移前的语义相同，**只对 Nonoka 认识的键成立**；用户手写过 Nonoka 不认识的表（如 `[segmentation]`、`[pools]`）的，行为会回到默认值，迁移报告里写明（D11c）。

## 3. 阻碍、需要改上锁文件的地方、与计划不符之处

### 3.1 需要改上锁文件的地方

**没有。** 每一处外部影响都能在上锁文件之外封住：环境变量由分类表控制；`%LOCALAPPDATA%\FineSub` 与 `~\.cache\…` 回落由兼容包的 `paths`、`model_caches` 名字改向；检出判定靠包的目录布局；孙进程找包靠 D9 的 `PYTHONPATH`。

### 3.2 阻碍与风险

1. **检出根判定挡不住编译缓存和 tokcount 候选**（1.8）。vendor 目录与 `app-resources` 里的副本都被引擎当成「检出」，qwen 裁判编译缓存现在落在 vendor 下（`finesub/paths.py:253-255`）。WP-R7 删 vendor 的 `pyproject.toml`、WP-P1 的包不带 `pyproject.toml` 之后，缓存改落到 `runtime\cache\<包>\`：只是缓存位置变，第一次会重新编译，结果不变。**WP-P1 的包目录不许同时含 `pyproject.toml` 与 `src\finesub`**，否则全部检出模式的回落都会重新生效。
2. **venv 的基础解释器写死在 `pyvenv.cfg` 里**。本机指向系统的 `D:\Envir\Python312`（D10 定为边界）。若某台机器用的是 Nonoka 托管的基础 Python（`runtime\python-builds\`，`finesub_bootstrap/environment.py:1039`），`home` 是旧根里的绝对路径，大文件根一改名 venv 就失效。D23 定了迁移时搬到 `tools\python-builds\` 并改写 `home`。同样的问题在**以后用户搬存储位置时**还会出现：WP-R3/WP-R6 的「存储位置搬迁」也要在搬完后改写 `home`。本机没有这种情况，未实测。
3. **D3 让闸门的环境检查必然不过**：基线 `tests/fixtures/quality/environment.json` 记着 `FINESUB_ENV_FILE` 的内容（含门测用的假密钥）。用户已批准 WP-R5 时重录（D19），须单独提交并在交付说明里写明原因。
4. **`pipeline.py` 还 import 依赖面清单之外的名字**（`logs.open_log` 等，1.1）。WP-B 拆它时去掉，或兼容包补上。
5. **MCP 服务子进程里用户级 site-packages 仍然生效**（1.10 第 2 条），由 Agent CLI 拉起、只转交引擎给的那份环境，上锁文件不能改。WP-H 报告里列为边界。
6. **venv 搬家后 `Scripts\*.exe` 失效**（1.5）。引擎不用它们；WP-M1 实测确认。
7. **引擎的线路判定会联网探测**（1.4 第 5 条）。兼容包改为直接用上下文的线路后，这个网络行为消失；这是行为变化，但不影响结果。

### 3.3 与计划不符之处

1. 第 2.4 节把 `FINESUB_KEYRING` 列为引擎的环境变量入口——它是 `.env` 里一行的名字（`finesub_bootstrap/secrets.py:58`），不是环境变量。
2. 第 2.4 节说 `FINESUB_MODEL_MANIFEST`、`FINESUB_DOWNLOAD_SOURCES`、`FINESUB_HF_ENDPOINT` 是引擎留的入口：它们都在 `finesub_bootstrap` 里，换成兼容包后不再存在，兼容包直接读启动上下文。
3. 第 2.5 节最后一行说「没有显式变量时」才回落到 `%LOCALAPPDATA%\FineSub`：实际上下载线路状态、HF 确认的 `data_root`/`models_root`、日志、参考数据、capsule、会话台账**总是**用它，没有任何变量能改（1.1、1.8）。
4. 第 2.5 节说 `settings.bind_environment` 把 `FINESUB_CHECKOUT_DATA` 设成 `0`「挡住」了检出数据：挡住了配置、密钥、知识库、状态、日志、Agent，没挡住编译缓存、tokcount 候选（1.8）。
5. 第 2.5 节说 `CUDA_VISIBLE_DEVICES`「只在外部没设时才由 Nonoka 设」（`worker.py:593`）：请求指定 `cuda:N` 时 `local_provider.py:1561-1565` 在启动 worker 前已经无条件设了，`worker.py:593` 是第二道条件设置。
6. 第 2.6 节的大文件根清单含 `cache/`、`models/torch`：本机都不存在（`UV_CACHE_DIR` 指向那里但没写过）；`agent-capsules` 不在大文件根而在 `%LOCALAPPDATA%\FineSub`。
7. **第 4 节第 18 条**「`finesub.env`、`finesub.toml` 里 Nonoka 不认识的键原样透传」、「迁移后派生出的 `finesub.toml` 必须与迁移前的语义相同」，以及 **WP-R4、WP-M2 完成标准**里「含 Nonoka 不认识的键……都读得回来」：被 D11、D11c、D13 改为丢弃并列名，语义相同只对认识的键成立。计划需要据此修改（交回）。
8. 第 4 节第 32 条「包如果必须落盘才能让引擎读到」：查实不必落盘（1.2），D3 定为只走环境变量。
9. 第 4 节第 17 条「主数据目录只放 Nonoka 自己的东西」与第 33 条「除包的私有目录外」：按第 27–37 条为准，D4 把包的私有目录放在主数据目录的 `engines\` 下。
10. 计划假设 worker 隔离启动只是加 `-I`：还要处理孙进程的 `PYTHONPATH`（D9）和 worker 自己的标准输出编码（1.10 第 3 条）。

## 4. 给后续工作包的要点

- **WP-R1**：组件 id 按 D12；清单路径以后由启动上下文给兼容包。
- **WP-R2**：目录布局按 1.12；`download_route.py:366` 不再读 `FINESUB_GITHUB_FILE_PROXY`。
- **WP-R3**：就绪判定按 1.5，运行时记录里登记基础解释器（D10）；新根名 `runtime`，Go 侧 `storage/locations.go:30` 一并改（WP-R6）。
- **WP-R4**：`settings.json`、`secrets.json` 按 2.1、2.2；迁移按 2.7 第 3 条；完成标准里的「Nonoka 不认识的键」按 D11c 改。
- **WP-R5**：派生按 2.5；重录环境基线（D19）单独提交。
- **WP-R7**：兼容包按 1.11，含可执行的 `finesub_bootstrap.model_ensure` 模块入口。
- **WP-M1**：迁移按 2.7；`python-builds` 与 `pyvenv.cfg` 按 D23。
- **WP-R3/WP-R6**：存储位置搬迁搬完后改写 `pyvenv.cfg` 的 `home`（3.2 第 2 条）。
- **WP-P0/P1**：包目录不许是「检出」形状（3.2 第 1 条）；1.11 末尾的名字归属待核实。
- **WP-H**：分类表按 1.7.3；1.7.2 未逐项核实的库变量由读取审计补全；边界列出 D10、D16、3.2 第 5 条。
