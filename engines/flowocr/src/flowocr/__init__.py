"""flowocr —— 从视频里抽全部屏幕文字、带起止时间、按区域聚类。

正式包（project-structure 计划）。
迁移期间 `tools/` 里的模块逐个搬进来（2026-09-22 搬完、shim 删光；调用方一律包 import），
三个阶段的子包（extract / analyze / output / artifacts）**有真代码才建目录**，
这里不预先摆空壳。
"""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as _version

try:
    __version__ = _version("flowocr")   # 唯一来源是 pyproject.toml 的 version，经包元数据读
except PackageNotFoundError:            # 没装成包、只把 src/ 放进 PYTHONPATH 时
    __version__ = "0+unknown"
