"""The knowledge package's process: `python -m finesub_knowledge_host` (started by the core's engine host).

Requests:

- `prepare_subjects`: resolve a task's picked subjects to the names the engine
  takes, creating new ones first (`finesub_knowledge.manage.prepare_subjects`);
- `serve`: become the task's knowledge process (`finesub_knowledge.server`):
  answer with the endpoint the task's engine processes connect to, then serve
  them until the owner (the core's task worker) has exited and the last
  connection is closed -- one knowledge process for every engine process of a
  task, as one worker process had one (WP-K, engine-packages.md 11.2);
- `manage`: the knowledge page's list, read or save
  (`finesub_knowledge.manage.handle`), answered as `{"result"}` or `{"error"}`.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import Any, Mapping

from . import bridge
from .failures import describe_failure
from .wire import Wire, serve


def apply_context(context: Mapping[str, Any], wire: Wire) -> None:
    """FineSub's knowledge code, from its launch context: `paths.run`, `paths.state_file`, `paths.knowledge_root`.

    The knowledge code reads no key, route or model; its file variables name
    files that are not there, which FineSub reads as "nothing configured".
    """

    bridge.drop_inherited()
    bridge.bind_package_data(context)
    paths = context.get("paths") or {}
    variables: dict[str, str] = {}
    if paths.get("run"):
        variables.update(bridge.file_variables(Path(paths["run"])))
    if paths.get("state_file"):
        variables["FINESUB_STATE_FILE"] = str(paths["state_file"])
    root = paths.get("knowledge_root")
    if root:
        variables["FINESUB_KNOWLEDGE_ROOT"] = str(root)
    os.environ.update(variables)


def main(argv: list[str] | None = None, *, after_context: Any = None) -> int:
    wire = Wire()
    serving: dict[str, Any] = {}

    def prepare_subjects(request: Mapping[str, Any]) -> Any:
        from finesub_knowledge.manage import prepare_subjects as prepare

        return prepare(
            request.get("subjects") or [],
            knowledge=request.get("knowledge"),
            task_id=str(request.get("task_id") or ""),
            knowledge_root=request.get("knowledge_root"),
        )

    def manage(request: Mapping[str, Any]) -> Any:
        from finesub_knowledge.manage import handle

        try:
            return {"result": handle(dict(request.get("request") or {}))}
        except Exception as exc:  # noqa: BLE001 - the page shows the reason
            return {"error": str(exc)}

    def serve_task(request: Mapping[str, Any]) -> Any:
        from finesub_knowledge.server import Server

        if "server" in serving:
            return serving["server"].endpoint()
        server = Server(int(request.get("owner") or os.getppid()))
        server.register()
        thread = threading.Thread(target=server.serve_forever, name="knowledge-server", daemon=True)
        thread.start()
        serving["server"], serving["thread"] = server, thread
        return server.endpoint()

    code = serve(
        {"prepare_subjects": prepare_subjects, "serve": serve_task, "manage": manage},
        on_context=apply_context,
        describe_error=describe_failure,
        after_context=after_context,
        wire=wire,
    )
    server = serving.get("server")
    if server is None:
        return code
    # The core has said all it will; the task's engine processes may still be
    # talking. The server ends this process once its owner is gone and no
    # connection is left (`Server._maybe_exit`).
    try:
        serving["thread"].join()
    finally:
        server.unregister()
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
