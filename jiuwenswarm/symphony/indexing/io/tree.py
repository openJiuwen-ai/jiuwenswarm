from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Iterable, List, Sequence

from indexing.io.items_jsonl import is_passthrough_item_uri


def write_tree_preset(payload: Dict[str, object], path: Path) -> None:
    nodes = sort_tree_nodes(payload.get("nodes") or [])
    lines = ["nodes:"]
    for node in nodes:
        if not isinstance(node, dict):
            continue
        lines.append(f"  - cid: {json.dumps(str(node.get('cid', '')), ensure_ascii=False)}")
        lines.append(f"    type: {json.dumps(str(node.get('type', '')), ensure_ascii=False)}")
        description = str(node.get("description", ""))
        if description:
            lines.append(f"    description: {json.dumps(description, ensure_ascii=False)}")
        select_when = str(node.get("select_when", ""))
        if select_when:
            lines.append(f"    select_when: {json.dumps(select_when, ensure_ascii=False)}")
        dont_select_when = str(node.get("dont_select_when", ""))
        if dont_select_when:
            lines.append(f"    dont_select_when: {json.dumps(dont_select_when, ensure_ascii=False)}")
        source_description = str(node.get("source_description", ""))
        if source_description:
            lines.append(f"    source_description: {json.dumps(source_description, ensure_ascii=False)}")
        worker_id = str(node.get("worker_id", "")).strip()
        if worker_id:
            lines.append(f"    worker_id: {json.dumps(worker_id, ensure_ascii=False)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def sort_tree_nodes(nodes: Sequence[Dict[str, object]]) -> List[Dict[str, object]]:
    return sorted(
        [dict(node) for node in nodes if isinstance(node, dict)],
        key=lambda node: (
            len(str(node.get("cid") or "").split(".")),
            str(node.get("cid") or ""),
        ),
    )


def normalize_item_paths(item_paths: Iterable[str | Path]) -> List[str]:
    normalized: List[str] = []
    seen: set[str] = set()
    for item in item_paths:
        raw = str(item).strip()
        if not raw:
            continue
        if is_passthrough_item_uri(raw):
            key = raw
        else:
            key = str(Path(raw).expanduser().resolve())
        if key in seen:
            continue
        seen.add(key)
        normalized.append(key)
    return normalized


def load_tree_preset(path: Path) -> Dict[str, object]:
    try:
        import yaml
    except Exception:
        yaml = None
    text = path.read_text(encoding="utf-8")
    if yaml is not None:
        payload = yaml.safe_load(text) or {}
        if isinstance(payload, dict):
            payload["nodes"] = sort_tree_nodes(payload.get("nodes") or [])
            return payload
    return parse_simple_nodes_yaml(text)


def parse_simple_nodes_yaml(text: str) -> Dict[str, object]:
    nodes: List[Dict[str, object]] = []
    current: Dict[str, object] | None = None
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if line.strip() == "nodes:":
            continue
        if line.startswith("  - "):
            if current:
                nodes.append(current)
            current = {}
            line = line[4:]
        elif current is None:
            continue
        else:
            line = line.strip()
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        raw_value = value.strip()
        if not raw_value:
            parsed_value = ""
        elif (
            raw_value[:1] in {'"', "[", "{", "-"}
            or raw_value in {"true", "false", "null"}
            or raw_value.replace(".", "", 1).isdigit()
        ):
            try:
                parsed_value = json.loads(raw_value)
            except Exception:
                parsed_value = raw_value.strip('"')
        else:
            parsed_value = raw_value
        current[str(key).strip()] = parsed_value
    if current:
        nodes.append(current)
    return {"nodes": sort_tree_nodes(nodes)}


__all__ = [
    "load_tree_preset",
    "normalize_item_paths",
    "parse_simple_nodes_yaml",
    "sort_tree_nodes",
    "write_tree_preset",
]
