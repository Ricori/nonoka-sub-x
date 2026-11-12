"""The cloud check's structure-and-statistics comparison (plan section 0.1, rule 3).

Offline: synthetic run directories in the layout `cloud._write_run` leaves,
and, when this machine has them, the two runs recorded under
`test_data/quality-baseline/cloud/` (one AOTI, one eager). Nothing here
reaches Modal.
"""

from __future__ import annotations

import argparse
import io
import json
import zipfile
from pathlib import Path

import pytest

from tests.quality_harness import cloud

RECORDED = cloud.BASELINE_ROOT

ALL_THRESHOLDS = cloud.Thresholds(
    segment_count_ratio=0.0, text_distance_ratio=0.0, speaker_count_delta=0, coverage_delta=0.0,
)


def _npz() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("energy.npy", b"\x93NUMPY\x01\x00" + b"\x00" * 8)
    return buffer.getvalue()


def _segment(start: float, end: float, words: list[tuple[float, float, str]]) -> dict:
    timed = [{"start": a, "end": b, "word": word, "space_before": False, "confidence": 1.0} for a, b, word in words]
    return {"start": start, "end": end, "text": "".join(word for _, _, word in words), "words": timed}


def _lines(*texts: str, length: float = 2.0) -> list[dict]:
    """One segment per text, back to back, one timed word per character."""

    segments, at = [], 0.0
    for text in texts:
        step = length / len(text)
        segments.append(_segment(at, at + length, [(at + i * step, at + (i + 1) * step, char)
                                                     for i, char in enumerate(text)]))
        at += length + 1.0
    return segments


def _write(run_dir: Path, segments: list[dict], *, speakers: list[int] | None = None,
           duration: float = 60.0, skip: tuple[str, ...] = (), overrides: dict[str, bytes] | None = None) -> Path:
    folder = run_dir / "artifacts"
    folder.mkdir(parents=True)
    files = {
        "subtitle-stable.json": json.dumps({"segments": segments, "vad_timeline": {}, "metadata": {}}).encode(),
        "subtitle-aligned.json": json.dumps({"segments": segments}).encode(),
        "subtitle-vad.json": json.dumps({"schema": 1, "audio_duration": duration}).encode(),
        "subtitle-vad-energy.npz": _npz(),
        "subtitle-vocal.ogg": b"OggS" + b"\x00" * 60,
        "subtitle-metadata.json": b"{}",
    }
    handed_back = ["stable_json"]
    if speakers is not None:
        tracks = [{"track": track, "start": 2.0 * index, "end": 2.0 * index + 1.5}
                  for index, track in enumerate(speakers)]
        files["subtitle-diar.json"] = json.dumps({
            "metadata": {"backend": "nemotron3", "track_count": len(set(speakers)),
                         "parameters": {"frame_seconds": 0.02}},
            "tracks": tracks,
        }).encode()
        handed_back.append("diar_json")
    files.update(overrides or {})
    for name, data in files.items():
        if name not in skip:
            (folder / name).write_bytes(data)
    (run_dir / "summary.json").write_text(json.dumps({"ok": True, "handed_back": handed_back}), encoding="utf-8")
    (run_dir / "request.json").write_text(json.dumps({"source": {"duration": duration}}), encoding="utf-8")
    return run_dir


def _compare(baseline: Path, run: Path) -> dict:
    return cloud.compare_statistics(cloud.run_statistics(baseline), cloud.run_statistics(run))


# --- the measures ------------------------------------------------------------------


def test_edit_distance() -> None:
    assert cloud.edit_distance("", "") == 0
    assert cloud.edit_distance("abc", "abc") == 0
    assert cloud.edit_distance("kitten", "sitting") == 3
    assert cloud.edit_distance("", "あいう") == 3
    assert cloud.edit_distance("あいうえお", "あいえお") == 1
    assert cloud.edit_distance("prefix-x-suffix", "prefix-yy-suffix") == 2


