"""The correction package's process: `python -m finesub_correct` (started by the core's engine host).

A task's correction process answers, in order: `begin` (the task's LLM model
pin), `task_mode` (what a Chinese-source task cannot use), `check` (before the
transcription), `run` (after it), or `axis` (a source-text axis, instead of
both). One process serves the whole task, so what FineSub keeps per process is
what one worker process kept before.

The sidecar's resident process answers single calls instead: `llm.complete`
(the editor's and plugins' LLM calls, formerly `nonoka_x.llm_worker`),
`catalog` (the packaged model catalog the settings page offers), and
`product_rows` (the lines of a task's FineSub stable with their speakers, which
the projection reads).
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path
from typing import Any, Mapping

from . import bridge
from .engine import FineSubCorrection, NoSourceText, for_task_mode, run_source_text
from .failures import describe_failure
from .llm import Session, failure
from .wire import ForwardingReporter, Wire, serve


class AxisError(ValueError):
    """The axis cannot be translated: it has no source text."""


#: What this process learned of FineSub's packaged catalog before its own files existed.
PACKAGED: dict[str, Any] = {}


def catalog_facts() -> dict[str, Any]:
    """The packaged model catalog and local-agent roster, as the settings page offers them.

    Read with no file of the user's in place: the packaged declaration is the
    only source of these, and a broken user route must not reach the settings.
    """

    from finesub.llm.routing.model_catalog import default_model_catalog
    from finesub.llm.routing.model_routes import load_model_routes

    gemini = [
        {
            "provider_tier": entry.provider_tier,
            "api_model_id": entry.api_model_id,
            "fact_id": entry.fact_id,
            "display_name": entry.display_name,
            "supports_audio": bool(entry.supports_audio),
            "supports_video": bool(entry.supports_video),
            "supports_native_search": bool(entry.supports_native_search),
            "self_reported": bool(entry.self_reported),
        }
        for entry in default_model_catalog()
    ]
    routes = load_model_routes(user_config={})
    local_agent: dict[str, dict[str, dict[str, Any]]] = {}
    for target in routes.targets.values():
        if target.backend != "local_agent":
            continue
        fact = routes.facts.get(target.fact_id)
        if fact is None:
            continue
        local_agent.setdefault(str(fact.provider_tier), {})[fact.api_model_id] = {
            "api_model_id": fact.api_model_id,
            "display_name": fact.display_name,
            "supports_audio": bool(fact.supports_audio),
            "supports_video": bool(fact.supports_video),
            "supports_native_search": bool(fact.supports_native_search),
            "target_id": target.id,
        }
    return {"gemini": gemini, "local_agent": {tier: list(models.values()) for tier, models in local_agent.items()}}


def apply_context(context: Mapping[str, Any], wire: Wire) -> None:
    """FineSub's correction half, from its launch context.

    `paths.run`, `paths.state_file`, `paths.knowledge_root`; `secrets` (the
    LLM and search keys by name, and `NONOKA_CLOUD_KEY`); `llm` (the routes,
    endpoints and pins the files are derived from); `proxy`;
    `knowledge_endpoint` (the task's knowledge process).
    """

    bridge.drop_inherited()
    paths = context.get("paths") or {}
    variables: dict[str, str] = {}
    if paths.get("run"):
        run = Path(paths["run"])
        named = bridge.file_variables(run)
        os.environ.update(named)
        PACKAGED["catalog"] = catalog_facts()
        bridge.derive_llm_files(run, context.get("llm") or {})
        bridge.forget_read_configuration()
    if paths.get("state_file"):
        variables["FINESUB_STATE_FILE"] = str(paths["state_file"])
    variables.update(bridge.knowledge_variables(context))
    variables.update(bridge.secret_variables(context))
    os.environ.update(variables)
    bridge.install_proxy_policy(context.get("proxy"))
    bridge.install_region_policy()


def main(argv: list[str] | None = None, *, after_context: Any = None) -> int:
    wire = Wire()
    reporter = ForwardingReporter(wire)
    state: dict[str, Any] = {}

    def correction() -> FineSubCorrection:
        if "engine" not in state:
            state["engine"] = FineSubCorrection(
                reporter, log=wire.log, cloud_provider=str((state.get("llm") or {}).get("cloud_provider") or ""),
            )
        return state["engine"]

    def knowledge(request: Mapping[str, Any]) -> Any:
        """The task's knowledge process, started after this one: reached from now on."""

        os.environ.update(bridge.knowledge_variables({"knowledge_endpoint": request["endpoint"]}))
        return {}

    def begin(request: Mapping[str, Any]) -> Any:
        bridge.install_llm_model_override(request.get("llm_model"), request.get("task_groups"))
        return {}

    def task_mode(request: Mapping[str, Any]) -> Any:
        return for_task_mode(request["request"], Path(request["record_dir"]))

    def check(request: Mapping[str, Any]) -> Any:
        correction().check(request["job"])
        return {}

    def run(request: Mapping[str, Any]) -> Any:
        return correction().run(request["job"])

    def axis(request: Mapping[str, Any]) -> Any:
        from finesub.reporting import quieted_libraries, reporting_to

        from .engine import correction_proofread

        options = dict(request.get("options") or {})
        options["proofread"] = correction_proofread(options, str((state.get("llm") or {}).get("cloud_provider") or ""))
        with reporting_to(reporter), quieted_libraries("normal"):
            try:
                result = run_source_text(
                    request["rows"],
                    output_path=request["output_path"],
                    task_id=str(request.get("task_id") or ""),
                    task_artifact_dir=request["task_artifact_dir"],
                    task_summary=str(request.get("task_summary") or ""),
                    options=options,
                    knowledge=str(request.get("knowledge") or "none"),
                    source_path=request.get("source_path"),
                    knowledge_subjects=request.get("knowledge_subjects") or None,
                    on_notice=lambda notice: wire.log(notice),
                )
            except NoSourceText as error:
                raise AxisError(str(error)) from None
        return {
            "stable_json": str(result.stable_json),
            "final_srt": str(result.final_srt),
            "annotated_csv": str(result.annotated_csv),
        }

    session = Session()

    def complete(request: Mapping[str, Any]) -> Any:
        try:
            return session.complete(request["request"])
        except Exception as exc:  # noqa: BLE001 - every failure of a call is an answer about it
            traceback.print_exc(file=sys.stderr)
            return failure(exc)

    def catalog(_request: Mapping[str, Any]) -> Any:
        return PACKAGED.get("catalog") or catalog_facts()

    def product_rows(request: Mapping[str, Any]) -> Any:
        from .engine import product_rows as rows

        return rows(request["stable_json"])

    def remember(context: Mapping[str, Any]) -> None:
        state["llm"] = dict(context.get("llm") or {})
        if after_context is not None:
            after_context(context)

    try:
        return serve(
            {
                "begin": begin,
                "knowledge": knowledge,
                "task_mode": task_mode,
                "check": check,
                "run": run,
                "axis": axis,
                "llm.complete": complete,
                "catalog": catalog,
                "product_rows": product_rows,
            },
            on_context=apply_context,
            describe_error=describe_failure,
            after_context=remember,
            wire=wire,
        )
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
