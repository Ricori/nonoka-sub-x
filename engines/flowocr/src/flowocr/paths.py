"""代码在哪、产物和素材在哪——**这两件事分开**。

照搬另一个项目（finesub）的做法：

> Running from a checkout uses the checkout's own data …
> **A git worktree resolves to the main checkout.**

理由在这个项目里更硬：`out/`、`tmp/`、`data/` 里的素材和 explore 下各实验的 venv 都是
**ignore 的**，linked worktree 里根本不存在。所以：

* **代码**从这份文件所在的 checkout 走（槽里改的代码就是跑的代码，`git_head` 也从这里算）；
* **产物与素材**走 `data_root()`——在 worktree 里就是它背后的**主 checkout**。

这样"在槽里改完直接跑整片"不需要建 junction、不需要手抄绝对路径，
而 provenance 里的 `git_head` 仍然是**槽自己的**那棵干净的树。

**三种运行形态**（2026-09-21 迁进正式包时加的第三种）：

| 形态 | 判据 | `CODE_ROOT` | `data_root()` |
| --- | --- | --- | --- |
| 源码 checkout | `<root>/.git` 存在（目录）且有 `pyproject.toml` | 那个 checkout | 同 `CODE_ROOT` |
| linked worktree | `<root>/.git` 是个 `gitdir:` **文件** | 槽 | **主 checkout** |
| 安装后运行 | 上面两条都不成立（包在 site-packages 里） | 无意义（只剩 `PACKAGE_ROOT`） | 用户数据目录（下表） |

安装态的用户数据目录：Windows `%LOCALAPPDATA%/flowocr`，其余 `$XDG_DATA_HOME/flowocr`
或 `~/.local/share/flowocr`。`FLOWOCR_DATA_ROOT` 在三种形态下都能覆盖
（对应 finesub 的 `FINESUB_ROOT`）。⚠ 装进 `<checkout>/.venv` 的**非** editable 安装也算安装态：
判据看的是包文件自己往上三层有没有 checkout，不看 venv 在哪。

    python -m flowocr.paths            # 打印产物根（驱动脚本 cd 到这里）
    python -m flowocr.paths --code     # 打印代码根
    flowocr-paths                      # console script，同一份实现（`tools/paths.py` 那个 shim 2026-09-22 已删）
"""
from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
"""包自己所在的目录（`src/flowocr/` 或 `site-packages/flowocr/`）。"""

CODE_ROOT = PACKAGE_ROOT.parents[1]
"""这份代码所在的 checkout（`src/flowocr/` 往上两层）。**只在源码 / worktree 形态下有意义**，
安装态下它指向 site-packages 的上一层，别拿它拼路径——先问 `is_checkout()`。"""


def main_checkout(root: Path) -> Path | None:
    """`root` 是 linked worktree 的话，返回它背后的主 checkout；否则 `None`。

    linked worktree 的 `.git` 是**一个文件**，内容形如
    `gitdir: <main>/.git/worktrees/<slot>`；主 checkout 就在它上面三层
    （去掉 `<slot>`、`worktrees`、`.git`）。这段判据抄自 finesub 的
    `_main_worktree_root`，连"要检查倒数第二、三段是不是 worktrees/.git"都一样——
    少了那一步，`gitdir:` 指向别处的仓库也会被当成主 checkout。
    """
    pointer = root / ".git"
    if not pointer.is_file():
        return None
    try:
        body = pointer.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not body.startswith("gitdir:"):
        return None
    recorded = Path(body.removeprefix("gitdir:").strip())
    if not recorded.is_absolute():
        recorded = (root / recorded).resolve()
    if (recorded.parent.name, recorded.parent.parent.name) != ("worktrees", ".git"):
        return None
    return recorded.parents[2]


def is_linked_worktree(root: Path | None = None) -> bool:
    return main_checkout(root or CODE_ROOT) is not None


def is_checkout(root: Path | None = None) -> bool:
    """`root` 是不是一份源码 checkout（主 checkout 或 linked worktree）。

    两个条件都要：`.git`（目录或 `gitdir:` 文件）+ `pyproject.toml`。只看 `.git` 不够——
    site-packages 的上一层碰巧在某个别的仓库里时也有 `.git`。
    """
    root = root or CODE_ROOT
    return (root / ".git").exists() and (root / "pyproject.toml").is_file()


def user_data_root() -> Path:
    """安装态的产物 / 缓存根。不建目录，只算路径。"""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "flowocr"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "flowocr"


