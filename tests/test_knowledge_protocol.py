"""WP-K: only the knowledge package touches knowledge data (plan 4.6 item 35a).

- The transcription and correction halves' processes open no file under a
  knowledge base and make no `sqlite3.connect` (an audit hook in every child
  of the K1-K8 cases; resolving the root's path does not count, appendix B
  question 45). Their knowledge process does both, as the control.
- A request that uses the base is refused before it runs when the knowledge
  package is absent, and one that does not is not.
- The protocol itself: values, exceptions, handles, callbacks, the write lock
  across a client that dies, the generation pin, the reporter.
- The locked files take the knowledge functions that open the store from the
  client and nowhere else.
"""

from __future__ import annotations

import ast
import enum
import json
import os
import subprocess
import sys
import textwrap
import time
from collections import namedtuple
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src"
#: The access points are in the transcription and correction halves; a file both carry is the same bytes.
SOURCES = (ROOT / "engines" / "finesub-correct" / "src", ROOT / "engines" / "finesub-transcribe" / "src")
#: The knowledge package's and the transcription and correction packages' copies of the protocol.
PROTOCOL_COPIES = tuple(
    ROOT / "engines" / package / "adapter" / "finesub_knowledge"
    for package in ("finesub-knowledge", "finesub-correct", "finesub-transcribe")
)
PYTHONPATH = os.pathsep.join([str(ROOT), str(SOURCE)])
CHILD_ENVIRONMENT = {**os.environ, "PYTHONPATH": PYTHONPATH, "PYTHONIOENCODING": "utf-8"}
#: A child that drives a client and its server from one process: the packages' code side by side.
UNION = "from tests.quality_harness import union; union.install()\n"

AUDIT_HOOK = textwrap.dedent(
    '''
    import json, os, sys
    _directory = os.environ.get("KNOWLEDGE_AUDIT_DIR")
    if _directory:
        _argv = " ".join(getattr(sys, "orig_argv", sys.argv))
        if "finesub_knowledge.server" in _argv or "finesub_knowledge_host" in _argv:
            _role = "server"
        elif any(flag in _argv for flag in ("--build", "--hold-lock", "--ui-door")):
            _role = "harness"
        else:
            _role = "client"
        _log = open(os.path.join(_directory, f"{os.getpid()}.jsonl"), "a", encoding="utf-8")
        _log.write(json.dumps({"role": _role, "argv": _argv}) + "\\n")
        _log.flush()

        def _hook(event, args):
            if event == "sqlite3.connect" or (event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike))):
                path = os.fsdecode(args[0]) if not isinstance(args[0], int) else ""
                _log.write(json.dumps({"event": event, "path": path}) + "\\n")
                _log.flush()

        sys.addaudithook(_hook)
    '''
)


def _audit_records(directory: Path) -> list[dict]:
    processes = []
    for log in directory.glob("*.jsonl"):
        lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
        if lines:
            processes.append({"role": lines[0]["role"], "argv": lines[0]["argv"], "events": lines[1:]})
    return processes


def _under_a_base(path: str) -> bool:
    """Is `path` in one of the K cases' knowledge bases, or its write lock?

    The bases are `<scratch>/<case>/knowledge*` or `<scratch>/<case>/data/knowledge`;
    the registration directory beside one (`knowledge.run`) is not in it.
    """

    parts = Path(path).parts
    for index, part in enumerate(parts):
        if part.startswith("nonoka-quality-knowledge-"):
            tail = parts[index + 2:]
            if tail[:1] == ("data",):
                tail = tail[1:]
            if not tail or tail[0].endswith(".run") or not tail[0].startswith("knowledge"):
                return False
            return tail[0].endswith(".lock") or len(tail) > 1
    return False


