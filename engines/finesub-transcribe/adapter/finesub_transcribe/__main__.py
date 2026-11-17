"""The transcription package's process: `python -m finesub_transcribe` (started by the core's engine host).

One request, `transcribe`: the core's `TranscriptionJob` (as JSON) in, its
`TranscriptionResult` out. Before it, the launch context becomes what
FineSub's transcription half reads (`apply_context`).
"""

from __future__ import annotations

import os
import sys
from typing import Any, Mapping

from . import bridge, engine
from .wire import ForwardingReporter, Wire, serve
from .failures import describe_failure


def apply_context(context: Mapping[str, Any], wire: Wire) -> None:
    """FineSub's transcription half, from its launch context.

    `paths.run` (this process's run directory), `paths.state_file`,
    `paths.knowledge_root`; `runtime.models` (the model layer), `runtime.cache`
    (this package's directory under the big-file root's `cache\\`); `secrets`
    (only what `PACKAGE.json` names: `HF_TOKEN`, when the user set one); `llm`
    (the routes: the transcription half's preparation reads the route presets);
    `proxy`; `knowledge_endpoint` (the task's knowledge process).
    """

    bridge.drop_inherited()
    paths = context.get("paths") or {}
    if paths.get("run"):
        bridge.bind_llm_files(context)
    variables: dict[str, str] = {}
    if paths.get("state_file"):
        variables["FINESUB_STATE_FILE"] = str(paths["state_file"])
    if (context.get("runtime") or {}).get("models"):
        variables.update(bridge.model_variables(context))
    variables.update(bridge.knowledge_variables(context))
    variables.update(bridge.secret_variables(context))
    os.environ.update(variables)
    bridge.install_proxy_policy(context.get("proxy"))
    bridge.install_region_policy()


def main(argv: list[str] | None = None, *, after_context: Any = None) -> int:
    wire = Wire()
    reporter = ForwardingReporter(wire)

    def transcribe(request: Mapping[str, Any]) -> Any:
        job = request["job"]
        options = job.get("options") or {}
        # The task's LLM model pin, applied once per task in its first engine
        # process (the core says which): the worker applied it once in its own.
        if options.get("install_llm_model"):
            bridge.install_llm_model_override(options.get("llm_model"), options.get("task_groups"))
        return engine.transcribe(job, reporter)

    def knowledge(request: Mapping[str, Any]) -> Any:
        """The task's knowledge process, started after this one: reached from now on."""

        os.environ.update(bridge.knowledge_variables({"knowledge_endpoint": request["endpoint"]}))
        return {}

    return serve({"transcribe": transcribe, "knowledge": knowledge}, on_context=apply_context, describe_error=describe_failure,
                 after_context=after_context, wire=wire)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
