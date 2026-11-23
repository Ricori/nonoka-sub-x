"""阶段 1：提取（解码、检测、识别、框级复用、视觉时间证据）。

project-structure 计划。2026-09-22 第一刀只是把 `tools/` 里提取侧的共享模块**原样搬进来**
（framesource / ptsclock / edge_refine / typewriter_fuse / recpack / recdecode / recort / ortclient /
ocr_args / ocr_complete / ocr_parallel），算法一行没动；之后 `run_ocr2` 和它的 reuse_v2 / fast_det / recpool、ORT 服务也搬了进来。
搬走的模块曾在 `tools/` 原位留 shim；2026-09-22 调用方全部改成包 import 之后 shim 已删。
推理只走 ONNX Runtime（2026-10 运行时去掉了 Paddle）：ORT 的 GPU 进程（`ort_server.py`）自己预载 CUDA 库，包初始化不碰它们。
"""
