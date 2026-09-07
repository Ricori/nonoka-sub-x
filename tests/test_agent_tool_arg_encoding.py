"""What reaches the harness when a CLI escapes the tool arguments it forwards.

antigravity-cli 1.1.24 cannot put a non-ASCII character into a `tools/call`
argument: it spells every one of them as a literal six-character escape inside
the JSON string, so the harness MCP server decodes the frame and holds the
escape text rather than the text. Nothing downstream of `submit` could tell the
two apart -- the run measured on 2026-09-04 validated, wrote and delivered a
whole window of subtitles as escapes, and reported success.

Upstream 0.5.1 took this over from `0009-agent-tool-arg-unicode-escapes.patch`
and made it **two layers**, which is why this file outlived the patch:

* recovery, at the frame boundary (`agent_mcp_server._unescaped_arguments`) --
  the run still succeeds, and every repair says so on stderr, because the
  artifact records the repaired text and that line is the only evidence an
  edit happened;
* refusal, at the exit (`output_protocol.validate_translated_csv_text`) -- an
  escaped reply is a transport fault and can never become a subtitle, even if
  the repair misses a shape it has not seen.

Both read the same predicate, `finesub.text.looks_escaped`, and it judges
**whole frames**: the fault escapes everything a CLI sends, while a single
field can hold two CJK characters and nothing else, so a per-field verdict
would repair some cells of a reply and leave others escaped.
"""

from __future__ import annotations

import io
import sys
import unittest
import unittest.mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "third_party/finesub/src"))

from finesub.llm.agent.agent_mcp_server import (  # noqa: E402
    HarnessToolServer,
    _unescaped_arguments,
)
from finesub.llm.output_protocol import validate_translated_csv_text  # noqa: E402
from finesub.text import looks_escaped, unescape_codepoints  # noqa: E402


BACKSLASH = "\\"


def _escaped(text: str) -> str:
    """``text`` as the CLI hands it over: every non-ASCII character escaped."""

    escaped = []
    for character in text:
        if character.isascii():
            escaped.append(character)
            continue
        # UTF-16 code units, so a character outside the BMP is two escapes.
        units = character.encode("utf-16-be")
        escaped.extend(
            BACKSLASH + "u%02x%02x" % (units[index], units[index + 1])
            for index in range(0, len(units), 2)
        )
    return "".join(escaped)


class EscapePredicateTests(unittest.TestCase):
    """`looks_escaped` decides; `unescape_codepoints` only carries it out."""

    def test_a_mangled_reply_is_recognised_and_restored(self) -> None:
        answer = "sub|1|伝える言葉は|想说的话早已想好|high|10|"
        mangled = _escaped(answer)
        self.assertTrue(looks_escaped(mangled))
        self.assertEqual(unescape_codepoints(mangled), answer)

    def test_astral_characters_survive_the_surrogate_pair(self) -> None:
        self.assertEqual(unescape_codepoints(_escaped("🌞")), "🌞")

    def test_text_no_cli_mangled_is_left_alone(self) -> None:
        for value in (
            "sub|1|伝える言葉は|想说的话早已想好|high|10|",  # a driver that behaves
            "plain ascii payload",
            "a " + BACKSLASH + "u0041 b",  # decodes to ASCII: the model meant it
            "C:" + BACKSLASH + "users",  # a backslash that is not an escape
        ):
            with self.subTest(value=value):
                self.assertFalse(looks_escaped(value))
                self.assertEqual(unescape_codepoints(value), value)

    def test_a_lone_surrogate_is_convicted_but_not_rewritten(self) -> None:
        """The two halves answer separately, and here they disagree on purpose.

        `\\ud83d` is above U+007F, so the predicate says the frame is escaped --
        it cannot know the pair is broken. The decoder is where that shows up,
        and it hands the string back rather than emit an unpaired surrogate.
        """

        value = BACKSLASH + "ud83d alone"
        self.assertTrue(looks_escaped(value))
        self.assertEqual(unescape_codepoints(value), value)