def test_the_halves_open_nothing_of_the_base(tmp_path, monkeypatch):
    from tests.quality_harness import knowledge

    hook = tmp_path / "hook"
    hook.mkdir()
    (hook / "sitecustomize.py").write_text(AUDIT_HOOK, encoding="utf-8")
    audit = tmp_path / "audit"
    audit.mkdir()
    original = knowledge.hermetic_environment

    def audited(root):
        from tests.quality_harness.package_child import PRELOAD_ENVIRONMENT, PRELOAD_VARIABLES

        environment = original(root)
        environment["PYTHONPATH"] = os.pathsep.join([str(hook), environment["PYTHONPATH"]])
        environment["KNOWLEDGE_AUDIT_DIR"] = str(audit)
        # The engine packages' processes are isolated (WP-H): the hook reaches
        # them through the gate's preload, not through PYTHONPATH.
        environment[PRELOAD_ENVIRONMENT] = os.pathsep.join(
            part for part in (environment.get(PRELOAD_ENVIRONMENT, ""), str(hook / "sitecustomize.py")) if part
        )
        variables = json.loads(environment.get(PRELOAD_VARIABLES) or "{}")
        variables["KNOWLEDGE_AUDIT_DIR"] = str(audit)
        environment[PRELOAD_VARIABLES] = json.dumps(variables)
        return environment

    monkeypatch.setattr(knowledge, "hermetic_environment", audited)
    # The K5 door is itself a knowledge process (the knowledge page's
    # operations are the package's own), so it is not one of the halves.
    cases = [name for name in knowledge.CASES if name != "k5-ui-operations"]
    records = knowledge.run_all(cases)
    assert all(record.get("exit") in (None, 0) for record in records.values())

    processes = _audit_records(audit)
    clients = [process for process in processes if process["role"] == "client"]
    servers = [process for process in processes if process["role"] == "server"]
    assert clients and servers
    touched = [
        (process["argv"], event)
        for process in clients
        for event in process["events"]
        if event["event"] == "sqlite3.connect" or _under_a_base(event["path"])
    ]
    assert touched == []
    # The control: the knowledge processes did open the stores.
    assert any(event["event"] == "sqlite3.connect" for process in servers for event in process["events"])
    assert any(_under_a_base(event["path"]) for process in servers for event in process["events"])


BLOCK_SERVER = textwrap.dedent(
    '''
    import importlib.abc, sys

    class _NoKnowledgePackage(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name == "finesub_knowledge.server":
                raise ImportError("the knowledge package is not installed")
            return None

    sys.meta_path.insert(0, _NoKnowledgePackage())
    '''
)


def engines_without(tmp_path: Path, removed: str) -> Path:
    """An engines root with every package but `removed`, each one the real package (a directory junction)."""

    import _winapi

    root = tmp_path / "engines"
    root.mkdir()
    for package in (ROOT / "engines").iterdir():
        if package.is_dir() and package.name != removed:
            _winapi.CreateJunction(str(package), str(root / package.name))
    return root


def test_a_request_using_the_base_is_refused_without_the_package(tmp_path, monkeypatch):
    from tests.quality_harness import calls

    engines = engines_without(tmp_path, "finesub-knowledge")
    original = calls.hermetic_environment

    def without_package(root):
        environment = original(root)
        environment["NONOKA_QUALITY_ENGINES"] = str(engines)
        return environment

    monkeypatch.setattr(calls, "hermetic_environment", without_package)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    # `update` with a new subject, the default (`update`, so the ASR context
    # reads the base too); then requests that do not use the base at all: one
    # with `knowledge: none`, and a Chinese source, which only corrects and so
    # runs with the base off (`worker.for_task_mode`).
    for name in ("final-srt-judge-audio-max2-update-new-subject", "final-srt-ja-defaults"):
        refused = calls.run_scenario(name, scratch)
        assert refused["exit"] == 1, name
        assert "知识库包未安装" in " ".join(refused["failed"]), name
        # Nothing ran: the only record is the model-route overlay the worker
        # installs before it looks at the request's knowledge.
        assert {call["entry"] for call in refused["calls"]} <= {"model_routes.install_runtime_preferred"}, name
    for name in ("raw-srt-legacy-gpu-budget", "raw-srt-zh-update-subject"):
        unaffected = calls.run_scenario(name, scratch)
        assert unaffected == json.loads(calls.baseline_path(name).read_text(encoding="utf-8")), name


