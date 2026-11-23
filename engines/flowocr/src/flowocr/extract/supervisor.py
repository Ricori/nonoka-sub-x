"""`run_ocr2` 的**外层监督者**：不 import 任何推理库，只起 worker 子进程干活，硬解中途交付不对就等第一趟整棵进程树退干净再起软解那一趟。

2026-09-22 从 `run_ocr2.py` 里搬出来（正规化阶段 D）：发行包的命令行入口 `flowocr-ocr`（`pyproject` 的 `[project.scripts]`）
要能**不 import `run_ocr2`** 就起监督者——那个模块顶层 import cv2 / numpy 和整个提取包（当时还直接 `from paddleocr import …`），
console script 会先把模块整个 import 进来，监督者"自己不占显存"这条（Codex 复审第三轮 P2）就破了。`python -m flowocr.extract.run_ocr2`（启动器 shim 已删）
照旧：`run_ocr2` 的 `__main__` 在推理库 import 之前调 `supervise()`，行为不变。

常量（环境变量名 / 退出码）也住在这里：worker 侧（`run_ocr2`）从这里 import。
契约测试：`dev_tools/hw_retry_check.py`（重试前整棵树退干净，Job Object 与 psutil 兜底两条路）。
Linux 没有 Job Object（2026-10-17，WSL 上的 Linux 试跑后补）：监督者设成 child subreaper、worker 设 `PR_SET_PDEATHSIG`，
收尾照样按进程树（`_become_subreaper` / `_die_with_parent`）。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from flowocr.paths import child_env

WORKER_ENV = "FLOWOCR_OCR_WORKER"
"""设了就是 worker 那一趟（监督者起子进程时设）；没设就是监督者。"""
HW_RETRY_ENV = "FLOWOCR_HWACCEL_RETRIED"
"""第二趟（软解重跑）的环境里带着第一趟失败的原因，worker 写进 `_meta.hwaccel_fallback`。"""
HW_REASON_ENV = "FLOWOCR_HWACCEL_REASON_FILE"
"""worker 把硬解交付不对的原因写进这个文件，监督者读了再决定起不起第二趟。"""
EXIT_HW_MISMATCH = 75
"""硬解跑到一半交付不对时 worker 的退出码：外层监督者见到它，**等这一趟完全退出之后**再起软解那一趟。"""
EXIT_HW_UNCLEAN = 76
"""硬解交付不对、而且第一趟的子进程没能确认全部退出：监督者**不重试**，也不许调用方（`run_groups` 的整批软解重跑）重试——
残留和新一趟会同时占卡（显存预算 6.5 GB，owner 2026-10-05）。2026-09-25 审计（Codex P1）：广播解码那条路原来仍退 75，run_groups 照样重跑。"""
OCR_LANG_ENV = "FLOWOCR_OCR_LANG_PROBE"
"""`--ocr-lang auto` 判出的结果（`langprobe` 写的 JSON 文件路径）。监督者 / run_groups 探测一次后设，worker、切段的子进程、
整段重跑和多组的各组都读同一份；已经设了的（多组的各组子进程又经过一次监督者）不再探。"""
FAULT_ORPHAN_ENV = "FLOWOCR_FAULT_ORPHAN"
"""**只给测试用**：值是一个文件路径。硬解故障那一刻 worker 故意留下一个**不收的孙进程**（模拟不听话的模型服务），
pid 写进这个文件——验证监督者在起第二趟之前把它清掉（`hw_retry_check.py`）。"""
FAULT_NO_JOB_ENV = "FLOWOCR_FAULT_NO_JOB"
"""**只给测试用**：假装 Job Object 建不起来，验证 psutil 兜底那条路。"""
PR_SET_PDEATHSIG, PR_SET_CHILD_SUBREAPER = 1, 36
"""Linux `prctl` 的两个选项（`<linux/prctl.h>`）。"""
_SUBREAPER = False
"""这个进程已经设成 child subreaper（`_become_subreaper`）：`_drain_tree` 把归到自己名下的孤儿也算进这棵树。"""


def supervise(argv: list[str] | None = None) -> int:
    """**外层监督者**（2026-09-20，Codex 复审第三轮 P2）：不 import 任何推理库，只起 worker 子进程干活。

    为什么不在同一个进程里重试：第一版在 `except HwaccelMismatch` 里直接起软解子进程——
    traceback 还挂着 `main()` 的整个栈帧（ORT 客户端、det 模型都活着），推理库的显存池也不会因为对象被回收就还给系统，
    于是两套推理资源同时在卡上，"合计仍在 8 GB 内"那句注释是没核过的推断。**进程边界才是可靠的释放**：
    worker 退出码 `EXIT_HW_MISMATCH` -> 这里等它 `wait()` 完、再起 `--hwaccel ''` 那一趟。
    **监督者死了 worker 也要跟着死**（不留孤儿占卡）：Windows 上用 Job Object + `KILL_ON_JOB_CLOSE`——
    监督者哪怕被强杀，job 句柄随之关闭，系统把 worker **连同它起的 ffmpeg / ORT 服务**一起杀掉。
    ⚠ 第一版用的是 stdin 看门狗（worker 起一根线程阻塞在 `sys.stdin.read()` 上），**在 Windows 上会死锁**：
    同步管道句柄上有读挂着时，worker 起 ffmpeg 要复制继承 stdin 句柄，就一直等那个读结束——
    契约测试 `hw_retry_check.py` 跑出来的是"worker 一行输出都没有就卡死"。ORT 服务端用同一招没事，
    是因为它启动看门狗之后不再起子进程。"""
    import tempfile
    args = list(sys.argv[1:] if argv is None else argv)
    argv = [sys.executable, "-m", "flowocr.extract.run_ocr2", *args]
    fd, reason_file = tempfile.mkstemp(prefix="flowocr-hw-", suffix=".txt")
    os.close(fd)
    env = child_env(dict(os.environ, **{WORKER_ENV: "1", HW_REASON_ENV: reason_file}))
    probe_file = None
    if wants_auto(args) and not os.environ.get(OCR_LANG_ENV):
        probe_file = probe_lang(args, env)
        if probe_file is None:
            os.unlink(reason_file)
            return 2
        env[OCR_LANG_ENV] = probe_file
    try:
        rc, clean = _run_worker(argv, env)
        if rc == EXIT_HW_MISMATCH and os.environ.get("FLOWOCR_DECODE_HUB"):
            # 多组共用一个广播解码进程（run_groups --share-decode）：**不在这里各自重跑**——N 组各自改软解就是 N 路软解同时起
            # （审核：av1 一路软解 189 核·秒 / 5 分钟，两路就吃满 CPU）。退出码原样交给 run_groups，由它整批改软解逐组重跑
            if not clean:
                print("[硬解] 跑到一半交付不对，但这一趟的子进程没能确认全部退出：**不交给 run_groups 重跑**（重跑会和残留同时占卡）", flush=True)
                return EXIT_HW_UNCLEAN
            print("[硬解] 跑到一半交付不对；这一组由广播解码供帧，交给 run_groups 整批重跑", flush=True)
            return rc
        if rc == EXIT_HW_MISMATCH:
            reason = Path(reason_file).read_text(encoding="utf-8").strip() or "（原因没写出来）"
            if not clean:
                # 确认不了第一趟的整棵进程树（ORT 服务 / ffmpeg）都退了，就**不起**第二趟——
                # 宁可这一段失败，也不让两套推理资源同时占卡（显存预算 6.5 GB，owner 2026-10-05）
                print(f"[硬解] 交付不对（{reason}），但第一趟的子进程没能确认全部退出，**不重试**", flush=True)
                return EXIT_HW_UNCLEAN
            print(f"[硬解] 跑到一半交付不对，第一趟的整棵进程树已清空；整段改软解重跑：{reason}", flush=True)
            rc, _ = _run_worker(argv + ["--hwaccel", ""], dict(env, **{HW_RETRY_ENV: reason[:500]}))
        return rc
    finally:
        for f in (reason_file, probe_file):
            try:
                if f:
                    os.unlink(f)
            except OSError:
                pass


def arg_value(args: list[str], name: str) -> str | None:
    """命令行里某个选项的值（最后一个算数，同 argparse；`--x v` 和 `--x=v` 都认）。不 import 解析器：监督者不碰推理库，也不为这个多载一层。"""
    got = None
    for i, a in enumerate(args):
        if a == name and i + 1 < len(args):
            got = args[i + 1]
        elif a.startswith(name + "="):
            got = a.split("=", 1)[1]
    return got


def wants_auto(args: list[str]) -> bool:
    """命令行里要 `--ocr-lang auto`。"""
    return arg_value(args, "--ocr-lang") == "auto"


def probe_lang(args: list[str], env: dict, extra: tuple[str, ...] = ()) -> str | None:
    """`--ocr-lang auto`：起一个**独立的探测子进程**（`flowocr.extract.langprobe`）跑完、整棵进程树退干净，返回结果文件；
    失败（取不到模型、打不开视频……）打出原因、返回 None——不悄悄退回默认语言（韩语片按默认模型读出来全是错字）。"""
    import tempfile
    if arg_value(args, "--rec-onnx"):
        # 冲突要在探测之前报：探测子进程只解析不补全参数，等 worker 的 resolve 报出来时三个模型已经下完、探测已经跑完
        print("[判语言] --rec-onnx 和 --ocr-lang auto 只能给一个：自带的 rec 模型认哪些字由它自己的字表定", flush=True)
        return None
    fd, out = tempfile.mkstemp(prefix="flowocr-lang-", suffix=".json")
    os.close(fd)
    cmd = [sys.executable, "-m", "flowocr.extract.langprobe", "--probe-out", out, *extra, *args]
    rc, clean = _run_worker(cmd, {k: v for k, v in env.items() if k != WORKER_ENV})
    if rc != 0 or not clean or not Path(out).read_text(encoding="utf-8").strip():
        print(f"[判语言] 探测没成（退出码 {rc}{'、子进程没确认退干净' if not clean else ''}）：不开跑。"
              "要先在联网处取好模型就跑 `flowocr-models fetch rec-ko rec-ru rec-th`，或者直接点名 --ocr-lang", flush=True)
        try:
            os.unlink(out)
        except OSError:
            pass
        return None
    return out


def _kill_on_close_job():
    """Windows：建一个"句柄一关、里面的进程全杀"的 Job Object；别的系统或建不成返回 None（照常跑，只是少一道保险）。"""
    if os.name != "nt" or os.environ.get(FAULT_NO_JOB_ENV):
        return None
    import ctypes
    from ctypes import wintypes

    class BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class IO(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in ("r", "w", "o", "rb", "wb", "ob")]

    class EXT(ctypes.Structure):
        _fields_ = [("Basic", BASIC), ("Io", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    job = k32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = EXT()
    info.Basic.LimitFlags = 0x2000                       # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not k32.SetInformationJobObject(wintypes.HANDLE(job), 9, ctypes.byref(info), ctypes.sizeof(info)):
        return None                                      # 9 = JobObjectExtendedLimitInformation
    return (k32, job)


def _job_active(k32, h) -> int:
    """Job 里还活着几个进程（JobObjectBasicAccountingInformation.ActiveProcesses）。查不到返回 -1。"""
    import ctypes
    from ctypes import wintypes

    class ACC(ctypes.Structure):
        _fields_ = [("TotalUserTime", ctypes.c_int64), ("TotalKernelTime", ctypes.c_int64),
                    ("ThisPeriodTotalUserTime", ctypes.c_int64), ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                    ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
                    ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD)]
    acc = ACC()
    if not k32.QueryInformationJobObject(wintypes.HANDLE(h), 1, ctypes.byref(acc), ctypes.sizeof(acc), None):
        return -1
    return int(acc.ActiveProcesses)


def _prctl():
    """Linux 的 `prctl`（libc）；别的系统或取不到返回 None。在 fork 之前取好：`preexec_fn` 里别再 dlopen。"""
    if not sys.platform.startswith("linux"):
        return None
    import ctypes
    try:
        return ctypes.CDLL(None, use_errno=True).prctl
    except (OSError, AttributeError):
        return None


def _become_subreaper() -> bool:
    """Linux：把本进程设成 child subreaper。没有 Job Object 时收尾按 ppid 求闭包，Windows 上成立是因为孤儿的 ppid 仍指着
    死掉的父进程；Linux 上孤儿被 init 收养、ppid 指不回这棵树。设成 subreaper 后，树里谁的父进程先死，孤儿都归到本进程名下，
    `_drain_tree` 顺着 ppid = 本进程找得到（也由本进程收尸）。设不上返回 False（照常跑，只是少一道保险）。"""
    global _SUBREAPER
    prctl = _prctl()
    if not _SUBREAPER and prctl is not None:
        _SUBREAPER = prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0
    return _SUBREAPER


def _die_with_parent():
    """Linux：给 worker 的 `preexec_fn`——监督者一死，worker 收 SIGKILL（`PR_SET_PDEATHSIG`，对应 Windows 上 Job 的
    `KILL_ON_JOB_CLOSE`）。worker 起的 ffmpeg / ORT 服务靠连锁反应跟着退（管道断、stdin EOF），和 Windows 上
    逃出 Job 的那些一样。别的系统返回 None。"""
    import signal
    prctl, parent = _prctl(), os.getpid()
    if prctl is None:
        return None

    def f() -> None:
        prctl(PR_SET_PDEATHSIG, int(signal.SIGKILL), 0, 0, 0)
        if os.getppid() != parent:           # fork 之后、prctl 之前监督者已经死了：信号不会再来，自己退
            os._exit(1)
    return f


def _drain_job(job, timeout: float = 30.0) -> bool:
    """**这一趟结束后，把 Job 里剩下的进程全杀掉、等到清零、再关句柄**（2026-09-20，Codex 复审第四轮）。

    上一版只 `wait()` 了直接子进程（worker），Job 句柄一直开着，`KILL_ON_JOB_CLOSE` 要等**监督者最后退出**才触发——
    worker 退出码 75 之后，它起的 ORT 服务 / ffmpeg 这类孙进程可能还活着，第二趟就和它们同时占卡。
    返回"确认清空了"；清不空（超时 / 查不到）返回 False，调用方据此**不起下一趟**。"""
    from ctypes import wintypes
    k32, h = job
    k32.TerminateJobObject(wintypes.HANDLE(h), 1)
    deadline = time.time() + timeout
    n = _job_active(k32, h)
    while n != 0 and time.time() < deadline:
        time.sleep(0.1)
        n = _job_active(k32, h)
    k32.CloseHandle(wintypes.HANDLE(h))
    return n == 0


def _track_tree(p: subprocess.Popen, seen: dict) -> None:
    """Job 不可用时的兜底：worker 活着的时候反复记下它的整棵进程树（pid -> create_time，防 pid 复用）。"""
    try:
        import psutil
        for c in psutil.Process(p.pid).children(recursive=True):
            try:
                seen.setdefault(c.pid, c.create_time())
            except psutil.Error:
                pass
    except Exception:                        # noqa: BLE001  psutil 不在 / 进程刚好退了
        pass


def _ppid_table(psutil) -> dict[int, int]:
    """全机 pid -> ppid。psutil 的 `ppid_map`（私有，但它自己的 `Process.children` 就靠它）一次拿全、3 ms；
    没有这个函数（换了 psutil 版本 / 平台）就退回 `process_iter`（冷的时候 1 s 级，结果相同）。"""
    f = getattr(getattr(psutil, "_psplatform", None), "ppid_map", None)
    if f is not None:
        try:
            return dict(f())
        except Exception:                        # noqa: BLE001  私有接口出任何问题都退回公开的那条
            pass
    return {q.pid: q.info["ppid"] for q in psutil.process_iter(["ppid"])}


def _drain_tree(seen: dict, root_pid: int, t_start: float, timeout: float = 30.0) -> bool:
    """兜底的收尾：记下来的那些进程里还活着的（create_time 对得上），逐个杀掉并等它们退出。

    ⚠ 轮询是每 0.5 s 一次，**worker 退出前一瞬间才起的子进程**记不到——所以收尾时再按 `ppid` 扫一遍、求闭包：
    Windows 上孤儿进程的 `ppid` 仍然指着已经死掉的父进程，起始时间晚于这一趟开始的就是它的后代；
    Linux 上孤儿归到设成 subreaper 的本进程名下（`_become_subreaper`），所以本进程也算一个根。"""
    try:
        import psutil
    except ImportError:
        print("[监督] ⚠ 没有 Job Object、也没有 psutil：确认不了第一趟的子进程都退了", flush=True)
        return False
    roots = {root_pid, *seen, *([os.getpid()] if _SUBREAPER else [])}
    grew = True
    while grew:
        grew = False
        # pid -> ppid 全表一次拿，`create_time` 只问父进程在树里的那几个（2026-09-23，decode-buffer）：
        # `process_iter` 冷的时候要给每个进程建 Process 对象、逐个打开进程取 create_time，全机 325 个 1.07 s——每次 worker 退出都白付
        for pid, ppid in _ppid_table(psutil).items():
            if ppid not in roots or pid in roots:
                continue
            try:
                ct = psutil.Process(pid).create_time()
            except psutil.Error:
                continue
            if ct >= t_start - 1:
                roots.add(pid)
                seen.setdefault(pid, ct)
                grew = True
    alive = []
    for pid, ct in seen.items():
        try:
            q = psutil.Process(pid)
            if abs(q.create_time() - ct) < 1e-3:
                alive.append(q)
        except psutil.Error:
            pass
    for q in alive:
        try:
            q.kill()
        except psutil.Error:
            pass
    _gone, still = psutil.wait_procs(alive, timeout=timeout)
    return not still


def _run_worker(argv: list[str], env: dict) -> tuple[int, bool]:
    """跑一趟 worker，返回 `(退出码, 这一趟的整棵进程树确认清空了没有)`。"""
    t_start = time.time()
    _become_subreaper()
    p = subprocess.Popen(argv, env=env, preexec_fn=_die_with_parent())
    job = _kill_on_close_job()
    if job is not None:
        from ctypes import wintypes
        k32, h = job
        if not k32.AssignProcessToJobObject(wintypes.HANDLE(h), wintypes.HANDLE(int(p._handle))):
            print("[监督] ⚠ 没能把 worker 放进 Job Object，改用进程树兜底", flush=True)
            k32.CloseHandle(wintypes.HANDLE(h))
            job = None
    seen: dict = {}
    try:
        while True:
            try:
                rc = p.wait(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                _track_tree(p, seen)
    finally:
        if p.poll() is None:                 # 监督者自己出事（Ctrl-C 之类）：别留下占卡的 worker
            p.kill()
            p.wait()
    # ⚠ **Job 在也要按进程树收尾**（2026-09-20，契约测试 hw_retry_check.py 跑出来的）：Windows 上 venv 的
    # `Scripts\python.exe` 是个启动器，它给真解释器另建一个允许**静默脱离**的 Job，于是 worker 起的子进程全都逃出了
    # 我们这个 Job——`TerminateJobObject` 之后 Job 里立刻是 0 个进程，那个故意不收的孙进程却一直活到监督者退出之后。
    # 上一轮"只杀监督者、3 秒后全没了"能过，靠的是连锁反应（worker 死 -> ORT 服务读到 stdin EOF、ffmpeg 管道断），
    # 不是 Job 杀的。所以 Job 只当"监督者被强杀"时的尽力保险，**确认清空靠进程树**
    clean_job = _drain_job(job) if job is not None else True
    clean = _drain_tree(seen, p.pid, t_start) and clean_job
    return rc, clean


def main() -> int:
    """`flowocr-ocr` 的入口（`[project.scripts]`）：参数原样交给 worker。"""
    return supervise()


if __name__ == "__main__":
    raise SystemExit(main())
