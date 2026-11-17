"""The engine packages' code as one import tree, for a test or gate process that needs pieces of several.

Since WP-P1 a top-level package name can live in more than one engine
package: `finesub_knowledge` is the protocol's client in the transcription and
correction packages and its server in the knowledge package. Each package's
own process only ever sees its own copy (`nonoka_x.engine_host`); a test that
sets a case up with FineSub's own code, or drives a client and its server from
one process, needs them side by side. `install()` makes each such package a
regular package whose `__path__` lists every package's directory of it, in
`ORDER`: a module is found in the first one that has it, and the files the
packages share are the same bytes (`tests/test_engine_packages.py`).

Only for tests and the gate's own children: nothing the desktop runs imports it.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

from . import ENGINES

#: Where each package's import roots are, in the order a shared name is looked up.
#: The knowledge package first: a process that starts `finesub_knowledge.server`
#: from the client (`client._server_environment`) puts this order on its path.
ORDER = ("finesub-knowledge", "finesub-correct", "finesub-transcribe")
#: The top-level packages more than one engine package carries.
SHARED = ("finesub_knowledge",)


def roots() -> list[Path]:
    """Every engine package's import roots, in `ORDER`."""

    import json

    found: list[Path] = []
    for package in ORDER:
        manifest = json.loads((ENGINES / package / "PACKAGE.json").read_text(encoding="utf-8"))
        for relative in manifest.get("paths") or ():
            root = (ENGINES / package / relative).resolve()
            if root not in found:
                found.append(root)
    return found


def install() -> None:
    """Put every package's roots on `sys.path` and merge each shared package's directories."""

    for root in reversed(roots()):
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
    for name in SHARED:
        directories = [str(root / name) for root in roots() if (root / name / "__init__.py").is_file()]
        if not directories:
            continue
        module = sys.modules.get(name) or importlib.import_module(name)
        module.__path__[:] = list(dict.fromkeys(directories))
