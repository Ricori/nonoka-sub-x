"""Where the second-model referee runs, and whether it says anything.

Both promises started as ``patches/finesub/0007-referee-live-vram-and-verify-progress.patch``
and both were paid for in silence:

* The referee used to be put on the card from the *tier's* VRAM budget alone.
  On a `standard` card with a game open -- 2.4 GiB actually free against the
  6.5 the tier assumes -- it was loaded beside the Whisper pool anyway, and the
  collision either dragged the decode out for minutes with no event or killed
  the worker with an access violation that CTranslate2 cannot report. The
  desktop's log stopped on ``group ASR (...)`` and nothing followed.
* The tail verification pass then ran for minutes without a single event, which
  looks exactly like the hang above.

Upstream 0.5.1 took both over, and **narrowed the first one on purpose** -- see
`RedecodePlacementTests` below, which pins the narrowing rather than the patch.
The patch is gone; the behaviour is what this file is for. A sync that rewrites
either mechanism keeps it green as long as a busy card still keeps the referee
off the pool and the pass still reports.
"""

from __future__ import annotations

import unittest
from unittest import mock

import numpy as np

from finesub.reporting import reporting_to
from finesub.speech.recognition import lang_redecode, vad_asr_stage
from finesub.speech.runtime import resources
from finesub.speech.verification import qwen_referee


class RecordingReporter:
    """Every call the engine can make on a reporter, kept for inspection."""

    def __init__(self) -> None:
        self.progress_calls: list[dict[str, object]] = []
        self.warnings: list[tuple[str, str]] = []
        self.debugs: list[str] = []

    def planned(self, stages) -> None:
        return

    def stage_started(self, stage, *, reused=False, detail="") -> None:
        return

    def progress(self, stage, *, completed, total=None, unit="", detail="") -> None:
        self.progress_calls.append(
            {
                "stage": stage,
                "completed": completed,
                "total": total,
                "unit": unit,
                "detail": detail,
            }
        )

    def summary(self, stage, metrics) -> None:
        return

    def warning(self, code, message, *, impact="", action="") -> None:
        self.warnings.append((code, message))

    def debug(self, message, fields=None) -> None:
        self.debugs.append(message)

    def completed(self, output, elapsed_sec) -> None:
        return

    def failed(self, stage, message) -> None:
        return


def _card(free_gib):
    """The two questions a placement asks the machine, both stubbed.

    `cuda_usable` too: the subject here is the VRAM arithmetic, and on a
    machine with no CUDA at all every case would answer "cpu" for a reason
    none of these tests are about.
    """

    return (
        mock.patch("finesub.speech.runtime.device.cuda_usable", return_value=True),
        mock.patch(
            "finesub.speech.runtime.device.free_vram_gib", return_value=free_gib
        ),
    )


def _place(free_gib, *, pool_resident, tier="standard", asr_device="cuda", **kwargs):
    """`referee_device` with question 5 switched on, which is the subject.

    Passing `live_vram_veto` is not decoration: it is *how* the question is
    asked at all. A call without it -- which is what the tail referee makes --
    stops at the tier's own arithmetic, so a test that omitted it would assert
    nothing about the live figure.
    """

    profile = resources.RESOURCE_PROFILES[tier]
    usable, free = _card(free_gib)
    with usable, free:
        return lang_redecode.referee_device(
            asr_device,
            profile,
            "large-v3-turbo",
            1,
            requested_device=kwargs.pop("requested_device", "cuda"),
            pool_resident=pool_resident,
            live_vram_veto=kwargs.pop("live_vram_veto", lambda free, needed: None),
            **kwargs,
        )


