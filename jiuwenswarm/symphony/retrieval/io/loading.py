from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Dict, List, Sequence

from models.retrieval import RetrieverItem, RetrieverNode, RetrieverChoice
from shared.storage import is_s3_uri, materialize_s3_dir
from shared.tags import normalize_tags


@dataclass(frozen=True)
class CatalogRecord:
    choice_id: str
    payload: str
    worker_id: str = ""
    name: str = ""
    description: str = ""
    retrieval_text: str = ""
    branch_path: tuple[str, ...] = ()
    metadata: Dict[str, object] = field(default_factory=dict)
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class LoadedRetrieverIndex:
    index_dir: Path
    tree_root: RetrieverNode
    choices: tuple[RetrieverChoice, ...]
    catalog_records: tuple[CatalogRecord, ...]
    manifest: Dict[str, object] = field(default_factory=dict)


def load_retriever_index(index_dir: str | Path) -> LoadedRetrieverIndex:
    base_dir = _materialize_index_dir(index_dir)
    manifest = _load_manifest(base_dir)
    catalog_path = base_dir / _artifact_path(manifest, "catalog", "catalog.jsonl")
    tree_path = base_dir / _artifact_path(manifest, "tree_index", "tree_index.yaml")
    catalog_records = tuple(load_catalog_records(catalog_path))
    catalog_records = tuple(_apply_tree_worker_ids(catalog_records, worker_ids_by_cid=_load_tree_worker_ids(tree_path)))
    tree_root = load_tree_root(tree_path, catalog_records=catalog_records)
    choices = tuple(
        RetrieverChoice(
            choice_id=record.choice_id,
            payload=record.payload,
            description=record.description or record.retrieval_text,
        )
        for record in catalog_records
    )
    return LoadedRetrieverIndex(
        index_dir=base_dir,
        tree_root=tree_root,
        choices=choices,
        catalog_records=catalog_records,
        manifest=manifest,
    )


def _materialize_index_dir(index_dir: str | Path) -> Path:
    raw = str(index_dir).strip()
    if is_s3_uri(raw):
        return materialize_s3_dir(raw, cache_namespace="retriever-s3-index-cache")
    return Path(index_dir).resolve()


def load_catalog_records(path: str | Path) -> List[CatalogRecord]:
    records: List[CatalogRecord] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text:
            continue
        payload = json.loads(text)
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        choice_id = str(payload.get("name") or payload.get("choice_id") or "").strip()
        candidate_payload = str(payload.get("cid") or payload.get("payload") or "").strip()
        if not choice_id or not candidate_payload:
            continue
        records.append(
            CatalogRecord(
                choice_id=choice_id,
                payload=candidate_payload,
                worker_id=str(payload.get("worker_id") or "").strip(),
                name=str(payload.get("name") or choice_id).strip() or choice_id,
                description=str(payload.get("description") or "").strip(),
                retrieval_text=str(payload.get("retrieval_text") or "").strip(),
                branch_path=tuple(
                    str(item).strip() for item in (payload.get("branch_path") or []) if str(item).strip()
                ),
                metadata={
                    "skill_path": str(payload.get("skill_path") or "").strip(),
                    "category": str(payload.get("category") or "").strip(),
                    "worker_id": str(payload.get("worker_id") or "").strip(),
                    "tags": list(normalize_tags(payload.get("tags"), metadata.get("tags"))),
                },
                tags=normalize_tags(payload.get("tags"), metadata.get("tags")),
            )
        )
    records.sort(key=lambda item: (item.payload, item.choice_id))
    return records


def load_tree_root(path: str | Path, *, catalog_records: Sequence[CatalogRecord]) -> RetrieverNode:
    payload = _load_yaml_like(Path(path).read_text(encoding="utf-8"))
    nodes = payload.get("nodes") or []
    record_by_payload = {record.payload: record for record in catalog_records}
    return _build_tree_from_nodes(nodes, record_by_payload=record_by_payload)


def _load_tree_worker_ids(path: str | Path) -> Dict[str, str]:
    payload = _load_yaml_like(Path(path).read_text(encoding="utf-8"))
    worker_ids_by_cid: Dict[str, str] = {}
    for raw_node in payload.get("nodes") or []:
        if not isinstance(raw_node, dict):
            continue
        cid = str(raw_node.get("cid") or "").strip()
        worker_id = str(raw_node.get("worker_id") or "").strip()
        if cid and worker_id:
            worker_ids_by_cid[cid] = worker_id
    return worker_ids_by_cid


