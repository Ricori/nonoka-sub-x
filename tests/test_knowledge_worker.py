import pytest
import os
from pathlib import Path

from nonoka_x.knowledge_worker import handle
from finesub.llm.knowledge.node.repo import KnowledgeRepo


@pytest.fixture
def knowledge(tmp_path, monkeypatch):
    monkeypatch.setenv("FINESUB_KNOWLEDGE_ROOT", str(tmp_path))
    repo = KnowledgeRepo.open(tmp_path)
    with repo.store.begin("user") as txn:
        subject = txn.create_node("test-subject", "subject", {"surface": "テスト", "intro": "原始简介", "category": "common"})
    yield subject.local_id, repo
    KnowledgeRepo.forget(tmp_path)


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
    provider = LocalProvider(tmp_path / "tasks", project / "third_party/finesub", issues=[])
    environment = dict(os.environ, PYTHONPATH=os.pathsep.join([str(project / "src"), str(project / "third_party/finesub/src")]), PYTHONIOENCODING="utf-8")
    monkeypatch.setattr(provider, "_engine_environment", lambda: environment)
    assert provider.knowledge({"action": "list"})["entries"][0]["id"] == ident
    before = provider.knowledge({"action": "read", "id": ident})
    saved = provider.knowledge({"action": "save", "id": ident, "version": before["version"],
                                "content": before["content"].replace("原始简介", "桌面编辑")})
    assert "桌面编辑" in saved["content"]
    with pytest.raises(ProviderError, match="已被其他任务修改"):
        provider.knowledge({"action": "save", "id": ident, "version": before["version"], "content": before["content"]})
