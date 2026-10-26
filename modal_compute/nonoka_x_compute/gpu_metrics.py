"""Modal GPU metrics that include allocations outside PyTorch."""

from __future__ import annotations

import os
import threading
from typing import Any


class NvmlProcessMemorySampler:
    """Sample this container process's GPU memory and retain its peak.

    FineSub's generic metric uses the PyTorch allocator.  Whisper is executed
    by CTranslate2, which talks to CUDA directly, so that metric is correctly
    zero from PyTorch's point of view but misleading for this deployment.
    NVML observes both allocators without changing the vendored engine.
    """

    def __init__(self, device_index: int = 0, interval: float = 0.25) -> None:
        self.device_index = int(device_index)
        self.interval = max(0.05, float(interval))
        self.peak_bytes: int | None = None
        self._nvml: Any = None
        self._handle: Any = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> "NvmlProcessMemorySampler":
        try:
            import pynvml

            pynvml.nvmlInit()
            self._nvml = pynvml
            self._handle = pynvml.nvmlDeviceGetHandleByIndex(self.device_index)
            self._sample()
        except Exception:
            self._nvml = None
            self._handle = None
            return self
        self._thread = threading.Thread(
            target=self._run,
            name="nvml-process-memory",
            daemon=True,
        )
        self._thread.start()
        return self

    def _processes(self) -> list[Any]:
        processes = list(
            self._nvml.nvmlDeviceGetComputeRunningProcesses(self._handle)
        )
        try:
            processes.extend(
                self._nvml.nvmlDeviceGetGraphicsRunningProcesses(self._handle)
            )
        except Exception:
            pass
        return processes

    def _sample(self) -> None:
        if self._nvml is None or self._handle is None:
            return
        try:
            used = sum(
                int(process.usedGpuMemory)
                for process in self._processes()
                if int(process.pid) == os.getpid()
                and isinstance(process.usedGpuMemory, int)
                and 0 <= int(process.usedGpuMemory) < (1 << 60)
            )
        except Exception:
            return
        if used > (self.peak_bytes or 0):
            self.peak_bytes = used

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            self._sample()

    def stop(self) -> int | None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        self._sample()
        if self._nvml is not None:
            try:
                self._nvml.nvmlShutdown()
            except Exception:
                pass
        self._nvml = None
        self._handle = None
        return self.peak_bytes


def format_bytes(value: int) -> str:
    size = float(max(0, int(value)))
    units = ("B", "KB", "MB", "GB", "TB")
    index = 0
    while size >= 1024 and index < len(units) - 1:
        size /= 1024
        index += 1
    return f"{size:.2f} {units[index]}"
