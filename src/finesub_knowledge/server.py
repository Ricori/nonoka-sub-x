"""The knowledge process: FineSub's knowledge code, executed for one client process.

`python -m finesub_knowledge.server --owner <pid>` is started by a client (a
task's worker) for itself, one knowledge process per client process
(engine-packages.md 11.2, owner 2026-11-12): the write lock is shared by the
threads of one process and exclusive between processes (`base.py`
`knowledge_write_lock`), and the generation pins and the once-per-process
warnings are process state too. Pairing keeps every one of those meaning what
it meant when the client opened the store itself.

It listens on a fresh named pipe (a Unix socket elsewhere) with a one-off key,
prints `{"address", "family", "authkey"}` as its first line on stdout and
closes stdout. Each client thread connects once; each connection is served on
its own thread, so a store connection made for a client thread is used by
that thread only, as before. It exits once the owner has exited and no
connection is left open.

An agent's MCP server is started by the agent CLI, not by the worker, with an
environment FineSub writes (`client.py`, `agent_session_host.py`; not
changed). It finds a live knowledge process for its root through the
registration this process writes beside the base (`registration_dir`).

What the client may call by name is `ALLOWED`: the knowledge functions the
engine files take from `finesub_knowledge.client`, plus `manage`'s
operations. Anything a call returns that is not plain data is lent as a
handle, and the client's further use of it executes here too.
"""

from __future__ import annotations

import argparse
import contextlib
import contextvars
import json
import os
import secrets
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, Mapping

from .protocol import PROTOCOL_VERSION, Peer, ProtocolError, bind_peer, registration_dir, resolve

#: What a client may call by name: `module:qualname`.
ALLOWED = frozenset({
    "finesub.llm.knowledge.base:_repo",
    "finesub.llm.knowledge.base:kb_index_block_text",
    "finesub.llm.knowledge.base:knowledge_root_path",
    "finesub.llm.knowledge.base:knowledge_signals_enabled",
    "finesub.llm.knowledge.base:knowledge_version",
    "finesub.llm.knowledge.base:knowledge_write_lock",
    "finesub.llm.knowledge.base:load_entry_texts",
    "finesub.llm.knowledge.base:load_index_entries",
    "finesub.llm.knowledge.base:load_index_texts",
    "finesub.llm.knowledge.base:load_preinjected_entries",
    "finesub.llm.knowledge.base:match_index_keywords",
    "finesub.llm.knowledge.base:match_terms",
    "finesub.llm.knowledge.base:worktree_blocks_knowledge_write",
    "finesub.llm.knowledge.node.edit:harvest_rendered_edits_at_run_start",
    "finesub.llm.knowledge.node.repo:KnowledgeRepo.open",
    "finesub.llm.knowledge.node.repo:KnowledgeRepo.forget",
    "finesub.llm.knowledge.style:load_style_catalog",
    "finesub.llm.knowledge.style:load_style_entries",
    "finesub.llm.knowledge.style:record_style_injected",
    "finesub.llm.knowledge.style:render_style_block",
    "finesub.llm.knowledge.style:resolve_style_keys",
    "finesub.llm.knowledge.style:style_vote_facts",
    "finesub.llm.knowledge.subjects:render_subject_block",
    "finesub.llm.knowledge.subjects:resolve_subject_keys",
    "finesub_knowledge.manage:prepare_subjects",
    "finesub_knowledge.manage:describe",
})

def _resolve_call(name: str) -> Callable[..., Any]:
    if name not in ALLOWED:
        raise ProtocolError(f"{name} is not a knowledge operation")
    return resolve(name)


class _RecordingReporter:
    """Collects what the knowledge code reports, for the client to report again."""

    def __init__(self) -> None:
        self.events: list[list[Any]] = []

    def __getattr__(self, method: str) -> Callable[..., None]:
        if method.startswith("_"):
            raise AttributeError(method)

        def record(*args: Any, **kwargs: Any) -> None:
            self.events.append([method, list(args), kwargs])

        return record


def _run(message: Mapping[str, Any], execute: Callable[[], Any], peer: Peer) -> tuple[Any, dict[str, Any]]:
    """Execute one request the way it would have run in the client: its pins, its reporter."""

    from finesub.llm.knowledge.base import bind_generation_pins
    from finesub.reporting import reporting_to

    recorder = _RecordingReporter() if message.get("rep") else None
    pins = {str(key): int(value) for key, value in (message.get("pins") or {}).items()}

    def inside() -> Any:
        bind_generation_pins(pins)
        with reporting_to(recorder):  # None binds the silent reporter
            return execute()

    def extra() -> dict[str, Any]:
        return {"events": peer.codec.encode(recorder.events)} if recorder is not None and recorder.events else {}

    try:
        result = contextvars.Context().run(inside)
    except BaseException as error:
        error.__knowledge_extra__ = extra()  # type: ignore[attr-defined]
        raise
    return result, extra()


