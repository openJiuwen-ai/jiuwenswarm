"""Pure table/selection presentation; no target resolution or native input."""
from ..native_semantics import PROPERTIES, RELATIONS
from ..protocol.primitives import safeInteger


def cell_ranges(source, span):
    """AT-SPI positions are meaningful only for visible, showing cells.

    Keep the original four-out-parameter record intact. Unknown additions or
    failed visibility facts cannot be folded into a confident table address.
    """
    values, statuses = source['values'], source['fieldStatus']
    if any(key in statuses for key in ('TableCell.rowColumnSpan', 'visible', 'showing')):
        return None
    if values.get('visible') is not True or values.get('showing') is not True:
        return None
    if not isinstance(span, dict) or set(span) != {'row', 'column', 'rowSpan', 'columnSpan'}:
        return None
    if not all(safeInteger(value) for value in span.values()):
        return None
    if span['row'] < 0 or span['column'] < 0 or span['rowSpan'] < 1 or span['columnSpan'] < 1:
        return None
    if not all(safeInteger(span[key] + span[key + 'Span']) for key in ('row', 'column')):
        return None
    return {key + 'Range': dict(location=span[key], length=span[key + 'Span']) for key in ('row', 'column')}


def uia_cell_range(source, property):
    """Combine one declared UIA coordinate/span pair; do not infer visibility.

    GridItem coordinates are logical positions. An absent/failed counterpart
    stays raw rather than receiving a default index or span. Row and column
    evidence are independent, and every contributing source stays accounted.
    """
    axis = {'GridItem.Row': 'Row', 'GridItem.RowSpan': 'Row',
            'GridItem.Column': 'Column', 'GridItem.ColumnSpan': 'Column'}.get(property)
    if axis is None:
        return None
    keys = 'GridItem.' + axis, 'GridItem.' + axis + 'Span'
    if any(key in source['fieldStatus'] for key in keys):
        return None
    index, span = (source['values'].get(key) for key in keys)
    if not safeInteger(index) or not safeInteger(span) or index < 0 or span < 1 or not safeInteger(index + span):
        return None
    return axis.lower() + 'Range', dict(location=index, length=span)


def selection_summaries(native, refs):
    """Describe the container's claim without changing any node's state."""
    aliases = {token: ref for ref, token in refs.items()}
    nodes = {node['node']: node for node in native['nodes']}
    labels = dict(selectedChildren='children', selectedCells='cells', selectedRows='rows', selectedColumns='columns')
    output = []
    for relation in native['relations']:
        kind = RELATIONS[native['platform']].get(relation['property'])
        if kind not in labels or relation['state'] in ('absent', 'unsupported'):
            continue
        details = []
        if relation['certainty'] != 'confirmed':
            details.append('membership ' + relation['certainty'])
        if relation['state'] != 'complete':
            details.append(relation['state'])
        if 'total' in relation:
            details.append(str(len(relation['targets'])) + ' listed of ' + str(relation['total']))
        if relation.get('offset', 0):
            details.append('offset ' + str(relation['offset']))
        unread = [aliases[token] for token in relation['targets'] if token not in nodes]
        if unread:
            details.append('not expanded: ' + ', '.join(unread))
        selected = PROPERTIES[native['platform']].get('selected')
        conflicts = [aliases[token] for token in relation['targets'] if token in nodes
                     and nodes[token]['values'].get(selected) is False
                     and selected not in nodes[token]['fieldStatus']]
        if conflicts:
            details.append('node reports selected=false: ' + ', '.join(conflicts))
        if native['platform'] == 'linux':
            source = nodes[relation['source']]
            count = source['values'].get('Selection.nSelectedChildren')
            if (safeInteger(count) and count >= 0 and 'Selection.nSelectedChildren' not in source['fieldStatus']
                    and 'total' in relation and count != relation['total']):
                details.append('separate count reports ' + str(count))
        targets = ', '.join(aliases[token] for token in relation['targets'])
        if not targets:
            targets = ('none' if relation['state'] == 'complete' and relation['certainty'] == 'confirmed'
                       else 'no confirmed items collected')
        output.append('Selected ' + labels[kind] + ' of ' + aliases[relation['source']] + ': ' + targets
                      + (' (' + '; '.join(details) + ')' if details else ''))
    return output
