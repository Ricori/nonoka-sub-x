"""Knowledge-base cases K1-K8 (WP-K): every path that reads or writes the knowledge base.

The other checks run with no knowledge base or a fresh empty one, so a change
to how the engine reaches the base (WP-K moves every access behind the
knowledge package's protocol) would go unseen there. These cases run on a base
with content -- `fixture_base` builds it from `tests/fixtures/quality/knowledge/base/`
-- and compare what the engine does with it:

- K1 `k1-read-correction`: the LLM half from a stable with the base read
  (subjects picked, a style in the base, the speaker judge on a two-track
  timeline). Every request byte for byte, the rendered subtitles, and the base
  afterwards. K3 (signal events: matched / exposed / landed / style injected)
  and K6 (a hand-edited `rendered/` file absorbed when the run starts) are
  part of that export.
- K2 `k2-update-writeback`: the same with `knowledge: update` and an answer
  that writes back (an edited term line, an appended line, a new entry): the
  requests, the base afterwards and `task-artifacts.jsonl`.
- K4 `k4-asr-context`: call equivalence to `raw-srt` with the base read: the
  ASR context text handed to `run_vad_asr` and the context report in the run
  metadata.
- K5 `k5-ui-operations`: the desktop's knowledge page through
  `python -m nonoka_x.knowledge_worker` (list, read, save, a stale save, an
  invalid save, a save while another process holds the write lock, a read of
  a gone entry): every response and the base afterwards.
- K7 `k7-mcp-tools`: the agent MCP server's `kb_*` tools on the base: every
  reply and the exposure events they book.
- K8 `k8-prepare-subjects`: `worker.prepare_knowledge_subjects` on an empty
  base and on the fixture: return values, errors and the base afterwards.

What is compared of a base (`export_base`): every table row of the store, and
every file under `rendered/`. Left out: wall-clock columns (`created_at`,
`resolved_at`), the per-row autoincrement ids, the dedupe hashes and a
revision's `proposal_hash` (they follow from insertion order or from the ids
below). Node ids a run creates are
random (`uuid4`); they are numbered by first appearance, after the rows are
ordered without them. The fixture's own ids are fixed. The knowledge base's
"today" (an entry's 最近更新日期) is `GATE_TODAY` in every process of a case
(`knowledge_today/sitecustomize.py`), so the rendered files and their hashes do
not depend on the day of the run.

Run as `python -m tests.quality_harness.knowledge --<role> <spec>` for the
child processes; everything else is the parent side.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Mapping

from . import ENGINES, FIXTURES, ROOT
from .calls import Normalizer, manifest_record, render
from .calls import hermetic_environment as _calls_environment
from .replay import GateResponder

FIXTURE_DIR = FIXTURES / "knowledge"
BASE_SOURCE = FIXTURE_DIR / "base"
BASELINE_DIR = FIXTURE_DIR / "cases"
STABLE_FIXTURE = ROOT / "tests" / "fixtures" / "finesub" / "sample-stable.json"

#: The fixture entries: (fixed local id, name, category, intro, source file).
FIXTURE_ENTRIES = (
    ("fixture-streamer", "柊優花", "streamer", "游戏主播，常玩 RPG。", "streamer.md"),
    ("fixture-game", "星屑クエスト", "common", "2025 年发售的 RPG 游戏。", "common.md"),
    ("fixture-style", "门禁字幕组", "style", "门禁用的风格", "style.md"),
)
#: The hand edit a person made in `rendered/` since the last refresh (K6).
HAND_EDIT_FILE = "common/星屑クエスト.md"
HAND_EDIT_AFTER = "マタネ村|回见村||初始村庄"
HAND_EDIT_LINE = "- ホシノ城|星之城||魔王的城堡"

SUBJECT_STREAMER = {"id": "fixture-streamer", "kind": "streamer", "subject": "柊優花", "aliases": "", "description": ""}
SUBJECT_GAME = {"id": "fixture-game", "kind": "work", "subject": "星屑クエスト", "aliases": "", "description": ""}
EXTRA_INFO = "这期是柊優花玩星屑クエスト，ミラ和ゴザイマス都出场了。"

#: Store columns that describe when or in what order a row was written, not what
#: it says, or that hash the run's random node ids (`proposal_hash` digests the
#: applied ops, ids included; the revision's `note` names the proposal text's own
#: digest, which is compared).
DROPPED_COLUMNS = frozenset({
    "created_at", "resolved_at", "dedupe_key", "event_id", "evidence_id", "decision_id", "ack_id",
    "proposal_hash",
})
_UUID4 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:\d{2}|Z)?")


#: The knowledge base's "today" in every K case (`knowledge_today/sitecustomize.py`).
GATE_TODAY = "2026-01-02"
TODAY_HOOK = Path(__file__).resolve().parent / "knowledge_today"


def hermetic_environment(root: Path) -> dict[str, str]:
    """`calls.hermetic_environment`, with the knowledge base's date fixed for the run."""

    environment = _calls_environment(root)
    environment["PYTHONPATH"] = os.pathsep.join([str(TODAY_HOOK), environment["PYTHONPATH"]])
    environment["NONOKA_QUALITY_KNOWLEDGE_TODAY"] = GATE_TODAY
    return environment


