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
- 云端识别作业的结果见下一节，与媒体库在同一个发版里切换。

## 云端识别作业的结果（WP-CL）

计划 4.7 第 36 条。作业结果从 FineSub 的产物（`stable_json`、可选 `diar_json`）换成与本机转写引擎同形的结果：`transcript_json` 加不透明附件 `finesub.stable_json`、`finesub.diar_json`。**不留兼容期**：新 Worker 只收、只发新名字（旧名字的下载回 426，旧名字的 `/complete` 回 422），新桌面只读新名字、提交前检查 `artifact_schema == 2`，新 Modal 应用只交新名字。所以 Worker、Modal 应用、桌面三者要在同一个切换窗口里换，换之前不能有作业正在跑。

脚本是 `python -m nonoka_x_compute.operations.job_convert`（下称「作业脚本」，同样在云端仓库的 `modal_compute\` 里、本机跑，只需要 nonoka-sub-x 检出里的转写包）。作业最多存 48 小时，线上通常只有几条。

### J1. 停止接新作业，等在跑的作业结束

和媒体库第 1 步同一个窗口：临时 WAF 规则再拦 `POST /v1/tasks`、`POST /v1/tasks/*/retry`、`POST /v1/tasks/*/resume`（回 503）。等 `GET /v1/admin/jobs` 里没有 `queued` / `running` 的作业（或取消它们）。

校验：`作业脚本 survey` 的 `by_artifact_names` 只剩 `diar_json,stable_json`、`stable_json`、`(none)` 这几种。
回退：删掉临时规则。

### J2. 转换作业结果

1. 副本（只读）：`作业脚本 copy --to <本机目录>`；在副本上 `作业脚本 convert --store <副本目录> --report <报告>`，退出码 0，再跑一遍全是 `already` / `no-result`。
2. 线上：`作业脚本 convert --store live --dry-run --report <报告>`，`would-convert` 的条数等于副本的 `converted`；然后去掉 `--dry-run` 正式跑。每条作业：用转写包的 `transcript_json` 操作从存着的 stable 出 `transcript.json`（语言取作业请求；旧作业没有 `-vad.json`，`duration` 取最后一行的结束时间），放到同一个 `jobs/<owner>/<job>/<run>/` 下并读回核对，再按 `rev` 比较并写回 D1 行：`stable_json`→`finesub.stable_json`、`diar_json`→`finesub.diar_json`，**R2 原对象原地不动**，只改登记名，所以不需要备份前缀。

校验：退出码 0；报告的 `verify_problems` 为空（每条都从存储读回、按记录的 sha256 与字节数核对、`transcript.json` 能被核心读入且记着附件 stable 的 sha256）；没有 `raced`（有就再跑一遍）。
回退：`作业脚本 revert --store live --report <报告>`：登记名改回旧名，`transcript_json.transcript.json` 对象留着但不再登记（下次清扫随作业删）。

### J3. 部署新 Modal 应用与新 Worker

与媒体库第 3 步一起部署 Worker；Modal 应用按 `modal_compute/docs/compute.md` 部署（镜像只改了 `PYTHONPATH` 那一步 `.env`，其余层复用）。

校验：用测试 Key 提交一条短音频：`/v1/tasks/<job>/artifacts` 列出 `transcript_json` 与 `finesub.stable_json`；`/artifacts/stable_json` 回 426。
回退：`wrangler rollback` 回旧 Worker、Modal 回旧应用版本，再按 J2 的回退把作业改回旧名。

### J4. 发布新桌面版

与媒体库第 4 步是同一个发布（`minVersion` 强制升级）。新桌面上，旧版本已取回但还没在本机续跑的云端任务（中继目录里只有旧的 `stable.json`）会以 `cloud_result_outdated` 失败并退回云端段，重试会重新上传识别，票据保证不再扣次；已经建好本机续跑任务的，按本机任务自己的断点继续。

### J5. 恢复接新作业

删掉 J1 的临时规则。

校验：新桌面提交一条云端任务，走到 `final-srt`。

