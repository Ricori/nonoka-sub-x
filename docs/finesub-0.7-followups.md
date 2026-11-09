# FineSub 0.7.0 后续接入计划

前提：FineSub 0.7.0 升级已合入（见 [engine.md](engine.md)）。建议顺序是 B → C，由小到大。

## B. 识别后补救移到云端 GPU 腿（中，需实测）

**现状：** 云端 Whisper 在 GPU 容器（2 CPU / 3 GB 内存），Qwen 在 CPU 调度容器预热，`stable` 在 CPU 腿跑。补救要同时用 Whisper 和 Qwen，上游的钩子不可拆。

**方案：** 在 GPU 腿写完 `aligned` 后，就地跑 Qwen 校验和 `stable`（带补救）；CPU 腿靠 `_use_or_create` 复用 `-stable.json`，只负责收尾和发布。这样不用新增引擎补丁。

**改动：**
- `phases.py`：GPU 腿增加 Qwen + stable；CPU 腿不再做这两步，去掉本轮关闭补救的参数。
- `images.py`：GPU 镜像加 `pykakasi==2.3.0`、`pypinyin==0.55.0`，确认 Qwen 所需的 `transformers` 版本。
- `gpu.py`：按实测调高内存（3 GB 大概率不够 Qwen）。

**先测再定：** 在 L4 和 T4 上各跑日语长、短样本，记录峰值显存/内存和 GPU 秒数。T4 显存放不下 Whisper + Qwen 时，要么只在 L4 开，要么放弃。

**待定：** 成本上升多少可以接受（上游称总耗时约 +15–20%，云端要按 GPU 秒重新算）。

**验收：** 同一素材云端与本地的 `-stable.json` 补救结果基本一致；失败时退回不补救，不让任务失败。

## C. 一体转写 `--asr`（大，建议分期）

**是什么：** 一个模型直接转写原始混音，一次给出带时间和说话人的结果，跳过人声分离、Whisper、Qwen、声学分离。默认关。

**建议分期：**
- 一期：`moss-local`（不上传，需 RTX 30 系列以上、8 GB 显存）和 `moss-pro`（每 key 每天约 1.25 小时免费）。
- 二期：`gemini-35t`（复用 Gemini 免费 key，大陆要代理）和 `mai2`（付费）。
- 只接本地引擎；Nonoka Cloud 云端转写不接。

**改动：**
- 请求与后端：`local_provider` 校验新字段；`worker.py` 传 `asr`；中断续跑时以任务记录的后端为准。
- UI（`TranscriptionDialog.tsx`）：增加「转写引擎」选择；选一体转写时隐藏人声分离和说话人分离（`_backend_diarize` 会强制标说话人）；首次上传前明确告知音频去向；`moss-local` 按 `gpu_tier` 预检，不满足的显卡不可选。
- Key 与费用：在设置页增加 `MOSS_PRO` 等 key，沿用 `finesub.env`；建议写 `[asr] moss_pro_paid = false`，避免免费额度用完后扣预付余额。
- 模型与运行环境：`model_install.py` 支持 `moss-transcribe-diarize`（约 1.7 GB）；运行环境依赖已在 0.7.0 的 lock 里；发布时更新 GitHub 兜底副本和离线包。
- 产物：`worker.py` 把 `<名字>-cloud/` 缓存纳入产物管理，清理时不删（重跑不再上传、不再花额度）。
- 编辑器：MOSS 没有词级时间戳，`words` 为空时逐字功能要降级；MOSS 和 `gemini-35t` 允许两人字幕时间重叠（`同时`/`换回`），`projector` 和时间轴要支持重叠的行。这是最大的风险点，建议先单独验证。
- 代理：一体转写走 `[api] proxy`，沿用升级中改好的代理策略，SOCKS 不可用要有提示。

**待定：** 一期范围是否如上；重叠字幕是在编辑器完整支持，还是先按轨道拆开显示。

**验收：** 每个后端完成一次全流程（转写 → 说话人 → 纠错 → 编辑 → 导出）；额度耗尽、key 无效、显卡不符时给出明确报错；续跑不重复上传。
