# Nonoka X Compute（Modal 计算端）

`modal_compute/` 是云端后端拆分后留在 Modal 上的部分，app 名 `nonoka-x-compute`。它只负责计算：

- CPU 调度器 `orchestrate_task`，以及跑在它进程里的 prepare、aligned-prepare、cpu 三段；
- 两个 L4 GPU 段：`VocalSeparator`（人声分离）和 `run_aligned_stage`（Whisper，按需加说话人时间线）；
- 只给 Cloudflare Worker 调用的控制端点 `control`；
- 每天跑一次的 scratch 清扫 `sweep_scratch`。

对外接口、鉴权、计费、作业状态与事件、媒体库、上传槽位都在 Cloudflare Worker（`nonoka-cloud-cf`，契约见 `nonoka-cloud-cf/docs/cloud-api.md`）。Modal 不保存任何用户数据，也不持有 R2 凭据：源音频经 Worker 的内部接口流式下载，结果放在 `/complete` 的请求体里交回 Worker，由 Worker 写入 R2。

旧 app `nonoka-x-cloud`（`modal_backend/`）在切换前继续服役，两边可以并行部署。

```
桌面端 ──► CF Worker ──POST /spawn /cancel /probe /purge──► control（本 app）
                ▲                                              │ spawn
                │                                              ▼
                └──── /internal/jobs/{id}/* ◄──── orchestrate_task ─► VocalSeparator（L4）
                       （JobChannel）                       └────────► run_aligned_stage（L4）
```

## 代码结构

```
modal_compute/
  pyproject.toml  uv.lock
  ct2_patches/            CTranslate2 补丁（从 modal_backend 拷贝，13 个）
  nonoka_x_compute/
    app.py                部署入口，注册下面所有函数
    resources.py          app、Volume、Secret、WORKER_GPU
    images.py             scheduler / gpu_worker / vocal_accel / control 四个镜像
    channel.py            JobChannel：调 Worker 内部接口的 HTTP 客户端
    control.py            控制端点（ASGI，Bearer CONTROL_TOKEN）
    scheduler.py          orchestrate_task：五段编排
    phases.py             prepare / aligned-prepare / gpu / cpu 四段的执行体
    vocal.py              VocalSeparator
    gpu.py                run_aligned_stage
    vocal_policy.py       分离采样率三档（tier_for_request）
    vocal_accel.py        分离器 AOTI 加速包
    gpu_metrics.py        NVML 显存采样
    pipeline_rules.py     source_duration_fault、产物名与后缀
    sweep.py              scratch 安全网
    operations/prime.py   预热 AOTI 包与说话人分离模型（临时 app）
    migrate/export_legacy.py  旧数据导出（一次性，临时 app）
  tests/
```

## 资源与 Secret

| 资源 | 名字 | 用途 |
| :--- | :--- | :--- |
| Volume | `nonoka-x-scratch`（新建，挂在 `/data`） | 断点目录 `tasks/<job_id>/`：源音频、解码后的 FLAC、人声轨、VAD、aligned JSON 等 |
| Volume | `nonoka-x-models`（沿用旧的） | HF 缓存、分离器 AOTI 包、说话人分离权重。沿用同名 Volume，不用重新预热 |
| Secret | `huggingface`（沿用） | `HF_TOKEN` |
| Secret | `nonoka-compute-control`（新建） | `CONTROL_TOKEN`、`CF_CALLBACK_URL`、`WORKER_CALLBACK_TOKEN` |

本 app 不挂 `r2`，也不使用任何 Modal Dict。

创建新 Secret：

```bash
modal secret create nonoka-compute-control \
  CONTROL_TOKEN=<与 Worker 的 MODAL_CONTROL_TOKEN 相同> \
  CF_CALLBACK_URL=https://cloud.nonoka.online \
  WORKER_CALLBACK_TOKEN=<与 Worker 的 WORKER_CALLBACK_TOKEN 相同>
```

staging 用 Modal 的独立环境（`--env staging`）建同名 Secret，`CF_CALLBACK_URL` 指向 `https://cloud-staging.nonoka.online`。

## 部署

> **必须在 `modal_compute/` 目录下执行。** 镜像用 `add_local_dir("ct2_patches", ...)`、`add_local_dir("../third_party/finesub", ...)`、`add_local_dir("../src", ...)` 挂载源码，相对路径按 `modal deploy` 的工作目录解析，换个目录会静默挂错内容。`modal_compute/` 和旧的 `modal_backend/` 是同级目录，所以这些路径和旧 app 完全一样。