def test_package_problem_names_what_is_missing():
    from finesub_knowledge import client

    assert client.package_problem() is None
    blocked = subprocess.run(
        [sys.executable, "-c", UNION + BLOCK_SERVER + "\nfrom finesub_knowledge import client\nprint(client.package_problem())"],
        env=CHILD_ENVIRONMENT, capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
    )
    assert "finesub_knowledge.server" in blocked.stdout


# --- the protocol ------------------------------------------------------------------------


@pytest.fixture
def base(tmp_path):
    """A fixture base built the gate's way, and this test process as its client."""

    root = tmp_path / "knowledge"
    spec = tmp_path / "build.json"
    spec.write_text(json.dumps({"root": str(root), "diar": ""}), encoding="utf-8")
    subprocess.run(
        [sys.executable, "-m", "tests.quality_harness.knowledge", "--build", str(spec)],
        cwd=ROOT, env=CHILD_ENVIRONMENT, check=True,
    )
    return root


CLIENT_SCRIPT = textwrap.dedent(
    '''
    import json, os, sys, threading
    from pathlib import Path
    root = Path(sys.argv[1])
    os.environ["FINESUB_KNOWLEDGE_ROOT"] = str(root)
    from finesub_knowledge import client
    from finesub.llm.knowledge.node.repo import AmbiguousName, ResolvedEntry
    from finesub.llm.knowledge.base import KeywordMatch, TermMatch, active_generation_pins
    from finesub.reporting import reporting_to
    out = {}
    found, missing = client.load_entry_texts(root, ["ゆうか", "没有"])
    out["entries"] = [sorted(found), missing]
    entries, matches = client.load_preinjected_entries(root, "柊優花")
    out["match_types"] = sorted({type(match).__name__ for match in matches})
    out["terms"] = [type(term) is TermMatch for term in client.match_terms(root, "ミラ")]
    repo = client.KnowledgeRepo.open(client.knowledge_root_path(root))
    out["resolved"] = type(repo.resolve("ゆうか")) is ResolvedEntry
    out["same_handle"] = repo is client.KnowledgeRepo.open(client.knowledge_root_path(root))
    out["row"] = list(repo.store.conn.execute("SELECT MAX(rev) AS top FROM revisions").fetchone().keys())
    with client.pinned_generation_rev(root) as rev:
        out["pinned"] = [rev, list(active_generation_pins().values())]
        out["version_in_pin"] = client.knowledge_version(root)
    out["after_pin"] = active_generation_pins()
    seen = []
    from finesub.llm.token_budget import HeuristicTokenCounter, LocalGeminiTokenCounter
    class Exact(LocalGeminiTokenCounter):
        """Reads as a runnable tokcount; counts heuristically, and says it was asked."""
        def __init__(self):
            self.heuristic = HeuristicTokenCounter()
        available = True
        def count_text(self, text):
            seen.append(len(text))
            return self.heuristic.count_text(text)
    local = client.render_subject_block(root, ["streamer/柊優花"], count_tokens=Exact().count_text)
    out["callback"] = bool(local)
    out["callback_calls"] = len(seen) > 0
    try:
        client.KnowledgeRepo.open(root).resolve_qualified("ミラ")
        out["ambiguous"] = "no error"
    except LookupError as error:
        out["ambiguous"] = type(error).__name__
    class Recorder:
        def __init__(self): self.events = []
        def __getattr__(self, name): return lambda *a, **k: self.events.append(name)
    recorder = Recorder()
    with reporting_to(recorder):
        client.knowledge_signals_enabled(root)
    out["reported"] = recorder.events
    box = {}
    thread = threading.Thread(target=lambda: box.update(v=client.knowledge_version(root)))
    thread.start(); thread.join()
    out["thread"] = box["v"]
    mode = sys.argv[2] if len(sys.argv) > 2 else ""
    if mode == "die-holding-lock":
        lock = client.knowledge_write_lock(root)
        print(json.dumps({"held": lock.__enter__()}), flush=True)
        sys.stdin.readline()
        os._exit(3)
    print(json.dumps(out, ensure_ascii=False), flush=True)
    '''
)


