"""The LLM speaker judge: one step of a correction task, between the preparation task and the windows.

``windows`` takes the correction windows (and halves one that does not fit), ``prompt`` renders what
the judge sees, ``answers`` reads its replies, ``run`` drives the windows and writes the sidecar that
``finesub.speech.diarization.judgments`` reads back for the `spk` cell and the split-track export.
"""

from .run import JudgeInputs, JudgeReport, run_speaker_judge  # noqa: F401
