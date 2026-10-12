"""Human-version marks on the human projection (owner 2026-10-01).

The model may rewrite or retire any line, including one a human wrote; the
price is paid in visibility. Wherever a line's current value differs from
the latest value a human ENDORSED, the human projection (``entry_text``:
``rendered/`` and ``show``) prints that endorsed value in an HTML comment
under the line -- or, for a retired line, in its old section. The prompt
projections never see it.

A line is one child node of the subject, valued as the text the human
projection shows for it (``format_line`` with its alias column), trimmed.
That is the value a person can see and can restore by copying it back; the
fields the line does not render (misheard items, archive residue) could be
neither. The entry HEAD -- H1 (``surface``), intro and the subject's own
alias items -- is one more unit of the same kind, shaped like a term line
(name, description, alias column; owner 2026-10-01). Only those three count:
``updated_date`` moves on every write, and ``category``, the recategorize
reason, ``section_order`` and the derived retrieval fields are not what the
head shows.

"Endorsed" is decided by VALUE, never by revision kind alone:

* a ``kind=user`` revision (edit, CLI, ``rendered/`` harvest) at which the
  line's value actually changed -- a user revision that only flips
  visibility (``share mark``) changes no value and endorses nothing;
* an ack (``line_acks``): the human accepts the current value as is.

Everything else -- harness, pull, share, import, revert, restore -- is the
non-human side. A mark never clears by itself: only a newer endorsement, or
the current value coming back to the endorsed one, makes it go away.

Known cost, not addressed: `refresh_rendered` renders every entry on every
refresh, and each `entry_text` runs `line_marks` -- a handful of queries plus
a walk of each line's version history -- even for entries nothing touched.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from ..base import join_names
from .model import NodeVersion
from .presets import preset_for_category
from .render import format_line, line_label, missing_core_slots, ordered_section_names
from .store import KnowledgeStore

#: The comment opener every mark starts with; one place, so the tests and the
#: docs point at the same text.
EDITED_LABEL = "人改版"
ACKED_LABEL = "人认可版"


@dataclass(frozen=True)
class LineMark:
    node_id: str
    #: The revision the endorsement belongs to (the user revision, or the
    #: store rev an ack was taken at).
    rev: int
    #: The endorsed value: the JSON `_line_value` / `_head_value` builds.
    value: str
    #: True when an ack, not an edit, produced the endorsement.
    acked: bool
    #: Where the line sits (live) or sat last (gone).
    section: str
    order_key: int
    #: What the line reads now (same shape); None when it is gone.
    current: str | None = None
    #: The entry head (H1 + intro + entry aliases) rather than a line.
    head: bool = False
    #: H1 of the entry this line was moved here from (a cross-entry
    #: `move_line`, e.g. before a merge), "" when it was always here.
    moved_from: str = ""

    @property
    def gone(self) -> bool:
        return self.current is None

    @property
    def line_id(self) -> str:
        """What ``knowledge ack <entry> <line>`` takes: a short prefix of the
        node id, printed in the mark itself (the human projection carries no
        other handle)."""

        return self.node_id[:8]


@dataclass
class LineMarks:
    live: dict[str, LineMark] = field(default_factory=dict)
    gone: dict[str, list[LineMark]] = field(default_factory=dict)
    head: LineMark | None = None

    def all(self) -> list[LineMark]:
        out = [self.head] if self.head is not None else []
        out.extend(self.live.values())
        for marks in self.gone.values():
            out.extend(marks)
        return out

    def __bool__(self) -> bool:
        return bool(self.live or self.gone or self.head)


# ---- value history ---------------------------------------------------------------


def _live(row: Any, at: int) -> bool:
    return row["valid_from_rev"] <= at and (row["valid_to_rev"] is None or row["valid_to_rev"] > at)


class _LineHistory:
    """One node's version rows plus its alias item rows, answering "what did
    this line read at rev r" without a query per rev."""

    def __init__(
        self,
        kind: str,
        node_rows: list[Any],
        alias_rows: list[Any],
        aliases_shown: Callable[[dict[str, Any], int], bool] | None = None,
    ) -> None:
        self.kind = kind
        self.node_rows = node_rows
        self.alias_rows = alias_rows
        #: Head only: does the human projection render the entry's alias slot
        #: at this rev? When it does not, the aliases are no part of the value.
        self.aliases_shown = aliases_shown

    def change_revs(self, at: int) -> list[int]:
        revs: set[int] = set()
        for row in (*self.node_rows, *self.alias_rows):
            revs.add(row["valid_from_rev"])
            if row["valid_to_rev"] is not None and row["valid_to_rev"] <= at:
                revs.add(row["valid_to_rev"])
        return sorted(revs)

    def value_at(self, rev: int) -> str | None:
        row = next((r for r in self.node_rows if _live(r, rev)), None)
        if row is None or row["maturity"] == "tentative":
            return None
        payload = json.loads(row["payload"])
        aliases = [
            item["value"]
            for item in self.alias_rows
            if _live(item, rev) and item["maturity"] != "tentative"
        ]
        if self.kind == "subject":
            shown = self.aliases_shown is None or self.aliases_shown(payload, rev)
            return _head_value(payload, aliases if shown else None)
        node = NodeVersion(
            local_id=row["local_id"], kind=self.kind, valid_from_rev=row["valid_from_rev"],
            valid_to_rev=row["valid_to_rev"], payload=payload,
        )
        return _line_value(node, aliases if self.kind == "term" else [])


