"""Claims about the compute code that are cheapest checked against its text.

Ported from the still-meaningful assertions of
`modal_backend/tests/test_modal_task_runtime.py`: the compute behaviour is meant
to be exactly the old app's, and most of it only runs on a GPU.
"""

from __future__ import annotations

import ast
import warnings
from pathlib import Path


PACKAGE = Path(__file__).resolve().parents[1] / "nonoka_x_compute"


def _source(name: str) -> str:
    return (PACKAGE / name).read_text(encoding="utf-8")


RESOURCES = _source("resources.py")
IMAGES = _source("images.py")
VOCAL = _source("vocal.py")
GPU = _source("gpu.py")
PHASES = _source("phases.py")
SCHEDULER = _source("scheduler.py")
CONTROL = _source("control.py")
PRIME = _source("operations/prime.py")
ENTRYPOINT = _source("app.py")
COMPUTE = "\n".join(
    _source(path.relative_to(PACKAGE).as_posix())
    for path in PACKAGE.rglob("*.py")
    if "migrate" not in path.parts
)


def _registered(app) -> set[str]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return set(app.registered_functions)


def test_the_app_registers_compute_only() -> None:
    from nonoka_x_compute.app import app
    from nonoka_x_compute.resources import APP_NAME

    assert APP_NAME == "nonoka-x-compute"
    assert app.name == APP_NAME
    assert _registered(app) == {
        "control",
        "orchestrate_task",
        "run_aligned_stage",
        "sweep_scratch",
        "VocalSeparator.*",
    }


def test_modal_holds_no_user_data_and_no_bucket_credential() -> None:
    for absent in (
        "modal.Dict",
        'from_name("r2")',
        "r2_secret",
        "boto3.client",
        "append_task_event",
        "task_controls",
        'from_name("nonoka-x-artifacts"',
        "artifacts_volume",
        "jobs[",
        "uploads[",
    ):
        assert absent not in COMPUTE, absent
    assert '"nonoka-x-scratch"' in RESOURCES
    assert 'modal.Volume.from_name("nonoka-x-models"' in RESOURCES
    assert 'modal.Secret.from_name("nonoka-compute-control")' in RESOURCES
    assert 'modal.Secret.from_name("huggingface")' in RESOURCES


def test_every_leg_that_reports_carries_the_callback_secret() -> None:
    for source in (VOCAL, GPU, SCHEDULER, CONTROL):
        assert "control_secret" in source


def test_no_log_event_is_sent_to_the_worker() -> None:
    from nonoka_x_compute import channel

    tree = ast.parse(_source("channel.py"))
    published = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and node.value in {"stage", "progress", "warning", "handoff", "log", "failed"}
    }
    assert "log" not in published and "failed" not in published
    assert channel.PROGRESS_MIN_INTERVAL_SEC == 1.0
    assert 'if event_type == "log":' in PHASES
    assert 'if event_type != "log":' in VOCAL


def test_user_event_stream_excludes_cloud_operator_diagnostics() -> None:
    assert "if code.startswith(self.OPERATOR_ONLY_WARNINGS):" in PHASES
    assert 'OPERATOR_ONLY_WARNINGS = ("srt-line-budget", "unverified-accelerator", "resource-budget")' in PHASES


def test_a_resumed_task_never_buys_a_stage_it_already_has() -> None:
    assert 'vocal_output = task_dir(job_id) / "subtitle-vocal.ogg"' in SCHEDULER
    assert "if not vocal_output.is_file():" in SCHEDULER
    assert 'aligned_output = task_dir(job_id) / "subtitle-aligned.json"' in SCHEDULER
    assert "if not aligned_output.is_file():" in SCHEDULER
    assert "if delivery.is_file():" in VOCAL
    assert "read_vad_prefix(" in PHASES
    assert "if paths.aligned_json.is_file():" in PHASES
    assert "if vocal_source.is_file():" in PHASES
    assert "if source.is_file():" in PHASES


def test_a_failed_cloud_task_is_never_re_run_without_the_user_asking() -> None:
    for source in (GPU, SCHEDULER, VOCAL, CONTROL, _source("sweep.py")):
        assert "retries=" not in source
    for source in (PHASES, VOCAL, SCHEDULER):
        assert "isinstance(exc, (KeyboardInterrupt, SystemExit))" in source


def test_failure_codes_are_the_contracts() -> None:
    assert '"source_rejected" if isinstance(exc, SourceRejected) else "engine_failed"' in PHASES
    assert 'channel.fail("vocal_failed"' in VOCAL
    assert 'channel.fail("pipeline_failed"' in SCHEDULER
    assert '"dispatch_incomplete"' in _source("channel.py")


def test_the_postprocess_leg_cleans_up_after_a_successful_hand_back() -> None:
    complete = PHASES.index("channel.complete(artifacts)")
    cleanup = PHASES.index("shutil.rmtree(work, ignore_errors=True)")
    assert complete < cleanup
    # And only there: a cancelled or failed run keeps its breakpoints.
    assert PHASES.count("shutil.rmtree(") == 1


def test_qwen_preload_is_joined_with_a_bound() -> None:
    assert "QWEN_PRELOAD_JOIN_SEC = 120.0" in SCHEDULER
    assert "qwen_thread.join(timeout=QWEN_PRELOAD_JOIN_SEC)" in SCHEDULER
    assert 'QwenReferee(device="cpu")' in SCHEDULER


def test_cpu_postprocess_does_not_rebroadcast_completed_prerequisite_stages() -> None:
    assert 'if self._postprocess_phase and reused and stage in {"vocal", "aligned"}:' in PHASES


