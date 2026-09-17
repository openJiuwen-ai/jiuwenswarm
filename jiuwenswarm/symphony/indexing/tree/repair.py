from __future__ import annotations

from typing import TYPE_CHECKING

from shared.rich_compat import Console, Panel

from .schema import TreeNode

if TYPE_CHECKING:
    from .builder import TreeBuilder


console = Console()


def _builder_attr(builder: "TreeBuilder", name: str):
    return getattr(builder, name)


def _builder_call(builder: "TreeBuilder", name: str, *args, **kwargs):
    return getattr(builder, name)(*args, **kwargs)


class TreeRepairEngine:
    """Owns post-build repair passes and equivalence regrouping."""

    def __init__(self, builder: "TreeBuilder") -> None:
        self._builder = builder

    def postprocess_tree(self, root: TreeNode, verbose: bool = False) -> None:
        total_reassignments = 0
        max_passes = int(_builder_attr(self._builder, "_postprocess_max_passes"))
        for repair_pass in range(1, max_passes + 1):
            moved = self.postprocess_node(root, verbose=verbose)
            total_reassignments += moved
            if verbose:
                console.print(f"[dim]  Post-process pass {repair_pass}: reassigned {moved} skills[/dim]")
            if moved <= 0:
                break
        if total_reassignments > 0:
            console.print(
                Panel(
                    "[bold green]Post-process repaired "
                    f"{total_reassignments} misplaced skill assignments.[/bold green]",
                    title="[bold green]Tree Repair[/bold green]",
                    border_style="green",
                )
            )

    def postprocess_node(self, node: TreeNode, verbose: bool = False) -> int:
        if node.is_leaf:
            return 0
        moved = 0
        for child in list(node.children):
            moved += self.postprocess_node(child, verbose=verbose)
        moved += self.rebalance_child_assignments(node, verbose=verbose)
        return moved

    def rebalance_child_assignments(self, node: TreeNode, verbose: bool = False) -> int:
        builder = self._builder
        if len(node.children) < 2:
            return 0
        groups = {
            child.id: {
                "name": child.name,
                "description": child.description,
                "select_when": child.select_when,
                "dont_select_when": child.dont_select_when,
            }
            for child in node.children
        }
        if len(groups) < 2:
            return 0

        skill_entries: list[dict] = []
        skill_data_by_id: dict[str, dict] = {}
        source_leaf_by_skill_id: dict[str, TreeNode] = {}
        source_child_by_skill_id: dict[str, str] = {}

        for child in node.children:
            for leaf_node, skill_data in self.collect_subtree_skill_locations(child):
                skill_id = str(skill_data.get("id", "")).strip()
                if not skill_id:
                    continue
                skill_entries.append(skill_data)
                skill_data_by_id[skill_id] = skill_data
                source_leaf_by_skill_id[skill_id] = leaf_node
                source_child_by_skill_id[skill_id] = child.id

        if len(skill_entries) < int(_builder_attr(builder, "_postprocess_min_skills")):
            return 0

        assignments = _builder_call(builder, "_classify_skills", skill_entries, groups, verbose=False)
        assignments = _builder_call(
            builder,
            "_validate_and_recover",
            skill_entries,
            groups,
            assignments,
            verbose=False,
        )

        child_by_id = {child.id: child for child in node.children}
        moves: list[tuple[str, str]] = []
        for skill_id, current_child_id in source_child_by_skill_id.items():
            target_child_id = assignments.get(skill_id)
            if not target_child_id or target_child_id == current_child_id or target_child_id not in child_by_id:
                continue
            moves.append((skill_id, target_child_id))

        if not moves:
            return 0

        for skill_id, target_child_id in moves:
            source_leaf = source_leaf_by_skill_id.get(skill_id)
            skill_data = skill_data_by_id.get(skill_id)
            target_child = child_by_id.get(target_child_id)
            if source_leaf is None or skill_data is None or target_child is None:
                continue
            source_leaf.skills = [skill for skill in source_leaf.skills if skill.id != skill_id]
            _builder_call(builder, "_insert_skill_into_subtree", target_child, skill_data)

        removed_empty = _builder_call(builder, "_prune_empty_children", node)
        if removed_empty:
            _builder_call(builder, "_prune_empty_children", node)
            if len(node.children) >= 2:
                _builder_call(
                    builder,
                    "_rewrite_node_label_after_singleton",
                    node,
                    _builder_call(builder, "_existing_child_groups", node.children),
                    verbose=verbose,
                )

        total_moved = len(moves)
        if verbose and total_moved > 0:
            console.print(
                f"[dim]  Post-process repaired '{node.id}': moved={len(moves)}, " f"removed_empty={removed_empty}[/dim]"
            )
        return total_moved

    def collect_subtree_skill_locations(self, node: TreeNode) -> list[tuple[TreeNode, dict]]:
        if node.is_leaf:
            return [(node, _builder_call(self._builder, "_skill_to_data", skill)) for skill in node.skills]
        results: list[tuple[TreeNode, dict]] = []
        for child in node.children:
            results.extend(self.collect_subtree_skill_locations(child))
        return results

    def collect_subtree_skill_dicts(self, node: TreeNode) -> list[dict]:
        return [skill_data for _, skill_data in self.collect_subtree_skill_locations(node)]

    def normalize_to_equivalence_groups(self, root: TreeNode, verbose: bool = False) -> None:
        if root.is_leaf:
            return
        updated_children: list[TreeNode] = []
        split_count = 0
        for child in list(root.children):
            self.normalize_to_equivalence_groups(child, verbose=verbose)
            if root.id != "root" and self.is_second_leaf_node(child):
                replacement_nodes = self.split_second_leaf_node_into_equiv_groups(root, child, verbose=verbose)
                updated_children.extend(replacement_nodes)
                if len(replacement_nodes) > 1 or replacement_nodes[0].id != child.id:
                    split_count += 1
            else:
                updated_children.append(child)
        root.children = updated_children
        if verbose and split_count > 0:
            console.print(
                f"[dim]  Equivalence regrouping updated {split_count} second-leaf nodes under '{root.id}'[/dim]"
            )

    @staticmethod
    def is_second_leaf_node(node: TreeNode) -> bool:
        if not node.children:
            return False
        return all(child.is_leaf for child in node.children)

    def split_second_leaf_node_into_equiv_groups(
        self,
        parent_node: TreeNode,
        second_leaf_node: TreeNode,
        verbose: bool = False,
    ) -> list[TreeNode]:
        builder = self._builder
        leaf_children = list(second_leaf_node.children)
        if len(leaf_children) <= 1:
            return [second_leaf_node]
        groups = _builder_call(
            builder,
            "_discover_equivalence_groups",
            second_leaf_node,
            leaf_children,
            verbose=verbose,
        )
        if not groups:
            return [second_leaf_node]
        normalized_groups = _builder_call(builder, "_normalize_equivalence_groups", leaf_children, groups)
        if len(normalized_groups) <= 1:
            only_group = normalized_groups[0]
            second_leaf_node.name = only_group.get("name", second_leaf_node.name)
            second_leaf_node.description = only_group.get("description", second_leaf_node.description)
            second_leaf_node.select_when = only_group.get("select_when", second_leaf_node.select_when)
            second_leaf_node.dont_select_when = only_group.get("dont_select_when", second_leaf_node.dont_select_when)
            return [second_leaf_node]

        used_ids = {child.id for child in parent_node.children}
        replacement_nodes: list[TreeNode] = []
        for idx, group in enumerate(normalized_groups, start=1):
            base_id = _builder_call(
                builder,
                "_build_equivalence_group_id",
                group_id=str(group.get("id") or "").strip(),
                group_name=str(group.get("name") or "").strip(),
                fallback=f"{second_leaf_node.id}-equiv-{idx}",
            )
            group_id = base_id
            suffix = 2
            while group_id in used_ids:
                group_id = f"{base_id}-{suffix}"
                suffix += 1
            used_ids.add(group_id)
            new_node = TreeNode(
                id=group_id,
                name=str(group.get("name") or group_id),
                description=str(group.get("description") or second_leaf_node.description),
                select_when=str(group.get("select_when") or ""),
                dont_select_when=str(group.get("dont_select_when") or ""),
                depth=second_leaf_node.depth,
                parent_id=second_leaf_node.parent_id,
            )
            for leaf in group.get("leaf_nodes", []):
                leaf.parent_id = new_node.id
                leaf.depth = new_node.depth + 1
                new_node.children.append(leaf)
            replacement_nodes.append(new_node)
        if verbose:
            console.print(
                f"[dim]  Split '{second_leaf_node.id}' into {len(replacement_nodes)} equivalence groups[/dim]"
            )
        return replacement_nodes