# --- the fixture base (child) ----------------------------------------------------


def build_main(spec: Mapping[str, Any]) -> int:
    """Build the fixture base at `spec["root"]` with FineSub's own write paths.

    Ids are made deterministic here, in the build only: the subjects get fixed
    ids, and the nodes `edit_subject` creates for their lines get counted ones.
    """

    import itertools
    import uuid

    counter = itertools.count(1)
    uuid.uuid4 = lambda: uuid.UUID(int=next(counter))  # type: ignore[assignment]
    root = Path(spec["root"])
    os.environ["FINESUB_KNOWLEDGE_ROOT"] = str(root)
    from finesub.llm.knowledge.node.edit import edit_subject
    from finesub.llm.knowledge.node.repo import KnowledgeRepo

    repo = KnowledgeRepo.open(root)
    with repo.store.begin("user", note="quality-gate fixture") as txn:
        for local_id, surface, category, intro, _source in FIXTURE_ENTRIES:
            payload = {"surface": surface, "intro": intro, "category": category}
            if category == "common":
                payload["entry_type"] = "游戏"
            txn.create_node(local_id, "subject", payload)
    for local_id, _surface, _category, _intro, source in FIXTURE_ENTRIES:
        report = edit_subject(
            repo, repo.store.node(local_id), (BASE_SOURCE / source).read_text(encoding="utf-8"),
            note="quality-gate fixture",
        )
        if report.rejected or report.staged:
            raise RuntimeError(f"fixture entry {source} did not apply cleanly: {report}")
    repo.refresh_rendered()
    edited = root / "rendered" / HAND_EDIT_FILE
    lines = edited.read_text(encoding="utf-8").splitlines(keepends=True)
    index = next(i for i, line in enumerate(lines) if HAND_EDIT_AFTER in line)
    lines.insert(index + 1, HAND_EDIT_LINE + "\n")
    edited.write_text("".join(lines), encoding="utf-8")
    KnowledgeRepo.forget()
    if spec.get("diar"):
        # A two-track timeline for the stable, the way a cloud task with a
        # timeline continues (as `calls._setup_cloud_diar`).
        from .calls import _setup_cloud_diar

        _setup_cloud_diar(Path(spec["diar"]))
    return 0


def _child(role: str, spec: Mapping[str, Any], root: Path, *, timeout: int = 600) -> subprocess.CompletedProcess:
    spec_path = root / f"{role}.spec.json"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, "-m", "tests.quality_harness.knowledge", f"--{role}", str(spec_path)],
        cwd=ROOT, env=hermetic_environment(root), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"knowledge {role} child failed ({completed.returncode}):\n{completed.stderr[-4000:]}")
    return completed


def fixture_base(root: Path, knowledge_root: Path, *, diar: Path | None = None) -> None:
    _child("build", {"root": str(knowledge_root), "diar": str(diar) if diar else ""}, root)


# --- exporting a base (parent) -----------------------------------------------------


def _masked(value: Any) -> str:
    return _UUID4.sub("$UUID", json.dumps(value, ensure_ascii=False, sort_keys=True))


def export_base(knowledge_root: Path) -> dict[str, Any] | None:
    """Every store row (wall-clock and insertion-order columns left out) and every rendered file."""

    store = knowledge_root / "knowledge.sqlite"
    if not store.is_file():
        return None
    connection = sqlite3.connect(str(store))
    connection.row_factory = sqlite3.Row
    try:
        names = [row["name"] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )]
        tables: dict[str, list[dict[str, Any]]] = {}
        for name in names:
            rows = [
                {key: row[key] for key in row.keys() if key not in DROPPED_COLUMNS}
                for row in connection.execute(f'SELECT * FROM "{name}"')
            ]
            tables[name] = sorted(rows, key=_masked)
    finally:
        connection.close()
    rendered = {}
    directory = knowledge_root / "rendered"
    if directory.is_dir():
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                rendered[path.relative_to(directory).as_posix()] = path.read_text(encoding="utf-8")
    return {"tables": tables, "rendered": rendered}


