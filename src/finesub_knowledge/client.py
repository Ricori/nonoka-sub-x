"""What the transcription and correction halves take from the knowledge package.

Each name here stands for the FineSub knowledge function of the same name and
signature (`finesub.llm.knowledge.base.load_entry_texts` and so on): calling
it runs the original in this process's knowledge process
(`finesub_knowledge.server`) and hands back what it returned. Nothing in this
process opens the knowledge base, reads a file under it, or connects to its
SQLite store; the engine files only changed where these names come from
(plan section 0.1 rule 5).

Two things stay in this process because they are this process's state, not
the base's: the generation pin (`pinned_generation_rev`,
`repin_generation_rev` set the same ContextVar `base.py` keeps here, reading
the revision through the protocol), and the reporter (what the knowledge code
reports in the knowledge process is reported again here, after the call).

Where the knowledge process comes from, in this order:

1. `FINESUB_KNOWLEDGE_ENDPOINT` names one (JSON: address, family, authkey).
2. An agent's MCP server (`FINESUB_MCP_SESSION` set) uses a live one serving
   its base (`FINESUB_MCP_KNOWLEDGE_ROOT`), found by the registration beside
   the base (`server.registration_dir`). FineSub writes the MCP environment
   itself; nothing is added to it.
3. Otherwise this process starts its own (`python -m finesub_knowledge.server
   --owner <pid>`) on first use, with this process's interpreter and
   environment, and keeps it to the end.

`package_problem()` is the readiness check: whether the knowledge package is
here at all.
"""

from __future__ import annotations

import importlib.util
import json
import os
import secrets
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from .protocol import PROTOCOL_VERSION, Peer, ProtocolError, set_peer_factory

ENDPOINT_VARIABLE = "FINESUB_KNOWLEDGE_ENDPOINT"
#: Set by FineSub in the environment of an agent's MCP server (`client.py`,
#: `agent_session_host.py`), and only there.
MCP_SESSION_VARIABLE = "FINESUB_MCP_SESSION"
MCP_ROOT_VARIABLE = "FINESUB_MCP_KNOWLEDGE_ROOT"
SERVER_MODULE = "finesub_knowledge.server"
#: What the knowledge package is made of before WP-P1 puts it in its own
#: directory: the server, and FineSub's knowledge code it runs.
PACKAGE_MODULES = (SERVER_MODULE, "finesub.llm.knowledge.node.repo")


class KnowledgeUnavailable(RuntimeError):
    """The knowledge package cannot be reached from this process."""


def package_problem() -> str | None:
    """Why the knowledge package is not usable here, or None when it is."""

    for module in PACKAGE_MODULES:
        try:
            found = importlib.util.find_spec(module)
        except (ImportError, ValueError):
            found = None
        if found is None:
            return f"知识库包未安装（找不到 {module}）"
    return None


# --- reaching the knowledge process ------------------------------------------------------

_LOCK = threading.Lock()
_ENDPOINT: dict[str, Any] | None = None
_SERVER: subprocess.Popen | None = None
_NONCE = secrets.token_hex(8)


def _server_environment() -> dict[str, str]:
    """This process's environment, with the code it runs first on the import path.

    The knowledge process must run the same knowledge code (and this package)
    this process would have: where `finesub` and `finesub_knowledge` were
    imported from here, whatever put them on the path (a `PYTHONPATH`, a test
    runner's `sys.path`).
    """

    environment = dict(os.environ)
    roots: list[str] = []
    for package in ("finesub_knowledge", "finesub"):
        spec = importlib.util.find_spec(package)
        for location in (spec.submodule_search_locations or []) if spec is not None else []:
            root = str(Path(location).resolve().parent)
            if root not in roots:
                roots.append(root)
    existing = [part for part in environment.get("PYTHONPATH", "").split(os.pathsep) if part]
    environment["PYTHONPATH"] = os.pathsep.join([*roots, *(part for part in existing if part not in roots)])
    return environment