class FrameRepairTests(unittest.TestCase):
    def test_every_string_in_the_arguments_is_repaired(self) -> None:
        arguments = {
            "payload": _escaped("纠错终稿"),
            "offset": 0,
            "queries": [_escaped("夏影 主题曲"), "ascii"],
        }
        self.assertEqual(
            _unescaped_arguments(arguments),
            {"payload": "纠错终稿", "offset": 0, "queries": ["夏影 主题曲", "ascii"]},
        )

    def test_the_verdict_is_taken_over_the_whole_frame(self) -> None:
        """A field too small to convict on its own still gets repaired.

        Two escapes in a mostly-English note is under any per-field bar worth
        having, and the CLI that produced it escaped the payload beside it as
        well. Judging the frame is what keeps one reply from arriving half
        repaired.
        """

        arguments = {
            "payload": _escaped("那句台词其实是「夏影」的副歌"),
            "note": _escaped("kept 原文 here"),
        }
        self.assertEqual(
            _unescaped_arguments(arguments),
            {"payload": "那句台词其实是「夏影」的副歌", "note": "kept 原文 here"},
        )

    def test_an_intact_frame_is_handed_on_untouched(self) -> None:
        arguments = {"payload": "sub|1|伝える|想说的话|high|10|", "offset": 0}
        self.assertEqual(_unescaped_arguments(arguments), arguments)

    def test_a_repair_is_never_silent(self) -> None:
        """The artifact keeps the repaired text, so this line is the only record.

        It carries the count as well, which is what separates a whole corrupted
        window from a single ambiguous edit.
        """

        stderr = io.StringIO()
        with unittest.mock.patch.object(sys, "stderr", stderr):
            _unescaped_arguments({"payload": _escaped("终稿")})
        self.assertIn("escaped", stderr.getvalue())
        self.assertIn("2", stderr.getvalue())

    def test_the_repair_happens_before_the_call_is_dispatched(self) -> None:
        # The fingerprint, the dedup and every handler have to see the argument
        # the model wrote, so the repair belongs to `handle`, not to `submit`.
        seen: dict[str, object] = {}

        class RecordingServer(HarnessToolServer):
            def __init__(self) -> None:  # the runtime is never reached
                pass

            def _call(self, name, arguments, *, rpc_id):  # type: ignore[override]
                seen["name"] = name
                seen["arguments"] = dict(arguments)
                return {"status": "accepted"}

        RecordingServer().handle(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "submit", "arguments": {"payload": _escaped("终稿")}},
            }
        )
        self.assertEqual(seen, {"name": "submit", "arguments": {"payload": "终稿"}})


class ExitRefusalTests(unittest.TestCase):
    """The layer the patch never had, and the one that makes delivery safe.

    The repair is a recovery for one known CLI; this is the invariant. A reply
    that arrives escaped is rejected before coverage, adjacency, scoring or the
    `char_count` normalization can read it -- that last one is what saw the real
    incident and normalized the evidence away.
    """

    def test_an_escaped_reply_cannot_become_a_subtitle(self) -> None:
        row = _escaped("sub|1|伝える|想说的话|high|10|")
        reply = "<translated>\n" + row + "\n</translated>"
        result = validate_translated_csv_text(reply, [])
        self.assertFalse(result.ok)
        self.assertEqual(result.segments, [])
        self.assertTrue(result.errors)

    def test_an_intact_reply_is_not_refused_for_transport(self) -> None:
        """Whatever else fails, it must not fail *as a transport fault*."""

        reply = "<translated>\nsub|1|伝える|想说的话|high|10|\n</translated>"
        result = validate_translated_csv_text(reply, [])
        self.assertNotIn(
            "transport fault",
            " ".join(result.errors),
            "an honest reply was read as escaped",
        )


if __name__ == "__main__":
    unittest.main()
