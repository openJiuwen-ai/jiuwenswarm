"""Windows range/coverage summaries. Exact provider records stay in facts.

Selected ranges, keyboard focus, caret activity and visibility are independent.
Never manufacture an offset from CompareEndpoints or from matching a substring.
"""
from .formatting import json_string


def read_summaries(native, refs):
    if native['platform'] != 'windows':
        return []
    aliases = {token: ref for ref, token in refs.items()}
    output = []
    for node in native['nodes']:
        values, statuses = node['values'], node['fieldStatus']
        ref = aliases[node['node']]
        collection = values.get('UIA.Collection')
        if isinstance(collection, dict) and 'UIA.Collection' not in statuses:
            if collection.get('mayContainVirtualizedItems') is True:
                output.append(
                    f'Collection {ref}: may contain virtualized items; exposed children are not the logical total. '
                    'Read is passive; scroll explicitly to explore more.')
            if collection.get('isVirtualizedItem') is True:
                output.append(f'Item {ref}: exposes VirtualizedItem; properties may be unavailable until realized.')
        selection = values.get('Text.Selection')
        if not isinstance(selection, dict) or 'Text.Selection' in statuses:
            continue
        ranges = selection.get('ranges')
        if not isinstance(ranges, list):
            continue
        pieces = []
        for index, item in enumerate(ranges):
            if not isinstance(item, dict) or not isinstance(item.get('values'), dict):
                pieces.append(f'range {index + 1}: unknown')
                continue
            observed, errors = item['values'], item.get('fieldStatus', {})
            text = observed.get('text')
            state = 'empty range' if observed.get('degenerate') is True and 'degenerate' not in errors else 'range'
            shown = json_string(text[:160]) + ('…' if len(text) > 160 else '') if isinstance(
                text, str) else 'text unavailable'
            detail = '; ' + ', '.join(f'{key}: {value.get("state", "unknown")}'
                                      for key, value in errors.items()) if errors else ''
            pieces.append(f'{state} {index + 1}: {shown}{detail}')
        coverage = selection.get('state', 'unknown')
        count = selection.get('total', 'unknown')
        output.append(f'Text selection of {ref} ({coverage}; {len(ranges)}/{count} ranges): ' +
                      ('; '.join(pieces) if pieces else 'none') +
                      '. Does not assert keyboard focus or caret activity; exact ranges in details.')
    return output
