"""The knowledge package's own operations, run inside a knowledge process.

Moved here unchanged from the core (WP-K): the knowledge page's list, read and
save (`handle`, formerly `nonoka_x.knowledge_worker.handle`) and the subject
preparation a task does before it starts (`prepare_subjects`, formerly the
store half of `nonoka_x.worker.prepare_knowledge_subjects`). Both open the
store with FineSub's own code, which is why they run here and nowhere else.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping, Sequence

#: The subject kinds the desktop offers, as the core's prompt context names
#: them (`nonoka_x.worker._KIND_LABELS`, which the correction half keeps).
KIND_LABELS = {
    "streamer": "主播 / 频道",
    "work": "作品 / 节目",
    "topic": "其他知识主题",
}


# --- the knowledge page --------------------------------------------------------------


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


# --- subjects before a task --------------------------------------------------------------


def _split_aliases(value: Any, subject: str) -> list[str]:
    aliases: list[str] = []
    for alias in re.split(r"[、,，;；\n]+", str(value or "")):
        alias = alias.strip()
        if alias and alias != subject and alias not in aliases:
            aliases.append(alias)
    return aliases


def _create_knowledge_subject(repo: Any, item: Mapping[str, Any], task_id: str) -> Any:
    """Create one user-described subject; returns its resolved entry.

    The subject fields are explicit user input, not a guess extracted from a
    transcript. Persisting that small scaffold makes an empty repository
    usable immediately; the model-owned update still decides which video
    facts are reliable enough to append.
    """

    from finesub.llm.knowledge.node.proposals import apply_model_proposals

    subject = str(item.get("subject") or "").strip()
    kind = str(item.get("kind") or "streamer")
    category = "streamer" if kind == "streamer" else "common"
    kind_label = KIND_LABELS.get(kind, KIND_LABELS["topic"])
    description = " ".join(str(item.get("description") or "").split())
    intro = (description or f"用户在字幕任务中指定的{kind_label}知识主体。")[:240]
    proposal = {
        "op": "create_entry",
        "category": category,
        "entry": subject,
        "intro": intro,
        "entry_type": "其他" if category == "common" else "",
        "aliases": _split_aliases(item.get("aliases"), subject),
        "reason": "用户在任务设置中明确指定此主体；初始化词条以承接本次及后续任务知识。",
    }
    proposal_text = (
        "<knowledge_proposals>\n"
        + json.dumps(proposal, ensure_ascii=False, separators=(",", ":"))
        + "\n</knowledge_proposals>"
    )
    report = apply_model_proposals(
        proposal_text,
        repo=repo,
        task_id=f"{task_id}:subject-bootstrap",
        knowledge_read_rev=repo.rev,
    )
    # A concurrent task may have created it after our first resolve; either
    # way the entry must exist now.
    resolved = repo.resolve(subject)
    if resolved is None:
        reasons = "; ".join(record.reason for record in report.skipped) or "unknown reason"
        raise RuntimeError(f"知识主体「{subject}」初始化失败：{reasons}")
    return resolved


def prepare_subjects(
    subjects: Sequence[Mapping[str, Any]], *, knowledge: str | None, task_id: str,
    knowledge_root: Any = None,
) -> dict[str, Any]:
    """Resolve the picked subjects to engine names, creating new ones first.

    Existing subjects travel by store id, which survives a rename; the engine
    is handed `category/key`, the qualified form its human lookup accepts
    without ambiguity. A subject without an id is created here -- the provider
    only lets that through under `update` -- unless an entry of that name
    already exists, which is then reused rather than duplicated.
    """

    from finesub.llm.knowledge.base import knowledge_root_path
    from finesub.llm.knowledge.node.repo import KnowledgeRepo

    repo = KnowledgeRepo.open(knowledge_root_path(knowledge_root))
    names: list[str] = []
    created: list[str] = []
    for item in subjects:
        subject = str(item.get("subject") or "").strip()
        ident = str(item.get("id") or "").strip()
        if ident:
            node = next((s for s in repo.subjects() if s.local_id == ident), None)
            if node is None:
                raise RuntimeError(f"知识主体「{subject}」已不在知识库中，请重新选择")
            category, key = str(node.payload["category"]), str(node.payload["surface"])
        else:
            resolved = repo.resolve(subject)
            if resolved is None:
                if knowledge != "update":
                    raise RuntimeError(f"知识库里没有「{subject}」，只有自动更新模式可以新建主体")
                resolved = _create_knowledge_subject(repo, item, task_id)
                created.append(resolved.key)
            category, key = resolved.category, resolved.key
        name = f"{category}/{key}"
        if name not in names:
            names.append(name)
    return {"names": names, "created": created, "rev": repo.rev}


def describe() -> dict[str, Any]:
    """What this knowledge package is, for a readiness check that asks."""

    from .protocol import PROTOCOL_VERSION

    return {"package": "finesub-knowledge", "protocol": PROTOCOL_VERSION}
