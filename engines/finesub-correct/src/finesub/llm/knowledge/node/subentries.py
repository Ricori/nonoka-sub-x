"""Sub-entries: one section of an entry split off into an entry of its own
(plan kb-entry-size, step 3).

The relation is a ``part_of`` link, child -> parent -- not a pointer line in
the parent, which would be prose no injection or index could use. The child's
name is derived from the parent's and the section's, `母条目·节名`, so the
model never names it and the spelling is uniform. One level only: a child is
never split again.
"""

from __future__ import annotations

from .store import KnowledgeStore

PART_OF = "part_of"
SUBENTRY_SEPARATOR = "·"


def derived_name(parent_surface: str, section: str) -> str:
    return f"{parent_surface}{SUBENTRY_SEPARATOR}{section}"


def parent_of(store: KnowledgeStore, subject_id: str, rev: int | None = None) -> str | None:
    """The live parent entry this one is part of, if any."""

    for link in store.links_from(subject_id, rev):
        if link.rel == PART_OF and store.node(link.target_id, rev) is not None:
            return link.target_id
    return None


def children_of(store: KnowledgeStore, subject_id: str, rev: int | None = None) -> list[str]:
    """The live sub-entries of this entry, in name order."""

    out = []
    for link in store.links_to(subject_id, rev):
        if link.rel != PART_OF:
            continue
        child = store.node(link.source_id, rev)
        if child is not None:
            out.append((str(child.payload.get("surface") or ""), link.source_id))
    return [sid for _surface, sid in sorted(out)]


def child_holding(store: KnowledgeStore, parent_id: str, name: str, rev: int | None = None) -> str | None:
    """The sub-entry of ``parent_id`` holding a line called ``name`` (a term's
    source form or Chinese name, a note's label), if any. A feedback hint
    names the entry AND the line (`KnowledgeHint.sub`); once that line's
    section is split off, the update has to fetch the sub-entry, or it could
    never touch the line again (plan kb-entry-size)."""

    from ..base import _match_normalize

    needle = _match_normalize(name)
    if not needle:
        return None
    for child_id in children_of(store, parent_id, rev):
        for membership in store.children(child_id, rev):
            line = store.node(membership.child_id, rev)
            if line is None:
                continue
            payload = line.payload
            names = (payload.get("surface"), payload.get("zh"), payload.get("label"))
            if needle in {_match_normalize(str(value)) for value in names if value}:
                return child_id
    return None
