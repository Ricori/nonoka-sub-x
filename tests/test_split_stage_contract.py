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

It also pins the split of the orchestration itself (plan WP-B, 4.1 item 2):
`run_transcription` up to the stable, `run_post_stable` after it with its
speaker source and judge track said explicitly, `run_pipeline` the two in one
run -- and the joint-ASR branch gone from `stages.py` and `pipeline.py`. The
end-to-end runs go through a child process with nothing of this machine's
environment (`quality_harness.calls.hermetic_environment`).
"""

from __future__ import annotations

import ast
import inspect
import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "third_party" / "finesub"
STAGE_PATH = VENDOR / "src/finesub/speech/recognition/vad_asr_stage.py"
REFEREE_PATH = VENDOR / "src/finesub/speech/verification/qwen_referee.py"
STAGES_PATH = VENDOR / "src/finesub/stages.py"
PIPELINE_PATH = VENDOR / "src/finesub/pipeline.py"


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


def _imported_modules(path: Path) -> set[str]:
    """Every module `path` imports, relative ones spelled with their dots (`.speech.joint`)."""

    found: set[str] = set()
    for node in ast.walk(_module(path)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = "." * node.level + (node.module or "")
            found.add(base)
            found.update(f"{base.rstrip('.')}.{alias.name}" if node.module else f"{base}{alias.name}"
                         for alias in node.names)
    return found


class JointBranchRemovedTests(unittest.TestCase):
    """The joint-ASR backend leaves FineSub (WP1); its branch in the orchestration went first (WP-B)."""

    def test_neither_front_file_imports_the_joint_backend(self) -> None:
        for path in (STAGES_PATH, PIPELINE_PATH):
            with self.subTest(path=path.name):
                offenders = sorted(name for name in _imported_modules(path) if "joint" in name.split("."))
                self.assertEqual(offenders, [])

    def test_run_pipeline_takes_no_backend_choice(self) -> None:
        from finesub.stages import run_pipeline

        parameters = inspect.signature(run_pipeline).parameters
        for name in ("asr", "asr_aligner", "asr_repair"):
            with self.subTest(name=name):
                self.assertNotIn(name, parameters)

    def test_the_command_line_has_no_backend_options(self) -> None:
        from finesub.pipeline import build_parser

        flags = {flag for action in build_parser()._actions for flag in action.option_strings}  # noqa: SLF001
        for flag in ("--asr", "--asr-aligner", "--asr-repair"):
            with self.subTest(flag=flag):
                self.assertNotIn(flag, flags)
        # The recognition options that only share the prefix stay.
        self.assertTrue({"--asr-context", "--asr-decode-batch", "--asr-stabilize-profile"} <= flags)

    def test_the_command_line_needs_only_the_batch_lock_beyond_the_dependency_surface(self) -> None:
        """`logs.prune`/`open_log` are copied in; the cross-process lock is handed back (WP-P1)."""

        bootstrap = sorted(name for name in _imported_modules(PIPELINE_PATH) if name.startswith("finesub_bootstrap"))
        self.assertEqual(
            bootstrap,
            ["finesub_bootstrap.locks", "finesub_bootstrap.locks.LockUnavailable",
             "finesub_bootstrap.locks.holding_lock"],
        )


class EntryPointTests(unittest.TestCase):
    """What each entry point refuses, before it reads any configuration or touches the disk."""

    def test_post_stable_asks_for_its_speaker_source_and_judge_track(self) -> None:
        from finesub.stages import run_post_stable

        parameters = inspect.signature(run_post_stable).parameters
        for name in ("speaker_source", "judge_audio"):
            with self.subTest(name=name):
                self.assertIs(parameters[name].kind, inspect.Parameter.KEYWORD_ONLY)
                self.assertIs(parameters[name].default, inspect.Parameter.empty)

    def test_each_half_stops_only_on_its_own_side_of_the_stable(self) -> None:
        from finesub import stages

        self.assertEqual(
            (*stages.TRANSCRIPTION_STAGES, *stages.POST_STABLE_STAGES), tuple(stages.PIPELINE_STAGE_ORDER)
        )
        for stage in stages.POST_STABLE_STAGES:
            with self.subTest(half="transcription", stage=stage), self.assertRaises(ValueError):
                stages.run_transcription("missing.wav", stage=stage)
        for stage in stages.TRANSCRIPTION_STAGES:
            with self.subTest(half="post-stable", stage=stage), self.assertRaises(ValueError):
                stages.run_post_stable("missing.wav", speaker_source="acoustic", judge_audio="vocal", stage=stage)

    def test_post_stable_refuses_unknown_sources_and_tracks(self) -> None:
        from finesub.stages import run_post_stable

        with self.assertRaises(ValueError):
            run_post_stable("missing.wav", speaker_source="joint", judge_audio="vocal")
        with self.assertRaises(ValueError):
            run_post_stable("missing.wav", speaker_source="acoustic", judge_audio="mix")

    def test_the_halves_take_run_pipelines_keywords_and_no_others(self) -> None:
        from finesub import stages

        with self.assertRaises(TypeError):
            stages.run_transcription("missing.wav", asr="mai2")
        with self.assertRaises(TypeError):
            stages.run_post_stable("missing.wav", speaker_source="acoustic", judge_audio="vocal", asr_repair="on")
        options = stages._pipeline_options(stage="stable", model_name="x")  # noqa: SLF001
        defaults = {
            name: parameter.default
            for name, parameter in inspect.signature(stages.run_pipeline).parameters.items()
            if parameter.kind is parameter.KEYWORD_ONLY
        }
        self.assertEqual(options, {**defaults, "stage": "stable", "model_name": "x"})


#: A stable whose lines carry nothing but the transcription's own speaker labels: no `-diar.json`, no
#: acoustic labels, no record of which backend made it.
_LABELLED_LINES = [
    {"id": "1", "start": 1.0, "end": 2.0, "text": "おはよう", "line_speaker": "A",
     "words": [{"word": "おはよう", "start": 1.0, "end": 2.0}]},
    {"id": "2", "start": 2.2, "end": 3.0, "text": "ございます", "line_speaker": "B",
     "words": [{"word": "ございます", "start": 2.2, "end": 3.0}]},
    {"id": "3", "start": 4.0, "end": 5.0, "text": "またね", "line_speaker": "A",
     "words": [{"word": "またね", "start": 4.0, "end": 5.0}]},
    {"id": "4", "start": 5.5, "end": 6.0, "text": "うん",
     "words": [{"word": "うん", "start": 5.5, "end": 6.0}]},
]
_PLAIN_LINES = [{key: value for key, value in line.items() if key != "line_speaker"} for line in _LABELLED_LINES]

#: Run in a child with a clean environment: the modules it holds at the end are the run's own.
_CHILD = textwrap.dedent(
    """
    import json
    import sys
    from pathlib import Path

    from finesub import stages
    from finesub.reporting import NullReporter, reporting_to

    root = Path(sys.argv[1])
    lines = json.loads(sys.argv[2])


    class Recorder(NullReporter):
        def __init__(self):
            self.planned_stages, self.started = [], []

        def planned(self, stages):
            self.planned_stages.append(list(stages))

        def stage_started(self, stage, *, reused=False, detail=""):
            self.started.append([stage, reused])


    def task(name, segments):
        directory = root / name
        directory.mkdir()
        source = directory / f"{name}.wav"
        source.write_bytes(b"")
        output = directory / "out" / f"{name}.srt"
        if segments is not None:
            output.parent.mkdir()
            output.with_name(f"{name}-stable.json").write_text(
                json.dumps({"segments": segments}, ensure_ascii=False), encoding="utf-8")
        return source, output


    def products(paths):
        return {path.name.replace(paths.final_srt.stem, "<task>"): path.read_text(encoding="utf-8")
                for path in sorted(paths.final_srt.parent.iterdir())
                if path.is_file() and path != paths.metadata_json}


    result = {}
    source, output = task("labels", lines["labelled"])
    recorder = Recorder()
    with reporting_to(recorder):
        paths = stages.run_post_stable(source, output_path=output, speaker_source="line-labels",
                                       judge_audio="source", stage="raw-srt")
    result["labels"] = {
        "products": products(paths),
        "diar_json": paths.diar_json.exists(),
        "planned": recorder.planned_stages,
        "started": recorder.started,
        "modules": sorted(name for name in ("torch", "numpy") if name in sys.modules),
    }

    source, output = task("missing", None)
    try:
        stages.run_post_stable(source, output_path=output, speaker_source="acoustic", judge_audio="vocal")
        result["missing"] = "ran"
    except FileNotFoundError:
        result["missing"] = "FileNotFoundError"
    result["missing_left"] = output.parent.exists()

    def whole(source, output):
        return stages.run_pipeline(source, output_path=output, stage="raw-srt")

    def split(source, output):
        stages.run_transcription(source, output_path=output)
        return stages.run_post_stable(source, output_path=output, speaker_source="acoustic",
                                      judge_audio="vocal", stage="raw-srt")

    for name, run in (("whole", whole), ("split", split)):
        source, output = task(name, lines["plain"])
        with reporting_to(Recorder()):
            result[name] = products(run(source, output))
    result["modules"] = sorted(name for name in ("torch", "numpy") if name in sys.modules)
    print(json.dumps(result, ensure_ascii=False))
    """
)


class SplitRunTests(unittest.TestCase):
    """The halves run end to end, from a stable on disk, with no GPU, model or network."""

    @classmethod
    def setUpClass(cls) -> None:
        from tests.quality_harness.hygiene import isolated_environment

        cls._scratch = tempfile.TemporaryDirectory()
        root = Path(cls._scratch.name)
        environment, _removed = isolated_environment(root)
        environment.update({
            "PYTHONPATH": str(VENDOR / "src"),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "TEMP": str(root / "tmp"),
            "TMP": str(root / "tmp"),
        })
        (root / "tmp").mkdir()
        (root / "runs").mkdir()
        completed = subprocess.run(
            [sys.executable, "-c", _CHILD, str(root / "runs"),
             json.dumps({"labelled": _LABELLED_LINES, "plain": _PLAIN_LINES}, ensure_ascii=False)],
            cwd=root, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=300,
        )
        if completed.returncode != 0:
            raise AssertionError(f"the child run failed:\n{completed.stdout}\n{completed.stderr}")
        cls.result = json.loads(completed.stdout.strip().splitlines()[-1])

    @classmethod
    def tearDownClass(cls) -> None:
        cls._scratch.cleanup()

    def test_line_labels_reach_raw_srt_without_torch_or_numpy(self) -> None:
        labels = self.result["labels"]
        self.assertEqual(labels["modules"], [])
        self.assertIn("<task>-raw.srt", labels["products"])
        self.assertIn("おはよう", labels["products"]["<task>-raw.srt"])

    def test_line_labels_are_written_as_the_rows_record(self) -> None:
        labels = self.result["labels"]
        self.assertFalse(labels["diar_json"], "the acoustic timeline is neither read nor written")
        rows = json.loads(labels["products"]["<task>-diar-rows.json"])
        text = json.dumps(rows, ensure_ascii=False)
        # Tracks in order of first appearance, the transcription's own names kept beside them, and the
        # name used for a stable that records no backend.
        self.assertIn('"joint_speaker": "A"', text)
        self.assertIn('"joint_speaker": "B"', text)
        self.assertIn('"joint:line-labels"', text)

    def test_line_labels_are_planned_and_announced_before_the_raw_srt(self) -> None:
        labels = self.result["labels"]
        self.assertEqual(labels["planned"], [["diarize", "raw-srt"]])
        self.assertEqual(labels["started"], [["diarize", False], ["raw-srt", False]])

    def test_post_stable_without_a_stable_stops_before_writing_anything(self) -> None:
        self.assertEqual(self.result["missing"], "FileNotFoundError")
        self.assertFalse(self.result["missing_left"])

    def test_the_halves_one_after_the_other_make_what_one_run_makes(self) -> None:
        self.assertEqual(self.result["split"], self.result["whole"])
        self.assertIn("<task>-raw.srt", self.result["whole"])
        self.assertEqual(self.result["modules"], [])


if __name__ == "__main__":
    unittest.main()
