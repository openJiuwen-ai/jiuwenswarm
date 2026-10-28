"""Remove presentation-only wrappers, preserving boundaries and unknowns."""
from ..tree import disclosure
from .policy import REGIONS, WRAPPERS, independent, layout, same_blocking


def anonymous_layout(node):
    """A removable wrapper must not carry its own displayed identity/state.

    A role match alone is insufficient: providers can describe two AXGroups
    as entirely different UI regions. Explicit visibility also belongs to the
    original container; promoting its children must not imply they are visible.
    """
    return (layout(node) and not node.get('identifier') and node.get('visible') is not False
            and node.get('showing') is not False and
            (not node.get('roleDescription') or node['role'] == 'group' and node['roleDescription'] in ('group', '组')))


def flatten_redundant_hierarchy(state, node, scope):
    """Codex's equal-description container splice, with source preservation.

    Only anonymous, complete layout groups are equivalent here. A named card,
    an editor, table, selected group or independent action is a boundary. This
    does not mean that every group with several children is redundant.
    """
    must_keep_hierarchy = (not state.context.get('compact') or scope.in_table or node['role'] != 'group'
                           or state.keep(node) or node['ref'] == state.context.get('focusedRef')
                           or not anonymous_layout(node))
    if must_keep_hierarchy:
        return [node]
    children = []
    for child in node['children']:
        redundant_child_group = (child['role'] == node['role'] and anonymous_layout(child)
                                 and same_blocking(node, child) and not state.keep(child)
                                 and child['ref'] != state.context.get('focusedRef'))
        if redundant_child_group:
            state.changes.append(dict(sourceRef=child['ref'], targetRef=node['ref'], kind='layout'))
            children.extend(child['children'])
        else:
            children.append(child)
    node['children'] = children
    return [node]


def prune_layout(state, node, scope):
    parent = scope.parent
    sole_wrapper = (parent is not None and len(parent['children']) == 1 and parent['role'] in REGIONS
                    and (parent['role'] not in WRAPPERS or parent.get('name') or parent.get('description')))
    removable = anonymous_layout(node) if state.context.get('compact') else layout(node)
    can_remove_layout = (not state.keep(node) and node['ref'] != state.context.get('focusedRef') and removable
                         and (not scope.in_table or node['role'] not in ('cell', 'column'))
                         and (len(node['children']) <= 1 or sole_wrapper))
    if can_remove_layout:
        state.changes.append(dict(sourceRef=node['ref'], kind='layout'))
        return node['children']
    return [node]


def prune_empty_rows(state, node, scope):
    can_remove_empty_row = (
        not scope.in_table and not state.keep(node) and node['role'] == 'row' and not node.get('name')
        and not node.get('description') and 'value' not in node and not node['children']
        and not any(node.get(k)
                    for k in ('selected', 'selectable', 'focused', 'pressable')) and node.get('enabled') is not False
        and disclosure(node) is None and not independent(node) and node['ref'] != state.context.get('focusedRef')
        and not any(ref == state.context.get('focusedRef') or state.native_nodes.get(ref, {}).get('focused')
                    for ref in node['sourceRefs']))
    if can_remove_empty_row:
        state.changes.append(dict(sourceRef=node['ref'], kind='layout'))
        return []
    return [node]
