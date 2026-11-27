# pylint: disable=line-too-long,huawei-redefined-outer-name
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Dict, List, Sequence

from ..models import CatalogRecord


def scan_skill_paths(item_paths: Sequence[Path]) -> Dict[str, dict]:
    from ..scanners import SkillScanner

    skill_map: Dict[str, dict] = {}
    for path in item_paths:
        scanner = SkillScanner(path.parent if path.name == "skills" else path.parent)
        for item in scanner.to_dict_list():
            if str(item.get("id") or "") == path.name:
                skill_map[path.name] = item
    return skill_map


def merge_added_skills_into_tree(*, nodes: Sequence[object], added_skills: Dict[str, dict]) -> List[Dict[str, object]]:
    normalized = [dict(node) for node in nodes if isinstance(node, dict)]
    used_cids = {str(node.get("cid") or "") for node in normalized}
    branch_nodes = [node for node in normalized if str(node.get("type") or "") == "branch"]
    branch_token_cache = {str(node.get("cid") or ""): text_tokens(str(node.get("cid") or ""), str(node.get("description") or "")) for node in branch_nodes}
    for worker_id, skill in sorted(added_skills.items()):
        parent_cid = choose_parent_branch_for_skill(skill=skill, branch_nodes=branch_nodes, branch_token_cache=branch_token_cache)
        cid = unique_child_cid(parent_cid=parent_cid, segment=slug_term(worker_id, fallback="skill"), used=used_cids)
        node = {
            "cid": cid,
            "type": "leaf",
            "description": str(skill.get("description") or "").strip(),
            "worker_id": worker_id,
        }
        normalized.append(node)
        used_cids.add(cid)
    return normalized


def choose_parent_branch_for_skill(*, skill: dict, branch_nodes: Sequence[Dict[str, object]], branch_token_cache: Dict[str, set[str]]) -> str:
    if not branch_nodes:
        return ""
    skill_tokens = text_tokens(
        str(skill.get("name") or ""),
        str(skill.get("description") or ""),
        str(skill.get("content") or ""),
    )
    best_cid = ""
    best_score = -1
    for node in branch_nodes:
        cid = str(node.get("cid") or "")
        overlap = len(skill_tokens & branch_token_cache.get(cid, set()))
        depth_bonus = len(cid.split(".")) if cid else 0
        score = overlap * 100 + depth_bonus
        if score > best_score:
            best_score = score
            best_cid = cid
    return best_cid


def prune_deleted_skills_from_tree(nodes: Sequence[object], *, removed_worker_ids: set[str]) -> List[Dict[str, object]]:
    normalized = [dict(node) for node in nodes if isinstance(node, dict)]
    normalized = [node for node in normalized if str(node.get("worker_id") or "") not in removed_worker_ids]
    while True:
        child_counts: Dict[str, int] = {}
        for node in normalized:
            cid = str(node.get("cid") or "")
            if not cid:
                continue
            parent = parent_cid(cid)
            if parent:
                child_counts[parent] = child_counts.get(parent, 0) + 1
        pruned = [node for node in normalized if str(node.get("type") or "") != "branch" or child_counts.get(str(node.get("cid") or ""), 0) > 0]
        if len(pruned) == len(normalized):
            return pruned
        normalized = pruned


def align_leaf_nodes_with_catalog(nodes: Sequence[object], records_by_worker: Dict[str, CatalogRecord]) -> List[Dict[str, object]]:
    aligned: List[Dict[str, object]] = []
    for raw_node in nodes:
        if not isinstance(raw_node, dict):
            continue
        node = dict(raw_node)
        worker_id = str(node.get("worker_id") or "").strip()
        if worker_id and worker_id in records_by_worker:
            record = records_by_worker[worker_id]
            node["cid"] = record.cid
            node["description"] = record.description
        aligned.append(node)
    return aligned


def build_catalog_records_from_existing(*, nodes: Sequence[object], records_by_worker: Dict[str, CatalogRecord]) -> List[CatalogRecord]:
    records: List[CatalogRecord] = []
    for raw_node in nodes:
        if not isinstance(raw_node, dict):
            continue
        worker_id = str(raw_node.get("worker_id") or "").strip()
        if not worker_id:
            continue
        record = records_by_worker.get(worker_id)
        if record is None:
            continue
        records.append(
            CatalogRecord(
                skill_id=record.skill_id,
                worker_id=record.worker_id,
                cid=str(raw_node.get("cid") or record.cid),
                name=record.name,
                description=str(raw_node.get("description") or record.description),
                skill_path=record.skill_path,
                branch_path=tuple(str(raw_node.get("cid") or record.cid).split(".")[:-1]),
                category=record.category,
                retrieval_text=record.retrieval_text,
                metadata=dict(record.metadata),
            )
        )
    return sorted(records, key=lambda item: item.cid)


