from __future__ import annotations

from typing import Any


def _vibeskill_filenode_base_name(relative: str) -> str:
    relative = relative.rstrip("/")
    if not relative:
        return ""
    return relative.split("/")[-1]


def _vibeskill_filenode_absolute(task_id: str, relative: str) -> str:
    rel = str(relative or "").lstrip("/")
    root = f"/vibeskill/{task_id}/skill"
    if not rel:
        return root
    return f"{root}/{rel}"


def skilldev_tree_to_file_tree_nodes(
    tree: list[Any],
    *,
    task_id: str,
) -> list[dict[str, Any]]:
    """SkillDev 内部树 → VibeSkill FileTreeNode[]（嵌套 children，与前端 FileTreeNode 对齐）。"""

    def convert_node(n: Any) -> dict[str, Any] | None:
        if not isinstance(n, dict):
            return None
        kind = str(n.get("type") or "")
        raw_path = str(n.get("path") or "")

        if kind == "dir":
            rel = raw_path.rstrip("/")
            name = _vibeskill_filenode_base_name(rel) or rel
            children: list[dict[str, Any]] = []
            ch = n.get("children")
            if isinstance(ch, list):
                for child in ch:
                    converted = convert_node(child)
                    if converted is not None:
                        children.append(converted)
            return {
                "name": name,
                "path": rel,
                "absolute": _vibeskill_filenode_absolute(task_id, rel),
                "type": "directory",
                "ignored": False,
                "children": children,
            }
        if kind == "file":
            rel = raw_path
            name = _vibeskill_filenode_base_name(rel) or rel
            return {
                "name": name,
                "path": rel,
                "absolute": _vibeskill_filenode_absolute(task_id, rel),
                "type": "file",
                "ignored": False,
            }
        return None

    out: list[dict[str, Any]] = []
    if isinstance(tree, list):
        for item in tree:
            node = convert_node(item)
            if node is not None:
                out.append(node)
    return out
