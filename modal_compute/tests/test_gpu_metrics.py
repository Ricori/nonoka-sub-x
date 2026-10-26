from __future__ import annotations

import os
import sys
from types import SimpleNamespace


def test_nvml_sampler_captures_non_pytorch_process_memory(monkeypatch) -> None:
    from nonoka_x_compute.gpu_metrics import NvmlProcessMemorySampler

    fake_nvml = SimpleNamespace(
        nvmlInit=lambda: None,
        nvmlShutdown=lambda: None,
        nvmlDeviceGetHandleByIndex=lambda index: f"gpu-{index}",
        nvmlDeviceGetComputeRunningProcesses=lambda handle: [
            SimpleNamespace(pid=os.getpid(), usedGpuMemory=768 * 1024**2),
            SimpleNamespace(pid=os.getpid() + 1, usedGpuMemory=2 * 1024**3),
        ],
        nvmlDeviceGetGraphicsRunningProcesses=lambda handle: [],
    )
    monkeypatch.setitem(sys.modules, "pynvml", fake_nvml)

    sampler = NvmlProcessMemorySampler(interval=60).start()
    peak = sampler.stop()

    assert peak == 768 * 1024**2
