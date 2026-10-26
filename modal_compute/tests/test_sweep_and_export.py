"""The scratch sweep's plan, and the legacy export's merge and owner rules."""

from __future__ import annotations

import json
import os

from nonoka_x_compute.migrate.export_legacy import (
    library_files,
    merge_records,
    read_envelopes,
    resolve_owner,
    video_owners,
)
from nonoka_x_compute.sweep import SCRATCH_MAX_AGE_SEC, plan_scratch_sweep


DAY = 24 * 60 * 60


def _age(path, seconds_ago, now):
    os.utime(path, (now - seconds_ago, now - seconds_ago))


def test_the_sweep_takes_only_trees_idle_for_a_week(tmp_path) -> None:
    now = 2_000_000_000.0
    tasks = tmp_path / "tasks"
    old = tasks / "job_old"
    fresh = tasks / "job_fresh"
    touched = tasks / "job_touched"
    for directory in (old, fresh, touched):
        (directory / "deep").mkdir(parents=True)
        (directory / "deep" / "file").write_bytes(b"x")
    for path in (old / "deep" / "file", old / "deep", old):
        _age(path, 8 * DAY, now)
    for path in (fresh / "deep" / "file", fresh / "deep", fresh):
        _age(path, 1 * DAY, now)
    # The directory itself looks old, but a file inside it was just written.
    _age(touched / "deep" / "file", 60, now)
    _age(touched / "deep", 8 * DAY, now)
    _age(touched, 8 * DAY, now)
    (tasks / "stray-file").write_bytes(b"x")
    _age(tasks / "stray-file", 30 * DAY, now)

    assert plan_scratch_sweep(tmp_path, now=now) == [old]
    assert SCRATCH_MAX_AGE_SEC == 7 * DAY


def test_the_sweep_of_an_empty_volume_is_empty(tmp_path) -> None:
    assert plan_scratch_sweep(tmp_path, now=0) == []


def _envelope(directory, name, identifier, record):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.json").write_text(json.dumps({"id": identifier, "record": record, "filedAt": "t"}), encoding="utf-8")


def test_dict_entries_win_and_envelopes_fill_the_gaps(tmp_path) -> None:
    filed = tmp_path / "records" / "keys"
    _envelope(filed, "a", "key-a", {"id": "key-a", "remaining": 1})
    _envelope(filed, "b", "key-b", {"id": "key-b", "remaining": 7})
    (filed / "broken.json").write_text("{", encoding="utf-8")
    _envelope(filed, "c", "key-c", {})

    merged = merge_records(
        [("key-a", {"id": "key-a", "remaining": 5}), ("key-d", {"id": "key-d", "remaining": 2}), ("key-e", None)],
        read_envelopes(filed),
    )

    assert merged == [
        {"id": "key-a", "remaining": 5},
        {"id": "key-b", "remaining": 7},
        {"id": "key-d", "remaining": 2},
    ]


def test_a_record_without_an_id_gets_its_key(tmp_path) -> None:
    assert merge_records([("vid_1", {"title": "t"})], {}) == [{"title": "t", "id": "vid_1"}]


def test_the_owner_is_the_record_s_then_the_listing_key_s_then_admin() -> None:
    owners = video_owners([{"id": "key-a", "video_ids": ["vid_1", "vid_2"]}, {"id": "key-b"}])
    assert resolve_owner({"id": "vid_1", "ownerId": "key-z"}, owners) == "key-z"
    assert resolve_owner({"id": "vid_2"}, owners) == "key-a"
    assert resolve_owner({"id": "vid_3"}, owners) == "admin"


def test_library_files_map_to_their_r2_keys(tmp_path) -> None:
    entry = tmp_path / "library" / "key-a" / "vid_1"
    entry.mkdir(parents=True)
    for name in ("stable_json.stable.json", "raw_srt.raw.srt", "final_srt.srt", "record.json", "junk.txt"):
        (entry / name).write_text("x", encoding="utf-8")

    assert sorted(key for _, key in library_files(tmp_path, "key-a", "vid_1")) == [
        "library/key-a/vid_1/final_srt.srt",
        "library/key-a/vid_1/raw_srt.raw.srt",
        "library/key-a/vid_1/stable_json.stable.json",
    ]


def test_files_filed_under_another_directory_are_still_found(tmp_path) -> None:
    entry = tmp_path / "library" / "key-old" / "vid_9"
    entry.mkdir(parents=True)
    (entry / "annotated_csv.annotated.csv").write_text("x", encoding="utf-8")

    ((path, key),) = library_files(tmp_path, "key-new", "vid_9")
    assert path == entry / "annotated_csv.annotated.csv"
    assert key == "library/key-new/vid_9/annotated_csv.annotated.csv"
    assert library_files(tmp_path, "key-new", "vid_missing") == []
