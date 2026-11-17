"""LLM half replay: what the correction layer sends for a fixed stable, and what it renders.

Model answers are not reproducible, so the comparison is about what goes out:
for one stable JSON, the sequence of requests the correction, translation and
proofreading stages send -- every message in full, the routed provider and
model, and the call parameters -- must equal the baseline byte for byte. Each
request is answered deterministically (`Responder`), so the rendered
`final.srt` and `-annotated.csv` must equal theirs byte for byte too.

Where the requests are caught: `finesub.llm.llm_runtime.chat_complete`, the one
function every API-routed `RoleClient.complete` call goes through
(`llm/client.py`, after routing, prompt assembly and output budgeting). It is
replaced in a child interpreter, never edited. The routes are pinned to an
OpenAI-compatible endpoint (`calls.SETTINGS_PROFILES["compat"]`) so every call
takes that path; local agent CLIs and Gemini media uploads are not exercised.
Sockets are refused in the child, so a call that bypassed the replacement
would fail the run instead of reaching the network.

The run enters at Nonoka's outermost door: `nonoka_x.worker.main` with a
`cloud_stable` request, which is exactly "the LLM half from a stable that is
already in hand". Run as `python -m tests.quality_harness.replay --child <spec>`.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Mapping

from . import ENGINES, FIXTURES, ROOT
from .calls import KNOWLEDGE_ENTRY, Normalizer, adopt_worker_environment, bind_settings, hermetic_environment, render

BASELINE_DIR = FIXTURES / "replay"
#: Baselines made from the real sample's recognition. They quote a real
#: recording's transcript, so they stay beside it, outside the repository.
PRIVATE_BASELINE_DIR = ROOT / "test_data" / "quality-baseline" / "replay"
#: The stable inputs of the private cases, kept apart from the real-sample
#: baseline: the replay fixes one recognition and checks what the LLM half does
#: with it, so re-recording the real samples (as on 2026-11-11, when compiled
#: acceleration was turned off) must not move these inputs.
REAL_STABLE = "test_data/quality-baseline/replay-inputs/sample_ja-stable.json"
MULTI_WORKSPACE = "test_data/quality-baseline/replay-inputs"
TASK_ID = "gate-replay"

#: One replay per stable input; `stable` and `media` are relative to the
#: repository. A `private` case reads the real sample's stable and is skipped
#: where that has not been recorded (`real.py record`).
CASES: dict[str, dict[str, Any]] = {
    "real-sample-ja-text-quality": {
        "private": True,
        "stable": REAL_STABLE,
        "media": "test_data/audio/sample_ja.m4a",
        "request": {
            "target": "final-srt",
            "language": "ja",
            "knowledge": "none",
            "correction": {"media": "text", "retrieval": "none", "difficulty": "quality"},
        },
    },
    "real-sample-ja-text-efficiency": {
        "private": True,
        "stable": REAL_STABLE,
        "media": "test_data/audio/sample_ja.m4a",
        "request": {
            "target": "final-srt",
            "language": "ja",
            "knowledge": "none",
            "correction": {"media": "text", "retrieval": "none", "difficulty": "efficiency"},
        },
    },
    "real-multi-person-judge-text": {
        # The speaker judge over the real acoustic labels, the way a cloud
        # task with a timeline continues.
        "private": True,
        "stable": f"{MULTI_WORKSPACE}/sample_multi_person-stable.json",
        "diar": f"{MULTI_WORKSPACE}/sample_multi_person-diar.json",
        "media": "test_data/audio/sample_multi_person.aac",
        "request": {
            "target": "final-srt",
            "language": "ja",
            "knowledge": "none",
            "diarization": {"level": "judge-text"},
            "correction": {"media": "text", "retrieval": "none", "difficulty": "quality"},
        },
    },
    "real-sample-ja-text-quality-small-windows": {
        # Preparation, then several windows: what carries over between them
        # (read-only preceding context, accumulated advice) is in the prompts.
        "private": True,
        "stable": REAL_STABLE,
        "media": "test_data/audio/sample_ja.m4a",
        "config_append": "[chunking]\nmax_window_subtitle_tokens = 1500\n",
        "request": {
            "target": "final-srt",
            "language": "ja",
            "knowledge": "none",
            "correction": {"media": "text", "retrieval": "none", "difficulty": "quality", "fast": "off"},
        },
    },
    "real-sample-ja-text-quality-windows": {
        # `fast: off`: the normal flow -- preparation, then window by window.
        "private": True,
        "stable": REAL_STABLE,
        "media": "test_data/audio/sample_ja.m4a",
        "request": {
            "target": "final-srt",
            "language": "ja",
            "knowledge": "none",
            "correction": {"media": "text", "retrieval": "none", "difficulty": "quality", "fast": "off"},
        },
    },
    "sample-stable-zh-correct-only": {
        # A Chinese source only corrects: the correct-only prompt set.
        "stable": "tests/fixtures/finesub/sample-stable.json",
        "request": {
            "target": "final-srt",
            "language": "zh",
            "knowledge": "none",
            "correction": {"media": "text", "retrieval": "none", "difficulty": "quality"},
        },
    },
    "sample-stable-knowledge-update": {
        # The knowledge update after the correction, into a fresh base.
        "stable": "tests/fixtures/finesub/sample-stable.json",
        "request": {
            "target": "final-srt",
            "language": "ja",
            "knowledge": "update",
            "knowledge_subjects": [{"kind": "streamer", "subject": "猫又おかゆ", "aliases": "おかゆ",
                                    "description": "Hololive 所属"}],
            "correction": {"media": "text", "retrieval": "none", "difficulty": "quality"},
        },
    },
    "sample-stable-translated-srt": {
        "stable": "tests/fixtures/finesub/sample-stable.json",
        "request": {
            "target": "translated-srt",
            "language": "ja",
            "knowledge": "none",
            "correction": {"media": "text", "retrieval": "none", "difficulty": "quality"},
        },
    },
    "sample-stable-text-quality": {
        "stable": "tests/fixtures/finesub/sample-stable.json",
        "request": {
            "target": "final-srt",
            "language": "ja",
            "knowledge": "none",
            "correction": {"media": "text", "retrieval": "none", "difficulty": "quality"},
        },
    },
    "sample-stable-text-efficiency": {
        "stable": "tests/fixtures/finesub/sample-stable.json",
        "request": {
            "target": "final-srt",
            "language": "ja",
            "knowledge": "none",
            "correction": {"media": "text", "retrieval": "none", "difficulty": "efficiency",
                           "extra_info": "测试用背景", "extra_style": "口语化"},
        },
    },
}


class Responder:
    """Answers nothing; what an unanswerable model looks like."""

    def answer(self, messages: list[dict[str, Any]], kwargs: Mapping[str, Any]) -> str:
        return ""


_ASR_BLOCK = re.compile(r"<asr_result>\s*\n(?P<body>.*?)</asr_result>", re.S)


def _text_of(message: Mapping[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    from finesub.llm.exchange_log import render_message_text

    return render_message_text(content)


class GateResponder(Responder):
    """A well-formed answer of each kind the correction layer asks for, built from the request.

    Deterministic by construction: the answer is a function of the last user
    message only. It is not a good subtitle -- every row is kept as it is and
    "translated" to a fixed marker -- because what the replay checks is that
    the engine asks the same things and renders the same answers the same way.
    """

    def answer(self, messages: list[dict[str, Any]], kwargs: Mapping[str, Any]) -> str:
        users = [_text_of(message) for message in messages if message.get("role") == "user"]
        # A repair round appends the validator's complaint as the last turn;
        # the task itself is still the turn that carried the rows.
        markers = ("<asr_result>", "<proofread>", "<context_pack>", "<judge>", "<knowledge_proposals>")
        task = next((text for text in reversed(users) if any(marker in text for marker in markers)), "")
        blocks = list(_ASR_BLOCK.finditer(task))
        if "<translated>" in task and blocks:
            return self.translated(task, blocks[-1].group("body"))
        if "<proofread>" in task:
            return self.proofread(task)
        if "<context_pack>" in task:
            return self.context_pack()
        if "<judge>" in task:
            return self.judge(task)
        if "<knowledge_proposals>" in task:
            # The knowledge update: nothing worth writing back.
            return "<knowledge_proposals>\n</knowledge_proposals>"
        return ""

    @staticmethod
    def judge(task: str) -> str:
        """The speaker judge: every row kept, the last one moved to a new track."""

        ids = re.findall(r"^▶ (q\d+) ", task, re.M)
        tracks = re.findall(r"^- (s\d+)：", task, re.M)
        lines = []
        for index, ident in enumerate(ids):
            last = index == len(ids) - 1 and len(ids) > 1
            answer = {"id": ident, "track": "@甲" if last else "keep", "why": "门禁回放的固定答案"}
            lines.append(json.dumps(answer, ensure_ascii=False))
        named = [f"{track} = 不明" for track in tracks] + (["@甲 = 不明"] if len(ids) > 1 else [])
        return "<judge>\n" + "\n".join(lines) + "\n</judge>\n<tracks>\n" + "\n".join(named) + "\n</tracks>"

    @staticmethod
    def context_pack() -> str:
        """The preparation's delivery: a valid, empty context."""

        pack = {
            "general_context": {
                "global_summary": "门禁回放的固定准备结果",
                "entities": [], "terminology": [], "streamer_notes": [], "asr_risks": [],
                "uncertainties": [], "user_requests": [], "sources": [],
            },
            "window_contexts": [],
        }
        return "<context_pack>\n" + json.dumps(pack, ensure_ascii=False) + "\n</context_pack>"

    @staticmethod
    def proofread(user: str) -> str:
        """Pass 3: one deterministic edit, on the first row it is shown."""

        rows = [
            line.split("|")
            for line in user.splitlines()
            if re.fullmatch(r"\d+\|[^|]*\|[^|]*\|[^|]*\|[^|]*\|[^|]*\|[^|]*\|[^|]*", line)
        ]
        if not rows:
            return "<proofread>\n</proofread>"
        first = rows[0]
        return f"<proofread>\n{first[0]}|1|{first[4]}|校对译文{first[0]}|门禁回放的固定修改\n</proofread>"

    @staticmethod
    def rows(body: str) -> list[list[str]]:
        lines = [line for line in body.strip().splitlines() if line.strip()]
        return [line.split("|") for line in lines[1:]]

    def translated(self, task: str, body: str) -> str:
        """Every source kept as its own row, its text unchanged, "translated" to a marker."""

        header_match = re.search(r"header：`([^`]+)`", task)
        header = header_match.group(1) if header_match else "type|position|corrected_text|translation|conf|note"
        columns = header.split("|")
        rows = self.rows(body)
        window_start = float(rows[0][1]) if rows else 0.0
        out = []
        if "<decisions>" in task:
            out += ["<decisions>", "逐行保留，不合并", "</decisions>"]
        out += ["<translated>", header]
        for row in rows:
            source, start, text = row[0], row[1], row[-1]
            cells = {
                "type": "sub",
                "position": source,
                "start": f"{float(start) - window_start:.1f}",
                "corrected_text": text,
                "translation": f"测试译文{source}",
                "conf": "high",
                "note": "",
            }
            out.append("|".join(cells.get(column, "") for column in columns))
        out += ["</translated>", "<next_advice>", "</next_advice>"]
        return "\n".join(out)


