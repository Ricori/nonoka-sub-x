"""User-picked knowledge subjects, the engine half.

Promises kept by `patches/finesub/0010-pinned-knowledge-subjects.patch`: the
subjects a task names resolve by qualified name, ride into every correction
window as one budgeted block, and are pinned at the head of the knowledge
update's entry selection so the model can write into them.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from finesub.llm.knowledge.entries import EntrySelection, pin_style_entries, pin_subject_entries
from finesub.llm.knowledge.node.proposals import apply_model_proposals
from finesub.llm.knowledge.node.repo import KnowledgeRepo
from finesub.llm.knowledge.subjects import (
    MAX_KNOWLEDGE_SUBJECTS,
    SubjectSelectionError,
    render_subject_block,
    resolve_subject_keys,
)


def _create(repo: KnowledgeRepo, category: str, entry: str, intro: str) -> None:
    proposal = {
        "op": "create_entry",
        "category": category,
        "entry": entry,
        "intro": intro,
        "entry_type": "其他" if category == "common" else "",
        "aliases": [],
        "reason": "test fixture",
    }
    apply_model_proposals(
        "<knowledge_proposals>\n" + json.dumps(proposal, ensure_ascii=False) + "\n</knowledge_proposals>",
        repo=repo,
        task_id="fixture",
        knowledge_read_rev=repo.rev,
    )


class KnowledgeSubjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name) / "knowledge"
        repo = KnowledgeRepo.open(self.root)
        _create(repo, "streamer", "柊優花", "以游戏直播为主的主播")
        _create(repo, "common", "星之海", "一款像素风 RPG")

    def tearDown(self) -> None:
        KnowledgeRepo.forget(self.root)
        self._temp.cleanup()

    def test_names_resolve_qualified_and_bare(self) -> None:
        self.assertEqual(
            resolve_subject_keys(self.root, ["streamer/柊優花", "星之海", "星之海"]),
            [("streamer", "柊優花"), ("common", "星之海")],
        )

    def test_a_missing_subject_fails_loudly(self) -> None:
        with self.assertRaisesRegex(SubjectSelectionError, "没有这个条目"):
            resolve_subject_keys(self.root, ["不存在的主体"])
        with self.assertRaisesRegex(SubjectSelectionError, "最多"):
            resolve_subject_keys(self.root, [f"主体{i}" for i in range(MAX_KNOWLEDGE_SUBJECTS + 1)])

    def test_block_carries_every_subject_body(self) -> None:
        block = render_subject_block(
            self.root, ["streamer/柊優花", "common/星之海"], count_tokens=len
        )
        self.assertIn("<pinned_entries>", block)
        self.assertIn("# 柊優花", block)
        self.assertIn("# 星之海", block)
        self.assertLess(block.index("柊優花"), block.index("星之海"))
        self.assertEqual(render_subject_block(self.root, [], count_tokens=len), "")

    def test_update_selection_pins_subjects_after_style(self) -> None:
        ranked = [
            EntrySelection(category="common", key="别的条目", score=3.0, exists=True),
            EntrySelection(category="common", key="星之海", score=1.0, exists=True, applied=True),
        ]
        pinned = pin_style_entries(
            pin_subject_entries(ranked, [("streamer", "柊優花"), ("common", "星之海")]),
            ["某字幕组"],
        )
        self.assertEqual(
            [(s.category, s.key) for s in pinned],
            [("style", "某字幕组"), ("streamer", "柊優花"), ("common", "星之海"), ("common", "别的条目")],
        )
        # A pinned subject that an earlier chunk already wrote keeps saying so.
        self.assertTrue(pinned[2].applied)
        self.assertEqual(pinned[1].score, float("inf"))


if __name__ == "__main__":
    unittest.main()