```bash
cd modal_compute
uv sync --extra test        # 本地依赖（modal、fastapi，测试再加 pytest、httpx）

# 部署（staging 加 --env staging）
uv run modal deploy -m nonoka_x_compute.app

# 预热 AOTI 包：单容器；换卡或升级 torch/镜像后用 prime_fleet 覆盖整个机群
uv run modal run -m nonoka_x_compute.operations.prime::prime_finesub_gpu
uv run modal run -m nonoka_x_compute.operations.prime::prime_fleet

# 预下载说话人分离模型（约 450 MB，不占 GPU）
uv run modal run -m nonoka_x_compute.operations.prime::prime_diarization_models
```

`nonoka-x-models` 是沿用的，已经铺好的包和权重可以直接用，正常情况下预热不需要再跑。

部署后把控制端点的地址（`https://<workspace>--nonoka-x-compute-control.modal.run`，以 `modal deploy` 的输出为准）配置为 Worker 的 `MODAL_CONTROL_URL`。

镜像的每一步构建都和旧 `images.py` 逐字相同（`test_the_images_are_the_old_ones_step_for_step` 守着这一点）。Modal 按构建步骤缓存镜像层，定义相同就能复用旧 app 已经构建好的层。Vocal 镜像尤其重要：它的 audio-separator 依赖没有锁版本，现在能用全靠缓存，重新求解会失败。所以 boto3 虽然已经不用了，也留在构建步骤里。

### 部署的函数

| 函数 | 镜像 | 资源 | 触发 |
| :--- | :--- | :--- | :--- |
| `control` | `control_image` | 0.25 CPU / 256 MiB，单容器 20 并发，`scaledown_window=300` | HTTP（Worker 调用） |
| `orchestrate_task` | `scheduler_image` | 2 CPU / 3.5 GiB，超时 3h，`max_containers=10` | `control` 的 `/spawn` |
| `VocalSeparator` | `vocal_accel_image` | L4 / 3 GiB，超时 30min | 调度器 spawn |
| `run_aligned_stage` | `gpu_worker_image` | L4 / 3 GiB，超时 3h | 调度器 spawn |
| `sweep_scratch` | `control_image` | CPU，超时 15min | 每天一次 |

所有函数都**不带** Modal `retries`。恢复由用户决定：失败或取消的任务用 `/retry`，中断的用 `/resume`，两者都从 scratch Volume 上已提交的断点续跑。自动重试分不清引擎的确定性错误和抢占，只会租着 GPU 重复失败。

## 计算流程

调度器按顺序执行五段：

1. **prepare**（调度器进程）：经 `GET /internal/jobs/{id}/source` 下载源音频到 `tasks/<id>/source.m4a`（先写 `.part` 再改名），用 ffprobe 复核真实时长（超出申报时长的 1.1 倍加 60 秒，或超过 2 小时，就以 `source_rejected` 拒绝），再用 ffmpeg 解码成 `source-vocal-decoded.flac`（只换容器，不改采样率和位深）。
2. **Vocal**（L4）：分块分离（`nonoka_x.vocal_blocks` 规划，`demix.separate_waveform` 执行），输入够长时装 AOTI 加速包，用 FineSub 自己的编码器输出 16 kHz 单声道 `subtitle-vocal.ogg`。
3. **aligned-prepare**（调度器进程）：VAD。开启 Silero 辅助时留给 GPU 段在 CUDA 上算。
4. **aligned**（L4）：打过补丁的 CTranslate2 Whisper，flash attention 打开。请求带 `diarization` 时，接着在 GPU 上算说话人时间线 `subtitle-diar.json`。这一段运行期间，调度器在 CPU 上预载 Qwen 裁判，GPU 段结束后最多再等 120 秒。
5. **cpu**（调度器进程）：Qwen 校验收尾、stable、写说话人标签，然后 `POST /complete` 交回 `stable_json`。请求要求了说话人分离且 `subtitle-diar.json` 存在时，同时交回 `diar_json`。交回成功后删除 `tasks/<id>/`。

断点跳过规则：

- `subtitle-vocal.ogg` 已存在：调度器不租 Vocal 卡，Vocal 段自己也会检查；
- `subtitle-aligned.json` 已存在：调度器不租 Whisper 卡，也不预载 Qwen；
- `source.m4a`、`source-vocal-decoded.flac`、VAD 前缀已存在且匹配：各自复用。

失败与中断：

