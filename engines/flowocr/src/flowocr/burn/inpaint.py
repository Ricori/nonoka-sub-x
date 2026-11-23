"""LaMa 修补器：把底板盖住的原文从画面里抹掉（inpainting 路线）。

* 分组切窗：竖向挨着（间隙 ≤ `GROUP_GAP`）、横向有重叠的底板并成一组，整帧缩小 `scale` 倍后每组切一串扁窗（窗高 × 宽见档位），
  补完放大回原尺寸，只贴回底板矩形里的像素。尺寸不能被整除时先镜像补到整除；缩小后不足一个窗的那一边镜像补齐，掩码跟着镜像。
* 时间上复用：同一组（外接矩形不变）的上下文（外接矩形外扩 `CTX_PX` 的一圈、掩码外的像素，缩到 1/8 后的 Lab）和上次重补时比，
  均差超过 `diff_thr` 或隔了 `max_age_s` 秒才重补；沿用时按上下文一圈的均色差给补丁整体校色。重补时从旧补丁逐帧渐变到新补丁，`fade_s` 秒走完。
  时长类参数按调用的帧率换成帧数、四舍五入。有状态：要按帧序调用。
* 打字机：调用时给要抹的部分（这一刻底板画出来的那段），只贴它；分组、推理、沿用仍按整块底板，还没有字露出来的组不补。
* 非 1080p：组间隙、上下文宽、窗、缩小倍数都是在 1080p 上定的，别的高度先把整帧重采样到等效 1080p 再修补，补完重采样回来、只贴回要抹的部分；
  1080p 上这一步不做。
* 推理：TensorRT fp16（两档的图都把激活整体缩小了 64 倍，免得 fp16 溢出出 NaN，见 `lamagraph.scaled`），出了 NaN 的那一窗用 fp32 重算，
  fp32 会话用到才建；没装 TensorRT 走 CUDA EP fp32，没有可用的 GPU 走 CPU EP fp32。

    inp = Inpainter(PRESETS["default"], session(PRESETS["default"]), fps=60)
    out = inp(img_bgr, rects, shown)     # rects / shown：整数 (x0, y0, x1, y1)，视频像素
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from flowocr.burn import lamagraph

OVERLAP = 128                               # 横向相邻窗的重叠
GROUP_GAP = 32                              # 竖向间隙不超过这么多 px 的底板并成一组
CTX_MIN = 128                               # 一组在缩小后的坐标里最高为窗高减这么多，上下各留至少 64 px 上下文
CTX_PX = 24                                 # 上下文一圈的宽度（px）
REF_H = 1080                                # 上面这些常量和档位按这个画面高定
CUDA_MEM_LIMIT = 2 << 30                    # 每个会话 CUDA EP 显存池的上限（超了当场报错，不去挤别人的显存）；TRT 引擎自己的显存另算


@dataclass(frozen=True)
class Cfg:
    scale: int                              # 整帧缩小这么多倍再切窗
    diff_thr: float                         # 上下文 Lab 均差（ΔE）超过它就重补
    max_age_s: float                        # 最多沿用这么多秒
    fade_s: float                           # 重补时从旧补丁渐变到新补丁用的秒数
    win: tuple[int, int]                    # 推理窗的高、宽（缩小后的坐标）
    act: int                                # 图里激活整体缩小的倍数（`lamagraph.scaled`）


PRESETS = {
    # 扁窗：一个窗装下原型 7 段素材里最高的一组（298 px）和最宽的三行（原尺寸约 1218 px），上下各留至少 64 px（缩小后）上下文。
    # 重补的阈值：owner 2026-10-30 看了烧片，在重补率和画面跳动之间定的。
    "default": Cfg(scale=2, diff_thr=2, max_age_s=0.33, fade_s=0.1, win=(320, 768), act=64),   # 速度档
    "hq": Cfg(scale=1, diff_thr=1, max_age_s=0.2, fade_s=0.1, win=(448, 1344), act=64),       # 高质量档：原尺寸
}


# ---- 会话 ----

def backend_of(want: str = "auto") -> tuple[str, str]:
    """定推理后端：返回 (trt | cuda | cpu, 为什么不是更快的那个；是最快的就是空串)。
    auto：装了 TensorRT 且和这份 onnxruntime 对得上就 trt，否则有可用的 GPU 就 cuda，再不然 cpu。指定了就照办（用不了建会话时报错）。"""
    import onnxruntime as ort
    if want != "auto":
        return want, ""
    from flowocr import trt as T
    from flowocr.extract import ocr_args
    if "CUDAExecutionProvider" not in ort.get_available_providers():
        return "cpu", f"这份 onnxruntime（{ort.__version__}）没有 CUDA EP"
    if ocr_args.visible_gpu_count() == 0:
        return "cpu", "没有可用的 CUDA 设备"
    if not all(T.have(m) for m in T.COMMON):
        old = T.stale_install()
        return "cuda", (f"装的是 TensorRT {old}，这一版要 {T.TRT_VERSION}（先 flowocr-trt remove 再 install）" if old
                        else "没装 TensorRT（flowocr-trt install 之后推理约快 6 倍、整片约快一倍多）")
    return ("cuda", T.ort_compatible()) if T.ort_compatible() else ("trt", "")


def session(cfg: Cfg, backend: str = "trt", prec: str = "fp16"):
    """建推理会话。trt 的引擎缓存和阶段 1 的放在一起（`flowocr-trt remove` 一并删），缺了先建（每档约 1 分钟，只在第一次）。
    cuda / cpu 一律 fp32。"""
    import onnxruntime as ort
    model = Path(lamagraph.path(*cfg.win, cfg.act))
    ort.set_default_logger_severity(3)      # 不然建 TRT 会话时打一行 "No registered plugin EP device found" 的警告
    so = ort.SessionOptions()
    so.log_severity_level = 3
    if backend == "cpu":
        return ort.InferenceSession(str(model), so, providers=["CPUExecutionProvider"])
    ort.preload_dlls()                      # 不调它 CUDA EP 的 DLL 找不到，会静默退回 CPU
    cuda = {"gpu_mem_limit": str(CUDA_MEM_LIMIT)}
    if backend == "cuda":
        s = ort.InferenceSession(str(model), so, providers=[("CUDAExecutionProvider", cuda)])
        if s.get_providers()[0] != "CUDAExecutionProvider":
            raise RuntimeError(f"CUDA EP 没生效：{s.get_providers()}")
        return s
    from flowocr import trt as T
    from flowocr.extract import trt_engine as E
    E.setup_dlls(T.lib_dir())
    cap = E.compute_cap(0)
    log = lambda m: print(m, flush=True)
    prefix = E.build("lama", model, prec, T.engine_dir(), cap, T.TRT_VERSION, log=log)
    return E.make_session("lama", model, prec, prefix, T.engine_dir(), so=so, cuda=cuda)


# ---- 几何 ----

def windows(x0: int, x1: int, width: int, tw: int) -> list[tuple[int, int, int]]:
    """一个方向上的窗（窗长 tw，横竖通用）：(窗起点, 写回起点, 写回终点)；两头各留 32 px 还装得下就只用一个窗。
    竖向只有高过 `窗高 − 64` 的单块底板才切成几行窗（分组并完不超过 `窗高 − CTX_MIN`，碰不到）。"""
    pad, step = OVERLAP // 2, tw - OVERLAP
    if x1 - x0 <= tw - 64:
        return [(min(max(0, (x0 + x1) // 2 - tw // 2), width - tw), x0, x1)]
    starts = [x0 - pad]
    while starts[-1] + tw - pad < x1:
        starts.append(starts[-1] + step)
    return [(min(max(0, wx), width - tw), x0 if i == 0 else wx + pad, x1 if i == len(starts) - 1 else starts[i + 1] + pad)
            for i, wx in enumerate(starts)]


def groups(rects: list, max_h: int) -> list[tuple[int, int, int, int]]:
    """把底板并成组，返回每组的外接矩形：竖向间隙 ≤ GROUP_GAP、横向重叠的并起来，并完不超过 max_h 高。按顺序贪心地并。"""
    boxes = [list(r) for r in rects]
    merged = True
    while merged:
        merged = False
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                a, b = boxes[i], boxes[j]
                gap = max(a[1], b[1]) - min(a[3], b[3])
                u = [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]
                if gap <= GROUP_GAP and min(a[2], b[2]) > max(a[0], b[0]) and u[3] - u[1] <= max_h:
                    boxes[i] = u
                    del boxes[j]
                    merged = True
                    break
            if merged:
                break
    return [tuple(b) for b in boxes]


def ctx_sig(img: np.ndarray, box, mask: np.ndarray, keeps: dict) -> tuple[np.ndarray, np.ndarray]:
    """上下文签名：外接矩形外扩 CTX_PX 的范围缩到 1/8 的 Lab，和"掩码外"的格。
    keeps：同一套底板矩形下按组缓存"掩码外"的格（掩码只由矩形决定，矩形不变就不用重算）。"""
    h, w = mask.shape
    x0, y0, x1, y1 = max(0, box[0] - CTX_PX), max(0, box[1] - CTX_PX), min(w, box[2] + CTX_PX), min(h, box[3] + CTX_PX)
    sz = (max(1, (x1 - x0) // 8), max(1, (y1 - y0) // 8))
    lab = cv2.cvtColor(cv2.resize(img[y0:y1, x0:x1], sz, interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2LAB).astype(np.float32)
    lab[..., 0] *= 100 / 255
    lab[..., 1:] -= 128
    keep = keeps.get(box)
    if keep is None:
        keep = keeps[box] = cv2.resize((~mask[y0:y1, x0:x1]).astype(np.float32), sz, interpolation=cv2.INTER_AREA) > 0.99
    return lab, keep


def paste(dst: np.ndarray, src: np.ndarray, box, rects: list) -> None:
    """把 src（box 范围内的图）里落在 rects 里的像素贴到 dst 的 box 范围；按切片拷，不走布尔索引。"""
    x0, y0, x1, y1 = box
    for a0, b0, a1, b1 in rects:
        a0, b0, a1, b1 = max(a0, x0), max(b0, y0), min(a1, x1), min(b1, y1)
        if a1 > a0 and b1 > b0:
            dst[b0:b1, a0:a1] = src[b0 - y0:b1 - y0, a0 - x0:a1 - x0]


def shift_bgr(patch: np.ndarray, mean: np.ndarray, d: np.ndarray) -> np.ndarray:
    """补丁整体加一个 Lab 平移 d 的近似：在补丁均色 mean（BGR，0–1）处把 d 换成 BGR 偏移，每个像素加同一个偏移。
    逐像素转 Lab 再转回要 5 ms 一块（1080p 三行），这样不到 1 ms；偏移只在均色处准，离均色远的像素有偏差。"""
    lab = cv2.cvtColor(mean.reshape(1, 1, 3), cv2.COLOR_BGR2LAB)
    bgr = cv2.cvtColor(lab + d.astype(np.float32).reshape(1, 1, 3), cv2.COLOR_LAB2BGR)
    off = np.round((bgr.reshape(3) - mean) * 255).astype(np.int16)
    if not off.any():
        return patch
    return cv2.add(patch, (*off.tolist(), 0), dtype=cv2.CV_8U)


# ---- 修补器 ----

@dataclass
class Held:
    box: tuple
    patch: np.ndarray                       # 补好的外接矩形（BGR）
    sig: tuple
    mean: np.ndarray                        # 补丁的 BGR 均色（float32，0–1），沿用时校色的基点
    age: int = 0
    prev: np.ndarray | None = None          # 渐变：重补前最后显示的那块，从它渐变到新补丁
    fade_left: int = 0


@dataclass
class Inpainter:
    cfg: Cfg
    sess: object
    fps: float
    backend: str = "trt"
    fallback: object = None                 # fp16 出 NaN 时用的 fp32 会话，用到才建
    held: list[Held] = field(default_factory=list)
    stats: dict = field(default_factory=lambda: {"frames": 0, "groups": 0, "reused": 0, "tiles": 0, "nan_tiles": 0, "run_s": 0.0})
    keeps: tuple = field(default_factory=lambda: ([], {}))   # (这一帧的底板矩形, ctx_sig 按组缓存的"掩码外"格)

    def __post_init__(self):
        n = lambda sec: round(sec * self.fps)
        self.n_age, self.n_fade = max(1, n(self.cfg.max_age_s)), n(self.cfg.fade_s)

    def __call__(self, img: np.ndarray, rects: list, shown: list) -> np.ndarray:
        """rects：在屏的整块底板（分组、推理、沿用都按它）；shown：这一刻要抹掉的部分，只贴它。返回新图，img 不动。"""
        h, w = img.shape[:2]
        if h == REF_H or not rects:
            return self.at_ref(img, rects, shown)
        k = REF_H / h                                                        # 别的高度：整帧换到等效 1080p 修补，再换回来只贴要抹的部分
        sw = round(w * k)
        up = lambda r: (max(0, int(r[0] * k)), max(0, int(r[1] * k)), min(sw, -int(-r[2] * k)), min(REF_H, -int(-r[3] * k)))
        big = cv2.resize(img, (sw, REF_H), interpolation=cv2.INTER_AREA if k < 1 else cv2.INTER_CUBIC)
        fixed = cv2.resize(self.at_ref(big, [up(r) for r in rects], [up(r) for r in shown]), (w, h),
                           interpolation=cv2.INTER_AREA if k > 1 else cv2.INTER_CUBIC)
        out = img.copy()
        paste(out, fixed, (0, 0, w, h), shown)
        return out

    def at_ref(self, img: np.ndarray, rects: list, shown: list) -> np.ndarray:
        c, out = self.cfg, img.copy()
        h, w = img.shape[:2]
        self.stats["frames"] += 1
        if not rects:
            self.held = []
            return out
        clamp = lambda rs: [r for r in ((max(0, x0), max(0, y0), min(w, x1), min(h, y1)) for x0, y0, x1, y1 in rs)   # noqa: E731
                            if r[2] > r[0] and r[3] > r[1]]                   # 整块出画的不要：负的终点会让切片标上一大片
        rects, shown = clamp(rects), clamp(shown)
        if not rects:
            self.held = []
            return out
        mask = np.zeros((h, w), bool)
        for x0, y0, x1, y1 in rects:
            mask[y0:y1, x0:x1] = True
        if self.keeps[0] != rects:
            self.keeps = (rects, {})
        todo, keep = [], []
        for box in groups(rects, (c.win[0] - CTX_MIN) * c.scale):
            box = (max(0, box[0]), max(0, box[1]), min(w, box[2]), min(h, box[3]))
            if box[2] <= box[0] or box[3] <= box[1]:
                continue
            old = next((x for x in self.held if x.box == box), None)
            if not any(min(r[2], box[2]) > max(r[0], box[0]) and min(r[3], box[3]) > max(r[1], box[1]) for r in shown):
                if old is not None:                                           # 这一组还一个字都没打出来：不补，留着旧补丁
                    keep.append(old)
                continue
            self.stats["groups"] += 1
            sig = ctx_sig(img, box, mask, self.keeps[1])
            if old is not None and self.fresh(old, sig):
                self.stats["reused"] += 1
                old.age += 1
                keep.append(old)
                paste(out, self.shown_patch(old, self.corrected(old, sig)), box, shown)
            else:
                prev = self.corrected(old, sig) if old is not None and self.n_fade else None
                if prev is not None and old.fade_left:                        # 上一段渐变还没走完：从正在显示的那一帧接着变
                    prev = self.shown_patch(old, prev)
                todo.append((box, sig, prev))
        if todo:
            for (box, sig, prev), p in zip(todo, self.infer(img, mask, [b for b, *_ in todo])):
                hd = Held(box, p, sig, p.reshape(-1, 3).mean(axis=0, dtype=np.float32) / 255,
                          prev=prev, fade_left=self.n_fade if prev is not None else 0)
                paste(out, self.shown_patch(hd, p), box, shown)
                keep.append(hd)
        self.held = keep
        return out

    def corrected(self, old: Held, sig) -> np.ndarray:
        """沿用的补丁按上下文一圈的 Lab 均色差整体校色。"""
        k = sig[1] & old.sig[1]
        if k.any():
            d = (sig[0][k] - old.sig[0][k]).mean(axis=0)                      # Lab 均色差 → 补丁均色处换成 BGR 的整体平移
            return shift_bgr(old.patch, old.mean, d)
        return old.patch

    def shown_patch(self, hd: Held, patch: np.ndarray) -> np.ndarray:
        """渐变中就和重补前那块按帧混合（渐变 n 帧时第 1 帧新的占 1/(n+1)，n 帧后全是新的），并走一帧。"""
        if not hd.fade_left or hd.prev is None:
            return patch
        a = 1 - hd.fade_left / (self.n_fade + 1)
        hd.fade_left -= 1
        return cv2.addWeighted(hd.prev, 1 - a, patch, a, 0)

    def fresh(self, old: Held, sig) -> bool:
        if old.age + 1 >= self.n_age:
            return False
        k = sig[1] & old.sig[1]
        if not k.any():
            return False
        return float(np.linalg.norm(sig[0][k] - old.sig[0][k], axis=1).mean()) <= self.cfg.diff_thr

    def run(self, imgs: np.ndarray, masks: np.ndarray) -> np.ndarray:
        """推理一窗；出了 NaN 的窗都计数（`nan_tiles`），TensorRT fp16 的用 fp32 重算（fp32 会话用到才建，建会话不算进推理时间）。"""
        t0 = time.perf_counter()
        r = self.sess.run(None, {"image": imgs, "mask": masks})[0]
        dt = time.perf_counter() - t0
        bad = bool(np.isnan(r).any())
        self.stats["nan_tiles"] += bad
        if bad and self.backend == "trt":
            if self.fallback is None:
                print("[阶段 5] fp16 推理出了 NaN，建 fp32 会话重算这样的窗", file=sys.stderr, flush=True)
                self.fallback = session(self.cfg, "trt", "fp32")
            t0 = time.perf_counter()
            r = self.fallback.run(None, {"image": imgs, "mask": masks})[0]
            dt += time.perf_counter() - t0
        self.stats["run_s"] += dt
        self.stats["tiles"] += 1
        return r

    def infer(self, img: np.ndarray, mask: np.ndarray, boxes: list) -> list[np.ndarray]:
        """补这几组，按组返回外接矩形大小的补丁（BGR）。"""
        f = self.cfg.scale
        h0, w0 = img.shape[:2]
        if h0 % f or w0 % f:                                                  # 补到能整除，最右 / 最下几列也有结果
            img = cv2.copyMakeBorder(img, 0, -h0 % f, 0, -w0 % f, cv2.BORDER_REFLECT)
            mask = np.pad(mask, ((0, -h0 % f), (0, -w0 % f)), mode="symmetric")
        h, w = img.shape[:2]
        if f > 1:
            sw, sh = w // f, h // f
            small = cv2.resize(img, (sw, sh), interpolation=cv2.INTER_AREA)
            smask = cv2.resize(mask.view(np.uint8) * np.uint8(255), (sw, sh), interpolation=cv2.INTER_AREA) > 0   # 整数倍：块里有一个就算
        else:
            sw, sh, small, smask = w, h, img, mask
        TH, TW = self.cfg.win
        ph, pw = max(0, TH - sh), max(0, TW - sw)
        if ph or pw:                                                          # 缩小后不足一个窗：镜像补齐，镜像出来的字也遮掉
            small = cv2.copyMakeBorder(small, 0, ph, 0, pw, cv2.BORDER_REFLECT)
            smask = np.pad(smask, ((0, ph), (0, pw)), mode="symmetric")
        H, W = small.shape[:2]
        res = small.copy()
        imgs = np.zeros((1, 3, TH, TW), np.float32)
        masks = np.zeros((1, 1, TH, TW), np.float32)
        for x0, y0, x1, y1 in boxes:
            a0, b0, a1, b1 = x0 // f, y0 // f, -(-x1 // f), -(-y1 // f)
            for wy, vlo, vhi in windows(b0, min(H, b1), H, TH):
                for wx, lo, hi in windows(a0, min(W, a1), W, TW):
                    t = small[wy:wy + TH, wx:wx + TW]
                    for ch in range(3):                                       # 按通道写，比整块 transpose 快约 3 倍
                        np.divide(t[..., 2 - ch], np.float32(255), out=imgs[0, ch], dtype=np.float32)
                    masks[0, 0] = smask[wy:wy + TH, wx:wx + TW]
                    r = self.run(imgs, masks)[0]                              # 只转要写回的那一条
                    res[vlo:vhi, lo:hi] = r[::-1, vlo - wy:vhi - wy, lo - wx:hi - wx].clip(0, 255).astype(np.uint8).transpose(1, 2, 0)
        res = res[:sh, :sw]
        out = []
        for x0, y0, x1, y1 in boxes:
            if f > 1:                                                         # 只放大这一组附近，裁出外接矩形
                a0, b0, a1, b1 = max(0, x0 // f - 2), max(0, y0 // f - 2), min(sw, -(-x1 // f) + 2), min(sh, -(-y1 // f) + 2)
                up = cv2.resize(res[b0:b1, a0:a1], ((a1 - a0) * f, (b1 - b0) * f), interpolation=cv2.INTER_LINEAR)
                out.append(up[y0 - b0 * f:y1 - b0 * f, x0 - a0 * f:x1 - a0 * f].copy())
            else:
                out.append(res[y0:y1, x0:x1].copy())
        return out
