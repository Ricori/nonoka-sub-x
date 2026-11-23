"""rec 的 **ONNX Runtime 后端**（`run_ocr2` 的 rec），接口照抄 PaddleX 的 `TextRecognition`（当初是为了和 Paddle rec 换着用）。

为什么长成这样（inference-runtime 计划第 2 条）：
* **为什么是 ORT**：同一份前处理张量上，ORT fp16 + argmax 融合比 Paddle 的 runner 快 **48%~63%**
  （一次性探针 `probe_ort_rec.py`）。大头不是算力，是 Paddle 把 `[batch, T, 18710]` 的 logits 整个拷回主机
  （batch 32 = 191 MB），而管线只要 argmax + 那一位的概率。
* **为什么走另一个进程**：当初 onnxruntime 和 Paddle 各带一套 CUDA 库、会抢 cuDNN / cuBLAS，所以隔离成独立的服务进程
  （2026-10 起运行时没有 Paddle 了，服务进程照旧：它同时服务 det 和多个 rec 在途）。**票价量过：一次往返 0.12~0.33 ms = 2%~3%**
  （一次性探针 `probe_ort_rec_ipc.py`），所以这条路划算。
* **为什么接口照抄 `TextRecognition`**：`run_ocr2` 里 rec 只有两个调用点
  （`recpack.predict_bucketed` 的批、`rec_one` 的单框回补），都只用 `predict(crops, batch_size=…)`；
  照抄接口就两处都不用改，分桶那份代码也照旧。

**正确性**：前后处理走 `flowocr.extract.recdecode`（那份对着 PaddleX 逐条验过：74/74 文本逐字相同、分数差 0）；
这条后端自己也验过——同样 74/74 逐字相同，**分数最大差 0.09、中位 0.0001**（fp16 的量化差）。
⚠ 所以它**不是逐字节相同的改动**：分数会动，`conf` 在下游有门（≥0.5 那类），
换它要走 A/B 的规矩（`arm_vs_truth` 五栏 + 命中集合差）。
"""
from __future__ import annotations

import time
from pathlib import Path


from flowocr import paths
from flowocr.extract import ortclient, recdecode, recprep

MAX_BATCH = 16
"""共享内存按这个批上限开（和 `--rec-bucket` 的默认值一致；2026-09-21 随它从 32 降到 16）。超了就自动分几次送。"""

GRID = 320
"""**张量宽向上取整到它的倍数**——和 worker 的"每形状一个 session"是**配套的两半**。

ORT 的 CUDA EP **每次形状变化就重新规划**：同宽连打 4.6 ms，8 个宽轮转 **81 ms**（17 倍，
预热也没用；一次性探针 `probe_ort_rec_ipc.py`）。而管线里张量宽在 320/545/792/960/962… 之间抖，
第一版没收形状，5 分钟片段上 rec 段 5.9 s → **29.0 s**（比 Paddle 慢 5 倍）。
收到 320 的倍数之后只剩 8~10 种形状（`probe_rec_decode.py --grid 320` 验过**零填充不改文本**，
444/444 逐字相同）；再配上每形状一个 session，8 个宽轮转回到 5.2 ms。
**两个一起才有意义**：只收形状不分 session 还是 27.8 s，只分 session 不收形状会把显存吃光。

⚠ **这个数不能再调大**（2026-09-21，一次性探针 `probe_rec_pad.py`，next-steps）：右侧多补零是有剂量反应的伤害——
多补 ≤320 列在噪声里（err 赢/输 10/14），+480 列 15/32、+640 列 16/40、补到 3200 是 15/117。320 正好贴在安全区边上。
要减形状数去砍**批维**梯子（批维不影响读数：547 条差 0~1 条），别动宽度。"""


BATCH_AREA = 20480
"""一批的 **批 × 张量宽** 封顶（2026-10-05，resource 计划，defaults.md 1.42）：宽批的显存几乎全是识别头的输出 `[批, 宽/8, 18710]`
那几份 fp32 张量，16 × 2560 这一个形状就要多 2.9 GB 显存（探针）（宽 1280 以下的 16 批不到 100 MB）。按 20480 封顶 = 宽 ≤ 1280 照旧 16 批、
1600–2560 最多 8、2880 / 3200 最多 4（都是 2 的幂，补齐不越界）。quick 六段一批都碰不到；zzz 整片里约 20–24 批会被拆开
（按 `_meta.rec_pool.shape_hist` 里批大小超过上限的数，五份整片 obs 20–24，异步池每趟拼法不同；同一张量宽、只是批小了）。"""


