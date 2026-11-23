"""只读 obs，重建**缝合之前**的两层（run、窗区域），给聚类这一层的尺和原型共用（next-steps）。

为什么单独一个模块：量聚类要在**同一批 run、同一批窗区域**上换缝合算法（各臂只差 `stitch_slots`
那一步），而 `build_runs` 在整片上要几分钟——所以建一次、缓存、各臂共用。

**参数只有一个来源**：`build_tracks.apply_matcher_patch()`——管线走的那条（默认值 + 匹配器按游戏给的聚类补丁
+ 调用方给的 `extra`，`extra` 压过补丁）。不在这里抄默认值（`probe_band_layers` 那次漏传 `region_time_gate` 的教训）。

缓存落在**数据根**的 `tmp/cluster/`（槽里没有 tmp/）。层缓存的键 = obs 的大小和 mtime + **这两层吃的参数**（`LAYER_ARGS`）+
`build_tracks.py` 里**决定这两层的那一段代码**的 hash（`LAYER_END` 之前）；缝合结果的缓存另外带整份文件的 hash。对不上就重建。

用法：
    from flowocr.analyze import cluster_layers as CL
    L = CL.load("f5")                      # 或 CL.load("gi2", ["--region-time-gate", "2"])
    slots = CL.stitch(L)                   # 当前默认臂
    slots = CL.stitch(L, reassign=False)   # 换一条臂：只覆盖 stitch_slots 的关键字
"""
from __future__ import annotations

import hashlib
import json
import pickle
import sys
from dataclasses import dataclass
from pathlib import Path

from flowocr.analyze import build_tracks as bt  # noqa: E402
from flowocr import paths  # noqa: E402
from flowocr.provenance import local  # noqa: E402

FILMS = {f"f{i}": f"yuka-f{i}" for i in range(1, 6)}
"""yuka 五部：tag -> 产物目录（obs 的位置从那份产物的 provenance 取）。别的 tag 走 `out/gamestream/<tag>.jsonl`。"""


def obs_path(tag: str) -> Path:
    root = paths.data_root()
    if tag in FILMS:
        from flowocr.artifacts import tracksio
        doc = tracksio.load(root / "out" / FILMS[tag] / f"{tag}-tracks.json")
        return local(doc["provenance"]["obs"])      # 不猜文件名，从产物的 provenance 取（记的是 portable 路径）
    return root / "out" / "gamestream" / f"{tag}.jsonl"


@dataclass
class Layers:
    tag: str
    obs: Path
    video: str
    W: int
    H: int
    frame_us: int
    args: dict
    runs: list
    win_regions: list
    key: str = ""


LAYER_ARGS = ("min_conf", "iou", "sim", "gap_frames", "no_typewriter", "text_pick", "tail_max", "vote_independent",
              "prefix_min", "typewriter_min_chars", "window_sec", "mutual_x", "region_time_gate",
              "region_h_ratio", "grow_h_ratio", "rejoin_split")
"""run / 窗区域这两层吃的参数——**只有它们**进层缓存的键。第一版把整份参数和整个 `build_tracks.py` 的 hash 都放进去，
于是翻一个缝合层的默认值、甚至改一句帮助文本，七部片子各 7 分钟的缓存就全废（2026-09-21 一天内重建了三遍）。"""
LAYER_END = "def slot_fits("
"""`build_tracks.py` 里这一行之前的代码决定 run 和窗区域（`build_runs` / `build_tracks` / `build_regions` /
`cluster_windowed` / `region_geom` 都在它前面）；之后是缝合、打标、导出。守卫核着这个假设。"""


def _code(part: str) -> str:
    src = "\n".join(Path(bt.__file__).read_text(encoding="utf-8").splitlines())     # 换行归一
    cut = src.index("\n" + LAYER_END)
    return hashlib.sha1((src[:cut] if part == "layers" else src).encode()).hexdigest()[:12]


def _args(tag: str, extra: list[str]) -> dict:
    a = bt.apply_matcher_patch(bt.make_parser(),
                               ["<obs>", "--outdir", "<out>", "--tag", tag, "--matcher", "gametext", *extra])
    return {k: v for k, v in vars(a).items() if k not in ("matcher", "matcher_ctx", "matcher_patch", "matcher_overridden", "matcher_context")}


def layer_key(obs: Path, a: dict) -> str:
    """层缓存的键。**守卫量的是这个函数**（第一版的键改了一半没生效、而我只验了旁边的 `_code`，缓存照样天天全废）。"""
    st = obs.stat()
    return hashlib.sha1(json.dumps([str(obs), st.st_size, st.st_mtime_ns, {k: a[k] for k in LAYER_ARGS},
                                    _code("layers")], sort_keys=True, default=str).encode()).hexdigest()[:12]


