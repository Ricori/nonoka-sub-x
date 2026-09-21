"""A video's ASS style sheet lives inside its own document, and must survive.

Styles used to be one machine-global `styles.ass`. They now travel in the
document's `styles` field, which puts them in the path of two whitelists that
silently drop whatever they do not enumerate: `DocumentStore.save`, which builds
its update from named keyword arguments, and `DocumentStore.create`, which on a
re-projection copies only the fields it names forward from the current document.

Both losses are silent -- the save succeeds, the document just comes back
looking like the built-in defaults -- so they get a test each.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nonoka_x.document_store import DocumentStore
from nonoka_x.local_provider import ProviderError

SHEET = (
    "[V4+ Styles]\n"
    "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour,"
    " BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle,"
    " BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
    "Style: 优花,荆南波波黑,90,&H00D59B57,&H000000FF,&H00FFFFFF,&H00000000,"
    "0,0,0,0,100,100,0,0,1,5,0,2,10,10,30,1\n"
)


def _projection(**over):
    value = {
        "schema": 1,
        "title": "fixture.mp4",
        "source": "task",
        "fp": None,
        "subtitles": [{"t0": 0.0, "t1": 1.0, "ja": "こんにちは", "zh": "你好"}],
        "tracks": [],
        "track_meta": {
            "name": "默认轨",
            "ja": {"hidden": False, "style": "JP"},
            "zh": {"hidden": False, "style": "CN"},
        },
        "projection": {"schema": 1, "mode": "stable"},
    }
    value.update(over)
    return value


def test_a_saved_style_sheet_comes_back(tmp_path: Path) -> None:
    store = DocumentStore(tmp_path)
    created = store.create("vid", _projection())

    saved = store.save(
        "vid",
        expected_rev=created["rev"],
        subtitles=created["subtitles"],
        styles=SHEET,
    )
    assert saved["styles"] == SHEET
    assert store.read("vid")["styles"] == SHEET


def test_saving_without_styles_leaves_the_sheet_alone(tmp_path: Path) -> None:
    """Omitting the field is "no opinion", not "clear it".

    The editor only sends `styles` once the document has one; an older client --
    or a plugin that round-trips a document it read before the field existed --
    must not wipe the sheet just by saving subtitles.
    """

    store = DocumentStore(tmp_path)
    created = store.create("vid", _projection())
    store.save("vid", expected_rev=created["rev"], subtitles=created["subtitles"], styles=SHEET)

    after = store.save("vid", expected_rev=created["rev"] + 1, subtitles=[])
    assert after["styles"] == SHEET


def test_reprojection_keeps_the_style_sheet(tmp_path: Path) -> None:
    """Re-running a task replaces the subtitles, not how they look."""

    store = DocumentStore(tmp_path)
    created = store.create("vid", _projection())
    store.save("vid", expected_rev=created["rev"], subtitles=created["subtitles"], styles=SHEET)

    again = store.create(
        "vid",
        _projection(subtitles=[{"t0": 2.0, "t1": 3.0, "ja": "またね", "zh": "回见"}]),
        replace_default=True,
    )
    assert again["styles"] == SHEET
    assert again["subtitles"][0]["ja"] == "またね"


def test_a_first_projection_carries_whatever_it_was_given(tmp_path: Path) -> None:
    store = DocumentStore(tmp_path)
    created = store.create("vid", _projection(styles=SHEET))
    assert created["styles"] == SHEET
    assert store.read("vid")["styles"] == SHEET


def test_the_provider_rejects_a_sheet_that_is_not_text(tmp_path: Path) -> None:
    from nonoka_x import local_provider

    store = DocumentStore(tmp_path)
    created = store.create("vid", _projection())

    provider = local_provider.LocalProvider.__new__(local_provider.LocalProvider)
    provider.documents = store

    with pytest.raises(ProviderError):
        provider.save_document("vid", {"rev": created["rev"], "subtitles": [], "styles": 42})
    with pytest.raises(ProviderError):
        provider.save_document(
            "vid",
            {"rev": created["rev"], "subtitles": [], "styles": "x" * (1 << 21)},
        )

    saved = provider.save_document("vid", {"rev": created["rev"], "subtitles": [], "styles": SHEET})
    assert saved["styles"] == SHEET