def normalized(record: Any, roots: Mapping[str, Path]) -> Any:
    """Roots to placeholders, then run-created ids numbered and timestamps named."""

    text = json.dumps(Normalizer(roots).value(record), ensure_ascii=False, sort_keys=True)
    numbers: dict[str, str] = {}

    def number(match: re.Match[str]) -> str:
        return numbers.setdefault(match.group(0), f"$UUID{len(numbers) + 1}")

    text = _UUID4.sub(number, text)
    text = _TIMESTAMP.sub("$TIME", text)
    return json.loads(text)


# --- K1 / K2: the LLM half on the fixture base ------------------------------------


def _handles(task: str) -> list[tuple[str, str]]:
    """(handle, the line it sits on) for every `<!-- @kN -->` in a prompt."""

    return [
        (match.group(2), match.group(1).strip())
        for match in re.finditer(r"^(.*?)<!-- (@k\d+) -->", task, re.M)
    ]


class KnowledgeResponder(GateResponder):
    """The gate's fixed answers, plus a knowledge update that writes something back.

    The proposals are built from the handles the update request shows, so the
    answer is a function of the request, as every other gate answer is.
    """

    def answer(self, messages: list[dict[str, Any]], kwargs: Mapping[str, Any]) -> str:
        from .replay import _text_of

        users = [_text_of(message) for message in messages if message.get("role") == "user"]
        task = next((text for text in reversed(users) if "<knowledge_proposals>" in text), "")
        if task and not any(marker in task for marker in ("<asr_result>", "<proofread>", "<context_pack>", "<judge>")):
            return self.proposals(task)
        system = "\n".join(_text_of(message) for message in messages if message.get("role") == "system")
        answered = any(message.get("role") == "assistant" for message in messages)
        if not answered and "<requested_entries>" in system and any("<asr_result>" in text for text in users):
            return self.request_turn(system)
        return super().answer(messages, kwargs)

    @staticmethod
    def request_turn(system: str) -> str:
        """A window's first reply: ask for the first common entry by name, pick the first style candidate."""

        out = ["<window_notes>", "</window_notes>", "<requested_entries>"]
        common = re.search(r"<common_index>(.*?)</common_index>", system, re.S)
        first = re.search(r"^- (.+?)(?: \[[^\]]+\])? \|", common.group(1), re.M) if common else None
        if first:
            out.append(first.group(1))
        out += ["</requested_entries>", "<keep_entries>", "</keep_entries>"]
        if "<style_selection>" in system:
            candidates = system.split("候选（每行一套", 1)[-1]
            style = re.search(r"^- ([^｜\n]+)｜", candidates, re.M)
            choice = {"selected": style.group(1) if style else "", "reason": "门禁回放的固定选择：第一套候选"}
            out.append("<style_selection>" + json.dumps(choice, ensure_ascii=False) + "</style_selection>")
        return "\n".join(out)

    @staticmethod
    def proposals(task: str) -> str:
        handles = _handles(task)
        rows = []
        mira = next((handle for handle, line in handles if line.startswith("ミラ|米拉|Mira|")), None)
        if mira:
            rows.append({"op": "update", "id": mira, "line": "ミラ|米拉|Mira|主角的搭档，擅长弓箭。",
                         "reason": "门禁回放的固定提案：改一条术语行"})
        game = next((handle for handle, line in handles if line.startswith("# 星屑クエスト")), None)
        if game:
            rows.append({"op": "append_lines", "entry": game, "section": "角色",
                         "content": "ルナ|露娜||新登场的商人。", "reason": "门禁回放的固定提案：追加一行"})
        rows.append({"op": "create_entry", "category": "common", "entry": "月影オンライン", "entry_type": "游戏",
                     "intro": "主播提到的另一款网游。", "aliases": ["月影"],
                     "reason": "门禁回放的固定提案：新建条目，与已有条目无关"})
        if "<rejected" not in task and "被拒" not in task:
            # One the apply refuses (a handle the request never showed), so
            # the update's repair round runs; the repair answer leaves it out.
            rows.append({"op": "update", "id": "@k999", "line": "x|y||z", "reason": "门禁回放的固定提案：无效句柄"})
        body = "\n".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in rows)
        return f"<knowledge_proposals>\n{body}\n</knowledge_proposals>"


