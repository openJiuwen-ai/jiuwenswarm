"""Agentic skill-tree disclosure toolkit.

This module intentionally builds on the existing symphony/dispatch index and
disclosure primitives. It does not call an LLM; the calling agent decides which
tree branches to inspect next.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import RLock
from time import monotonic
from typing import Any

from jiuwenswarm.symphony.skill_retrieval.config import RetrieveSettings, load_settings
from jiuwenswarm.symphony.skill_retrieval.dispatch_imports import dispatch_import_path
from jiuwenswarm.symphony.skill_retrieval.index_service import SkillIndexService
from jiuwenswarm.symphony.skill_retrieval.markdown import render_disabled
from jiuwenswarm.symphony.skill_retrieval.api import build_skill_index as build_skill_index_blocking

from .tool_result import AgenticToolResult


@dataclass(frozen=True)
class _NodeStats:
    branch_count: int
    skill_count: int


@dataclass(frozen=True)
class _DescriptionParts:
    description: str = ""
    select_when: str = ""
    dont_select_when: str = ""


@dataclass(frozen=True)
class _SkillEntry:
    label: str
    worker_id: str
    description: str
    skill_md_path: str


@dataclass(frozen=True)
class _ToolkitCacheEntry:
    toolkit: "AgenticRetrievalToolKit"


@dataclass(frozen=True)
class _StatusCacheEntry:
    created_at: float
    status: dict[str, Any]


@dataclass(frozen=True)
class _ReadyStatusCacheKey:
    skills_dir: str
    artifact_root: str
    build: tuple[tuple[str, str], ...]
    llm: tuple[tuple[str, str], ...]
    index_artifacts: tuple[Any, ...]
    state_file: tuple[int, int] | None


@dataclass(frozen=True)
class _SettingsCacheEntry:
    key: tuple[Any, ...]
    settings: Any


_INDEX_FILENAMES = ("tree_index.yaml", "catalog.jsonl", "manifest.json")
_STATE_FILENAME = "state.json"
_SETTINGS_ENV_KEYS = (
    "SYMPHONY_SKILL_RETRIEVAL_ENABLED",
    "SYMPHONY_SKILL_RETRIEVAL_ROOT",
    "MODEL_NAME",
    "API_KEY",
    "API_BASE",
)
_READY_STATUS_TTL_SECONDS = 30.0
_SETTINGS_CACHE: _SettingsCacheEntry | None = None
_TOOLKIT_CACHE: dict[tuple[Any, ...], _ToolkitCacheEntry] = {}
_READY_STATUS_CACHE: dict[_ReadyStatusCacheKey, _StatusCacheEntry] = {}
_CACHE_LOCK = RLock()


class AgenticRetrievalToolKit:
    """Expose indexed skill-tree disclosure operations for an agent."""

    def __init__(
        self,
        *,
        loaded_index: Any,
        progressive_config: Any,
        top_k: int,
        visible_skill_names: set[str] | frozenset[str] | None = None,
    ) -> None:
        self._loaded_index = loaded_index
        self._root = loaded_index.tree_root
        self._progressive_config = progressive_config
        self._top_k = max(1, int(top_k))
        self._visible_skill_names = _normalize_visible_skill_names(visible_skill_names)
        self._node_by_id: dict[str, Any] = {}
        self._path_by_id: dict[str, tuple[str, ...]] = {}
        self._stats_by_id: dict[str, _NodeStats] = {}
        self._leaf_ids: set[str] = set()
        self._catalog_by_payload: dict[str, Any] = {}
        for record in tuple(getattr(loaded_index, "catalog_records", ()) or ()):
            payload = str(getattr(record, "payload", "") or "").strip()
            if payload:
                self._catalog_by_payload[payload] = record
            worker_id = str(getattr(record, "worker_id", "") or "").strip()
            name = str(getattr(record, "name", "") or "").strip()
            if self._is_visible_skill(payload, worker_id, name):
                for value in (payload, worker_id, name):
                    if value:
                        self._leaf_ids.add(value)
        self._index_nodes(self._root, ("ROOT",))
        self._analyze_node(self._root)

    @classmethod
    def from_index(
        cls,
        index_dir: str | Path,
        *,
        retrieve_settings: RetrieveSettings | None = None,
        visible_skill_names: set[str] | frozenset[str] | None = None,
    ) -> "AgenticRetrievalToolKit":
        """Load an existing symphony skill index from *index_dir*."""
        with dispatch_import_path():
            from retrieval.io.loading import load_retriever_index

            loaded_index = load_retriever_index(index_dir)
            progressive_config, top_k = _progressive_config_from_settings(retrieve_settings)
        return cls(
            loaded_index=loaded_index,
            progressive_config=progressive_config,
            top_k=top_k,
            visible_skill_names=visible_skill_names,
        )

    def skill_branch_peek(self, node_ids: Sequence[str]) -> dict[str, Any]:
        """Return direct child branch summaries for branches that may need inspection."""
        nodes, error = self._resolve_nodes(node_ids, default_root=True)
        if error:
            return _tool_payload(False, error)

        lines = ["# Skill Branch Peek", ""]
        steps: list[dict[str, Any]] = []
        for index, node in enumerate(nodes):
            if index:
                lines.append("")
            self._render_peek_node(lines, node)
            steps.append(self._skill_tree_peek_step(order=len(steps), node=node))
        return _tool_payload(
            True,
            "\n".join(lines).rstrip(),
            skill_tree=_skill_tree_payload(
                query=_skill_tree_query("skill_branch_peek", nodes),
                steps=steps,
                candidates=[],
            ),
        )

    def skill_branch_explore(self, node_ids: Sequence[str]) -> dict[str, Any]:
        """Return the current disclosure fragment for the requested branch nodes."""
        nodes, error = self._resolve_nodes(node_ids)
        if error:
            return _tool_payload(False, error)
        root_nodes = [
            str(getattr(node, "node_id", "") or "ROOT").strip() or "ROOT"
            for node in nodes
            if (str(getattr(node, "node_id", "") or "ROOT").strip() or "ROOT") == "ROOT"
        ]
        if root_nodes:
            return _tool_payload(
                False,
                "# Skill Tree Node Error\n\n"
                "`ROOT` is already summarized by the first-level categories in the system prompt. "
                "Choose one or more of those category ids for `skill_branch_explore`. "
                "Use `skill_branch_peek` with `ROOT` only if the listed categories are insufficient.",
            )

        with dispatch_import_path():
            from retrieval.tree.subtree import DefaultCurrentSubtreeProvider
            from retrieval.tree.types import SearchCursor

            provider = DefaultCurrentSubtreeProvider(
                config=self._progressive_config,
                subtree_item_count=lambda current: self._analyze_node(current).skill_count,
                cache={},
                cache_lock=None,
            )

            lines = ["# Skill Branch Explore", ""]
            steps: list[dict[str, Any]] = []
            candidates: list[dict[str, Any]] = []
            for index, node in enumerate(nodes):
                if index:
                    lines.append("")
                branch_path = self._path_by_id.get(str(node.node_id), ("ROOT",))
                subtree = provider.get_current_subtree(
                    cursor=SearchCursor(
                        node=node,
                        depth=max(0, len(branch_path) - 1),
                        branch_path=branch_path,
                        top_k=self._top_k,
                    )
                )
                self._render_expand_node(lines, node, subtree.fragment)
                fragment_steps, fragment_candidates = self._skill_tree_explore_fragment(
                    node=node,
                    fragment=subtree.fragment,
                    start_order=len(steps),
                    start_rank=len(candidates) + 1,
                )
                steps.extend(fragment_steps)
                candidates.extend(fragment_candidates)
        return _tool_payload(
            True,
            "\n".join(lines).rstrip(),
            skill_tree=_skill_tree_payload(
                query=_skill_tree_query("skill_branch_explore", nodes),
                steps=steps,
                candidates=candidates,
            ),
        )

    def root_prompt_markdown(self, *, language: str = "cn", max_children: int = 30) -> str:
        """Render a compact system-prompt section for first-level categories."""
        if language == "en":
            lines = [
                "# Agentic Skill Retrieval",
                "",
                "Agentic retrieval for installed skills is enabled. For tasks that may need skills, avoid "
                "relying on a full injected skill list.",
                "",
                "`skill_branch_explore` and `skill_branch_peek` are skill-directory browsing tools. They "
                "disclose categories and installed skill entries.",
                "Use the directory results to shortlist skills; read a returned SKILL.md only after the skill "
                "looks likely needed.",
                "For a task that clearly needs installed skills, do not return a final answer with no relevant "
                "skills before using at least one directory tool.",
                "",
                "When using the skill directory, first identify these possible branches from the task:",
                "1. Main capability branch",
                "2. Input format branch",
                "3. Output format branch",
                "4. Validation/evaluation/test branch",
                "5. Required execution environment branch",
                "",
                "Prioritize exploring the main capability branch. Explore supporting branches only when a task "
                "constraint would change skill selection, execution strategy, or acceptance criteria.",
                "Input/output forms, validation requirements, execution constraints, and dependency conditions "
                "can all be task constraints.",
                "Do not expand extra branches for background details that do not affect skill selection.",
                "When terminal skills include both broad overview skills and exact task-specific skills, prefer "
                "the exact skills.",
                "Use broad overview skills only as supplements when they add necessary context.",
                "",
                "The first-level categories below are already visible. Start by selecting the most relevant "
                "category id from this list and call `skill_branch_explore` on that category.",
                "Use the backticked category id as `node_ids`; never use a display number as `node_ids`.",
                "Do not call `skill_branch_explore` with `ROOT` as the first step.",
                "When `skill_branch_explore` returns a `skills` section, those entries are installed skills "
                "rather than branch ids.",
                "Use Name and Description to shortlist them, and read SKILL.md only after choosing a skill as "
                "likely useful.",
                "Use `skill_branch_peek` with `ROOT` only if you cannot decide from the listed categories.",
                "If either tool says the index is missing or stale, call `skill_index_build` once and retry.",
                "",
                "## First-Level Categories",
            ]
        else:
            lines = [
                "# Agentic 技能检索",
                "",
                "已启用适用于较大规模已安装技能场景下的 Agentic 技能检索。处理可能需要技能的任务时，不要依赖全量技能列表注入。",
                "",
                "`skill_branch_explore` 和 `skill_branch_peek` 是技能目录查阅工具。"
                "它们只披露分类和已安装技能条目；请先根据目录结果筛选候选技能，"
                "只有当某个技能看起来确实需要使用时，才读取其 SKILL.md。",
                "对于明显需要已安装技能的任务，不要在未使用至少一个目录工具前直接返回“没有相关技能”或空技能结果。",
                "",
                "使用技能目录时，先根据任务识别：",
                "1. 主能力分支",
                "2. 输入格式分支",
                "3. 输出格式分支",
                "4. 验证/评估/测试分支",
                "5. 必要的执行环境分支",
                "",
                "优先探索主能力分支；只有当某个任务约束会改变技能选择、执行方案或验收标准时，才补充探索对应辅助分支。",
                "输入/输出形式、验证要求、执行约束、依赖条件等都可以作为任务约束；对只提供背景且不影响技能选择的信息，不要额外展开分支。",
                "当叶子技能中同时出现宽泛概览技能和精确任务技能时，优先选择精确技能；宽泛概览技能只在需要补充必要背景时使用。",
                "",
                "下面已经给出第一层分类。请先从这些分类中选择最相关的分类 id，"
                "并对该分类调用 `skill_branch_explore`。",
                "调用工具时必须使用反引号中的分类 id，不要把展示序号当作 `node_ids`。",
                "不要把 `ROOT` 作为首轮 `skill_branch_explore` 的输入。",
                "当 `skill_branch_explore` 返回 `skills` 小节时，其中条目是已安装技能而不是 branch id。",
                "先根据 Name 和 Description 筛选，只有当某个技能看起来需要使用时，"
                "才读取其 SKILL.md，不要继续展开技能名。",
                "只有当无法根据第一层分类判断方向时，才用 `skill_branch_peek` 查看 `ROOT` 的轻量摘要。",
                "如果工具提示索引缺失或过期，调用一次 `skill_index_build` 后重试。",
                "",
                "## 第一层分类",
            ]

        children = self._visible_child_branches(self._root)
        if not children:
            lines.append("No first-level branches are available." if language == "en" else "当前索引树没有第一层分支。")
            return "\n".join(lines)

        for child in children[:max(1, int(max_children))]:
            parts = _split_description(str(getattr(child, "description", "") or ""))
            desc = _compact_branch_text(parts.description or parts.select_when, 180)
            suffix = f": {desc}" if desc else ""
            lines.append(f"- `{child.node_id}`{suffix}")
        if len(children) > max_children:
            omitted = len(children) - max_children
            lines.append(f"... {omitted} more first-level branches omitted.")
        return "\n".join(lines)

    def _resolve_nodes(self, node_ids: Sequence[str], *, default_root: bool = False) -> tuple[list[Any], str]:
        normalized = [str(item or "").strip() for item in node_ids or [] if str(item or "").strip()]
        if not normalized:
            if default_root:
                normalized = ["ROOT"]
            else:
                return [], (
                    "# Skill Tree Node Error\n\n"
                    "`node_ids` is required. Choose one or more branch ids from the first-level categories "
                    "in the system prompt."
                )

        nodes: list[Any] = []
        missing: list[str] = []
        leaf_ids: list[str] = []
        for node_id in normalized:
            node = self._node_by_id.get(node_id)
            if node is None:
                if node_id in self._leaf_ids:
                    leaf_ids.append(node_id)
                else:
                    missing.append(node_id)
            else:
                if node_id == "ROOT" and default_root:
                    nodes.append(node)
                elif self._analyze_node(node).skill_count > 0:
                    nodes.append(node)
                else:
                    missing.append(node_id)

        if leaf_ids:
            return [], (
                "# Skill Tree Node Error\n\n"
                f"{', '.join(f'`{item}`' for item in leaf_ids)} "
                "is a skill id, not a branch id. Explore its parent branch or peek `ROOT`."
            )
        if missing:
            return [], (
                "# Skill Tree Node Not Found\n\n"
                f"Unknown branch node id(s): {', '.join(f'`{item}`' for item in missing)}.\n\n"
                "Peek `ROOT` or a known branch id to inspect valid branch ids."
            )
        return nodes, ""

    def _index_nodes(self, node: Any, path: tuple[str, ...]) -> None:
        node_id = str(getattr(node, "node_id", "") or "ROOT").strip() or "ROOT"
        self._node_by_id[node_id] = node
        self._path_by_id[node_id] = path
        for child in tuple(getattr(node, "children", ()) or ()):
            child_id = str(getattr(child, "node_id", "") or "").strip()
            if child_id:
                self._index_nodes(child, path + (child_id,))

    def _analyze_node(self, node: Any) -> _NodeStats:
        node_id = str(getattr(node, "node_id", "") or "ROOT").strip() or "ROOT"
        cached = self._stats_by_id.get(node_id)
        if cached is not None:
            return cached

        items = tuple(getattr(node, "items", ()) or ())
        for item in items:
            for attr_name in ("payload", "item_id", "label"):
                value = str(getattr(item, attr_name, "") or "").strip()
                if value and self._is_visible_item(item):
                    self._leaf_ids.add(value)
        skill_count = sum(1 for item in items if self._is_visible_item(item))
        child_branch_count = 0
        for child in tuple(getattr(node, "children", ()) or ()):
            child_stats = self._analyze_node(child)
            if child_stats.skill_count > 0:
                child_branch_count += child_stats.branch_count
                skill_count += child_stats.skill_count
        branch_count = 1 + child_branch_count if skill_count > 0 else 0
        stats = _NodeStats(branch_count=branch_count, skill_count=skill_count)
        self._stats_by_id[node_id] = stats
        return stats

    def _visible_child_branches(self, node: Any) -> list[Any]:
        return [
            child
            for child in list(getattr(node, "children", ()) or [])
            if self._analyze_node(child).skill_count > 0
        ]

    def _is_visible_item(self, item: Any) -> bool:
        return self._is_visible_skill(
            str(getattr(item, "payload", "") or "").strip(),
            str(getattr(item, "item_id", "") or "").strip(),
            str(getattr(item, "label", "") or "").strip(),
        )

    def _is_visible_resolution(self, resolution: Any | None) -> bool:
        if resolution is None:
            return False
        item = getattr(resolution, "item", None)
        record = None
        payload = ""
        if item is not None:
            payload = str(getattr(item, "payload", "") or "").strip()
            record = self._catalog_by_payload.get(payload)
        worker_id = str(getattr(record, "worker_id", "") if record is not None else "").strip()
        name = str(getattr(record, "name", "") if record is not None else "").strip()
        return self._is_visible_skill(
            payload,
            worker_id,
            name,
            str(getattr(item, "item_id", "") if item is not None else "").strip(),
            str(getattr(item, "label", "") if item is not None else "").strip(),
            str(getattr(resolution, "canonical_id", "") or "").strip(),
            str(getattr(resolution, "label", "") or "").strip(),
        )

    def _is_visible_skill(self, *values: str) -> bool:
        visible = self._visible_skill_names
        if visible is None:
            return True
        return any(str(value or "").strip() in visible for value in values)

    def _is_visible_exposed_branch(self, node: Any, resolution: Any | None) -> bool:
        node_id = _exposed_node_id(node, resolution)
        indexed_node = self._node_by_id.get(node_id)
        if indexed_node is None:
            return True
        return self._analyze_node(indexed_node).skill_count > 0

    def _render_peek_node(self, lines: list[str], node: Any) -> None:
        node_id = str(getattr(node, "node_id", "") or "ROOT").strip() or "ROOT"
        parts = _split_description(str(getattr(node, "description", "") or ""))
        lines.append(f"## input `{node_id}`")
        if parts.select_when:
            lines.append(f"use: {_compact(parts.select_when, 180)}")
        if parts.dont_select_when:
            lines.append(f"avoid: {_compact(parts.dont_select_when, 180)}")

        children = self._visible_child_branches(node)
        if not children:
            lines.append("No child branches.")
            return

        for index, child in enumerate(children, start=1):
            child_id = str(getattr(child, "node_id", "") or "").strip()
            child_parts = _split_description(str(getattr(child, "description", "") or ""))
            stats = self._analyze_node(child)
            desc = _compact_branch_text(child_parts.description, 180)
            suffix = f": {desc}" if desc else ""
            lines.append(f"{index}. `{child_id}`{suffix}")
            if child_parts.select_when:
                lines.append(f"   use: {_compact(child_parts.select_when, 160)}")
            if child_parts.dont_select_when:
                lines.append(f"   avoid: {_compact(child_parts.dont_select_when, 160)}")
            lines.append(
                f"   covers: {stats.branch_count} {_plural('branch', stats.branch_count)}, "
                f"{stats.skill_count} {_plural('skill', stats.skill_count)}"
            )
            if index < len(children):
                lines.append("")

    def _render_expand_node(self, lines: list[str], node: Any, fragment: Any) -> None:
        node_id = str(getattr(node, "node_id", "") or "ROOT").strip() or "ROOT"
        lines.append(f"## input `{node_id}`")
        children = tuple(getattr(fragment.root, "children", ()) or ())
        if not children:
            lines.append("No exposed branches or skills.")
            return

        code_to_resolution = getattr(fragment, "code_to_resolution", {}) or {}
        resolution_by_canonical_id = {
            str(resolution.canonical_id): resolution
            for resolution in tuple(code_to_resolution.values())
        }
        self._render_exposed_children(
            lines,
            children=children,
            resolution_by_canonical_id=resolution_by_canonical_id,
        )

    def _render_exposed_children(
        self,
        lines: list[str],
        *,
        children: Sequence[Any],
        resolution_by_canonical_id: dict[str, Any],
    ) -> None:
        terminal_children, branch_children = _classify_exposed_children(
            children,
            resolution_by_canonical_id,
        )
        terminal_children = [
            child
            for child in terminal_children
            if self._is_visible_resolution(_resolution_for_exposed(child, resolution_by_canonical_id))
        ]
        branch_children = [
            child
            for child in branch_children
            if self._is_visible_exposed_branch(child, _resolution_for_exposed(child, resolution_by_canonical_id))
        ]

        rendered = False
        if terminal_children:
            rendered = True
            lines.append("")
            lines.append("### skills")
            lines.append(
                "Candidate installed skills, not branch ids. "
                "Shortlist by Name/Description; read SKILL.md only after choosing a skill as likely useful."
            )
            lines.append("")
            for index, child in enumerate(terminal_children, start=1):
                resolution = _resolution_for_exposed(child, resolution_by_canonical_id)
                if resolution is None:
                    continue
                self._render_terminal_skill(lines, index=index, resolution=resolution)

        for child in branch_children:
            rendered = True
            resolution = _resolution_for_exposed(child, resolution_by_canonical_id)
            node_id = _exposed_node_id(child, resolution)
            description = (
                str(getattr(resolution, "description", "") or "").strip()
                if resolution is not None
                else str(getattr(child, "description", "") or "").strip()
            )
            parts = _split_description(description)
            lines.extend(["", f"### branch `{node_id}`"])
            if parts.description:
                lines.append(f"desc: {_compact_branch_text(parts.description, 160)}")
            if parts.select_when:
                lines.append(f"use: {_compact(parts.select_when, 180)}")
            if parts.dont_select_when:
                lines.append(f"avoid: {_compact(parts.dont_select_when, 180)}")
        if not rendered:
            lines.append("No exposed branches or visible skills.")

    def _render_terminal_skill(self, lines: list[str], *, index: int, resolution: Any) -> None:
        entry = self._skill_entry_from_resolution(resolution)
        if entry is None:
            return

        desc = _compact(entry.description, 240)
        lines.append(f"{index}. `{entry.label}`")
        if desc:
            lines.append(f"   - Description: {desc}")
        if entry.skill_md_path:
            lines.append(f"   - SKILL.md: `{entry.skill_md_path}`")
        else:
            lines.append("   - SKILL.md: unavailable")

    def _skill_tree_peek_step(self, *, order: int, node: Any) -> dict[str, Any]:
        node_id = str(getattr(node, "node_id", "") or "ROOT").strip() or "ROOT"
        children = tuple(self._visible_child_branches(node))
        return {
            "order": order,
            "event_type": "fragment_built",
            "node_id": node_id,
            "label": _humanize_node_id(node_id),
            "depth": self._node_depth(node_id),
            "selectable_count": len(children),
            "selected": [],
            "branches": [_named_id(str(getattr(child, "node_id", "") or "")) for child in children],
            "leaves": [],
            "candidate_count": 0,
        }

    def _skill_tree_explore_fragment(
        self,
        *,
        node: Any,
        fragment: Any,
        start_order: int,
        start_rank: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        node_id = str(getattr(node, "node_id", "") or "ROOT").strip() or "ROOT"
        depth = self._node_depth(node_id)
        children = tuple(getattr(fragment.root, "children", ()) or ())
        resolution_by_canonical_id = {
            str(resolution.canonical_id): resolution
            for resolution in tuple((getattr(fragment, "code_to_resolution", {}) or {}).values())
        }
        terminal_children, branch_children = _classify_exposed_children(
            children,
            resolution_by_canonical_id,
        )
        terminal_children = [
            child
            for child in terminal_children
            if self._is_visible_resolution(_resolution_for_exposed(child, resolution_by_canonical_id))
        ]
        branch_children = [
            child
            for child in branch_children
            if self._is_visible_exposed_branch(child, _resolution_for_exposed(child, resolution_by_canonical_id))
        ]
        branch_ids = [
            _named_id(_exposed_node_id(child, _resolution_for_exposed(child, resolution_by_canonical_id)))
            for child in branch_children
        ]
        leaf_ids = [
            _named_id(_terminal_skill_id(_resolution_for_exposed(child, resolution_by_canonical_id)))
            for child in terminal_children
        ]
        candidates = [
            self._skill_tree_candidate(
                rank=start_rank + index,
                resolution=_resolution_for_exposed(child, resolution_by_canonical_id),
                source_node_id=node_id,
            )
            for index, child in enumerate(terminal_children)
        ]
        candidates = [candidate for candidate in candidates if candidate is not None]

        steps = [
            {
                "order": start_order,
                "event_type": "fragment_built",
                "node_id": node_id,
                "label": _humanize_node_id(node_id),
                "depth": depth,
                "selectable_count": len(children),
                "selected": [],
                "branches": branch_ids,
                "leaves": leaf_ids,
                "candidate_count": len(candidates),
            }
        ]
        if branch_ids or leaf_ids:
            steps.append(
                {
                    "order": start_order + 1,
                    "event_type": "fragment_continue",
                    "node_id": node_id,
                    "label": _humanize_node_id(node_id),
                    "depth": depth + 1,
                    "selectable_count": len(children),
                    "selected": branch_ids,
                    "branches": branch_ids,
                    "leaves": leaf_ids,
                    "candidate_count": len(candidates),
                }
            )
        if candidates:
            steps.append(
                {
                    "order": start_order + 2,
                    "event_type": "search_complete",
                    "node_id": node_id,
                    "label": _humanize_node_id(node_id),
                    "depth": depth,
                    "selectable_count": len(children),
                    "selected": [],
                    "branches": branch_ids,
                    "leaves": leaf_ids,
                    "candidate_count": len(candidates),
                }
            )
        return steps, candidates

    def _skill_tree_candidate(
        self,
        *,
        rank: int,
        resolution: Any | None,
        source_node_id: str,
    ) -> dict[str, Any] | None:
        if resolution is None:
            return None
        entry = self._skill_entry_from_resolution(resolution)
        if entry is None or not entry.label:
            return None
        return {
            "rank": rank,
            "label": entry.label,
            "worker_id": entry.worker_id or entry.label,
            "description": _compact(entry.description, 180),
            "path": list(self._path_by_id.get(source_node_id, ("ROOT",))) + [entry.label],
            "selected": True,
            "source": "skill_branch_explore",
        }

    def _node_depth(self, node_id: str) -> int:
        return max(0, len(self._path_by_id.get(node_id, ("ROOT",))) - 1)

    def _skill_entry_from_resolution(self, resolution: Any) -> _SkillEntry | None:
        item = getattr(resolution, "item", None)
        payload = str(getattr(item, "payload", "") or getattr(resolution, "canonical_id", "") or "").strip()
        record = self._catalog_by_payload.get(payload)
        metadata = getattr(record, "metadata", {}) if record is not None else {}
        metadata = metadata if isinstance(metadata, dict) else {}
        worker_id = str(
            getattr(record, "worker_id", "")
            or metadata.get("worker_id")
            or getattr(item, "item_id", "")
            or payload
        ).strip()
        name = str(
            getattr(record, "name", "")
            or getattr(item, "label", "")
            or getattr(resolution, "label", "")
            or worker_id
        ).strip()
        description = str(
            getattr(item, "description", "")
            or getattr(resolution, "description", "")
            or getattr(record, "description", "")
            or ""
        ).strip()
        label = name or worker_id or payload
        if not label:
            return None
        return _SkillEntry(
            label=label,
            worker_id=worker_id,
            description=_split_description(description).description,
            skill_md_path=_skill_md_path(metadata),
        )


def build_skill_index(manager: Any | None = None) -> dict[str, Any]:
    """Build or reuse the installed-skill retrieval index before the agent continues."""
    resolved_manager = _resolve_manager(manager)
    _clear_runtime_caches()
    payload = build_skill_index_blocking(resolved_manager, force=False, source="tool")
    _clear_runtime_caches()
    return _tool_payload(
        bool(payload.get("success")),
        _agentic_build_result(str(payload.get("result") or "")),
    )


def is_agentic_retrieval_enabled() -> bool:
    """Return whether agentic installed-skill retrieval is enabled."""
    try:
        return bool(_cached_settings().enabled)
    except Exception:
        return False


def skill_branch_peek(node_ids: Sequence[str], manager: Any | None = None) -> dict[str, Any]:
    """Peek child branch summaries from the current skill tree index."""
    return skill_branch_peek_for_visible_skills(node_ids, manager=manager, visible_skill_names=None)


def skill_branch_peek_for_visible_skills(
    node_ids: Sequence[str],
    manager: Any | None = None,
    *,
    visible_skill_names: set[str] | frozenset[str] | None = None,
) -> dict[str, Any]:
    """Peek child branch summaries within the caller-visible skill set."""
    status, settings, _ = _ready_status(manager)
    if not status.get("success"):
        return _tool_payload(False, str(status.get("result") or ""))
    toolkit = _cached_toolkit(
        str(status["index_dir"]),
        retrieve_settings=settings.retrieve,
        visible_skill_names=visible_skill_names,
    )
    return toolkit.skill_branch_peek(node_ids)


def skill_branch_explore(node_ids: Sequence[str], manager: Any | None = None) -> dict[str, Any]:
    """Explore branch nodes using the current disclosure settings."""
    return skill_branch_explore_for_visible_skills(node_ids, manager=manager, visible_skill_names=None)


def skill_branch_explore_for_visible_skills(
    node_ids: Sequence[str],
    manager: Any | None = None,
    *,
    visible_skill_names: set[str] | frozenset[str] | None = None,
) -> dict[str, Any]:
    """Explore branch nodes within the caller-visible skill set."""
    status, settings, _ = _ready_status(manager)
    if not status.get("success"):
        return _tool_payload(False, str(status.get("result") or ""))
    toolkit = _cached_toolkit(
        str(status["index_dir"]),
        retrieve_settings=settings.retrieve,
        visible_skill_names=visible_skill_names,
    )
    return toolkit.skill_branch_explore(node_ids)


def render_skill_retrieval_prompt(manager: Any | None = None, *, language: str = "cn") -> str:
    """Render the prompt section used by jiuwenswarm when agentic retrieval is enabled."""
    return render_skill_retrieval_prompt_for_visible_skills(
        manager,
        language=language,
        visible_skill_names=None,
    )


def render_skill_retrieval_prompt_for_visible_skills(
    manager: Any | None = None,
    *,
    language: str = "cn",
    visible_skill_names: set[str] | frozenset[str] | None = None,
) -> str:
    """Render the agentic retrieval prompt for the caller-visible skill set."""
    status, settings, _ = _ready_status(manager, allow_stale=False, prompt_mode=True)
    if not status.get("enabled", False):
        return ""
    if not status.get("success"):
        if language == "en":
            return (
                "# Agentic Skill Retrieval\n\n"
                "Agentic retrieval for installed skills is enabled, but the skill index is not ready.\n\n"
                f"{str(status.get('result') or '').strip()}\n\n"
                "Use `skill_index_build` when you need indexed skill retrieval, or continue with the original "
                "jiuwenswarm flow."
            ).strip()
        return (
            "# Agentic 技能检索\n\n"
            "已启用适用于较大规模已安装技能场景下的 Agentic 技能检索，但当前技能索引尚未就绪。\n\n"
            f"{str(status.get('result') or '').strip()}\n\n"
            "需要索引化技能检索时调用 `skill_index_build`；也可以继续按 jiuwenswarm 原有流程执行。"
        ).strip()

    toolkit = _cached_toolkit(
        str(status["index_dir"]),
        retrieve_settings=settings.retrieve,
        visible_skill_names=visible_skill_names,
    )
    return toolkit.root_prompt_markdown(language=language)


def _ready_status(
    manager: Any | None,
    *,
    allow_stale: bool = False,
    prompt_mode: bool = False,
) -> tuple[dict[str, Any], Any, Any]:
    settings = _cached_settings()
    resolved_manager = _resolve_manager(manager)
    if not settings.enabled:
        return {"success": False, "enabled": False, "result": render_disabled()}, settings, resolved_manager

    status = _cached_ready_status(resolved_manager, settings)
    status["enabled"] = True
    if not status.get("index_exists"):
        status["success"] = False
        status["result"] = _index_not_ready_markdown("missing", prompt_mode=prompt_mode)
        return status, settings, resolved_manager
    if not allow_stale and not status.get("fresh"):
        status["success"] = False
        status["result"] = _index_not_ready_markdown("stale", prompt_mode=prompt_mode)
        return status, settings, resolved_manager
    status["success"] = True
    status["result"] = ""
    return status, settings, resolved_manager


def _cached_toolkit(
    index_dir: str | Path,
    *,
    retrieve_settings: RetrieveSettings | None,
    visible_skill_names: set[str] | frozenset[str] | None,
) -> AgenticRetrievalToolKit:
    path = Path(index_dir).expanduser().resolve()
    visible = _normalize_visible_skill_names(visible_skill_names)
    key = (
        str(path),
        _index_artifact_signature(path),
        _retrieve_settings_signature(retrieve_settings),
        _visible_skill_signature(visible),
    )
    with _CACHE_LOCK:
        entry = _TOOLKIT_CACHE.get(key)
        if entry is not None:
            return entry.toolkit
        toolkit = AgenticRetrievalToolKit.from_index(
            path,
            retrieve_settings=retrieve_settings,
            visible_skill_names=visible,
        )
        _TOOLKIT_CACHE.clear()
        _TOOLKIT_CACHE[key] = _ToolkitCacheEntry(toolkit=toolkit)
        return toolkit


def _cached_settings() -> Any:
    global _SETTINGS_CACHE

    key = _settings_cache_key()
    with _CACHE_LOCK:
        entry = _SETTINGS_CACHE
        if entry is not None and entry.key == key:
            return entry.settings

    settings = load_settings()
    with _CACHE_LOCK:
        _SETTINGS_CACHE = _SettingsCacheEntry(key=key, settings=settings)
    return settings


def _settings_cache_key() -> tuple[Any, ...]:
    return (_config_file_signature(), _settings_env_signature())


def _config_file_signature() -> tuple[int, int] | None:
    try:
        from jiuwenswarm.common.config import get_config_file

        return _file_signature(get_config_file())
    except Exception:
        return None


def _settings_env_signature() -> tuple[tuple[str, str | None], ...]:
    return tuple((key, os.getenv(key)) for key in _SETTINGS_ENV_KEYS)


def _cached_ready_status(manager: Any, settings: Any) -> dict[str, Any]:
    key = _ready_status_cache_key(manager, settings)
    now = monotonic()
    with _CACHE_LOCK:
        entry = _READY_STATUS_CACHE.get(key)
        if entry is not None and now - entry.created_at <= _READY_STATUS_TTL_SECONDS:
            return dict(entry.status)

    status = SkillIndexService(manager).status()
    with _CACHE_LOCK:
        _READY_STATUS_CACHE.clear()
        _READY_STATUS_CACHE[key] = _StatusCacheEntry(created_at=now, status=dict(status))
    return status


def _ready_status_cache_key(manager: Any, settings: Any) -> _ReadyStatusCacheKey:
    skills_dir = str(getattr(manager, "_skills_dir", "") or "")
    artifact_root = Path(settings.artifact_root).expanduser().resolve()
    index_dir = artifact_root / "index"
    state_file = artifact_root / _STATE_FILENAME
    return _ReadyStatusCacheKey(
        skills_dir=skills_dir,
        artifact_root=str(artifact_root),
        build=_settings_signature(settings.build),
        llm=_settings_signature(settings.llm),
        index_artifacts=_index_artifact_signature(index_dir),
        state_file=_file_signature(state_file),
    )


def _index_artifact_signature(index_dir: Path) -> tuple[Any, ...]:
    return tuple((filename, _file_signature(index_dir / filename)) for filename in _INDEX_FILENAMES)


def _file_signature(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def _retrieve_settings_signature(settings: RetrieveSettings | None) -> tuple[Any, ...]:
    return _settings_signature(settings or RetrieveSettings())


def _normalize_visible_skill_names(
    visible_skill_names: set[str] | frozenset[str] | None,
) -> frozenset[str] | None:
    if visible_skill_names is None:
        return None
    return frozenset(str(name or "").strip() for name in visible_skill_names if str(name or "").strip())


def _visible_skill_signature(visible_skill_names: frozenset[str] | None) -> tuple[str, ...] | None:
    if visible_skill_names is None:
        return None
    return tuple(sorted(visible_skill_names))


def _settings_signature(settings: Any) -> tuple[tuple[str, str], ...]:
    try:
        payload = asdict(settings)
    except TypeError:
        payload = vars(settings) if hasattr(settings, "__dict__") else {"value": settings}
    return tuple(sorted((str(key), repr(value)) for key, value in dict(payload).items()))


def _clear_runtime_caches() -> None:
    global _SETTINGS_CACHE

    with _CACHE_LOCK:
        _SETTINGS_CACHE = None
        _TOOLKIT_CACHE.clear()
        _READY_STATUS_CACHE.clear()


def _index_not_ready_markdown(reason: str, *, prompt_mode: bool = False) -> str:
    if reason == "stale":
        detail = "Skill retrieval index is stale because installed skills or build settings changed."
    else:
        detail = "Skill retrieval index does not exist."
    if prompt_mode:
        next_step = "Call `skill_index_build` before using `skill_branch_explore` or `skill_branch_peek`."
    else:
        next_step = (
            "Next step: call `skill_index_build`, then call `skill_branch_explore` with known branch node ids "
            "or `skill_branch_peek` when you need to inspect branch summaries."
        )
    return (
        "# Skill Tree Retrieval Unavailable\n\n"
        f"{detail}\n\n"
        f"{next_step}\n\n"
        "Handling options:\n"
        "- Ignore this tool result and continue with the original jiuwenswarm flow.\n"
        "- Build the index once if indexed retrieval is useful for this task."
    )


def _progressive_config_from_settings(settings: RetrieveSettings | None) -> tuple[Any, int]:
    with dispatch_import_path():
        from retrieval.service.models import (
            GenerationConfig,
            OpenAIClientConfig,
            RenderConfig,
            RetrieverConfig,
            TraversalConfig,
            runtime_retriever_config_from_config,
        )

        retrieve = settings or RetrieveSettings()
        config = RetrieverConfig(
            top_k=retrieve.top_k,
            llm_client_config=OpenAIClientConfig(),
            traversal_config=TraversalConfig(
                max_branch_choices=retrieve.max_branch_choices,
                max_parallel_branches=retrieve.max_parallel_branches,
                enable_parallel_branches=True,
            ),
            render_config=RenderConfig(
                compact_codes_enabled=retrieve.compact_codes_enabled,
                flatten_tree=retrieve.flatten_tree,
                max_exposure_depth=retrieve.max_exposure_depth,
            ),
            generation_config=GenerationConfig(
                max_tokens=retrieve.max_tokens,
                request_timeout_seconds=retrieve.request_timeout_seconds,
            ),
        )
        runtime_config = runtime_retriever_config_from_config(config)
    return runtime_config.progressive, runtime_config.top_k


def _resolve_manager(manager: Any | None) -> Any:
    if manager is not None:
        return manager
    from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager

    return SkillManager()


def _tool_payload(
    success: bool,
    result: str,
    *,
    skill_tree: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"success": bool(success), "result": str(result or "")}
    if skill_tree is not None:
        detailed_output = dict(payload)
        detailed_output["skill_tree"] = skill_tree
        return AgenticToolResult(payload, detailed_output=detailed_output)
    return AgenticToolResult(payload)


def _agentic_build_result(result: str) -> str:
    text = str(result or "")
    if "Use `skill_retrieve` with a task query to retrieve relevant installed skills." not in text:
        return text
    return text.replace(
        "Use `skill_retrieve` with a task query to retrieve relevant installed skills.",
        "Use `skill_branch_explore` as the primary branch retrieval tool, "
        "or use `skill_branch_peek` when you need lightweight branch summaries.",
    )


def _resolution_for_exposed(node: Any, resolution_by_canonical_id: dict[str, Any]) -> Any | None:
    selectable_id = str(getattr(node, "selectable_canonical_id", "") or "").strip()
    if selectable_id:
        resolution = resolution_by_canonical_id.get(selectable_id)
        if resolution is not None:
            return resolution
    canonical_id = str(getattr(node, "canonical_id", "") or "").strip()
    return resolution_by_canonical_id.get(canonical_id)


def _classify_exposed_children(
    children: Sequence[Any],
    resolution_by_canonical_id: dict[str, Any],
) -> tuple[list[Any], list[Any]]:
    terminal_children: list[Any] = []
    branch_children: list[Any] = []
    for child in children:
        resolution = _resolution_for_exposed(child, resolution_by_canonical_id)
        if resolution is not None and bool(getattr(resolution, "is_terminal", False)):
            terminal_children.append(child)
        else:
            branch_children.append(child)
    return terminal_children, branch_children


def _skill_tree_payload(
    *,
    query: str,
    steps: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "query": query,
        "elapsed_ms": None,
        "max_depth": max((int(step.get("depth") or 0) for step in steps), default=0),
        "candidate_count": len(candidates),
        "steps": steps,
        "candidates": candidates,
    }


def _skill_tree_query(tool_name: str, nodes: Sequence[Any]) -> str:
    node_ids = [
        str(getattr(node, "node_id", "") or "ROOT").strip() or "ROOT"
        for node in nodes
    ]
    return f"{tool_name}: {', '.join(node_ids)}"


def _named_id(identifier: str) -> dict[str, str]:
    text = str(identifier or "").strip()
    return {"id": text, "label": _humanize_node_id(text)}


def _humanize_node_id(node_id: str) -> str:
    text = str(node_id or "").strip()
    if not text:
        return ""
    last = text.split(".")[-1]
    return last.replace("_", " ").replace("-", " ").strip() or text


def _terminal_skill_id(resolution: Any | None) -> str:
    if resolution is None:
        return ""
    item = getattr(resolution, "item", None)
    return str(
        getattr(item, "payload", "")
        or getattr(item, "item_id", "")
        or getattr(item, "label", "")
        or getattr(resolution, "canonical_id", "")
        or getattr(resolution, "label", "")
        or ""
    ).strip()


def _exposed_node_id(node: Any, resolution: Any | None) -> str:
    if resolution is not None:
        canonical_id = str(getattr(resolution, "canonical_id", "") or "").strip()
        if canonical_id:
            return canonical_id
    canonical_id = str(getattr(node, "canonical_id", "") or "").strip()
    if canonical_id.startswith("node::"):
        return canonical_id.split("::", 1)[1]
    selectable_id = str(getattr(node, "selectable_canonical_id", "") or "").strip()
    if selectable_id:
        return selectable_id
    return str(getattr(node, "label", "") or canonical_id or "UNKNOWN").strip()


def _skill_md_path(metadata: dict[str, Any]) -> str:
    raw = (
        metadata.get("skill_md_path")
        or metadata.get("skill_file")
        or metadata.get("skill_path")
        or metadata.get("path")
        or ""
    )
    text = str(raw or "").strip()
    if not text:
        return ""
    path = Path(text)
    if path.name.lower() == "skill.md":
        return text
    return str(path / "SKILL.md")


def _split_description(description: str) -> _DescriptionParts:
    desc_lines: list[str] = []
    select_lines: list[str] = []
    dont_lines: list[str] = []
    current = "description"
    for raw_line in str(description or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lowered = line.lower()
        if lowered.startswith("select when:"):
            current = "select"
            select_lines.append(line.split(":", 1)[1].strip())
            continue
        if lowered.startswith("don't select when:") or lowered.startswith("dont select when:"):
            current = "dont"
            dont_lines.append(line.split(":", 1)[1].strip())
            continue
        if current == "select":
            select_lines.append(line)
        elif current == "dont":
            dont_lines.append(line)
        else:
            desc_lines.append(line)
    return _DescriptionParts(
        description=" ".join(desc_lines).strip(),
        select_when=" ".join(select_lines).strip(),
        dont_select_when=" ".join(dont_lines).strip(),
    )


def _compact(text: str, limit: int) -> str:
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 3)].rstrip() + "..."


def _compact_branch_text(text: str, limit: int) -> str:
    normalized = " ".join(str(text or "").split())
    for marker in (" Covers ", " Representative keywords", " Representative descendants"):
        if marker in normalized:
            normalized = normalized.split(marker, 1)[0].rstrip(". ")
    return _compact(normalized, limit)


def _plural(word: str, count: int) -> str:
    if int(count) == 1:
        return word
    if word.endswith("ch"):
        return f"{word}es"
    return f"{word}s"
