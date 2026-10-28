"""Ordered, independently testable full-tree presentation passes.

Explicit relations run first. Captions/editors run before children; region and
layout passes run after children, so a newly exposed heading names its region.
This scheduling matters: running all pruning before all control composition can
mistake two separate controls for parts of one. No pass touches driver bindings.
"""
from dataclasses import dataclass

from ..tree import is_window
from .controls import merge_control_labels, merge_editors
from .labels import associate_labels, merge_region_labels, promote_description
from .layout import flatten_redundant_hierarchy, prune_empty_rows, prune_layout
from .outline import outline
from .policy import secondary_actions
from .state import ProjectionState
from .text import merge_repeated_text


@dataclass(frozen=True)
class Scope:
    parent: dict | None = None
    in_table: bool = False


BEFORE_CHILDREN = (promote_description, merge_editors, merge_control_labels)
AFTER_CHILDREN = (merge_repeated_text, merge_region_labels, flatten_redundant_hierarchy, prune_layout, prune_empty_rows)


def reconstruct_outlines(state):
    for node in state.roots:
        outline(state, node)


def finalize_sources(state):

    def actions(node):
        secondary = {id(a) for a in secondary_actions(node)}
        omitted = [a['name'] for a in node.get('actions', []) if id(a) not in secondary]
        if omitted:
            state.changes.append(dict(sourceRef=node['ref'], kind='actions', actions=omitted))
        for child in node['children']:
            actions(child)

    for root in state.roots:
        actions(root)
    for ref, owner in state.folded_into.items():
        target, seen = owner, {ref}
        while target in state.folded_into:
            if target in seen:
                raise ValueError('Projection fold contains a cycle')
            seen.add(target)
            target = state.folded_into[target]
        state.folded_into[ref] = target


def project(root, context=None):
    state = ProjectionState(root, context)
    state.run(associate_labels)

    def visit(node, parent=None, in_table=False):
        scope = Scope(
            parent, node['role'] == 'table'
            or (node['role'] == 'row' and sum(c['role'] == 'cell' for c in node['children']) > 1)
            or (in_table and node['role'] != 'outline'))
        for step in BEFORE_CHILDREN:
            state.run(step, node, scope)
        node['children'] = [c for child in node['children'] for c in visit(child, node, scope.in_table)]
        for step in AFTER_CHILDREN:
            replacement = state.run(step, node, scope)
            # A removed wrapper's children already completed their own passes.
            if len(replacement) != 1 or replacement[0] is not node:
                return replacement
        return [node]

    if 'app' in state.context and root['role'] == 'application' and not state.context.get('subtreeRef'):
        state.roots = [c for child in state.view['children'] for c in visit(child)]
    else:
        state.roots = visit(state.view)
    if 'app' in state.context:
        state.roots = ([n for n in state.roots if is_window(n)] +
                       [n for n in state.roots if not is_window(n) and n['role'] != 'menuBar'] +
                       [n for n in state.roots if n['role'] == 'menuBar'])
    state.run(reconstruct_outlines)
    state.run(finalize_sources)
    return state.result()
