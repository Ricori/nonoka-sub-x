"""det 的**模型配置**与 **DB 后处理**，不依赖 Paddle（2026-09-25）。

为什么（decode-buffer 计划的审计余项"检测的模型配置、前后处理、执行后端解耦"）：det 推理换到 ORT 之后，管线进程原来仍要
建一份完整的 Paddle det，只为借它两样东西——前处理参数（缩放上限、归一化系数）和后处理对象（`DBPostProcess`）。
这里把那两样自己拿一份（2026-10 起运行时干脆没有 Paddle 了）：

* `DetSpec`：一组前后处理参数。`from_yml` 从 ONNX 仓库自带的 `inference.yml` 读（PaddleX 就是从同一份 yml 建的，
  缩放默认按模型名同 PaddleX 的 `_get_text_det_resize_defaults`）。读法照 PaddleX 的 `_build`：守卫拿冻结的 PaddleX 结果
  （几种 yml 写法、全字段）做回归。
* `DBPostProcess`：照抄 PaddleX `paddlex/inference/models/text_detection/processors.py` 的同名类（Apache-2.0，
  `LICENSES/Apache-2.0.txt`），只保留管线用到的 `box_type=quad` + `score_mode=fast` + 不膨胀，算术逐行相同——
  照抄时和 PaddleX 的原实现**逐字节相同**（随机概率图对拍；真帧上另有探针验过 det 的多边形逐个相同）；守卫现在对着
  冻结的 PaddleX 输出（输入一起存着）做回归。

这个模块只依赖 numpy / cv2 / pyclipper / pyyaml（都在 `pyproject.toml` 的主依赖里）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import pyclipper

MAX_LIMIT_MODELS = frozenset({
    "PP-OCRv5_server_det", "PP-OCRv5_mobile_det", "PP-OCRv4_server_det", "PP-OCRv4_mobile_det",
    "PP-OCRv3_server_det", "PP-OCRv3_mobile_det", "PP-OCRv6_medium_det", "PP-OCRv6_small_det", "PP-OCRv6_tiny_det",
})
"""PaddleX 里缩放默认是"长边 ≤ 960"的那些模型（`predictor._TEXT_DET_MAX_LIMIT_MODELS`）；其余默认"短边 ≥ 736"。"""

KNOWN_OPS = frozenset({"DecodeImage", "DetLabelEncode", "DetResizeForTest", "NormalizeImage", "ToCHWImage", "KeepKeys"})
"""PaddleX det 的 `_FUNC_MAP` 认的前处理 op（`DetLabelEncode` / `KeepKeys` 在它那里是空操作）。"""


def _parse_scale(v) -> float:
    """`NormalizeImage.scale`：yml 里常写成字符串 `1./255.`。PaddleX 用 `eval`；这里只认数或 `a/b`，
    按 `float(a) / float(b)` 算——和 `eval` 同一次浮点除法，结果逐位相同，不执行模型目录里的任意字符串。"""
    if not isinstance(v, str):
        return float(v)
    a, sep, b = v.partition("/")
    try:
        return float(a) / float(b) if sep else float(a)
    except ValueError:
        raise ValueError(f"NormalizeImage.scale 只认数或 a/b：{v!r}") from None


@dataclass(frozen=True)
class DetSpec:
    """det 的前后处理参数——`FastDet` 只认这一份，不认 predictor。"""

    model_name: str
    limit_side_len: int
    limit_type: str
    max_side_limit: int
    img_mode: str
    """`DecodeImage.img_mode`：BGR 就不换通道；RGB 才要 BGR->RGB。"""
    alpha: tuple[float, float, float]
    beta: tuple[float, float, float]
    """归一化 `v * alpha[c] + beta[c]`（PaddleX `NormalizeImage`：alpha = scale / std、beta = −mean / std，Python 双精度）。"""
    thresh: float
    box_thresh: float
    unclip_ratio: float
    max_candidates: int

    @classmethod
    def from_yml(cls, path: str | Path) -> "DetSpec":
        """从模型目录的 `inference.yml` 建（官方 ONNX 仓库自带；和 PaddleX 模型目录里的是同一份）。
        **照 PaddleX `TextDetRunnerPredictor._build` 的读法**，包括它不看的键（守卫拿冻结的 PaddleX `_build` 结果对几种 yml 写法做回归）：
        * 读图默认 RGB，`DecodeImage` 在才按它的 `img_mode`；
        * `DetResizeForTest` 里的 `limit_side_len` / `limit_type` / `max_side_limit` PaddleX 都不看（前两个被 `build_resize` 的具名参数吃掉、
          换成按模型名的默认；最后一个调用时被 predictor 的 4000 盖掉），只有 `resize_long` 改默认边长；`image_shape` 是另一种缩放，快路径没写；
        * `NormalizeImage` 的 `order` 缺省是 `""`（按 hwc 走），只有 `chw` 快路径没写；
        * 认不得的前处理 op PaddleX 会 KeyError，这里同样拒绝。"""
        import yaml
        cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        name = cfg["Global"]["model_name"]
        ops: dict[str, dict] = {}
        for op in cfg["PreProcess"]["transform_ops"]:
            key = list(op)[0]
            if key not in KNOWN_OPS:
                raise ValueError(f"{path}：前处理 op {key} PaddleX 的 det 不认")
            ops[key] = op[key] or {}
        if "NormalizeImage" not in ops or "DetResizeForTest" not in ops:
            raise ValueError(f"{path}：不是 DB det 的前处理（要 DetResizeForTest + NormalizeImage）")
        norm = ops["NormalizeImage"]
        if norm.get("order", "") == "chw":
            raise ValueError("这个模型的 Normalize 是 chw 序，快路径只写了 hwc 序")
        scale = _parse_scale(norm.get("scale", 1 / 255))
        mean = norm.get("mean") or [0.485, 0.456, 0.406]
        std = norm.get("std") or [0.229, 0.224, 0.225]
        rz = ops["DetResizeForTest"]
        if "image_shape" in rz:
            raise ValueError(f"{path}：DetResizeForTest 给了 image_shape（定形缩放），快路径只写了按边长缩放")
        side = rz.get("resize_long", 960 if name in MAX_LIMIT_MODELS else 736)
        kind = "max" if name in MAX_LIMIT_MODELS else "min"
        if "DecodeImage" in ops:
            if "img_mode" not in ops["DecodeImage"]:
                raise ValueError(f"{path}：DecodeImage 没写 img_mode（PaddleX 在这里直接报错）")
            img_mode = str(ops["DecodeImage"]["img_mode"])
        else:
            img_mode = "RGB"
        post = cfg["PostProcess"]
        if post.get("name") != "DBPostProcess":
            raise ValueError(f"{path}：后处理是 {post.get('name')}，只支持 DBPostProcess")
        if post.get("box_type", "quad") != "quad" or post.get("score_mode", "fast") != "fast" or post.get("use_dilation", False):
            raise ValueError(f"{path}：只支持 box_type=quad、score_mode=fast、不膨胀的 DBPostProcess")
        return cls(model_name=name, limit_side_len=int(side), limit_type=kind, max_side_limit=4000, img_mode=img_mode,
                   alpha=tuple(scale / std[i] for i in range(3)), beta=tuple(-mean[i] / std[i] for i in range(3)),
                   thresh=float(post.get("thresh", 0.3)), box_thresh=float(post.get("box_thresh", 0.6)),
                   unclip_ratio=float(post.get("unclip_ratio", 2.0)), max_candidates=int(post.get("max_candidates", 1000)))


class DBPostProcess:
    """DB 的后处理（概率图 -> 四点框 + 分数），照抄 PaddleX 的 `DBPostProcess`（quad / fast / 不膨胀那条路）。

    调用形状和 PaddleX 一样：`post(preds, img_shapes, thresh=…, box_thresh=…, unclip_ratio=…)`，
    `preds[0]` 是 `[N, 1, H, W]` 的概率图，`img_shapes[i] = (src_h, src_w, ratio_h, ratio_w)`；
    返回 `(boxes 列表, scores 列表)`，每帧的 boxes 是 `int16` 的 `[k, 4, 2]`。
    ⚠ 算术**一处都别"顺手改"**：`round` 是 Python 的银行家舍入、`box_score_fast` 的 floor / ceil 夹在 `w-1` 上、
    `min_size` 的两道门一道 `< 3` 一道 `< 5`——每一处都决定框的像素坐标。
    """

    MIN_SIZE = 3

    def __init__(self, spec: DetSpec) -> None:
        self.thresh, self.box_thresh, self.unclip_ratio = spec.thresh, spec.box_thresh, spec.unclip_ratio
        self.max_candidates = spec.max_candidates

    def __call__(self, preds, img_shapes, thresh: float | None = None, box_thresh: float | None = None,
                 unclip_ratio: float | None = None):
        boxes, scores = [], []
        for pred, img_shape in zip(preds[0], img_shapes):
            b, s = self.process(pred, img_shape, thresh or self.thresh, box_thresh or self.box_thresh,
                                unclip_ratio or self.unclip_ratio)
            boxes.append(b)
            scores.append(s)
        return boxes, scores

    def process(self, pred, img_shape, thresh: float, box_thresh: float, unclip_ratio: float):
        pred = pred[0, :, :]
        src_h, src_w, _ratio_h, _ratio_w = img_shape
        return self.boxes_from_bitmap(pred, pred > thresh, src_w, src_h, box_thresh, unclip_ratio)

    def boxes_from_bitmap(self, pred, bitmap, dest_width, dest_height, box_thresh: float, unclip_ratio: float):
        height, width = bitmap.shape
        width_scale = dest_width / width
        height_scale = dest_height / height
        # PaddleX 写的是 `(bitmap * 255).astype(np.uint8)`——bool 先升成 int64 再转回，一帧两次 50 万元素的临时数组。
        # `findContours` 只看非零，bool 的 0 / 1 按字节看就是 uint8 的 0 / 1，轮廓逐点相同（随机图 + 真帧 120 帧对拍过）
        contours, _ = cv2.findContours(bitmap.view(np.uint8), cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        boxes, scores = [], []
        for index in range(min(len(contours), self.max_candidates)):
            contour = contours[index]
            points, sside = self.get_mini_boxes(contour)
            if sside < self.MIN_SIZE:
                continue
            points = np.array(points)
            score = self.box_score_fast(pred, points.reshape(-1, 2))
            if box_thresh > score:
                continue
            box = self.unclip(points, unclip_ratio).reshape(-1, 1, 2)
            box, sside = self.get_mini_boxes(box)
            if sside < self.MIN_SIZE + 2:
                continue
            box = np.array(box)
            for i in range(box.shape[0]):
                box[i, 0] = max(0, min(round(box[i, 0] * width_scale), dest_width))
                box[i, 1] = max(0, min(round(box[i, 1] * height_scale), dest_height))
            boxes.append(box.astype(np.int16))
            scores.append(score)
        return np.array(boxes, dtype=np.int16), scores

    @staticmethod
    def unclip(box, unclip_ratio: float):
        area = cv2.contourArea(box)
        length = cv2.arcLength(box, True)
        distance = area * unclip_ratio / length
        offset = pyclipper.PyclipperOffset()
        offset.AddPath(box, pyclipper.JT_ROUND, pyclipper.ET_CLOSEDPOLYGON)
        try:
            expanded = np.array(offset.Execute(distance))
        except ValueError:
            expanded = np.array(offset.Execute(distance)[0])
        return expanded

    @staticmethod
    def get_mini_boxes(contour):
        bounding_box = cv2.minAreaRect(contour)
        points = sorted(list(cv2.boxPoints(bounding_box)), key=lambda x: x[0])
        if points[1][1] > points[0][1]:
            index_1, index_4 = 0, 1
        else:
            index_1, index_4 = 1, 0
        if points[3][1] > points[2][1]:
            index_2, index_3 = 2, 3
        else:
            index_2, index_3 = 3, 2
        box = [points[index_1], points[index_2], points[index_3], points[index_4]]
        return box, min(bounding_box[1])

    @staticmethod
    def box_score_fast(bitmap, _box):
        h, w = bitmap.shape[:2]
        box = _box.copy()
        xmin = max(0, min(math.floor(box[:, 0].min()), w - 1))
        xmax = max(0, min(math.ceil(box[:, 0].max()), w - 1))
        ymin = max(0, min(math.floor(box[:, 1].min()), h - 1))
        ymax = max(0, min(math.ceil(box[:, 1].max()), h - 1))
        mask = np.zeros((ymax - ymin + 1, xmax - xmin + 1), dtype=np.uint8)
        box[:, 0] = box[:, 0] - xmin
        box[:, 1] = box[:, 1] - ymin
        cv2.fillPoly(mask, box.reshape(1, -1, 2).astype(np.int32), 1)
        return cv2.mean(bitmap[ymin: ymax + 1, xmin: xmax + 1], mask)[0]
