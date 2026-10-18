"""Bounded raw graph reads shared by semantic paths and input resolution.

One operation and deadline; completeness is never inferred from missing data.
"""
from dataclasses import replace

from ..errors import ComputerUseError
from ..native_semantics import UNKNOWN as UNKNOWN
from ..native_semantics import (
    EDITING_FIELDS,
    FIELDS,
    PROPERTIES,
    RELATIONS,
    field_value,
    normalize_control_values,
)
from ..protocol.facts import decode_facts, decode_relation_page
from .native import SelectionDependency


class NativeGraph:

    def __init__(self, port, operation, root, remaining, *, fields, max_nodes=4096, max_depth=40, follow_relations=()):
        self.port, self.operation, self.root, self.remaining = port, operation, root, remaining
        self.platform = port.platform
        if self.platform not in FIELDS:
            raise ValueError('Native query platform is not supported')
        self.properties = PROPERTIES[self.platform]
        self.relations = {semantic: native for native, semantic in RELATIONS[self.platform].items()}
        self.fields = tuple(fields)
        self.follow_relations = tuple(follow_relations)
        self.max_nodes, self.max_depth = max_nodes, max_depth
        invalid_budget = type(max_nodes) is not int or not 1 <= max_nodes <= 8192 or type(
            max_depth) is not int or not 0 <= max_depth <= 40
        if invalid_budget:
            raise ValueError('Invalid native query budget')
        self.nodes, self.edges, self.completed_edges = {}, {}, {}
        self.node_sources, self.edge_sources, self.dependencies = {}, {}, {}

    def _use(self, observed, node, *, fields=(), actions=False, relations=()):
        value = self.dependencies.setdefault((observed, node), dict(fields=set(), actions=False, relations=set()))
        value['fields'].update(fields)
        value['relations'].update(relations)
        value['actions'] |= actions

    def _use_relation(self, token, relation_name):
        for observed in self.edge_sources.get((token, relation_name), ()):
            self._use(observed, token, relations=(relation_name, ))

    @property
    def basis(self):
        return tuple(
            SelectionDependency(observed, node, tuple(sorted(value['fields'])), value['actions'],
                                tuple(sorted(value['relations'])))
            for (observed, node), value in self.dependencies.items())

    async def _actions(self, token):
        node = await self._node(token)
        self._use(self.node_sources[token], token, actions=True)
        return node['actions']

    def _check_parent(self, token, ancestors):
        key = self.relations.get('parent')
        edge = self.edges.get((token, key))
        if edge is not None:
            self._use_relation(token, key)
        # A reverse edge may skip a layout wrapper on this confirmed forward
        # path. It must not point into a different control or outer window.
        conflicting_parent = (edge is not None and edge['state'] == 'complete' and edge['certainty'] == 'confirmed'
                              and edge['targets']
                              and (len(edge['targets']) != 1 or edge['targets'][0] not in ancestors))
        if conflicting_parent:
            self.fail('observation_incomplete', 'The control containment has conflicting native parent evidence.')

    @staticmethod
    def fail(code, detail):
        raise ComputerUseError(
            dict(code=code, effect='none', target='input receiver', detail=detail,
                 next='read the intended control or use its window observation; no input was sent by this lookup'))

    async def _node(self, token, *, depth=None):
        self.remaining()
        if token not in self.nodes:
            available = min(2000, self.max_nodes - len(self.nodes))
            if available < 1:
                self.fail('observation_incomplete', 'Query exhausted its native node budget.')
            batch = await self._collect_nodes(token, available, self.max_depth if depth is None else depth)
            self._remember_nodes(batch)
        return self.nodes[token]

    async def _collect_nodes(self, token, available, depth, *, relation_page=None):
        options = dict(follow_relations=self.follow_relations) if self.follow_relations else {}
        if relation_page is not None:
            options['relation_page'] = relation_page
        batch = decode_facts(await
                             self.port.nodes(self.operation, replace(self.root, node=token),
                                             [self.properties[key] for key in self.fields if key in self.properties],
                                             available, depth, self.remaining(), **options))
        batch_scope_changed = (batch['session'] != self.root.session or batch['surface'] != self.root.surface
                               or batch['platform'] != self.platform or batch['roots'] != [token]
                               or len(batch['nodes']) > available)
        if batch_scope_changed:
            raise ValueError('Native query returned a different scope or exceeded its budget')
        return batch

    def _remember_nodes(self, batch):
        # Earlier facts keep their collection identity. Supplementary reads
        # do not quietly overwrite a branch already used for matching.
        new = {n['node'] for n in batch['nodes']} - self.nodes.keys()
        if len(self.nodes) + len(new) > self.max_nodes:
            self.fail('observation_incomplete', 'Query exhausted its native node budget.')
        self.nodes.update({n['node']: n for n in batch['nodes'] if n['node'] in new})
        self.node_sources.update({node: batch['observed']['id'] for node in new})
        for relation in batch['relations']:
            if relation['source'] in new:
                key = relation['source'], relation['property']
                self.edges[key] = relation
                self.edge_sources[key] = [batch['observed']['id']]

    async def _next_relation(self, token, relation_name, cursor, limit):
        # Native relation pages can collect the original target objects in the
        # same request. One slot is reserved for the already-known source node;
        # new facts still share the graph's total node/deadline budget.
        available = min(2000, self.max_nodes - len(self.nodes) + 1)
        can_batch = 'nativeRelationPages' in getattr(self.port, 'capabilities', ()) and available > 1 and self.max_depth
        if can_batch:
            # Amortize native prefix validation across a bounded batch. Query
            # fields are narrower than a full observation's field profile.
            limit = min(512, limit, available - 1)
        page = decode_relation_page(
            await self.port.relation(
                self.operation, replace(self.root, node=token), relation_name, cursor, limit, self.remaining()
            )
        )
        relation = page['relation']
        page_scope_changed = (page['session'] != self.root.session or page['surface'] != self.root.surface
                              or page['platform'] != self.platform or relation['source'] != token
                              or relation['property'] != relation_name)
        if page_scope_changed:
            raise ValueError('Native query page changed its source or scope')
        # Different content relations can expose the same native objects.
        # Read their identities first so already-collected facts stay reusable.
        needs_target_facts = (can_batch and relation['certainty'] == 'confirmed'
                              and relation['state'] in ('complete', 'partial')
                              and any(target not in self.nodes for target in relation['targets']))
        if needs_target_facts:
            # A short final page must not fill spare slots from other relations.
            fact_limit = len(relation['targets'])
            batch = await self._collect_nodes(
                token, fact_limit + 1, 1, relation_page=dict(property=relation_name, cursor=cursor, limit=fact_limit)
            )
            collected = next((r for r in batch['relations'] if r['source'] == token and r['property'] == relation_name),
                             None)
            collected_relation_changed = (collected is None or collected['targets'] != relation['targets']
                                          or len(collected['targets']) > limit
                                          or collected.get('offset', 0) != relation.get('offset', 0)
                                          or collected.get('total') != relation.get('total')
                                          or collected['certainty'] != 'confirmed'
                                          or collected['state'] not in ('complete', 'partial'))
            if collected_relation_changed:
                self.fail('observation_incomplete', 'Relation changed while collecting its target facts.')
            return page, batch
        return page, None

    async def _field(self, token, semantic):
        node = await self._node(token)
        relation_name = self.properties.get(semantic)
        if relation_name in node['requestedFields']:
            self._use(self.node_sources[token], token, fields=(relation_name, ))
        return field_value(self.platform, node, semantic)

    async def _control_values(self, token):
        values = {key: await self._field(token, key) for key in EDITING_FIELDS if key != 'sensitive'}
        # A known ENABLED state already suffices for the Linux native contract;
        # do not guard an unrelated SENSITIVE read when it was not consulted.
        if self.platform == 'linux' and values['enabled'] is not True:
            values['sensitive'] = await self._field(token, 'sensitive')
        return normalize_control_values(self.platform, values)

    async def _relation(self, token, kind, *, read_missing=False, optional=False):
        await self._node(token)
        key = self.relations.get(kind)
        if key is None:
            self.fail('observation_incomplete', 'The platform has no declared ' + kind + ' relation.')
        cache_key = token, key
        if cache_key in self.completed_edges:
            return self.completed_edges[cache_key]
        relation = self.edges.get(cache_key)
        if relation is None and read_missing:
            page = decode_relation_page(await self.port.relation(self.operation, replace(self.root, node=token), key,
                                                                 None, min(2000, self.max_nodes), self.remaining()))
            relation = page['relation']
            supplement_scope_changed = (page['session'] != self.root.session or page['surface'] != self.root.surface
                                        or page['platform'] != self.platform or relation['source'] != token
                                        or relation['property'] != key)
            if supplement_scope_changed:
                raise ValueError('Supplementary native relation changed its source or scope')
            self.edges[cache_key] = relation
            self.edge_sources[cache_key] = [page['observed']['id']]
        if relation is None:
            self.fail('observation_incomplete', 'Required native relation was not collected: ' + kind)
        # Standard AX optional relationships use unsupported for leaf/absent
        # attributes. A failed read or an unqueried relation is not absence.
        known_empty_relation = relation['state'] == 'absent' or ((self.platform == 'macos' or optional)
                                                                 and relation['state'] == 'unsupported')
        if known_empty_relation:
            result = []
        else:
            if (relation['certainty'] != 'confirmed' or relation['state'] not in ('complete', 'partial')
                    or relation.get('offset', 0) != 0):
                self.fail('observation_incomplete', 'Required native relation is incomplete: ' + kind)
            result = list(relation['targets'])
            seen = set()
            while relation.get('cursor') is not None:
                cursor = relation['cursor']
                if cursor in seen or len(result) >= self.max_nodes:
                    self.fail('observation_incomplete', 'Relation continuation exceeded the query budget.')
                seen.add(cursor)
                page, batch = await self._next_relation(token, key, cursor, min(2000, self.max_nodes - len(result)))
                next_relation = page['relation']
                continued_relation_changed = (page['session'] != self.root.session
                                              or page['surface'] != self.root.surface
                                              or page['platform'] != self.platform or next_relation['source'] != token
                                              or next_relation['property'] != key
                                              or next_relation.get('offset', 0) != len(result)
                                              or next_relation.get('total') != relation.get('total')
                                              or next_relation['certainty'] != 'confirmed'
                                              or next_relation['state'] not in ('complete', 'partial'))
                if continued_relation_changed:
                    self.fail('observation_incomplete', 'Relation changed while resolving the target.')
                if batch is not None:
                    self._remember_nodes(batch)
                self.edge_sources[cache_key].append(page['observed']['id'])
                result.extend(next_relation['targets'])
                relation = next_relation
            unread_remainder = (len(result) > self.max_nodes or (relation['state'] != 'complete' and
                                                                 (not seen or relation.get('total') != len(result))))
            if unread_remainder:
                self.fail('observation_incomplete', 'The query needs a relation with an unread remainder.')
        self._use_relation(token, key)
        self.completed_edges[cache_key] = result
        return result