def test_an_identical_run_measures_zero_everywhere(tmp_path: Path) -> None:
    segments = _lines("こんにちは", "さようなら")
    baseline = _write(tmp_path / "a", segments, speakers=[1, 2, 1])
    run = _write(tmp_path / "b", segments, speakers=[1, 2, 1])
    comparison = _compare(baseline, run)
    assert comparison["missing_artifacts"] == comparison["extra_artifacts"] == []
    assert comparison["problems"] == []
    assert {name: entry["value"] for name, entry in comparison["statistics"].items()} == {
        "segment_count_ratio": 0.0, "text_distance_ratio": 0.0, "speaker_count_delta": 0, "coverage_delta": 0.0,
    }
    assert comparison["reading"] == {"rows": 2, "correction_lines": 2, "timeline_tracks": 3}


def test_lines_cut_elsewhere_with_the_same_words_are_no_text_distance(tmp_path: Path) -> None:
    whole = _lines("あいうえおかきくけこ", length=10.0)[0]
    words = [(word["start"], word["end"], word["word"]) for word in whole["words"]]
    halves = [_segment(0.0, 5.0, words[:5]), _segment(5.0, 10.0, words[5:])]
    comparison = _compare(_write(tmp_path / "a", [whole]), _write(tmp_path / "b", halves))
    statistics = comparison["statistics"]
    assert statistics["text_distance_ratio"]["value"] == 0.0
    assert statistics["text_distance_ratio"]["changed_lines"] == 0
    assert statistics["segment_count_ratio"] == {"baseline": 1, "run": 2, "value": 1.0}


def test_text_distance_counts_changed_lost_and_added_text(tmp_path: Path) -> None:
    baseline = _write(tmp_path / "a", _lines("あいうえお", "かきくけこ"))
    changed = _write(tmp_path / "b", _lines("あいうえお", "かきXけこ"))
    text = _compare(baseline, changed)["statistics"]["text_distance_ratio"]
    assert (text["distance"], text["baseline_characters"], text["value"]) == (1, 10, 0.1)
    assert text["changed_lines"] == 1 and text["changed_line_ratio"] == 0.5

    lost = _write(tmp_path / "c", _lines("あいうえお"))
    assert _compare(baseline, lost)["statistics"]["text_distance_ratio"]["distance"] == 5

    # A line the baseline does not have lands on the nearest baseline line.
    added = _write(tmp_path / "d", _lines("あいうえお", "かきくけこ", "さしすせそ"))
    assert _compare(baseline, added)["statistics"]["text_distance_ratio"]["distance"] == 5


def test_speakers_are_measured_only_where_the_baseline_diarized(tmp_path: Path) -> None:
    segments = _lines("あいうえお")
    assert "speaker_count_delta" not in _compare(_write(tmp_path / "a", segments),
                                                  _write(tmp_path / "b", segments))["statistics"]
    three = _compare(_write(tmp_path / "c", segments, speakers=[1, 2]),
                     _write(tmp_path / "d", segments, speakers=[1, 2, 3]))["statistics"]["speaker_count_delta"]
    assert three == {"baseline": 2, "run": 3, "value": 1}


def test_coverage_is_the_union_of_the_lines_over_the_audio(tmp_path: Path) -> None:
    overlapping = [_segment(0.0, 4.0, [(0.0, 4.0, "あ")]), _segment(3.0, 6.0, [(3.0, 6.0, "い")])]
    stats = cloud.run_statistics(_write(tmp_path / "a", overlapping, duration=12.0))
    assert stats["covered_sec"] == 6.0 and stats["coverage"] == 0.5
    shorter = [_segment(0.0, 4.0, [(0.0, 4.0, "あ")]), _segment(3.0, 4.5, [(3.0, 4.5, "い")])]
    delta = _compare(tmp_path / "a", _write(tmp_path / "b", shorter, duration=12.0))["statistics"]["coverage_delta"]
    assert delta == {"baseline": 0.5, "run": 0.375, "value": 0.125}


