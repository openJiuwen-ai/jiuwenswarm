from __future__ import annotations

import re
from typing import TYPE_CHECKING

try:
    import yaml
except ModuleNotFoundError:
    yaml = None

from shared.rich_compat import Console, RichTree

from .schema import TreeNode

if TYPE_CHECKING:
    from .builder import TreeBuilder


console = Console()
GENERIC_TERMS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "how",
    "if",
    "in",
    "into",
    "is",
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "tool",
    "tools",
    "use",
    "used",
    "using",
    "when",
    "with",
}


class TreePresetWriter:
    """Owns recursive tree serialization, preset flattening, and YAML output."""

    def __init__(self, builder: "TreeBuilder") -> None:
        self._builder = builder

    def tree_to_dict(self, tree: TreeNode) -> dict:
        return self.node_to_dict(tree)

    def tree_to_orchestrator_preset(self, tree_dict: dict) -> dict:
        nodes = self.flatten_capability_tree(tree_dict)
        nodes = self.rename_leaf_nodes(nodes)
        return {
            "tree_sketch": self.build_tree_sketch(nodes),
            "nodes": nodes,
        }

    def flatten_capability_tree(self, tree: dict) -> list[dict]:
        nodes: list[dict] = []
        used_cids: set[str] = set()

        def walk_category(node: dict, parent_cid: str, top_category_id: str) -> None:
            branch_id = str(node.get("id") or node.get("name") or "category")
            branch_name = str(node.get("name") or branch_id)
            branch_description = str(node.get("description") or branch_name)
            branch_select_when = str(node.get("select_when") or "").strip()
            branch_dont_select_when = str(node.get("dont_select_when") or "").strip()
            branch_cid = self.unique_child_cid(parent_cid, self.cid_term(branch_id, fallback="Category"), used_cids)

            nodes.append(
                {
                    "cid": branch_cid,
                    "type": "branch",
                    "description": branch_description,
                    "select_when": branch_select_when,
                    "dont_select_when": branch_dont_select_when,
                    "keywords": self.extract_keywords(
                        branch_id, branch_name, branch_description, branch_select_when, branch_dont_select_when
                    ),
                    "examples": [],
                    "category": top_category_id,
                    "source_type": "capability_group",
                }
            )

            for child in list(node.get("children", []) or []):
                if isinstance(child, dict):
                    walk_category(child, branch_cid, top_category_id)

            for skill in list(node.get("skills", []) or []):
                if not isinstance(skill, dict):
                    continue
                skill_id = str(skill.get("id") or skill.get("name") or "skill").strip()
                skill_name = str(skill.get("name") or skill_id).strip()
                skill_description = str(skill.get("description") or skill_name).strip()
                leaf_cid = self.unique_child_cid(
                    branch_cid,
                    self.cid_term(skill_id or skill_name, fallback="Skill"),
                    used_cids,
                )
                nodes.append(
                    {
                        "cid": leaf_cid,
                        "type": "leaf",
                        "worker_id": skill_id,
                        "description": skill_description,
                        "select_when": str(skill.get("select_when") or "").strip(),
                        "dont_select_when": str(skill.get("dont_select_when") or "").strip(),
                        "source_description": str(skill.get("source_description") or "").strip(),
                        "keywords": self.extract_keywords(skill_id, skill_name, skill_description),
                        "examples": [],
                    }
                )

        root_children = tree.get("children", []) if str(tree.get("id", "")).strip().lower() == "root" else [tree]
        root_children = sorted(
            [item for item in root_children if isinstance(item, dict)],
            key=lambda item: str(item.get("id") or item.get("name") or ""),
        )
        for root_child in root_children:
            top_category_id = self.slug_term(
                str(root_child.get("id") or root_child.get("name") or "category"),
                fallback="category",
            )
            walk_category(root_child, "", top_category_id)
        return nodes

    def rename_leaf_nodes(self, nodes: list[dict]) -> list[dict]:
        if not nodes:
            return nodes
        branch_cids = {
            str(item.get("cid", "")).strip()
            for item in nodes
            if str(item.get("type", "")).strip() == "branch" and str(item.get("cid", "")).strip()
        }
        used: set[str] = set(branch_cids)
        leaf_items = [
            item for item in nodes if str(item.get("type", "")).strip() == "leaf" and str(item.get("cid", "")).strip()
        ]
        leaf_items.sort(key=lambda item: str(item.get("cid", "")))

        renamed: dict[str, str] = {}
        for item in leaf_items:
            old_cid = str(item.get("cid", "")).strip()
            parent_cid = self.parent_cid(old_cid)
            old_term = old_cid.rsplit(".", 1)[-1] if old_cid else "Skill"
            preferred_cid_seed = self.compact_leaf_cid_seed(
                worker_id=str(item.get("worker_id") or "").strip(),
                display_name=str(item.get("name") or "").strip(),
                old_term=old_term,
            )
            segment = self.cid_term(preferred_cid_seed, fallback="Skill")
            new_cid = self.unique_child_cid(parent_cid, segment, used)
            renamed[old_cid] = new_cid

        updated: list[dict] = []
        for item in nodes:
            copied = dict(item)
            cid = str(copied.get("cid", "")).strip()
            if cid in renamed:
                copied["cid"] = renamed[cid]
            updated.append(copied)
        return updated

    @staticmethod
    def compact_leaf_cid_seed(*, worker_id: str, display_name: str, old_term: str) -> str:
        name_tokens = [t for t in re.split(r"[^A-Za-z0-9]+", display_name or "") if t]
        if len(name_tokens) >= 2:
            return " ".join(name_tokens)

        raw = worker_id or old_term or "Skill"
        tokens = [t for t in re.split(r"[^A-Za-z0-9]+", raw) if t]
        if not tokens:
            return "Skill"

        noise_prefix = {
            "claude",
            "code",
            "template",
            "templates",
            "skill",
            "skills",
            "plugin",
            "plugins",
            "repo",
            "github",
            "starter",
            "boilerplate",
            "awesome",
            "example",
            "examples",
        }
        noise_anywhere = {
            "template",
            "templates",
            "skill",
            "skills",
            "plugin",
            "plugins",
            "boilerplate",
        }

        compact = list(tokens)
        while len(compact) > 2:
            head = compact[0].lower()
            has_digit = any(ch.isdigit() for ch in head)
            if head in noise_prefix or has_digit:
                compact.pop(0)
                continue
            break

        filtered: list[str] = []
        for token in compact:
            if len(filtered) >= 2 and token.lower() in noise_anywhere:
                continue
            filtered.append(token)
        if len(filtered) >= 2:
            compact = filtered

        if len(compact) > 4:
            compact = compact[-4:]

        return " ".join(compact or tokens)

    @staticmethod
    def cid_term(value: str, fallback: str = "Node") -> str:
        raw = str(value or "")
        parts = [part for part in re.split(r"[^A-Za-z0-9]+", raw) if part]
        if not parts:
            parts = [fallback]
        token = "".join(part[:1].upper() + part[1:] for part in parts)
        if not token:
            token = fallback
        if not token[0].isalpha():
            token = "N" + token
        return token

    def build_tree_sketch(self, nodes: list[dict]) -> str:
        if not nodes:
            return ""

        by_cid = {str(item.get("cid", "")): item for item in nodes if item.get("cid")}
        children_by_parent: dict[str, list[dict]] = {}
        for node in nodes:
            cid = str(node.get("cid", "")).strip()
            if not cid:
                continue
            children_by_parent.setdefault(self.parent_cid(cid), []).append(node)

        leaf_count_cache: dict[str, int] = {}

        def descendant_leaf_count(cid: str) -> int:
            cached = leaf_count_cache.get(cid)
            if cached is not None:
                return cached
            node = by_cid.get(cid, {})
            if str(node.get("type", "")) != "branch":
                leaf_count_cache[cid] = 1
                return 1
            total = 0
            for child in children_by_parent.get(cid, []):
                child_cid = str(child.get("cid", ""))
                if child_cid:
                    total += descendant_leaf_count(child_cid)
            leaf_count_cache[cid] = total
            return total

        def branch_children(cid: str) -> list[dict]:
            return sorted(
                [item for item in children_by_parent.get(cid, []) if str(item.get("type", "")) == "branch"],
                key=lambda item: str(item.get("cid", "")),
            )

        def leaf_children(cid: str) -> list[dict]:
            return sorted(
                [item for item in children_by_parent.get(cid, []) if str(item.get("type", "")) != "branch"],
                key=lambda item: str(item.get("cid", "")),
            )

        lines: list[str] = [
            "Global Tree Sketch",
            "- Use this sketch only for global orientation across the whole tree.",
            "- When selecting a concrete node path, prefer the current local state over globally similar nodes.",
        ]

        def render_branch(cid: str, indent: int) -> None:
            node = by_cid.get(cid)
            if not node:
                return
            prefix = "  " * indent
            description = self.routing_text_for_node(node) or "No summary"
            child_branches = branch_children(cid)
            child_leaves = leaf_children(cid)
            details = [description, f"descendant_leaves={descendant_leaf_count(cid)}"]
            if child_branches:
                details.append(
                    "child_branches="
                    + ", ".join(
                        str(item.get("cid", "")).split(".")[-1] for item in child_branches[:5] if item.get("cid")
                    )
                )
            if child_leaves:
                details.append(
                    "representative_leaves="
                    + ", ".join(str(item.get("cid", "")).split(".")[-1] for item in child_leaves[:3] if item.get("cid"))
                )
            lines.append(f"{prefix}- {cid}: " + " | ".join(details))
            for child in child_branches:
                render_branch(str(child.get("cid", "")), indent + 1)

        root_branches = sorted(
            [item for item in children_by_parent.get("", []) if str(item.get("type", "")) == "branch"],
            key=lambda item: str(item.get("cid", "")),
        )
        for branch in root_branches:
            render_branch(str(branch.get("cid", "")), 0)
        return "\n".join(lines).strip()

    @staticmethod
    def slug_term(value: str, fallback: str = "node") -> str:
        cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", str(value or ""))
        cleaned = cleaned.replace("_", "-").lower()
        cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-")
        if not cleaned:
            cleaned = fallback
        if not cleaned[0].isalpha():
            cleaned = f"n-{cleaned}"
        return cleaned

    @staticmethod
    def join_cid(parent: str, child: str) -> str:
        return f"{parent}.{child}" if parent else child

    @staticmethod
    def parent_cid(cid: str) -> str:
        if "." not in cid:
            return ""
        return cid.rsplit(".", 1)[0]

    def unique_child_cid(self, parent_cid: str, segment: str, used: set[str]) -> str:
        base = self.join_cid(parent_cid, segment)
        candidate = base
        suffix = 2
        while candidate in used:
            candidate = f"{base}-{suffix}"
            suffix += 1
        used.add(candidate)
        return candidate

    @staticmethod
    def extract_keywords(*values: str, limit: int = 8) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for value in values:
            for token in re.findall(r"[A-Za-z0-9]+", str(value or "").lower()):
                if len(token) < 3 or token in GENERIC_TERMS:
                    continue
                if token in seen:
                    continue
                seen.add(token)
                result.append(token)
                if len(result) >= limit:
                    return result
        return result

    def node_to_dict(self, node: TreeNode) -> dict:
        payload = {"id": node.id, "name": node.name, "description": node.description}
        if node.select_when:
            payload["select_when"] = node.select_when
        if node.dont_select_when:
            payload["dont_select_when"] = node.dont_select_when
        child_nodes = list(node.children)
        if child_nodes:
            payload["children"] = []
            for child in child_nodes:
                payload["children"].append(self.node_to_dict(child))
        skill_items = list(node.skills)
        if skill_items:
            payload["skills"] = [skill.to_dict() for skill in skill_items]
        return payload

    @staticmethod
    def routing_text_for_node(node: dict) -> str:
        description = str(node.get("description") or "").strip()
        select_when = str(node.get("select_when") or "").strip()
        dont_select_when = str(node.get("dont_select_when") or "").strip()
        parts = [description] if description else []
        if select_when:
            parts.append(f"Select when: {select_when}")
        if dont_select_when:
            parts.append(f"Don't select when: {dont_select_when}")
        return "\n".join(parts).strip()

    def write_yaml(self, tree_dict: dict) -> None:
        output_path = self._builder.output_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if yaml is not None:
            with open(output_path, "w", encoding="utf-8") as f:
                yaml.dump(
                    tree_dict,
                    f,
                    allow_unicode=True,
                    default_flow_style=False,
                    sort_keys=False,
                    width=120,
                )
            return
        try:
            from ruamel.yaml import YAML
        except ModuleNotFoundError as exc:
            raise RuntimeError("A YAML writer is required. Install PyYAML (`yaml`) or `ruamel.yaml`.") from exc
        yml = YAML()
        yml.default_flow_style = False
        yml.allow_unicode = True
        with open(output_path, "w", encoding="utf-8") as f:
            yml.dump(tree_dict, f)

    def print_tree(self, tree_dict: dict) -> None:
        total_skills = self.count_skills_in_dict(tree_dict)
        root_label = tree_dict.get("name", "Skill Tree")
        rich_tree = RichTree(f"[bold]{root_label}[/bold] ({total_skills} skills)")
        self._populate_rich_branch(rich_tree, list(tree_dict.get("children", [])))
        console.print(rich_tree)

    def add_node_to_rich_tree(self, parent_branch, node_dict: dict) -> None:
        descendants = self.count_skills_in_dict(node_dict)
        branch = parent_branch.add(self._branch_label(node_dict, descendants))
        self._populate_rich_branch(branch, list(node_dict.get("children", [])))
        self._render_skill_preview(branch, list(node_dict.get("skills", [])))

    def _populate_rich_branch(self, branch, child_nodes: list[dict]) -> None:
        for child in child_nodes:
            self.add_node_to_rich_tree(branch, child)

    @staticmethod
    def _branch_label(node_dict: dict, skill_count: int) -> str:
        node_id = node_dict.get("id", "unknown")
        tone = "yellow" if node_dict.get("children") else "green"
        return f"[{tone}]{node_id}[/{tone}] ({skill_count} skills)"

    @staticmethod
    def _render_skill_preview(branch, skills: list[dict]) -> None:
        preview = skills[:3]
        for skill in preview:
            branch.add(f"[blue]{skill['id']}[/blue]")
        extra = len(skills) - len(preview)
        if extra > 0:
            branch.add(f"[dim]... +{extra} more[/dim]")

    @staticmethod
    def count_skills_in_dict(node_dict: dict) -> int:
        total = 0
        stack = [node_dict]
        while stack:
            current = stack.pop()
            total += len(current.get("skills", []))
            stack.extend(list(current.get("children", [])))
        return total
