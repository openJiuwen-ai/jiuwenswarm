"""A lossless presentation of validated native facts when projection fails.

This formatter deliberately has no dependency on the semantic tree renderer.
It does not acquire new facts, infer containment, or create operation targets.
"""
import json
import re


def literal(value):
    encoded = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    # Keep each value on one physical line, including unusual provider text.
    return re.sub(r'[\x7f-\x9f\ud800-\udfff\u2028\u2029]', lambda match: f'\\u{ord(match[0]):04x}', encoded)


def branch_references(root, refs):
    """Capture membership before calling a fallible presentation transform."""
    members, seen, pending = [], set(), [root]
    while pending:
        node = pending.pop()
        reference = node['ref']
        if reference not in refs or reference in seen:
            raise ValueError('Invalid source-tree identity before presentation')
        seen.add(reference)
        members.append(reference)
        pending.extend(reversed(node['children']))
    return tuple(members)


class UncompressedFacts:

    def __init__(self, native, refs):
        self.refs = refs
        self.aliases = {node: reference for reference, node in refs.items()}
        if len(self.aliases) != len(refs):
            raise ValueError('Native presentation has duplicate object aliases')
        self.nodes = {node['node']: node for node in native['nodes']}
        self.relations = {}
        for relation in native['relations']:
            self.relations.setdefault(relation['source'], []).append(relation)

    def render(self, members, error):
        """Emit all acquired fields/actions/relations for this source branch.

        Independent roots stay normally rendered. Shared and uncollected
        targets keep their original aliases; an edge is never invented from
        the order of these flat records. Existing captured-text bindings remain
        owned by the snapshot and are not replaced with preview strings.
        """
        issue = dict(root=members[0], error=type(error).__name__, detail=str(error))
        lines = [
            f'Presentation fallback for {members[0]}: uncompressed collected facts '
            f'({issue["error"]}: {literal(issue["detail"])}). '
            'Relations below use element references; record order is not containment.'
        ]
        for reference in members:
            node = self.nodes[self.refs[reference]]
            lines.append(reference + ' native element')
            for name in node['requestedFields']:
                if name in node['values']:
                    lines.append('\t' + literal(name) + ': ' + literal(node['values'][name]))
                if name in node['fieldStatus']:
                    lines.append('\t' + literal(name) + ' status: ' + literal(node['fieldStatus'][name]))
            # Preserve the complete action records, including description
            # failures, and all relation coverage instead of guessing defaults.
            lines.append('\tNative actions: ' + literal(node['actions']))
            for relation in self.relations.get(node['node'], ()):
                observed = {
                    key: value
                    for key, value in relation.items() if key not in ('source', 'property', 'targets')
                }
                observed['targets'] = [self.aliases[target] for target in relation['targets']]
                lines.append('\tRelation ' + literal(relation['property']) + ': ' + literal(observed))
        return dict(text='\n'.join(lines), refs=list(members), presentation='uncompressed', issue=issue)