def _client(base: Path, *mode: str, **kwargs):
    return subprocess.run(
        [sys.executable, "-c", UNION + CLIENT_SCRIPT, str(base), *mode],
        cwd=ROOT, env=CHILD_ENVIRONMENT, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120, **kwargs,
    )


def test_values_handles_callbacks_pins_and_reporter_cross(base):
    completed = _client(base)
    assert completed.returncode == 0, completed.stderr
    out = json.loads(completed.stdout.strip().splitlines()[-1])
    assert out["entries"] == [["柊優花"], ["没有"]]
    assert out["match_types"] == ["KeywordMatch"]
    assert out["terms"] and all(out["terms"])
    assert out["resolved"] is True
    assert out["same_handle"] is True
    assert out["row"] == ["top"]
    rev = out["pinned"][0]
    assert out["pinned"] == [rev, [rev]]
    assert out["version_in_pin"] == f"rev:{rev}"
    assert out["after_pin"] == {}
    assert out["callback"] is True and out["callback_calls"] is True
    assert out["ambiguous"] in ("no error", "AmbiguousName")
    assert out["thread"] == f"rev:{rev}"


def test_a_client_that_dies_inside_the_write_lock_does_not_leave_it_taken(base):
    holder = subprocess.Popen(
        [sys.executable, "-c", UNION + CLIENT_SCRIPT, str(base), "die-holding-lock"],
        cwd=ROOT, env=CHILD_ENVIRONMENT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        text=True, encoding="utf-8",
    )
    try:
        line = holder.stdout.readline()
        assert json.loads(line) == {"held": True}
        holder.stdin.write("\n")
        holder.stdin.flush()
        assert holder.wait(timeout=60) == 3
    finally:
        holder.kill()
    # The knowledge process sees its connection drop, leaves the lock and,
    # its owner gone, exits; the next writer gets the lock at once.
    from finesub.llm.knowledge.base import knowledge_lock_path
    from finesub_bootstrap.locks import LockUnavailable, holding_lock

    deadline = time.monotonic() + 30
    while True:
        try:
            with holding_lock(knowledge_lock_path(base), timeout=1):
                break
        except LockUnavailable:
            assert time.monotonic() < deadline, "the write lock stayed taken"


class Colour(enum.Enum):
    RED = "red"


Pair = namedtuple("Pair", "a b")


def test_codec_round_trips_plain_data():
    import datetime

    from finesub_knowledge.protocol import Codec

    codec = Codec("server", "test")
    value = {
        "list": [1, 2.5, "x", None, True], "tuple": (1, (2, 3)), "set": {1, 2}, "frozen": frozenset({"a"}),
        "bytes": b"\x00\xff", "path": Path("a") / "b", "date": datetime.date(2026, 11, 12),
        "when": datetime.datetime(2026, 11, 12, 8, 30), (1, "k"): "tuple key", "enum": Colour.RED,
        "named": Pair(1, 2),
    }
    wire = json.loads(json.dumps(codec.encode(value)))
    assert codec.decode(wire) == value


def test_exceptions_come_back_as_their_type():
    from finesub.llm.knowledge.node.repo import AmbiguousName, ResolvedEntry
    from finesub_knowledge.protocol import Codec, RemoteError

    codec = Codec("server", "test")
    error = AmbiguousName("名字", [ResolvedEntry("streamer", "名字", "id-1")])
    rebuilt = codec.decode_exception(json.loads(json.dumps(codec.encode_exception(error))))
    assert type(rebuilt) is AmbiguousName and str(rebuilt) == str(error) and rebuilt.matches == error.matches
    unknown = codec.decode_exception({"c": "no.such.module:Error", "args": ["x"], "message": "boom"})
    assert isinstance(unknown, RemoteError)


def test_mutable_client_objects_stay_with_the_client():
    from dataclasses import dataclass, field

    from finesub_knowledge.protocol import EXPORTS, Codec

    @dataclass
    class Box:
        items: list = field(default_factory=list)

    box = Box()
    wire = Codec("client", "test").encode(box)
    assert {"$h", "c"} <= set(wire)
    assert EXPORTS.get(wire["$h"]) is box


# --- the locked files ----------------------------------------------------------------------