class RefereePlacementTests(unittest.TestCase):
    #: `standard` minus a resident large-v3-turbo pool. The tier says 4.43 GiB
    #: are spare whatever the card is really doing, which is the figure that
    #: used to decide this on its own.
    RESIDENT = 2.07

    def test_a_busy_card_keeps_the_referee_off_the_gpu(self) -> None:
        """The regression: 2.4 GiB free is not 4.43 GiB spare."""

        self.assertEqual(_place(2.40, pool_resident=True), "cpu")

    def test_a_free_card_still_gets_the_referee(self) -> None:
        """The feature has to survive its own fix.

        6.48 GiB free is the figure the desktop reported on the run that
        started this: at the moment of the check the card was fine, and the
        referee belongs on it.
        """

        self.assertEqual(_place(6.48, pool_resident=True), "cuda")

    def test_the_pool_is_bought_once(self) -> None:
        """`pool_resident` is the difference between two moments in the stage.

        Asked after `FwRefineModelPool.warm` the pool is already out of the
        live figure (`True`); asked before it is built, Whisper still has to
        come out of it (`False`). 4.0 GiB free covers the referee alone but not
        the referee plus a 2.07 GiB pool, so the two answers must differ here.
        """

        self.assertEqual(_place(4.00, pool_resident=True), "cuda")
        self.assertEqual(_place(4.00, pool_resident=False), "cpu")

    def test_an_unreadable_card_leaves_the_tier_in_charge(self) -> None:
        """No live figure is not evidence of a full card."""

        self.assertEqual(_place(None, pool_resident=True), "cuda")

    def test_a_caller_that_does_not_ask_gets_the_tier_answer(self) -> None:
        """Question 5 is opt-in, and the opt-in is the veto callback itself.

        The tail referee is the caller that does not ask: by then the pool is
        closed, so there is nothing of ours to collide with, and the figure it
        would move decides a decode path that is not bit-exact.
        """

        self.assertEqual(
            _place(0.20, pool_resident=True, live_vram_veto=None), "cuda"
        )

    def test_the_veto_says_what_it_measured(self) -> None:
        """The callback is how a caller words the cost of its own demotion.

        Upstream deliberately did not fix one message for every call site: what
        a veto *costs* differs between them, so `referee_device` reports the
        gap and the caller says what it means.
        """

        seen: list[tuple[float, float]] = []
        self.assertEqual(
            _place(0.66, pool_resident=True, live_vram_veto=lambda f, n: seen.append((f, n))),
            "cpu",
        )
        self.assertEqual(len(seen), 1)
        free, needed = seen[0]
        self.assertAlmostEqual(free, 0.66)
        self.assertGreater(needed, free)

    def test_policy_still_outranks_the_card(self) -> None:
        """An idle card does not overturn an answer that was never about VRAM.

        `--device cpu` and the CPU-ish tiers decide before the arithmetic runs.
        """

        self.assertEqual(_place(24.0, pool_resident=True, requested_device="cpu"), "cpu")
        self.assertEqual(_place(24.0, pool_resident=True, tier="entry"), "cpu")

    def test_an_asr_on_the_cpu_still_has_to_fit_on_the_card(self) -> None:
        """⚠ Behaviour change against the retired patch, and upstream is right.

        The patch returned "cuda" unconditionally here: with Whisper on the CPU
        there is no pool of *ours* to collide with. But the thing filling the
        card may not be ours -- a game, another process, another model -- and
        the referee still has to fit beside it. Question 5 therefore applies,
        with nothing to buy for Whisper.
        """

        self.assertEqual(_place(0.20, pool_resident=False, asr_device="cpu"), "cpu")
        self.assertEqual(_place(24.0, pool_resident=False, asr_device="cpu"), "cuda")


class WarmPlacementTests(unittest.TestCase):
    """The one call site that asks question 5, and what a veto there costs."""

    def _warm(self, free_gib, *, qwen_verify="auto", reporter=None):
        usable, free = _card(free_gib)
        with usable, free, reporting_to(reporter or RecordingReporter()):
            return vad_asr_stage.referee_warm_device(
                qwen_verify=qwen_verify,
                device="cuda",
                resource_profile=resources.RESOURCE_PROFILES["standard"],
                model_name="large-v3-turbo",
                decode_batch=1,
                requested_device="cuda",
            )

    def test_a_busy_card_skips_the_preload(self) -> None:
        self.assertIsNone(self._warm(2.40))

    def test_a_free_card_preloads_beside_the_decode(self) -> None:
        self.assertEqual(self._warm(6.48), "cuda")

    def test_the_skip_is_a_debug_and_not_a_warning(self) -> None:
        """What is lost is the ~3 s load, not the check.

        The tail referee is placed afterwards by `tail_verify_device`, which
        does not ask question 5 -- so it still goes on the card, and telling
        the user "the check will run on the CPU" would be false. It is also
        nothing they can act on mid-run, which is the line `docs/reporting.md`
        draws for warnings.
        """

        reporter = RecordingReporter()
        self.assertIsNone(self._warm(0.66, reporter=reporter))
        self.assertEqual(reporter.warnings, [])
        self.assertTrue(reporter.debugs)


