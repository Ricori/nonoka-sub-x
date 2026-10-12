"""The speaker judge inside a correction task: what it is given, and the record it leaves.

`correction_translation._run_task` calls `judge_task` between the preparation task and the correction
windows, and `preview_task` in a dry run. What the judge is told comes from the task: the title (the
material's description, else the output's name), the preparation task's summary and people, the
knowledge base's streamers -- read only when the run reads the knowledge base. Its modality is the
request's own (`speaker_judge`: `text` / `audio`), not the correction's.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any, Sequence

from ....speech.diarization import judgments as sidecar
from ...chunking import SubtitleWindow
from ...knowledge.base import append_task_artifact
from ...token_budget import TokenCounter
from .prompt import audio_layout, candidates, judge_prompt, profiles, rows_of
from .roster import roster_entries
from .run import (
    JudgeInputs,
    JudgeReport,
    entities_of,
    judge_group,
    marks_shown,
    run_speaker_judge,
    speakers_of,
    summary_of,
    vocal_audio_for,
)
from .windows import split_windows

#: The title when the request carries no description and the output has no name.
UNTITLED = "（未提供标题）"


def judge_inputs(request: Any, context_pack: Any, *, knowledge_root: Any, fallback_title: str) -> JudgeInputs:
    """`knowledge_root=None` when the run does not read the knowledge base. The title is the extra-info
    block the correction already has (URL, scraped title, description, `--extra-info`); a local file
    with none of that falls back to its name, which is usually the title."""
    title = (request.extra_info or "").strip() or (fallback_title or "").strip() or UNTITLED
    roster = roster_entries(knowledge_root, title)
    return JudgeInputs(title, summary_of(context_pack),
                       tuple(candidates(entities_of(context_pack), roster, speakers_of(context_pack))))


def audio_route_usable(difficulty: str) -> bool:
    """Whether the judge's audio group has a model that hears audio: checked before the run, because a
    call no candidate can take ends as "no route", never as a fallback to text."""
    try:
        from ...routing.capabilities import ChainRequirement, _effective_endpoints, usable_endpoints
        from ...routing.config import role_config_for

        endpoints = _effective_endpoints(role_config_for(judge_group("audio"), difficulty))
        return bool(usable_endpoints(ChainRequirement(label="说话人裁判", endpoints=endpoints, needs_audio=True)))
    except Exception:  # noqa: BLE001 - no route declaration to consult: let the call find out
        return True


def judge_media(request: Any, warn: Any) -> str:
    """`audio` when the request asks for it and a model can hear it, else `text` (with a warning)."""
    if request.speaker_judge != "audio":
        return "text"
    if audio_route_usable(request.profile.difficulty):
        return "audio"
    warn("说话人裁判要带音频（--diarize judge-audio），但能听音频的那组模型里没有可用的：这次按纯文本判")
    return "text"


def judge_audio(request: Any, media: str, warn: Any) -> Path | None:
    """The separated vocals (passed in, else beside the stable), else the source with a warning;
    None when the judge reads text only."""
    if media != "audio":
        return None
    for vocal in (request.vocal_audio, vocal_audio_for(request.stable_json)):
        if vocal is not None and Path(vocal).exists():
            return Path(vocal)
    if request.audio_path:
        warn("说话人裁判没找到人声分离轨（-vocal.ogg / .flac），改用原始音频：声音样本与录音会混着伴奏")
        return Path(request.audio_path)
    return None


def _limits(media: str, difficulty: str) -> Any:
    """The judge's own group's planning envelope; None when there is no declaration to consult."""
    try:
        from ...routing.config import planning_limits_for

        return planning_limits_for(judge_group(media), difficulty)
    except Exception:  # noqa: BLE001 - no route declaration (a stub config): skip the input check
        return None


def judge_task(request: Any, correction_windows: Sequence[SubtitleWindow], context_pack: Any, *,
               client: Any, artifact_dir: Path, task_id: str, knowledge_root: Any, fallback_title: str,
               warn: Any, counter: TokenCounter | None = None) -> JudgeReport:
    media = judge_media(request, warn)
    report = run_speaker_judge(
        stable_json=request.stable_json, correction_windows=correction_windows, profile=request.profile,
        client=client, inputs=judge_inputs(request, context_pack, knowledge_root=knowledge_root,
                                           fallback_title=fallback_title),
        media=media, audio=judge_audio(request, media, warn), task_artifact_dir=artifact_dir, task_id=task_id,
        parallel=request.parallel_windows, warn=warn, limits=_limits(media, request.profile.difficulty),
        counter=counter)
    judged = sidecar.read_judgments(request.stable_json, warn=warn)
    append_task_artifact(artifact_dir, kind="speaker_judge_report", task_id=task_id, payload={
        **asdict(report),
        "moved": len(judged.moved) if judged else 0,
        "new_tracks": [f"s{track}" for track in judged.new_tracks] if judged else [],
    })
    if report.stopped:
        warn(f"说话人裁判提前停了（{report.stopped}）：没判的 {report.pending} 窗保留声学标签，续跑时再判")
    return report


def preview_task(request: Any, correction_windows: Sequence[SubtitleWindow], *, knowledge_root: Any,
                 fallback_title: str, counter: TokenCounter) -> dict[str, Any]:
    """The first judge window's prompt, as the dry run shows it, and what the judge would cost in input.
    Calls no model; the candidates are the knowledge base's only (the preparation task has not run)."""
    import json

    segments = json.loads(Path(request.stable_json).read_text(encoding="utf-8")).get("segments", [])
    rows = rows_of(segments)
    windows = split_windows([row.id for row in rows], correction_windows)
    if not windows or not sidecar.judgeable(segments):
        return {"speaker_judge": {"windows": 0, "skipped": "no usable speaker labels"}}
    inputs = judge_inputs(request, None, knowledge_root=knowledge_root, fallback_title=fallback_title)
    messages, tokens = [], 0
    for index, window in enumerate(windows):
        profile = profiles(rows, window)
        layout = audio_layout(rows, window, profile.samples) if request.speaker_judge == "audio" else None
        system, user, _ = judge_prompt(rows, window, title=inputs.title, summary=inputs.summary, cast=inputs.pool,
                                       marks=marks_shown(), profile=profile, audio=layout)
        tokens += counter.count_text(system) + counter.count_text(user)
        if layout is not None:
            tokens += counter.count_audio_seconds(layout.seconds)
        if index == 0:
            messages = [[{"role": "system", "content": system}, {"role": "user", "content": user}]]
    return {"speaker_judge": {"windows": len(windows), "input_tokens": tokens, "candidates": len(inputs.pool)},
            "speaker_judge_messages": messages}
