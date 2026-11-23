"""自选 OCR 范围的多组（ocr-regions 计划）：**每组一条独立 OCR 线、同时跑**，阶段一能共用的部分共用。

    python -m flowocr.extract.run_groups <视频> --regions <json> --outdir <目录> [--start 秒 --end 秒] [其余参数原样给 run_ocr2]

产物：`<目录>/<组名>.jsonl`（和单组 `run_ocr2 --regions … --region-group …` 同一份东西，`_meta.run_groups` 另记这一趟的共用方式）。
之后各组各自建轨（阶段二）、各自输出（阶段三）。

**共用的是什么**（落地结构，方案的首版）：每组一个管线进程（组之间的事件状态天然隔离），
det 和 rec 走**同一个**共享 ORT 服务——模型和显存一份，几组的请求按 批 × 宽 准入（同 `--workers`，`run_ocr2.start_ort_services`，defaults §1.42）；
`--share-decode`（默认开）时解码也只有一份：一个广播解码进程（`decode_proc.hub_serve`）按分片解一遍、同一份采样帧给各组、
辅助流按组各一条有界队列转给各组的回抠进程。为什么值得（方案）：两组各自分片解 = 4 个 ffmpeg 上下文，det / rec 推理被分时
拖慢约 1.5 倍；各自不分片则供帧跟不上——共用一份分片解码两头都要。
硬解中途交付不对：各组**不**各自改软解（那是 N 路软解同时起），整批失败后这里改软解**逐组串行**重跑。
各组遮罩不同，整帧 det 的**计算**本来就是 N 份；共用服务省的是权重和 CUDA 上下文。

`--serial`：逐组串行（对照臂；不共用 ORT 服务）。一组失败**不取消**其余组（各组是独立的线，互不依赖），退出码取第一个非零。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from flowocr.paths import child_env

GROUPS_ENV = "FLOWOCR_RUN_GROUPS"
"""子进程从这里认出自己是 `run_groups` 的一组（run_ocr2 写进 `_meta.run_groups`；产物不变，所以不进 config）。"""


def group_names(spec_path: str) -> list[str]:
    d = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    return [g["name"] for g in (d.get("groups") or [{"name": "all"}])]


def per_group(argv: list[str], g: str) -> list[str]:
    """各组写各自的时间线（`--timeline P` / `--timeline=P` -> `P.<组名>`；几组写同一份会互相覆盖）。纯函数，守卫直接测。"""
    out: list[str] = []
    it = iter(range(len(argv)))
    for i in it:
        a = argv[i]
        if a == "--timeline" and i + 1 < len(argv):
            out += [a, f"{argv[i + 1]}.{g}"]
            next(it, None)
        elif a.startswith("--timeline="):
            out.append(f"{a}.{g}")
        else:
            out.append(a)
    return out


def check_rest(rest: list[str]) -> None:
    """原样转给 run_ocr2 的参数里不许有这几个：`--workers N` 会变成 组数 × N 个进程（每组再各切段）；
    `--out` / `--regions` / `--region-group` 由这里按组给。"""
    for a in rest:
        key = a.split("=", 1)[0]
        if key in ("--workers", "--out", "--regions", "--region-group"):
            raise SystemExit(f"run_groups：{key} 由这里按组给 / 不支持（多组 × 多段 = 进程数相乘），别放进转给 run_ocr2 的参数里")


def hub_wanted(args) -> str:
    """该不该起广播解码：返回不起的原因（空 = 起）。**看 `resolve_split` 之后的值**（审核：`--decode-shards 0/1` / `--decoder cv2` /
    `--frame-select index` 时各组根本不会去接广播进程，它就永远等不满订阅数、全体挂到超时）。纯函数，守卫直接测。"""
    import contextlib
    import copy
    import io

    from flowocr.extract import ocr_args
    a = ocr_args.resolve(copy.deepcopy(args))       # 副本：resolve_split 会把 decode_shards 就地改成 0，别动调用方那份
    with contextlib.redirect_stdout(io.StringIO()):
        ocr_args.resolve_split(a)
    if a.decode_shards <= 1:
        return getattr(a, "decode_shards_off", "") or f"--decode-shards {a.decode_shards}（不分片）"
    return ""


class Hub:
    """广播解码进程（`decode_proc --hub n`）：地址写进子进程环境（`decode_proc.HUB_ENV`）；`started` 在它开流之后置上；
    `close()` 收整棵进程树（它起的几路 ffmpeg 不能成孤儿）。起不来时 `proc is None`（各组各解各的）。"""

    def __init__(self, env: dict, n: int, reason_file: str) -> None:
        import secrets
        import threading

        from flowocr.extract import decode_proc as D

        key = secrets.token_bytes(16)
        self.started = threading.Event()
        self.clean = True                              # close() 确认整棵树退了没有（它自己正常退出的算干净）
        self.proc = subprocess.Popen([sys.executable, "-m", "flowocr.extract.decode_proc", "--hub", str(n)],
                                     env=child_env(dict(env, **{D.HUB_KEY_ENV: key.hex(), D.HUB_REASON_ENV: reason_file})),
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                     errors="replace")
        hello: dict = {}
        for _ in range(200):                           # 第一行 JSON 前可能混着 import 期的告警（stderr 并进来了）
            ln = self.proc.stdout.readline()
            if not ln:
                break
            try:
                hello = json.loads(ln)
                break
            except json.JSONDecodeError:
                print(f"[广播解码] {ln.rstrip()}", flush=True)
        if "port" not in hello:
            self.close()
            self.proc = None
            print("[多组] 广播解码进程起不来，各组各解各的", flush=True)
            return
        env[D.HUB_ENV] = f"{hello['port']}:{key.hex()}"
        print(f"[多组] 广播解码进程在 127.0.0.1:{hello['port']}（{n} 组共用一次解码）", flush=True)
        threading.Thread(target=self._drain, daemon=True, name="hub-out").start()

    def _drain(self) -> None:                          # 之后它打的行照常转出来；不读的话管道满了它就卡住
        for ln in self.proc.stdout:
            if ln.startswith('{"started"'):
                self.started.set()
            else:
                print(f"[广播解码] {ln.rstrip()}", flush=True)

    def close(self) -> bool:
        """整棵进程树收掉（psutil 在就先收子进程——它起的 ffmpeg；再收它自己）。返回**确认全退了**没有
        （整批软解重跑的前提；psutil 不在时子进程确认不了，按没确认算）。"""
        if self.proc is None:
            return True
        if self.proc.poll() is not None:
            return self.clean                          # 已经收过（运行中途有组先退时）：用那一次的结果；自己退的算干净
        kids_gone = False
        try:
            import psutil
            kids = psutil.Process(self.proc.pid).children(recursive=True)
            for c in kids:
                try:
                    c.kill()
                except psutil.Error:
                    pass
            kids_gone = not psutil.wait_procs(kids, timeout=10)[1]
        except Exception:                              # noqa: BLE001  psutil 不在 / 进程刚退
            pass
        self.proc.kill()
        self.clean = _reaped(self.proc, 10) and kids_gone
        return self.clean


def _reaped(p, timeout: float) -> bool:
    """等进程退出，限时；返回退没退（`TimeoutExpired` 不往外抛）。"""
    try:
        p.wait(timeout)
    except subprocess.TimeoutExpired:
        pass
    return p.poll() is not None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("video")
    ap.add_argument("--regions", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--start", type=float, default=0.0)
    ap.add_argument("--end", type=float, default=0.0)
    ap.add_argument("--serial", action="store_true", help="逐组串行（对照臂；不共用 ORT 服务）")
    ap.add_argument("--share-decode", action=argparse.BooleanOptionalAction, default=True,
                    help="多组共用一个广播解码进程（默认开；关 = 各组各解各的）")
    ns, rest = ap.parse_known_args(argv)
    check_rest(rest)
    names = group_names(ns.regions)
    out = Path(ns.outdir)
    out.mkdir(parents=True, exist_ok=True)
    base = [ns.video, "--start", repr(ns.start), "--end", repr(ns.end), "--regions", ns.regions, *rest]
    import os
    from flowocr.extract.supervisor import OCR_LANG_ENV, probe_lang, wants_auto
    if wants_auto(rest) and not os.environ.get(OCR_LANG_ENV):
        # --ocr-lang auto：起共用服务之前按**各组范围的并集**判一次，各组（和整批改软解的重跑）都读这一份；
        # 各组子进程经过的监督者见到已有结果就不再探
        got = probe_lang([*base, "--out", str(out / "_")], child_env(dict(os.environ)), ("--probe-all-groups",))
        if got is None:
            return 2
        os.environ[OCR_LANG_ENV] = got
        import atexit
        atexit.register(lambda: Path(got).unlink(missing_ok=True))     # 这一批跑完删（各组、整批重跑都读完了）
    cmds = {g: [sys.executable, "-m", "flowocr.extract.run_ocr2", *per_group(base, g), "--region-group", g,
                "--out", str(out / f"{g}.jsonl")] for g in names}
    t0 = time.time()
    if ns.serial or len(names) == 1:
        import os
        env = dict(os.environ, **{GROUPS_ENV: json.dumps({"groups": len(names), "mode": "serial", "ort_shared": False})})
        rcs = {g: subprocess.call(c, env=child_env(env)) for g, c in cmds.items()}
    else:
        from flowocr.extract import ocr_args
        from flowocr.extract import ffcheck
        from flowocr.extract import run_ocr2 as R
        args = ocr_args.parse_args([ns.video, "--out", str(out / "_"), *rest])
        ffcheck.require_runtime()                     # 没装 / 装重了推理后端就在父进程说清楚，别等 ort_server 的 ImportError
        srvs, env = R.start_ort_services(args, len(names), " 组")   # 共享服务时模型在父进程先取好（首跑时别让几组子进程同时去取同一份）
        import tempfile
        fd, reason_file = tempfile.mkstemp(prefix="flowocr-hub-", suffix=".txt")
        import os
        os.close(fd)
        why_not = hub_wanted(args) if ns.share_decode else "--no-share-decode"
        if why_not and ns.share_decode:
            print(f"[多组] 不起广播解码（{why_not}），各组各解各的", flush=True)
        hub = Hub(env, len(names), reason_file) if not why_not else None
        if hub is not None and hub.proc is None:
            hub = None
        env[GROUPS_ENV] = json.dumps({"groups": len(names), "mode": "parallel", "ort_shared": "FLOWOCR_ORT_ADDR" in env,
                                      "decode_shared": hub is not None})
        procs: dict = {}
        try:
            procs = {g: subprocess.Popen(c, env=child_env(env)) for g, c in cmds.items()}
            print(f"[多组] {len(names)} 组同时跑：{names}", flush=True)
            while any(p.poll() is None for p in procs.values()):
                # 广播进程开流之前有组退出了（模型建不起来之类）：它永远等不满订阅数——收掉它，其余组立刻断开、错误清楚
                if (hub is not None and not hub.started.is_set() and hub.proc.poll() is None
                        and any(p.poll() not in (None, 0) for p in procs.values())):   # 正常跑完的组（发了 "x" 自己解）不算
                    print("[多组] 有组在广播解码开流之前就退出了，收掉广播进程（其余组会报'广播解码进程断开'）", flush=True)
                    hub.close()
                time.sleep(0.5)
            rcs = {g: p.returncode for g, p in procs.items()}
        finally:
            # 先收各组、再收服务（审核：只收服务的话，还活着的组连接一断会按"接不上共享服务"那条路各自再起一整份 worker——显存翻倍）
            for p in procs.values():
                if p.poll() is None:
                    p.kill()
            # 这一批的资源**确认全退了**才许整批重跑（审计 2026-09-25，Codex 复审 P1）：各组、广播解码进程树、共享 ORT 服务——
            # 后两样是这里起的，不在各组监督者清理的进程树里，"各组都清干净了"盖不住它们
            leftover = [f"组 {g}" for g, p in procs.items() if not _reaped(p, 30)]
            if hub is not None and not hub.close():
                leftover.append("广播解码进程")
            for p0 in srvs:
                p0.kill()
            leftover += ["共享 ORT 服务" for p0 in srvs if not _reaped(p0, 30)]
        from flowocr.extract.supervisor import EXIT_HW_MISMATCH, EXIT_HW_UNCLEAN
        if hub is not None and (EXIT_HW_UNCLEAN in rcs.values() or (leftover and EXIT_HW_MISMATCH in rcs.values())):
            # 有组的子进程没能确认全部退出（supervisor 退 76），或这一批自己起的进程没退：整批重跑会和残留同时占卡，宁可这一批失败
            print(f"[多组] 广播解码硬解中途交付不对，但没能确认全部退出（{'、'.join(leftover) or '组内子进程'}）：**不整批重跑**", flush=True)
            rcs = {g: EXIT_HW_UNCLEAN if rc == EXIT_HW_MISMATCH else rc for g, rc in rcs.items()}
        elif hub is not None and EXIT_HW_MISMATCH in rcs.values():
            # 广播解码硬解交付不对：各组没有各自改软解（supervisor 见到广播地址就原样退 75），这里整批改软解、逐组串行
            print("[多组] 广播解码硬解中途交付不对，整批改软解逐组重跑", flush=True)
            import os
            from flowocr.extract.supervisor import HW_RETRY_ENV
            reason = Path(reason_file).read_text(encoding="utf-8").strip() or "（广播解码没写出原因）"
            env2 = dict(os.environ, **{GROUPS_ENV: json.dumps({"groups": len(names), "mode": "serial-soft-retry",
                                                              "ort_shared": False, "decode_shared": False}),
                                       HW_RETRY_ENV: reason[:500]})   # 重跑的 obs 记 `_meta.hwaccel_fallback`（为什么是软解）
            rcs = {g: subprocess.call(c + ["--hwaccel", ""], env=child_env(env2)) for g, c in cmds.items()}
        try:
            os.unlink(reason_file)
        except OSError:
            pass
    print(f"[多组] 完成 {time.time() - t0:.1f} s，退出码 {rcs}", flush=True)
    bad = [rc for rc in rcs.values() if rc != 0]
    return bad[0] if bad else 0


if __name__ == "__main__":
    sys.exit(main())
