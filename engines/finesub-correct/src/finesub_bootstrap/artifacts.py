"""`finesub_bootstrap.artifacts` for the finesub-correct package: only the names its FineSub code uses (WP-P1).

Copied from FineSub v0.7.0 (`7123055`), `src/finesub_bootstrap/artifacts.py`, as
vendored in Nonoka X (the transcription package carries the original). Kept: the
top-level definitions the names below need, unchanged and in the original's order;
everything else is left out. `__all__` is exactly the names this package's FineSub
code imports (docs/engine-packages.md section 4); `tests/test_engine_packages.py`
holds this package to them.

The original's description:

    What a finished task leaves on disk, and what may be removed.

    What a run produces is the pipeline's business; what happens to it afterwards
    is the front end's, and the pipeline never deletes anything it made.

    The lists are shared. The desktop read all of them -- it tidied a finished
    task down to what a rerun needs. The CLI runs where the user pointed it and
    takes nothing out of their folder, so it reads the rest: which two files are
    worth copying into the task directory as the record of this work, which file a
    stage delivers, and what that file is called in a record both front ends read.

    Naming is the pipeline's, restated. `finesub.pipeline` derives every
    artifact from the delivered SRT, and this module has to name the same files
    without importing that package -- the CLI shell resolves them before it has an
    interpreter that could import it, and `finesub_bootstrap` must not depend on
    the pipeline. `test_paths.py` pins the two lists against each other, which is
    what keeps a restatement from becoming a divergence.
"""

from __future__ import annotations


__all__ = ["ARTIFACT_DIR_SUFFIX", "RESOLVED_SWITCHES_FILENAME"]


#: Appended to the stem to name the LLM harness's working directory. Removed
#: whole: session and window checkpoints exist to survive a *failure*, and a
#: task that reached cleanup had none.
ARTIFACT_DIR_SUFFIX = ".llm-artifacts"


#: Inside the artifact directory: the LLM switches the correction task ran
#: with and where each came from. Written by the main package's correction
#: task, pointed at by the CLI's task record -- one spelling for both.
RESOLVED_SWITCHES_FILENAME = "resolved-switches.json"
