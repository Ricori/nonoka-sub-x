"""The correction window loop, split by what each part is responsible for.

``run`` plans and finalizes a task; ``serial`` and ``parallel`` are the two
drivers over its windows; ``attempts`` is the per-window retry/split loop they
share; ``request_turn`` is the window's first call; ``context`` holds the state a run's windows
share; ``commit`` owns what survives a rerun; ``metadata`` describes calls for
the artifacts.

Test doubles go on the module that does the name lookup, not here: a patch of
``finesub.llm.stages.correction.RoleClient`` would rebind nothing (see
``finesub.llm.stages.correction.run``).
"""

from .attempts import correction_role_for_profile  # noqa: F401
from .commit import WINDOW_CACHE_FILENAME  # noqa: F401
from .context import CorrectionRun  # noqa: F401
from .metadata import window_to_metadata  # noqa: F401
from .context import RequestTurnProduct  # noqa: F401
from .run import (  # noqa: F401
    CorrectionPlan,
    execute_correction_windows,
    refresh_annotated_speakers,
    resolve_correction_plan,
)
