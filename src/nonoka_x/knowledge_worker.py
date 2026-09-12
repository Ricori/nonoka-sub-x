"""Knowledge maintenance in the engine interpreter, without an LLM call."""
from __future__ import annotations

import hashlib
import json
import sys


def handle(request: dict) -> dict:
    from finesub.llm.knowledge.base import knowledge_root_path, knowledge_write_lock
    from finesub.llm.knowledge.node.edit import edit_subject
    from finesub.llm.knowledge.node.repo import KnowledgeRepo

    root = knowledge_root_path()
    repo = KnowledgeRepo.open(root)

    def entry(subject, rev):
        content = repo.entry_text(subject.local_id, rev)
        return {"id": subject.local_id, "name": subject.payload["surface"],
                "category": subject.payload["category"], "content": content,
                "version": hashlib.sha256(content.encode()).hexdigest(), "rev": rev}

    action = request.get("action")
    if action == "list":
        rev = repo.rev
        return {"entries": [{"id": s.local_id, "name": s.payload["surface"],
                             "category": s.payload["category"], "intro": s.payload.get("intro", "")}
                            for s in repo.subjects(rev=rev)], "rev": rev}
    if action not in ("read", "save"):
        raise ValueError("未知知识库操作")
    ident = request.get("id")
    if not isinstance(ident, str):
        raise ValueError("请选择知识库条目")
    if action == "read":
        rev = repo.rev
        subject = next((s for s in repo.subjects(rev=rev) if s.local_id == ident), None)
        if subject is None:
            raise ValueError("条目已不存在，请刷新列表")
        return entry(subject, rev)
    content = request.get("content")
    if not isinstance(content, str) or not content.strip() or len(content.encode()) > 1_000_000:
        raise ValueError("条目内容不能为空，且不能超过 1 MB")
    with knowledge_write_lock(root) as acquired:
        if not acquired:
            raise ValueError("知识库正忙，请稍后重试")
        subject = next((s for s in repo.subjects() if s.local_id == ident), None)
        if subject is None:
            raise ValueError("条目已不存在，请刷新列表；当前编辑已保留")
        current = entry(subject, repo.rev)
        if request.get("version") != current["version"]:
            raise ValueError("条目已被其他任务修改，未保存。请复制当前编辑后重新加载条目")
        report = edit_subject(repo, subject, content, note="Nonoka X 知识库编辑")
        result = entry(repo.store.node(ident), repo.rev)
        result["warnings"] = report.rejected
        return result


def main() -> None:
    try:
        result = {"result": handle(json.load(sys.stdin))}
    except Exception as exc:
        result = {"error": str(exc)}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
