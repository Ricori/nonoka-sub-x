# 云端媒体库切换到 Nonoka 格式：操作手册（WP-L）

计划 4.8 第 38 条。媒体库条目从 FineSub 的四个产物（`stable_json`、`raw_srt`、`annotated_csv`、`final_srt`）换成 Nonoka 的格式（`transcript_json`、`correction_json`、`raw_srt`、`final_srt`，`docs/provider-spec.md` 7.5）。**不留兼容期**：新 Worker 只认新名字，新桌面只读新名字，所以线上数据必须一次转完，切换顺序固定。

本手册由用户在发版时执行，或由用户明确批准后代为执行。WP-L 本身没有动线上数据（只在用户批准的只读副本上跑过转换，见 WP-L 交付说明）。

## 前提

- 新桌面版（含 WP-L、WP-M2）已构建好、未发布；发布清单准备好 `minVersion` = 这个版本号（`desktop/internal/selfupdate/service.go` 的 `releaseMandatory` 读它）。
- 云端仓库含 WP-L 的分支已合并，但**还没部署**。
- 本机：已登录 `wrangler`（`npx wrangler whoami` 能看到账户）；一个装着两个 FineSub 包的 nonoka-sub-x 检出（与要发的版本同一提交），`NONOKA_X_CHECKOUT` 指向它；一个能跑这两个包的 Python（`D:\Envir\Python312\python` 即可：转换只用它们的纯 Python 部分，不要 GPU、模型和运行环境层）。
- 下面命令都在云端仓库的 `modal_compute\` 目录里跑，脚本是 `python -m nonoka_x_compute.operations.library_convert`（下称「脚本」）。脚本只在本机跑，不在 Modal 上跑。

## 步骤

每一步都写了校验与回退。任何一步校验不过：按该步的回退做，回到旧 Worker、旧数据，停下来查原因。

### 1. 停写：旧 Worker 进入只读

只读开关（变量 `LIBRARY_READ_ONLY=1`：同步与删除回 503「云端媒体库正在升级，暂时只能查看，请稍后再同步」，读照常）是 WP-L 加的，现在线上的旧 Worker 没有。转换期间旧 Worker 还在跑，它的写入口这样关：

- 在 Cloudflare 控制台给 `nonoka-cloud` 加一条临时 WAF 自定义规则，拦截 `POST /v1/library/sync` 与 `DELETE /v1/library/*`（回 503），转换完成、第 5 步之前一直留着；
- 或者，若用户愿意：把只读开关那一个提交（云端仓库 `cc/wp-l-library-format` 上的第二个提交）单独摘到旧代码上先部署一次，设 `LIBRARY_READ_ONLY=1`。

新 Worker 部署时同样先带着 `LIBRARY_READ_ONLY=1`（第 3 步），到第 5 步才解除。

校验：用一个测试 Key 同步一次，得到 503；`GET /v1/library` 照常。
回退：删掉临时规则。

### 2. 批量转换并校验

1. 摸底（只读，不读任何对象）：`脚本 survey`。记下 `rows`、`bytes`、各种产物名组合的条数。
2. 备份副本（只读）：`脚本 copy --to <本机目录>`。超过 2 GB 或 2000 条自动停下，先确认再加 `--max-bytes`、`--max-rows`。这是出问题时的最后一道保险，切换完成并确认无误之前不要删。
3. 在副本上演练：`脚本 convert --store <副本目录> --report <报告>`。要求退出码 0：`counts` 里没有 `mismatch`、`failed`，`verify_problems` 为空，`rows_before == rows_after`。`unopenable` 是旧版桌面本来就打不开的条目（没有 `stable_json`，或旧投影就失败），转换不动它们，逐条看报告决定留不留。再跑一遍，`counts` 全是 `already`（外加同样的 `unopenable`），副本目录不变。
4. 线上预演：`脚本 convert --store live --dry-run --report <报告>`，结果应与副本演练相同（`would-convert` 的条数等于副本的 `converted`）。
5. 线上转换：`脚本 convert --store live --report <报告>`。每个条目：旧对象先复制到 `library-backup/wp-l/<owner>/<id>/` 并读回核对，再放新对象、改 D1 行、最后把 `stable_json`、`annotated_csv` 从 `library/` 下拿走；两份字幕的键不变。脚本在写之前逐条比对「旧桌面怎么投影旧文件」与「新投影怎么读 `correction.json`」，不同就不写这一条（报 `mismatch`）。

校验：退出码 0；报告的 `verify_problems` 为空（每个转过的条目都从存储里重新读回、按记录的 sha256 核对、能投影，`library/` 下不再有旧对象）；再跑一次全是 `already`；`survey` 的 `rows` 与转换前相同。另外抽几条用新桌面（本机构建、指向线上）取回打开，与转换前在旧桌面上打开的样子对照。
回退：转换中途停了，直接再跑一遍（转过的跳过，停在一半的会补完）。要整体退回旧数据：对每个条目把 `library-backup/wp-l/<owner>/<id>/` 下的对象放回 `library/<owner>/<id>/`、删掉 `transcript_json.transcript.json` 与 `correction_json.correction.json`、把 D1 行的 `artifact_names` / `artifact_meta` 改回旧值（转换报告的 `before` 与副本里的 `library.json` 有原值）。这时旧 Worker 照常能读。

### 3. 部署新 Worker

先在 `wrangler.jsonc` 的 `vars` 里加 `"LIBRARY_READ_ONLY": "1"`，`npm run deploy`。

校验：`npm test` 通过之后才部署；部署后用测试 Key：旧格式的同步（只带 `stable_json` 等）回 426 与升级提示；`GET /v1/library` 与取回一个转换过的条目正常；同步回 503（只读）。
回退：`wrangler rollback` 回旧版本（旧数据还在备份前缀下，按第 2 步的回退恢复）。

### 4. 发布新桌面版并设 `minVersion`

发布清单设 `minVersion` 为新版本号，旧桌面启动时被强制升级。

校验：一台装旧版的机器启动后看到强制升级；升级后能取回云端条目。
回退：撤下这次发布（发布清单回到上一版），然后按第 3 步回退 Worker、第 2 步回退数据。

### 5. 解除只读

从 `vars` 里删掉 `LIBRARY_READ_ONLY`，再部署一次（若第 1 步用了临时 WAF 规则，一并删掉）。

校验：新桌面完成一个本地任务后自动同步成功，条目里是新名字；`library-backup/` 下的对象数等于转换报告里 `converted` 条目的旧对象数之和。

### 6. 收尾

- 确认无误一段时间后（由用户定），再删 `library-backup/wp-l/` 前缀与第 2 步的本机副本。**这是永久删除用户数据，删之前问用户。**
- 任务结果（`jobs/…`）不在本手册里：它们最多存 48 小时，格式由 WP-CL 换（计划 4.7 第 36 条）；WP-CL 往同一个脚本里加任务结果那一步之前，切换窗口里等未取的作业取完或过期。
