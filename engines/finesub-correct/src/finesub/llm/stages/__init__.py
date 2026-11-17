"""Stage modules for the correction/translation pipeline.

Each stage is a plain function operating on explicit inputs; the orchestration
(order, skip/resume semantics) lives in ``finesub.llm.correction_translation``.
"""

from .correction import (  # noqa: F401
    WINDOW_CACHE_FILENAME,
    execute_correction_windows,
    window_to_metadata,
)
from .plan import FastDecision, decide_fast_mode, plan_fast_window  # noqa: F401
