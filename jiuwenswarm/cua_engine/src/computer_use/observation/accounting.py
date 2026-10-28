"""Account for native values at their actual presentation destinations.

The renderer records emissions, not property reads or string matches. Values
without an equivalent destination are grouped in a lossless supplement. This
module never resolves an execution target or acquires new native facts.
"""
from .uncompressed import literal


class RenderedFields:

    def __init__(self):
        self.entries = []
        self._by_ref = {}

    def _append(self, entry):
        self.entries.append(entry)
        self._by_ref.setdefault(entry['ref'], []).append(entry)

    def record(self, node, field, *, kind='display', target=None):
        if field not in node:
            return
        parts = ([part for part in node.get('textSources', ()) if part['field'] == field]
                 if field in ('name', 'description', 'value') and kind != 'continuation' else [])
        if parts:
            for part in parts:
                self._append(
                    dict(ref=part['sourceRef'], field=part['attribute'], value=part['text'], target=target
                         or node['ref'], kind='equivalent'))
        else:
            self._append(dict(ref=node['ref'], field=field, value=node[field], target=target or node['ref'], kind=kind))

    def equivalent_text(self, node, field):
        value = node.get(field)
        if not isinstance(value, str) or not value:
            return
        claim = next((entry for entry in self._by_ref.get(node['ref'], ())
                      if entry['field'] != field and entry['value'] == value and entry['kind'] != 'continuation'), None)
        if claim is not None:
            self.record(node, field, kind='equivalent', target=claim['target'])


def account_fields(members, refs, sources, native_nodes, rendered):
    """Return grouped residuals and a destination for each collected value.

    Known normalization is explicit in sources; unknown values already have native
    annotations. A continuation is valid only for the publicly captured value,
    never for an arbitrary field which happens to have the same text.
    """
    claims = {}
    for claim in rendered.get('fieldCoverage', ()):
        claims.setdefault((claim['ref'], claim['field']), []).append(claim)
    groups, destinations, actions = {}, [], []

    def supplement(ref, label):
        groups.setdefault(label, []).append(ref)

    for ref in members:
        native = native_nodes[refs[ref]]
        for key, raw in native['values'].items():
            source = sources[(ref, key)]
            destination = dict(ref=ref, property=key)
            if source['representation'] == 'annotation':
                destination['kind'] = 'annotation'
            elif source['representation'] == 'projection':
                projected = []
                for part in source['projections']:
                    claim = None
                    for candidate in claims.get((ref, part['field']), ()):
                        if candidate['kind'] == 'continuation':
                            continue
                        equal_value = (type(candidate['value']) is type(part['normalized'])
                                       and candidate['value'] == part['normalized'])
                        if equal_value:
                            claim = candidate
                            break
                    if claim is None:
                        break
                    projected.append(dict(field=part['field'], target=claim['target'], kind=claim['kind']))
                if projected and len(projected) == len(source['projections']):
                    destination.update(kind='equivalent', projections=projected)
                else:
                    supplement(ref, key + ': ' + literal(raw))
                    destination['kind'] = 'supplement'
            else:
                matching = [
                    claim for claim in claims.get((ref, source['field']), ())
                    if type(claim['value']) is type(source['normalized']) and claim['value'] == source['normalized']
                ]
                status = native['fieldStatus'].get(key)
                captured = source['field'] == 'value' and isinstance(raw, str) and (status is None
                                                                                    or 'textRef' in status)
                claim = next((claim for claim in matching if claim['kind'] != 'continuation' or captured), None)
                if claim is None:
                    supplement(ref, key + ': ' + literal(raw))
                    destination['kind'] = 'supplement'
                else:
                    destination.update(kind=claim['kind'], target=claim['target'])
            destinations.append(destination)

        # Primary actions may be omitted from the tree as "clickable" and
        # descriptions may alias a name. Preserve the complete native catalogue
        # once per distinct record, including its coverage and description status.
        catalogue = native['actions']
        label = ('Native actions: none (complete)'
                 if catalogue == dict(state='complete', items=[]) else 'Native actions: ' + literal(catalogue))
        supplement(ref, label)
        actions.append(dict(ref=ref, kind='supplement'))

    return groups, dict(values=destinations, actions=actions)