- 引擎错误发布 `engine_failed`，源音频不符发布 `source_rejected`，Vocal 段发布 `vocal_failed`，调度器兜底发布 `pipeline_failed`。Worker 不会覆盖已经发布的失败，所以先发布的代码生效；
- `KeyboardInterrupt` / `SystemExit`（容器被抢占或停止）不发布任何失败，由 Worker 的探活发现调用已结束，再把作业标为 `interrupted`，桌面端据此提供"继续"；
- 运行已被取消时抛出的错误一律当作取消处理，不发布失败。

## Worker 内部接口（`channel.py`）

Base 为 `CF_CALLBACK_URL`，每个请求带 `Authorization: Bearer <WORKER_CALLBACK_TOKEN>`，JSON 格式。Worker 侧实现在 `nonoka-cloud-cf/src/internal/routes.ts`。每个请求都带 `run_id`，Worker 在一条语句里检查围栏：run 必须是作业当前的 run、状态是 queued 或 running、没有被取消。不满足时返回 `active:false`，`JobChannel` 据此抛出 `TaskCancelled`，当前段随即停下。

| 方法 | 路径 | 说明 |
| :--- | :--- | :--- |
| GET | `/internal/jobs/{id}/run?run_id=` | `{active, call_id, owner_id, request}`；404 表示作业已不存在，当作不活跃 |
| GET | `/internal/jobs/{id}/source?run_id=` | 流式返回源音频（`audio/mp4`）；409 表示 run 不活跃，404 表示音频不存在 |
| POST | `/internal/jobs/{id}/events` | `{run_id, events:[{type, payload}], stage?, progress?}` → `{active}`；type 只能是 `stage`、`progress`、`warning`、`handoff`。第一次调用会把作业从 queued 改为 running |
| POST | `/internal/jobs/{id}/child` | `{run_id, child_call_id}` → `{active}`；记录 GPU 段的 call，取消时一并取消，段结束后清空 |
| POST | `/internal/jobs/{id}/complete` | `{run_id, artifacts:{stable_json, diar_json?}}` → `{active, state}` |
| POST | `/internal/jobs/{id}/fail` | `{run_id, code, message, stage?}` → `{active:false}` |

行为约定：

- **重试**：5xx、429 和网络错误按指数退避重试（0.5 s 起翻倍，单次最多 8 s，共 8 次，约半分钟）；401 立即抛 `ChannelUnauthorized`，不重试；其他 4xx 抛 `ChannelError`。下载源音频时遇到网络中断或长度不符，从头重下。
- **callId 握手**：Worker 要等 `/spawn` 返回后才知道调用 id。每一段开始前每 0.1 秒轮询一次 `/run`，等 `call_id` 出现再发布事件；15 秒还没等到，就以 `dispatch_incomplete` 调用 `/fail` 并退出。
- **事件**：`log` 类型不发给 Worker，只打到容器 stdout。`srt-line-budget`、`unverified-accelerator`、`resource-budget` 这几类告警也只留在 stdout。
- **进度**：值没变就不发，发送频率不超过每秒 1 次。新阶段后的第一条和 `completed == total` 的最后一条总会发出。
- **取消检测**：每次发布事件都会顺带确认 run 是否活跃；只打日志、没有发布的那段时间里，最多每 5 秒问一次 `/run`。
- `fail` 是尽力而为，永远不抛异常：它总在失败路径上调用，那里要往外抛的是原来的错误。

## 控制端点（`control.py`）

Worker 用 `Authorization: Bearer <CONTROL_TOKEN>` 调用（客户端在 `nonoka-cloud-cf/src/modal.ts`）。没有配置 `CONTROL_TOKEN` 时一律返回 401。

| 方法 | 路径 | 说明 |
| :--- | :--- | :--- |
| GET | `/` | `{ok:true}`，不需要鉴权 |
| POST | `/spawn` | `{owner_id, job_id, run_id, request}` → `{call_id}`，即 `orchestrate_task.spawn(...)` |
| POST | `/cancel` | `{call_ids:[...]}` → `{cancelled:[...], errors:{id: msg}}` |
| POST | `/probe` | `{call_id}` → `{state: running\|finished\|failed\|unknown, error?}`。用 `FunctionCall.get(timeout=0)` 判断：超时为 running，正常返回为 finished，抛出异常（包括 Modal 不认识的 id）为 failed，连不上 Modal 控制面为 unknown |
| POST | `/purge` | `{job_id}` → `{removed}`，删除 scratch 上的 `tasks/<job_id>/` |

`job_id` 必须匹配 `job_[0-9a-f]{24}`，`call_id` 必须是 `fc-…`，所以 `/purge` 不可能删到 `tasks/` 以外的东西。