def _plain_messages(messages: Any) -> Any:
    from .stubs import encode

    return encode(messages)


#: The correction package's entry module, and where it keeps a task's files (plan 4.6 item 34).
CORRECTION_ENTRY = "finesub_correct"
CORRECTION_OUTPUTS = Path("native") / "finesub-correct"


def _refuse_network() -> None:
    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("network refused by the quality gate replay")

    socket.socket.connect = refuse  # type: ignore[method-assign]
    socket.socket.connect_ex = refuse  # type: ignore[method-assign]
    socket.create_connection = refuse  # type: ignore[assignment]


def package_hook(spec: Mapping[str, Any], entry: str, context: Mapping[str, Any]) -> None:
    """In each engine package's process, once its bridge ran: no network, and the model's answers.

    The correction's requests are taken where they leave FineSub
    (`llm_runtime.chat_complete`) and written, in the order they are sent, to
    the run's shared record. A case's `config_append` is one engine setting
    off its default, added to the `finesub.toml` this process's bridge wrote.
    """

    from . import stubs
    from .package_child import SharedRecord

    _refuse_network()
    if entry == KNOWLEDGE_ENTRY:
        return
    sys.meta_path.insert(0, stubs.RefuseHeavyImports())
    if spec.get("config_append") and os.environ.get("FINESUB_CONFIG_FILE"):
        config = Path(os.environ["FINESUB_CONFIG_FILE"])
        existing = config.read_text(encoding="utf-8") if config.is_file() else ""
        config.write_text(existing + "\n" + spec["config_append"], encoding="utf-8")
        from finesub.config import clear_config_cache

        clear_config_cache()
    try:
        from finesub.llm import llm_runtime
    except ImportError:
        return

    exchanges = SharedRecord(Path(spec["record"]))
    lock = threading.Lock()
    responder = _load_responder(spec.get("responder"))

    def chat_complete(messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        content = responder.answer(messages, kwargs)
        sent = {
            name: value for name, value in sorted(kwargs.items())
            if name not in {"rate_limiter"}
        }
        with lock:
            exchanges.append({
                "thread": threading.current_thread().name,
                "messages": _plain_messages(messages),
                "parameters": stubs.encode(sent),
                "answer": content,
            })
        return {
            "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": int(kwargs.get("estimated_input_tokens") or 0),
                      "completion_tokens": max(1, len(content) // 2)},
            "model": kwargs.get("model"),
        }

    llm_runtime.chat_complete = chat_complete
    if entry != CORRECTION_ENTRY:
        return
    # What the correction's behaviour turns on, as its process has it (see `child_main`).
    machine = Path(spec["record"]).with_suffix(".machine.json")
    import importlib.util

    machine.write_text(json.dumps({
        "opencc_importable": importlib.util.find_spec("opencc") is not None,
        "tokcount_on_path": shutil.which("tokcount") is not None,
    }), encoding="utf-8")




def child_main(spec_path: Path) -> int:
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    root = Path(spec["root"])
    from . import stubs
    from .package_child import read_shared, use_in_worker

    sys.meta_path.insert(0, stubs.RefuseHeavyImports())
    _refuse_network()

    settings = bind_settings(root / "data", "compat")
    adopt_worker_environment(root / "data", settings)
    record_file = root / "exchanges.jsonl"
    use_in_worker({**spec, "hook": "tests.quality_harness.replay:package_hook", "record": str(record_file)},
                  root / "package-child.json")
    from nonoka_x.local_provider import validate_request

    request = validate_request({
        "schema": 1,
        "provider": "local",
        "source": {
            "kind": "cloud_stable",
            "path": spec["media_file"],
            "title": "节目",
            "stable_json": str(root / "cloud" / "cloud.stable.json"),
            **({"diar_json": str(root / "cloud" / "cloud.diar.json")} if spec.get("diar") else {}),
        },
        **spec["request"],
    })

    from nonoka_x import worker

    task_dir = root / "data" / "tasks" / TASK_ID
    stdout = io.StringIO()
    previous = sys.stdin
    sys.stdin = io.StringIO(json.dumps(request, ensure_ascii=False) + "\n")
    try:
        with contextlib.redirect_stdout(stdout):
            code = worker.main(["--task-id", TASK_ID, "--task-dir", str(task_dir), "--engines", str(ENGINES),
                                "--data-dir", str(root / "data")])
    finally:
        sys.stdin = previous
    events = [json.loads(line) for line in stdout.getvalue().splitlines() if line.startswith("{")]
    workspace = task_dir / CORRECTION_OUTPUTS
    outputs = {}
    for name in ("节目.srt", "节目-annotated.csv", "节目-translated.srt", "节目-raw.srt"):
        path = workspace / name
        outputs[name] = path.read_text(encoding="utf-8") if path.is_file() else None
    machine_file = record_file.with_suffix(".machine.json")
    machine = json.loads(machine_file.read_text(encoding="utf-8")) if machine_file.is_file() else {}

    result = {
        # Machine facts the correction's behaviour turns on, in the process
        # the correction ran in: traditional -> simplified folding needs
        # opencc, and token counting (so the window plan) changes when a
        # `tokcount` binary is on PATH. Compared, so a replay on a different
        # interpreter says so here first.
        "machine": machine,
        "exit": code,
        "failed": [event["payload"].get("message") for event in events if event["type"] == "failed"],
        "warnings": [event["payload"] for event in events if event["type"] == "warning"],
        "exchanges": read_shared(record_file),
        "outputs": outputs,
    }
    Path(spec["out"]).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return 0


def _load_responder(name: str | None) -> Responder:
    if not name:
        return Responder()
    module_name, _, attribute = name.rpartition(".")
    import importlib

    return getattr(importlib.import_module(module_name), attribute)()


def write_silence(path: Path, *, seconds: float) -> Path:
    """A real, readable audio file: the correction probes the source's duration."""

    import wave

    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(bytes(2) * int(16000 * seconds))
    return path


def run_case(name: str, scratch: Path, *, responder: str | None = None) -> dict[str, Any]:
    case = CASES[name]
    root = (scratch / name).resolve()
    if root.exists():
        shutil.rmtree(root)
    for directory in ("media", "cloud", "data", "tmp"):
        (root / directory).mkdir(parents=True)
    if case.get("media"):
        media = root / "media" / f"节目{Path(case['media']).suffix}"
        shutil.copyfile(ROOT / case["media"], media)
    else:
        media = write_silence(root / "media" / "节目.wav", seconds=float(case.get("seconds", 6.0)))
    shutil.copyfile(ROOT / case["stable"], root / "cloud" / "cloud.stable.json")
    if case.get("diar"):
        shutil.copyfile(ROOT / case["diar"], root / "cloud" / "cloud.diar.json")
    spec = {
        "request": case["request"], "root": str(root), "out": str(root / "record.json"),
        "media_file": str(media),
        "diar": bool(case.get("diar")),
        "config_append": case.get("config_append", ""),
        "responder": responder or case.get("responder", "tests.quality_harness.replay.GateResponder"),
    }
    (root / "spec.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, "-m", "tests.quality_harness.replay", "--child", str(root / "spec.json")],
        cwd=ROOT, env=hermetic_environment(root), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=600,
    )
    out = root / "record.json"
    if completed.returncode != 0 or not out.is_file():
        raise RuntimeError(f"replay {name} child failed ({completed.returncode}):\n{completed.stderr[-4000:]}")
    raw = json.loads(out.read_text(encoding="utf-8"))
    record = Normalizer({"$ROOT": root, "$REPO": ROOT}).value(raw)
    # Said about this machine, not about the requests (a missing opencc, no
    # tokcount): kept for the reader, never compared.
    record["warnings_not_compared"] = record.pop("warnings")
    return record


def missing_inputs(name: str) -> list[str]:
    """The inputs of case `name` that are not on this machine (repository-relative)."""

    case = CASES[name]
    wanted = [case["stable"]] + ([case["media"]] if case.get("media") else [])
    return [relative for relative in wanted if not (ROOT / relative).is_file()]


def available(name: str) -> bool:
    return not missing_inputs(name)


def unavailable() -> dict[str, list[str]]:
    """Case -> its missing inputs, for every case that cannot run here.

    Such a case is not a pass: nothing was compared. The gate reports it as
    undecided, so a checkout without `test_data/` (another machine, a fresh
    worktree) does not read as having passed every replay.
    """

    return {name: missing for name in CASES if (missing := missing_inputs(name))}


def baseline_path(name: str) -> Path:
    return (PRIVATE_BASELINE_DIR if CASES[name].get("private") else BASELINE_DIR) / f"{name}.json"


def compared(record: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if key != "warnings_not_compared"}


def run_all(names: list[str] | None = None) -> dict[str, dict[str, Any]]:
    """Every available case (or `names`), run in parallel children."""

    from concurrent.futures import ThreadPoolExecutor

    selected = [name for name in (names or CASES) if available(name)]
    scratch = Path(tempfile.mkdtemp(prefix="nonoka-quality-replay-"))
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda name: run_case(name, scratch), selected))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return dict(zip(selected, results))


