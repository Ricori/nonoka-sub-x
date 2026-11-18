"""Nemotron-3-Diarization in plain torch: log-mel front end, encoder, and the offline chunk loop.

A port of Hugging Face transformers' `nemotron3_diarization` (Apache-2.0, merged 2026-09-23 at
`c8b81b63`, not in a release yet) and its `NemotronAsrStreamingFeatureExtractor`, cut down to what
one offline pass over one file needs. It loads NVIDIA's own `model.safetensors` from
`nvidia/Nemotron-3-Diarization` unchanged, so production carries neither `nemo_toolkit` nor an
export of ours -- and one copy of the weights serves CPU and GPU alike.

What the model is: 128-mel features at 10 ms, stacked eight at a time and projected (80 ms encoder
frames), 31 pre-LN Transformer layers with rotary positions, then back up to 10 ms through a
sub-pixel convolution and an 8-way sigmoid head. The file is walked in chunks of 340 encoder frames
with 40 of look-ahead; every chunk also attends to an Arrival-Order Speaker Cache (264 frames kept
by score, grouped by speaker) and a 40-frame FIFO of the most recent frames. That cache is what
keeps track ids stable across a long file, and it is also why this chain has no "too small to
matter" numerical difference: a top-k selection that flips once makes the caches diverge for the
rest of the file (`docs/diarization-findings.md` 5.1 measured the same shape on Sortformer).

⚠ Checked against NeMo eager on real audio, which is what every exploration reading came from;
`tools/diarization_explore/nemotron3_port.py` holds that check and `docs/diarization-findings.md`
2.6 what it found. Changing the arithmetic here -- the attention kernel, where the
features are cut, the order of an operation -- is an execution-form change that owes the same
comparison again.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import torch
from torch import nn

from .chunking import feature_segments

SAMPLE_RATE = 16000
HOP_LENGTH = 160
N_FFT = 512
WIN_LENGTH = 400
PREEMPHASIS = 0.97
LOG_GUARD = 2.0**-24

#: The model's output rate: one row per spectrogram frame.
FRAME_SECONDS = HOP_LENGTH / SAMPLE_RATE

#: The two files a run opens, both from NVIDIA's repository at the pinned revision.
CONFIG_NAME = "config.json"
WEIGHTS_NAME = "model.safetensors"


# --- front end ------------------------------------------------------------------------------------

def mel_filters(num_mels: int, *, n_fft: int = N_FFT, sample_rate: int = SAMPLE_RATE):
    """`librosa.filters.mel(norm="slaney", htk=False)`, float32, reproduced to the bit.

    ⚠ Float32 accumulation on purpose: librosa fills a float32 array and scales it in place, and
    both NeMo and transformers take their filters from librosa. transformers' own float64
    `mel_filter_bank` differs in the last bits, which is why they did not use it either.
    """

    # librosa's Slaney scale, each constant computed the way it computes it.
    f_sp = 200.0 / 3
    min_log_hz = 1000.0
    min_log_mel = min_log_hz / f_sp
    logstep = np.log(6.4) / 27.0

    def hz_to_mel(hz):
        hz = np.asanyarray(hz, dtype=np.float64)
        logarithmic = min_log_mel + np.log(np.maximum(hz, min_log_hz) / min_log_hz) / logstep
        return np.where(hz >= min_log_hz, logarithmic, hz / f_sp)

    def mel_to_hz(mels):
        mels = np.asanyarray(mels, dtype=np.float64)
        return np.where(mels >= min_log_mel, min_log_hz * np.exp(logstep * (mels - min_log_mel)),
                        f_sp * mels)

    weights = np.zeros((num_mels, 1 + n_fft // 2), dtype=np.float32)
    fft_frequencies = np.fft.rfftfreq(n=n_fft, d=1.0 / sample_rate)
    mel_f = mel_to_hz(np.linspace(hz_to_mel(0.0), hz_to_mel(sample_rate / 2), num_mels + 2))
    fdiff = np.diff(mel_f)
    ramps = np.subtract.outer(mel_f, fft_frequencies)
    for index in range(num_mels):
        lower = -ramps[index] / fdiff[index]
        upper = ramps[index + 2] / fdiff[index + 1]
        weights[index] = np.maximum(0, np.minimum(lower, upper))
    weights *= (2.0 / (mel_f[2:num_mels + 2] - mel_f[:num_mels]))[:, np.newaxis]
    return weights


def _as_checkpoint(values):
    """`values` as NeMo loads them: rounded to bfloat16, computed in float32.

    ⚠⚠ Not cosmetic. The `.nemo` checkpoint is stored in bfloat16 -- the STFT window and the mel
    filterbank included, as buffers -- and NeMo loads those buffers over the ones it computes. So
    every reading this backend was chosen on used the bf16-rounded window and filters (checked:
    the checkpoint's buffers equal these values rounded, bit for bit). transformers' conversion
    drops the buffers and recomputes them at full precision, which is a different front end:
    measured on hina-10 it moved log-mel values by up to 3.0 in the quietest bands, and through the
    speaker cache that became 15% of frames and three extra tracks. The weights need no such step:
    `model.safetensors` already holds the bf16 values, widened.
    """
    return values.to(torch.bfloat16).to(torch.float32)


def _log_mel_piece(samples, filters, window):
    """Centred log-mel frames of one contiguous stretch of audio, `(frames, mels)`."""
    emphasised = torch.cat([samples[:1], samples[1:] - PREEMPHASIS * samples[:-1]])
    spectrum = torch.stft(emphasised[None], N_FFT, hop_length=HOP_LENGTH, win_length=WIN_LENGTH,
                          window=window, return_complex=True, pad_mode="constant", center=True)
    # sqrt then square rather than `abs() ** 2`: the order the reference front ends use.
    magnitudes = torch.sqrt(torch.view_as_real(spectrum).pow(2).sum(-1)).pow(2)
    return torch.log(filters @ magnitudes + LOG_GUARD)[0].transpose(0, 1)


def log_mel(waveform, *, num_mels: int, device) -> tuple["torch.Tensor", int]:
    """`(features, valid)`: `(frames, mels)` float32 on the host, and how many frames are audio.

    `frames` is `samples // hop + 1` -- the centred STFT's count -- of which the first `valid =
    samples // hop` are audio and the last is zeroed, as the reference extractor's attention mask
    does.

    ⚠ Computed in bounded pieces (`chunking.feature_segments`: ten minutes kept, ten seconds of real
    audio on either side). NeMo's one call over a whole file needed 5.3 GiB for 82 minutes; a piece
    here is flat whatever the length. The context is far wider than anything a kept frame reads
    (256 samples of window, one sample of pre-emphasis), and every cut is on the hop grid.
    """
    samples = int(waveform.shape[-1])
    source = waveform.reshape(-1).to(torch.float32).cpu()
    valid = samples // HOP_LENGTH
    features = torch.zeros((valid + 1, num_mels), dtype=torch.float32)
    filters = _as_checkpoint(torch.from_numpy(mel_filters(num_mels))).to(device)
    window = _as_checkpoint(torch.hann_window(WIN_LENGTH, periodic=False)).to(device)
    for segment in feature_segments(samples):
        piece = source[segment.sample_start:segment.sample_end].to(device)
        frames = _log_mel_piece(piece, filters, window)
        kept = frames[segment.keep_from:segment.keep_from + segment.frame_count]
        features[segment.frame_start:segment.frame_start + segment.frame_count] = kept.cpu()
        del piece, frames, kept
    return features, valid


# --- configuration --------------------------------------------------------------------------------

@dataclass(frozen=True)
class Nemotron3Config:
    """What `config.json` says, under the names this module uses.

    The chunking values are the model card's offline profile (`chunk_length`,
    `chunk_right_context`, `fifo_length`, `speaker_cache_update_period` at the top level); the
    speaker-cache policy is the streaming block's, which both profiles share.
    """

    hidden_size: int = 512
    num_layers: int = 31
    num_heads: int = 8
    intermediate_size: int = 2048
    num_mels: int = 128
    subsampling: int = 8
    rope_theta: float = 10000.0
    head_hidden_size: int = 192
    num_speakers: int = 8
    chunk_length: int = 340
    chunk_right_context: int = 40
    fifo_length: int = 40
    update_period: int = 300
    cache_length: int = 264
    silence_frames: int = 1
    score_threshold: float = 0.25
    latest_boost: float = 0.05
    strong_boost_rate: float = 0.75
    weak_boost_rate: float = 1.5
    min_positive_rate: float = 0.5

    @classmethod
    def from_file(cls, path: str | Path) -> "Nemotron3Config":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        audio, head, streaming = raw["audio_config"], raw["head_config"], raw["streaming_config"]
        rope = audio.get("rope_parameters") or {}
        if rope.get("rope_type", "default") != "default" or rope.get("partial_rotary_factor", 1.0) != 1.0:
            raise ValueError(f"unsupported rotary configuration {rope}")
        if audio.get("hidden_act", "gelu") != "gelu" or audio["num_key_value_heads"] != audio["num_attention_heads"]:
            raise ValueError("unsupported encoder configuration")
        return cls(
            hidden_size=audio["hidden_size"], num_layers=audio["num_hidden_layers"],
            num_heads=audio["num_attention_heads"], intermediate_size=audio["intermediate_size"],
            num_mels=audio["num_mel_bins"], subsampling=audio["subsampling_factor"],
            rope_theta=float(rope.get("rope_theta", 10000.0)),
            head_hidden_size=head["hidden_size"], num_speakers=head["num_speakers"],
            chunk_length=raw["chunk_length"], chunk_right_context=raw["chunk_right_context"],
            fifo_length=raw["fifo_length"], update_period=raw["speaker_cache_update_period"],
            cache_length=streaming["speaker_cache_length"],
            silence_frames=streaming["speaker_cache_silence_frames_per_speaker"],
            score_threshold=streaming["prediction_score_threshold"],
            latest_boost=streaming["latest_frames_score_boost"],
            strong_boost_rate=streaming["strong_boost_rate"],
            weak_boost_rate=streaming["weak_boost_rate"],
            min_positive_rate=streaming["min_positive_scores_rate"],
        )


# --- the network ----------------------------------------------------------------------------------

def _rotate_half(x):
    half = x.shape[-1] // 2
    return torch.cat((-x[..., half:], x[..., :half]), dim=-1)


class _Attention(nn.Module):
    def __init__(self, config: Nemotron3Config):
        super().__init__()
        self.num_heads = config.num_heads
        self.head_dim = config.hidden_size // config.num_heads
        self.scaling = self.head_dim ** -0.5
        width = config.num_heads * self.head_dim
        self.q_proj = nn.Linear(config.hidden_size, width, bias=False)
        self.k_proj = nn.Linear(config.hidden_size, width, bias=False)
        self.v_proj = nn.Linear(config.hidden_size, width, bias=False)
        self.o_proj = nn.Linear(width, config.hidden_size, bias=True)

    def forward(self, hidden, cos, sin, mask):
        batch, length, _ = hidden.shape
        shape = (batch, length, self.num_heads, self.head_dim)
        query = self.q_proj(hidden).view(shape).transpose(1, 2)
        key = self.k_proj(hidden).view(shape).transpose(1, 2)
        value = self.v_proj(hidden).view(shape).transpose(1, 2)
        query = query * cos + _rotate_half(query) * sin
        key = key * cos + _rotate_half(key) * sin
        weights = torch.matmul(query, key.transpose(2, 3)) * self.scaling
        if mask is not None:
            weights = weights + mask
        weights = torch.softmax(weights, dim=-1, dtype=torch.float32).to(query.dtype)
        out = torch.matmul(weights, value).transpose(1, 2).reshape(batch, length, -1)
        return self.o_proj(out)


class _Layer(nn.Module):
    def __init__(self, config: Nemotron3Config):
        super().__init__()
        self.layer_norm1 = nn.LayerNorm(config.hidden_size)
        self.self_attn = _Attention(config)
        self.layer_norm2 = nn.LayerNorm(config.hidden_size)
        self.mlp = nn.Module()
        self.mlp.fc1 = nn.Linear(config.hidden_size, config.intermediate_size)
        self.mlp.fc2 = nn.Linear(config.intermediate_size, config.hidden_size)

    def forward(self, hidden, cos, sin, mask):
        hidden = hidden + self.self_attn(self.layer_norm1(hidden), cos, sin, mask)
        mlp = self.mlp
        return hidden + mlp.fc2(nn.functional.gelu(mlp.fc1(self.layer_norm2(hidden))))


class Nemotron3Network(nn.Module):
    """The weights, named as in `model.safetensors` so it loads with `strict=True`."""

    def __init__(self, config: Nemotron3Config):
        super().__init__()
        self.config = config
        self.model = nn.Module()
        tower = self.model.audio_tower = nn.Module()
        tower.embedder = nn.Module()
        tower.embedder.projection = nn.Linear(config.subsampling * config.num_mels,
                                              config.hidden_size, bias=False)
        tower.input_layer_norm = nn.LayerNorm(config.hidden_size)
        tower.layers = nn.ModuleList(_Layer(config) for _ in range(config.num_layers))
        tower.layer_norm = nn.LayerNorm(config.hidden_size)
        self.model.proj = nn.Linear(config.hidden_size, config.head_hidden_size)
        self.model.upsampler = nn.Module()
        self.model.upsampler.conv = nn.Conv1d(config.head_hidden_size,
                                              config.head_hidden_size * config.subsampling,
                                              kernel_size=3, padding=1)
        self.classifier = nn.Module()
        self.classifier.dense = nn.Linear(config.head_hidden_size, config.head_hidden_size)
        self.classifier.out_proj = nn.Linear(config.head_hidden_size, config.num_speakers)
        self.silence_embeds = nn.Parameter(torch.zeros(config.hidden_size))
        head_dim = config.hidden_size // config.num_heads
        inv_freq = 1.0 / (config.rope_theta ** (torch.arange(0, head_dim, 2, dtype=torch.float) / head_dim))
        self.register_buffer("inv_freq", inv_freq, persistent=False)

    def embed(self, features):
        """`(batch, frames, mels)` -> `(batch, ceil(frames / 8), hidden)`; the last group zero-padded."""
        batch, frames, mels = features.shape
        padding = -frames % self.config.subsampling
        features = nn.functional.pad(features, (0, 0, 0, padding))
        stacked = features.reshape(batch, (frames + padding) // self.config.subsampling,
                                   mels * self.config.subsampling)
        return self.model.audio_tower.embedder.projection(stacked)

    def _rotary(self, length: int, device):
        positions = torch.arange(length, device=device, dtype=torch.float)[None, :]
        frequencies = (self.inv_freq[None, :, None].to(device) @ positions[:, None, :]).transpose(1, 2)
        angles = torch.cat((frequencies, frequencies), dim=-1)
        return angles.cos()[:, None], angles.sin()[:, None]

    def logits(self, embeds, valid):
        """Speaker logits at 10 ms for one step's encoder input.

        `valid` is a `(batch, length)` bool mask of real frames, or `None` when all are. Positions
        restart at every step, as in the original.
        """
        tower = self.model.audio_tower
        cos, sin = self._rotary(embeds.shape[1], embeds.device)
        mask = None
        if valid is not None and not bool(valid.all()):
            mask = torch.zeros(valid.shape, dtype=embeds.dtype, device=embeds.device)
            mask = mask.masked_fill(~valid, torch.finfo(embeds.dtype).min)[:, None, None, :]
        hidden = tower.input_layer_norm(embeds)
        for layer in tower.layers:
            hidden = layer(hidden, cos, sin, mask)
        hidden = self.model.proj(tower.layer_norm(hidden))
        batch, length, width = hidden.shape
        hidden = self.model.upsampler.conv(hidden.transpose(1, 2)).transpose(1, 2)
        hidden = hidden.reshape(batch, length * self.config.subsampling, width)
        head = self.classifier
        return head.out_proj(torch.relu(head.dense(torch.relu(hidden))))


# --- the Arrival-Order Speaker Cache --------------------------------------------------------------

class SpeakerCache:
    """The streaming state one offline pass carries from chunk to chunk.

    `transformers`' `Nemotron3DiarizationSpeakerCache` at batch size one, with the offline FIFO
    sizes. Kept close to the original on purpose: every line here decides which frames later chunks
    attend to, and a rewrite that is "equivalent" but orders a top-k differently is a different model.
    """

    def __init__(self, config: Nemotron3Config):
        self.config = config
        budget = config.cache_length // config.num_speakers - config.silence_frames
        self.min_positive_scores = math.floor(budget * config.min_positive_rate)
        self.strong_boosted = math.floor(budget * config.strong_boost_rate)
        self.weak_boosted = math.floor(budget * config.weak_boost_rate)
        self.embeds = self.probs = self.fifo = None
        self.cache_frames = 0
        self.fifo_frames = 0
        self.compressed = False

    def _initialise(self, like):
        config = self.config
        options = {"device": like.device, "dtype": like.dtype}
        self.embeds = torch.zeros(1, config.cache_length, like.shape[-1], **options)
        self.probs = torch.zeros(1, config.cache_length, config.num_speakers, **options)
        self.fifo = torch.zeros(1, config.fifo_length, like.shape[-1], **options)

    def cached(self, like):
        if self.embeds is None:
            self._initialise(like)
        return torch.cat([self.embeds[:, :self.cache_frames], self.fifo[:, :self.fifo_frames]], dim=1)

    def update(self, step_embeds, step_logits, silence_embeds, chunk_frames: int, valid):
        config = self.config
        cache_frames, fifo_frames = self.cache_frames, self.fifo_frames
        factor = config.subsampling
        probs = nn.functional.avg_pool1d(step_logits.sigmoid().transpose(1, 2), factor, factor).transpose(1, 2)
        if valid is not None:
            probs = probs * valid.to(dtype=probs.dtype)[..., None]
        probs = probs.to(self.probs)

        start = cache_frames + fifo_frames
        fifo_embeds = torch.cat([self.fifo[:, :fifo_frames], step_embeds[:, start:start + chunk_frames]], dim=1)
        total = fifo_embeds.shape[1]
        popped = 0 if total <= config.fifo_length else min(max(config.update_period, total - config.fifo_length), total)
        if popped:
            fifo_probs = probs[:, cache_frames:cache_frames + total]
            # An uncompressed cache still holds plain chunk frames, whose probabilities this step
            # re-estimates; a compressed one is out of order, so only its stored probabilities fit.
            stored = self.probs[:, :cache_frames] if self.compressed else probs[:, :cache_frames]
            cache_embeds = torch.cat([self.embeds[:, :cache_frames], fifo_embeds[:, :popped]], dim=1)
            cache_probs = torch.cat([stored, fifo_probs[:, :popped]], dim=1)
            fifo_embeds = fifo_embeds[:, popped:]
            if cache_embeds.shape[1] > config.cache_length:
                cache_embeds, cache_probs = self._compress(cache_embeds, cache_probs, silence_embeds)
                self.compressed = True
            self.cache_frames = cache_embeds.shape[1]
            index = torch.arange(self.cache_frames, device=self.embeds.device)
            self.embeds.index_copy_(1, index, cache_embeds)
            self.probs.index_copy_(1, index, cache_probs)
        self.fifo_frames = fifo_embeds.shape[1]
        self.fifo.index_copy_(1, torch.arange(self.fifo_frames, device=self.fifo.device), fifo_embeds)

    def _scores(self, probs):
        threshold = self.config.score_threshold
        log_probs = torch.log(probs.clamp(min=threshold))
        log_complements = torch.log((1.0 - probs).clamp(min=threshold))
        scores = log_probs - log_complements + log_complements.sum(dim=-1, keepdim=True) - math.log(0.5)
        speech = probs > 0.5
        scores = scores.masked_fill(~speech, float("-inf"))
        positive = scores > 0
        enough = positive.sum(dim=1, keepdim=True) >= self.min_positive_scores
        return scores.masked_fill(~positive & speech & enough, float("-inf"))

    @staticmethod
    def _boost(scores, count: int, boost: float):
        _, indices = torch.topk(scores, count, dim=1, sorted=False)
        return scores.scatter_add(1, indices, scores.new_full(indices.shape, boost))

    def _compress(self, embeds, probs, silence_embeds):
        """Keep the `cache_length` best frames, grouped by speaker, in order within a speaker."""
        config = self.config
        _, frames, speakers = probs.shape
        scores = self._scores(probs)
        # Frames beyond the cache capacity are the ones just popped from the FIFO.
        scores[:, config.cache_length:] += config.latest_boost
        scores = self._boost(scores, self.strong_boosted, -2.0 * math.log(0.5))
        scores = self._boost(scores, self.weak_boosted, -math.log(0.5))
        scores = nn.functional.pad(scores, (0, 0, 0, config.silence_frames), value=float("inf"))
        embeds = torch.cat([embeds, silence_embeds.to(embeds).view(1, 1, -1)], dim=1)
        probs = nn.functional.pad(probs, (0, 0, 0, 1))

        scored = frames + config.silence_frames
        sentinel = scored * speakers
        flat = scores.transpose(1, 2).reshape(1, -1)
        top_scores, top = torch.topk(flat, config.cache_length, dim=1, sorted=False)
        top = top.masked_fill(top_scores == float("-inf"), sentinel)
        top, _ = torch.sort(top, dim=1)
        frame = torch.where(top == sentinel, frames, (top % scored).clamp(max=frames))
        return embeds[0, frame[0]][None], probs[0, frame[0]][None]


# --- loading and the offline pass -----------------------------------------------------------------

def load(directory: str | Path, device) -> Nemotron3Network:
    """Build the network from `config.json` and load `model.safetensors` into it, strictly."""
    from safetensors.torch import load_file

    directory = Path(directory)
    network = Nemotron3Network(Nemotron3Config.from_file(directory / CONFIG_NAME))
    network.load_state_dict(load_file(str(directory / WEIGHTS_NAME)), strict=True)
    return network.to(device).eval()


@torch.inference_mode()
def speaker_probabilities(network: Nemotron3Network, waveform, *, device) -> "np.ndarray":
    """`(frames, speakers)` activity probabilities at 10 ms for one mono 16 kHz waveform.

    The offline profile: chunks of `chunk_length` encoder frames, each seeing `chunk_right_context`
    frames ahead and everything the cache holds. Tracks are numbered by first arrival.

    ⚠ The stacking projection runs per chunk, on that chunk and its look-ahead, as NeMo's
    streaming loop does -- not once over the whole file as transformers does. A GEMM's last bits
    can depend on how many rows it is given, and this chain amplifies last bits.
    """
    config = network.config
    features, valid = log_mel(waveform, num_mels=config.num_mels, device=device)
    frames = features.shape[0]
    factor = config.subsampling
    encoder_frames = -(-frames // factor)
    # An encoder frame is real when its first spectrogram frame is.
    real = torch.arange(encoder_frames) * factor < valid
    cache = SpeakerCache(config)
    pieces = []
    for start in range(0, encoder_frames, config.chunk_length):
        end = min(start + config.chunk_length, encoder_frames)
        stop = min(end + config.chunk_right_context, encoder_frames)
        chunk = network.embed(features[start * factor:stop * factor][None].to(device))
        cached = cache.cached(chunk)
        step = torch.cat([cached, chunk], dim=1)
        step_valid = torch.cat([torch.ones(cached.shape[1], dtype=torch.bool),
                                real[start:stop]])[None].to(device)
        logits = network.logits(step, step_valid)
        cache.update(step, logits, network.silence_embeds, end - start,
                     None if bool(step_valid.all()) else step_valid)
        first = cached.shape[1] * factor
        pieces.append(logits[0, first:first + (end - start) * factor].sigmoid().cpu())
        del chunk, cached, step, logits
    if not pieces:
        return np.zeros((0, config.num_speakers), dtype=np.float32)
    return torch.cat(pieces)[:frames].numpy()