# --- structure ---------------------------------------------------------------------


def test_a_missing_or_extra_artifact_fails_whatever_the_thresholds(tmp_path: Path) -> None:
    segments = _lines("あいうえお")
    baseline = _write(tmp_path / "a", segments)
    missing = cloud.judge(_compare(baseline, _write(tmp_path / "b", segments, skip=("subtitle-vad-energy.npz",))),
                          ALL_THRESHOLDS)
    assert missing["failures"] == ["artifact missing: subtitle-vad-energy.npz"]
    extra = cloud.judge(_compare(baseline, _write(tmp_path / "c", segments, speakers=[1])), ALL_THRESHOLDS)
    assert "artifact not in the baseline: subtitle-diar.json" in extra["failures"]
    assert "handed back, not in the baseline: diar_json" in extra["failures"]


@pytest.mark.parametrize(("name", "data", "expected"), [
    ("subtitle-vad.json", b"{not json", "subtitle-vad.json: JSONDecodeError"),
    ("subtitle-vocal.ogg", b"RIFF....", "subtitle-vocal.ogg: no Ogg page at the start"),
    ("subtitle-vad-energy.npz", b"PK-not-a-zip", "subtitle-vad-energy.npz: not a zip archive"),
    ("subtitle-stable.json", json.dumps({"segments": []}).encode(), "-stable.json has no segments"),
    ("subtitle-stable.json", json.dumps({"segments": [{"start": 1.0, "text": "あ"}]}).encode(),
     "-stable.json segment 0: no numeric start/end or no text"),
])
def test_a_malformed_artifact_fails(tmp_path: Path, name: str, data: bytes, expected: str) -> None:
    segments = _lines("あいうえお")
    verdict = cloud.judge(_compare(_write(tmp_path / "a", segments),
                                   _write(tmp_path / "b", segments, overrides={name: data})), ALL_THRESHOLDS)
    assert any(expected in failure for failure in verdict["failures"]), verdict["failures"]


def test_a_stable_the_correction_half_cannot_read_fails(tmp_path: Path) -> None:
    segments = _lines("あいうえお")
    unreadable = json.dumps({"segments": "not a list"}).encode()
    verdict = cloud.judge(_compare(_write(tmp_path / "a", segments),
                                   _write(tmp_path / "b", segments, overrides={"subtitle-stable.json": unreadable})),
                          ALL_THRESHOLDS)
    assert any("the correction half cannot read the stable" in failure for failure in verdict["failures"]), \
        verdict["failures"]


def test_a_handed_back_result_missing_fails(tmp_path: Path) -> None:
    segments = _lines("あいうえお")
    baseline = _write(tmp_path / "a", segments, speakers=[1, 2])
    run = _write(tmp_path / "b", segments, speakers=[1, 2])
    (run / "summary.json").write_text(json.dumps({"ok": True, "handed_back": ["stable_json"]}), encoding="utf-8")
    assert cloud.judge(_compare(baseline, run), ALL_THRESHOLDS)["failures"] == ["not handed back: diar_json"]


# --- thresholds --------------------------------------------------------------------


def test_the_shipped_thresholds_are_the_approved_ones() -> None:
    # Plan section 0.1 rule 3 / appendix C item 3: owner-approved on 2026-11-12 (WP-C option A).
    # Changing one is the owner's decision, never a way to let a run through.
    assert cloud.THRESHOLDS == cloud.Thresholds(segment_count_ratio=0.02, text_distance_ratio=0.005,
                                                speaker_count_delta=0, coverage_delta=0.002)