REPLAY_CASES: dict[str, dict[str, Any]] = {
    "k1-read-correction": {
        "request": {
            "target": "final-srt",
            "language": "ja",
            "knowledge": "read",
            "knowledge_subjects": [SUBJECT_STREAMER, SUBJECT_GAME],
            "diarization": {"level": "judge-text"},
            "correction": {"media": "text", "retrieval": "none", "difficulty": "quality", "extra_info": EXTRA_INFO},
        },
    },
    "k1b-read-windows-named-style": {
        # The normal flow (`fast: off`): preparation, then several windows in
        # parallel over a longer stable that says the base's names, and the
        # style named in the config instead of picked by the model.
        "stable": FIXTURE_DIR / "stable-long.json",
        "seconds": 125.0,
        "config_append": '[chunking]\nmax_window_subtitle_tokens = 700\n\n[llm]\nstyle = "门禁字幕组"\n',
        "request": {
            "target": "final-srt",
            "language": "ja",
            "knowledge": "read",
            "knowledge_subjects": [SUBJECT_STREAMER],
            "correction": {"media": "text", "retrieval": "none", "difficulty": "quality", "fast": "off",
                           "extra_info": EXTRA_INFO},
        },
    },
    "k2-update-writeback": {
        # The style named, so the update also judges the style's conventions.
        "config_append": '[llm]\nstyle = "门禁字幕组"\n',
        "request": {
            "target": "final-srt",
            "language": "ja",
            "knowledge": "update",
            "knowledge_subjects": [SUBJECT_STREAMER, SUBJECT_GAME],
            "correction": {"media": "text", "retrieval": "none", "difficulty": "quality", "extra_info": EXTRA_INFO},
        },
    },
}


def run_replay_case(name: str, scratch: Path) -> dict[str, Any]:
    from .replay import write_silence

    case = REPLAY_CASES[name]
    root = (scratch / name).resolve()
    for directory in ("media", "cloud", "data", "tmp"):
        (root / directory).mkdir(parents=True)
    media = write_silence(root / "media" / "节目.wav", seconds=float(case.get("seconds", 6.0)))
    shutil.copyfile(case.get("stable", STABLE_FIXTURE), root / "cloud" / "cloud.stable.json")
    judge = (case["request"].get("diarization") or {}).get("level", "").startswith("judge")
    knowledge_root = root / "data" / "knowledge"
    fixture_base(root, knowledge_root, diar=root / "cloud" / "cloud.diar.json" if judge else None)
    spec = {
        "request": case["request"], "root": str(root), "out": str(root / "record.json"),
        "media_file": str(media), "diar": judge, "config_append": case.get("config_append", ""),
        "responder": "tests.quality_harness.knowledge.KnowledgeResponder",
    }
    (root / "spec.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, "-m", "tests.quality_harness.replay", "--child", str(root / "spec.json")],
        cwd=ROOT, env=hermetic_environment(root), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=600,
    )
    out = root / "record.json"
    if completed.returncode != 0 or not out.is_file():
        raise RuntimeError(f"knowledge case {name} child failed ({completed.returncode}):\n{completed.stderr[-4000:]}")
    raw = json.loads(out.read_text(encoding="utf-8"))
    artifacts = root / "data" / "tasks" / "gate-replay"
    task_artifacts = sorted(artifacts.rglob("task-artifacts.jsonl"))

    def where(path: Path) -> str:
        # Since WP-P1 in the correction package's directory of the task
        # (plan 4.6 item 34); the baselines name it `workspace`.
        return path.relative_to(artifacts).as_posix().replace("native/finesub-correct/", "workspace/", 1)

    record = {
        "case": name,
        "machine": raw["machine"],
        "exit": raw["exit"],
        "failed": raw["failed"],
        "knowledge_warnings": [
            warning for warning in raw["warnings"] if str(warning.get("code", "")).startswith("knowledge")
        ],
        "exchanges": raw["exchanges"],
        "outputs": raw["outputs"],
        "task_artifacts": {
            where(path): [
                {key: value for key, value in json.loads(line).items() if key != "created_at"}
                for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
            ]
            for path in task_artifacts
        },
        "base": export_base(knowledge_root),
    }
    return normalized(record, {"$ROOT": root, "$REPO": ROOT})


# --- K4: the ASR context ------------------------------------------------------------


K4_REQUEST = {
    "schema": 1,
    "provider": "local",
    "source": {"kind": "local_file", "path": "$MEDIA", "title": "节目"},
    "language": "ja",
    "target": "raw-srt",
    "knowledge": "read",
    "knowledge_subjects": [SUBJECT_GAME],
    # Names no streamer: her terms are matched one by one (`match_terms`),
    # the picked game whole (`load_preinjected_entries`).
    "correction": {"extra_info": "这期提到了ミラ，还有粉丝ほしくず。"},
}


