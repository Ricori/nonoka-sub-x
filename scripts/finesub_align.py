#!/usr/bin/env python3
"""Report how an upstream FineSub release differs on the key algorithm paths.

``diff --ref <tag>`` compares three trees, all restricted to the key algorithm
paths of plan section 3.2 (``KEY_DIRECTORIES`` and ``KEY_FILES`` below):

* the *base*: upstream at the commit ``third_party/finesub/UPSTREAM.json``
  records;
* the *target*: upstream at ``<tag>``;
* the *local* vendor under ``third_party/finesub``.

It reports what upstream changed between base and target, what the local
vendor changed relative to the base (the entries of
``third_party/finesub/LOCAL_CHANGES.md``), and the files both touched, which an
upgrade has to merge by hand. It only reports: no file is written, and the
exit status is non-zero only when the comparison itself could not run.

The two upstream trees are fetched as GitHub source archives unless
``--base-dir`` / ``--upstream-dir`` name an already extracted repository root
(the directory holding ``src/finesub``), which also makes the command work
offline. An upgrade itself is a separate, user-approved change (plan section
0, rule 7) and goes through ``scripts/quality_gate.py``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_VENDOR = REPO_ROOT / "third_party" / "finesub"
ENGINE = "src/finesub"
ARCHIVE_TIMEOUT_SECONDS = 120

#: Key algorithm directories, relative to ``src/finesub``: every file below
#: them is followed, except under ``EXCLUDED_DIRECTORIES``.
KEY_DIRECTORIES = (
    "speech",
    "media",
    "subtitles",
    "llm/stages",
    "llm/prompt_templates",
)

#: ``speech/joint/`` is the joint ASR backend the refactor moves out of the
#: engine (plan WP1); it is orchestration, not an algorithm Nonoka follows.
EXCLUDED_DIRECTORIES = ("speech/joint",)

#: Key algorithm files outside those directories, relative to ``src/finesub``.
#: The ``llm/`` and ``llm/knowledge/`` entries were confirmed by the user on
#: 2026-11-11 (plan WP-A).
KEY_FILES = (
    "text.py",
    # Prompts.
    "llm/prompts.py",
    "llm/prompt_compose.py",
    "llm/prompt_constants.py",
    "llm/prompt_tables.py",
    "llm/prompt_variants.py",
    "llm/prepare_prompts.py",
    "llm/request_prompts.py",
    "llm/example_builder.py",
    "llm/shape_criteria.py",
    "llm/general_context.py",
    "llm/injection_budget.py",
    "llm/kb_phonetic_hints.py",
    # Windowing.
    "llm/chunking.py",
    "llm/grouping.py",
    "llm/length_tier.py",
    "llm/token_budget.py",
    "llm/token_truncate.py",
    # Answer parsing and repair.
    "llm/output_protocol.py",
    "llm/output_tags.py",
    "llm/session_contract.py",
    "llm/text_cells.py",
    "llm/serial_shift.py",
    "llm/correction_patch.py",
    "llm/degeneration.py",
    "llm/content_filter.py",
    # The correction and preparation tasks themselves.
    "llm/correction_translation.py",
    "llm/correction_request.py",
    "llm/research.py",
    "llm/overlap_candidates.py",
    # Knowledge rendered into the correction windows.
    "llm/knowledge/style.py",
    "llm/knowledge/style_marks.py",
    "llm/knowledge/subjects.py",
    "llm/knowledge/entries.py",
    "llm/knowledge/node/render.py",
)


class AlignError(RuntimeError):
    """Raised when a tree cannot be fetched or read."""


def is_key_path(relative: str) -> bool:
    """Whether a path relative to ``src/finesub`` is a key algorithm path."""

    if any(relative == d or relative.startswith(d + "/") for d in EXCLUDED_DIRECTORIES):
        return False
    if relative in KEY_FILES:
        return True
    return any(relative.startswith(d + "/") for d in KEY_DIRECTORIES)


def _ignored(path: Path) -> bool:
    return "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}


def key_files(root: Path) -> dict[str, str]:
    """Map each key path under ``root/src/finesub`` to the sha256 of its bytes."""

    engine = root / ENGINE
    if not engine.is_dir():
        raise AlignError(f"{root} has no {ENGINE} directory")
    files: dict[str, str] = {}
    for path in engine.rglob("*"):
        if not path.is_file() or _ignored(path.relative_to(engine)):
            continue
        relative = path.relative_to(engine).as_posix()
        if is_key_path(relative):
            files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


@dataclass(frozen=True)
class Changes:
    added: tuple[str, ...]
    removed: tuple[str, ...]
    modified: tuple[str, ...]

    @property
    def paths(self) -> set[str]:
        return {*self.added, *self.removed, *self.modified}

    def as_dict(self) -> dict[str, list[str]]:
        return {"added": list(self.added), "removed": list(self.removed), "modified": list(self.modified)}


def compare(before: dict[str, str], after: dict[str, str]) -> Changes:
    return Changes(
        added=tuple(sorted(after.keys() - before.keys())),
        removed=tuple(sorted(before.keys() - after.keys())),
        modified=tuple(sorted(p for p in before.keys() & after.keys() if before[p] != after[p])),
    )


def github_slug(repository: str) -> str:
    parsed = urllib.parse.urlparse(repository)
    parts = [part for part in parsed.path.removesuffix(".git").split("/") if part]
    if parsed.netloc != "github.com" or len(parts) != 2:
        raise AlignError(f"not a GitHub repository URL: {repository}")
    return "/".join(parts)


def fetch_tree(repository: str, ref: str, destination: Path) -> Path:
    """Download and extract upstream at ``ref``; return the repository root."""

    url = f"https://codeload.github.com/{github_slug(repository)}/tar.gz/{urllib.parse.quote(ref, safe='')}"
    archive = destination / "source.tar.gz"
    try:
        with urllib.request.urlopen(url, timeout=ARCHIVE_TIMEOUT_SECONDS) as response, archive.open("wb") as out:
            while chunk := response.read(1 << 20):
                out.write(chunk)
    except OSError as exc:
        raise AlignError(f"cannot download {url}: {exc}") from exc
    extracted = destination / "tree"
    try:
        with tarfile.open(archive, "r:gz") as bundle:
            bundle.extractall(extracted, filter="data")
    except (OSError, tarfile.TarError) as exc:
        raise AlignError(f"cannot extract {url}: {exc}") from exc
    roots = [path for path in extracted.iterdir() if path.is_dir()]
    if len(roots) != 1:
        raise AlignError(f"unexpected archive layout from {url}")
    return roots[0]


def build_report(base: Path, target: Path, vendor: Path) -> dict[str, object]:
    base_files = key_files(base)
    upstream = compare(base_files, key_files(target))
    local = compare(base_files, key_files(vendor))
    return {
        "upstream": upstream.as_dict(),
        "local": local.as_dict(),
        "both": sorted(upstream.paths & local.paths),
    }


def _section(title: str, changes: dict[str, list[str]]) -> Iterable[str]:
    total = sum(len(paths) for paths in changes.values())
    yield f"{title}: {total} file(s)"
    for kind in ("added", "removed", "modified"):
        for path in changes[kind]:
            yield f"  {kind:<8} {path}"


def render_text(report: dict[str, object]) -> str:
    lines = [
        f"FineSub key algorithm paths: {report['base_ref']} ({report['base_commit']}) -> {report['target_ref']}",
        "",
        *_section("Changed upstream", report["upstream"]),  # type: ignore[arg-type]
        "",
        *_section("Changed locally (see third_party/finesub/LOCAL_CHANGES.md)", report["local"]),  # type: ignore[arg-type]
        "",
        f"Changed on both sides, to merge by hand: {len(report['both'])} file(s)",  # type: ignore[arg-type]
        *(f"  {path}" for path in report["both"]),  # type: ignore[union-attr]
    ]
    return "\n".join(lines) + "\n"


def run_diff(args: argparse.Namespace) -> dict[str, object]:
    vendor: Path = args.vendor_dir
    try:
        upstream = json.loads((vendor / "UPSTREAM.json").read_text(encoding="utf-8"))
        repository, commit = upstream["repository"], upstream["commit"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise AlignError(f"cannot read {vendor / 'UPSTREAM.json'}: {exc}") from exc
    with tempfile.TemporaryDirectory(prefix="finesub-align-") as scratch:
        def tree(given: Path | None, ref: str, name: str) -> Path:
            if given is not None:
                return given
            directory = Path(scratch, name)
            directory.mkdir()
            return fetch_tree(repository, ref, directory)

        base = tree(args.base_dir, commit, "base")
        target = tree(args.upstream_dir, args.ref, "target")
        report = build_report(base, target, vendor)
    return {
        "base_ref": upstream.get("ref"),
        "base_commit": commit,
        "target_ref": args.ref,
        **report,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Report-only comparison of FineSub's key algorithm paths.")
    commands = parser.add_subparsers(dest="command", required=True)
    diff = commands.add_parser("diff", help="compare an upstream release with the recorded one and the vendor")
    diff.add_argument("--ref", required=True, help="upstream tag (or commit) to compare against")
    diff.add_argument("--upstream-dir", type=Path, help="extracted upstream root at --ref instead of downloading it")
    diff.add_argument("--base-dir", type=Path, help="extracted upstream root at the recorded commit instead of downloading it")
    diff.add_argument("--vendor-dir", type=Path, default=DEFAULT_VENDOR, help=argparse.SUPPRESS)
    diff.add_argument("--json", action="store_true", help="print the report as JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = run_diff(args)
    except AlignError as exc:
        print(f"finesub-align: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        sys.stdout.write(render_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
