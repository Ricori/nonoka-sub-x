"""A well-formed correction reply that is not an answer, the engine half.

Promises kept by `patches/finesub/0009-reject-placeholder-correction.patch`:
on 2026-10-13 an agy window submitted `text|text` on all 52 rows -- a probe of
the submit channel after its real answers were refused -- and the
structure-only checks accepted it as the window's subtitles. A reply whose
rows are all one line, or mostly a placeholder word, is now a repairable error.
"""

from __future__ import annotations

import unittest

from finesub.llm.chunking import SubtitleSegment
from finesub.llm.output_protocol import validate_correction_output_text
from finesub.llm.prompt_variants import resolve_variant

SOURCE = [
    SubtitleSegment(str(index), 2.0 * index, 2.0 * index + 1.5, text)
    for index, text in enumerate(
        ["うん知ってるよ", "どんなお芝居するか", "今日収録いったね", "発表がないから", "いつなのよ", "安心して"],
        1,
    )
]
TRANSLATIONS = ["嗯，我知道哦", "她会怎么演", "今天去录音了呢", "因为还没公布", "到底是什么时候嘛", "放心吧"]
HEADER = "type|position|duration|gap|corrected_text|translation|conf|char_count|note"


def _reply(rows: list[tuple[str, str]]) -> str:
    body = "\n".join(
        f"sub|{position}|1.5|0.5|{corrected}|{translation}|high|{len(translation)}|"
        for position, (corrected, translation) in enumerate(rows, 1)
    )
    return (
        f"<reasoning>\ntest\n</reasoning>\n<translated>\n{HEADER}\n{body}\n</translated>\n"
        "<next_advice>\n</next_advice>\n"
    )


def _validate(rows: list[tuple[str, str]], source: list[SubtitleSegment] = SOURCE):
    return validate_correction_output_text(_reply(rows), source, variant=resolve_variant(None))


class PlaceholderAnswerTests(unittest.TestCase):
    def test_a_real_answer_passes(self) -> None:
        result = _validate([(item.text, text) for item, text in zip(SOURCE, TRANSLATIONS)])
        self.assertTrue(result.ok, result.errors)

    def test_one_line_on_every_row_is_refused(self) -> None:
        result = _validate([("text", "text")] * len(SOURCE))
        self.assertFalse(result.ok)
        self.assertIn("same text", result.errors[0])

    def test_mostly_placeholder_words_are_refused(self) -> None:
        rows = [("test", "test"), ("TEXT", "text"), ("placeholder", "TODO"), ("todo", "tbd")]
        rows += [(item.text, text) for item, text in zip(SOURCE[4:], TRANSLATIONS[4:])]
        result = _validate(rows)
        self.assertFalse(result.ok)
        self.assertIn("placeholder", result.errors[0])

    def test_a_short_window_may_repeat_itself(self) -> None:
        source = [SubtitleSegment(str(index), 2.0 * index, 2.0 * index + 0.5, "うん") for index in (1, 2, 3)]
        result = _validate([("うん", "嗯")] * 3, source)
        self.assertTrue(result.ok, result.errors)

    def test_a_lone_placeholder_word_is_a_line_like_any_other(self) -> None:
        rows = [(item.text, text) for item, text in zip(SOURCE, TRANSLATIONS)]
        rows[2] = ("テスト", "test")
        result = _validate(rows)
        self.assertTrue(result.ok, result.errors)


if __name__ == "__main__":
    unittest.main()