def run_asr_context_case(name: str, scratch: Path) -> dict[str, Any]:
    from urllib.parse import urlparse
    from urllib.request import url2pathname

    root = (scratch / name).resolve()
    for directory in ("media", "cloud", "data", "tmp"):
        (root / directory).mkdir(parents=True)
    (root / "media" / "节目.m4a").write_bytes(b"quality-gate audio placeholder")
    fixture_base(root, root / "data" / "knowledge")
    spec = {"request": K4_REQUEST, "settings": "compat", "setup": [], "root": str(root), "out": str(root / "record.json")}
    (root / "spec.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, "-m", "tests.quality_harness.calls", "--child", str(root / "spec.json")],
        cwd=ROOT, env=hermetic_environment(root), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=300,
    )
    out = root / "record.json"
    if completed.returncode != 0 or not out.is_file():
        raise RuntimeError(f"knowledge case {name} child failed ({completed.returncode}):\n{completed.stderr[-4000:]}")
    raw = json.loads(out.read_text(encoding="utf-8"))
    normalizer = Normalizer({"$ROOT": root, "$REPO": ROOT})
    asr = [call for call in raw.get("calls", []) if "asr_context" in call.get("arguments", {})]
    metadata_report = None
    entry = ((raw.get("manifest") or {}).get("artifacts") or {}).get("metadata_json")
    if entry:
        metadata = json.loads(Path(url2pathname(urlparse(entry["uri"]).path)).read_text(encoding="utf-8"))
        metadata_report = metadata.get("asr_context")
    record = {
        "case": name,
        "exit": raw.get("exit"),
        "failed": raw.get("failed"),
        "asr_context": [
            {"entry": call["entry"], "asr_context": call["arguments"]["asr_context"]} for call in asr
        ],
        "metadata_asr_context": metadata_report,
        "manifest": manifest_record(raw["manifest"], normalizer) if raw.get("manifest") else None,
    }
    return normalized(record, {"$ROOT": root, "$REPO": ROOT})


# --- K5: the desktop's knowledge page -------------------------------------------------


#: How long a save waits for a write lock another process holds before it
#: reports the base busy. FineSub waits 90 s; the gate shortens only that wait
#: (`ui_door_main`), so the busy answer arrives in seconds.
UI_LOCK_TIMEOUT_SECONDS = 2.0


def ui_door_hook(spec: Mapping[str, Any], entry: str, context: Mapping[str, Any]) -> None:
    """In the knowledge package's process: the write lock's wait shortened (`UI_LOCK_TIMEOUT_SECONDS`)."""

    from finesub.llm.knowledge import base

    base.KNOWLEDGE_LOCK_TIMEOUT_SECONDS = UI_LOCK_TIMEOUT_SECONDS


def knowledge_process(root: Path, knowledge_root: Path, *, hook: str | None = None) -> Any:
    """The knowledge package's process the way the desktop starts one (`LocalProvider.knowledge`), on `knowledge_root`."""

    from nonoka_x import engine_host

    from .package_child import use_in_worker

    if hook is not None:
        use_in_worker({"hook": hook}, root / "package-child.json")
    package = engine_host.knowledge_package(engine_host.discover(ENGINES))
    run = root / "run"
    run.mkdir(exist_ok=True)
    context = {"schema": 1, "package_id": package.id,
               "paths": {"package_data": str(root / "engines" / package.id), "run": str(run),
                         "knowledge_root": str(knowledge_root)}}
    return engine_host.PackageProcess(package, context)


def ui_door_main(spec: Mapping[str, Any]) -> int:
    """The door `LocalProvider.knowledge` uses (the knowledge package's `manage`), with the lock wait shortened."""

    requests = json.loads(Path(spec["requests"]).read_text(encoding="utf-8"))
    with knowledge_process(Path(spec["root"]), Path(spec["knowledge_root"]),
                           hook="tests.quality_harness.knowledge:ui_door_hook") as process:
        response = process.request("manage", {"request": requests})
    print(json.dumps(response, ensure_ascii=False))
    return 0


def hold_lock_main(spec: Mapping[str, Any]) -> int:
    """Hold the base's write lock, from another process, until stdin closes."""

    from finesub.llm.knowledge.base import knowledge_write_lock

    with knowledge_write_lock(spec["root"]) as acquired:
        print("held" if acquired else "not-held", flush=True)
        sys.stdin.read()
    return 0


