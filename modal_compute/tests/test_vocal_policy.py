from __future__ import annotations

import ast
from pathlib import Path

import pytest

from nonoka_x_compute.vocal_policy import (
    DEFAULT_TIER,
    DELIVERY_SAMPLE_RATE,
    NATIVE_SAMPLE_RATE,
    REQUEST_FIELD,
    TIER_ENV_VAR,
    VOCAL_TIER_LADDER,
    VOCAL_TIERS,
    tier_for_duration,
    tier_for_request,
)


ROOT = Path(__file__).resolve().parents[2]
VENDOR_SOURCE = ROOT / "third_party" / "finesub" / "src"
SEPARATION_SOURCE = (
    VENDOR_SOURCE / "finesub/speech/preprocessing/separator/separation.py"
)
VOCAL_CONTAINER = ROOT / "modal_compute" / "nonoka_x_compute" / "vocal.py"


def _upstream_literal(name: str):
    """One module-level constant from the vendored separator, without importing it."""

    tree = ast.parse(SEPARATION_SOURCE.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name
            for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} is gone from {SEPARATION_SOURCE.name}")


def test_tiers_are_the_three_the_benchmark_kept() -> None:
    assert {
        (tier.sample_rate, tier.channels) for tier in VOCAL_TIERS.values()
    } == {(44_100, 2), (32_000, 2), (22_050, 2)}
    # 16 kHz mono was the policy that scored 36.42% CER; nothing may select it.
    assert all(
        tier.sample_rate > DELIVERY_SAMPLE_RATE and tier.channels == 2
        for tier in VOCAL_TIERS.values()
    )


def test_the_ladder_stays_inside_the_rates_upstream_supports() -> None:
    """The three rungs must be rates the vendored separator accepts.

    FineSub 0.4.2 gave the separator a working-rate switch whose allowed set is
    exactly this ladder's three rates.  That makes the ladder a *policy over
    upstream's own vocabulary* rather than a private one, and this is what says
    so: an upstream release that drops a rate, adds one, or moves the default
    lands here instead of in a separation that quietly sounds different.

    Read out of the source text, not imported: ``separation`` pulls in torch and
    torchaudio at module scope, which is exactly why `nonoka_x.vocal_blocks` is a
    copy too.  Same reason, same technique as `tests/test_separator_block_plan`.
    """

    rates = _upstream_literal("SEPARATOR_SAMPLE_RATES")
    default = _upstream_literal("DEFAULT_SEPARATOR_SAMPLE_RATE")
    assert {tier.sample_rate for tier in VOCAL_TIERS.values()} <= set(rates), (
        "the cloud ladder offers a rate the vendored separator would reject"
    )
    assert NATIVE_SAMPLE_RATE == default
    # Upstream's default is the top rung, so an unpinned upstream call and this
    # ladder's most faithful tier separate identically.
    assert VOCAL_TIERS["44.1k-stereo"].sample_rate == NATIVE_SAMPLE_RATE


def test_the_worker_pins_the_rate_the_way_upstream_does() -> None:
    """The GPU worker must set the same attributes `_acquire_separator` sets.

    The Vocal container drives ``Separator`` itself, so it cannot take
    upstream's ``separator_sample_rate`` keyword; it applies the rate by hand
    instead.  By hand is only safe while it stays the same pair of assignments,
    which is what this reads out of both files.
    """

    upstream = SEPARATION_SOURCE.read_text(encoding="utf-8")
    worker = VOCAL_CONTAINER.read_text(encoding="utf-8")
    for attribute in ("separator.sample_rate", "separator.model_instance.sample_rate"):
        assert f"lease.{attribute} = sample_rate" in upstream, (
            f"upstream no longer pins {attribute}; re-read _acquire_separator"
        )
        assert f"self.{attribute} = int(sample_rate)" in worker


def test_one_decoded_source_serves_every_tier() -> None:
    """The decode is tier-independent, so a moved ladder does not redo it.

    The name used to be keyed on the tier's rate and channel count, because
    the decode applied both and a retry after the deployed ladder moved would
    otherwise have reused a file at the wrong rate. The decode now applies
    neither -- it changes the container and nothing else, the way the
    desktop's `transcode_to_lossless_audio` does -- so there is one artifact,
    it is reusable across a ladder change, and nothing about the tier belongs
    in its name.
    """

    from nonoka_x_compute.vocal_policy import PREPARED_SOURCE_NAME

    assert PREPARED_SOURCE_NAME == "source-vocal-decoded.flac"
    assert not hasattr(VOCAL_TIERS["32k-stereo"], "prepared_name")


@pytest.mark.parametrize(
    ("audio_sec", "expected"),
    [
        (1.0, "44.1k-stereo"),
        (11 * 60, "44.1k-stereo"),
        (12 * 60 - 1, "44.1k-stereo"),
        (12 * 60, "32k-stereo"),
        (39 * 60, "32k-stereo"),
        (40 * 60 - 1, "32k-stereo"),
        (40 * 60, "22.05k-stereo"),
        (2 * 60 * 60, "22.05k-stereo"),
    ],
)
def test_duration_picks_the_tier(audio_sec: float, expected: str) -> None:
    tier, reason = tier_for_duration(audio_sec)
    assert tier is VOCAL_TIERS[expected]
    assert reason == f"{audio_sec:.0f}s"


def test_the_ladder_covers_every_length_and_names_only_real_tiers() -> None:
    ceilings = [ceiling for ceiling, _ in VOCAL_TIER_LADDER]
    assert ceilings == sorted(ceilings)
    assert ceilings[-1] == float("inf"), "the last rung must catch everything"
    assert all(name in VOCAL_TIERS for _, name in VOCAL_TIER_LADDER)


def test_an_unusable_duration_degrades_to_the_balanced_tier() -> None:
    """A task is accepted and billed before a GPU sees it; never raise here."""

    for value in (None, 0, -1, "not a number"):
        tier, reason = tier_for_duration(value)  # type: ignore[arg-type]
        assert tier is VOCAL_TIERS[DEFAULT_TIER]
        assert reason == "duration unknown"


def test_environment_pins_one_tier_but_only_when_valid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TIER_ENV_VAR, "22.05k-stereo")
    tier, reason = tier_for_duration(60.0)
    assert tier is VOCAL_TIERS["22.05k-stereo"], "the pin outranks the ladder"
    assert TIER_ENV_VAR in reason
    # A typo in a deploy variable falls back to the ladder rather than taking
    # the cloud pipeline down.
    monkeypatch.setenv(TIER_ENV_VAR, "44.1")
    assert tier_for_duration(60.0)[0] is VOCAL_TIERS["44.1k-stereo"]
    monkeypatch.delenv(TIER_ENV_VAR)
    assert tier_for_duration(60.0)[0] is VOCAL_TIERS["44.1k-stereo"]


def test_request_pin_survives_a_moved_ladder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three containers read this; none of them may re-derive it."""

    monkeypatch.setenv(TIER_ENV_VAR, "44.1k-stereo")
    assert tier_for_request({REQUEST_FIELD: "22.05k-stereo"}) is VOCAL_TIERS[
        "22.05k-stereo"
    ]
    # An old task recorded before the field existed still has to run.
    assert tier_for_request({}) is VOCAL_TIERS[DEFAULT_TIER]
