"""det 的前后处理（推理在 ORT 服务里）。前处理最早是为绕开 PaddleX 的 Python 前处理链写的，要求和它**逐字节相同**。

为什么（fps-and-rec-budget 报告）：det 每帧的时间里，`run()` 和进出拷贝之外
还有一大块是 **PaddleX 的 Python 前后处理 + 胶水**——换运行时（ORT / TensorRT）动不到它。
它有多大，同一次运行里的逐段计时见 一次性探针 `probe_infer_split.py`（原来那个"约 12 ms"
是跨两次运行相减估的，端到端只兑现了 3–4 ms/帧，别再引）。这一层做的事本身很少：

    Read(PP-OCRv6 det 的配置里是 BGR，不换序) -> Resize(长边 960、对齐 32)
    -> Normalize(逐通道 astype/乘/加) -> ToCHW(transpose) -> ToBatch(stack)

慢在实现：`Normalize` 走 `cv2.split` -> 三次 `astype(float32)` -> 三次乘加 -> `cv2.merge`，
每一步都新建一个 6.3 MB 的数组；再加上每个 op 上的 `@benchmark.timeit` 装饰器。

这里把 **astype + 乘 + 加** 折成**一张 256×1×3 的 float32 LUT**，`cv2.LUT` 一趟 C++ 出结果；
若某个模型的 Read 真要 RGB，换序也折进 LUT 的下标（见 `__init__` 里按 Read op 实测的那段）。
**这是恒等变换，不是近似**：LUT 里存的就是 `v * alpha[c] + beta[c]`，
uint8 的 256 个取值全枚举，float32 的乘加逐位相同。

后处理（DBPostProcess）是正确性关键的一块：用 `flowocr.extract.detpost` 里照抄 PaddleX 的那份
（照抄时和 PaddleX 逐字节相同；2026-10 运行时去掉 Paddle 之后，守卫对着冻结的 PaddleX 输出做回归）。

用法：

    fast = FastDet.from_onnx(onnx_path, runner, default_yml_ok)  # 配置读 ONNX 旁边的 inference.yml，后处理是 detpost 的
    polys = fast.predict(frame_bgr)
"""
from __future__ import annotations

import time
from pathlib import Path

import cv2
import numpy as np

from flowocr.extract import detpost
from flowocr.extract import timeline as _tl   # --timeline：det 拆成前处理 / 推理 / 后处理三段（关着零开销）


