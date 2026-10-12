"""WeSpeaker's fbank front end, recomputed here because it cannot be exported.

`WeSpeakerResNet34.forward` is exactly two steps -- `compute_fbank(waveforms)` and then
`resnet(fbank, weights)[1]`. The second is traced into `diarizen_embedding.ts`; the first cannot
be, because it runs `torch.vmap` over `torchaudio.compliance.kaldi.fbank` and the tracer stops
inside it with "Unsupported value kind: Tensor".

That is not a problem to route around, because the function is torchaudio's own and torchaudio is
already a declared dependency: production computes the same features from the same public API
rather than shipping a second copy of a mel filterbank.

⚠ The three lines here are the whole contract and each one matters:

* the ``* (1 << 15)`` scaling puts float audio on Kaldi's int16 convention -- drop it and every
  feature is off by 90 dB;
* ``dither=0.0`` is what makes this deterministic at all, and it comes from the checkpoint's own
  hyper-parameters rather than from torchaudio's default (which is 1.0, i.e. random);
* the per-utterance mean subtraction at the end is part of the model, not a normalisation someone
  added -- WeSpeaker trained with it.

`tools/diarizen_export/` checks this against the research path's own `compute_fbank` and requires
bit-exact agreement.
"""

from __future__ import annotations

from typing import Any

#: The checkpoint's own front-end settings (`WeSpeakerResNet34.__init__` defaults, which the
#: released weights were trained and released with). Named rather than inlined so the export-time
#: comparison and production read one copy.
FBANK = {
    "num_mel_bins": 80,
    "frame_length": 25,
    "frame_shift": 10,
    "dither": 0.0,
    "sample_frequency": 16000,
    "window_type": "hamming",
    "use_energy": False,
}


def compute_fbank(waveforms: Any) -> Any:
    """`(batch, channel, samples)` float audio -> `(batch, frames, mel)` features.

    Mirrors `BaseWeSpeakerResNet.compute_fbank`. `torch.vmap` rather than a Python loop because
    that is what the original does, and a loop over the batch is not the same computation once
    torchaudio's internals see a different shape.
    """
    import torch
    from torchaudio.compliance import kaldi

    scaled = waveforms * (1 << 15)
    features = torch.vmap(lambda one: kaldi.fbank(one, **FBANK))(scaled)
    return features - torch.mean(features, dim=1, keepdim=True)