def _alias_list(aliases: list[str]) -> list[str]:
    return [alias for alias in dict.fromkeys(a.strip() for a in aliases) if alias]


def _line_value(node: NodeVersion, aliases: list[str]) -> str:
    """A line's value, kept STRUCTURED so the alias column can be compared as
    a set without parsing the rendered line back (a description may hold
    pipes): ``line`` is the trimmed text the projection shows, ``base`` the
    same line with an empty alias column, ``aliases`` the column's items.
    Canonical JSON, so equal values are equal strings."""

    aliases = _alias_list(aliases)
    return json.dumps(
        {
            "line": format_line(node, aliases=aliases).strip(),
            "base": format_line(node, aliases=[]).strip(),
            "aliases": aliases,
        },
        ensure_ascii=False, sort_keys=True,
    )


def _head_value(payload: dict[str, Any], aliases: list[str] | None) -> str:
    """The head's value: H1, intro and the entry aliases, each trimmed, as
    canonical JSON -- an exact key, whatever characters the parts hold.
    ``aliases`` is None while the projection does not show the alias slot:
    the aliases are then no part of what a person sees or can restore.

    ⚠ Known corner: whether the alias slot shows (see `_alias_slot_probe`)
    is decided at the head's own change revs only, and a flip of it -- a
    line in 档案 gaining or losing the 别名 label -- is not one. So for an
    entry whose 档案 holds an old imported line labelled 别名, the flip
    surfaces at the head's next change, not when it happens."""

    return json.dumps(
        {
            "surface": str(payload.get("surface") or "").strip(),
            "intro": str(payload.get("intro") or "").strip(),
            "aliases": None if aliases is None else _alias_list(aliases),
        },
        ensure_ascii=False, sort_keys=True,
    )


def covers(current: str | None, endorsed: str) -> bool:
    """Does the current value still hold everything the human endorsed?

    The alias part compares as a SET and only for loss (owner 2026-10-01):
    the model appending an alias, or the order changing, is no departure
    from the human's version; dropping or rewriting one of theirs is.
    Everything else -- the line without its alias column, the H1, the
    intro -- compares exactly (both sides are already trimmed)."""

    if current is None:
        return False
    now, then = json.loads(current), json.loads(endorsed)
    rest = lambda value: {k: v for k, v in value.items() if k not in ("aliases", "line")}  # noqa: E731
    if rest(now) != rest(then):
        return False
    if now["aliases"] is None or then["aliases"] is None:
        return True  # a head whose alias slot is (or was) not shown: nothing to compare
    return set(then["aliases"]) <= set(now["aliases"])


def describe(mark: LineMark, value: str | None) -> str:
    """A mark's value (or its current value) as a person reads it: the line
    as rendered, or the head's three parts."""

    if value is None:
        return ""
    data = json.loads(value)
    if not mark.head:
        return data["line"]
    shown = f"H1「{data['surface']}」；简介「{data['intro']}」"
    if data["aliases"] is not None:
        shown += f"；别名「{join_names(data['aliases'])}」"
    return shown