class FastDet:
    """一组前后处理参数（`detpost.DetSpec`）+ 一个 `runner([blob]) -> preds` + 一个后处理对象。"""

    def __init__(self, spec: detpost.DetSpec, runner, post_op) -> None:
        self.spec = spec
        self.runner = runner
        self.post_op = post_op
        self.thresh, self.box_thresh, self.unclip_ratio = spec.thresh, spec.box_thresh, spec.unclip_ratio
        self.max_side_limit = spec.max_side_limit
        self.limit_side_len, self.limit_type = spec.limit_side_len, spec.limit_type

        # **通道序按配置的 Read op 来，别照 PaddleX 源码猜**：`_build()` 里写的是 `ReadImage(format="RGB")`，
        # 但配置里的 `DecodeImage: img_mode: BGR` 会把它整个换掉——PP-OCRv6 det 实际是
        # **BGR 进、alpha/beta 直接按通道下标用**（那组常数虽然长得像 ImageNet 的 RGB 均值，
        # 但模型就是这么训的）。我按源码前几行假设了换序，产物立刻对不上。
        if spec.img_mode == "RGB":
            self._swap = True          # 需要 BGR->RGB：换序折进 LUT 下标
            order = [2, 1, 0]
        elif spec.img_mode == "BGR":
            self._swap = False
            order = [0, 1, 2]
        else:
            raise ValueError(f"快路径不支持 Read format={spec.img_mode}")

        lut = np.empty((256, 1, 3), np.float32)
        v = np.arange(256, dtype=np.float32)
        for c in range(3):
            k = order[c]               # 输入第 c 通道在模型眼里是第 k 个通道
            lut[:, 0, c] = v * np.float32(spec.alpha[k]) + np.float32(spec.beta[k])
        self._lut = lut
        self._bufs: dict[str, np.ndarray] = {}

    @classmethod
    def from_onnx(cls, onnx_path: str, runner, default_yml_ok: bool) -> "FastDet":
        """配置读 ONNX 旁边的 `inference.yml`（官方仓库自带；模型名也从它来，`spec.model_name`），后处理用 `detpost.DBPostProcess`。
        旁边没有 yml 时**只有** `default_yml_ok`（默认模型的官方 ONNX / 以前自转的 `ocr_args.OLD_DET_ONNX`，
        `ocr_args.EQUIV_VALUES` 把两者当同一类）按默认模型的官方 yml 取；自带的别的 ONNX 缺 yml 就报错——
        按默认模型的阈值和缩放跑别的权重，框会悄悄变（审计 2026-09-25）。"""
        yml = Path(onnx_path).with_name("inference.yml")
        if not yml.is_file():
            if not default_yml_ok:
                raise ValueError(f"{onnx_path} 旁边没有 inference.yml：自带的 det ONNX 要连同它的 inference.yml 放在同一个目录")
            from flowocr import models
            yml = Path(models.path("det")).with_name("inference.yml")
            print(f"[det] {onnx_path} 旁边没有 inference.yml，前后处理配置按默认模型的官方 yml：{yml}", flush=True)
        spec = detpost.DetSpec.from_yml(yml)
        return cls(spec, runner, detpost.DBPostProcess(spec))

    # -- 前处理 ---------------------------------------------------------------

    def _resize_shape(self, h: int, w: int) -> tuple[int, int, float, float]:
        """照抄 processors.py 的 resize_image_type0，**包括那两处 int() 截断**。"""
        lsl, lt = self.limit_side_len, self.limit_type
        if lt == "max":
            ratio = float(lsl) / (h if h > w else w) if max(h, w) > lsl else 1.0
        elif lt == "min":
            ratio = float(lsl) / (h if h < w else w) if min(h, w) < lsl else 1.0
        elif lt == "resize_long":
            ratio = float(lsl) / max(h, w)
        else:
            raise ValueError(f"not support limit type {lt}")
        rh, rw = int(h * ratio), int(w * ratio)
        if max(rh, rw) > self.max_side_limit:
            r2 = float(self.max_side_limit) / max(rh, rw)
            rh, rw = int(rh * r2), int(rw * r2)
        rh = max(int(round(rh / 32) * 32), 32)
        rw = max(int(round(rw / 32) * 32), 32)
        return rh, rw, rh / float(h), rw / float(w)

    def _target_hw(self, h: int, w: int) -> tuple[int, int]:
        """这帧会被缩到多大（含 PaddleX 那条"太小先补到 32"的规矩）。"""
        if h + w < 64:
            h, w = max(32, h), max(32, w)
        return self._resize_shape(h, w)[:2]

    def preprocess(self, img: np.ndarray, *, copy: bool = False, dst: np.ndarray | None = None):
        """⚠ 返回的 blob 默认是**复用的缓冲区**，下一次调用就会被覆盖。

        要留着（比如攒一批做对照）就传 `copy=True`。复用不是微优化：
        每帧新分配 6.3 MB 再释放，实测让端到端多花约 4.5 ms/帧——
        新页是冷的，`copy_from_cpu` 读它要吃缺页。
        给了 `dst`（形状 `(1, 3, rh, rw)` 的 float32 视图）就直接写进它——批处理用，省一次拷贝。
        """
        if img.ndim != 3 or img.shape[2] != 3 or img.dtype != np.uint8:
            # LUT 是按 uint8 的 256 个取值建的，换了 dtype 就不是恒等变换了；
            # 通道数不对 `cv2.LUT` 会用同一张表去套，静默算出别的东西。
            raise ValueError(f"快路径只吃 3 通道 uint8，拿到 {img.shape} {img.dtype}")
        src_h, src_w = img.shape[:2]
        if src_h + src_w < 64:
            # PaddleX 的 `resize()` 在这里先补零到至少 32×32，再算比例，
            # 而返回的 shape 向量记的仍是**原始**尺寸。真素材碰不到，但换形状对账会咬。
            pad = np.zeros((max(32, src_h), max(32, src_w), img.shape[2]), img.dtype)
            pad[:src_h, :src_w] = img
            img = pad
        h, w = img.shape[:2]
        rh, rw, ratio_h, ratio_w = self._resize_shape(h, w)
        if (rh, rw) != (h, w):
            img = cv2.resize(img, (rw, rh), dst=self._buf("rs", (rh, rw, 3), np.uint8))
            ratio_h, ratio_w = rh / float(h), rw / float(w)
        else:
            ratio_h = ratio_w = 1.0
        # uint8 HWC -> 归一化的 float32 HWC，一趟 C++（BGR->RGB 若需要则已折进 LUT 系数）
        normed = cv2.LUT(img, self._lut, dst=self._buf("norm", (rh, rw, 3), np.float32))
        chw = normed.transpose(2, 0, 1)
        if self._swap:                             # LUT 只改了系数，通道位置还要真的换
            chw = chw[::-1]
        if dst is not None:
            if dst.shape != (1, 3, rh, rw) or dst.dtype != np.float32:
                raise ValueError(f"dst 形状不对：要 {(1, 3, rh, rw)} float32，给的 {dst.shape} {dst.dtype}")
            blob = dst
        else:
            blob = self._buf("blob", (1, 3, rh, rw), np.float32)
        blob[0] = chw                              # 写进常驻缓冲，不新分配
        return (blob.copy() if copy else blob), np.array([src_h, src_w, ratio_h, ratio_w])

    def _buf(self, key: str, shape: tuple, dtype) -> np.ndarray:
        b = self._bufs.get(key)
        if b is None or b.shape != shape or b.dtype != dtype:
            b = np.empty(shape, dtype)
            self._bufs[key] = b
        return b

    # -- 全流程 ---------------------------------------------------------------

    def predict(self, img: np.ndarray):
        t = time.perf_counter()
        blob, shape = self.preprocess(img)
        t = _tl.span("det_pre", t)
        preds = self.runner([blob])
        t = _tl.span("det_infer", t)
        polys, scores = self.post_op(
            preds, [shape],
            thresh=self.thresh,
            box_thresh=self.box_thresh,
            unclip_ratio=self.unclip_ratio,
        )
        _tl.span("det_post", t)
        return polys[0], scores[0]

    def predict_batch(self, imgs: list[np.ndarray]) -> list[tuple]:
        """几帧一次送进模型（`run_ocr2 --det-batch`）。返回每帧的 `(polys, scores)`，顺序同输入。

        **不逐字节等于逐帧**：cuDNN 按批大小可能换卷积算法，f5 上 48 帧里 2 帧 poly 有细微差
        （一次性探针 `probe_det_batch.py`）。尺寸不一的帧拼不了批，退回逐帧。
        """
        return self.post_batch(self.infer_batch(imgs))

    def infer_batch(self, imgs: list[np.ndarray]) -> tuple:
        """`predict_batch` 的前半：前处理 + 推理，返回一个不透明的 state 交给 `post_batch`（2026-09-25，`run_ocr2 --det-lookahead`：
        辅助线程跑这一半，和上一批的后处理重叠——det 线程原来把前处理 1.5 s、推理 10.9 s、后处理 3.7 s 串在一起）。
        state 里只有 runner 返回的新数组和 shape 向量：前处理的常驻缓冲区下一次就被覆盖，不能留在 state 里。"""
        if len(imgs) == 1 or len({im.shape for im in imgs}) != 1:
            return ("done", [self.predict(im) for im in imgs])
        t = time.perf_counter()
        rh, rw = self._target_hw(*imgs[0].shape[:2])
        batch = self._buf(f"batch{len(imgs)}", (len(imgs), 3, rh, rw), np.float32)
        shapes = [self.preprocess(im, dst=batch[i:i + 1])[1] for i, im in enumerate(imgs)]
        t = _tl.span("det_pre", t)
        preds = self.runner([batch])
        _tl.span("det_infer", t)
        return ("preds", preds, shapes)

    def post_batch(self, state: tuple) -> list[tuple]:
        """`predict_batch` 的后半：DB 后处理。和 `infer_batch` 合起来就是原来的 `predict_batch`，计算一步不差。"""
        if state[0] == "done":
            return state[1]
        _, preds, shapes = state
        t = time.perf_counter()
        polys, scores = self.post_op(
            preds, shapes,
            thresh=self.thresh,
            box_thresh=self.box_thresh,
            unclip_ratio=self.unclip_ratio,
        )
        _tl.span("det_post", t)
        return list(zip(polys, scores))
