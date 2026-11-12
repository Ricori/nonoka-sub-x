#!/usr/bin/env python3
"""FineSub quality gate: one command, pass or fail.

    python scripts/quality_gate.py            # the checks that need no GPU
    python scripts/quality_gate.py --real     # also rerun the real samples
    python scripts/quality_gate.py --cloud    # also rerun the samples on Modal (costs money)

Checks (docs/transcription-engines-plan.md, section 0 and WP-Q):

1. quality lock     every locked engine file byte-identical to QUALITY_LOCK.json
   runtime pins     src/nonoka_x/resources/RUNTIME_PINS.json against what the
                    runtime is pinned to, and every finesub_bootstrap name the
                    locked engine uses still resolving (with --real, also the
                    installed runtime measured against the pins)
2. call equivalence what each task request makes Nonoka hand FineSub's
                    algorithm entry points, against tests/fixtures/quality/calls/
3. environment      the worker environment and the generated finesub.toml,
                    model catalog and env file, against tests/fixtures/quality/environment.json
4. LLM replay       requests the correction layer sends for a fixed stable, and
                    the final.srt / annotated.csv fixed answers render to,
                    against tests/fixtures/quality/replay/ (and, for the cases
                    built on the real sample, test_data/quality-baseline/replay/)
4b. knowledge base  the K1-K8 cases on a base with content (WP-K): requests,
                    ASR context, the knowledge page, the MCP kb tools, subject
                    preparation, and the base each leaves behind, against
                    tests/fixtures/quality/knowledge/cases/
5. real samples     (--real) the samples rerun to raw-srt on this machine's
                    managed runtime, against test_data/quality-baseline/real/
6. cloud samples    (--cloud) the samples rerun to stable by the unchanged
                    Modal compute legs (nonoka-sub-cloud/modal_compute, one L4
                    per GPU leg), against test_data/quality-baseline/cloud/ by
                    structure and statistics, not bytes (plan section 0.1, rule 3)

Exit status: 0 every check passed; 1 a check failed; 2 nothing failed, but
something was left undecided -- a replay case whose inputs are missing here,
an artifact `--real` cannot judge by bytes, or a `--cloud` statistic whose
threshold the owner has not approved yet (see the report). That is a decision
for the owner, not a pass.

`--record PART` rewrites a baseline from the current code. Doing that to make
the gate pass is exactly what the gate exists to prevent: only with the
owner's approval, in its own commit.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.quality_harness import ROOT as HARNESS_ROOT  # noqa: E402

assert HARNESS_ROOT == ROOT

PASS, FAIL, SKIP, UNDECIDED = "PASS", "FAIL", "SKIP", "UNDECIDED"


class Result:
    def __init__(self, name: str) -> None:
        self.name = name
        self.status = PASS
        self.summary = ""
        self.details: list[str] = []
        self.seconds = 0.0

    def fail(self, summary: str, details: list[str] | None = None) -> "Result":
        self.status, self.summary = FAIL, summary
        self.details += details or []
        return self


def _excerpt(text: str, limit: int) -> list[str]:
    lines = text.splitlines()
    if limit and len(lines) > limit:
        return lines[:limit] + [f"... ({len(lines) - limit} more lines; --full shows all)"]
    return lines


# --- 1. quality lock ------------------------------------------------------------


def check_lock(_args: argparse.Namespace) -> Result:
    from tests.quality_harness import lock

    result = Result("quality lock")
    problems = lock.problems()
    recorded = json.loads(lock.LOCK_FILE.read_text(encoding="utf-8")) if lock.LOCK_FILE.is_file() else {}
    if problems:
        return result.fail(f"{len(problems)} problem(s) in third_party/finesub/src", problems)
    result.summary = f"{len(recorded.get('files', []))} locked files match QUALITY_LOCK.json"
    return result


# --- 1b. runtime pins and the bootstrap surface ----------------------------------


def check_pins(args: argparse.Namespace) -> Result:
    from tests.quality_harness import pins

    result = Result("runtime pins and bootstrap surface")
    recorded = json.loads(pins.PINS_FILE.read_text(encoding="utf-8"))
    # Nonoka's manifests (since WP-R1), and finesub_bootstrap's own files the
    # transcription package still reads at run time.
    problems = pins.problems(recorded)
    surface = json.loads(pins.SURFACE_FILE.read_text(encoding="utf-8"))
    current = pins.bootstrap_surface()
    problems += [f"engine now uses (not recorded): {entry}" for entry in sorted(set(current) - set(surface))]
    problems += [f"engine no longer uses: {entry}" for entry in sorted(set(surface) - set(current))]
    problems += [f"does not resolve: {entry}" for entry in pins.unresolvable(surface)]
    if args.real:
        measured = pins.measure_installed()
        if "skipped" in measured:
            problems.append(f"installed runtime not measured: {measured['skipped']}")
        else:
            problems += [f"installed runtime: {line}" for line in pins.measured_problems(measured)]
            result.details.append(f"installed runtime measured into {pins.MEASURED_FILE.relative_to(ROOT).as_posix()}")
    if problems:
        return result.fail(f"{len(problems)} problem(s)", problems + result.details)
    packages = len(recorded["python"]["global"])
    result.summary = (f"{packages} packages, {len(recorded['tools']['resources'])} tools, "
                      f"{len(recorded['models'])} models pinned as recorded; {len(surface)} bootstrap names resolve"
                      + ("; installed runtime matches" if args.real else ""))
    return result


# --- 2. call equivalence -------------------------------------------------------


def _first_call_difference(expected: dict[str, Any], actual: dict[str, Any]) -> str:
    """Where two scenario records part, in words: which entry point and which argument."""

    for key in ("rejected", "exit", "failed", "request"):
        if expected.get(key) != actual.get(key):
            if key == "request":
                names = sorted(
                    name for name in set(expected.get(key) or {}) | set(actual.get(key) or {})
                    if (expected.get(key) or {}).get(name) != (actual.get(key) or {}).get(name)
                )
                return f"normalized request differs in: {', '.join(names)}"
            return f"{key}: {json.dumps(expected.get(key), ensure_ascii=False)[:200]} -> " \
                   f"{json.dumps(actual.get(key), ensure_ascii=False)[:200]}"
    old, new = expected.get("calls") or [], actual.get("calls") or []
    for index, (a, b) in enumerate(zip(old, new)):
        if a == b:
            continue
        if a.get("entry") != b.get("entry"):
            return f"call #{index + 1}: entry point {a.get('entry')} -> {b.get('entry')}"
        args_a, args_b = a.get("arguments") or {}, b.get("arguments") or {}
        changed = []
        for name in sorted(set(args_a) | set(args_b)):
            if args_a.get(name, "<absent>") != args_b.get(name, "<absent>"):
                changed.append(
                    f"{name}: {json.dumps(args_a.get(name, '<absent>'), ensure_ascii=False)[:160]}"
                    f" -> {json.dumps(args_b.get(name, '<absent>'), ensure_ascii=False)[:160]}"
                )
        others = sorted(key for key in set(a) | set(b) if key not in {"arguments", "entry"} and a.get(key) != b.get(key))
        return f"call #{index + 1} {a.get('entry')}: " + "; ".join(changed + [f"{key} differs" for key in others])
    if len(old) != len(new):
        extra = new[len(old):] if len(new) > len(old) else old[len(new):]
        return f"call count {len(old)} -> {len(new)} ({'added' if len(new) > len(old) else 'missing'}: " \
               + ", ".join(item.get("entry", "?") for item in extra[:5]) + ")"
    if expected.get("manifest") != actual.get("manifest"):
        names = sorted(
            name
            for name in set((expected.get("manifest") or {}).get("artifacts", {}))
            | set((actual.get("manifest") or {}).get("artifacts", {}))
            if (expected.get("manifest") or {}).get("artifacts", {}).get(name)
            != (actual.get("manifest") or {}).get("artifacts", {}).get(name)
        )
        return f"artifact manifest differs: {', '.join(names) or 'engine_commit'}"
    return "records differ (see diff)"


def check_calls(args: argparse.Namespace) -> Result:
    from tests.quality_harness import calls

    result = Result("call equivalence")
    records = calls.run_all()
    if args.record == "calls":
        calls.write_baselines(records)
        result.summary = f"RECORDED {len(records)} scenario baselines"
        return result
    found = calls.differences(records)
    broken = [name for name, record in records.items() if "rejected" in record or record.get("exit") != 0]
    if not found and not broken:
        result.summary = f"{len(records)} scenarios match tests/fixtures/quality/calls/"
        return result
    details = []
    for name in sorted(set(found) | set(broken)):
        path = calls.baseline_path(name).relative_to(ROOT).as_posix()
        expected = json.loads(calls.baseline_path(name).read_text(encoding="utf-8")) if calls.baseline_path(name).is_file() else {}
        details.append(f"{name}  [{path}]")
        details.append(f"    {_first_call_difference(expected, records.get(name, {}))}")
        if name in found:
            details += ["    " + line for line in _excerpt(found[name], 0 if args.full else 40)]
    return result.fail(f"{len(set(found) | set(broken))} of {len(records)} scenarios differ", details)


# --- 3. environment ------------------------------------------------------------


def check_environment(args: argparse.Namespace) -> Result:
    from tests.quality_harness import environment

    result = Result("worker environment and generated config")
    record = environment.run_all()
    if args.record == "environment":
        environment.write_baseline(record)
        result.summary = "RECORDED tests/fixtures/quality/environment.json"
        return result
    diff = environment.difference(record)
    if not diff:
        result.summary = "matches tests/fixtures/quality/environment.json"
        return result
    return result.fail("differs from tests/fixtures/quality/environment.json",
                       _excerpt(diff, 0 if args.full else 60))


# --- 4. LLM replay ---------------------------------------------------------------


def check_replay(args: argparse.Namespace) -> Result:
    from tests.quality_harness import replay

    result = Result("LLM replay")
    records = replay.run_all()
    unavailable = replay.unavailable()
    skipped = sorted(set(replay.CASES) - set(records))
    if args.record == "replay":
        replay.write_baselines(records)
        result.summary = f"RECORDED {len(records)} replay baselines" + (
            f" ({len(skipped)} not recorded: inputs missing here)" if skipped else "")
        return result
    found = replay.differences(records)
    broken = [name for name, record in records.items() if record.get("exit") != 0]
    note = f"; {len(skipped)} case(s) not run: inputs missing here" if skipped else ""
    # A case whose inputs are missing compared nothing: undecided, never a pass.
    not_run = [f"NOT RUN   {name}: missing {', '.join(unavailable.get(name) or ['inputs'])}" for name in skipped]
    if not found and not broken:
        requests = sum(len(record.get("exchanges", [])) for record in records.values())
        result.summary = f"{len(records)} cases, {requests} requests and their outputs match{note}"
        if skipped:
            result.status = UNDECIDED
            result.details = not_run + ["(real-sample recognition is kept under test_data/, outside the "
                                        "repository; run the gate in the main checkout that has it)"]
        return result
    details = list(not_run)
    for name in sorted(set(found) | set(broken)):
        details.append(f"{name}  [{replay.baseline_path(name).relative_to(ROOT).as_posix()}]")
        if records[name].get("exit") != 0:
            details.append(f"    run failed: {records[name].get('failed')}")
        if name in found:
            details += ["    " + line for line in _excerpt(found[name], 0 if args.full else 40)]
    return result.fail(f"{len(set(found) | set(broken))} of {len(records)} cases differ{note}", details)


# --- 4b. knowledge base -----------------------------------------------------------


def check_knowledge(args: argparse.Namespace) -> Result:
    from tests.quality_harness import knowledge

    result = Result("knowledge base")
    records = knowledge.run_all()
    if args.record == "knowledge":
        knowledge.write_baselines(records)
        result.summary = f"RECORDED {len(records)} knowledge-case baselines"
        return result
    found = knowledge.differences(records)
    broken = [name for name, record in records.items() if record.get("exit") not in (None, 0)]
    if not found and not broken:
        result.summary = f"{len(records)} cases (K1-K8) match tests/fixtures/quality/knowledge/cases/"
        return result
    details = []
    for name in sorted(set(found) | set(broken)):
        details.append(f"{name}  [{knowledge.baseline_path(name).relative_to(ROOT).as_posix()}]")
        if name in broken:
            details.append(f"    run failed: {records[name].get('failed')}")
        if name in found:
            details += ["    " + line for line in _excerpt(found[name], 0 if args.full else 40)]
    return result.fail(f"{len(set(found) | set(broken))} of {len(records)} knowledge cases differ", details)


# --- 5. real samples -----------------------------------------------------------------


def _judge_sample(name: str, comparison: dict[str, Any], baseline_comparison: dict[str, Any],
                  standards: dict[str, str], lines: list[str], failures: list[str],
                  undecided: list[str]) -> tuple[int, int]:
    """Judge one sample's artifacts against the baseline; returns (by bytes, by standard).

    An artifact the baseline's two runs reproduced byte for byte must match
    byte for byte; one they did not is judged by its accepted standard (plan
    section 0, rule 3), or left undecided when it has none. Used by `--real`;
    `--cloud` judges structure and statistics instead (`cloud.judge`).
    """

    by_bytes = by_standard = 0
    for artifact, entry in sorted(comparison.items()):
        reproducible = baseline_comparison.get(artifact, {}).get("status") == "identical"
        standard = standards.get(artifact)
        where = f"{name}/{artifact}"
        if entry["status"] == "identical":
            by_bytes += 1
            lines.append(f"  ok        {where}")
        elif entry["status"] == "missing":
            failures.append(where)
            lines.append(f"  MISSING   {where}: only in the {'baseline' if entry.get('in') == 'first' else 'new run'}")
        elif reproducible:
            failures.append(where)
            lines.append(f"  DIFFERS   {where}: {json.dumps(entry.get('first_difference', ''), ensure_ascii=False)[:300]}")
        elif standard is not None:
            if entry.get(standard) is True:
                by_standard += 1
                lines.append(f"  ok        {where} (by the accepted standard: {standard})")
            else:
                failures.append(where)
                detail = entry.get("first_difference") or entry.get("decoded_pcm_sha256") or entry.get("status")
                lines.append(f"  DIFFERS   {where} under the accepted standard {standard}: "
                             f"{json.dumps(detail, ensure_ascii=False)[:300]}")
        else:
            undecided.append(where)
            facts = {key: entry[key] for key in (
                "identical_outside_metadata", "identical_decoded_samples", "differing_path_count",
            ) if key in entry}
            lines.append(f"  UNJUDGED  {where}: not byte-reproducible even on unchanged code; {facts}")
    return by_bytes, by_standard


def check_real(args: argparse.Namespace) -> Result:
    from tests.quality_harness import real

    result = Result("real samples (raw-srt, managed runtime)")
    if not args.real:
        result.status, result.summary = SKIP, "not requested (pass --real)"
        return result
    if args.record == "real":
        report = real.record()
        result.summary = "RECORDED test_data/quality-baseline/real/ (two runs per sample, see report.json)"
        result.details = [json.dumps(report["samples"][name]["runs"]) for name in report["samples"]]
        return result
    issues = real.runtime_issues()
    if issues:
        return result.fail("cannot run here", issues)
    report_path = real.BASELINE_ROOT / "report.json"
    if not report_path.is_file():
        return result.fail(f"no baseline: {report_path.relative_to(ROOT).as_posix()} (record one first)")
    recorded = json.loads(report_path.read_text(encoding="utf-8"))
    scratch = Path(tempfile.mkdtemp(prefix="nonoka-quality-real-"))
    failures, undecided, lines = [], [], []
    by_bytes = by_standard = 0
    try:
        outcome = real.check(scratch)
        for name, sample in outcome.items():
            if "error" in sample:
                failures.append(name)
                lines.append(f"{name}: {sample['error']}")
                continue
            if sample["run"]["exit"] != 0:
                failures.append(name)
                lines.append(f"{name}: the run failed (exit {sample['run']['exit']}); see {scratch / name / 'events.ndjson'}")
                continue
            counts = _judge_sample(name, sample["comparison"], recorded["samples"][name]["comparison"],
                                   real.ACCEPTED_STANDARDS, lines, failures, undecided)
            by_bytes += counts[0]
            by_standard += counts[1]
    finally:
        if not args.keep:
            shutil.rmtree(scratch, ignore_errors=True)
        else:
            lines.append(f"runs kept in {scratch}")
    result.details = lines
    if failures:
        result.status, result.summary = FAIL, f"{len(failures)} reproducible artifact(s) changed"
    elif undecided:
        result.status = UNDECIDED
        result.summary = (f"every reproducible artifact matches; {len(undecided)} artifact(s) cannot be judged "
                          "by bytes (owner decision, see test_data/quality-baseline/real/report.json)")
    else:
        result.summary = (f"every artifact matches: {by_bytes} byte for byte, {by_standard} by the accepted "
                          "standards (plan section 0, rule 3)")
    return result


# --- 6. cloud samples ---------------------------------------------------------------


def check_cloud(args: argparse.Namespace) -> Result:
    """Structure and statistics against the baseline run of the same accelerator (plan section 0.1, rule 3).

    Not bytes: the cloud does not reproduce itself (plan 2.10). A structural
    problem fails; a statistic fails over its threshold and is undecided while
    its threshold is not approved (`cloud.THRESHOLDS`).
    """

    from tests.quality_harness import cloud

    result = Result(f"cloud samples (stable, Modal {cloud.GPU})")
    if not args.cloud:
        result.status, result.summary = SKIP, "not requested (pass --cloud; rents GPUs on Modal)"
        return result
    if args.record == "cloud":
        report = cloud.record()
        result.summary = "RECORDED test_data/quality-baseline/cloud/ (two runs per sample, see report.json)"
        result.details = [json.dumps(report["samples"][name]["runs"], ensure_ascii=False, default=str)
                          for name in report["samples"]]
        if not all(run.get("ok") for sample in report["samples"].values() for run in sample["runs"]):
            result.status = FAIL
            result.summary += "; a run failed, so this baseline is not usable"
        return result
    issues = cloud.issues()
    if issues:
        return result.fail("cannot run here", issues)
    report_path = cloud.BASELINE_ROOT / "report.json"
    if not report_path.is_file():
        return result.fail(f"no baseline: {report_path.relative_to(ROOT).as_posix()} (record one first)")
    recorded = json.loads(report_path.read_text(encoding="utf-8"))
    scratch = Path(tempfile.mkdtemp(prefix="nonoka-quality-cloud-"))
    failures, undecided, lines = [], [], []
    lines.append(f"thresholds: {json.dumps(asdict(cloud.THRESHOLDS))}")
    try:
        outcome = cloud.check(scratch)
        if "skipped" in outcome:
            return result.fail("cannot run here", outcome["skipped"])
        for name, sample in outcome.items():
            if "error" in sample:
                failures.append(name)
                lines.append(f"{name}: {sample['error']}")
                continue
            if not sample["run"].get("ok"):
                failures.append(name)
                lines.append(f"{name}: the run failed ({sample['run'].get('error') or sample['run'].get('failures')}); "
                             f"see {scratch / 'runner.log'} and {scratch / name / 'run.json'}")
                continue
            # Reported, not judged: a different card or a rebuilt accelerator
            # explains a difference, it does not excuse one.
            index = list(cloud.BASELINE_RUNS).index(sample["baseline"])
            runs = recorded["samples"][name]["runs"]
            for note in cloud.environment_changes(runs[index] if index < len(runs) else {}, sample["run"]):
                lines.append(f"  note      {name}: {note}")
            separations = sample["run"].get("separations") or []
            lines.append(f"  note      {name}: separator accelerator {[entry.get('accel') for entry in separations]}, "
                         f"compared with {sample['baseline_why']}")
            verdict = cloud.judge(sample["statistics"]["comparison"])
            failures += [f"{name}: {failure}" for failure in verdict["failures"]]
            undecided += [f"{name}: {statistic}" for statistic in verdict["undecided"]]
            lines += [f"  {line.split(' ', 1)[0]:<9} {name}: {line.split(' ', 1)[1].lstrip()}"
                      for line in verdict["lines"]]
    finally:
        if not args.keep:
            shutil.rmtree(scratch, ignore_errors=True)
        else:
            lines.append(f"runs kept in {scratch}")
    result.details = lines
    if failures:
        result.status, result.summary = FAIL, f"{len(failures)} structural problem(s), statistic(s) or run(s) failed"
    elif undecided:
        result.status = UNDECIDED
        result.summary = (f"structure matches; {len(undecided)} statistic(s) have no approved threshold yet "
                          "(plan section 0.1 rule 3, appendix C item 3)")
    else:
        result.summary = ("structure matches and every statistic is within its approved threshold "
                          "(plan section 0.1, rule 3)")
    return result


CHECKS: dict[str, Callable[[argparse.Namespace], Result]] = {
    "lock": check_lock,
    "pins": check_pins,
    "calls": check_calls,
    "environment": check_environment,
    "replay": check_replay,
    "knowledge": check_knowledge,
    "real": check_real,
    "cloud": check_cloud,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--real", action="store_true", help="also rerun the real samples (needs the managed runtime and GPU)")
    parser.add_argument("--only", choices=sorted(CHECKS), action="append", help="run only this check (repeatable)")
    parser.add_argument("--full", action="store_true", help="print whole diffs")
    parser.add_argument("--cloud", action="store_true",
                        help="also rerun the samples on Modal with the unchanged compute legs (rents GPUs, costs money)")
    parser.add_argument("--keep", action="store_true", help="keep the --real / --cloud runs' directories")
    parser.add_argument("--record", choices=("calls", "environment", "replay", "knowledge", "real", "cloud"),
                        help="rewrite that baseline from the current code (owner approval only)")
    args = parser.parse_args(argv)
    if args.record == "real":
        args.real = True
    if args.record == "cloud":
        args.cloud = True
    selected = args.only or (list(CHECKS) if not args.record else ["lock", args.record])

    print("FineSub quality gate")
    if args.real:
        from tests.quality_harness.hygiene import inventory

        machine = inventory()
        print(f"  influencing variables in this environment (kept out of every child): "
              f"{', '.join(machine['variables']) or 'none'}")
        for location, entries in machine["default_location_files"].items():
            print(f"  default-location files present: {location} ({len(entries)} entries)")
    results = []
    for name in selected:
        started = time.perf_counter()
        try:
            result = CHECKS[name](args)
        except Exception as error:  # noqa: BLE001 - a check that crashes has failed
            result = Result(name).fail(f"crashed: {type(error).__name__}", str(error).splitlines()[-40:])
        result.seconds = time.perf_counter() - started
        results.append(result)
        print(f"[{result.status}] {result.name}: {result.summary}  ({result.seconds:.1f}s)", flush=True)
        for line in result.details:
            print(f"       {line}")
    statuses = {result.status for result in results}
    if FAIL in statuses:
        verdict, code = "FAIL", 1
    elif UNDECIDED in statuses:
        verdict, code = "UNDECIDED (nothing failed; see the UNDECIDED check above)", 2
    else:
        verdict, code = "PASS", 0
    print(f"RESULT: {verdict}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
