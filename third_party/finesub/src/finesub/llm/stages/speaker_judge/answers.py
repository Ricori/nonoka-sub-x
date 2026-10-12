"""Reading the judge's replies, refusing anything that is not an answer, and telling a cut-off one.

A refused reply is retried (agy's answers came back malformed about one time in five in the
experiments); only what the sidecar needs survives: results, never `keep` -- a word that only means
something inside this prompt (the sidecar: `docs/llm_harness_behavior.md`「说话人裁判」).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ....speech.diarization.judgments import acoustic_main
from .prompt import Row
from .windows import JudgeWindow

_BLOCK = r"<{0}>(.*?)</{0}>"
_COMPOUND = re.compile(r"s(\d+)(?:\+s\d+)*")


class AnswerError(ValueError):
    """The reply did not answer what was asked."""


@dataclass(frozen=True)
class Verdict:
    kind: str  # keep / track / new / abstain
    why: str = ""
    track: int | None = None
    label: str | None = None


def _block(text: str, name: str) -> str:
    match = re.search(_BLOCK.format(name), text or "", re.S)
    if match is None:
        raise AnswerError(f"reply has no <{name}> block")
    return match.group(1)


def parse_judge(text: str, ids: Sequence[str], tracks: Sequence[int]) -> dict[str, Verdict]:
    """`{id: verdict}` covering exactly `ids`. A copied cell counts as its main track (`s5+s4` is s5,
    `??` is keep); a track the profiles do not list is refused -- the answer can only name those."""
    wanted, known = set(ids), set(tracks)
    out: dict[str, Verdict] = {}
    for line in _block(text, "judge").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as error:
            raise AnswerError(f"row is not JSON: {line!r}") from error
        if not isinstance(payload, Mapping):
            raise AnswerError(f"row is not an object: {line!r}")
        key, answer, why = str(payload.get("id", "")), str(payload.get("track", "")).strip(), str(payload.get("why", ""))
        if key not in wanted or key in out:
            raise AnswerError(f"unexpected or repeated id {key!r}")
        if answer in ("keep", "??"):
            out[key] = Verdict("keep", why)
        elif answer == "abstain":
            out[key] = Verdict("abstain", why)
        elif answer.startswith("@") and answer[1:].strip():
            out[key] = Verdict("new", why, label=answer[1:].strip())
        elif (match := _COMPOUND.fullmatch(answer)) is not None and int(match.group(1)) in known:
            out[key] = Verdict("track", why, track=int(match.group(1)))
        else:
            raise AnswerError(f"{key}: {answer!r} is not one of the answers")
    if set(out) != wanted:
        raise AnswerError(f"reply misses {len(wanted - set(out))} of {len(wanted)} rows")
    return out


def parse_tracks(text: str) -> dict[str, str]:
    """The `<tracks>` block's `label = name` lines; fail-soft (names only go to the sidecar)."""
    try:
        body = _block(text, "tracks")
    except AnswerError:
        return {}
    pairs = (line.split("=", 1) for line in body.splitlines() if "=" in line)
    return {key.strip(): value.strip() for key, value in pairs if key.strip()}


def cut_off(text: str) -> bool:
    """A reply that opened `<judge>` and never closed it: what the output ceiling leaves when usage
    cannot say so (the correction's open `<translated>`)."""
    opened = (text or "").find("<judge>")
    return opened >= 0 and "</judge>" not in text[opened:]


def results(rows: Sequence[Row], window: JudgeWindow, verdicts: Mapping[str, Verdict],
            carried: Mapping[int, tuple[str, str, str]] | None = None) -> list[dict[str, Any]]:
    """The sidecar's per-row results for one window, in row order: what changed and the abstentions.
    `from` is the row's acoustic main track when judged, so a reader can tell a verdict on another
    labelling apart; naming the track a row already leads (or the one its mark filled) is a keep.
    A carried track (`carried`: its number -> the window, label and verdict that opened it) is written as
    those, never as the number, which depends on what the earlier windows hold."""
    carried = carried or {}
    out = []
    for row in rows[window.low:window.high]:
        verdict = verdicts[f"q{row.id}"]
        origin = acoustic_main(row.speaker)
        base: dict[str, Any] = {"row": row.id, "from": origin, "why": verdict.why}
        if verdict.kind == "abstain":
            out.append({**base, "abstain": True})
        elif verdict.kind == "new":
            out.append({**base, "label": verdict.label})
        elif verdict.kind == "track" and verdict.track in carried:
            window_id, label, of = carried[verdict.track]
            out.append({**base, "carried": {"window": window_id, "label": label, "of": of}})
        elif verdict.kind == "track":
            current = origin["filled"] if isinstance(origin, dict) else origin
            if verdict.track != current:
                out.append({**base, "track": verdict.track})
    return out