def run_ui_case(name: str, scratch: Path) -> dict[str, Any]:
    root = (scratch / name).resolve()
    (root / "tmp").mkdir(parents=True)
    knowledge_root = root / "knowledge"
    fixture_base(root, knowledge_root)
    environment = hermetic_environment(root)
    environment["FINESUB_KNOWLEDGE_ROOT"] = str(knowledge_root)
    door_root = root / "door"
    door_root.mkdir()

    def door(request: Mapping[str, Any]) -> Any:
        requests = door_root / "request.json"
        requests.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        spec = door_root / "ui-door.spec.json"
        spec.write_text(json.dumps({"root": str(door_root), "knowledge_root": str(knowledge_root),
                                    "requests": str(requests)}, ensure_ascii=False), encoding="utf-8")
        completed = subprocess.run(
            [sys.executable, "-m", "tests.quality_harness.knowledge", "--ui-door", str(spec)],
            cwd=ROOT, env=environment, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=120,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"knowledge door failed ({completed.returncode}):\n{completed.stderr[-4000:]}")
        return json.loads(completed.stdout.strip().splitlines()[-1])

    steps: list[dict[str, Any]] = []

    def step(label: str, request: Mapping[str, Any]) -> Any:
        response = door(request)
        steps.append({"step": label, "request": request, "response": response})
        return response

    step("list", {"action": "list"})
    before = step("read", {"action": "read", "id": "fixture-game"})["result"]
    edited = before["content"].replace("2025 年发售的 RPG 游戏。", "2025 年发售的长篇 RPG 游戏。")
    step("save", {"action": "save", "id": "fixture-game", "version": before["version"], "content": edited})
    step("save-stale", {"action": "save", "id": "fixture-game", "version": before["version"], "content": before["content"]})
    current = step("read-after-save", {"action": "read", "id": "fixture-game"})["result"]
    step("save-invalid", {"action": "save", "id": "fixture-game", "version": current["version"], "content": "invalid"})
    step("save-empty", {"action": "save", "id": "fixture-game", "version": current["version"], "content": "  "})
    holder_spec = root / "hold.spec.json"
    holder_spec.write_text(json.dumps({"root": str(knowledge_root)}), encoding="utf-8")
    holder = subprocess.Popen(
        [sys.executable, "-m", "tests.quality_harness.knowledge", "--hold-lock", str(holder_spec)],
        cwd=ROOT, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8",
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
        busy = current["content"].replace("2025 年发售的长篇 RPG 游戏。", "被锁挡下的编辑。")
        step("save-busy", {"action": "save", "id": "fixture-game", "version": current["version"], "content": busy})
    finally:
        assert holder.stdin is not None
        holder.stdin.close()
        holder.wait(timeout=60)
    step("list-after", {"action": "list"})
    step("read-streamer", {"action": "read", "id": "fixture-streamer"})
    step("read-gone", {"action": "read", "id": "no-such-entry"})
    step("unknown-action", {"action": "delete", "id": "fixture-game"})
    record = {"case": name, "steps": steps, "base": export_base(knowledge_root)}
    return normalized(record, {"$ROOT": root, "$REPO": ROOT})


# --- K7: the agent MCP server's knowledge tools -----------------------------------


class _RecordingRuntime:
    """What `HarnessToolServer` asks of the assignment runtime for the kb tools, recorded."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[dict[str, Any]] = []

    def record_kb_read(self, **fields: Any) -> dict[str, Any]:
        self.calls.append({"record_kb_read": fields})
        return {}

    def record_pull(self, **fields: Any) -> dict[str, Any]:
        self.calls.append({"record_pull": fields})
        return {"owed_blocks": []}


MCP_CALLS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("kb_index", {}),
    ("kb_search", {"query": "ミラ"}),
    ("kb_search", {"query": "ございます", "entry": "星屑クエスト"}),
    ("kb_search", {"query": "ミラ", "entry": "没有这个条目"}),
    ("kb_read", {"entry": "星屑クエスト"}),
    ("kb_read", {"entry": "ゆうか", "sections": ["频道用语"]}),
    ("kb_read", {"entry": "style/门禁字幕组"}),
    ("kb_read", {"entry": "没有这个条目"}),
    ("kb_read_node", {"handle": "$FIRST_SEARCH_HANDLE"}),
    ("kb_read_node", {"handle": "@k999"}),
    ("kb_validate", {"proposals": (
        "<knowledge_proposals>\n"
        '{"op":"append_lines","category":"common","entry":"星屑クエスト","section":"地名","content":"ホシノ城|星之城||魔王的城堡","reason":"门禁"}\n'
        '{"op":"create_entry","category":"common","entry":"月影オンライン","entry_type":"游戏","intro":"另一款网游。","reason":"门禁"}\n'
        '{"op":"update","id":"@k999","line":"x|y||z","reason":"门禁"}\n'
        "</knowledge_proposals>"
    )}),
)


def mcp_main(spec: Mapping[str, Any]) -> int:
    # The MCP server reaches the base through the task's knowledge process, which the
    # core starts and names to the correction process (the endpoint the server inherits).
    process = knowledge_process(Path(spec["runtime_root"]).parent, Path(spec["knowledge_root"]))
    endpoint = process.request("serve", {"owner": os.getpid()})
    process.detach()
    os.environ["FINESUB_KNOWLEDGE_ENDPOINT"] = json.dumps(endpoint)

    from finesub.llm.agent.agent_mcp_server import KB_TOOL_NAMES, HarnessToolServer

    runtime = _RecordingRuntime(Path(spec["runtime_root"]))
    server = HarnessToolServer(
        runtime,  # type: ignore[arg-type]
        assignment_id="gate-assignment", worker_id="gate-worker", session_id="gate-session",
        instance_id="gate-instance", tools=list(KB_TOOL_NAMES),
    )
    server._task = {"task_id": "gate-task", "lease_generation": 1}
    server._manifest = {
        "metadata": {
            "kb_tools": "propose",
            "kb_root": spec["knowledge_root"],
            "knowledge_identity": f"rev:{spec['rev']}",
            "kb_signal_task": "gate-mcp-task",
            "kb_signal_window": "gate-window",
        },
        "required_blocks": [],
    }
    replies = []
    first_handle = ""
    for index, (tool, arguments) in enumerate(MCP_CALLS):
        if arguments.get("handle") == "$FIRST_SEARCH_HANDLE":
            arguments = {**arguments, "handle": first_handle}
        payload, error = server._dispatch(tool, arguments, request_id=f"gate-{index}")
        if tool == "kb_search" and payload and payload.get("matches") and not first_handle:
            first_handle = str(payload["matches"][0]["handle"])
        replies.append({"tool": tool, "arguments": arguments, "payload": payload, "error": error})
    Path(spec["out"]).write_text(
        json.dumps({"replies": replies, "runtime_calls": runtime.calls}, ensure_ascii=False), encoding="utf-8"
    )
    return 0


def run_mcp_case(name: str, scratch: Path) -> dict[str, Any]:
    root = (scratch / name).resolve()
    (root / "tmp").mkdir(parents=True)
    (root / "assignment").mkdir()
    knowledge_root = root / "knowledge"
    fixture_base(root, knowledge_root)
    exported = export_base(knowledge_root) or {}
    rev = max(row["rev"] for row in exported["tables"]["revisions"])
    out = root / "mcp.json"
    _child("mcp", {"knowledge_root": str(knowledge_root), "rev": rev, "runtime_root": str(root / "assignment"),
                   "out": str(out)}, root)
    raw = json.loads(out.read_text(encoding="utf-8"))
    record = {"case": name, "rev": rev, **raw, "base": export_base(knowledge_root)}
    return normalized(record, {"$ROOT": root, "$REPO": ROOT})


# --- K8: subjects prepared before a task ------------------------------------------


PREPARE_STEPS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("update-new", {"knowledge": "update", "knowledge_subjects": [
        {"kind": "streamer", "subject": "猫又おかゆ", "aliases": "Okayu、おかゆ", "description": "Hololive 所属"}]}),
    ("update-again-reuses", {"knowledge": "update", "knowledge_subjects": [
        {"kind": "streamer", "subject": "猫又おかゆ", "aliases": "", "description": ""}]}),
    ("read-by-alias", {"knowledge": "read", "knowledge_subjects": [
        {"kind": "streamer", "subject": "おかゆ", "aliases": "", "description": ""}]}),
    ("read-unknown", {"knowledge": "read", "knowledge_subjects": [
        {"kind": "work", "subject": "没有的作品", "aliases": "", "description": ""}]}),
    ("update-work-and-topic", {"knowledge": "update", "knowledge_subjects": [
        {"kind": "work", "subject": "月影オンライン", "aliases": "月影", "description": ""},
        {"kind": "topic", "subject": "门禁话题", "aliases": "", "description": "测试用话题"}]}),
    ("by-id-gone", {"knowledge": "read", "knowledge_subjects": [
        {"id": "no-such-id", "kind": "streamer", "subject": "不在了", "aliases": "", "description": ""}]}),
    ("none", {"knowledge": "none"}),
)
PREPARE_FIXTURE_STEPS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("fixture-by-id", {"knowledge": "read", "knowledge_subjects": [SUBJECT_STREAMER, SUBJECT_GAME]}),
    ("fixture-update-existing-name", {"knowledge": "update", "knowledge_subjects": [
        {"kind": "streamer", "subject": "ゆうかちゃん", "aliases": "", "description": ""},
        {"kind": "work", "subject": "星屑クエスト", "aliases": "", "description": ""}]}),
)


def prepare_main(spec: Mapping[str, Any]) -> int:
    """What a task's worker asks the knowledge package before it starts (`prepare_subjects`), step by step."""

    from nonoka_x import engine_host, worker

    steps = PREPARE_FIXTURE_STEPS if spec["fixture"] else PREPARE_STEPS
    results = []
    root = Path(spec["out"]).parent / f"prepare-{'fixture' if spec['fixture'] else 'empty'}"
    root.mkdir(exist_ok=True)
    with knowledge_process(root, Path(spec["knowledge_root"])) as process:
        for label, request in steps:
            asked = worker.subject_request(request, f"gate-{label}")
            try:
                prepared = process.request("prepare_subjects", asked) if asked else {"names": [], "created": []}
                outcome: dict[str, Any] = {"result": prepared}
            except engine_host.PackageError as exc:  # the error is the result
                outcome = {"error": {"type": exc.payload.get("type"), "message": exc.payload.get("message")}}
            results.append({"step": label, "request": request, **outcome})
    Path(spec["out"]).write_text(json.dumps(results, ensure_ascii=False), encoding="utf-8")
    return 0


def run_prepare_case(name: str, scratch: Path) -> dict[str, Any]:
    root = (scratch / name).resolve()
    (root / "tmp").mkdir(parents=True)
    record: dict[str, Any] = {"case": name}
    for part, fixture in (("empty", False), ("fixture", True)):
        knowledge_root = root / f"knowledge-{part}"
        if fixture:
            fixture_base(root, knowledge_root)
        out = root / f"prepare-{part}.json"
        _child("prepare", {"knowledge_root": str(knowledge_root), "fixture": fixture, "out": str(out)}, root)
        record[part] = {
            "steps": json.loads(out.read_text(encoding="utf-8")),
            "base": export_base(knowledge_root),
        }
    return normalized(record, {"$ROOT": root, "$REPO": ROOT})


# --- the parent ---------------------------------------------------------------------

CASES: dict[str, Callable[[str, Path], dict[str, Any]]] = {
    "k1-read-correction": run_replay_case,
    "k1b-read-windows-named-style": run_replay_case,
    "k2-update-writeback": run_replay_case,
    "k4-asr-context": run_asr_context_case,
    "k5-ui-operations": run_ui_case,
    "k7-mcp-tools": run_mcp_case,
    "k8-prepare-subjects": run_prepare_case,
}


def run_all(names: list[str] | None = None) -> dict[str, dict[str, Any]]:
    selected = list(names or CASES)
    scratch = Path(tempfile.mkdtemp(prefix="nonoka-quality-knowledge-"))
    try:
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda name: CASES[name](name, scratch), selected))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return dict(zip(selected, results))


