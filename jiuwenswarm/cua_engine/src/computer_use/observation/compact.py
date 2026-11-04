"""Task-facing presentation policy. Exact facts remain in snapshot details.

These are rendering choices, never changes to capabilities, source values or
native action catalogues. Unknown controls and meaningful state stay visible.
"""
import re

from ..native_semantics import RELATIONS
from .fact_groups import reference_set
from .formatting import json_string
from .presentation_policy import SUPPORTING_ACTIONS, editing


def display_actions(node, actions):
    # Confirmed Codex renderer policy, not a claim to implement its entire
    # native filterActions pass. A context-menu action remains executable.
    return [action for action in actions if action['name'] not in SUPPORTING_ACTIONS]


def display_selection(node):
    value = node.get('selectionRange')
    return value if editing(node) or value != dict(location=0, length=0) else None


def unexpanded_menu(node):
    return (node['role'] == 'menuBarItem' and not node['children'] and node.get('childrenStart') == 0
            and node.get('childrenEnd') == 0 and node.get('nextChildOffset') == 0 and node.get('childCount', 0) > 0)


def display_url(value, format_field, limit=160):
    """Bound incidental URLs without destroying paths, queries or data bytes.

    The exact URL is in details from this same capture. Unlike Codex's display
    shortening, do not silently drop a query/fragment or rewrite a destination.
    """
    if value.lower().startswith(('data:', 'webdoc:')):
        scheme, _, rest = value.partition(':')
        kind = rest.split(',', 1)[0] if scheme.lower() == 'data' else ''
        return format_field(scheme + ':' + kind[:60]) + ' [embedded; see details]'
    if len(value) <= limit:
        return format_field(value)
    return format_field(value[:limit]) + '… [URL shortened; see details]'


_ENCODED = re.compile(
    r'\A(?P<header>(?:data:[\w!#$&^.+-]+/[\w!#$&^.+-]+(?:;[^;,\s]{1,128})*|/[\w!#$&^.+-]+);base64),'
    r'(?P<data>[A-Za-z0-9+/]{128,}={0,2})\Z', re.IGNORECASE)


def display_embedded(value, format_field):
    """Preview an explicitly encoded carrier, never guess from long text.

    Chromium may expose an image's data URL as its description with the
    `data:image` prefix removed. Retain the exact field in accounting/details;
    this function makes no claim that the payload was emitted or decoded.
    """
    match = _ENCODED.fullmatch(value)
    if match is None:
        return None
    return (format_field(match['header']) + f' [embedded data, {len(match["data"])} encoded chars; see details]')


def child_continuations(roots):
    """References whose compact tree already carries a child-page entry."""
    refs, pending = set(), list(roots)
    while pending:
        node = pending.pop()
        if 'nextChildOffset' in node:
            refs.add(node['ref'])
        pending.extend(node['children'])
    return refs


def observation_warnings(snapshot):
    """Keep actual collection failures in the main view, not provider defaults."""
    aliases = {token: ref for ref, token in snapshot.refs.items()}
    collected = {node['node'] for node in snapshot.native['nodes']}
    kinds = RELATIONS[snapshot.native['platform']]
    warnings, groups = [], {}
    continued = child_continuations(snapshot.roots)
    covered_properties = set()

    def label(value):
        return value if re.fullmatch(r'[\w.:-]+', value) else json_string(value)

    def detail(value):
        return ': ' + json_string(value['detail']) if value.get('detail') else ''

    for node in snapshot.native['nodes']:
        for key, value in node['fieldStatus'].items():
            if value['state'] in ('error', 'timeout', 'truncated'):
                groups.setdefault(label(key) + ' (' + value['state'] + ')' + detail(value),
                                  []).append(aliases[node['node']])
    for relation in snapshot.native['relations']:
        # Native coverage is already stated next to the source, with a valid
        # continuation. Do not repeat that same coverage as a diagnostic.
        # Provider errors, conflicts and other details remain visible.
        covered = (kinds.get(relation['property']) == 'children' and aliases[relation['source']] in continued
                   and relation['state'] == 'partial' and relation['certainty'] == 'confirmed'
                   and relation.get('detail') in (None, 'native relation has an unread continuation'))
        if covered:
            covered_properties.add(relation['property'])
        uncovered_relation_gap = (relation['state'] in ('error', 'timeout', 'truncated') or relation['certainty']
                                  == 'conflicting' or relation['state'] not in ('absent', 'unsupported') and
                                  (relation.get('detail') or relation['certainty'] == 'unknown')) and not covered
        if uncovered_relation_gap:
            groups.setdefault(
                label(relation['property']) + ' (' + relation['state'] + ', ' + relation['certainty'] + ')' +
                detail(relation), []).append(aliases[relation['source']])
        # Missing labels/selected members/content remain discoverable even
        # when the relation query itself completed successfully. Parent/window
        # endpoints outside this observation are scope metadata, not content.
        missing = [aliases[token] for token in relation['targets'] if token not in collected]
        if missing and kinds.get(relation['property']) not in ('children', 'parent', 'window'):
            groups.setdefault(
                label(relation['property']) + ' -> ' + ', '.join(missing) +
                ' (not collected; inspect with app.element(ref).read())', []).append(aliases[relation['source']])
    warnings.extend(reference_set(refs) + ': ' + message for message, refs in groups.items())
    # Shared/cyclic containment and disconnected roots are actionable gaps.
    # Uncollected ancestor/window endpoints alone do not mean missing content;
    # incomplete child branches already carry a local coverage annotation.
    covered_lines = {'Collection: ' + json_string('partial native relation: ' + prop) for prop in covered_properties}
    for line in snapshot.annotations:
        relation_note = (' contains ' in line or ' has no expanded containment path ' in line
                         or line.startswith('Collection:'))
        if relation_note and line not in covered_lines:
            warnings.append(line)
    return ['Observation notes (see details):\n' + '\n'.join('\t' + line for line in warnings)] if warnings else []
