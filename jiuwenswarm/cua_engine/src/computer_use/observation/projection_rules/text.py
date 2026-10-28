"""Fold exact duplicate captions while retaining their source references.

No substring matching, whitespace trimming or representative-element routing.
Only the presentation leaf disappears; its fields and original ref are retained.
"""
from ...protocol.primitives import utf16_length
from ..presentation_policy import editing
from ..tree import VALUE_PREVIEW_LIMIT, has_incomplete_children
from .labels import passive_caption
from .policy import CONTROLS, REGIONS, sources


def merge_repeated_text(state, node, scope):
    must_keep_text_structure = (not node.get('_factBacked') or scope.in_table or editing(node)
                                or node['role'] not in REGIONS | CONTROLS | {'heading', 'staticText'}
                                or node.get('preserveGroup') or has_incomplete_children(node))
    if must_keep_text_structure:
        return [node]

    # Adopt only whole, already displayed fields, not substrings or combined
    # captions with several different components. Keeping this invariant lets
    # later passes safely propagate the same textSources through another fold.
    destinations = {}
    for field in ('name', 'description', 'value'):
        # A checkbox's native "1" may render as "on"; a heading's numeric
        # value is its level. Neither is a duplicate textual caption.
        if field == 'value' and (node.get('valueIsNumber') or node['role'] in ('checkBox', 'switch', 'radioButton')):
            continue
        value = node.get(field)
        parts = [part for part in node['textSources'] if part['field'] == field]
        short_equivalent_value = (isinstance(value, str) and value and utf16_length(value) <= VALUE_PREVIEW_LIMIT
                                  and (not parts or all(part['text'] == value for part in parts)))
        if short_equivalent_value:
            destinations.setdefault(value, field)

    retained = []
    for child in node['children']:
        parts = sources(child)
        must_keep_child_text = (child['role'] != 'staticText' or child['children']
                                or not passive_caption(state, child, node)
                                or any(child.get(key) for key in ('labelTargets', 'titleRef', 'valueIsNumber'))
                                or not parts or any(part['text'] not in destinations for part in parts))
        if must_keep_child_text:
            retained.append(child)
            continue
        for part in parts:
            try:
                field = destinations[part['text']]
            except KeyError as error:
                raise ValueError("The rendered label has no matching source field") from error
            if not any(p['field'] == field for p in node['textSources']):
                # Value was previously recorded on its own ref. Include it as
                # well as the caption source, so neither field loses coverage.
                node['textSources'].append(dict(sourceRef=node['ref'], attribute=field, field=field, text=node[field]))
            state.adopt(node, [part], field)
        state.fold(child, node, 'label')
    node['children'] = retained
    return [node]
