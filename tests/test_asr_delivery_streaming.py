"""The ASR delivery reads its source once, front to back (vendor patch 0012).

On 2026-10-30 a task with separation off sat in the vocal stage for hours on
a 71-minute 48 kHz mp3. `stream_asr_frames` reopened and seeked the source
for every window, libsndfile seeks an mp3 by decoding from the start, and the
window -- a fixed 2000 ratio steps -- was 0.125s at 48 kHz. These pin the
read pattern and the window size, and that neither changed a sample.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
import torchaudio.functional as AF


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "third_party/finesub/src"))

from finesub.speech.preprocessing.separator import separation  # noqa: E402


def _write_tone(path: Path, rate: int, seconds: float, channels: int = 2) -> np.ndarray:
    frames = int(rate * seconds)
    t = np.arange(frames, dtype=np.float64) / rate
    rng = np.random.default_rng(rate)
    mono = 0.3 * np.sin(2 * np.pi * 440 * t) + 0.05 * rng.standard_normal(frames)
    data = np.stack([mono * (1 - 0.2 * c) for c in range(channels)], axis=1)
    sf.write(str(path), data.astype(np.float32), rate, subtype="FLOAT")
    return data.astype(np.float32)


def _whole_file(data: np.ndarray, rate: int) -> np.ndarray:
    mono = torch.from_numpy(np.ascontiguousarray(data.T)).mean(dim=0)
    return AF.resample(mono.unsqueeze(0), rate, separation.ASR_TARGET_SR).squeeze(0).numpy()


class _CountingSoundFile(sf.SoundFile):
    opened = 0
    seeks = 0

    def __init__(self, *args, **kwargs) -> None:
        type(self).opened += 1
        super().__init__(*args, **kwargs)

    def seek(self, frames, whence=sf.SEEK_SET):
        # `read()` itself seeks to where it already is, to ask the position and
        # to sync it after reading; only a jump counts.
        before = super().seek(0, sf.SEEK_CUR)
        after = super().seek(frames, whence)
        if after != before:
            type(self).seeks += 1
        return after


class StreamAsrFramesTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.dir = Path(self._temp.name)
        self._window = separation._RESAMPLE_WINDOW_SECONDS
        # Short windows so a few seconds of audio cross several seams.
        separation._RESAMPLE_WINDOW_SECONDS = 1

    def tearDown(self) -> None:
        separation._RESAMPLE_WINDOW_SECONDS = self._window
        self._temp.cleanup()

    def _stream(self, path: Path) -> np.ndarray:
        return np.concatenate(list(separation.stream_asr_frames(path)))

    def test_matches_a_whole_file_resample(self) -> None:
        for rate in (44100, 48000, 22050, 16000):
            with self.subTest(rate=rate):
                path = self.dir / f"tone-{rate}.wav"
                data = _write_tone(path, rate, 4.37)
                streamed = self._stream(path)
                expected = _whole_file(data, rate)
                self.assertEqual(streamed.shape, expected.shape)
                np.testing.assert_allclose(streamed, expected, atol=1e-5)

    def test_opens_the_source_once_and_never_seeks(self) -> None:
        path = self.dir / "tone.wav"
        _write_tone(path, 48000, 4.37)
        _CountingSoundFile.opened = _CountingSoundFile.seeks = 0
        original = separation.sf.SoundFile
        separation.sf.SoundFile = _CountingSoundFile
        try:
            chunks = list(separation.stream_asr_frames(path))
        finally:
            separation.sf.SoundFile = original
        self.assertEqual(len(chunks), 5)
        self.assertEqual(_CountingSoundFile.opened, 1)
        self.assertEqual(_CountingSoundFile.seeks, 0)


class WindowSizeTest(unittest.TestCase):
    def test_window_is_twenty_seconds_at_every_rate(self) -> None:
        for rate in (44100, 48000, 32000, 22050, 96000):
            with self.subTest(rate=rate):
                step_src, _ = separation._rational_ratio(rate, separation.ASR_TARGET_SR)
                window = separation._whole_steps(
                    separation._RESAMPLE_WINDOW_SECONDS, rate, step_src
                )
                self.assertEqual(window % step_src, 0)
                self.assertAlmostEqual(window / rate, 20.0, delta=step_src / rate)

    def test_44100_keeps_the_old_step_counts(self) -> None:
        step_src, _ = separation._rational_ratio(44100, separation.ASR_TARGET_SR)
        self.assertEqual(
            separation._whole_steps(separation._RESAMPLE_WINDOW_SECONDS, 44100, step_src),
            2000 * step_src,
        )
        self.assertEqual(
            separation._whole_steps(separation._RESAMPLE_CONTEXT_SECONDS, 44100, step_src),
            20 * step_src,
        )


if __name__ == "__main__":
    unittest.main()
