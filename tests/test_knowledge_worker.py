"""The knowledge page's list, read and save (`finesub_knowledge.manage.handle`, the knowledge package's).

In this process, then through the desktop's door: `LocalProvider.knowledge`,
which starts the knowledge package's own process on the data directory's base.
"""

import pytest
from pathlib import Path

from finesub_knowledge.manage import handle
from finesub.llm.knowledge.node.repo import KnowledgeRepo


@pytest.fixture
def knowledge(tmp_path, monkeypatch):
    # Where every launch context names it (WP-M2: the knowledge package's directory).
    root = tmp_path / "data" / "engines" / "finesub-knowledge" / "knowledge"
    monkeypatch.setenv("FINESUB_KNOWLEDGE_ROOT", str(root))
    repo = KnowledgeRepo.open(root)
    with repo.store.begin("user") as txn:
        subject = txn.create_node("test-subject", "subject", {"surface": "テスト", "intro": "原始简介", "category": "common"})
    yield subject.local_id, repo
    KnowledgeRepo.forget(root)


def test_read_edit_and_stale_save(knowledge):
    ident, repo = knowledge
    assert handle({"action": "list"})["entries"][0]["id"] == ident
    before = handle({"action": "read", "id": ident})
    saved = handle({"action": "save", "id": ident, "version": before["version"],
                    "content": before["content"].replace("原始简介", "更新简介")})
    assert "更新简介" in saved["content"]
    assert saved["rev"] > before["rev"]
    assert repo.store.revision(saved["rev"]).kind == "user"
    with pytest.raises(ValueError, match="已被其他任务修改"):
        handle({"action": "save", "id": ident, "version": before["version"], "content": before["content"]})
    assert "更新简介" in handle({"action": "read", "id": ident})["content"]


def test_invalid_edit_preserves_store(knowledge):
    ident, repo = knowledge
    before = handle({"action": "read", "id": ident})
    with pytest.raises(Exception):
        handle({"action": "save", "id": ident, "version": before["version"], "content": "invalid"})
    assert repo.rev == before["rev"]
    assert handle({"action": "read", "id": ident}) == before


def test_unrelated_entry_update_does_not_block_save(knowledge):
    ident, repo = knowledge
    before = handle({"action": "read", "id": ident})
    with repo.store.begin("user") as txn:
        txn.create_node("other-subject", "subject", {"surface": "別", "intro": "其他条目", "category": "common"})
    saved = handle({"action": "save", "id": ident, "version": before["version"],
                    "content": before["content"].replace("原始简介", "更新简介")})
    assert "更新简介" in saved["content"]


def test_provider_worker_roundtrip(knowledge, tmp_path, monkeypatch):
    from nonoka_x.local_provider import LocalProvider, ProviderError

    ident, _ = knowledge
    project = Path(__file__).resolve().parents[1]
    # The data directory's base, the one the knowledge package's context names.
    provider = LocalProvider(tmp_path / "data" / "tasks", project / "engines", issues=[])
    assert provider.knowledge({"action": "list"})["entries"][0]["id"] == ident
    before = provider.knowledge({"action": "read", "id": ident})
    saved = provider.knowledge({"action": "save", "id": ident, "version": before["version"],
                                "content": before["content"].replace("原始简介", "桌面编辑")})
    assert "桌面编辑" in saved["content"]
    with pytest.raises(ProviderError, match="已被其他任务修改"):
        provider.knowledge({"action": "save", "id": ident, "version": before["version"], "content": before["content"]})
