"""URLs of the user-facing docs, for messages that must survive leaving the repo.

A packaged install has no checkout, so a message that says
``docs/manual/repo-install.md`` is a dead end for the people most likely to see
it. These are the addresses to quote instead.

Stdlib-only and import-free on purpose: `speech/runtime/cuda_libs.py` may not
import torch, and it reads these.

`finesub_bootstrap` keeps its own copy of the install page (it must not import
this package at all); `test_the_install_doc_url_is_the_same_everywhere` holds
the two together.
"""

from __future__ import annotations

#: Source install, per platform and vendor.
REPO_INSTALL_DOC = (
    "https://github.com/caca2331/finesub/blob/main/docs/manual/repo-install.md"
)

#: Which patched CTranslate2 wheel to install, and how to overwrite stock.
CT2_WHEEL_DOC = (
    "https://github.com/caca2331/finesub/blob/main/docs/manual/ct2-wheel.md"
)
