"""The few business rules a compute leg still applies for itself.

Everything else about a job -- what it costs, who may see it, when it expires
-- is the Worker's. These stay here because they are checked against things
only a container has: the downloaded audio, and the files a run wrote.
"""

from __future__ import annotations


#: What a finished job publishes, and the file name the Worker stores each
#: artifact under. Must match `JOB_ARTIFACT_SUFFIXES` in
#: `nonoka-cloud-cf/src/jobs/model.ts`, which refuses any other name.
#:
#: `diar_json` is the engine's acoustic speaker timeline (`subtitle-diar.json`),
#: delivered only when the request asked for diarization: the desktop places it
#: in its own work directory and runs the judge levels there, as a local run
#: would.
JOB_ARTIFACT_SUFFIXES = {
    "stable_json": ".stable.json",
    "diar_json": ".diar.json",
}

#: The cloud library's artifact names and their file suffixes. The export of the
#: old app's library (`migrate/export_legacy.py`) copies files under these names.
LIBRARY_ARTIFACT_SUFFIXES = {
    "stable_json": ".stable.json",
    "raw_srt": ".raw.srt",
    "annotated_csv": ".annotated.csv",
    "final_srt": ".srt",
}

MAX_AUDIO_SECONDS = 2 * 60 * 60

#: How far the audio may legitimately run past what the client declared before
#: the task is refused. Container duration, stream duration and a VFR source's
#: own metadata routinely disagree by a second or two, and the desktop reads one
#: of them, so a little slack costs nothing. What this is sized against is not
#: noise: the declared length picks the separation tier and clears the two-hour
#: ceiling, both before a single byte has been uploaded, so a task that declares
#: a minute and uploads two hours buys the most expensive rate for the longest
#: input the ladder was built to make cheap.
DURATION_TOLERANCE_RATIO = 1.1
DURATION_TOLERANCE_SECONDS = 60.0


def source_duration_fault(declared: float, actual: float) -> str:
    """Say why decoded audio does not match the task it was accepted for.

    Returns an empty string when the source is acceptable. Everything the
    Worker could check about length it checked against a number the client
    supplied, because the upload had not happened yet -- this is the first
    point at which the real audio is on disk and the claim can be tested.

    An unreadable probe is not a fault: refusing a task because ffprobe printed
    something unexpected would turn a diagnostic into an outage, and everything
    downstream reads the audio for itself anyway.

    Over-declaring is not a fault either. It buys a lower separation rate for a
    shorter input, which costs the deployment nothing and only the client any
    quality.
    """

    try:
        real = float(actual)
        claimed = float(declared)
    except (TypeError, ValueError):
        return ""
    if real <= 0:
        return ""
    if real > MAX_AUDIO_SECONDS:
        return (
            f"上传音频实际时长 {real:.0f}s 超过 2 小时上限"
        )
    if claimed <= 0:
        return ""
    ceiling = claimed * DURATION_TOLERANCE_RATIO + DURATION_TOLERANCE_SECONDS
    if real > ceiling:
        return (
            f"上传音频实际时长 {real:.0f}s 远超申报的 {claimed:.0f}s，"
            "任务按申报时长定价与选档，拒绝执行"
        )
    return ""


def diarization_requested(request: dict) -> bool:
    """Whether this run computes, and therefore delivers, a speaker timeline."""

    block = request.get("diarization")
    return isinstance(block, dict) and block.get("level") not in (None, "", "off")
