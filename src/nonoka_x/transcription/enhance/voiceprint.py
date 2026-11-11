"""WeSpeaker's fbank front end and the PLDA transforms, for the voiceprints `relink` compares.

Copied from FineSub v0.7.0 @ 7123055, unchanged (WP1): `FBANK` and `compute_fbank` from
`finesub/speech/diarization/diarizen_features.py`, `l2_norm` and `load_plda` from `diarizen_cluster.py`, and
`MODEL_FILES` after `nemotron3_backend.PLDA_FILES` (the speaker-embedding model's PLDA files). The model's
directory comes from Nonoka's model manifest (`speaker-embedding`, through `enhance.model_dir`), not from
FineSub's artifact search or its self-download. Nonoka maintains this copy; FineSub's originals are left as they
are.

⚠ From `diarizen_features`: the ``* (1 << 15)`` scaling puts float audio on Kaldi's int16 convention,
``dither=0.0`` is what makes this deterministic, and the per-utterance mean subtraction is part of the model.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

#: The model-manifest id of the voiceprint model, and the files `relink` opens in it.
MODEL_ID = "speaker-embedding"
PLDA_FILES = ("plda/plda.npz", "plda/xvec_transform.npz")
MODEL_FILES = ("wespeaker-cpu.ts", *PLDA_FILES)

#: The checkpoint's own front-end settings (`WeSpeakerResNet34.__init__` defaults).
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


def l2_norm(matrix):
    """Row-wise L2 normalisation, matching `VBx.l2_norm` for the 2-D case."""
    import numpy as np

    return matrix / np.linalg.norm(matrix, axis=1, ord=2)[:, np.newaxis]


def load_plda(plda_dir: str | Path):
    """`(xvec_tf, plda_tf, plda_psi)` -- the transforms that put embeddings in PLDA space.

    ⚠ `plda_psi` and `plda_tr` are REORDERED by the generalised eigenvalue solve and then reversed,
    so the largest across-class variance comes first. Skipping the reversal keeps the shapes and
    silently ranks the dimensions backwards.
    """
    import numpy as np
    from scipy.linalg import eigh

    directory = Path(plda_dir)
    transform = np.load(directory / "xvec_transform.npz")
    mean1, mean2, lda = transform["mean1"], transform["mean2"], transform["lda"]

    plda = np.load(directory / "plda.npz")
    plda_mu, plda_tr, plda_psi = plda["mu"], plda["tr"], plda["psi"]

    within = np.linalg.inv(plda_tr.T.dot(plda_tr))
    between = np.linalg.inv((plda_tr.T / plda_psi).dot(plda_tr))
    acvar, wccn = eigh(between, within)
    plda_psi = acvar[::-1]
    plda_tr = wccn.T[::-1]

    def xvec_tf(x):
        return np.sqrt(lda.shape[1]) * l2_norm(
            lda.T.dot(np.sqrt(lda.shape[0]) * l2_norm(x - mean1).T).T - mean2
        )

    def plda_tf(x0, lda_dim=lda.shape[1]):
        return (x0 - plda_mu).dot(plda_tr.T)[:, :lda_dim]

    return xvec_tf, plda_tf, plda_psi
