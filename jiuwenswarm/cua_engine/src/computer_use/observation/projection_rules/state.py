"""One display transaction. Native nodes and execution bindings are read-only.

Passes edit a private view and account for every removed node. The source map is
not an alias map for input: folding ref 32 into ref 30 never retargets ref 32.
"""
from copy import deepcopy

from ..presentation_policy import display_copy
from ..tree import interaction_view, unique, with_interactions
from .policy import content


class ProjectionState:

    def __init__(self, root, context=None):
        self.context = {} if context is None else dict(context)
        self.native_nodes, self.folded_into, self.changes = {}, {}, []
        self.nodes, self.parents, self.scopes, self.order = {}, {}, {}, []
        self.targets, self.stages, self._stage = {}, {}, None

        def index(node):
            self.native_nodes[node['ref']] = node
            for child in node['children']:
                index(child)

        index(root)

        def copy(node, scope):
            if node['role'] in ('window', 'sheet', 'menuBar'):
                scope = node['ref']
            text_sources = [
                dict(sourceRef=node['ref'], attribute=field, field=field, text=node[field])
                for field in ('name', 'description') if node.get(field)
            ]
            display = display_copy(node) if self.context.get('nativeFacts') else node
            # Nested action/capability/control records must not alias the source.
            view = deepcopy({k: v for k, v in display.items() if k != 'children'})
            view.update(native=self.native_nodes[node['ref']], sourceRefs=[node['ref']], textSources=text_sources)
            self.nodes[node['ref']], self.scopes[node['ref']] = view, scope
            self.order.append(node['ref'])
            for child in node['children']:
                self.parents[child['ref']] = node['ref']
            view['children'] = [copy(child, scope) for child in node['children']]
            return view

        self.view = copy(
            interaction_view(with_interactions(root, self.context.get('inheritedBlocks')),
                             bool(self.context.get('includeBackground'))), root['ref'])
        self.roots = [self.view]
        self.expanded = {
            m['ref']
            for m in self.native_nodes.get(self.context.get('subtreeRef'), {}).get('control', {}).get('members', [])
        }
        self._routes = {ref: self.routes(node) for ref, node in self.nodes.items()}
        for node in self.nodes.values():
            if node.get('control', {}).get('kind') == 'textInput':
                for member in node['control']['members']:
                    if member['part'] == 'editor' and member['ref'] in self.nodes:
                        self.nodes[member['ref']]['editorOwnerRef'] = node['ref']

    @staticmethod
    def routes(node):
        return deepcopy({k: node[k] for k in ('role', 'capabilities', 'actions', 'control') if k in node})

    def keep(self, node):
        return node['ref'] == self.context.get('subtreeRef') or node['ref'] in self.expanded

    @staticmethod
    def member_of(child, owner):
        return any(m['ref'] == child['ref'] for m in owner.get('control', {}).get('members', []))

    def ancestor_of(self, ancestor, child):
        parent = self.parents.get(child)
        while parent is not None:
            if parent == ancestor:
                return True
            parent = self.parents.get(parent)
        return False

    def adopt(self, target, parts, field, separator=', '):
        for part in parts:
            if not any(existing['sourceRef'] == part['sourceRef'] and existing['attribute'] == part['attribute']
                       and existing['field'] == field and existing['text'] == part['text']
                       for existing in target['textSources']):
                target['textSources'].append({**part, 'field': field})
        target['sourceRefs'] = unique(target['sourceRefs'] + [part['sourceRef'] for part in parts])
        value = separator.join(unique(part['text'] for part in target['textSources'] if part['field'] == field))
        if target.get(field) != value and self._stage is not None:
            self._stage['affectedRefs'].add(target['ref'])
        target[field] = value

    @staticmethod
    def represented(source, target):
        texts = {part['text'] for part in target['textSources']}
        return all(text in texts for text in content(source))

    def fold(self, source, target, kind):
        self.folded_into[source['ref']] = target['ref']
        target['sourceRefs'] = unique(target['sourceRefs'] + [source['ref']] + source['sourceRefs'])
        self.changes.append(dict(sourceRef=source['ref'], targetRef=target['ref'], kind=kind))
        if kind == 'label' and source.get('actions'):
            self.changes.append(
                dict(sourceRef=source['ref'], kind='actions', actions=[a['name'] for a in source['actions']]))

    def run(self, step, *args):
        """Invoke the same independently testable pass, with optional local audit."""
        if not self.context.get('recordStages'):
            return step(self, *args)
        previous = self._stage
        record = self.stages.setdefault(step.__name__, dict(calls=0, changes=0, affectedRefs=set()))
        self._stage, start = record, len(self.changes)
        record['calls'] += 1
        try:
            return step(self, *args)
        finally:
            events = self.changes[start:]
            record['changes'] += len(events)
            record['affectedRefs'].update(e['sourceRef'] for e in events)
            self._stage = previous

    def assert_integrity(self):
        """Reject silent node loss, invented refs, cycles and capability transfer.

        Field/action/relation accounting happens after text rendering. This
        earlier check protects the display-to-native identities themselves.
        """
        seen, pending = set(), list(self.roots)
        while pending:
            node = pending.pop()
            ref = node['ref']
            if ref not in self.nodes or ref in seen:
                raise ValueError('Projection contains an unknown or repeated element reference')
            seen.add(ref)
            if self.routes(node) != self._routes[ref]:
                raise ValueError('Projection changed an element role or operation capability')
            if node['native'] is not self.native_nodes[ref]:
                raise ValueError('Projection changed a native source binding')
            if any(source not in self.native_nodes for source in node['sourceRefs']):
                raise ValueError('Projection contains an unknown source reference')
            for part in node['textSources']:
                source = self.native_nodes.get(part['sourceRef'], {})
                if source.get(part['attribute']) != part['text']:
                    raise ValueError('Projection caption has no matching source field')
            pending.extend(node['children'])
        omitted = {event['sourceRef'] for event in self.changes if event['kind'] in ('label', 'layout', 'editor')}
        if 'app' in self.context and self.view['role'] == 'application' and not self.context.get('subtreeRef'):
            omitted.add(self.view['ref'])
        if set(self.nodes) - seen - omitted:
            raise ValueError('Projection removed an element without recording its source')
        for ref, owner in self.folded_into.items():
            if ref not in self.native_nodes or owner not in self.native_nodes:
                raise ValueError('Projection fold has no native source or target')

    def result(self):
        self.assert_integrity()
        result = dict(roots=self.roots, foldedInto=self.folded_into, nativeNodes=self.native_nodes,
                      changes=self.changes)
        if self.context.get('recordStages'):
            result['stages'] = [
                dict(name=name, **{**record, 'affectedRefs': sorted(record['affectedRefs'])})
                for name, record in self.stages.items()
            ]
        return result