def line_marks(store: KnowledgeStore, subject_id: str, rev: int | None = None) -> LineMarks:
    """The marks one subject's human projection carries at ``rev``."""

    at = store.current_rev() if rev is None else int(rev)
    conn = store.conn
    places: dict[str, tuple[str, int, bool, int | None]] = {}
    membership_of: dict[str, str] = {}
    member_rows = conn.execute(
        "SELECT membership_id, child_id, section, order_key, valid_from_rev, valid_to_rev"
        " FROM membership_versions WHERE parent_id=? AND valid_from_rev <= ?"
        " ORDER BY valid_from_rev",
        (subject_id, at),
    ).fetchall()
    for row in member_rows:
        # the latest placement wins: a line moved between sections is shown
        # (or remembered) where it was last
        places[row["child_id"]] = (
            row["section"], row["order_key"], _live(row, at), row["valid_to_rev"],
        )
        membership_of[row["child_id"]] = row["membership_id"]
    marks = LineMarks()
    # the subject's children ever, plus the subject itself (its head)
    units = "SELECT child_id FROM membership_versions WHERE parent_id=? UNION SELECT ?"
    kinds: dict[str, str] = {}
    node_rows: dict[str, list[Any]] = {}
    for row in conn.execute(
        "SELECT v.*, n.kind AS kind FROM node_versions v JOIN nodes n USING(local_id)"
        f" WHERE v.local_id IN ({units}) AND v.valid_from_rev <= ? ORDER BY v.valid_from_rev",
        (subject_id, subject_id, at),
    ):
        kinds[row["local_id"]] = row["kind"]
        node_rows.setdefault(row["local_id"], []).append(row)
    alias_rows: dict[str, list[Any]] = {}
    for row in conn.execute(
        "SELECT v.*, i.local_id AS local_id FROM item_versions v JOIN items i USING(item_id)"
        f" WHERE i.local_id IN ({units}) AND i.field='aliases' AND v.valid_from_rev <= ?"
        " ORDER BY i.rowid, v.valid_from_rev",
        (subject_id, subject_id, at),
    ):
        alias_rows.setdefault(row["local_id"], []).append(row)
    user_revs = {
        row["rev"]
        for row in conn.execute("SELECT rev FROM revisions WHERE kind='user' AND rev <= ?", (at,))
    }
    acks: dict[str, list[Any]] = {}
    for row in conn.execute(
        f"SELECT * FROM line_acks WHERE node_id IN ({units}) AND rev <= ?"
        " ORDER BY rev, ack_id",
        (subject_id, subject_id, at),
    ):
        acks.setdefault(row["node_id"], []).append(row)
    part_of_rows = conn.execute(
        "SELECT valid_from_rev, valid_to_rev FROM link_versions"
        " WHERE source_id=? AND rel='part_of' AND valid_from_rev <= ?",
        (subject_id, at),
    ).fetchall()
    if subject_id in kinds:
        head = _LineHistory(
            "subject", node_rows[subject_id], alias_rows.get(subject_id, []),
            aliases_shown=_alias_slot_probe(member_rows, node_rows, part_of_rows),
        )
        endorsed = _endorsed(head, at, user_revs, acks.get(subject_id, []))
        current = head.value_at(at)
        if (
            endorsed is not None
            and endorsed[1] is not None
            and current is not None
            and not covers(current, endorsed[1])
        ):
            marks.head = LineMark(
                node_id=subject_id, rev=endorsed[0], value=endorsed[1], acked=endorsed[2],
                section="", order_key=0, current=current, head=True,
            )
    for node_id, (section, order_key, placed_here, closed_at) in places.items():
        if node_id not in kinds:
            continue
        history = _LineHistory(kinds[node_id], node_rows[node_id], alias_rows.get(node_id, []))
        endorsed = _endorsed(history, at, user_revs, acks.get(node_id, []))
        if endorsed is None or endorsed[1] is None:
            # never endorsed, or the human's last word was "this line goes"
            continue
        endorsed_rev, endorsed_value, acked = endorsed
        current = history.value_at(at)
        if current is not None and not placed_here:
            continue  # still live, but under another parent now: shown there
        if covers(current, endorsed_value):
            continue
        if current is None:
            node_live = any(row["valid_to_rev"] is None or row["valid_to_rev"] > at for row in history.node_rows)
            if node_live:
                continue  # tentative again (shadow-only): not a retirement
            retired_at = max(row["valid_to_rev"] for row in history.node_rows)
            if closed_at is not None and closed_at != retired_at:
                # It left this entry before it was retired (moved to another
                # one), so the retirement happened elsewhere. Every parent it
                # still hung under when retired closes with it, so a
                # multi-parent line is remembered in each of them -- and so is
                # one whose membership here was never closed at all (a pull
                # that retires an upstream node only tombstones the node).
                continue
        mark = LineMark(
            node_id=node_id, rev=endorsed_rev, value=endorsed_value, acked=acked,
            section=section, order_key=order_key, current=current,
            moved_from=_moved_from(store, membership_of[node_id], subject_id, at),
        )
        if mark.gone:
            marks.gone.setdefault(section, []).append(mark)
        else:
            marks.live[node_id] = mark
    for gone in marks.gone.values():
        gone.sort(key=lambda mark: mark.order_key)
    return marks


