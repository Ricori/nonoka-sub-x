"""Quality gate, knowledge cases only: the knowledge base's "today" is one fixed date.

FineSub stamps an entry it writes with `KnowledgeRepo.today()` (its 最近更新日期),
and that date reaches the rendered files, their hashes in `rendered/.manifest.json`
and the knowledge page's content versions. A baseline recorded on one day would
not match on the next. The K cases (`tests.quality_harness.knowledge`) put this
directory first on `PYTHONPATH` and name the date in `NONOKA_QUALITY_KNOWLEDGE_TODAY`;
every process they start -- the worker, a knowledge process it starts -- then
gets that date from `KnowledgeRepo.today()`, the one place the knowledge code
asks for it. Nothing else changes: the module is loaded as it is, and only that
method is replaced once it has run.
"""

import importlib.abc
import os
import sys

_TODAY = os.environ.get("NONOKA_QUALITY_KNOWLEDGE_TODAY", "")
_TARGET = "finesub.llm.knowledge.node.repo"


class _FixedToday(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):  # noqa: ANN001 - finder protocol
        if name != _TARGET:
            return None
        for finder in sys.meta_path:
            if finder is self or not hasattr(finder, "find_spec"):
                continue
            spec = finder.find_spec(name, path, target)
            if spec is not None:
                break
        else:
            return None
        execute = spec.loader.exec_module

        def exec_module(module):  # noqa: ANN001
            execute(module)
            module.KnowledgeRepo.today = lambda self: _TODAY

        spec.loader.exec_module = exec_module
        return spec


if _TODAY:
    sys.meta_path.insert(0, _FixedToday())
