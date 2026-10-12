"""Keep native libraries from misreading non-ASCII paths on Windows.

Python hands a path to a native library as UTF-8 bytes. A library that opens
it with a narrow-character API -- libtorch's `fopen` behind `torch.jit.load` --
goes through the Universal CRT, which turns those bytes back into UTF-16 with
the system ANSI code page unless the process's CRT locale is UTF-8 (UCRT
`_sopen_nolock` -> `__acrt_get_utf8_acp_compatibility_codepage`).
Python sets `LC_CTYPE` to the user default at startup, whose code page *is* the
ANSI code page. So on a machine with the "Beta: Use Unicode UTF-8" option on
(code page 65001) everything works, and on a stock Chinese Windows (GBK, 936)
an install under `C:\\Users\\<中文名>\\...` fails with "No such file or
directory" for a file that is right there -- the 0.5.1 report that
`silero_vad.jit` did not exist.

Two measures, deliberately overlapping:

- `use_utf8_crt_locale` switches the CRT to UTF-8 once per process. One call
  covers every library that links the UCRT dynamically, including loaders
  nobody has found yet; `c10.dll`, `torch_cpu.dll` and `python312.dll` all
  import `api-ms-win-crt-*`. It cannot be verified on a UTF-8 machine, which
  is what the maintainer has.
- `load_torchscript` reads the archive through a Python file object, so the
  path never reaches libtorch at all. It holds by construction, which is why
  it stays even though the locale should already cover it.

CTranslate2 needs neither: since v3.14.0 its `open_file_read` converts the
UTF-8 path with `MultiByteToWideChar(CP_UTF8)` and opens the wide string on
Windows, so the code page never reaches its model reader.
"""

from __future__ import annotations

import locale
import sys
from pathlib import Path
from typing import Any


def use_utf8_crt_locale() -> str | None:
    """Make the CRT decode narrow-character paths as UTF-8 (Windows only).

    Only `LC_CTYPE` carries the code page, so only it changes. The locale is
    read when a file is opened, not when a library is imported, so calling this
    after torch or CTranslate2 are already imported is fine -- it has to happen
    before the first model file is opened.

    Returns what went wrong, for the caller to warn about: this runs before any
    reporter is bound, so it cannot report by itself.
    """

    if sys.platform != "win32":
        return None
    try:
        locale.setlocale(locale.LC_CTYPE, ".UTF8")
    except locale.Error as error:
        # The UCRT learned the UTF-8 locale in Windows 10 1803. Older systems
        # keep the ANSI code page; ASCII paths still work there.
        return (
            f"could not switch the C runtime to UTF-8 ({error}); "
            "model files under a non-ASCII path may fail to open."
        )
    return None


def load_torchscript(path: Path, *, map_location: Any) -> Any:
    """`torch.jit.load` without handing the path to libtorch.

    The only place in `src/` allowed to call `torch.jit.load`; a structural
    test holds that.
    """

    import torch

    with open(path, "rb") as handle:
        return torch.jit.load(handle, map_location=map_location)