def test_unapproved_thresholds_leave_every_statistic_undecided(tmp_path: Path) -> None:
    segments = _lines("あいうえお")
    verdict = cloud.judge(_compare(_write(tmp_path / "a", segments, speakers=[1, 2]),
                                   _write(tmp_path / "b", segments, speakers=[1, 2])), cloud.Thresholds())
    assert verdict["failures"] == []
    assert verdict["undecided"] == ["segment_count_ratio", "text_distance_ratio", "speaker_count_delta",
                                    "coverage_delta"]


def test_a_statistic_over_its_threshold_fails_and_one_within_passes(tmp_path: Path) -> None:
    baseline = _write(tmp_path / "a", _lines("あいうえお", "かきくけこ"))
    run = _write(tmp_path / "b", _lines("あいうえお", "かきXけこ"))
    comparison = _compare(baseline, run)
    within = cloud.Thresholds(segment_count_ratio=0.0, text_distance_ratio=0.1, coverage_delta=0.0)
    verdict = cloud.judge(comparison, within)
    assert (verdict["failures"], verdict["undecided"]) == ([], [])
    over = cloud.Thresholds(segment_count_ratio=0.0, text_distance_ratio=0.05, coverage_delta=0.0)
    assert cloud.judge(comparison, over)["failures"] == ["text_distance_ratio = 0.1 > 0.05"]


# --- the gate ----------------------------------------------------------------------


def _gate_against(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_segments: list[dict],
                  thresholds: cloud.Thresholds) -> object:
    from scripts import quality_gate

    root = tmp_path / "baseline"
    for name in cloud.SAMPLES:
        _write(root / name / "run-1", _lines("あいうえお", "かきくけこ"))
    (root / "report.json").write_text(json.dumps({"samples": {name: {"runs": [{}]} for name in cloud.SAMPLES}}),
                                      encoding="utf-8")

    def fake_check(scratch: Path) -> dict:
        result = {}
        for name in cloud.SAMPLES:
            _write(scratch / name, run_segments)
            result[name] = {"run": {"ok": True, "separations": [{"accel": "aoti"}]},
                            "baseline": "run-1", "baseline_why": "run-1 (aoti)",
                            "statistics": cloud._statistics_between(root / name / "run-1", scratch / name)}
        return result

    monkeypatch.setattr(cloud, "BASELINE_ROOT", root)
    monkeypatch.setattr(cloud, "issues", lambda: [])
    monkeypatch.setattr(cloud, "check", fake_check)
    monkeypatch.setattr(cloud, "THRESHOLDS", thresholds)
    return quality_gate.check_cloud(argparse.Namespace(cloud=True, record=None, keep=False))


def test_the_gate_is_undecided_until_thresholds_are_approved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result = _gate_against(tmp_path, monkeypatch, _lines("あいうえお", "かきくけこ"), cloud.Thresholds())
    assert result.status == "UNDECIDED", (result.summary, result.details)