class RedecodePlacementTests(unittest.TestCase):
    """⚠ The half of the retired patch upstream **refused**, pinned as refused.

    The language-vote referee decides whether a group's decode is *replaced*,
    so where it runs is a numeric path and not a speed knob: production dtype
    is `bfloat16` on CUDA and `float32` on CPU, and nothing in the engine shows
    those two agree. Letting the driver's live free figure choose between them
    would make the same audio produce different subtitles depending on what
    else happened to be open.

    The retired patch did exactly that. This test exists so a future sync
    cannot quietly put it back on the grounds that the warm site has it.
    """

    def test_a_busy_card_does_not_move_the_redecode_referee(self) -> None:
        usable, free = _card(0.20)
        with usable, free:
            placed = vad_asr_stage.redecode_referee_device(
                "cuda",
                resources.RESOURCE_PROFILES["standard"],
                "large-v3-turbo",
                1,
                requested_device="cuda",
            )
        self.assertEqual(placed, "cuda")


class _StubReader:
    """`_SpanReader` without a file: one second of silence for any span."""

    def __init__(self, path: str) -> None:
        self.path = path

    def read(self, start: float, end: float) -> np.ndarray:
        return np.zeros(qwen_referee.TARGET_SR, dtype=np.float32)


class _StubReferee:
    """Enough of `QwenReferee` for `apply_verification`, minus the model."""

    _model_name = "stub"
    requested_device = "cpu"

    def __init__(self) -> None:
        self.on_batch = None

    def transcribe_batch(self, clips, *, max_new_tokens=None, on_batch=None):
        self.on_batch = on_batch
        if on_batch is not None:
            on_batch(len(clips), len(clips))
        return [("", None)] * len(clips)


class VerificationProgressTests(unittest.TestCase):
    def test_the_pass_reports_before_and_after_the_model_runs(self) -> None:
        """The zero matters as much as the count.

        The load and the first `generate` are most of the wait, so a run that
        only reported after the first batch would still look hung for the part
        that actually looks hung.
        """

        reporter = RecordingReporter()
        referee = _StubReferee()
        with reporting_to(reporter), mock.patch.object(
            qwen_referee, "_SpanReader", _StubReader
        ):
            qwen_referee.apply_verification(
                [],
                vad_intervals=[{"start": 0.0, "end": 5.0}],
                audio_path="unused.wav",
                referee=referee,
            )

        self.assertIsNotNone(referee.on_batch, "the pass must pass its reporter in")
        counts = [(call["completed"], call["total"]) for call in reporter.progress_calls]
        self.assertEqual(counts, [(0, 1), (1, 1)])
        for call in reporter.progress_calls:
            self.assertEqual(call["unit"], "clips")
            # The tail of the ASR stage, not a stage of its own: a new stage
            # name here would read as the pipeline having moved on.
            self.assertEqual(call["stage"], "aligned")
            self.assertTrue(call["detail"])

    def test_nothing_to_verify_reports_nothing(self) -> None:
        """A 0/0 would be the one progress line that can never move."""

        reporter = RecordingReporter()
        with reporting_to(reporter):
            qwen_referee.apply_verification(
                [],
                vad_intervals=[],
                audio_path="unused.wav",
                referee=_StubReferee(),
            )
        self.assertEqual(reporter.progress_calls, [])


if __name__ == "__main__":
    unittest.main()
