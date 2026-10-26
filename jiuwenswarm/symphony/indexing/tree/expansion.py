from __future__ import annotations

from typing import Optional, TYPE_CHECKING

from shared.rich_compat import Console, Panel

from .prompts import NODE_LABEL_REWRITE_PROMPT
from .schema import FIXED_ROOT_CATEGORIES, TreeNode
from .types import ChildGroup

if TYPE_CHECKING:
    from .builder import TreeBuilder


console = Console()


def _builder_attr(builder: "TreeBuilder", name: str):
    return getattr(builder, name)


def _builder_call(builder: "TreeBuilder", name: str, *args, **kwargs):
    return getattr(builder, name)(*args, **kwargs)


class TreeExpansionEngine:
    """Owns node splitting, child creation, and unassigned-skill recovery."""

    def __init__(self, builder: "TreeBuilder") -> None:
        self._builder = builder

    def process_node(
        self,
        *,
        node: TreeNode,
        skills: list[dict],
        depth: int,
        parent_context: Optional[dict],
        verbose: bool = False,
    ) -> list[ChildGroup]:
        builder = self._builder
        self._update_progress(node_id=node.id, skill_count=len(skills), depth=depth)

        configured_groups = self._configured_groups_for_node(node=node)
        if not configured_groups and self._should_force_leaf(depth=depth, skill_count=len(skills)):
            self._emit_leaf(node, skills, verbose=verbose)
            return []

        if configured_groups:
            grouped = self._classify_configured_groups(
                skills,
                configured_groups,
                node_name=node.name,
                verbose=verbose,
            )
        else:
            grouped = _builder_call(builder, "_split_skills", skills, parent_context, verbose)

        if not grouped:
            self._emit_grouping_failure(node, skill_count=len(skills), depth_reached=depth >= builder.config.max_depth)
            self._emit_leaf(node, skills, verbose=verbose)
            return []
        return self.build_children_from_groups(node, skills, grouped, depth, verbose)

    def _update_progress(self, *, node_id: str, skill_count: int, depth: int) -> None:
        builder = self._builder
        progress = _builder_attr(builder, "_progress")
        progress_task = _builder_attr(builder, "_progress_task")
        if progress and progress_task is not None:
            progress.update(
                progress_task,
                description=f"Layer {depth}: {node_id} ({skill_count} skills)",
            )

    def _should_force_leaf(self, *, depth: int, skill_count: int) -> bool:
        builder = self._builder
        return skill_count <= builder.config.max_skills_per_node or depth >= builder.config.max_depth

    def _emit_leaf(self, node: TreeNode, skills: list[dict], *, verbose: bool) -> None:
        _builder_call(self._builder, "_assign_skills_to_leaf", node, skills)
        if verbose:
            console.print(f"[dim]  Finalized leaf '{node.id}' with {len(skills)} skills[/dim]")

    def _classify_root(self, skills: list[dict], *, verbose: bool) -> dict:
        return self._classify_configured_groups(
            skills,
            self.root_group_definitions(),
            node_name="Root",
            verbose=verbose,
        )

    def _classify_configured_groups(
        self,
        skills: list[dict],
        groups: dict,
        *,
        node_name: str,
        verbose: bool,
    ) -> dict:
        builder = self._builder
        if verbose:
            console.print(f"[cyan]Routing {len(skills)} skills into configured categories for {node_name}[/cyan]")
        initial = _builder_call(builder, "_classify_skills", skills, groups, verbose)
        repaired = _builder_call(builder, "_validate_and_recover", skills, groups, initial, verbose)
        return _builder_call(builder, "_build_groups_from_assignments", groups, repaired)

    def _configured_groups_for_node(self, *, node: TreeNode) -> dict | None:
        if node.id == "root":
            return self.root_group_definitions()
        configured_children = getattr(node, "_configured_children", None)
        return configured_children if isinstance(configured_children, dict) and configured_children else None

    @staticmethod
    def _emit_grouping_failure(node: TreeNode, *, skill_count: int, depth_reached: bool) -> None:
        if depth_reached:
            title = "[bold red]Max Depth Reached[/bold red]"
            body = (
                f"[bold red]Reached max depth at '{node.id}' with {skill_count} skills still attached.[/bold red]\n"
                "The subtree will stay collapsed into one leaf."
            )
        else:
            title = "[bold red]Grouping Failed[/bold red]"
            body = (
                f"[bold red]Could not derive stable child groups for '{node.id}' ({skill_count} skills).[/bold red]\n"
                "The subtree will stay collapsed into one fallback leaf."
            )
        console.print(Panel(body, title=title, border_style="red"))

    def root_group_definitions(self) -> dict[str, dict]:
        categories = self._builder.config.root_categories or FIXED_ROOT_CATEGORIES
        root_groups: dict[str, dict] = {}
        for category_id, payload in categories.items():
            root_groups[category_id] = {
                "name": payload.get("name", category_id),
                "description": payload.get("description", ""),
                "select_when": payload.get("select_when", ""),
                "dont_select_when": payload.get("dont_select_when", ""),
            }
            children = payload.get("children")
            if children:
                root_groups[category_id]["children"] = children
        return root_groups

    @staticmethod
    def create_child_node(*, parent: TreeNode, group_id: str, group_data: dict, depth: int) -> TreeNode:
        child_node = TreeNode(
            id=group_id,
            name=group_data.get("name", group_id),
            description=group_data.get("description", ""),
            select_when=group_data.get("select_when", ""),
            dont_select_when=group_data.get("dont_select_when", ""),
            depth=depth,
            parent_id=parent.id,
        )
        parent.children.append(child_node)
        return child_node

    def build_children_from_groups(
        self,
        node: TreeNode,
        skills: list[dict],
        groups: dict,
        depth: int,
        verbose: bool = False,
    ) -> list[ChildGroup]:
        builder = self._builder
        skill_map = {s["id"]: s for s in skills}
        children_to_process: list[ChildGroup] = []

        for group_id, group_data in groups.items():
            child_skill_ids = group_data.get("skill_ids", [])
            child_skills = [skill_map[sid] for sid in child_skill_ids if sid in skill_map]
            if not child_skills:
                continue

            child_node = self.create_child_node(
                parent=node,
                group_id=group_id,
                group_data=group_data,
                depth=depth + 1,
            )
            configured_children = group_data.get("children")
            if configured_children:
                setattr(child_node, "_configured_children", configured_children)
            for sid in child_skill_ids:
                skill_map.pop(sid, None)
            children_to_process.append(
                ChildGroup(
                    node=child_node,
                    skills=child_skills,
                    configured_children=configured_children,
                )
            )

        if skill_map:
            if children_to_process:
                self.assign_unassigned_skills(
                    node=node,
                    all_skills=skills,
                    remaining_skill_map=skill_map,
                    children_to_process=children_to_process,
                    verbose=verbose,
                )
            else:
                _builder_call(builder, "_assign_skills_to_leaf", node, skills)
                return []

        return children_to_process

    def reassign_skills_to_children(
        self,
        unassigned_skills: list[dict],
        children_to_process: list[ChildGroup],
    ) -> tuple[int, list[dict]]:
        builder = self._builder
        if not unassigned_skills or not children_to_process:
            return 0, unassigned_skills

        groups = {
            child_group.node.id: {
                "name": child_group.node.name,
                "description": child_group.node.description,
                "select_when": child_group.node.select_when,
                "dont_select_when": child_group.node.dont_select_when,
            }
            for child_group in children_to_process
        }
        assignments = _builder_call(
            builder,
            "_classify_skills_single",
            _builder_call(builder, "_sorted_skills", unassigned_skills),
            groups,
            verbose=False,
            is_retry=True,
        )
        if not assignments:
            return 0, unassigned_skills

        child_idx = {child_group.node.id: idx for idx, child_group in enumerate(children_to_process)}
        reassigned_count = 0
        remaining_unassigned = []
        for skill in unassigned_skills:
            group_id = assignments.get(skill["id"])
            idx = child_idx.get(group_id)
            if idx is None:
                remaining_unassigned.append(skill)
                continue
            children_to_process[idx].skills.append(skill)
            reassigned_count += 1
        return reassigned_count, remaining_unassigned

    def assign_unassigned_skills(
        self,
        *,
        node: TreeNode,
        all_skills: list[dict],
        remaining_skill_map: dict[str, dict],
        children_to_process: list[ChildGroup],
        verbose: bool = False,
    ) -> None:
        unassigned = list(remaining_skill_map.values())
        reassigned_count, unassigned = self.reassign_skills_to_children(unassigned, children_to_process)
        if unassigned:
            largest_idx = max(range(len(children_to_process)), key=lambda i: len(children_to_process[i].skills))
            largest_child = children_to_process[largest_idx]
            unassigned_ratio = len(unassigned) / len(all_skills) if all_skills else 0
            if unassigned_ratio > 0.1:
                console.print(
                    Panel(
                        f"[bold red]{len(unassigned)}/{len(all_skills)} skills ({unassigned_ratio:.0%}) unassigned "
                        f"at node '{node.id}'[/bold red]\n"
                        f"Dumping into '{largest_child.node.id}'.",
                        title="[bold red]High Unassigned Skill Count[/bold red]",
                        border_style="red",
                    )
                )
            elif verbose:
                console.print(f"[yellow]  {len(unassigned)} unassigned skills -> {largest_child.node.id}[/yellow]")
            largest_child.skills.extend(unassigned)
        elif verbose and reassigned_count > 0:
            console.print(f"[dim]  Reassigned {reassigned_count} skipped skills under '{node.id}'[/dim]")

    def rewrite_node_label_after_singleton(
        self,
        node: TreeNode,
        children_to_process: list[ChildGroup],
        verbose: bool = False,
    ) -> None:
        builder = self._builder
        if not children_to_process:
            return
        ranked_children = sorted(children_to_process, key=lambda item: (-len(item.skills), item.node.id))
        summary_lines = [self._child_summary_line(child_group) for child_group in ranked_children]

        prompt = NODE_LABEL_REWRITE_PROMPT.format(
            node_id=node.id,
            node_name=node.name,
            node_description=node.description or "(no description)",
            children_summary="\n".join(summary_lines),
        )
        result = _builder_call(builder, "_call_llm_json", prompt)
        new_name = str(result.get("name", "")).strip()
        new_description = str(result.get("description", "")).strip()
        new_select_when = str(result.get("select_when", "")).strip()
        new_dont_select_when = str(result.get("dont_select_when", "")).strip()
        if not new_name or not new_description:
            if verbose:
                console.print(f"[yellow]  Failed to rewrite label for '{node.id}', keeping original[/yellow]")
            return
        node.name = new_name
        node.description = new_description
        node.select_when = new_select_when
        node.dont_select_when = new_dont_select_when
        if verbose:
            console.print(f"[dim]  Rewrote node label for '{node.id}' after singleton reassignment[/dim]")

    @staticmethod
    def _child_summary_line(child_group: ChildGroup) -> str:
        child = child_group.node
        sample_ids = ", ".join(skill["id"] for skill in child_group.skills[:5]) or "(none)"
        description = child.description or "(no description)"
        select_when = child.select_when or ""
        dont_select_when = child.dont_select_when or ""
        rules = []
        if select_when:
            rules.append(f"select_when: {select_when}")
        if dont_select_when:
            rules.append(f"dont_select_when: {dont_select_when}")
        return (
            f"- {child.id} ({len(child_group.skills)} skills)\n"
            f"  label: {child.name}\n"
            f"  summary: {description}\n" + (f"  {'; '.join(rules)}\n" if rules else "") + f"  sample_ids: {sample_ids}"
        )

    def existing_child_groups(self, children: list[TreeNode]) -> list[ChildGroup]:
        builder = self._builder
        return [
            ChildGroup(node=child, skills=_builder_call(builder, "_collect_subtree_skill_dicts", child))
            for child in children
            if child.count_all_skills() > 0
        ]
