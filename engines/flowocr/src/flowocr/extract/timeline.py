"""各级时间线（`run_ocr2 --timeline <路径>`，NON_PRODUCT；decode-buffer）。

`STAGE` 只给每一级的**总和**：解码、det、判决、rec 各自 14~16 s，流水线却要 24.5 s——总和看不出**谁在什么时候堵谁**。
这里把每一段"在做什么 / 在等什么"记成区间 `(种类, 线程, 起, 止, 参数)`，跑完落盘，`dev_tools/timeline_report.py` 按时间对齐各线程来读。

* **关着的时候零开销**：`span` / `mark` 先看一个模块级的 None。开着时每条事件一个元组 append（GIL 下原子）；
  5 分钟段约十几万条、几 MB。**全在内存里、跑完才落盘**，所以封顶 `MAX_EVENTS` 条（约 1 GB 以内）：
  超了之后的丢掉、`_meta.dropped` 记丢了几条——整片要看时间线就开一个窗口跑，别开整片。
* **跨进程对得上**：时刻一律 `time.perf_counter()`——Windows 上是系统级的 QPC，各进程同一根轴；
  每份文件头上另记一对 `(time.time(), perf_counter())` 锚点，换平台时按锚点对齐。
* 子进程（`--edge-proc` 的回抠进程、`--decode-shards` 的只解码进程、ORT 推理服务）从环境变量 `FLOWOCR_TIMELINE` 认出开着，
  各写 `<路径>.<角色>.jsonl`；管线进程写 `<路径>` 本身。
"""
from __future__ import annotations

import json
import os
import threading
import time

ENV = "FLOWOCR_TIMELINE"

MAX_EVENTS = 5_000_000
_ev: list | None = None
_dropped = 0
_path: str | None = None
_role = "main"


def start(path: str, role: str = "main") -> None:
    """开始记。管线进程调这个（同时把路径放进环境变量，之后起的子进程都跟着记）。"""
    global _ev, _path, _role, _dropped
    _ev, _path, _role, _dropped = [], path, role, 0
    if role == "main":
        os.environ[ENV] = path


def start_from_env(role: str) -> None:
    """子进程：环境变量里有路径就开始记，写 `<路径>.<角色>.jsonl`。"""
    p = os.environ.get(ENV)
    if p:
        start(f"{p}.{role}.jsonl", role)


def on() -> bool:
    return _ev is not None


def span(kind: str, t0: float, t1: float | None = None, a=None) -> float:
    """记一段 `[t0, t1)`（t1 不给 = 现在），返回 t1——调用方可以接着当下一段的起点。"""
    t1 = time.perf_counter() if t1 is None else t1
    if _ev is not None:
        _add((kind, threading.current_thread().name, t0, t1, a))
    return t1


def mark(kind: str, a=None) -> None:
    """记一个时刻（零长的区间）。"""
    if _ev is not None:
        t = time.perf_counter()
        _add((kind, threading.current_thread().name, t, t, a))


def _add(e: tuple) -> None:
    global _dropped
    if len(_ev) < MAX_EVENTS:           # type: ignore[arg-type]
        _ev.append(e)                   # type: ignore[union-attr]
    else:
        _dropped += 1


def dump() -> str | None:
    """落盘（JSONL：头一行 `_meta`，之后一行一条）。没开就什么都不做。返回写到哪了。
    先写 `.part` 再换名：进程在写的半中间被杀时，留下的是上一次的完整一份，不是半截。"""
    if _ev is None or not _path:
        return None
    ev = list(_ev)
    part = _path + ".part"
    with open(part, "w", encoding="utf-8") as f:
        f.write(json.dumps({"_meta": {"role": _role, "pid": os.getpid(),
                                      "anchor": [time.time(), time.perf_counter()], "n": len(ev),
                                      "dropped": _dropped}}) + "\n")
        for k, th, t0, t1, a in ev:
            f.write(json.dumps({"k": k, "th": th, "t0": round(t0, 6), "t1": round(t1, 6), "a": a},
                               ensure_ascii=False) + "\n")
    os.replace(part, _path)
    return _path