def _moved_from(store: KnowledgeStore, membership_id: str, subject_id: str, at: int) -> str:
    """H1 of the entry this placement hung under before it was moved here,
    or "". Only the SAME membership counts: a line that also sits under
    another entry (multi-parent) was not moved from it."""

    row = store.conn.execute(
        "SELECT parent_id FROM membership_versions WHERE membership_id=? AND parent_id<>?"
        " AND valid_from_rev<=? ORDER BY valid_from_rev DESC LIMIT 1",
        (membership_id, subject_id, at),
    ).fetchone()
    if row is None:
        return ""
    # the source entry is usually retired by now (a merge), so read its last
    # version at or before `at`, live or not
    version = store.conn.execute(
        "SELECT payload FROM node_versions WHERE local_id=? AND valid_from_rev<=?"
        " ORDER BY valid_from_rev DESC LIMIT 1",
        (row["parent_id"], at),
    ).fetchone()
    if version is None:
        return ""
    return str(json.loads(version["payload"]).get("surface") or "").strip()


def _alias_slot_probe(
    member_rows: list[Any], node_rows: dict[str, list[Any]], part_of_rows: list[Any] = ()
) -> Callable[[dict[str, Any], int], bool]:
    """Whether the head's alias slot renders at a rev. The rules are the
    renderer's own (`ordered_section_names`, `missing_core_slots`); this
    only works out, from rows in memory, which labels each section's
    rendered lines carry -- rendering the entry once per change rev would
    cost a full render each time. A rendered line is what
    ``load_subject_tree`` keeps: a live membership to a live, non-tentative
    node. Same known corner as `_head_value`."""

    def shown(payload: dict[str, Any], rev: int) -> bool:
        if any(_live(row, rev) for row in part_of_rows):
            return False  # a sub-entry at that rev: identity slot only (`missing_core_slots`)
        preset = preset_for_category(payload.get("category") or "common")
        role = preset.label_by_role("aliases")
        if role is None:
            return False
        alias_spec = preset.section(role[0])
        used: dict[str, set[str]] = {}
        for member in member_rows:
            if not _live(member, rev):
                continue
            child = next((r for r in node_rows.get(member["child_id"], ()) if _live(r, rev)), None)
            if child is None or child["maturity"] == "tentative":
                continue
            used.setdefault(member["section"], set()).add(line_label(json.loads(child["payload"])))
        for name in ordered_section_names(payload, preset, used):
            spec = preset.section(name)
            if spec is not alias_spec or spec is None:
                continue
            if any(label.role == "aliases" for label in missing_core_slots(spec, used.get(name, ()))):
                return True
        return False

    return shown


