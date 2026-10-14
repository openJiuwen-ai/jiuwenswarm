"""Account for native relations against the hierarchy actually displayed.

This is a presentation pass. Neither a displayed ancestor nor an annotation
changes a native binding, authorizes input, or supplies execution evidence.
"""
from ..native_semantics import RELATIONS
from .uncompressed import literal
from .fact_groups import reference_set
import json


class RelationPresentation:
    def __init__(self, native, refs):
        self.refs = refs
        self.aliases = {token: ref for ref, token in refs.items()}
        self.kinds = RELATIONS[native['platform']]
        required = {node['node'] for node in native['nodes']} | set(native['unread'])
        if len(self.aliases) != len(refs) or not required <= self.aliases.keys():
            raise ValueError('Invalid source-tree identity before presentation')
        self.relations = {}
        for relation in native['relations']:
            self.relations.setdefault(self.aliases[relation['source']], []).append(relation)

    def render(self, members, rendered):
        """Return grouped explicit edges and one destination per source edge.

Only complete, confirmed Children and exact Parent/Window inverses without
independent metadata may use visible structure. The source tree is not proof:
projection can remove wrappers, fold labels or reparent outline rows.
        """
        fallback = rendered.get('presentation') == 'uncompressed'
        allowed = set(members)
        nodes, parents, windows = {}, {}, {}
        pending = [(root, None, None) for root in reversed(rendered.get('roots', ()))]
        while pending:
            node, parent, window = pending.pop()
            ref = node['ref']
            if ref not in allowed or ref in nodes:
                raise ValueError('Invalid identity in the displayed relation hierarchy')
            if node['role'] in ('window', 'sheet', 'popover'):
                window = ref
            nodes[ref], parents[ref], windows[ref] = node, parent, window
            pending.extend((child, ref, window) for child in reversed(node['children']))

        groups, sources = {}, []
        for ref in members:
            for relation in self.relations.get(ref, ()):
                kind = self.kinds.get(relation['property'])
                targets = [self.aliases[token] for token in relation['targets']]
                plain = (relation['state'] == 'complete' and relation['certainty'] == 'confirmed'
                         and not set(relation).difference({'source', 'property', 'targets', 'state', 'certainty', 'offset', 'total'}))
                implied = ref in nodes and (
                    kind == 'children' and targets == [child['ref'] for child in nodes[ref]['children']]
                    or kind == 'parent' and targets == [parents[ref]]
                    or kind == 'window' and targets == [windows[ref]])
                if fallback:
                    representation = 'uncompressed'
                elif plain and implied:
                    representation = 'containment'
                else:
                    representation = 'annotation'
                    record = {key: value for key, value in relation.items() if key not in ('source', 'property', 'targets')}
                    record['targets'] = targets
                    groups.setdefault((relation['property'], literal(record)), []).append(ref)
                sources.append(dict(source=relation['source'], property=relation['property'], representation=representation))
        return groups, sources


def render_relations(groups):
    """Factor repeated edge metadata, preserving ordered targets and all states.

    Unlike a reference set, a target array is never sorted or deduplicated.
    The reserved total expression is valid only for an explicitly observed
    integer equal to the target count; missing total/offset remain missing.
    """
    totals = {}
    for (property, encoded) in groups:
        record = json.loads(encoded)
        targets, total = record.pop('targets'), record.pop('total', None)
        if type(total) is int and total == len(targets):
            totals.setdefault((property, literal(record)), set()).add(total)
    by_metadata = {}
    for (property, encoded), refs in groups.items():
        record = json.loads(encoded)
        targets = record.pop('targets')
        common = {key: value for key, value in record.items() if key != 'total'}
        if (type(record.get('total')) is int and record['total'] == len(targets)
                and len(totals.get((property, literal(common)), ())) > 1):
            record['total'] = 'target-count'
        key = (property, literal(record))
        by_metadata.setdefault(key, {}).setdefault(literal(targets), []).extend(refs)
    # Different relation properties may share exactly the same map, not just
    # the same state. This Cartesian-product factoring is reversible.
    shared = {}
    for (property, metadata), mapping in by_metadata.items():
        mapping_key = tuple(sorted((targets, tuple(sorted(set(refs), key=int))) for targets, refs in mapping.items()))
        shared.setdefault((metadata, mapping_key), []).append(property)
    lines = []
    for (metadata, mapping), properties in shared.items():
        name = literal(properties[0]) if len(properties) == 1 else literal(properties)
        entries = [reference_set(refs) + ' -> ' + targets for targets, refs in mapping]
        lines.append('Relation ' + name + ' ' + metadata + ':')
        # Bound line length without splitting reference sets or target order.
        line = ''
        for entry in entries:
            if line and len(line) + len(entry) > 160:
                lines.append('\t' + line)
                line = ''
            line += ('; ' if line else '') + entry
        if line:
            lines.append('\t' + line)
    return lines
