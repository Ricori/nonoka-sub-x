"""产物要能追溯到是哪一版代码产的：`git_head()` 和 `code_fp()`，**全仓库只有这一份实现**。

原来 `build_tracks.py` 里那份（`gamescript` / `scriptmatch` / `refine_boundaries` 都借它用）
和 `ocr_args.py` 里那份各算各的，文件表一个记 basename、一个记 `dir/name`。
2026-09-21 搬进正式包时并成一份，**文件表统一记仓库相对路径**（`src/flowocr/analyze/build_tracks.py`、
`src/flowocr/paths.py`），于是搬目录之后表里写的就是代码真正所在的位置，
守卫不再读已迁走的旧路径（project-structure 计划第 1 条）。

⚠ 这次统一让所有产物的 `code_fp` 都变了（表的格式变了，不只是内容）。它证明的是代码身份，
证明不了新旧代码行为等价——迁移验收靠产物级对账（同一份 obs 重建、`obs_identical` / `obs_textdiff`），
不靠指纹相同。改动前的五个指纹记在 project-structure 账本（已删；归档的 project-structure 计划开头写了怎么回查）。

`git_head` 只在源码 checkout / worktree 里有意义；安装态下没有仓库，返回 `"?"`。
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from flowocr.paths import CODE_ROOT, PACKAGE_ROOT, data_root, is_checkout


def version() -> str | None:
    """flowocr 的发行版本（包元数据）。安装态没有 git，`git_head` 是 `"?"`，靠它看出是哪个发行版；取不到记 None。
    源码态以 `git_head` / `code_fp` 为准：这里读的是 venv 里登记的版本，可能落后于 `pyproject.toml`（改了版本号没重新 `uv sync`），别拿它对账。"""
    import importlib.metadata
    try:
        return importlib.metadata.version("flowocr")
    except importlib.metadata.PackageNotFoundError:
        return None


def portable(p) -> str | None:
    """产物里**记**的路径：绝对路径在数据根下的记成相对数据根、在用户目录下的记成 `~/…`，其余与相对路径原样。
    产物会被分享，绝对路径会带出用户名（release-plan F8）。**只用于记**；按记下的路径去开文件用 `local()`。
    相对路径不动：驱动都 cd 到数据根、传的就是相对路径，改它只会让参数值（`0`、`off`…）被误当路径。"""
    if p is None:
        return None
    s = str(p)
    q = Path(s)
    if not q.is_absolute():
        return s
    q = q.resolve()
    for base, prefix in ((data_root(), ""), (Path.home(), "~/")):
        try:
            return prefix + q.relative_to(base.resolve()).as_posix()
        except ValueError:
            continue
    return s


def local(p) -> Path:
    """`portable()` 记下的路径 -> 这台机器上的路径：`~` 展开；相对路径在当前目录不存在就按数据根拼。"""
    if not str(p):
        return Path("")
    q = Path(str(p)).expanduser()
    if q.is_absolute() or q.exists():
        return q
    return data_root() / q


def file_of(name: str, root: Path | None = None) -> Path:
    """指纹表里的仓库相对路径 -> 真文件。`src/flowocr/...` 按**包自己的位置**解析（安装态没有 `src/`，包在 site-packages；
    源码态 `PACKAGE_ROOT.parent` 就是 `<root>/src`，同一个文件）；其余（`tools/`、`explore/`）只在 checkout 里有。
    `root` 给了就只在它下面找（自检用）。2026-09-22 安装态第一次跑就在这里 FileNotFoundError。"""
    if root is not None:
        return root / name
    if name.startswith("src/"):
        return PACKAGE_ROOT.parent / name[len("src/"):]
    return CODE_ROOT / name

DIRTY_PATHS = ("src", "dev_tools", "explore")
"""`git_head()` 判 `+dirty` 时看哪些目录。**别在别处抄第二份**——
只看**产得出产物的代码**：正式包、`tools/`、`explore/`（`dev_tools/reference/match_ref.py` 是产
`ours*_jp.ass` 的一级代码，audit-5 点名要纳入）。文档和 `tests/` 不看——
改它们不影响产物，缀上去只会天天是 dirty。"""


def git_head(root: Path | None = None) -> str:
    """`HEAD` 的短 sha；**代码目录有未提交改动时后面缀 `+dirty`**。

    为什么必须缀（2026-09-08 自己踩的）：跑一批产物的中途我提交了一次，
    于是同一批 11 份产物的 provenance 里躺着两个 `git_head`——
    而**跑的代码自始至终是同一份**（那次提交没有改工作树的内容）。
    反过来更危险：`HEAD` 记的是"最后一次提交是谁"，改了没提交就跑，
    provenance 会**言之凿凿地指向一个并没有产生这份产物的 commit**。
    这和 09-06 那次"照着文档的复现命令跑打出另一张表"是同一个形状。
    """
    if root is None and not is_checkout():
        return "?"                                   # 安装态：没有仓库，别去问 cwd 碰巧所在的别人家的 git
    root = root or CODE_ROOT
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                              text=True, cwd=root, check=True).stdout.strip()[:12]
    except Exception:
        return "?"
    try:
        dirty = subprocess.run(["git", "status", "--porcelain", "--", *DIRTY_PATHS],
                               capture_output=True, text=True, cwd=root,
                               check=True).stdout.strip()
    except Exception:
        return head + "+?"
    return head + ("+dirty" if dirty else "")


def code_fp(files: tuple[str, ...], root: Path | None = None) -> str:
    """`files`（仓库相对路径）的**内容指纹**：sha256 前 12 位，换行统一成 LF。

    `git_head` 回答"最后一次提交是谁"，回答不了"这份代码是不是同一份"：HEAD 移动而代码没变时它会不同
    （feat/game-text 默认臂混了两个 git_head，靠人去 `git diff` 解释），`+dirty` 相同时它又证明不了相同。
    指纹两件都能回答，报表机械地比它（audit-6）。换行统一是因为主 checkout 和槽的
    `core.autocrlf` 可能不同，同一份代码不该因此两个指纹。`root` 只给自检用（默认代码根）。
    路径进 hash（不只内容）：同一份内容换了位置也算换了代码。"""
    h = hashlib.sha256()
    for name in sorted(files):
        h.update(name.encode("utf-8") + b"\0")
        h.update(file_of(name, root).read_bytes().replace(b"\r\n", b"\n") + b"\0")
    return h.hexdigest()[:12]
