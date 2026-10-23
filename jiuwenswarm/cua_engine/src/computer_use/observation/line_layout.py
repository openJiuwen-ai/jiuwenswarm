"""Text-only packing after semantic rendering; source refs stay in each line.

These stages do not rewrite the projected tree or field coverage. Relation
accounting checks that tree; the packed text retains every displayed reference.
"""
from .chains import compact_tree_chains
from .empty_values import compact_empty_values
from .text_parts import compact_text_parts


def group_empty_values(lines, roots, context, format_field):
    return compact_empty_values(lines, roots, context.get('focusedRef'))


def join_container_chains(lines, roots, context, format_field):
    return compact_tree_chains(lines, roots)


def join_text_parts(lines, roots, context, format_field):
    return compact_text_parts(lines, roots, context.get('focusedRef'), format_field)


STAGES = (group_empty_values, join_container_chains, join_text_parts)


def layout_lines(lines, roots, context, format_field):
    records = []
    # Explicit subtree reads expose its structure rather than packing chains.
    stages = STAGES[:1] if context.get('subtreeRef') or context.get('compact') else STAGES
    for stage in stages:
        output = stage(lines, roots, context, format_field)
        if context.get('recordStages'):
            records.append(dict(name=stage.__name__, beforeLines=len(lines), afterLines=len(output)))
        lines = output
    return lines, records