def baseline_path(name: str) -> Path:
    return BASELINE_DIR / f"{name}.json"


def write_baselines(records: Mapping[str, Any]) -> None:
    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    for name, record in records.items():
        baseline_path(name).write_bytes(render(record).encode("utf-8"))


def differences(records: Mapping[str, Any]) -> dict[str, str]:
    """Case -> unified diff against its baseline; empty when every one matches."""

    import difflib

    found: dict[str, str] = {}
    for name, record in records.items():
        path = baseline_path(name)
        actual = render(record)
        expected = path.read_text(encoding="utf-8") if path.is_file() else ""
        if actual != expected:
            found[name] = "".join(difflib.unified_diff(
                expected.splitlines(keepends=True), actual.splitlines(keepends=True),
                fromfile=f"baseline {path.relative_to(ROOT).as_posix()}", tofile=f"current {name}", n=2,
            ))[:20000] or f"{path.relative_to(ROOT).as_posix()} is missing"
    if BASELINE_DIR.is_dir():
        for orphan in sorted({path.stem for path in BASELINE_DIR.glob("*.json")} - set(CASES)):
            found[orphan] = f"baseline {orphan}.json has no case"
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    for role in ("build", "mcp", "prepare", "hold-lock", "ui-door"):
        group.add_argument(f"--{role}", type=str)
    args = parser.parse_args(argv)
    role, value = next((role, getattr(args, role.replace("-", "_"))) for role in
                       ("build", "mcp", "prepare", "hold-lock", "ui-door") if getattr(args, role.replace("-", "_")))
    spec = json.loads(Path(value).read_text(encoding="utf-8"))
    from . import stubs, union

    sys.meta_path.insert(0, stubs.RefuseHeavyImports())
    # The knowledge code, the protocol's client and its server side by side in
    # this one process (they live in different engine packages).
    union.install()
    return {"build": build_main, "mcp": mcp_main, "prepare": prepare_main, "hold-lock": hold_lock_main,
            "ui-door": ui_door_main}[role](spec)


if __name__ == "__main__":
    raise SystemExit(main())
