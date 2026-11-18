"""Speaker diarization: acoustic timeline from a vocal track.

Three backends, none importing its upstream framework: the default, Nemotron-3 + vbx,
is plain torch over NVIDIA's weights (`nemotron3_model.py`); DiariZen and Sortformer v2.1,
kept for research, run exported TorchScript -- see `tools/sortformer_export/` and
`tools/diarizen_export/` for how those artifacts are produced,
`backend.py` for the seam between them, and `docs/plans/speaker-diarization-plan.md`
(C1) for why.
"""
