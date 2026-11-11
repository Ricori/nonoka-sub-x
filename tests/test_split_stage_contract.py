"""The seams Nonoka X's cloud calls in the FineSub engine.

The cloud runs one task across three containers and only the Whisper leg needs
the GPU. Upstream 0.5.0 split the VAD half out by itself -- `run_vad_prefix`,
`write_vad_prefix`/`read_vad_prefix`, `run_vad_asr(vad_prefix_path=...)`, and
the referee's `warm` seam -- so the CPU container calls upstream code with
upstream arguments. What is still ours is the other end:
`finalize_qwen_verification` (local change 0001 in
``third_party/finesub/LOCAL_CHANGES.md``) attaches the referee's evidence to an
aligned artifact the GPU container already wrote.

This file pins the seams the cloud depends on -- upstream's and ours alike,
because an upgrade can take either away. That the engine files themselves are
unchanged is the quality lock's job (``tests/test_finesub_quality_lock.py``).
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "third_party" / "finesub"
STAGE_PATH = VENDOR / "src/finesub/speech/recognition/vad_asr_stage.py"
REFEREE_PATH = VENDOR / "src/finesub/speech/verification/qwen_referee.py"


def _module(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _top_level_names(module: ast.Module) -> set[str]:
    names = {
        node.name
        for node in module.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    for node in module.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names.update(
                target.id for target in targets if isinstance(target, ast.Name)
            )
    return names


#: What local change 0001 adds. A cloud container imports it by name.
ADDED_NAMES = ("finalize_qwen_verification",)

#: Upstream's own split seams, which the cloud's CPU container calls instead of
#: the ones a local change used to add. Listed here because an upgrade can retire them
#: as easily as it introduced them, and the failure would otherwise be a
#: traceback on the first cloud task rather than a red test.
UPSTREAM_NAMES = (
    "VadPrefix",
    "run_vad_prefix",
    "write_vad_prefix",
    "read_vad_prefix",
    "default_vad_prefix_path",
)


class SplitSeamTests(unittest.TestCase):
    def test_the_stage_exposes_every_seam_the_cloud_calls(self) -> None:
        names = _top_level_names(_module(STAGE_PATH))
        for name in (*UPSTREAM_NAMES, *ADDED_NAMES):
            with self.subTest(name=name):
                self.assertTrue(
                    name in names,
                    f"{name} is missing; the cloud's CPU containers import it "
                    f"by name and would fail at the first task.",
                )

    def test_run_vad_asr_takes_a_stored_prefix_and_defaults_to_none(self) -> None:
        """Upstream's own opt-in, and the reason our half stays small.

        ``None`` is what keeps every local run, the CLI and batch on the
        in-process prefix; the cloud passes the path the CPU container wrote.
        """

        functions = {
            node.name: node
            for node in _module(STAGE_PATH).body
            if isinstance(node, ast.FunctionDef)
        }
        arguments = functions["run_vad_asr"].args
        keyword_defaults = dict(zip(arguments.kwonlyargs, arguments.kw_defaults))
        parameter = next(
            arg for arg in arguments.kwonlyargs if arg.arg == "vad_prefix_path"
        )
        self.assertIsInstance(keyword_defaults[parameter], ast.Constant)
        self.assertIsNone(keyword_defaults[parameter].value)

    def test_the_referee_exposes_the_warm_seam(self) -> None:
        """`warm` is why the scheduler can preload Qwen beside the GPU stage.

        Upstream's since 0.5.0, pinned here for the same reason as
        `UPSTREAM_NAMES`: the cloud calls it by name.
        """

        classes = {
            node.name: node
            for node in _module(REFEREE_PATH).body
            if isinstance(node, ast.ClassDef)
        }
        methods = {
            node.name
            for node in classes["QwenReferee"].body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.assertIn("warm", methods)


class RetiredMechanismTests(unittest.TestCase):
    def test_no_execution_profile_switch_survives_in_the_engine(self) -> None:
        """The retired mechanism, kept retired.

        `finesub.execution` gated cloud-only behaviour inside shared code. Every
        such gate is gone; the cloud now differs from local by which functions
        it calls, which is visible at the call site instead of buried in a
        context variable.
        """

        self.assertFalse((VENDOR / "src/finesub/execution.py").exists())
        self.assertFalse((VENDOR / "src/finesub/execution_policy.py").exists())
        offenders = [
            path.relative_to(VENDOR).as_posix()
            for path in (VENDOR / "src").rglob("*.py")
            if "cloud_execution_enabled" in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(offenders, [])


class AddedCodeTests(unittest.TestCase):
    def test_added_functions_are_documented_where_they_are_defined(self) -> None:
        """A seam we added with no docstring is one upstream cannot review."""

        module = _module(STAGE_PATH)
        public = {
            node.name: node
            for node in module.body
            if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
        }
        for name in ADDED_NAMES:
            if name not in public:
                continue
            with self.subTest(name=name):
                self.assertTrue(ast.get_docstring(public[name]))


if __name__ == "__main__":
    unittest.main()
