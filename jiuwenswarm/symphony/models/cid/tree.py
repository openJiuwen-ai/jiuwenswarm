from __future__ import annotations

import logging
import re
from dataclasses import replace
from importlib import import_module
from typing import Any, Iterable, List, Tuple

from orchestration.utils.naming import fuzzy_name_distance, normalize_name_key, to_pascal_case
from .node_spec import CID, NodeSpec, NodeType


class NodeError(Exception):
    pass


class ParseError(NodeError):
    pass


class InvalidPathError(NodeError):
    pass


class CIDTree:
    _CID_PATTERN = re.compile(r"([A-Za-z][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]+)*)")
    _PLACEHOLDER_TOKENS = {"CID", "MESSAGE", "PATH", "NODE"}

    def __init__(
        self,
        *,
        nodes: dict[str, NodeSpec] | None = None,
        routing_policy: str = "",
        tree_sketch: str = "",
    ) -> None:
        self._nodes: dict[str, NodeSpec] = dict(nodes or {})
        self._children: dict[str, list[str]] = {}
        self._routing_policy = routing_policy
        self._tree_sketch = tree_sketch
        self._leaf_count_cache: dict[str, int] = {}
        self._subtree_cache: dict[tuple[str, int], str] = {}
        self.rebuild()

    @classmethod
    def from_yaml(cls, preset: str, logger: logging.Logger | None = None) -> "CIDTree":
        yaml = cls._require_yaml_module()
        data = yaml.safe_load(preset) or {}
        if not isinstance(data, dict):
            raise ValueError(
                "Preset content must deserialize to a mapping\
                with optional 'routing_policy', 'tree_sketch', and 'nodes' fields"
            )

        nodes = data.get("nodes", [])
        if not isinstance(nodes, list):
            raise ValueError("Preset 'nodes' must be a list")

        cid_tree = cls(
            routing_policy=cls._render_routing_policy(data.get("routing_policy")),
            tree_sketch=cls._render_tree_sketch(data.get("tree_sketch")),
        )
        for item in nodes:
            if not isinstance(item, dict):
                raise ValueError("Each node entry must be a mapping")
            spec = cid_tree._spec_from_definition(item)
            cid_tree.register(spec)

        for spec in list(cid_tree.all()):
            cid_tree._ensure_ancestors(spec.cid)

        cid_tree.rebuild()
        cid_tree._refresh_branch_descriptions()
        cid_tree.rebuild()

        if logger is not None:
            logger.info("Loaded %d nodes", len(list(cid_tree.all())))
        return cid_tree

    @property
    def routing_policy(self) -> str:
        return self._routing_policy

    @property
    def tree_sketch(self) -> str:
        return self._tree_sketch

    @staticmethod
    def _require_yaml_module():
        try:
            return import_module("yaml")
        except ModuleNotFoundError as exc:
            raise ModuleNotFoundError(
                "PyYAML is required to load orchestrator presets. Install the `yaml` package dependency first."
            ) from exc

    @classmethod
    def normalize_cid_str(cls, value: str) -> str:
        value = (value or "").strip()
        if value.startswith("<") and value.endswith(">") and len(value) > 2:
            value = value[1:-1].strip()
        return value

    @classmethod
    def parse_cid_message(cls, text: str) -> Tuple[str, str]:
        if not text or not text.strip():
            raise ParseError("Empty response")
        lines = text.strip().splitlines()
        for idx, line in enumerate(lines):
            if ":" not in line:
                continue
            left, right = line.split(":", 1)
            left = left.strip()
            if left.lower().startswith("to "):
                left = left[3:].strip()
            left = cls.normalize_cid_str(left)
            right = right.rstrip()
            if not left:
                continue
            if left.upper() in cls._PLACEHOLDER_TOKENS:
                continue
            if not cls._CID_PATTERN.fullmatch(left):
                continue
            rest = lines[idx + 1:] if idx + 1 < len(lines) else []
            message = right.strip()
            if rest:
                message = (message + "\n" + "\n".join(rest)).strip()
            return left, message
        raise ParseError("No 'To <CID>: <Instruction>' pattern found")

    @classmethod
    def extract_cid_from_text(cls, text: str) -> str | None:
        if not text:
            return None
        prefixed = re.search(r"\bTo\s+([A-Za-z][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]+)*)", text)
        if prefixed:
            candidate = cls.normalize_cid_str(prefixed.group(1))
            if candidate.upper() not in cls._PLACEHOLDER_TOKENS:
                return candidate
        match = cls._CID_PATTERN.search(text)
        if not match:
            return None
        candidate = cls.normalize_cid_str(match.group(1))
        if candidate.upper() in cls._PLACEHOLDER_TOKENS or candidate.lower() == "to":
            return None
        return candidate

    @staticmethod
    def parse_cid(value: str) -> CID:
        return CID.from_str(value)

    @staticmethod
    def _normalize_name_token(value: str) -> str:
        return normalize_name_key(value)

    @staticmethod
    def _display_name(spec: NodeSpec) -> str:
        fallback = spec.cid.terms[-1] if spec.cid.terms else "ROOT"
        name = str(spec.name or "").strip()
        return name if name else fallback

    def _spec_lookup_keys(self, spec: NodeSpec) -> set[str]:
        keys: set[str] = set()
        display_name = self._display_name(spec)
        if display_name:
            keys.add(self._normalize_name_token(display_name))
        cid_str = spec.cid.to_str()
        if cid_str:
            keys.add(self._normalize_name_token(cid_str))
        term = spec.cid.terms[-1] if spec.cid.terms else ""
        if term:
            keys.add(self._normalize_name_token(term))
        worker_id = str(spec.worker_id or "").strip()
        if worker_id:
            keys.add(self._normalize_name_token(worker_id))
        return {key for key in keys if key}

    def _match_specs_by_name(self, specs: Iterable[NodeSpec], raw_name: str) -> list[NodeSpec]:
        query_key = self._normalize_name_token(raw_name)
        if not query_key:
            return []

        exact_matches: list[NodeSpec] = []
        fuzzy_matches: list[tuple[int, NodeSpec]] = []
        for spec in specs:
            keys = self._spec_lookup_keys(spec)
            if not keys:
                continue
            if query_key in keys:
                exact_matches.append(spec)
                continue
            best_distance: int | None = None
            for key in keys:
                distance = fuzzy_name_distance(query_key, key)
                if distance is None:
                    continue
                if best_distance is None or distance < best_distance:
                    best_distance = distance
            if best_distance is not None:
                fuzzy_matches.append((best_distance, spec))

        if exact_matches:
            return self._dedupe_specs(exact_matches)
        if not fuzzy_matches:
            return []
        best_distance = min(distance for distance, _spec in fuzzy_matches)
        return self._dedupe_specs([spec for distance, spec in fuzzy_matches if distance == best_distance])

    @staticmethod
    def _dedupe_specs(specs: Iterable[NodeSpec]) -> list[NodeSpec]:
        ordered: list[NodeSpec] = []
        seen: set[str] = set()
        for spec in specs:
            cid = spec.cid.to_str()
            if cid in seen:
                continue
            seen.add(cid)
            ordered.append(spec)
        return ordered

    def resolve_cid_from_name_path(self, raw_path: str) -> str | None:
        text = str(raw_path or "").strip().strip("`").strip("<>").strip()
        if not text:
            return None
        if text.upper().startswith("ROOT."):
            text = text[5:].strip()
        if text.upper() == "ROOT":
            return ""

        # Direct CID fast-path.
        try:
            cid = CID.from_str(text)
            if self.exists(cid):
                return cid.to_str()
        except ValueError as e:
            if logger is not None:
                logger.debug("Direct CID parsing failed for '%s': %s", text, str(e))

        segments = [segment.strip() for segment in text.split(".") if segment.strip()]
        if not segments:
            return None
        if self._normalize_name_token(segments[0]) == "root":
            segments = segments[1:]
        if not segments:
            return ""

        current = CID(())
        for segment in segments:
            matches = self._match_specs_by_name(self.get_children(current), segment)
            if len(matches) != 1:
                return None
            current = matches[0].cid
        return current.to_str()

    def resolve_leaf_cids_from_name(self, raw_name: str) -> list[str]:
        leaf_specs = [spec for spec in self.all() if spec.node_type in (NodeType.LEAF, NodeType.SYSTEM)]
        return sorted(spec.cid.to_str() for spec in self._match_specs_by_name(leaf_specs, raw_name))

    def register(self, spec: NodeSpec) -> None:
        self._nodes[spec.cid.to_str()] = spec
        self._add_node(spec)

    def update(self, spec: NodeSpec) -> None:
        self._nodes[spec.cid.to_str()] = spec
        self.rebuild()

    def rebuild(self) -> None:
        self._children = {}
        for spec in self._nodes.values():
            self._add_node(spec)
        self._leaf_count_cache = {}
        self._subtree_cache = {}

    def get(self, cid: str | CID) -> NodeSpec | None:
        return self._nodes.get(self._coerce_cid(cid).to_str())

    def exists(self, cid: str | CID) -> bool:
        return self._coerce_cid(cid).to_str() in self._nodes

    def all(self) -> Iterable[NodeSpec]:
        return self._nodes.values()

    def get_children(self, cid: str | CID) -> List[NodeSpec]:
        parent = self._coerce_cid(cid)
        terms = self._children.get(parent.to_str(), [])
        children: List[NodeSpec] = []
        for term in terms:
            spec = self.get(parent.child(term))
            if spec is not None:
                children.append(spec)
        return children

    def validate_child(self, parent_cid: str | CID, term: str) -> bool:
        return term in self._children.get(self._coerce_cid(parent_cid).to_str(), [])

    def is_leaf(self, cid: str | CID) -> bool:
        spec = self.get(cid)
        if spec is None:
            return False
        return spec.node_type in (NodeType.LEAF, NodeType.SYSTEM)

    def is_second_leaf(self, cid: str | CID) -> bool:
        """
        Second-leaf node: a branch whose direct children are all leaf/system nodes.
        """
        node_cid = self._coerce_cid(cid)
        spec = self.get(node_cid)
        if spec is None or self.is_leaf(node_cid):
            return False
        children = self.get_children(node_cid)
        if not children:
            return False
        return all(self.is_leaf(child.cid) for child in children)

    def leaf_count(self, cid: str | CID) -> int:
        node_cid = self._coerce_cid(cid)
        key = node_cid.to_str()
        cached = self._leaf_count_cache.get(key)
        if cached is not None:
            return cached
        if self.is_leaf(node_cid):
            self._leaf_count_cache[key] = 1
            return 1
        count = sum(self.leaf_count(child.cid) for child in self.get_children(node_cid))
        self._leaf_count_cache[key] = count
        return count

    def get_descendant_leaves(self, cid: str | CID) -> List[NodeSpec]:
        node_cid = self._coerce_cid(cid)
        if self.is_leaf(node_cid):
            spec = self.get(node_cid)
            return [spec] if spec is not None else []
        leaves: List[NodeSpec] = []
        for child in self.get_children(node_cid):
            leaves.extend(self.get_descendant_leaves(child.cid))
        return leaves

    def child_terms(self, cid: str | CID) -> List[str]:
        return [spec.cid.terms[-1] for spec in self.get_children(cid)]

    def serialize_subtree(self, cid: str | CID, max_depth: int = 1) -> str:
        parent_cid = self._coerce_cid(cid)
        if max_depth <= 0:
            max_depth = 1
        cache_key = (parent_cid.to_str(), int(max_depth))
        cached = self._subtree_cache.get(cache_key)
        if cached is not None:
            return cached
        children = self.get_children(parent_cid)
        if not children:
            self._subtree_cache[cache_key] = "(no children)"
            return "(no children)"
        lines: List[str] = []
        for child in children:
            self._append_subtree_prompt_lines(lines, child, remaining_depth=max_depth, indent="")
        rendered = "\n".join(lines)
        self._subtree_cache[cache_key] = rendered
        return rendered

    def serialize_root_subtree(self, max_depth: int = 1) -> str:
        return self.serialize_subtree(CID(()), max_depth)

    def serialize_name_tree(self, cid: str | CID = CID(())) -> str:
        parent_cid = self._coerce_cid(cid)
        children = self.get_children(parent_cid)
        if not children:
            return "(no children)"
        lines: List[str] = []
        for child in children:
            self._append_name_tree_lines(lines, child, indent="")
        return "\n".join(lines)

    def serialize_cid_tree(
        self,
        cid: str | CID = CID(()),
        *,
        include_user_nodes: bool = True,
        include_system_nodes: bool = True,
        draft_selection_level: str = "leaf",
    ) -> str:
        selection_level = str(draft_selection_level or "leaf").strip().lower()
        if selection_level not in {"leaf", "second_leaf"}:
            selection_level = "leaf"
        parent_cid = self._coerce_cid(cid)
        children = []

        for child in self.get_children(parent_cid):
            if self._include_in_llm_tree(
                child,
                include_user_nodes=include_user_nodes,
                include_system_nodes=include_system_nodes,
                draft_selection_level=selection_level,
            ):
                children.append(child)

        if not children:
            return "(no children)"
        lines: List[str] = []
        for child in children:
            self._append_cid_tree_lines(
                lines,
                child,
                indent="",
                include_user_nodes=include_user_nodes,
                include_system_nodes=include_system_nodes,
                draft_selection_level=selection_level,
            )
        return "\n".join(lines)

    def clip_to_valid_prefix_with_flag(self, cid: str | CID) -> tuple[CID | None, bool]:
        current = CID(())
        clipped = False
        target = self._coerce_cid(cid)
        for term in target.terms:
            if self.validate_child(current, term):
                current = current.child(term)
                continue
            clipped = True
            break
        if current.is_root() and target.terms:
            return None, True
        return current, clipped

    def analyze_invalid_path(self, cid: str | CID) -> tuple[str, str]:
        target = self._coerce_cid(cid)
        current = CID(())
        for term in target.terms:
            if self.validate_child(current, term):
                current = current.child(term)
                continue
            parent = current.to_str() or "ROOT"
            children = self.child_terms(current)
            detail = f"CID '{target.to_str()}' is invalid because '{term}' is not a child of '{parent}'."
            if children:
                hint = f"Choose an existing child under '{parent}': {', '.join(children)}."
            else:
                hint = f"Go back to '{parent}' or choose User.Chat if no worker in this branch fits."
            return detail, hint
        return "The selected CID does not exist in the current tree.", "Choose an existing node from the current tree."

    def collapse_unique_chain(self, cid: str | CID) -> CID:
        current = self._coerce_cid(cid)
        visited: set[str] = set()
        while not self.is_leaf(current):
            key = current.to_str()
            if key in visited:
                break
            visited.add(key)
            children = self.get_children(current)
            if len(children) != 1:
                break
            current = children[0].cid
        return current

    def _add_node(self, spec: NodeSpec) -> None:
        cid_str = spec.cid.to_str()
        parent = spec.cid.parent()
        parent_str = parent.to_str() if parent else ""
        self._children.setdefault(parent_str, [])
        if not spec.cid.is_root():
            term = spec.cid.terms[-1]
            if term not in self._children[parent_str]:
                self._children[parent_str].append(term)
                self._children[parent_str].sort()
        self._children.setdefault(cid_str, [])

    def _append_subtree_prompt_lines(self, lines: List[str], spec: NodeSpec, remaining_depth: int, indent: str) -> None:
        label = self._llm_tree_label(spec)
        if spec.node_type == NodeType.SYSTEM:
            kind = "system"
        elif self.is_leaf(spec.cid):
            kind = "leaf"
        else:
            kind = "branch"
        line = f"{indent}- {label} [{kind}]"
        details = [spec.description]
        if spec.keywords:
            details.append("keywords: " + ", ".join(spec.keywords[:6]))
        if not self.is_leaf(spec.cid):
            details.append(f"descendant_leaves={self.leaf_count(spec.cid)}")
        line += ": " + " | ".join(detail for detail in details if detail)
        lines.append(line)
        if remaining_depth <= 1 or self.is_leaf(spec.cid):
            return
        for child in self.get_children(spec.cid):
            self._append_subtree_prompt_lines(lines, child, remaining_depth - 1, indent + "  ")

    def _append_name_tree_lines(self, lines: List[str], spec: NodeSpec, indent: str) -> None:
        label = self._display_name(spec)
        description = " ".join(str(spec.description or "").split())
        node_kind = "worker" if self.is_leaf(spec.cid) else "category"
        line = f"{indent}- {label} [{node_kind}]"
        if description and self.is_leaf(spec.cid):
            line += f": {description}"
        lines.append(line)
        if self.is_leaf(spec.cid):
            return
        for child in self.get_children(spec.cid):
            self._append_name_tree_lines(lines, child, indent + "  ")

    def _append_cid_tree_lines(
        self,
        lines: List[str],
        spec: NodeSpec,
        indent: str,
        *,
        include_user_nodes: bool,
        include_system_nodes: bool,
        draft_selection_level: str,
    ) -> None:
        label = self._llm_tree_label(spec)
        description = " ".join(str(spec.description or "").split())
        selection_level = str(draft_selection_level or "leaf").strip().lower()
        is_terminal = self.is_second_leaf(spec.cid) if selection_level == "second_leaf" else self.is_leaf(spec.cid)
        node_kind = "worker" if is_terminal else "category"
        line = f"{indent}- {label} [{node_kind}]"
        if description and is_terminal:
            line += f": {description}"
        lines.append(line)
        if is_terminal:
            return
        for child in self.get_children(spec.cid):
            if not self._include_in_llm_tree(
                child,
                include_user_nodes=include_user_nodes,
                include_system_nodes=include_system_nodes,
                draft_selection_level=selection_level,
            ):
                continue
            self._append_cid_tree_lines(
                lines,
                child,
                indent + "  ",
                include_user_nodes=include_user_nodes,
                include_system_nodes=include_system_nodes,
                draft_selection_level=selection_level,
            )

    @staticmethod
    def _include_in_llm_tree(
        spec: NodeSpec,
        *,
        include_user_nodes: bool,
        include_system_nodes: bool,
        draft_selection_level: str = "leaf",
    ) -> bool:
        selection_level = str(draft_selection_level or "leaf").strip().lower()
        cid_str = spec.cid.to_str()
        if cid_str == "Guard":
            return False
        if cid_str.startswith("User."):
            return include_user_nodes
        if selection_level == "second_leaf" and spec.node_type == NodeType.LEAF:
            # In second-leaf drafting mode, real leaf nodes are hidden.
            return False
        if spec.node_type == NodeType.SYSTEM:
            return include_system_nodes
        return True

    def _llm_tree_label(self, spec: NodeSpec) -> str:
        if self.is_leaf(spec.cid):
            worker_id = str(spec.worker_id or "").strip()
            if worker_id:
                return to_pascal_case(worker_id)
        term = spec.cid.terms[-1] if spec.cid.terms else "ROOT"
        return to_pascal_case(term) or term

    def _ensure_ancestors(self, cid: CID) -> None:
        for i in range(1, len(cid.terms)):
            ancestor = CID(tuple(cid.terms[:i]))
            if self.exists(ancestor):
                continue
            self.register(
                NodeSpec(
                    cid=ancestor,
                    name=ancestor.terms[-1] if ancestor.terms else "ROOT",
                    description="",
                    node_type=NodeType.BRANCH,
                    worker_id=None,
                )
            )

    def _refresh_branch_descriptions(self) -> None:
        branch_specs = sorted(
            [spec for spec in self.all() if spec.node_type == NodeType.BRANCH],
            key=lambda item: len(item.cid.terms),
        )
        for spec in branch_specs:
            if spec.description.strip():
                continue
            title = spec.cid.terms[-1] if spec.cid.terms else "Root"
            description = f"Semantic branch for {title}"
            self.update(replace(spec, description=description, keywords=spec.keywords))

    @classmethod
    def _render_routing_policy(cls, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, list):
            lines = [str(item).strip() for item in value if str(item).strip()]
            if not lines:
                return ""
            return "\n".join(f"- {line}" for line in lines)
        raise ValueError("routing_policy must be a string or a list of strings")

    @classmethod
    def _render_tree_sketch(cls, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, list):
            lines = [str(item).rstrip() for item in value if str(item).strip()]
            return "\n".join(lines).strip()
        raise ValueError("tree_sketch must be a string or a list of strings")

    @staticmethod
    def _sanitize_cid_value(value: str) -> str:
        parts = [part.strip() for part in str(value or "").split(".") if part.strip()]
        normalized: list[str] = []
        for part in parts:
            cleaned = re.sub(r"[^A-Za-z0-9_-]+", "", part)
            if not cleaned:
                cleaned = "Node"
            if not cleaned[0].isalpha():
                cleaned = "N" + cleaned
            normalized.append(cleaned)
        return ".".join(normalized)

    def _spec_from_definition(self, item: dict[str, Any]) -> NodeSpec:
        if "cid" not in item:
            raise ValueError("Each node must define a cid")
        if "type" not in item:
            raise ValueError(f"Node '{item.get('cid', '<unknown>')}' must define a type")
        raw_cid = str(item["cid"])
        try:
            cid = CID.from_str(raw_cid)
        except ValueError:
            cid = CID.from_str(self._sanitize_cid_value(raw_cid))
        if self.exists(cid):
            raise ValueError(f"Duplicate cid found in preset: {cid.to_str()}")
        node_type = NodeType(str(item["type"]))
        description = str(item.get("description", "") or "").strip()
        name = str(item.get("name", "") or "").strip()
        raw_worker_id = item.get("worker_id")
        worker_id = str(raw_worker_id).strip() if raw_worker_id is not None else None
        if node_type == NodeType.LEAF and not worker_id:
            raise ValueError(f"Leaf node '{cid.to_str()}' must define a non-empty worker_id")
        if node_type == NodeType.SYSTEM and not worker_id:
            raise ValueError(f"System node '{cid.to_str()}' must define a non-empty worker_id")
        if node_type == NodeType.BRANCH and worker_id:
            raise ValueError(f"Branch node '{cid.to_str()}' must not define worker_id")
        return NodeSpec(
            cid=cid,
            name=name or (cid.terms[-1] if cid.terms else "ROOT"),
            description=description,
            node_type=node_type,
            worker_id=worker_id,
            keywords=tuple(item.get("keywords", []) or []),
            examples=tuple(item.get("examples", []) or []),
        )

    @staticmethod
    def _coerce_cid(cid: str | CID) -> CID:
        if isinstance(cid, CID):
            return cid
        return CID.from_str(cid)
