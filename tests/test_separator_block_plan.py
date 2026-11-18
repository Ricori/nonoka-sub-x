"""Contract for Nonoka X's pinned copy of FineSub's separator block planner.

Cloud vocal separation cuts a track into blocks with `nonoka_x.vocal_blocks`,
which is a verbatim copy of arithmetic that lives in the vendored engine (see
that module for why it is a copy and not an import or a vendor patch). A copy
is only safe while something notices upstream editing the original, so that is
what most of this file does. The rest pins the numbers the cloud worker was
sized around, which no caller of the planner states for itself.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VENDOR_SOURCE = ROOT / "engines" / "finesub-transcribe" / "src"
UPSTREAM_STAGE = VENDOR_SOURCE / "finesub/speech/preprocessing/separator/separation.py"
NONOKA_X_COPY = ROOT / "src" / "nonoka_x" / "vocal_blocks.py"

sys.path.insert(0, str(ROOT / "src"))

from nonoka_x.vocal_blocks import (  # noqa: E402
    DEFAULT_BLOCK_SECONDS,
    DEFAULT_PAD_SECONDS,
    SeparationBlock,
    plan_separation_blocks,
    separator_worker_limit,
)


#: What the copy claims to have copied.
PINNED = (
    "_SeparationBlock",
    "WORKER_DURATION_THRESHOLD_SEC",
    "separator_worker_limit",
    "plan_separation_blocks",
)

#: Nonoka X's own names, which live beside the copy rather than inside it.
LOCAL_NAMES = {"DEFAULT_BLOCK_SECONDS", "DEFAULT_PAD_SECONDS", "SeparationBlock"}

SAMPLE_RATE = 44_100


def _definitions(path: Path) -> dict[str, str]:
    """Every top-level definition in a module, as the text that defines it."""

    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    found: dict[str, str] = {}
    for node in ast.parse(text).body:
        if isinstance(node, ast.Assign):
            target = node.targets[0]
            name = target.id if isinstance(target, ast.Name) else None
        else:
            name = getattr(node, "name", None)
        if name is None:
            continue
        start = min(
            [node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])]
        )
        # The comment block above a definition is part of what it says.
        while start > 1 and lines[start - 2].lstrip().startswith("#"):
            start -= 1
        found[name] = "".join(lines[start - 1 : node.end_lineno])
    return found


def _plan(duration_sec: float, *, workers: int) -> list[SeparationBlock]:
    return plan_separation_blocks(
        int(duration_sec * SAMPLE_RATE),
        SAMPLE_RATE,
        workers=workers,
        max_core_seconds=DEFAULT_BLOCK_SECONDS,
        pad_samples=int(round(DEFAULT_PAD_SECONDS * SAMPLE_RATE)),
    )


class PinnedUpstreamCopyTests(unittest.TestCase):
    """The copy must be the original, character for character."""

    def test_every_pinned_definition_matches_the_vendored_engine(self) -> None:
        upstream = _definitions(UPSTREAM_STAGE)
        copied = _definitions(NONOKA_X_COPY)
        for name in PINNED:
            with self.subTest(definition=name):
                self.assertIn(
                    name,
                    upstream,
                    f"{name} is gone from the vendored separator, so the copy "
                    f"in {NONOKA_X_COPY.name} has no original left to track.",
                )
                self.assertEqual(
                    copied.get(name),
                    upstream[name],
                    f"{name} drifted from the vendored separator. Cloud and "
                    f"local would plan different blocks, which means different "
                    f"audio. Re-copy it into {NONOKA_X_COPY.name} verbatim.",
                )

    def test_the_copy_adds_nothing_to_what_it_pins(self) -> None:
        """Nonoka X's own names may sit beside the copy, but not inside it.

        A helper quietly added between two copied functions would be invisible
        to the comparison above and would not exist upstream at all.
        """

        copied = set(_definitions(NONOKA_X_COPY))
        self.assertEqual(sorted(copied - LOCAL_NAMES), sorted(PINNED))

    def test_defaults_match_the_signature_they_were_taken_from(self) -> None:
        """Upstream keeps 600/10 as literal defaults; the copy names them."""

        stage = ast.parse(UPSTREAM_STAGE.read_text(encoding="utf-8"))
        signature = next(
            node
            for node in ast.walk(stage)
            if isinstance(node, ast.FunctionDef)
            and node.name == "run_vocal_separation"
        ).args
        # Only these two are read: the rest of the signature defaults to names
        # and calls that no `literal_eval` can be asked to make sense of.
        defaults = {
            arg.arg: value
            for arg, value in zip(signature.kwonlyargs, signature.kw_defaults)
        }
        self.assertEqual(
            ast.literal_eval(defaults["block_seconds"]), DEFAULT_BLOCK_SECONDS
        )
        self.assertEqual(
            ast.literal_eval(defaults["pad_seconds"]), DEFAULT_PAD_SECONDS
        )

    def test_the_copy_imports_nothing_the_cloud_image_lacks(self) -> None:
        """The Vocal image carries audio-separator but no ASR stack.

        A stray import would not break any test that runs on a full local
        install -- it would break the deployed worker, so it is checked in a
        clean interpreter instead.
        """

        watched = "'torch', 'torchaudio', 'numpy', 'soundfile', 'finesub'"
        probe = (
            "import sys;"
            f"sys.path.insert(0, {str(ROOT / 'src')!r});"
            "import nonoka_x.vocal_blocks;"
            f"print(','.join(sorted({{{watched}}} & set(sys.modules))))"
        )
        completed = subprocess.run(
            [sys.executable, "-S", "-c", probe],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "")


class BlockPlanTests(unittest.TestCase):
    """The numbers the cloud Vocal worker was sized around."""

    def test_defaults_are_a_ten_minute_core_and_a_ten_second_pad(self) -> None:
        self.assertEqual(DEFAULT_BLOCK_SECONDS, 600.0)
        self.assertEqual(DEFAULT_PAD_SECONDS, 10.0)

    def test_single_worker_splits_two_hours_into_twelve_ten_minute_cores(self) -> None:
        blocks = _plan(2 * 60 * 60, workers=1)
        self.assertEqual(len(blocks), 12)
        self.assertEqual(
            {block.block_start for block in blocks},
            {index * 600 * SAMPLE_RATE for index in range(12)},
        )

    def test_a_core_never_exceeds_the_limit_and_the_cores_tile_the_track(self) -> None:
        total_frames = int(41 * 60 * SAMPLE_RATE)
        blocks = plan_separation_blocks(
            total_frames,
            SAMPLE_RATE,
            workers=1,
            max_core_seconds=DEFAULT_BLOCK_SECONDS,
            pad_samples=int(round(DEFAULT_PAD_SECONDS * SAMPLE_RATE)),
        )
        starts = [block.block_start for block in blocks] + [total_frames]
        cores = [starts[index + 1] - starts[index] for index in range(len(blocks))]
        self.assertEqual(starts[0], 0)
        self.assertEqual(sum(cores), total_frames)
        self.assertLessEqual(max(cores), DEFAULT_BLOCK_SECONDS * SAMPLE_RATE)
        # Equal blocks, not a fixed core with a short remainder.
        self.assertLessEqual(max(cores) - min(cores), 1)

    def test_short_input_stays_one_block(self) -> None:
        """What keeps the cloud worker's whole-file fast path in play."""

        self.assertEqual(len(_plan(9 * 60, workers=1)), 1)
        self.assertEqual(len(_plan(11 * 60, workers=1)), 2)

    def test_pad_is_read_around_every_core_but_not_off_the_track(self) -> None:
        total_frames = int(30 * 60 * SAMPLE_RATE)
        blocks = plan_separation_blocks(
            total_frames,
            SAMPLE_RATE,
            workers=1,
            max_core_seconds=DEFAULT_BLOCK_SECONDS,
            pad_samples=int(round(DEFAULT_PAD_SECONDS * SAMPLE_RATE)),
        )
        pad = int(round(DEFAULT_PAD_SECONDS * SAMPLE_RATE))
        self.assertEqual(blocks[0].read_start, 0)
        self.assertEqual(blocks[-1].read_end, total_frames)
        for block in blocks[1:]:
            self.assertEqual(block.read_start, block.block_start - pad)

    def test_block_count_follows_the_worker_count(self) -> None:
        """The documented cost of equal blocks: edges move with the worker
        count, so cloud has to plan for the workers it actually runs."""

        self.assertEqual(len(_plan(2 * 60 * 60, workers=4)), 12)
        self.assertEqual(len(_plan(2 * 60 * 60, workers=5)), 15)
        self.assertNotEqual(
            [block.block_start for block in _plan(30 * 60, workers=1)],
            [block.block_start for block in _plan(30 * 60, workers=4)],
        )

    def test_worker_ladder_opens_one_worker_per_five_minutes(self) -> None:
        self.assertEqual(separator_worker_limit(0.0), 1)
        self.assertEqual(separator_worker_limit(299.0), 1)
        self.assertEqual(separator_worker_limit(300.0), 2)
        self.assertEqual(separator_worker_limit(1500.0), 6)


if __name__ == "__main__":
    unittest.main()
