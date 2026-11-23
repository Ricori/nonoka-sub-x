"""ORT 路要的 ONNX 模型：**全用官方发布的**（owner 2026-09-22），钉版本、验 sha256、缺了就取。

PaddlePaddle 自己在 Hugging Face 上发了 PP-OCRv6 的 ONNX（Apache-2.0，`LICENSES/PP-OCRv6-NOTICE.md`）。
以前用的是我们自己拿 paddle2onnx 转的那几份（`tools/export_*_onnx.sh`，落在 onnxrt 实验目录的 `models/` 下，
按约定随时可删、发行包里也没有）；换成官方的之后，源码安装和发行包取的是同一份、谁都能从上游核对。
owner 同日定：**旧模型产的数据和新模型的区别按噪音级算**（`ocr_args.EQUIV_VALUES` 里 `det_onnx` / `rec_onnx` 认旧的自转那份和空串互通），不重量。

**按语言另下的 rec**（owner 2026-10-05）：默认的 v6 medium 字表覆盖简中、繁中、英、日和西欧几种拉丁字母语言，没有韩文、西里尔字母、泰文。
`--ocr-lang ko / ru / th` 用到时才取对应的官方 v5 mobile 模型（`LANG_REC`），`auto` 三个都取；越南语没有覆盖的官方模型。
三个都含英文字母和数字、不含汉字和假名，输入形状和默认的一样（3×48×320），同样融进 argmax。

**我们只动一处**：rec 的图末尾加 `ArgMax` + `ReduceMax` 两个节点（CTC 贪心解码只要这两样），**权重一个字节不动**。
不融的话 worker 每批要把 `[batch, T, 18710]` 的 fp32 logits 整个拷回主机（批 32 约 191 MB，recort 文件头），
融了只回两个 `[batch, T]` 小张量——融合是 ORT 那条路快的主要原因（inference-runtime 第 2 条）。
**rec 不转半精度**：官方只发 fp32；端到端 fp32 + 融合 53.7 s 对 fp16 + 融合 54.9 s（yuka-f5 10 分钟，同一处），
半精度没买到墙钟，还在 ORT 1.26 / 1.27 的 CPU EP 上打不开（project-structure 账本，已删；归档的 project-structure 计划开头写了怎么回查）。

放在哪：`paths.models_root()`（`FLOWOCR_MODELS` > 数据根的 `models/`），每个仓库一个子目录。
下载走 `HF_ENDPOINT`（默认 `https://huggingface.co`，国内可设镜像）。先写 `.part` 再改名，中途断了不留半个文件。

    python -m flowocr.models fetch            # 默认要的（rec + det，默认都走 ORT）
    python -m flowocr.models fetch rec        # 只取点名的
    python -m flowocr.models list
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from flowocr import paths


@dataclass(frozen=True)
class Model:
    repo: str
    rev: str
    """Hugging Face 上的**提交号**（不是分支名）：`resolve/<rev>/` 下的内容不可变。"""
    files: dict[str, str] = field(default_factory=dict)
    """文件名 -> sha256。下载完逐个核，对不上就删掉、报错。"""
    fuse_argmax: bool = False
    main: str = "inference.onnx"
    """`files` 里管线要加载（或从它派生）的那个 ONNX。"""

    @property
    def dir(self) -> Path:
        return paths.models_root() / self.repo.split("/")[-1]

    @property
    def onnx(self) -> Path:
        """管线真正加载的那个文件。派生图的文件名带融合做法的版本（`FUSE_VERSION`）：几个 worktree / 分支共用一个模型根，
        做法不同的代码各用各的文件，不会互相把对方的派生图判成过期、来回重做。"""
        return self.dir / (f"inference_argmax{FUSE_VERSION}.onnx" if self.fuse_argmax else self.main)


MODELS = {
    "rec": Model("PaddlePaddle/PP-OCRv6_medium_rec_onnx", "50c7eacafc52fa7bcf4194e8cd08e46f8558504b", {
        "inference.onnx": "9c09abf0957f7968c7586464b7397b84ad2387a0497a351af40e9acc71b673ba",
        "inference.yml": "991b700facf5b50a7de193468207d5f4255b538dde0d312ae3b7c7a9b6873129",
    }, fuse_argmax=True),
    "det": Model("PaddlePaddle/PP-OCRv6_medium_det_onnx", "61323801669c338b7891481ec7bac61ce31b576a", {
        "inference.onnx": "eb13b44b25bb36f89528b68720af8a61d9cf381176107f465db1757b65d086e1",
        "inference.yml": "7298d5ead546584af2504d03355f881ac7a7bc0eb1e282d3e159277c1d0af871",
    }),
}
"""rec 的字表就用它自己仓库里的 `inference.yml`（`recort` 按模型所在目录找），模型和字表出自同一个提交。
2026-09-22 核过：它和 PaddleX 模型目录（`official_models/PP-OCRv6_medium_rec`）里那份 `inference.yml` **逐字节相同**（18,710 类）。
rec 的 `inference.onnx` 的 sha256 和 Hugging Face 上 LFS 记的 oid 一致；其余三个是取下来时算的（提交号钉住，内容本就不可变）。"""

MODELS.update({
    "rec-ko": Model("PaddlePaddle/korean_PP-OCRv5_mobile_rec_onnx", "5c6f574b8e2230adf4287b33e736d71b9fabd28e", {
        "inference.onnx": "92f0b7785e64fc9090106a241cf4c1eb97472824558272751b88a2a4476d3a08",
        "inference.yml": "f757fa1c40e99edcf27e9cce879b93eb2a51fa46f5ef39095689b8c37dd75998",
    }, fuse_argmax=True),
    "rec-ru": Model("PaddlePaddle/eslav_PP-OCRv5_mobile_rec_onnx", "9a32171fc5718746875e1a261818884517975013", {
        "inference.onnx": "b3018ef2b09a0250b6e0c8e871c927098363e5fd4df890cc68e8358eb0aaf1bd",
        "inference.yml": "025039bac23eb4a308efcefa4d58eab3af440767815c6ba6938468bf6353ee5a",
    }, fuse_argmax=True),
    "rec-th": Model("PaddlePaddle/th_PP-OCRv5_mobile_rec_onnx", "1d4adbbafb1034a2fd6618498575b81ea7b69f69", {
        "inference.onnx": "27618be66018f8598ac0a526a593f9f1cebf794e7eded93428e8fb016e537f5f",
        "inference.yml": "f6ba7fefc38ca1ff398ddafa75d67d16e0b3757c4e6c833adffee98a981766c9",
    }, fuse_argmax=True),
})
"""按语言另下的 rec（2026-10-05 取的提交号；onnx 的 sha256 是 Hugging Face 上 LFS 记的 oid，yml 是取下来时算的）。
俄语用 `eslav`（俄 / 乌 / 白）不用 `cyrillic`（字表大一倍、覆盖更多语言，俄语用不上）。许可都是 Apache-2.0（模型卡）。"""

MODELS["lama"] = Model("Carve/LaMa-ONNX", "c3c0c9e468934d62e79c329e35d82dd09ff8c444", {
    "lama_fp32.onnx": "1faef5301d78db7dda502fe59966957ec4b79dd64e16f03ed96913c7a4eb68d6",
}, main="lama_fp32.onnx")
"""阶段 5 inpainting 用的 LaMa（big-lama，512×512 输入）。**"全用官方发布的"的例外**（owner 2026-10-31："就用 Hugging Face 上的就好"）：
上游（advimman/lama）只发 PyTorch 权重，这份是 Carve 转的 ONNX（Apache-2.0，模型卡说傅里叶单元换了自己的写法、结果和原模型相同）。
sha256 是 Hugging Face 上 LFS 记的 oid。阶段 5 第一次用时才取；派生图（折叠、扁窗、激活缩放）由 `flowocr.burn.lamagraph` 做。"""

LANG_REC = {"zh": "rec", "ko": "rec-ko", "ru": "rec-ru", "th": "rec-th"}
"""`--ocr-lang` -> 用哪个 rec。`zh` 是默认模型（覆盖简中、繁中、英、日、德、法、西、葡、意、土、印尼）。"""

DEFAULT = ("rec", "det")
"""默认配置要的：rec 和 det（2026-09-24 起 det 也走 ORT，D1）。原来只列 rec，翻默认时没跟着改，
断网的机器上 `python -m flowocr.models fetch` 之后默认配置仍缺 det 的 ONNX（2026-09-24 Codex 审计）。"""


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _download(url: str, dst: Path, want_sha: str) -> None:
    tmp = dst.with_name(dst.name + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "flowocr"})
    print(f"[模型] 下载 {url}", flush=True)
    h = hashlib.sha256()
    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
        for chunk in iter(lambda: r.read(1 << 20), b""):
            f.write(chunk)
            h.update(chunk)
    got = h.hexdigest()
    if want_sha and got != want_sha:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"{url} 的 sha256 是 {got}，钉的是 {want_sha}——没装，别往下跑")
    os.replace(tmp, dst)


FUSE_SPLIT = 32
"""输出头沿类别维分几段归约（`fuse_argmax`）。"""
FUSE_VERSION = 2
"""融合做法的版本，写进来源标记：做法改了，各机器上已经派生过的图要重做（`derived_stale`）。
1 = 末尾直接 `ArgMax` / `ReduceMax`；2 = 沿类别维两级归约（2026-10-11，gpu-sched 计划 S1）。"""


def fuse_argmax(src: Path, dst: Path, split: int = FUSE_SPLIT, mark: Path | None = None, want: str = "") -> None:
    """rec 图末尾加 argmax / max：输出从 `[batch, T, C]` 的 softmax 输出变成 `cls`（int64）+ `prob`（float），形状 `[batch, T]`。
    只加节点、改输出声明，**不碰权重和已有节点**。

    **沿类别维两级归约**（2026-10-11，gpu-sched 计划 S1）：CUDA EP 的 `ArgMax` / `ReduceMax` 两个核占低、每次调用一截约 1 ms 的尾巴。
    推断（没读 ORT 源码）`ArgMax` 按行（batch × T）并行、一个线程在行内串行扫完 C 个类别（C = 18710 时批 1 宽 320 只有 40 行、一个块）；
    `ReduceMax` 走 cuDNN 的归约核、布局不同，收益是微基准量出来的。
    这里把每行切成 k 段（`[batch, T, k, L]`，段数挑法见下面；C 除不尽 k 时补 0 到 k·L），先在每段里取 max / argmax，再在 k 段之间取：
    max 两级算完全精确；argmax 两级都取第一个最大，等价于整行"第一个最大"（并列时取低下标，和原来一致）；
    补的 0 永远选不中（softmax 输出每行和为 1，最大值一定 > 0；就算整段下溢成 0，补齐段排在最后，第一个最大也先落在真位置上）。
    尾部单独量（空闲机器、按 hsr-s2 的形状加权）：每千次调用 1431 → 296 ms（10 段、不补齐；32 段补齐是 265 ms），对 numpy 的第一个最大逐位相同。
    C 不是定值（符号维）、不到 2·split、或源图太老（opset < 11 / IR < 4）时退回一级。
    "并列取低下标"在 CPU EP 上由守卫钉住；CUDA EP 的 `ArgMax` 并列时取哪个没单独钉，真实裁剪上新旧 cls 全同。"""
    import onnx
    from onnx import TensorProto, helper
    m = onnx.load(str(src))
    g = m.graph
    y = g.output[0]
    dims = [d.dim_param or d.dim_value for d in y.type.tensor_type.shape.dim]
    c = dims[2] if len(dims) == 3 and isinstance(dims[2], int) and dims[2] > 0 else 0
    opset = next((o.version for o in m.opset_import if o.domain in ("", "ai.onnx")), 0)
    i64 = lambda name, v, shape=None: helper.make_tensor(name, TensorProto.INT64, [len(v)] if shape is None else shape, v)

    def rmax(x: str, out: str, keep: int) -> "onnx.NodeProto":      # ReduceMax 的 axes 在 opset 18 从属性改成输入
        if opset >= 18:
            return helper.make_node("ReduceMax", [x, "fa_last"], [out], keepdims=keep)
        return helper.make_node("ReduceMax", [x], [out], axes=[-1], keepdims=keep)

    # 两级要 GatherElements 和以输入给的 Pad 参数（opset 11 起），initializer 不列进图输入要 IR ≥ 4：
    # 韩 / 俄 / 泰的 rec 是 IR 3 / opset 7，退回一级、也不加 initializer（不然 ORT 加载不了）
    if c < 2 * split or opset < 11 or m.ir_version < 4:
        if opset >= 18:
            g.initializer.append(i64("fa_last", [-1]))
        g.node.append(helper.make_node("ArgMax", [y.name], ["cls"], axis=-1, keepdims=0))
        g.node.append(rmax(y.name, "prob", 0))
    else:
        # 段数：优先挑能整除 C、离 split 最近的（[split/4, 2·split] 里），整除就不用补齐——补齐是把整个 softmax 输出复制一份，
        # 每个形状的 session 都多一块激活（quick 上整卡显存峰值多约 180 MB）；C = 18710 时取 10 段、每段 1871。挑不到才按 split 段补齐
        k = min((d for d in range(max(2, split // 4), 2 * split + 1) if c % d == 0), key=lambda d: abs(d - split), default=split)
        n = -(-c // k)                                       # 每段几个类别
        g.initializer.extend([i64("fa_seg_shape", [0, 0, k, n]), i64("fa_last", [-1]), i64("fa_n", [n], [])])
        if k * n > c:
            g.initializer.append(i64("fa_pads", [0, 0, 0, 0, 0, k * n - c]))
            g.node.append(helper.make_node("Pad", [y.name, "fa_pads"], ["fa_padded"], mode="constant"))
        g.node.extend([
            helper.make_node("Reshape", ["fa_padded" if k * n > c else y.name, "fa_seg_shape"], ["fa_seg"]),  # [batch, T, k, L]（0 = 照抄这一维）
            rmax("fa_seg", "fa_segmax", 0),                                                   # [batch, T, k]
            helper.make_node("ArgMax", ["fa_seg"], ["fa_segarg"], axis=-1, keepdims=0),
            helper.make_node("ArgMax", ["fa_segmax"], ["fa_which"], axis=-1, keepdims=1),     # [batch, T, 1]
            rmax("fa_segmax", "prob", 0),
            helper.make_node("GatherElements", ["fa_segarg", "fa_which"], ["fa_inner"], axis=-1),
            helper.make_node("Mul", ["fa_which", "fa_n"], ["fa_base"]),
            helper.make_node("Add", ["fa_base", "fa_inner"], ["fa_cls"]),
            helper.make_node("Squeeze", ["fa_cls", "fa_last"], ["cls"]) if opset >= 13       # Squeeze 的 axes 在 opset 13 改成输入
            else helper.make_node("Squeeze", ["fa_cls"], ["cls"], axes=[-1])])
    del g.output[:]
    g.output.extend([helper.make_tensor_value_info("cls", TensorProto.INT64, dims[:2]),
                     helper.make_tensor_value_info("prob", TensorProto.FLOAT, dims[:2])])
    _save_derived(m, dst, mark, want)


def _save_derived(m, dst: Path, mark: Path | None = None, want: str = "") -> None:
    """派生图先写 `.part-<pid>` 再改名，改名成功后写来源标记 `mark`（内容 `want`）：几个进程同时第一次派生
    （`--no-ort-share` 的多组 / `--workers`，父进程不预先生成）时各写各的。Windows 上目标正被别人打开、改名失败时，
    只有标记已经等于这次要写的（别的进程刚发布了同一版）才接受别人那份；目标是过期的旧图就照旧抛，不把它标成新的。"""
    import onnx
    tmp = dst.with_name(f"{dst.name}.part-{os.getpid()}")
    onnx.save(m, str(tmp))
    try:
        os.replace(tmp, dst)
    except OSError:
        tmp.unlink(missing_ok=True)
        if not (mark is not None and dst.is_file() and mark.is_file() and mark.read_text(encoding="utf-8").strip() == want):
            raise
        return
    if mark is not None:
        mark.write_text(want + "\n", encoding="utf-8")


def _src_mark(m: Model) -> Path:
    """派生文件旁边记"它是从哪份源文件、按哪版做法派生的"（`_mark_text`）。"""
    return m.onnx.with_name(m.onnx.name + ".src-sha256")


def _mark_text(m: Model) -> str:
    """来源标记的内容：源文件的 sha256 + 融合做法的版本（`FUSE_VERSION`）。没有版本的旧标记对不上，于是重做。"""
    return f"{m.files[m.main]} fuse{FUSE_VERSION}"


def derived_stale(m: Model) -> bool:
    """派生的 argmax 图要不要重做：不在、没有来源记录、来源不是现在钉的那份源文件、或融合做法换了版本。
    ⚠ 只看"派生文件在不在"是错的（Codex 复审）：表里换了提交号 / 源文件重新下载之后，旧的派生图照样被拿去用。"""
    if not m.fuse_argmax:
        return False
    mark = _src_mark(m)
    return not (m.onnx.is_file() and mark.is_file()
                and mark.read_text(encoding="utf-8").strip() == _mark_text(m))


def ensure(m: Model) -> Path:
    """一个模型就位：源文件缺了 / sha 对不上就（重新）下载，派生图过期就重做。返回管线要加载的那个文件。"""
    m.dir.mkdir(parents=True, exist_ok=True)
    base = os.environ.get("HF_ENDPOINT", "https://huggingface.co").rstrip("/")
    for fn, want in m.files.items():
        p = m.dir / fn
        if p.is_file() and (not want or sha256(p) == want):
            continue
        _download(f"{base}/{m.repo}/resolve/{m.rev}/{fn}", p, want)
    if derived_stale(m):
        fuse_argmax(m.dir / m.main, m.onnx, mark=_src_mark(m), want=_mark_text(m))
        print(f"[模型] {m.onnx.name}：融进 argmax（{m.onnx.stat().st_size / 2**20:.1f} MB）", flush=True)
    return m.onnx


def fetch(name: str) -> Path:
    """按名字取一个模型（`ensure(MODELS[name])`）。"""
    return ensure(MODELS[name])


F16_VERSION = 1
"""半精度派生图的做法版本（文件名和来源标记都带它，同 `FUSE_VERSION`）。"""


def fp16_derive(src: Path, dst: Path, mark: Path | None = None, want: str = "") -> None:
    """fp32 图 -> 半精度图：ORT 自带的转换器（`onnxruntime.transformers.float16`），**输入输出仍是 fp32**（`keep_io_types`，
    图里只多首尾两个 Cast），调用方和前后处理都不用改。权重转成 fp16、计算在 fp16 上（GPU 上走张量核）。
    只给 GPU 用：CPU EP 上半精度更慢、旧版还打不开（见模块文档），`run_ocr2.onnx_path` 在 CPU 上一律取 fp32。"""
    import onnx
    from onnxruntime.transformers.float16 import convert_float_to_float16
    m = convert_float_to_float16(onnx.load(str(src)), keep_io_types=True)
    _save_derived(m, dst, mark, want)


def fp16_path(name: str) -> str:
    """默认模型的半精度派生图（`--det-precision fp16`，defaults.md 1.58）：从 `path(name)` 那份 fp32 图派生，放在它旁边，
    来源标记记源文件的 sha256 + 做法版本，对不上就重做（同 argmax 派生图的规矩，`derived_stale`）。"""
    key = f"{name}:fp16"
    if key in _ready:
        return _ready[key]
    src = Path(path(name))
    dst = src.with_name(f"{src.stem}_fp16v{F16_VERSION}.onnx")
    mark = dst.with_name(dst.name + ".src-sha256")
    want = f"{sha256(src)} fp16v{F16_VERSION}"
    if not (dst.is_file() and mark.is_file() and mark.read_text(encoding="utf-8").strip() == want):
        fp16_derive(src, dst, mark, want)
        print(f"[模型] {dst.name}：转成半精度（{dst.stat().st_size / 2**20:.1f} MB）", flush=True)
    _ready[key] = str(dst)
    return _ready[key]


_ready: dict[str, str] = {}
"""本进程里核过的（每个进程核一次源文件 sha256，76 MB 约 0.15 s；run_ocr2 一趟会问好几次）。"""


def path(name: str) -> str:
    """管线用的入口：默认模型的路径，**缺了 / 过期了就当场取**（同 PaddleX 首次运行下它自己的模型）。
    取不到（断网、sha 对不上）就抛，报错里带手动取的命令。"""
    if name in _ready:
        return _ready[name]
    m = MODELS[name]
    try:
        _ready[name] = str(ensure(m))
    except Exception as exc:
        offline = ("或用 GitHub Release 上的模型包 `flowocr-models install`" if name in DEFAULT
                   else f"离线使用时把联网机器模型根下的 {m.dir.name}/ 拷进本机模型根")
        raise RuntimeError(f"默认 {name} 模型不在（或核不上）{m.onnx}，自动下载失败：{exc}。"
                           f"联网后跑 `flowocr-models fetch {name}`（镜像用 HF_ENDPOINT）；{offline}") from exc
    return _ready[name]


def meta(p: str) -> dict:
    """写进 obs `_meta.models` 的那一条：文件名 + sha256——复用判据只比路径字符串，**同名文件换了内容它看不出来**，这里记下内容。"""
    q = Path(p)
    return {"file": f"{q.parent.name}/{q.name}", "sha256": sha256(q) if q.is_file() else None}     # 带目录名：各语言的 rec 派生图同名（inference_argmax<版本>.onnx）


def pack(dst: Path) -> list[str]:
    """离线包（发版时由维护者打，挂在 GitHub Release）：默认模型的**源文件**（不含派生的 argmax 图，`install` 之后首次用时就地融）
    + `LICENSES/` 里的许可与改动说明。只打核过 sha256 的文件；源码 checkout 里才有 `LICENSES/`。"""
    lic = paths.CODE_ROOT / "LICENSES"
    if not lic.is_dir():
        raise SystemExit(f"找不到 {lic}：离线包要在源码 checkout 里打（要带上模型的许可说明）")
    names = []
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_STORED) as z:
        for n in DEFAULT:
            m = MODELS[n]
            for fn, want in m.files.items():
                src = m.dir / fn
                if not src.is_file() or sha256(src) != want:
                    raise SystemExit(f"{src} 不在或 sha256 对不上：先 `flowocr-models fetch {n}`")
                z.write(src, f"{m.dir.name}/{fn}")
                names.append(f"{m.dir.name}/{fn}")
        for f in sorted(lic.iterdir()):
            z.write(f, f"LICENSES/{f.name}")
    return names


def install(src: Path) -> list[str]:
    """把 `pack` 打的离线包装进模型根：只认表里的模型文件、逐个核 sha256，对不上就不写；`LICENSES/<文件名>` 一并放进模型根。
    名字之外的一律拒绝（`LICENSES/../x` 这类穿出模型根的也算）；写文件先写 `.part` 再改名，中途断了不留半个文件。"""
    known = {f"{m.dir.name}/{fn}": (m, fn, want) for m in MODELS.values() for fn, want in m.files.items()}
    root = paths.models_root()
    done = []
    with zipfile.ZipFile(src) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            name = info.filename
            data = z.read(info)
            if name in known:
                m, fn, want = known[name]
                if hashlib.sha256(data).hexdigest() != want:
                    raise SystemExit(f"{name} 的 sha256 对不上：包坏了，重新下一份")
                dst = m.dir / fn
            else:
                head, sep, leaf = name.partition("/")
                if not (head == "LICENSES" and sep and leaf and leaf not in (".", "..")
                        and not any(c in leaf for c in '/\\:')):
                    raise SystemExit(f"包里有不认识的文件 {name}：不是 `flowocr-models pack` 打的包")
                dst = root / "LICENSES" / leaf
            dst.parent.mkdir(parents=True, exist_ok=True)
            part = dst.with_name(dst.name + ".part")
            part.write_bytes(data)
            os.replace(part, dst)
            done.append(str(dst))
    return done


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="flowocr-models", description="取 / 列官方 ONNX 模型")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="下载并核对（已在就跳过）")
    f.add_argument("names", nargs="*", help=f"{' / '.join(MODELS)}，默认 {' '.join(DEFAULT)}（离线机器要别的语言：在联网的机器上取好，把模型根拷过去）")
    sub.add_parser("list", help="列出模型根里有什么")
    sub.add_parser("install", help="装离线包（GitHub Release 上的模型 zip）：核过 sha256 再放进模型根").add_argument("zip")
    sub.add_parser("pack", help="打离线包（维护者发版用）").add_argument("zip")
    a = ap.parse_args(argv)
    if a.cmd == "install":
        for p in install(Path(a.zip)):
            print(p)
        print(f"装好了：{paths.models_root()}（第一次跑会就地融 argmax 图，不联网）")
        return 0
    if a.cmd == "pack":
        print("\n".join(pack(Path(a.zip))))
        print(f"-> {a.zip}（{Path(a.zip).stat().st_size / 2**20:.1f} MB）")
        return 0
    if a.cmd == "fetch":
        bad = [n for n in a.names if n not in MODELS]
        if bad:
            ap.error(f"不认识的模型 {bad}（有 {list(MODELS)}）")
        for n in a.names or DEFAULT:
            print(f"{n}: {fetch(n)}")
        return 0
    print(f"模型根：{paths.models_root()}")
    for n, m in MODELS.items():
        src_ok = all((m.dir / fn).is_file() for fn in m.files)
        state = ("在" if m.onnx.is_file() and not derived_stale(m) else
                 "源文件在，argmax 图第一次用时就地融（不联网）" if src_ok else "缺")
        print(f"  {n}: {m.repo}@{m.rev[:8]} -> {m.onnx}（{state}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
