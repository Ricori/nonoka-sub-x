"""Who the knowledge base says may be speaking: its streamer entries the material's title mentions.

Matched the way the correction windows and `--asr-context` already match -- index keys, other-language
names and aliases, as substrings of the URL/title/description/extra-info block -- so there is one
retrieval, not a second one for speakers. Fail-soft: no knowledge base, or nothing matched, is an
empty list.
"""

from __future__ import annotations

from typing import Any

#: How many knowledge entries the candidates may draw on: the cap the correction windows'
#: pre-injection uses, so the judge never sees more people than the correction did.
ROSTER_MAX_ENTRIES = 8


def roster_entries(knowledge_root: Any, text: str) -> list[tuple[str, tuple[str, ...], str]]:
    """`(key, other names, intro)` per matched streamer entry. `knowledge_root=None` means the run
    has knowledge switched off: no read at all."""
    if knowledge_root is None or not (text or "").strip():
        return []
    try:
        from ...knowledge.base import load_index_entries, match_index_keywords

        matches = [match for match in match_index_keywords(knowledge_root, text, max_entries=ROSTER_MAX_ENTRIES)
                   if match.category == "streamer"]
        if not matches:
            return []
        index = {entry.key: entry for entry in load_index_entries(knowledge_root, "streamer")}
    except Exception:  # noqa: BLE001 - a roster is a hint, never a reason to fail
        return []
    out = []
    for match in matches:
        entry = index.get(match.key)
        other = tuple(dict.fromkeys((*entry.native_names, *entry.aliases))) if entry else ()
        out.append((match.key, other, (entry.intro or "").strip() if entry else ""))
    return out
