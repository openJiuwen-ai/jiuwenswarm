"""Exact duplicate captions, inspired by Codex's repetitive-static-text pass.

No substring matching, whitespace trimming or representative-element routing.
Only the presentation leaf disappears; its fields and original ref are retained.
"""
from ..presentation_policy import editing
from ..tree import hasIncompleteChildren, valuePreviewLimit
from ...protocol.primitives import utf16Length
from .labels import passive_caption
from .policy import CONTROLS, REGIONS, sources


def merge_repeated_text(state, node, scope):
    if (not node.get('_factBacked') or scope.in_table or editing(node)
            or node['role'] not in REGIONS | CONTROLS | {'heading', 'staticText'}
            or node.get('preserveGroup') or hasIncompleteChildren(node)):
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
        if (isinstance(value, str) and value and utf16Length(value) <= valuePreviewLimit
                and (not parts or all(part['text'] == value for part in parts))):
            destinations.setdefault(value, field)

    retained = []
    for child in node['children']:
        parts = sources(child)
        if (child['role'] != 'staticText' or child['children'] or not passive_caption(state, child, node)
                or any(child.get(key) for key in ('labelTargets', 'titleRef', 'valueIsNumber'))
                or not parts or any(part['text'] not in destinations for part in parts)):
            retained.append(child)
            continue
        for part in parts:
            field = destinations[part['text']]
            if not any(p['field'] == field for p in node['textSources']):
                # Value was previously recorded on its own ref. Include it as
                # well as the caption source, so neither field loses coverage.
                node['textSources'].append(dict(sourceRef=node['ref'], attribute=field,
                                                field=field, text=node[field]))
            state.adopt(node, [part], field)
        state.fold(child, node, 'label')
    node['children'] = retained
    return [node]