def test_the_gate_passes_within_and_fails_over_approved_thresholds(tmp_path: Path,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    approved = cloud.Thresholds(segment_count_ratio=0.0, text_distance_ratio=0.1, speaker_count_delta=0,
                                coverage_delta=0.0)
    passed = _gate_against(tmp_path / "pass", monkeypatch, _lines("あいうえお", "かきXけこ"), approved)
    assert passed.status == "PASS", (passed.summary, passed.details)
    failed = _gate_against(tmp_path / "fail", monkeypatch, _lines("あいXXお", "かきXけこ"), approved)
    assert failed.status == "FAIL", (failed.summary, failed.details)


# --- which baseline run --------------------------------------------------------------


def test_the_accelerator_comes_from_the_separations_then_the_vocal_leg() -> None:
    assert cloud.run_accel({"separations": [{"accel": "eager"}, {"accel": "eager"}]}) == "eager"
    assert cloud.run_accel({"separations": [{"accel": "eager"}, {"accel": "aoti"}]}) is None
    assert cloud.run_accel({"separations": [], "legs": {"vocal": {"accel": {"backend": "aoti"}}}}) == "aoti"
    assert cloud.run_accel({}) is None


def _baseline_with(root: Path, name: str) -> None:
    for run, accel in cloud.BASELINE_RUNS.items():
        _write(root / name / run, _lines("あいうえお"))
        (root / name / run / "summary.json").write_text(
            json.dumps({"ok": True, "handed_back": ["stable_json"], "separations": [{"accel": accel}]}),
            encoding="utf-8")


def test_each_run_is_compared_with_the_baseline_run_of_its_accelerator(tmp_path: Path,
                                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cloud, "BASELINE_ROOT", tmp_path)
    _baseline_with(tmp_path, "sample")
    assert cloud.baseline_for("sample", {"separations": [{"accel": "aoti"}]}) == (
        tmp_path / "sample" / "run-1", "run-1 (aoti)")
    assert cloud.baseline_for("sample", {"separations": [{"accel": "eager"}]}) == (
        tmp_path / "sample" / "run-2", "run-2 (eager)")
    assert cloud.baseline_for("sample", {"separations": [{"accel": "cudagraph"}]})[0] is None
    assert cloud.baseline_for("sample", {})[0] is None


def test_a_good_run_with_no_baseline_of_its_accelerator_is_an_error(tmp_path: Path,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "baseline"
    for name in cloud.SAMPLES:
        _baseline_with(root, name)
    monkeypatch.setattr(cloud, "BASELINE_ROOT", root)
    monkeypatch.setattr(cloud, "issues", lambda: [])

    def fake_run_samples(out: Path, names: list[str]) -> dict:
        for name in names:
            _write(out / name, _lines("あいうえお"))
        return {name: {"ok": True, "separations": [{"accel": "aoti" if index else "unknown"}]}
                for index, name in enumerate(names)}

    monkeypatch.setattr(cloud, "run_samples", fake_run_samples)
    first, second = list(cloud.SAMPLES)
    result = cloud.check(tmp_path / "scratch")
    assert result[first] == {"run": {"ok": True, "separations": [{"accel": "unknown"}]},
                             "error": f"no baseline run of {first} was made with the unknown separator"}
    assert (result[second]["baseline"], result[second]["baseline_why"]) == ("run-1", "run-1 (aoti)")


# --- the recorded runs (this machine only) -----------------------------------------


@pytest.mark.parametrize("sample", sorted(cloud.SAMPLES))
def test_the_recorded_aoti_and_eager_runs_are_structurally_the_same(sample: str) -> None:
    first, second = RECORDED / sample / "run-1", RECORDED / sample / "run-2"
    if not (first / "artifacts").is_dir() or not (second / "artifacts").is_dir():
        pytest.skip(f"no recorded cloud runs at {RECORDED / sample}")
    assert cloud.baseline_for(sample, json.loads((first / "summary.json").read_text(encoding="utf-8")))[0] == first
    assert cloud.baseline_for(sample, json.loads((second / "summary.json").read_text(encoding="utf-8")))[0] == second
    comparison = _compare(first, second)
    # Structure only: the thresholds hold within one accelerator, and these are AOTI and eager.
    verdict = cloud.judge(comparison, cloud.Thresholds())
    # Same artifacts, every one readable the way the desktop reads it.
    assert verdict["failures"] == [], verdict["lines"]
    assert comparison["baseline_problems"] == []
    statistics = comparison["statistics"]
    expected = {"segment_count_ratio", "text_distance_ratio", "coverage_delta"}
    if sample == "sample_multi_person":
        expected.add("speaker_count_delta")
    assert set(statistics) == expected
    # The two runs differ (plan 2.10), but not wholesale.
    assert 0 < statistics["text_distance_ratio"]["value"] < 0.5
    # A run against itself measures zero and passes the approved thresholds.
    assert all(entry["value"] == 0 for entry in _compare(first, first)["statistics"].values())
    assert cloud.judge(_compare(first, first))["failures"] == []