class Server:
    def __init__(self, owner: int) -> None:
        self.owner = owner
        self.authkey = secrets.token_bytes(32)
        self.connections = 0
        self.owner_gone = threading.Event()
        self._lock = threading.Lock()
        self.registration: Path | None = None
        if os.name == "nt":
            self.family = "AF_PIPE"
            self.address = rf"\\.\pipe\finesub-knowledge-{os.getpid()}-{secrets.token_hex(8)}"
        else:
            self.family = "AF_UNIX"
            self.address = str(Path(tempfile.mkdtemp(prefix="finesub-knowledge-")) / "socket")
        from multiprocessing.connection import Listener

        self.listener = Listener(self.address, family=self.family, authkey=self.authkey)

    def endpoint(self) -> dict[str, Any]:
        return {"address": self.address, "family": self.family, "authkey": self.authkey.hex(),
                "pid": os.getpid(), "owner": self.owner, "protocol": PROTOCOL_VERSION}

    def register(self) -> None:
        """Say how to reach this process beside the base it serves (for MCP servers)."""

        try:
            from finesub.llm.knowledge.base import knowledge_root_path

            directory = registration_dir(knowledge_root_path())
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{os.getpid()}.json"
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.endpoint()), encoding="utf-8")
            os.replace(temporary, path)
            self.registration = path
        except Exception:  # noqa: BLE001 - no root configured: nothing to register under
            self.registration = None

    def unregister(self) -> None:
        if self.registration is not None:
            with contextlib.suppress(OSError):
                self.registration.unlink()

    def serve_forever(self) -> None:
        threading.Thread(target=self._watch_owner, name="owner-watch", daemon=True).start()
        while True:
            try:
                connection = self.listener.accept()
            except (OSError, EOFError):
                if self.owner_gone.is_set():
                    self._maybe_exit()
                continue
            except Exception:  # noqa: BLE001 - a client that failed the key: ignore it
                continue
            with self._lock:
                self.connections += 1
            threading.Thread(target=self._serve, args=(connection,), daemon=True).start()

    def _serve(self, connection: Any) -> None:
        peer: Peer | None = None
        try:
            hello = json.loads(connection.recv_bytes().decode("utf-8"))
            if hello.get("k") != "hello" or hello.get("protocol") != PROTOCOL_VERSION:
                return
            origin = f"client:{hello.get('pid')}:{hello.get('nonce')}"
            peer = Peer(connection, "server", origin, resolve_call=_resolve_call,
                        run=lambda message, execute: _run(message, execute, peer))
            bind_peer(peer)
            connection.send_bytes(json.dumps({"k": "hello", "protocol": PROTOCOL_VERSION}).encode("utf-8"))
            while True:
                message = peer.receive()
                if message.get("k") != "req":
                    raise ProtocolError(f"unexpected frame {message.get('k')!r}")
                peer.send(peer.serve(message))
        except (EOFError, OSError):
            pass
        finally:
            if peer is not None:
                # A client that went away inside `with knowledge_write_lock`
                # must not leave the base locked for the next task.
                peer.exit_entered()
            bind_peer(None)
            with contextlib.suppress(Exception):
                connection.close()
            with self._lock:
                self.connections -= 1
            self._maybe_exit()

    def _watch_owner(self) -> None:
        _wait_for_exit(self.owner)
        self.owner_gone.set()
        self._maybe_exit()

    def _maybe_exit(self) -> None:
        with self._lock:
            done = self.owner_gone.is_set() and self.connections == 0
        if done:
            self.unregister()
            os._exit(0)


def _wait_for_exit(pid: int) -> None:
    if os.name == "nt":
        import ctypes

        synchronize, infinite = 0x00100000, 0xFFFFFFFF
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(synchronize, False, pid)
        if not handle:
            return  # already gone
        try:
            kernel32.WaitForSingleObject(handle, infinite)
        finally:
            kernel32.CloseHandle(handle)
        return
    import time

    while True:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.5)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FineSub knowledge process for one client process")
    parser.add_argument("--owner", type=int, required=True, help="the client process this one serves")
    args = parser.parse_args(argv)
    server = Server(args.owner)
    server.register()
    sys.stdout.write(json.dumps(server.endpoint()) + "\n")
    sys.stdout.flush()
    # Nothing else may reach the client's pipe; FineSub code prints nowhere here.
    devnull = open(os.devnull, "w", encoding="utf-8")
    with contextlib.suppress(OSError):
        os.dup2(devnull.fileno(), 1)
    sys.stdout = devnull
    try:
        server.serve_forever()
    finally:
        server.unregister()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