def write_baselines(records: Mapping[str, Any]) -> None:
    for name, record in records.items():
        path = baseline_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(render(record).encode("utf-8"))


def differences(records: Mapping[str, Any]) -> dict[str, str]:
    """Case -> what differs from its baseline, compared byte for byte; empty when all match.

    The report names the first differing exchange and field, then the whole
    unified diff of the compared part.
    """

    import difflib

    found: dict[str, str] = {}
    for name, record in records.items():
        path = baseline_path(name)
        if not path.is_file():
            found[name] = f"no baseline at {path.relative_to(ROOT).as_posix()}"
            continue
        expected_record = json.loads(path.read_text(encoding="utf-8"))
        expected, actual = render(compared(expected_record)), render(compared(record))
        if expected == actual:
            continue
        summary = []
        old, new = expected_record.get("exchanges", []), record.get("exchanges", [])
        if len(old) != len(new):
            summary.append(f"request count {len(old)} -> {len(new)}")
        for index, (a, b) in enumerate(zip(old, new)):
            for field in ("parameters", "messages", "answer"):
                if a.get(field) != b.get(field):
                    summary.append(f"first differing request: #{index + 1} ({field})")
                    break
            if summary and summary[-1].startswith("first"):
                break
        for output, text in (record.get("outputs") or {}).items():
            if (expected_record.get("outputs") or {}).get(output) != text:
                summary.append(f"output differs: {output}")
        diff = "".join(difflib.unified_diff(
            expected.splitlines(keepends=True), actual.splitlines(keepends=True),
            fromfile=f"baseline {path.relative_to(ROOT).as_posix()}", tofile=f"current {name}", n=1,
        ))
        found[name] = "\n".join(summary) + "\n" + diff[:20000]
    return found


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", type=Path, required=True)
    raise SystemExit(child_main(parser.parse_args().child))