def from_cache(d: dict, a: dict, key: str) -> Layers:
    """缓存里的层数据 + **本次**解析的参数。键收窄之后，缝合层参数不同的两次加载命中同一份缓存——
    缓存里那份 `args` 是**建缓存那一次**的，原样带回来就会让 `stitch(L)` 静默用上一次调用的旋钮
    （Codex 审计 2026-09-21 复现：缓存来自 `pooled / reassign=False`，请求 `median / True`，缝合收到的是前者）。"""
    return Layers(**{**d, "obs": Path(d["obs"]), "key": key, "args": a})


def load(tag: str, extra: list[str] | None = None, verbose: bool = True) -> Layers:
    obs = obs_path(tag)
    a = _args(tag, list(extra or []))
    st = obs.stat()
    key = layer_key(obs, a)
    cache = paths.data_root() / "tmp" / "cluster" / f"{tag}-{key}.pkl"
    if cache.exists():
        with open(cache, "rb") as f:          # 存的是字段字典：直接 pickle 数据类会把定义它的模块名
            d = pickle.load(f)                 # （当脚本跑时是 `__main__`）钉进缓存，别的入口就读不回来
        return from_cache(d, a, key)
    if verbose:
        print(f"[cluster_layers] 重建 {tag}（{obs.name}，{st.st_size / 1e6:.0f} MB）…", flush=True)
    with open(obs, encoding="utf-8") as f:
        meta = json.loads(f.readline())["_meta"]
        rows = [o for o in (json.loads(x) for x in f if x.strip()) if o["conf"] >= a["min_conf"]]
    W, H = meta.get("width"), meta.get("height")
    if not W:
        raise SystemExit(bt.OBS_NO_SIZE)
    frame_us = int(round(1e6 / meta["sample_fps"]))
    runs = bt.build_runs(rows, frame_us, a["iou"], a["sim"], a["gap_frames"],
                         grow_typewriter=not a["no_typewriter"], text_pick=a["text_pick"],
                         tail_max=a["tail_max"], vote_independent=a["vote_independent"],
                         prefix_min=a["prefix_min"], typewriter_min_chars=a["typewriter_min_chars"],
                         grow_h_ratio=a["grow_h_ratio"], rejoin_split=a["rejoin_split"])
    _, wr = bt.cluster_windowed(runs, W, H, int(a["window_sec"] * 1e6), a["mutual_x"],
                                a["region_time_gate"], a["region_h_ratio"])
    L = Layers(tag, obs, str(local(meta["video"])), W, H, frame_us, a, runs, wr, key)   # obs 记的是 portable()，开文件要本机路径
    cache.parent.mkdir(parents=True, exist_ok=True)
    with open(cache, "wb") as f:
        pickle.dump({**{k: v for k, v in L.__dict__.items() if k != "key"}, "obs": str(obs)}, f,
                    protocol=pickle.HIGHEST_PROTOCOL)
    if verbose:
        print(f"[cluster_layers] {tag}: {len(runs)} run、{len(wr)} 个窗区域 -> {cache.name}", flush=True)
    return L


def stitch(L: Layers, **over) -> list[list[int]]:
    """按生效参数缝合；`over` 覆盖 `stitch_slots` 的关键字（开臂用）。"""
    a = L.args
    kw = dict(anchor=not a["no_slot_anchor"], mutual_x=a["slot_mutual_x"],
              span_ratio=a["slot_span_ratio"], geom_mode=a["slot_geom"],
              move_seed_last=a["move_seed_last"], reassign=a["slot_reassign"],
              reassign_iters=a["slot_reassign_iters"], h_ratio=a["slot_h_ratio"])
    kw.update(over)
    # 整片上缝一条臂要一两分钟，而尺要反复量同几条臂——结果按（层缓存键 + 生效关键字）落盘。
    # 键里带整份 build_tracks.py 的内容 hash（层缓存的键只管前半段），所以改了缝合代码不会读到旧结果。
    h = hashlib.sha1(json.dumps([kw, _code("all")], sort_keys=True).encode()).hexdigest()[:10]
    cache = paths.data_root() / "tmp" / "cluster" / f"{L.tag}-{L.key}-stitch-{h}.pkl"
    if L.key and cache.exists():
        with open(cache, "rb") as f:
            return pickle.load(f)
    slots = bt.stitch_slots(L.runs, L.win_regions, L.W, L.H, **kw)
    if L.key:
        with open(cache, "wb") as f:
            pickle.dump(slots, f, protocol=pickle.HIGHEST_PROTOCOL)
    return slots


if __name__ == "__main__":
    for t in sys.argv[1:]:
        L = load(t)
        print(t, len(L.runs), "run", len(L.win_regions), "窗区域", L.W, L.H, L.video)
