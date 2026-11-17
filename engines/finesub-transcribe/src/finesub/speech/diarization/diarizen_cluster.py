"""Tie the per-chunk speakers together: AHC, then VBx, then a constrained assignment.

This is the half of DiariZen that is not a neural network. The research path reaches it through the
vendored `pyannote.audio` fork plus `diarizen.clustering.VBx`; production may carry neither, and it
does not have to -- the whole thing is numpy and scipy, both already declared dependencies.

Ported faithfully, not improved. The VB-HMM core comes from
`diarizen/clustering/VBx.py` (Apache-2.0), itself an implementation of Landini et al., "Bayesian
HMM clustering of x-vector sequences (VBx) in speaker diarization".

Four things decide whether the numbers match, and each is silent when wrong:

* **`loopProb = 0`**, so the update is the GMM branch, not the HMM one. The forward-backward path
  in the original is never taken here and is not ported -- a `loopProb > 0` caller gets an error
  rather than a plausible answer from code nobody checked.
* **Embeddings are filtered three ways before clustering**: the speaker must be active in the
  chunk, the embedding must not be NaN, and the speaker needs at least 10% of the chunk's frames
  to itself. If that leaves fewer than two, the frame requirement is dropped and only then -- a
  fallback for material that is short or wholly overlapped.
* **AHC runs on L2-normalised embeddings** with centroid linkage and a Euclidean metric, cut at
  0.6 by distance. Normalising after linkage, or using cosine there, gives a different tree.
* **The final assignment is constrained**: one cluster per speaker within a chunk, solved as an
  assignment problem. Plain `argmax` would happily give two local speakers the same identity.

⚠ `max_num_embeddings` is unbounded for this configuration, so nothing is subsampled. That matters
beyond speed: the original subsamples with `random.shuffle` under the global seed, which would
make the result depend on how much random state the process had consumed beforehand.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: `[clustering.args]` from the checkpoint's config.toml.
AHC_THRESHOLD = 0.6
AHC_CRITERION = "distance"
FA = 0.07
FB = 0.8
LDA_DIM = 128

#: ⚠ Not a "more iterations is more accurate" knob: six of seven files never satisfy
#: `elbo - previous < 1e-4` and stop because they hit this cap, so changing it picks a different
#: point on a trajectory rather than refining one -- and through the hard top-k downstream that
#: becomes flipped labels. Same class as `diarizen_runner.DEFAULT_BATCH_SIZE`. Upstream's value,
#: copied faithfully; `docs/diarization-findings.md` 5.2.
MAX_ITERS = 20

#: `cluster_vbx`'s own defaults.
INIT_SMOOTHING = 7.0
LOOP_PROB = 0.0
MIN_FRAMES_RATIO = 0.1


def merge_suggestions(centroids, centroid_of_track, sizes, *, keep: "int | None" = None,
                      max_distance: "float | None" = None):
    """`[{"track": a, "like": b, "distance": d}]` -- small tracks that look like a bigger one.

    ⚠ The second arm of Q11, and the reason it exists: a hard cap is **irreversible** in practice.
    `-diar.json` is a cache that is never recomputed on demand, so a cap baked into a timeline can
    only be undone by deleting it and rerunning. A suggestion keeps VBx's partition and attaches an
    attribute, leaving the acoustic tracks alone the way every later layer does -- and it works
    with no prior at all, where a cap needs a roster that may not exist.

    `sizes` is how much each track speaks (voiced frames, seconds -- any measure, indexed by
    track id). ⚠ **Required, because "smaller" is the whole semantics and there is no safe
    default.** Track ids come from `np.unique` renumbering cluster ids and carry no information
    about size, so ordering by id -- which this function did until 2026-09-20 -- compares each
    track against arbitrary others and lets a junk track serve as a merge target.

    `keep` is how many of the LARGEST tracks to treat as established; everything past them is a
    candidate. With `None` every track is compared against every larger one. `max_distance` drops
    suggestions further apart than that.

    ⚠⚠ **Measured twice, and the two readings disagree because the TRUTH differs** (2026-09-20).

        hina, 141 suggestions / 24 files:  50.4% correct, 36.9% would merge two different
                                           people, 12.8% name a track that leads no line
        onetonton, 9 / 15 files:           77.8% correct, 11.1% wrong, 11.1% unnamed
                                           gated at distance <= 0.5: 5 kept, 5 correct

    The difference is not the algorithm. hina's cast list holds role placeholders (`嘉宾`, `男主`)
    and game characters, so "the truth says these two tracks are the same person" does not mean
    one human said both -- see `docs/diarization-findings.md`. 姐姐咚咚 is three named humans
    throughout. **The onetonton reading is the one to believe, and the hina one is what the same
    measurement looks like when the labels are roles.**

    ⚠ Still no default `max_distance`: the gated onetonton population is 5 suggestions. It is
    right 5 times out of 5, which is encouraging and is not a calibration. The parameter exists so
    a caller can gate it, not so this function can pretend to know where the line is.

    ⚠ An earlier reading of this said "49.1% would merge two different people". That number came
    from a version of this function that ordered tracks by **id** -- so it compared each track
    against arbitrary others and let a track that barely speaks act as a merge target. Both the
    bug and the contaminated truth pushed the same way. Do not cite it.
    """
    import numpy as np
    from scipy.spatial.distance import cdist

    centroids = np.asarray(centroids)
    mapping = np.asarray(centroid_of_track)
    weight = np.asarray(sizes, dtype=np.float64).ravel()
    if centroids.shape[0] < 2 or not len(mapping):
        return []

    distances = cdist(centroids, centroids, metric="cosine")
    placed = [track for track in range(len(mapping))
              if mapping[track] >= 0 and track < len(weight)]
    # ⚠ Largest first. Ties break by id only to stay deterministic; everything else about the
    # ordering is the size, because the suggestion being offered is "fold this small one in".
    order = sorted(placed, key=lambda track: (-weight[track], track))
    established = order if keep is None else order[:max(1, keep)]

    found = []
    for rank, track in enumerate(order):
        if keep is None:
            # Only tracks that speak MORE than this one -- the earlier entries in the order.
            options = order[:rank]
        elif rank < max(1, keep):
            continue  # established: a merge target, not a candidate
        else:
            options = [other for other in established if other != track]
        if not options:
            continue
        nearest = min(options, key=lambda other: distances[mapping[track], mapping[other]])
        distance = float(distances[mapping[track], mapping[nearest]])
        if max_distance is not None and distance > max_distance:
            continue
        found.append({"track": int(track), "like": int(nearest),
                      "distance": round(distance, 4)})
    return found


@dataclass(frozen=True)
class ClusterResult:
    """What clustering one file produced.

    ⚠ `centroid_of_track` is not the identity. The `np.unique` renumbering gives the
    unplaceable-speaker sentinel an id of its own, so a track id is not a column of `soft` or
    `centroids`; index through this, with `-1` meaning "this track has no centroid".

    `single_speaker_log_bayes` is `ELBO_K - ELBO_1`: how much better the clustering's own model
    explains the embeddings than a single-speaker model does. Large means the model prefers
    several speakers; near zero or below means the evidence for more than one is weak.
    ⚠ **Not calibrated on the failure it is for.** What it should catch is genuinely single-speaker material being over-split, and the bed's single-speaker arm always clustered to K=1, where this ratio is identically 0 by construction -- a perfect separation that demonstrates nothing (`docs/diarization-findings.md` 4.4).  ⚠ A number, not a verdict --
    nothing suppresses labels on it. ⚠ It is a **total over the embeddings**, so it
    grows with the length of the file; `log_bayes_embeddings` is the divisor that makes two files
    comparable, and reading the total across files without it is meaningless.
    """

    hard: Any
    soft: Any
    centroids: Any
    centroid_of_track: Any
    single_speaker_log_bayes: float
    #: How many embeddings the two ELBOs were summed over.
    log_bayes_embeddings: int = 0
    #: ⚠ Total responsibility behind each centroid -- the divisor that turns `centroids` from a
    #: weighted SUM into a weighted MEAN. It is here because the sum is what the port produces
    #: and the sum is right for the only thing production does with it (cosine, which does not
    #: care about scale), but it is wrong for anything downstream that applies a transform with
    #: a TRANSLATION in it. See `mean_centroids`.
    centroid_weights: Any = None


def mean_centroids(result: "ClusterResult"):
    """`centroids` as a weighted MEAN, for anyone who needs a point rather than a direction.

    ⚠⚠ **Read this before comparing two clusters' voiceprints.** `cluster()` returns
    `gamma.T @ train` without dividing, because production only ever takes a cosine of it and a
    cosine cannot see scale. That makes the stored centroid proportional to how much the cluster
    SPOKE: a cluster with a hundred embeddings sits a hundred times further from the origin than
    the same voice with one.

    Every transform that starts by subtracting a mean -- `load_plda`'s `xvec_tf` does, and the
    identity beds run centroids through it -- destroys that invariance. Measured on real data:
    the same direction at 100x the mass lands at a PLDA cosine distance of **0.227** from itself
    (2026-09-20 audit, finding 5). So a "same person against different person" gap measured on
    raw centroids has track length mixed into it and is not purely about the voice.

    ⚠ A component with no responsibility at all keeps its row rather than becoming NaN; there is
    nothing there to normalise and a NaN would propagate into every distance in the matrix.
    """
    import numpy as np

    centroids = np.asarray(result.centroids, dtype=np.float64)
    weights = result.centroid_weights
    if weights is None or not len(np.asarray(weights)):
        return centroids
    divisor = np.asarray(weights, dtype=np.float64).reshape(-1, 1)
    return np.divide(centroids, divisor, out=centroids.copy(), where=divisor > 0)


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


def vbx(features, phi, *, fa: float, fb: float, gamma, max_iters: int = MAX_ITERS,
        epsilon: float = 1e-4):
    """`(gamma, pi, elbo)` -- the VB-HMM update, GMM branch only (`loopProb = 0`).

    ⚠ The ELBO is returned because it is the only principled answer to "is this one speaker":
    run the same update with a single component and the difference is a log Bayes factor, in the
    same space and the same model, with no new parameter to calibrate. It was being discarded.

    Ported from `diarizen/clustering/VBx.py` (Apache-2.0). Equation numbers refer to Landini et
    al.; the variable names are kept so the two can be read side by side.
    """
    import numpy as np
    from scipy.special import logsumexp

    dimension = features.shape[1]
    pi = np.full(gamma.shape[1], 1.0 / gamma.shape[1])
    constant = -0.5 * (np.sum(features ** 2, axis=1, keepdims=True)
                       + dimension * np.log(2 * np.pi))          # (23)
    rho = features * np.sqrt(phi)                                # (18)

    previous = None
    alpha = inv_l = None
    elbo = float("-inf")
    for iteration in range(max_iters):
        inv_l = 1.0 / (1 + fa / fb * gamma.sum(axis=0, keepdims=True).T * phi)   # (17)
        alpha = fa / fb * inv_l * gamma.T.dot(rho)                               # (16)
        log_p = fa * (rho.dot(alpha.T) - 0.5 * (inv_l + alpha ** 2).dot(phi) + constant)  # (23)

        log_pi = np.log(pi + 1e-8)
        log_px = logsumexp(log_p + log_pi, axis=-1)
        total = np.sum(log_px, axis=0)
        gamma = np.exp(log_p + log_pi - log_px[:, None])
        pi = np.sum(gamma, axis=0)
        pi = pi / pi.sum()

        elbo = total + fb * 0.5 * np.sum(np.log(inv_l) - inv_l - alpha ** 2 + 1)   # (25)
        if previous is not None and elbo - previous < epsilon:
            break
        previous = elbo
    return gamma, pi, float(elbo)


def clean_frame_mask(segmentations, min_frames: int):
    """Per (chunk, speaker): does this speaker have `min_frames` frames to itself?"""
    import numpy as np

    alone = np.sum(segmentations, axis=2, keepdims=True) == 1
    return np.sum(segmentations * alone, axis=1) >= min_frames


def filter_embeddings(embeddings, segmentations, *, min_frames_ratio: float = MIN_FRAMES_RATIO):
    """`(kept, dimension)` embeddings plus the (chunk, speaker) indices they came from."""
    import numpy as np

    active = np.sum(segmentations, axis=1) > 0
    valid = ~np.any(np.isnan(embeddings), axis=2)
    min_frames = round(min_frames_ratio * segmentations.shape[1])
    keep = active * valid * clean_frame_mask(segmentations, min_frames)
    chunk_idx, speaker_idx = np.where(keep)
    if len(chunk_idx) < 2:
        # Short or wholly overlapped material: drop the frame requirement, keep the rest.
        keep = active * valid * clean_frame_mask(segmentations, 0)
        chunk_idx, speaker_idx = np.where(keep)
    return embeddings[chunk_idx, speaker_idx], chunk_idx, speaker_idx


def constrained_argmax(soft_clusters):
    """One cluster per local speaker within a chunk, as an assignment problem."""
    import numpy as np
    from scipy.optimize import linear_sum_assignment

    soft_clusters = np.nan_to_num(soft_clusters, nan=np.nanmin(soft_clusters))
    chunks, speakers, _ = soft_clusters.shape
    hard = -2 * np.ones((chunks, speakers), dtype=np.int8)
    for index, cost in enumerate(soft_clusters):
        rows, columns = linear_sum_assignment(cost, maximize=True)
        for row, column in zip(rows, columns):
            hard[index, row] = column
    return hard


def cluster(embeddings, segmentations, plda_dir: str | Path, *,
            ahc_threshold: float = AHC_THRESHOLD, fa: float = FA, fb: float = FB,
            lda_dim: int = LDA_DIM, max_iters: int = MAX_ITERS,
            max_speakers: "int | None" = None) -> Any:
    """`(hard_clusters, soft_clusters, centroids, centroid_of_track)` for one file.

    ⚠⚠ `max_speakers` caps the cluster count by cutting the dendrogram at a COUNT rather than a
    distance, and it is **off by default because it was measured and it is worse**. 24 hina files,
    cap set to the TRUE headcount plus one: edit cost rose on **23 of 24**, median **+530**
    (range -37 to +1353), while tracks holding >=10 lines fell 7->4, 5->2, 7->3. A perfect prior
    with headroom still hurt, because the extra tracks are not noise -- they are guests, in-game
    character voices and background speech -- and squeezing them out does not remove the sound, it
    lands it in somebody's track.

    ⚠ **What is refuted is this stopping rule, not the idea of a cap.** `maxclust` cuts from the
    top of the tree, so what it merges first is whatever is CLOSEST -- two real people who sound
    alike -- while an outlying junk cluster (game audio, background) is merged last and survives
    to occupy one of the places the cap allows. The measured shape is exactly that: real people
    collapsed, junk kept. A rule that instead keeps the tracks that SPEAK MOST and reassigns the
    rest would fail differently, and has not been tried here. Kept as an explicit knob for a
    caller who knows better; never a default. See `docs/diarization-findings.md` 8.

    ⚠ The fourth element exists because the first three do not line up: `hard` is renumbered by
    `np.unique`, which gives the unplaceable-speaker sentinel an id of its own, so track ids and
    centroid indices differ whenever that happens. Index `soft`/`centroids` through it, never by
    track id.
    """
    import numpy as np
    from scipy.cluster.hierarchy import fcluster, linkage
    from scipy.spatial.distance import cdist
    from scipy.special import softmax

    train, _, _ = filter_embeddings(embeddings, segmentations)
    chunks, speakers, dimension = embeddings.shape
    if train.shape[0] < 2:  # noqa: PLR2004 - the fallback below returns the same 4-tuple
        # Silence, or one speaker with nothing to separate from. Everyone lands in cluster 0.
        # ⚠ An EMPTY `train` is reachable (audio with no clean single-speaker frames at all), and
        # `np.mean` of nothing is NaN plus a RuntimeWarning -- so the centroid is shaped, not
        # computed. Nothing downstream reads it in this branch, but a NaN handed back as if it
        # were an answer is how the next caller gets a surprise.
        hard = np.zeros((chunks, speakers), dtype=np.int8)
        soft = np.ones((chunks, speakers, 1))
        centroid = (np.mean(train, axis=0, keepdims=True) if train.shape[0]
                    else np.zeros((0, dimension), dtype=embeddings.dtype))
        # Nothing to compare against: one component IS the solution here.
        return ClusterResult(hard, soft, centroid, np.zeros(1, dtype=np.int16), 0.0,
                             int(train.shape[0]),
                             centroid_weights=np.full(centroid.shape[0],
                                                      float(train.shape[0])))

    normed = train / np.linalg.norm(train, axis=1, keepdims=True)
    dendrogram = linkage(normed, method="centroid", metric="euclidean")
    ahc = fcluster(dendrogram, ahc_threshold, criterion=AHC_CRITERION) - 1
    if max_speakers is not None and len(np.unique(ahc)) > max_speakers:
        # ⚠ The SAME dendrogram, cut at a count instead of a distance: a different stopping
        # rule on one tree, not a different algorithm. The distance cut runs first and only the
        # count overrides it, so a file that already has few enough clusters is untouched.
        # ⚠ `maxclust` merges the closest pairs first, which is why it takes real people before
        # it takes outliers -- see the measurement in the docstring before reaching for this.
        ahc = fcluster(dendrogram, max_speakers, criterion="maxclust") - 1
    _, ahc = np.unique(ahc, return_inverse=True)

    xvec_tf, plda_tf, plda_psi = load_plda(plda_dir)
    features = plda_tf(xvec_tf(train), lda_dim=lda_dim)
    phi = plda_psi[:lda_dim]

    initial = np.zeros((len(ahc), int(ahc.max()) + 1))
    initial[range(len(ahc)), ahc.astype(int)] = 1.0
    initial = softmax(initial * INIT_SMOOTHING, axis=1)
    gamma, weights, elbo = vbx(features, phi, fa=fa, fb=fb, gamma=initial, max_iters=max_iters)

    # ⚠ The K=1 arm has nothing to converge: with one component gamma is identically 1, so its
    # ELBO is settled after the first pass and the extra iterations change nothing. `epsilon=-inf`
    # says that explicitly rather than leaning on a stopping rule to decide it. (The K-component
    # arm does stop early sometimes; that asymmetry is harmless for the same reason. What would
    # NOT be harmless is capping the K arm lower -- MAX_ITERS already fails to converge on six of
    # seven files, and the arm with more to gain from iterations is that one.)
    _, _, elbo_one = vbx(features, phi, fa=fa, fb=fb,
                         gamma=np.ones((len(features), 1)), max_iters=max_iters, epsilon=-np.inf)

    # No division: the distance below is cosine, so the centroid's scale does not matter HERE.
    # ⚠ It matters to anything that applies a transform with a translation in it -- see
    # `mean_centroids`, and `centroid_weights` below, which is the divisor it needs.
    kept = weights > 1e-7
    centroids = gamma[:, kept].T @ train.reshape(-1, dimension)
    distance = cdist(embeddings.reshape(-1, dimension), centroids, metric="cosine")
    soft = (2 - distance).reshape(chunks, speakers, -1)

    hard = constrained_argmax(soft)
    # ⚠ The renumbering gives an id to the `-2` sentinel too when some local speaker could not be
    # placed, so a track id is NOT a centroid index. Faithful to the original (`clustering.py`,
    # "re-number clusters from 0"), but a consumer that indexes `soft` or `centroids` by track id
    # is silently reading the wrong column. `centroid_of_track` is that mapping, with -1 where a
    # track has no centroid at all.
    labels, hard = np.unique(hard, return_inverse=True)
    centroid_of_track = np.array([-1 if label < 0 else label for label in labels], dtype=np.int16)
    return ClusterResult(hard.reshape(chunks, speakers), soft, centroids, centroid_of_track,
                         float(elbo - elbo_one), int(len(features)),
                         centroid_weights=gamma[:, kept].sum(axis=0))