def data_root(root: Path | None = None) -> Path:
    """产物与素材的根。**worktree 解析回主 checkout；安装态落到用户数据目录。**"""
    configured = os.environ.get("FLOWOCR_DATA_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    root = root or CODE_ROOT
    if not is_checkout(root):
        return user_data_root()
    return main_checkout(root) or root


def models_root() -> Path:
    """ONNX 模型的根：`FLOWOCR_MODELS` > 数据根的 `models/`（源码态是主 checkout 的 `models/`，已 ignore；安装态是用户数据目录）。
    2026-09-22 起两种状态同一个位置：模型改成官方发布的（`flowocr.models`），不再依赖按约定随时可删的 onnxrt 实验目录下的 `models/`——
    那里还躺着以前自己转的几份，给历史臂用（显式路径照样认，见 `resolve_model`）。"""
    configured = os.environ.get("FLOWOCR_MODELS", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return data_root() / "models"


def trt_root() -> Path:
    """TensorRT 后端的库和引擎缓存（`flowocr.trt`）的根：`FLOWOCR_TRT` > 数据根的 `trt/`（源码态是主 checkout 的 `trt/`，已 ignore；
    安装态是用户数据目录）。下面按 TensorRT 版本分目录。"""
    configured = os.environ.get("FLOWOCR_TRT", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return data_root() / "trt"


def gametext_root() -> Path:
    """游戏文本包（`gtd-bundle/1`，`flowocr.analyze.gtdbundle`）的默认位置：`FLOWOCR_GAMETEXT` > 数据根的 `gametext/`
    （源码态是主 checkout 的 `gametext/`，已 ignore；安装态是用户数据目录）。包是私下拿到的，放进来即可，不用解压。"""
    configured = os.environ.get("FLOWOCR_GAMETEXT", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return data_root() / "gametext"


def resolve_model(path: str) -> str:
    """模型文件 / 目录的路径。给的路径存在（绝对，或相对当前目录——驱动脚本 cd 到产物根的老规矩）就原样用；
    不存在就按**文件名**到 `models_root()` 里找；没有就原样返回，让打开它的那一步报清楚。

    默认值字符串（`ocr_args.OLD_DET_ONNX` / `OLD_REC_ONNX`，旧的自转模型路径）**不动**：它写进 obs 的 `config`，换了会让磁盘上的现成产物全部对不上。"""
    p = Path(path)
    if p.exists():
        return str(p)
    cand = models_root() / p.name
    return str(cand) if cand.exists() else path


def python_cmd() -> str:
    """当前解释器写成能直接粘进终端的形式：`uv tool install` 装的用户 PATH 上的 `python` 导不进 flowocr，打给人照抄的
    `-m flowocr.…` 命令要用这一个。Windows 上带空格时按 PowerShell 写（`& "…"`），别处按 POSIX shell 引（安全的路径原样）。"""
    exe = sys.executable
    if sys.platform == "win32":
        return f'& "{exe}"' if " " in exe else exe
    return shlex.quote(exe)


def child_env(env: dict | None = None) -> dict:
    """给要 `-m flowocr.…` 的子进程用的环境：把装着 `flowocr` 的那个目录放进 `PYTHONPATH` 最前面。

    源码 checkout 里那是 `<root>/src`（默认不在 sys.path 上，父进程靠脚本开头的引导行放进去的，
    子进程继承不到）。**安装态什么都不加**：包在 site-packages 里本来就找得到，而把 site-packages 塞进 `PYTHONPATH`
    会泄漏给**别的解释器**起的子进程——2026-09-22 nightly 用 `FLOWOCR_ORT_PYTHON` 把 ORT worker 钉到另一个 venv，
    那个解释器却先 import 到了本 venv 的 onnxruntime 和 CUDA 库（PYTHONPATH 排在它自己的 site-packages 前面）。
    `run_ocr2` 的监督者起 worker、`--workers N` 切段都走这里。"""
    env = dict(os.environ if env is None else env)
    if not is_checkout():
        return env
    here = str(PACKAGE_ROOT.parent)
    old = env.get("PYTHONPATH", "")
    if here not in old.split(os.pathsep):
        env["PYTHONPATH"] = here + (os.pathsep + old if old else "")
    return env


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="打印代码根 / 产物根")
    ap.add_argument("--code", action="store_true", help="打印代码根（默认打印产物根）")
    a = ap.parse_args(argv)
    print(CODE_ROOT if a.code else data_root())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
