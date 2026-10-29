"""The Microsoft Visual C++ runtime that PyTorch needs on Windows.

torch's `c10.dll` links against `msvcp140.dll`, which neither the torch wheel
nor uv's managed Python ships: it comes from the system's VC++ 2015-2022
Redistributable. Without it the import fails with `[WinError 126]`; with one
older than 14.40 (Visual Studio 2022 17.10, whose STL the current torch is
built with) `c10.dll` fails its initialisation with `[WinError 1114]`. Both
only surface after the multi-gigabyte runtime is installed, in the import
probe, as a bare DLL error -- torch prints its own hint to stdout, which the
probe discards. This module checks the DLL before the download and words the
probe's failure in terms the user can act on.
"""

from __future__ import annotations

from collections.abc import Callable
import os
from pathlib import Path
import re
import sys

#: The first STL whose binaries the current torch wheels are compatible with.
MINIMUM_MSVCP_VERSION = (14, 40)

REDIST_URL = "https://aka.ms/vs/17/release/vc_redist.x64.exe"

_REMEDY = (
    f"请安装最新版 Microsoft Visual C++ 2015–2022 运行库（x64）：{REDIST_URL}"
    "（已安装时选择「修复」），重启电脑后再安装运行环境。"
)

VersionReader = Callable[[Path], tuple[int, ...] | None]


def file_version(path: Path) -> tuple[int, ...] | None:
    """The fixed file version of a Windows binary, or None when unreadable."""

    import ctypes
    from ctypes import wintypes

    version = ctypes.windll.version
    size = version.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        return None
    buffer = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(str(path), 0, size, buffer):
        return None
    info = ctypes.c_void_p()
    length = wintypes.UINT()
    if not version.VerQueryValueW(buffer, "\\", ctypes.byref(info), ctypes.byref(length)):
        return None
    # VS_FIXEDFILEINFO: signature, struct version, then MS and LS file version.
    fields = ctypes.cast(info, ctypes.POINTER(wintypes.DWORD * 4)).contents
    ms, ls = fields[2], fields[3]
    return (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF)


def msvc_runtime_problem(
    *,
    platform: str = sys.platform,
    system_root: Path | None = None,
    read_version: VersionReader | None = None,
) -> str | None:
    """Why torch would not load on this machine, or None when it would.

    Only a missing or provably old `msvcp140.dll` counts: a version that
    cannot be read is not a reason to refuse an install that may well work.
    """

    if platform != "win32":
        return None
    root = system_root or Path(os.environ.get("SystemRoot", r"C:\Windows"))
    dll = root / "System32" / "msvcp140.dll"
    if not dll.is_file():
        return f"系统缺少 Microsoft Visual C++ 运行库（找不到 msvcp140.dll），PyTorch 无法加载。{_REMEDY}"
    try:
        version = (read_version or file_version)(dll)
    except OSError:
        return None
    if version is None or version[:2] >= MINIMUM_MSVCP_VERSION:
        return None
    found = ".".join(str(part) for part in version)
    required = ".".join(str(part) for part in MINIMUM_MSVCP_VERSION)
    return (
        f"系统的 Microsoft Visual C++ 运行库版本过旧（msvcp140.dll {found}，需要 {required} 或更高），"
        f"PyTorch 无法加载。{_REMEDY}"
    )


_DLL_LOAD_FAILURE = re.compile(r"WinError (?:126|1114)\]|Error loading .*\.dll", re.IGNORECASE)


def explain_import_failure(detail: str) -> str:
    """Reword a DLL load failure from the import probe; pass anything else on."""

    if not _DLL_LOAD_FAILURE.search(detail):
        return detail
    return f"PyTorch 的系统依赖加载失败，通常是缺少或使用了过旧的 Microsoft Visual C++ 运行库。{_REMEDY}\n原始错误：{detail}"


def explaining(validator: Callable[[Path], tuple[bool, str]]) -> Callable[[Path], tuple[bool, str]]:
    """Wrap a runtime validator so its DLL load failures say what to do."""

    def validate(python_executable: Path) -> tuple[bool, str]:
        healthy, detail = validator(python_executable)
        return healthy, detail if healthy else explain_import_failure(detail)

    return validate