def _apply_tree_worker_ids(
    records: Sequence[CatalogRecord], *, worker_ids_by_cid: Dict[str, str]
) -> List[CatalogRecord]:
    if not worker_ids_by_cid:
        return list(records)
    updated: List[CatalogRecord] = []
    for record in records:
        worker_id = str(worker_ids_by_cid.get(record.payload) or record.worker_id or "").strip()
        if not worker_id:
            updated.append(record)
            continue
        metadata = dict(record.metadata or {})
        metadata["worker_id"] = worker_id
        updated.append(replace(record, worker_id=worker_id, metadata=metadata))
    return updated


def _build_tree_from_nodes(nodes: Sequence[object], *, record_by_payload: Dict[str, CatalogRecord]) -> RetrieverNode:
    branch_specs: Dict[str, Dict[str, object]] = {}
    children_map: Dict[str, List[str]] = {}
    items_by_parent: Dict[str, List[RetrieverItem]] = {}

    for raw_node in nodes:
        if not isinstance(raw_node, dict):
            continue
        cid = str(raw_node.get("cid") or "").strip()
        node_type = str(raw_node.get("type") or "").strip().lower()
        if not cid:
            continue
        parent_cid = cid.rsplit(".", 1)[0] if "." in cid else ""
        if node_type == "leaf":
            record = record_by_payload.get(cid)
            item_id = str(
                (record.name if record else "") or (record.choice_id if record else "") or cid.rsplit(".", 1)[-1]
            ).strip()
            label = str((record.name if record else "") or item_id or cid.rsplit(".", 1)[-1]).strip() or item_id or cid
            description = _compose_routing_description(
                description=str(raw_node.get("description") or (record.description if record else "") or "").strip(),
                select_when=str(raw_node.get("select_when") or "").strip(),
                dont_select_when=str(raw_node.get("dont_select_when") or "").strip(),
            )
            items_by_parent.setdefault(parent_cid, []).append(
                RetrieverItem(
                    item_id=item_id,
                    payload=cid,
                    label=label,
                    description=description,
                )
            )
            continue
        branch_specs[cid] = raw_node
        children_map.setdefault(parent_cid, []).append(cid)

    def build_branch(cid: str, *, root: bool = False) -> RetrieverNode | None:
        child_branch_ids = sorted(children_map.get(cid, []))
        children = [child for child in (build_branch(child_cid) for child_cid in child_branch_ids) if child is not None]
        items = sorted(items_by_parent.get(cid, []), key=lambda item: (item.payload, item.item_id))
        if not root and not children and not items:
            return None
        spec = branch_specs.get(cid, {})
        label = "ROOT" if root else (cid.rsplit(".", 1)[-1] if cid else "ROOT")
        description = (
            _compose_routing_description(
                description=str(spec.get("description") or "").strip(),
                select_when=str(spec.get("select_when") or "").strip(),
                dont_select_when=str(spec.get("dont_select_when") or "").strip(),
            )
            if isinstance(spec, dict)
            else ""
        )
        return RetrieverNode(
            node_id="ROOT" if root else cid,
            label=label,
            description=description,
            children=tuple(children),
            items=tuple(items),
        )

    root = build_branch("", root=True)
    return root or RetrieverNode(node_id="ROOT", label="ROOT")


def _compose_routing_description(*, description: str, select_when: str = "", dont_select_when: str = "") -> str:
    parts = [str(description or "").strip()] if str(description or "").strip() else []
    if select_when and "Select when:" not in description:
        parts.append(f"Select when: {select_when}")
    if dont_select_when and "Don't select when:" not in description:
        parts.append(f"Don't select when: {dont_select_when}")
    return "\n".join(part for part in parts if part).strip()


def _load_manifest(index_dir: Path) -> Dict[str, object]:
    manifest_path = index_dir / "manifest.json"
    if not manifest_path.exists():
        return {}
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def _artifact_path(manifest: Dict[str, object], key: str, fallback: str) -> str:
    artifacts = manifest.get("artifacts") or {}
    if isinstance(artifacts, dict):
        value = str(artifacts.get(key) or "").strip()
        if value:
            return value
    return fallback


def _load_yaml_like(text: str) -> Dict[str, object]:
    try:
        import yaml

        payload = yaml.safe_load(text) or {}
        if isinstance(payload, dict):
            return payload
    except Exception:
        return _parse_simple_nodes_yaml(text)
    return _parse_simple_nodes_yaml(text)


def _parse_simple_nodes_yaml(text: str) -> Dict[str, object]:
    nodes: List[Dict[str, str]] = []
    current: Dict[str, str] | None = None
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped or stripped == "nodes:":
            continue
        if stripped.startswith("- "):
            if current:
                nodes.append(current)
            current = {}
            stripped = stripped[2:]
        if current is None or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        current[key.strip()] = value.strip()
    if current:
        nodes.append(current)
    return {"nodes": nodes}


__all__ = [
    "CatalogRecord",
    "LoadedRetrieverIndex",
    "load_catalog_records",
    "load_retriever_index",
    "load_tree_root",
]