#: The knowledge functions that open the store, and the repository: what the
#: engine files outside the knowledge package take from the client only.
STORE_NAMES = frozenset({
    "kb_index_block_text", "knowledge_root_path", "knowledge_signals_enabled", "knowledge_version",
    "knowledge_write_lock", "load_entry_texts", "load_index_entries", "load_index_texts",
    "load_preinjected_entries", "match_index_keywords", "match_terms", "worktree_blocks_knowledge_write",
    "harvest_rendered_edits_at_run_start", "load_style_catalog", "load_style_entries", "record_style_injected",
    "render_style_block", "resolve_style_keys", "style_vote_facts", "render_subject_block",
    "resolve_subject_keys", "KnowledgeRepo", "pinned_generation_rev", "repin_generation_rev",
})
#: The access-point files of WP-P0 (engine-packages.md 11.1), relative to `finesub/`.
ACCESS_POINT_FILES = (
    "stages.py", "llm/correction_translation.py", "llm/knowledge/update.py", "llm/knowledge/entries.py",
    "llm/research.py", "llm/stages/correction/run.py", "llm/stages/correction/attempts.py",
    "llm/stages/correction/request_turn.py", "llm/stages/correction/parallel.py",
    "llm/stages/correction/serial.py", "llm/stages/correction/context.py", "llm/stages/prepare_task.py",
    "llm/stages/plan.py", "llm/stages/speaker_judge/roster.py", "llm/prompt_artifacts.py",
    "llm/length_tier.py", "llm/kb_phonetic_hints.py", "llm/client.py", "llm/agent/agent_mcp_server.py",
)


def _knowledge_imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = "." * node.level + (node.module or "")
            for alias in node.names:
                yield node.lineno, module, alias.name


def test_access_points_take_store_functions_from_the_client():
    wrong = []
    moved = 0
    for relative in ACCESS_POINT_FILES:
        path = next(root / "finesub" / relative for root in SOURCES if (root / "finesub" / relative).is_file())
        inside_knowledge = relative.startswith("llm/knowledge/")
        for line, module, name in _knowledge_imports(path):
            if name not in STORE_NAMES:
                continue
            if module == "finesub_knowledge.client":
                moved += 1
            elif "knowledge" in module or inside_knowledge and module.startswith("."):
                wrong.append(f"{relative}:{line} {name} from {module}")
    assert wrong == []
    assert moved == 65


def test_the_package_does_not_import_the_core():
    paths = [path for copy in PROTOCOL_COPIES for path in copy.glob("*.py")]
    assert paths
    for path in paths:
        for _line, module, _name in _knowledge_imports(path):
            assert not module.startswith("nonoka_x"), path
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert not any(alias.name.startswith("nonoka_x") for alias in node.names), path


def test_a_counter_crossing_is_still_recognised_as_the_local_tokcount():
    """`local_counter_available_for` looks at `count_tokens.__self__`; across the protocol it must see the same."""

    import threading
    from multiprocessing.connection import Pipe

    from finesub.llm.token_budget import (
        FallbackTokenCounter, HeuristicTokenCounter, LocalGeminiTokenCounter, local_counter_available_for,
    )
    from finesub_knowledge.protocol import Peer, bind_peer

    class Exact(LocalGeminiTokenCounter):
        def __init__(self) -> None:
            pass

        available = True  # a runnable tokcount, as on a user's machine

    local = Exact()
    counter = FallbackTokenCounter(counters=(local, HeuristicTokenCounter()))
    expected = local_counter_available_for(counter.count_text)
    server_end, client_end = Pipe()
    server = Peer(server_end, "server", "test-client", resolve_call=lambda name: local_counter_available_for)

    def serve():
        bind_peer(server)
        message = server.receive()
        server.send(server.serve(message))

    thread = threading.Thread(target=serve)
    thread.start()
    client = Peer(client_end, "client", "test-server")
    bind_peer(client)
    try:
        seen = client.request({"op": "call", "fn": "introspect"}, (counter.count_text,), {})
    finally:
        bind_peer(None)
        thread.join(timeout=30)
    assert expected is True and seen is True
