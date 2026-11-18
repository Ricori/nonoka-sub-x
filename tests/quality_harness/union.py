"""The engine packages' code as one import tree, for a test or gate process that needs pieces of several.

Since WP-P1 a top-level package name lives in more than one engine package:
`finesub` (each package carries the FineSub files its half needs, the shared
ones byte for byte the same), `finesub_bootstrap` (the original in the
transcription package, a compatibility package with fewer names in the other
two) and `finesub_knowledge` (the protocol's client in the transcription and
correction packages, its server in the knowledge package). Each package's own
process only ever sees its own copy (`nonoka_x.engine_host`); a test that sets
a case up with FineSub's own code, or drives a client and its server from one
process, needs them side by side.

`install()` puts every package's import roots on `sys.path` and a finder in
front of the others for the shared names: a module is loaded from the first
package, in that name's `ORDER`, that has it, and a package's `__path__` lists
its directory in every engine package, so the next level is looked up the same
way. The files the packages share are the same bytes
(`tests/test_engine_packages.py`), so which copy is loaded does not change what
runs, with one deliberate exception: `finesub_bootstrap` is the
transcription package's original, which has every name the compatibility
packages have.

Only for tests and the gate's own children: nothing the desktop runs imports it.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path

from . import ENGINES

#: The engine packages, in the order their roots go on `sys.path`.
PACKAGES = ("finesub-knowledge", "finesub-correct", "finesub-transcribe")
#: The top-level names more than one engine package carries, with the order a module of each is looked up.
ORDER = {
    # The knowledge package first: a process that starts `finesub_knowledge.server`
    # from the client (`client._server_environment`) puts this order on its path.
    "finesub_knowledge": ("finesub-knowledge", "finesub-correct", "finesub-transcribe"),
    # The correction package first: a package's data is found next to its `__init__`
    # (`importlib.resources`, `Path(__file__).parent`), and the LLM half's prompt
    # templates are there; a module only the transcription package has is still
    # found in it.
    "finesub": ("finesub-correct", "finesub-transcribe", "finesub-knowledge"),
    # The original, which has every module and name the compatibility packages have.
    "finesub_bootstrap": ("finesub-transcribe", "finesub-correct", "finesub-knowledge"),
}
#: The top-level packages more than one engine package carries.
SHARED = tuple(ORDER)


def package_roots(package: str) -> list[Path]:
    manifest = json.loads((ENGINES / package / "PACKAGE.json").read_text(encoding="utf-8"))
    return [(ENGINES / package / relative).resolve() for relative in manifest.get("paths") or ()]


def roots() -> list[Path]:
    """Every engine package's import roots, in `PACKAGES` order."""

    found: list[Path] = []
    for package in PACKAGES:
        if not (ENGINES / package / "PACKAGE.json").is_file():
            continue
        for root in package_roots(package):
            if root not in found:
                found.append(root)
    return found


class _UnionFinder(importlib.abc.MetaPathFinder):
    """Finds a shared name's modules across the engine packages (see the module docstring)."""

    def __init__(self) -> None:
        self.ordered = {
            top: [root for package in order if (ENGINES / package / "PACKAGE.json").is_file()
                  for root in package_roots(package)]
            for top, order in ORDER.items()
        }

    def find_spec(self, fullname, path=None, target=None):  # noqa: ANN001, ANN201
        top = fullname.split(".", 1)[0]
        if top not in self.ordered:
            return None
        parts = fullname.split(".")
        directories = [root.joinpath(*parts) for root in self.ordered[top]]
        packages = [directory for directory in directories if (directory / "__init__.py").is_file()]
        if packages:
            spec = importlib.util.spec_from_file_location(
                fullname, packages[0] / "__init__.py",
                submodule_search_locations=[str(directory) for directory in packages],
            )
            return spec
        for root in self.ordered[top]:
            file = root.joinpath(*parts[:-1], parts[-1] + ".py")
            if file.is_file():
                return importlib.util.spec_from_file_location(fullname, file)
        return None


def install() -> None:
    """Put every package's roots on `sys.path` and the union finder in front for the shared names."""

    for root in reversed(roots()):
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
    if not any(isinstance(finder, _UnionFinder) for finder in sys.meta_path):
        # Just ahead of the path finder, behind every finder already in front of
        # it: one that wraps a module as it loads (the knowledge cases' fixed
        # "today", `knowledge_today/sitecustomize.py`) asks the finders after
        # it, this one included.
        position = next((index for index, finder in enumerate(sys.meta_path)
                         if finder is importlib.machinery.PathFinder), len(sys.meta_path))
        sys.meta_path.insert(position, _UnionFinder())
