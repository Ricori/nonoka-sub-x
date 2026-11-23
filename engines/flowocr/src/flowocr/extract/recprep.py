"""rec 的**前处理**（裁剪 -> 张量），只依赖 numpy + cv2、**不 import 任何 flowocr 模块**。

为什么单独一个文件（2026-09-23，decode-buffer）：前处理原来在管线进程里做，6,400 个裁剪单独跑约 1 s，
管线里却要 5~9 s——几个小 numpy 操作每个都要抢一次 GIL，旁边有判决 / det / 辅助流几个线程。
挪进 ORT 服务进程（它有自己的 GIL）就不抢了；服务进程按**文件路径**加载这一份（`ort_server.load_recprep`），
不经过 `flowocr.extract` 的包初始化（服务进程可能跑在别的解释器里，也可能没装 flowocr）。
管线这边 `recpack` / `recdecode` 从这里 import，**公式只有这一份**。
"""
from __future__ import annotations

import numpy as np

REC_H = 48
"""rec 输入高（PP-OCRv6 rec 的 `RecResizeImg` 是 3×48×W）。"""

REC_MAX_W = 3200
"""`OCRReisizeNormImg.max_imgW`：超过它的输入会被整张压扁到这个宽。"""


def rec_target_w(h: int, w: int, img_h: int = 48, img_w: int = 320) -> int:
    """PaddleX 的 `OCRReisizeNormImg` 给一个 h×w 的裁剪定下的**张量宽**——分桶键就是它。

    `int(48 · max(320/48, w/h))`，超过 3200 的整张压扁到 3200（`max_imgW`），所以**封顶**：
    两个宽高比都超过 66:1 的裁剪，张量宽都是 3200，本来就能同批。
    （09-10 前管线里那份没封顶，只是少批几次，不错；探针里那份封顶了——两份各写各的，已并成这一份。）
    """
    return min(REC_MAX_W, int(img_h * max(img_w / img_h, w * 1.0 / h)))


def grid_w(tw: int, grid: int) -> int:
    """张量宽向上取整到 `grid` 的倍数（封顶 `REC_MAX_W`）；`grid <= 0` 原样返回。
    **形状宽只有这一份公式**：前处理补到它、`OrtRec.shape_w` 报给上游分桶的也是它。"""
    return min(REC_MAX_W, -(-tw // grid) * grid) if grid > 0 else tw


_NORM_LUT = ((np.arange(256, dtype=np.float32) / 255.0 - 0.5) / 0.5).reshape(256, 1).repeat(3, axis=1).reshape(256, 1, 3)
"""uint8 -> 归一化 float32 的查找表（每通道同一张），`cv2.LUT` 一次调用做完 `(x/255 − 0.5)/0.5`。"""


def preprocess(crop: np.ndarray, grid: int = 0) -> tuple[np.ndarray, int]:
    """一个 BGR 裁剪 -> (CHW float32 张量, 张量宽)。等价于 `RecResizeImg` + 归一化。

    高压到 48、按比例缩，右侧零填到 `rec_target_w`；归一化是 `x/255` 再 `(x-0.5)/0.5`。

    `grid > 0`：**把张量宽向上取整到 grid 的倍数**（多填一段零）。为什么要这个旋钮——
    ONNX Runtime 的 CUDA EP **每换一个动态形状就要重新规划**，实测同宽连打 4.6 ms、
    8 个宽轮转 81 ms（17 倍，而且预热无用；一次性探针 `probe_ort_rec_ipc.py`）。
    管线里张量宽在 320/545/792/960/962… 之间抖，所以必须把它收到少数几个宽上。
    右侧零填本来就是这套前处理的一部分，多填一段**不应该改文本**（CTC 会把多出来的 blank 收掉）——
    但这要验，见 `一次性探针 probe_rec_decode.py --grid`。

    归一化走查找表（2026-09-23）：原来是 astype / 除 255 / 减 0.5 / 除 0.5 四个小 numpy 操作，每个都要抢一次 GIL；
    表里的值和原来逐元素是**同一串 float32 运算**，结果逐位相同（300 个随机裁剪上和旧写法 tobytes 全等）。
    """
    import cv2
    h, w = crop.shape[:2]
    tw = grid_w(rec_target_w(h, w), grid)
    rw = max(1, min(tw, int(np.ceil(REC_H * w / h))))
    img = cv2.LUT(cv2.resize(crop, (rw, REC_H)), _NORM_LUT)
    out = np.zeros((3, REC_H, tw), dtype=np.float32)
    out[:, :, :rw] = img.transpose(2, 0, 1)
    return out, tw


def batch(crops: list[np.ndarray], grid: int, k: int) -> np.ndarray:
    """同一个张量宽的一组裁剪 -> `[k, 3, 48, tw]`（不足 k 行补零；k 是批维梯子上的那一档，见 `recort._pad_batch`）。
    管线进程（服务不支持送裁剪时的退回路）和 ORT 服务进程**都走这一个函数**。"""
    xs = [preprocess(c, grid)[0] for c in crops]
    if k > len(xs):
        xs += [np.zeros_like(xs[0])] * (k - len(xs))
    return np.stack(xs)
