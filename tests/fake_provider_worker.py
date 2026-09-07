from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


def emit(kind: str, payload: dict | None = None) -> None:
    print(json.dumps({"type": kind, "payload": payload or {}}, separators=(",", ":")), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-dir", type=Path, required=True)
    args = parser.parse_args()
    request = json.loads(sys.stdin.readline())
    mode = request["source"].get("title", "success")
    marker = args.task_dir / "resume.marker"
    should_pause = mode == "slow" or (mode == "resume" and not marker.exists())
    if should_pause:
        # The marker represents a durable engine checkpoint. Persist it before
        # reporting progress so a test that observes progress can safely model
        # an application shutdown followed by resume.
        marker.write_text("started", encoding="utf-8")
    emit("stage", {"stage": "stable", "message": "fixture"})
    emit("progress", {"stage": "stable", "completed": 1, "total": 2, "unit": "segments"})
    if mode == "fail":
        emit("failed", {"message": "API key is missing"})
        return 1
    if mode == "gbk":
        # A native library narrating in the Windows console code page: these
        # bytes are not valid UTF-8, and the provider must survive them.
        sys.stdout.flush()
        sys.stdout.buffer.write("警告：显卡驱动过旧\n".encode("gbk"))
        sys.stdout.buffer.flush()
    if mode == "onnx-cuda-warning":
        print("2026-09-04 20:43:44,121 - WARNING - separator - CUDAExecutionProvider not available in ONNXruntime, so acceleration will NOT be enabled", flush=True)
        print("2026-09-04 20:43:45,000 - INFO - separator - Useful information", flush=True)
    if mode == "encoding-environment":
        emit(
            "log",
            {
                "python_utf8": os.environ.get("PYTHONUTF8"),
                "python_io_encoding": os.environ.get("PYTHONIOENCODING"),
                "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
            },
        )
    if should_pause:
        time.sleep(30)
    artifact_path = args.task_dir / "fixture-stable.json"
    artifact_path.write_text(
        '{"segments":[{"id":1,"start":0.0,"end":1.0,"text":"fixture","words":[]}]}\n',
        encoding="utf-8",
    )
    manifest = {
        "schema": 1,
        "task_id": args.task_dir.name,
        "engine_commit": "fixture",
        "artifacts": {
            "stable_json": {
                "uri": artifact_path.resolve().as_uri(),
                "sha256": "fixture",
                "bytes": artifact_path.stat().st_size,
            }
        },
    }
    emit("completed", {"artifacts": manifest})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
