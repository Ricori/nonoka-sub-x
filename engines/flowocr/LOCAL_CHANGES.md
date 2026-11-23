# FlowOCR 本地改动

`src/flowocr/` 逐字节等于 `UPSTREAM.json` 记录的上游版本（`v1.0.0`，`fc813fc`），由 `QUALITY_LOCK.json` 看住：不许改、不许增删文件。

从上游仓库只取了 `src/flowocr/`、`LICENSE` 和 `LICENSES/`。没有取的：

- `pyproject.toml`、`uv.lock`：`engines/` 下不许有可安装的包（`tests/test_engine_packages.py`）。依赖由 `screen-text` 层提供，版本钉在 `src/nonoka_x/resources/pylock.screen-text-win-py312.toml`。
- `tests/`、`dev_tools/`、`explore/`、`docs/`、`examples/`：开发用，运行时用不到。

整合代码都在 `adapter/flowocr_host/`：

- `__main__.py`：引擎包协议（`wire.py`，与其他包逐字相同）。
- `bridge.py`：把启动上下文写成 FlowOCR 读的环境变量（数据根、模型目录、TensorRT 缓存、游戏文本包目录），并列出它读的全部变量。
- `ops.py`：`check`、`recognize`；`recognize` 调 FlowOCR 自己的命令行入口，产物只经 `flowocr.artifacts.tracksio` 读，转成 Nonoka 的 `ocr.json`。
- `jobobject.py`：Windows 上把本进程放进 `KILL_ON_JOB_CLOSE` 的 Job Object，进程被杀时 FlowOCR 的子进程一起退出。

升级上游：换掉 `src/flowocr/`，重写 `QUALITY_LOCK.json`，改 `UPSTREAM.json` 与 `PACKAGE.json` 的 `upstream`，重建 `screen-text` 层（派生图要用新版本重新生成）。