def batch_cap(tw: int, max_batch: int = MAX_BATCH, area: int = BATCH_AREA) -> int:
    """张量宽 `tw` 这一档一批最多几个：`max_batch` 和 `area // tw` 取小，再向下取到 2 的幂——默认的批梯子（2 的幂）补齐后不越过它；
    给了非默认的 `--ort-batch-ladder` 时补齐可能越过（梯子档位不是 2 的幂）。"""
    k = max(1, min(max_batch, area // max(1, tw)))
    return 1 << (k.bit_length() - 1)


def warm_shapes(max_batch: int = MAX_BATCH, grid: int = GRID, max_w: int = 2560) -> list[tuple[int, int]]:
    """ORT 服务起动后后台预热的 `(批, 宽)` 表（`ort_server --warm`，2026-09-24）：宽是分桶网格（`grid` 的倍数）、批是 2 的幂（`_pad_batch`），
    按 quick 六段实测的 `shape_hist` 取常见的——窄的（≤ 2 格）批能到 `max_batch`，3 格到 4，更宽的只有 1 / 2。
    **从这里生成、不写死在服务里**：网格 / 批梯子一改，预热表跟着变。"""
    out = []
    w = grid
    while w <= max_w:
        cap = min(max_batch, max_batch if w <= 2 * grid else (4 if w <= 3 * grid else 2))
        b = 1
        while b <= cap:
            out.append((b, w))
            b *= 2
        w += grid
    return out


class OrtRec:
    """和 `TextRecognition` 同接口的 rec 后端：只需要 `predict(crops, batch_size=…)`。

    给了 `client` 就复用外面那个 worker（det / rec 共用一个进程），否则自己起一个。
    """

    def __init__(self, model: str = "", max_batch: int = MAX_BATCH, grid: int = GRID,
                 client: "ortclient.OrtClient | None" = None, pre: str = "server") -> None:
        from flowocr import models
        model = paths.resolve_model(model) if model else models.path("rec")
        # 字表跟着模型走：官方 ONNX 仓库自带 `inference.yml`（默认模型是官方 ONNX，`flowocr.models`；不给 `model` 就 `models.path("rec")`，缺了当场取）。
        # 旁边没有就报错（2026-10 去掉 Paddle 之前是退回 PaddleX 缓存里的模型目录取字表）
        here = Path(model).parent
        if not (here / "inference.yml").is_file():
            raise FileNotFoundError(f"{model} 旁边没有 inference.yml（rec 的字表在里面）：自带的 rec ONNX 要连同它的 inference.yml 放在同一个目录")
        self.labels = recdecode.load_labels(here)
        self.model = model
        self.max_batch = max_batch
        self.grid = grid
        self.own = client is None
        self.cli = client or ortclient.OrtClient({"rec": model})
        self.pre_on_server = pre == "server" and getattr(self.cli, "pre", False)
        """前处理在 ORT 服务进程里做（`--rec-pre server`，默认；服务端报了 `pre` 才行）：送原样的 uint8 裁剪，
        不在管线进程里抢 GIL（decode-buffer）。张量和本地做逐位相同（同一份 `recprep.batch`）。"""
        self.t = {"pre": 0.0, "decode": 0.0}
        print(f"[rec] ONNX Runtime 后端：{Path(model).name}、标签 {len(self.labels)} 类、"
              f"张量宽收到 {grid} 的倍数", flush=True)
        self.ladder: tuple[int, ...] = ()
        """批维补齐的梯子（空 = 2 的幂）。见 `_pad_batch`。"""

    def shape_w(self, tw: int) -> int:
        """一个张量宽最终落在哪个**形状宽**上。`recpack.predict_bucketed` 见到这个方法就按它分桶——
        **(batch, width) 只由一处决定**（next-steps）：以前上游按精确宽分组、这里再按网格宽重分，
        330 和 340 两个框被拆成两次 `predict`、各跑一个 `(1, 640)`，而它们本来是同一个形状
        （回放三段切片：`session.run` 次数 −3% ~ −13%，一次性探针 `rec_shape_replay.py`）。"""
        return recdecode.grid_w(tw, self.grid)

    @staticmethod
    def _pad_batch(n: int, ladder: tuple[int, ...] = ()) -> int:
        """批维向上补齐：形状的**每一维**变化都会让 ORT 重规划，批维也算。

        默认补到 2 的幂（1/2/4/8/16，被 `max_batch` 封顶）。给了 `ladder` 就只补到那几档——
        **梯子越粗、形状越少**：组批之下 6 个批值把形状数从 12 撑到 28、
        池子只有 12 于是反复淘汰重建。代价是补零的浪费。
        """
        for k in ladder:
            if k >= n:
                return k
        if ladder:
            return max(ladder[-1], n)
        k = 1
        while k < n:
            k *= 2
        return k

    def predict(self, crops: list, batch_size: int = 1) -> list[dict]:
        """和 PaddleX 同形状的返回：`[{"rec_text": …, "rec_score": …}, …]`，顺序对齐输入。

        调用方（`recpack.predict_bucketed`）给进来的通常已经是同宽一批；这里**仍然自己按张量宽分组**，
        因为单框回补那条路不分桶，而一次 `session.run` 只能吃一个形状。
        """
        if not crops:
            return []
        t0 = time.perf_counter()
        widths = [recprep.grid_w(recprep.rec_target_w(*c.shape[:2]), self.grid) for c in crops]
        self.t["pre"] += time.perf_counter() - t0
        out: list[dict | None] = [None] * len(crops)
        groups: dict[int, list[int]] = {}
        for i, tw in enumerate(widths):
            groups.setdefault(tw, []).append(i)
        for tw, idx in groups.items():
            cap = batch_cap(tw, self.max_batch)           # 宽批按 批 × 宽 封顶（BATCH_AREA）：显存
            for s in range(0, len(idx), cap):
                part = idx[s:s + cap]
                k = self._pad_batch(len(part), self.ladder)      # 补零行，回来只取前几条
                if self.pre_on_server:
                    cls, prob = self.cli.run_crops("rec", [crops[i] for i in part], self.grid, k)
                else:
                    t0 = time.perf_counter()
                    x = recprep.batch([crops[i] for i in part], self.grid, k)
                    self.t["pre"] += time.perf_counter() - t0
                    cls, prob = self.cli.run("rec", x)
                t1 = time.perf_counter()
                dec = recdecode.ctc_greedy(cls, prob, self.labels)[:len(part)]
                self.t["decode"] += time.perf_counter() - t1
                for i, (txt, score) in zip(part, dec):
                    out[i] = {"rec_text": txt, "rec_score": score}
        return out

    def close(self) -> None:
        if self.t["pre"] or self.t["decode"]:
            print(f"[rec] ORT 后端：前处理 {self.t['pre']:.1f}s、解码 {self.t['decode']:.1f}s", flush=True)
            self.t["pre"] = self.t["decode"] = 0.0
        if self.own:
            self.cli.report("-rec")
            self.cli.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