def tree_nodes_to_tree_dict(nodes: Sequence[object], records: Sequence[CatalogRecord]) -> dict:
    nodes_by_cid = {str(node.get("cid") or ""): dict(node) for node in nodes if isinstance(node, dict)}
    children: Dict[str, list[str]] = {}
    for cid in nodes_by_cid:
        parent = parent_cid(cid)
        children.setdefault(parent, []).append(cid)
    records_by_cid = {record.cid: record for record in records}

    def build(cid: str) -> dict:
        node = nodes_by_cid.get(cid, {})
        label = cid.split(".")[-1] if cid else "ROOT"
        payload = {
            "name": str(node.get("worker_id") or label),
            "cid": cid,
            "type": str(node.get("type") or "branch"),
            "description": str(node.get("description") or ""),
            "children": [build(child) for child in sorted(children.get(cid, []))],
        }
        record = records_by_cid.get(cid)
        if record is not None:
            payload["skill_path"] = record.skill_path
            payload["worker_id"] = record.worker_id
        return payload

    root_children = [build(cid) for cid in sorted(children.get("", []))]
    return {"name": "ROOT", "cid": "ROOT", "type": "root", "children": root_children}


def enrich_branch_descriptions(nodes: Sequence[object], *, catalog_records: Sequence[CatalogRecord]) -> List[Dict[str, object]]:
    catalog_by_branch: Dict[str, list[CatalogRecord]] = {}
    for record in catalog_records:
        parts = record.cid.split(".")
        for depth in range(1, len(parts)):
            branch_cid = ".".join(parts[:depth])
            catalog_by_branch.setdefault(branch_cid, []).append(record)

    enriched: List[Dict[str, object]] = []
    for raw_node in nodes:
        if not isinstance(raw_node, dict):
            continue
        node = dict(raw_node)
        if str(node.get("type") or "") == "branch":
            cid = str(node.get("cid") or "")
            node["description"] = build_branch_description(
                cid=cid,
                existing_description=str(node.get("description") or ""),
                descendants=catalog_by_branch.get(cid, ()),
            )
        enriched.append(node)
    return enriched


def build_branch_description(*, cid: str, existing_description: str, descendants: Sequence[CatalogRecord]) -> str:
    base = strip_branch_exposure(existing_description)
    if not descendants:
        return base
    samples = sample_catalog_records(descendants, limit=3)
    parts: List[str] = []
    if base:
        parts.append(base)
    parts.append(f"Covers {len(descendants)} descendant skill{'s' if len(descendants) != 1 else ''}.")
    keywords = collect_branch_keywords(descendants, limit=8)
    if keywords:
        parts.append("Representative keywords: " + ", ".join(keywords))
    parts.append("Representative descendants: " + "; ".join(format_catalog_record_snippet(record) for record in samples))
    return "\n\n".join(part for part in parts if part).strip()


def strip_branch_exposure(description: str) -> str:
    marker = "Representative descendants:"
    head, _sep, _tail = str(description or "").partition(marker)
    return head.strip()


def sample_catalog_records(records: Sequence[CatalogRecord], *, limit: int) -> List[CatalogRecord]:
    target = max(0, limit)
    if target <= 0:
        return []
    ordered = sorted(records, key=lambda item: (item.name.lower(), item.worker_id.lower(), item.cid))
    selected: List[CatalogRecord] = []
    seen_worker_ids: set[str] = set()
    seen_tokens: set[str] = set()
    while len(selected) < min(target, len(ordered)):
        best_record: CatalogRecord | None = None
        best_score = -1
        for record in ordered:
            if record.worker_id in seen_worker_ids:
                continue
            tokens = _record_text_tokens(record)
            novelty = len(tokens - seen_tokens)
            coverage = len(tokens)
            score = novelty * 10 + coverage
            if best_record is None or score > best_score:
                best_record = record
                best_score = score
        if best_record is None:
            break
        selected.append(best_record)
        seen_worker_ids.add(best_record.worker_id)
        seen_tokens.update(_record_text_tokens(best_record))
    return selected


def format_catalog_record_snippet(record: CatalogRecord) -> str:
    name = str(record.name or record.worker_id).strip() or record.worker_id
    summary = _compact_summary(record.description, limit=96)
    if summary:
        return f"{name}: {summary}"
    return name


def collect_branch_keywords(records: Sequence[CatalogRecord], *, limit: int) -> List[str]:
    counter: Counter[str] = Counter()
    for record in records:
        counter.update(_record_text_tokens(record))
    if not counter:
        return []
    ranked = sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    return [token for token, _count in ranked[: max(0, limit)]]


def _record_text_tokens(record: CatalogRecord) -> set[str]:
    return text_tokens(record.name, record.description, record.worker_id, record.cid)


def _compact_summary(text: str, *, limit: int) -> str:
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 3)].rstrip() + "..."


def text_tokens(*values: str) -> set[str]:
    tokens: set[str] = set()
    for value in values:
        for token in str(value or "").replace("-", " ").replace("_", " ").replace(".", " ").split():
            cleaned = token.strip().lower()
            if cleaned:
                tokens.add(cleaned)
    return tokens


def slug_term(value: str, fallback: str = "node") -> str:
    raw = "".join(ch.lower() if ch.isalnum() else "-" for ch in str(value or ""))
    compact = "-".join(part for part in raw.split("-") if part)
    return compact or fallback


def join_cid(parent: str, child: str) -> str:
    return f"{parent}.{child}" if parent else child


def parent_cid(cid: str) -> str:
    return cid.rsplit(".", 1)[0] if "." in cid else ""


def unique_child_cid(*, parent_cid: str, segment: str, used: set[str]) -> str:
    candidate = join_cid(parent_cid, segment)
    if candidate not in used:
        return candidate
    index = 2
    while True:
        candidate = join_cid(parent_cid, f"{segment}-{index}")
        if candidate not in used:
            return candidate
        index += 1
