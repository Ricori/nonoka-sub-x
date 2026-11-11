# FineSub vendor directory

此目录是 Nonoka X 使用的 FineSub 引擎，基于 `UPSTREAM.json` 记录的上游版本。

规则（详见 [FineSub 引擎集成、质量锁与上游对齐](../../docs/engine.md)）：

- `src/` 下除 `finesub/stages.py`、`finesub/pipeline.py`、`finesub/speech/joint/`、`finesub_bootstrap/` 四处外，全部文件上锁：哈希记在 `QUALITY_LOCK.json`，由 `tests/test_finesub_quality_lock.py` 检查。改上锁文件、升级上游、更新 `QUALITY_LOCK.json` 都需要用户单独批准。
- 解锁的四处可以改，但 FineSub 路径的结果必须与改之前一致，由 `scripts/quality_gate.py` 证明；闸门不过就停下来报告。
- 相对上游改过的地方都记在 `LOCAL_CHANGES.md`，新的改动也记在那里。
- 升级上游前用 `python scripts/finesub_align.py diff --ref <tag>` 查看关键算法路径上的差异，它只出报告。
- 不要把机器上安装的 `site-packages/finesub` 复制到这里。
- 本地 runtime 和云端镜像必须使用同一 `engine_bundle_id`。

上游原始说明保存在 `UPSTREAM_README.md`。