def _start_server() -> dict[str, Any]:
    global _SERVER
    problem = package_problem()
    if problem:
        raise KnowledgeUnavailable(problem)
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    process = subprocess.Popen(
        [sys.executable, "-m", SERVER_MODULE, "--owner", str(os.getpid())],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, env=_server_environment(), **kwargs,
    )
    assert process.stdout is not None
    line = process.stdout.readline()
    process.stdout.close()
    if not line.strip():
        raise KnowledgeUnavailable(f"知识库进程没有启动（退出码 {process.wait()}）")
    _SERVER = process
    return json.loads(line)


def _registered_endpoints(knowledge_root: str) -> list[dict[str, Any]]:
    from .protocol import registration_dir

    directory = registration_dir(knowledge_root)
    found = []
    for path in sorted(directory.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            found.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return found


def _endpoint_candidates() -> list[dict[str, Any]]:
    global _ENDPOINT
    with _LOCK:
        if _ENDPOINT is not None:
            return [_ENDPOINT]
        named = os.environ.get(ENDPOINT_VARIABLE, "").strip()
        if named:
            _ENDPOINT = json.loads(named)
            return [_ENDPOINT]
        if os.environ.get(MCP_SESSION_VARIABLE) and os.environ.get(MCP_ROOT_VARIABLE):
            registered = _registered_endpoints(os.environ[MCP_ROOT_VARIABLE])
            if registered:
                return registered
        _ENDPOINT = _start_server()
        return [_ENDPOINT]


def _connect() -> Peer:
    from multiprocessing.connection import Client

    global _ENDPOINT
    last: Exception | None = None
    for endpoint in _endpoint_candidates():
        try:
            connection = Client(endpoint["address"], family=endpoint["family"],
                                authkey=bytes.fromhex(endpoint["authkey"]))
            connection.send_bytes(json.dumps({
                "k": "hello", "protocol": PROTOCOL_VERSION, "pid": os.getpid(), "nonce": _NONCE,
            }).encode("utf-8"))
            hello = json.loads(connection.recv_bytes().decode("utf-8"))
            if hello.get("protocol") != PROTOCOL_VERSION:
                raise ProtocolError(f"knowledge process speaks protocol {hello.get('protocol')}")
        except Exception as error:  # noqa: BLE001 - try the next one
            last = error
            continue
        with _LOCK:
            _ENDPOINT = endpoint
        return Peer(connection, "client", f"server:{endpoint.get('pid')}",
                    request_context=_request_context, on_response=_replay_events)
    raise KnowledgeUnavailable(f"连不上知识库进程：{last}")


set_peer_factory(_connect)


def _request_context() -> dict[str, Any]:
    """What a call carries from here: this context's generation pins, whether anything listens."""

    from finesub.llm.knowledge.base import active_generation_pins
    from finesub.reporting import current_reporter, reporter_delivers

    return {"pins": active_generation_pins(), "rep": reporter_delivers(current_reporter())}


def _replay_events(answer: Mapping[str, Any]) -> None:
    events = answer.get("events")
    if not events:
        return
    from finesub.reporting import current_reporter

    from .protocol import current_peer

    reporter = current_reporter()
    for method, args, kwargs in current_peer().codec.decode(events):
        getattr(reporter, method)(*args, **kwargs)


def _call(name: str, *args: Any, **kwargs: Any) -> Any:
    from .protocol import current_peer

    return current_peer().request({"op": "call", "fn": name}, args, kwargs)


def _remote(name: str) -> Callable[..., Any]:
    module, _, attribute = name.partition(":")

    def call(*args: Any, **kwargs: Any) -> Any:
        return _call(name, *args, **kwargs)

    call.__name__ = call.__qualname__ = attribute.rsplit(".", 1)[-1]
    call.__module__ = __name__
    call.__doc__ = f"`{module}.{attribute}`, run in the knowledge process."
    return call


# --- the names the engine files take ---------------------------------------------------------

_BASE = "finesub.llm.knowledge.base"
kb_index_block_text = _remote(f"{_BASE}:kb_index_block_text")
knowledge_root_path = _remote(f"{_BASE}:knowledge_root_path")
knowledge_signals_enabled = _remote(f"{_BASE}:knowledge_signals_enabled")
knowledge_version = _remote(f"{_BASE}:knowledge_version")
knowledge_write_lock = _remote(f"{_BASE}:knowledge_write_lock")
load_entry_texts = _remote(f"{_BASE}:load_entry_texts")
load_index_entries = _remote(f"{_BASE}:load_index_entries")
load_index_texts = _remote(f"{_BASE}:load_index_texts")
load_preinjected_entries = _remote(f"{_BASE}:load_preinjected_entries")
match_index_keywords = _remote(f"{_BASE}:match_index_keywords")
match_terms = _remote(f"{_BASE}:match_terms")
worktree_blocks_knowledge_write = _remote(f"{_BASE}:worktree_blocks_knowledge_write")

harvest_rendered_edits_at_run_start = _remote("finesub.llm.knowledge.node.edit:harvest_rendered_edits_at_run_start")

_STYLE = "finesub.llm.knowledge.style"
load_style_catalog = _remote(f"{_STYLE}:load_style_catalog")
load_style_entries = _remote(f"{_STYLE}:load_style_entries")
record_style_injected = _remote(f"{_STYLE}:record_style_injected")
render_style_block = _remote(f"{_STYLE}:render_style_block")
resolve_style_keys = _remote(f"{_STYLE}:resolve_style_keys")
style_vote_facts = _remote(f"{_STYLE}:style_vote_facts")

render_subject_block = _remote("finesub.llm.knowledge.subjects:render_subject_block")
resolve_subject_keys = _remote("finesub.llm.knowledge.subjects:resolve_subject_keys")


class KnowledgeRepo:
    """`finesub.llm.knowledge.node.repo.KnowledgeRepo`, opened in the knowledge process.

    `KnowledgeRepo.open(root)` returns a handle to the repository the knowledge
    process opened; its methods and its `store` run there.
    """

    open = staticmethod(_remote("finesub.llm.knowledge.node.repo:KnowledgeRepo.open"))
    forget = staticmethod(_remote("finesub.llm.knowledge.node.repo:KnowledgeRepo.forget"))


# --- this process's own state ------------------------------------------------------------------


@contextmanager
def pinned_generation_rev(knowledge_root: str | Path | None) -> Iterator[int]:
    """`base.pinned_generation_rev`: the pin is this run's, the revision is read there."""

    from finesub.llm.knowledge import base

    root_key = str(knowledge_root_path(knowledge_root))
    rev = _call(f"{_BASE}:_repo", knowledge_root).rev
    pins = dict(base._GENERATION_PINS.get() or {})
    pins[root_key] = rev
    token = base._GENERATION_PINS.set(pins)
    try:
        yield rev
    finally:
        base._GENERATION_PINS.reset(token)


def repin_generation_rev(knowledge_root: str | Path | None) -> int | None:
    """`base.repin_generation_rev`: same rule, the store's current revision read there."""

    from finesub.llm.knowledge import base

    root_key = str(knowledge_root_path(knowledge_root))
    pins = dict(base._GENERATION_PINS.get() or {})
    if root_key not in pins:
        return None
    rev = _call(f"{_BASE}:_repo", knowledge_root).rev
    pins[root_key] = rev
    base._GENERATION_PINS.set(pins)
    return rev


# --- the knowledge package's own operations --------------------------------------------------


def prepare_subjects(subjects: Any, *, knowledge: str | None, task_id: str, knowledge_root: Any = None) -> dict[str, Any]:
    """`manage.prepare_subjects`, in the knowledge process."""

    return _call("finesub_knowledge.manage:prepare_subjects", subjects, knowledge=knowledge,
                 task_id=task_id, knowledge_root=knowledge_root)