def _endorsed(
    history: _LineHistory, at: int, user_revs: set[int], acks: list[Any]
) -> tuple[int, str | None, bool] | None:
    """``(rev, value, acked)`` of the latest endorsement, or None.

    Walks the line's value changes in rev order; an ack taken at store rev
    ``a`` comes after every revision up to ``a``. What an ack endorses is
    the line's value AT ``a`` -- the head of the store when it was taken --
    so it is recomputed from the history, never read back from
    ``line_acks.value``: that column is a record, and its shape is whatever
    the value format was when the ack was written (a plain line, a head
    triple) -- reading it would tie every old ack to today's format."""

    events: list[tuple[int, int, int, Any]] = [(r, 0, 0, None) for r in history.change_revs(at)]
    events.extend((row["rev"], 1, row["ack_id"], row) for row in acks)
    events.sort(key=lambda event: event[:3])
    previous: str | None = None
    endorsed: tuple[int, str | None, bool] | None = None
    for rev, order, _ack_id, ack in events:
        if order == 1:
            endorsed = (rev, history.value_at(rev), True)
            continue
        value = history.value_at(rev)
        if rev in user_revs and value != previous:
            endorsed = (rev, value, False)
        previous = value
    return endorsed


# ---- the comment ---------------------------------------------------------------------

#: What would end an HTML comment early: ``-->``, and ``--!>`` which HTML
#: parsers also accept as a close.
_COMMENT_CLOSE_RE = re.compile(r"--(!?)>")
ESCAPE_NOTE = "值里 > 前的反斜杠是转义，抄回正文时删掉"


def escape_comment_text(text: str) -> str:
    r"""``-->`` becomes ``--\>`` (and ``--!>`` becomes ``--!\>``), so the
    value cannot close the comment it sits in -- a markdown viewer would
    otherwise show the rest as body text.

    ⚠ Known corner, accepted: the escape is not itself escaped, so a value
    that already holds a literal ``--\>`` AND a real ``-->`` reads the same
    after escaping, and "drop the backslash" restores both as ``-->``."""

    return _COMMENT_CLOSE_RE.sub(lambda m: f"--{m.group(1)}\\>", text)


def marker_comment(mark: LineMark, *, name_section: bool = False) -> str:
    """One line: ``<!-- 人改版 r41（行 3f2a9c1b）：<value> -->``; the head's
    reads ``（条目头 …）：H1「…」；简介「…」；别名「…」``.

    The harvest drops whole-line comments, so a mark is never read back as
    an edit; to restore the endorsed value, copy it back as a line (the
    head's three parts back into the H1, the intro and the 别名 row).
    ``name_section`` is for a retired line whose section is not rendered any
    more, so its comment cannot sit in that section."""

    label = ACKED_LABEL if mark.acked else EDITED_LABEL
    notes = [f"{'条目头' if mark.head else '行'} {mark.line_id}"]
    if mark.moved_from:
        notes.append(f"迁自「{escape_comment_text(mark.moved_from)}」")
    if mark.gone:
        notes.append("已被删除")
        if name_section and mark.section:
            notes.append(f"原在「{mark.section}」")
    shown = describe(mark, mark.value)
    value = escape_comment_text(shown)
    if value != shown:
        notes.append(ESCAPE_NOTE)
    return f"<!-- {label} r{mark.rev}（{'，'.join(notes)}）：{value} -->"


# ---- ack -----------------------------------------------------------------------------


class AckError(LookupError):
    pass


def ack_lines(store: KnowledgeStore, subject_id: str, line: str | None = None) -> list[LineMark]:
    """Record the current value of marked lines as endorsed by a human.

    ``line`` is the id a mark prints (any unique prefix of the node id);
    None acks every marked line of the subject. Only marked lines can be
    acked: an unmarked line already shows what the human last endorsed, or
    was never theirs to begin with. Returns the marks that were cleared."""

    marks = line_marks(store, subject_id).all()
    if line is not None:
        token = line.strip()
        chosen = [mark for mark in marks if token and mark.node_id.startswith(token)]
        if not chosen:
            raise AckError(f"该条目里没有带注记、行号以 {token!r} 开头的行")
        if len(chosen) > 1:
            ids = "、".join(mark.line_id for mark in chosen)
            raise AckError(f"行号 {token!r} 对应多行（{ids}），请写长一些")
        marks = chosen
    at = store.current_rev()
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if not marks:
        return []
    store.conn.execute("BEGIN IMMEDIATE")
    try:
        for mark in marks:
            store.conn.execute(
                "INSERT INTO line_acks(node_id, rev, value, created_at) VALUES (?, ?, ?, ?)",
                (mark.node_id, at, mark.current, created_at),
            )
        store.conn.execute("COMMIT")
    except BaseException:
        store.conn.execute("ROLLBACK")
        raise
    return marks