## scratch 清扫（`sweep.py`）

断点目录正常情况下会随作业一起消失：cpu 段交回成功后自己删除，作业被桌面端删除或被 Worker 清扫时，Worker 调 `/purge`。`sweep_scratch` 是安全网，每天删除 `tasks/*` 中 7 天内没有任何修改的目录（取目录树里最新的 mtime）。7 天远长于 Worker 48 小时的作业 TTL，不会删掉 retry/resume 还可能用到的断点。

## 从旧 app 迁移数据（`migrate/export_legacy.py`）

一次性脚本，是本工程里唯一碰旧资源的代码，对旧资源只读：Dict `nonoka-x-keys`、`nonoka-x-videos`，Volume `nonoka-x-artifacts`（只读挂载），以及 `r2` Secret 的 `R2_BUCKET` 指向的旧桶（只用来读封面）。所有写入都进新业务专用的桶 `nonoka-x-cloud`，可以用 `--target-bucket` 或环境变量 `TARGET_R2_BUCKET` 改。目标桶和旧桶同名时脚本直接报错。

**`r2` Secret 里的 token 必须能读旧桶、写新桶。** 两个桶在同一个 Cloudflare 账号下，签一把同时授权这两个桶的 token 即可。

```bash
cd modal_compute
uv run modal run -m nonoka_x_compute.migrate.export_legacy --dry-run          # 只导出记录、统计封面，不往 R2 写任何东西
uv run modal run -m nonoka_x_compute.migrate.export_legacy --out legacy-dump.json
uv run modal run -m nonoka_x_compute.migrate.export_legacy --target-bucket nonoka-x-cloud-staging --out staging-dump.json
```

做四件事：

1. 合并 Dict 和 `records/{keys,videos}/*.json` 信封里的全部 Key 与库记录。两边都有时以 Dict 为准，Dict 已过期丢失的由信封补上。
2. 把 Volume 上每个库条目的 `library/<owner>/<vid>/<name><suffix>`（后缀 `.stable.json`、`.raw.srt`、`.annotated.csv`、`.srt`）上传到新桶的同名键。owner 取记录上的 `ownerId`，没有就取 `video_ids` 里列着它的那个 Key，都没有就是 `admin`。这和 `import-legacy.ts` 写 D1 时的规则一致。
3. 封面：在旧桶里依次找 `nonoka-thumbs/<objectId>.jpg`、`nonoka-thumbs/<vid>.jpg`、`uploads/<objectId>.jpg`，把第一个找到且不超过 5 MiB 的复制到新桶的 `nonoka-thumbs/<vid>.jpg`，并在导出的记录上设 `thumbnailAvailable: true`。找不到就保留原值。导入脚本会丢掉 `objectId`，所以封面必须放在 `vid` 键下。dry run 只统计，不改这个标记。
4. 在本地写出 `{keys:[...], videos:[...]}`，作为 `nonoka-cloud-cf/scripts/import-legacy.ts` 的输入。

不复制任何 `uploads/*.m4a` 音频，运行中的作业不迁移。

可以重复执行：复制会覆盖，导入是 `INSERT OR REPLACE`。**`legacy-dump.json` 里有明文 Key**（已加入 `.gitignore`），导入 D1 后请删除。

注意：`nonoka-cloud-cf/docs/cloud-api.md` 写的是 `modal run modal_compute/migrate/export_legacy.py`，实际入口以本节为准（模块在包内，需要在 `modal_compute/` 下用 `-m` 运行）。

## 测试

```bash
cd modal_compute
uv run pytest
```

覆盖：

- `test_channel.py`：用假 HTTP 服务器测 `JobChannel` 的重试与退避、401、`active:false` → `TaskCancelled`、callId 握手与超时、进度去重与节流、源音频下载；
- `test_control.py`：控制端点的鉴权和四个操作，Modal 用假对象替换；
- `test_scheduler.py`：五段的执行顺序、两个断点跳过规则、取消和失败的处理、中断不发布失败；
- `test_phases.py`：各段向 Worker 发布的内容、`source_rejected`、产物（`diar_json` 只在要求时交付）；
- `test_source_contract.py`：从旧 `test_modal_task_runtime.py` 移植过来的、仍然有意义的源码断言；
- `test_vocal_policy.py`、`test_gpu_metrics.py`：从旧工程原样移植；
- `test_sweep_and_export.py`：清扫计划，导出的合并、owner 规则和封面查找顺序。

旧工程 `testing/` 下的基准与校验脚本（`verify_container`、`gpu_class_benchmark` 等）没有迁移，需要时再移植。