def test_the_cloud_stops_at_stable() -> None:
    assert '{"gpu": "aligned", "cpu": "stable"}' in PHASES
    for absent in ('"final_srt": Path(', '"raw_srt": Path(', '"annotated_csv"', "execution_profile", "gpu_target"):
        assert absent not in PHASES, absent


def test_cloud_defers_silero_vad_to_gpu_when_assisted() -> None:
    assert "if not silero_assist:" in PHASES
    assert "vad_silero_assist=finesub_settings.VAD_SILERO_ASSIST.resolve(" in PHASES
    assert 'device="cuda"' in PHASES


def test_the_prepare_decode_changes_the_container_and_nothing_else() -> None:
    assert '"-c:a",\n                            "flac",' in PHASES
    assert 'str(vocal_tier.channels)' in PHASES
    for absent in ('"pcm_s16le"', '"pcm_s24le"', '"-sample_fmt"', '"-ar",'):
        assert absent not in PHASES, absent
    assert "probe_duration(source)" in PHASES
    assert "source_duration_fault(" in PHASES


def test_every_stage_reads_one_pinned_vocal_tier() -> None:
    assert "tier_for_request(request)" in VOCAL
    assert "tier_for_request(request)" in PHASES
    assert "tier_for_duration(" not in VOCAL + PHASES


def test_cloud_vocal_blocks_and_separates_the_way_finesub_does() -> None:
    for expected in (
        "from nonoka_x.vocal_blocks import",
        "plan_separation_blocks(",
        "max_core_seconds=DEFAULT_BLOCK_SECONDS",
        "pad_samples=int(round(DEFAULT_PAD_SECONDS * source_sr))",
        "SEPARATOR_WORKERS = 1",
        "workers=SEPARATOR_WORKERS",
        "_append_separated_block(",
        "from finesub.speech.preprocessing.separator import demix",
        "demix.separate_waveform(",
        "load_audio_slice(",
        "use_autocast=self.separator.use_autocast,",
        "_encode_asr_delivery(merged, temporary)",
        "self._pin_sample_rate(tier.sample_rate)",
        "sf.info(str(merged))",
    ):
        assert expected in VOCAL, expected
    for absent in ("self.separator.separate(", '"-c:a"', "sf.read(str(merged)", "trim_left"):
        assert absent not in VOCAL, absent


def test_aoti_is_installed_once_per_container_against_a_real_input() -> None:
    assert "install_accel(" in VOCAL
    assert "self.accel: dict = {" in VOCAL
    assert "_install_accel(float(source_info.duration))" in VOCAL
    assert 'if self.accel.get("reason") != "not attempted":' in VOCAL
    assert VOCAL.index("def _install_accel") > VOCAL.index("def load(")
    accel = _source("vocal_accel.py")
    assert 'attention_backend="auto"' in accel
    assert 'paths.root / cpu_tag() / "aoti"' in accel


def test_flash_attention_stays_in_the_ct2_build() -> None:
    assert " -DWITH_FLASH_ATTN=ON" in IMAGES
    assert "ctranslate2.flash_attention_available()" in IMAGES
    patches = sorted(path.name for path in (PACKAGE.parent / "ct2_patches").glob("*.patch"))
    assert len(patches) == 13
    assert patches[11].startswith("0012-feat-whisper-run-the-encoder-self-attention-with-fla")


def test_images_mount_the_engine_and_nonoka_x_relative_to_modal_compute() -> None:
    assert '"PYTHONPATH": "/opt/finesub/src:/opt/nonoka_x"' in IMAGES
    assert IMAGES.count('.add_local_dir("../third_party/finesub", "/opt/finesub")') == 3
    assert IMAGES.count('.add_local_dir("../src", "/opt/nonoka_x")') == 3
    assert '.add_local_dir("ct2_patches", "/opt/ct2-patches", copy=True)' in IMAGES
    assert "api_image" not in IMAGES
    assert "control_image = (" in IMAGES


def test_the_images_are_the_old_ones_step_for_step() -> None:
    """Identical steps reuse the old app's cached layers -- the Vocal one cannot be rebuilt."""

    old = Path(__file__).resolve().parents[2] / "modal_backend" / "nonoka_x_modal" / "images.py"
    if not old.is_file():
        return
    legacy = old.read_text(encoding="utf-8")
    marker = "# Cloud Vocal deliberately mirrors"
    assert IMAGES[IMAGES.index(marker):] == legacy[legacy.index(marker):]


def test_both_gpu_legs_and_priming_rent_the_same_card() -> None:
    from nonoka_x_compute.resources import WORKER_GPU

    assert WORKER_GPU == "L4"
    for source in (GPU, VOCAL, PRIME):
        assert "gpu=WORKER_GPU" in source
        assert 'gpu="' not in source


def test_model_priming_uses_an_ephemeral_app() -> None:
    from nonoka_x_compute.operations.prime import app

    assert app.name is None
    assert _registered(app) == {"prime_finesub_gpu", "prime_fleet_separator", "prime_diarization_models"}
    assert "prime_" not in ENTRYPOINT
    assert 'if outcome.get("backend") != "aoti"' in PRIME
    assert "force=True" in PRIME


def test_the_legacy_export_is_its_own_app() -> None:
    from nonoka_x_compute.migrate import export_legacy

    assert export_legacy.app.name == "nonoka-x-legacy-export"
    assert _registered(export_legacy.app) == {"export"}
    assert "export_legacy" not in ENTRYPOINT
