"""rec 的**前处理 + CTC 贪心解码**：换推理后端时两边共用这一份。

为什么单独一个模块（"判据只有一份"，同 `srtio` / `recpack` 的思路）：
要把 rec 换到别的运行时（inference-runtime 计划第 2 条），**PaddleX 内部那两段**
——`RecResizeImg` 的前处理和 `CTCLabelDecode` 的后处理——就必须在我们这边有一份等价实现。
一旦这份和 PaddleX 对不上，换后端就会静默改文本，而"文本不一样"在下游看起来和"模型不一样"没区别。
所以：**这份实现先拿 一次性探针 `probe_rec_decode.py` 对着 PaddleX 逐条验，验过了再给后端用。**

口径来自模型自己的 `inference.yml`（不是抄网上的）：
* `PreProcess.RecResizeImg.image_shape = [3, 48, 320]`、`DecodeImage.img_mode = BGR`
  （cv2 读出来就是 BGR，**不做通道交换**）；
* 张量宽用 `recpack.rec_target_w`——**和管线分桶键同一份代码**；
* `PostProcess.name = CTCLabelDecode`、字表在 `PostProcess.character_dict` 里。
  标签表 = `['blank'] + 字表 + [' ']`（PaddleOCR 的 `use_space_char`），共 18,710 类。
"""
from __future__ import annotations

import functools
from pathlib import Path

import numpy as np

from flowocr.extract import recpack  # noqa: F401
# 前处理（裁剪 -> 张量）搬进了 recprep：ORT 服务进程要按文件路径加载它（2026-09-23，decode-buffer）。这里照旧导出
from flowocr.extract.recprep import REC_H, grid_w, preprocess  # noqa: F401

ITEM = "  - "
"""`character_dict` 那一段每一项的固定前缀（缩进两格）。"""


@functools.lru_cache(maxsize=4)
def load_labels(model_dir: str) -> tuple[str, ...]:
    """从模型目录的 `inference.yml` 里读标签表：`['blank'] + character_dict + [' ']`。

    ⚠ 不用 yaml 解析器（生产 venv 里不一定有）：这一段的结构是固定的
    `PostProcess:` -> `character_dict:` -> 一行一个 `  - <字符>`，直到下一个键。

    ⚠⚠ **不能对整行 `strip()`**：字表里**有空白字符本身**（全角空格 U+3000、` ` 这类），
    `str.strip()` 认 Unicode 空白，于是 `"  - 　"` 被 strip 成 `"-"`、判成"列表结束"——
    第一版就这么写的，解析在第 1,750 类上提前收工，解码时 `labels[i]` 直接 IndexError（2026-09-18 实测）。
    所以按**固定前缀** `ITEM` 切，剩下的部分原样留着。

    读出来的条数**必须**和模型输出的类别数对账（调用方拿 `len(labels)` 比）——18,710 类。
    """
    txt = (Path(model_dir) / "inference.yml").read_text(encoding="utf-8").splitlines()
    try:
        i = next(k for k, ln in enumerate(txt) if ln.strip() == "character_dict:")
    except StopIteration:
        raise ValueError(f"{model_dir}/inference.yml 里没有 PostProcess.character_dict")
    chars: list[str] = []
    for ln in txt[i + 1:]:
        if not ln.startswith(ITEM):
            break
        v = ln[len(ITEM):]
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1].replace("''", "'")
        chars.append(v)
    return ("blank", *chars, " ")


def ctc_greedy(cls: np.ndarray, prob: np.ndarray, labels: tuple[str, ...]) -> list[tuple[str, float]]:
    """CTC 贪心解码：逐样本**先并相邻重复、再去 blank**，分数 = 留下那些位置的概率均值。

    `cls` / `prob` 形状都是 [batch, T]（`cls` 是 argmax 的类别，`prob` 是那一位的概率）——
    正是 argmax 融合过的 ONNX 直接给的两个小张量，也等价于对 logits 自己取 argmax / max。
    顺序要紧：**先并重复再去 blank**（反过来会把 `ll` 这种叠字并成一个）。
    """
    out: list[tuple[str, float]] = []
    for k in range(cls.shape[0]):
        ids, ps = cls[k], prob[k]
        keep = np.ones(len(ids), dtype=bool)
        keep[1:] = ids[1:] != ids[:-1]
        keep &= ids != 0
        sel = np.nonzero(keep)[0]
        text = "".join(labels[i] for i in ids[sel]) if len(sel) else ""
        score = float(ps[sel].mean()) if len(sel) else 0.0
        out.append((text, score))
    return out
