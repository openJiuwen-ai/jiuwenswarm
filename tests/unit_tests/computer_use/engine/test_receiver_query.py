"""Owner/editor resolution preserves identity, completeness and input scope."""
import unittest

from computer_use.errors import ComputerUseError
from computer_use.execution.engine import Engine
from computer_use.execution.native import (Actuator, Channel, Confirmation, Dispatch, Facts, InputContext,
    Intent, Knowledge as K, Primitive, StepResult)
from computer_use.execution.receiver import ReceiverQuery, ReceiverResolution
from .test_target_query import GraphPort, ROOT, node


def editor(**values):
    return node('AXTextField', **{'settable.AXValue': True, 'settable.AXFocused': True, **values})


def parented(value, parent):
    return {**value, 'parents': [parent]}


class ReceiverPort(GraphPort):
    async def nodes(self, operation, target, fields, max_nodes, max_depth, timeout_ms):
        result = await super().nodes(operation, target, fields, max_nodes, max_depth, timeout_ms)
        self.events[-1] += (max_depth,)
        if 'parents' in self.graph[target.node]:
            parents = self.graph[target.node]['parents']
            result['requestedRelations'].append('AXParent')
            result['relations'].append(dict(source=target.node, property='AXParent', targets=parents,
                                            state='complete', certainty='confirmed'))
            result['unread'] = sorted(set(result['unread'] + parents) - {target.node})
        return result


class DeferredReceiverPort(ReceiverPort):
    """A provider exposes editing on the chosen control only after preparation."""
    capabilities = frozenset(('nativeActuators', 'nativeTextReplacement', 'nativeEditorPreparation'))

    def __init__(self, *, contained=False, exposes=True):
        self.receiver = 'editor' if contained else 'root'
        graph = {self.receiver: node('AXTextField', **{'settable.AXFocused': True})}
        if contained:
            graph.update(root=node('AXComboBox', children=['layout']), layout=node(children=['editor']))
        super().__init__(graph)
        self.exposes, self.prepared = exposes, False
        self.commands, self.inspections, self.payloads = [], [], []

    async def begin(self, intent, target, timeout_ms):
        self.intent = intent
        return await super().begin(intent, target, timeout_ms)

    async def inspect(self, operation, target, timeout_ms, *, within=(), actuator=None, basis=()):
        self.inspections.append((actuator, basis))
        # Advertise a ready keyboard even without editing declarations. The
        # engine must carry the query's qualification through to route policy.
        kind = 'clipboard_paste' if self.intent.kind == 'paste' else 'key_sequence'
        primitives = [Primitive('keys', kind, Channel.KEYBOARD, K.YES, K.YES)]
        if not self.prepared:
            primitives.insert(0, Primitive('prepare', 'prepare_receiver', Channel.EDITOR, K.YES, K.NO,
                pending=('receiver',), background_preparation=K.YES))
        elif self.exposes and self.intent.kind != 'paste':
            primitives.insert(0, Primitive('edit', 'native_text_edit', Channel.EDITOR, K.YES, K.YES))
        return Facts(target, 'context', 'text control', K.YES, K.YES, K.YES, (), tuple(primitives),
            InputContext(), platform='macos', native_role='AXTextField', actuator=actuator)

    async def execute(self, operation, step, command, timeout_ms):
        self.commands.append(command)
        if command.prepare:
            self.prepared = True
            if self.exposes:
                self.graph[self.receiver]['values']['settable.AXSelectedText'] = True
            return StepResult(command.target, Dispatch.NOT_DISPATCHED, 'native focus',
                preparation=('native element focus',), confirmation=Confirmation.CONFIRMED)
        self.payloads.append(dict(command.intent.parameters))
        return StepResult(command.target, Dispatch.DISPATCHED, 'sent once')


class ReceiverEngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_ready_owner_setter_does_not_bypass_an_unfocused_internal_editor(self):
        class ObjectOwnerPort(DeferredReceiverPort):
            def __init__(self, contained):
                super().__init__(contained=contained)
                self.graph['root']['values'].update({'settable.AXValue': True, 'settable.AXFocused': True})

            async def inspect(self, operation, target, timeout_ms, *, within=(), actuator=None, basis=()):
                if actuator is not None:
                    return await super().inspect(operation, target, timeout_ms, within=within, actuator=actuator, basis=basis)
                self.inspections.append((actuator, basis))
                return Facts(target, 'context', 'compound field', K.YES, K.YES, K.YES, (),
                    (Primitive('value', 'native_text_edit', Channel.OBJECT, K.YES, K.YES),),
                    InputContext(), platform='macos', native_role='AXComboBox')

        for contained in (True, False):
            with self.subTest(contained=contained):
                port = ObjectOwnerPort(contained)
                await Engine(port).execute(Intent('replace', {'text': 'new content'}), ROOT)
                self.assertEqual(port.payloads, [{'text': 'new content'}])
                self.assertTrue(all(c.target == ROOT and not c.foreground for c in port.commands))
                if contained:
                    self.assertEqual([c.primitive.channel for c in port.commands], [Channel.EDITOR, Channel.KEYBOARD])
                    self.assertTrue(all(c.actuator == Actuator('editor') for c in port.commands))
                    self.assertTrue(any(d.node == 'root' and 'AXChildren' in d.relations
                        for _, basis in port.inspections for d in basis))
                else:
                    self.assertEqual(len(port.commands), 1)
                    self.assertEqual(port.commands[0].primitive.channel, Channel.OBJECT)
                    self.assertIsNone(port.commands[0].actuator)

    async def test_deferred_direct_and_contained_editors_are_requeried_before_content(self):
        for contained in (False, True):
            for kind in ('insert', 'replace', 'paste'):
                with self.subTest(contained=contained, kind=kind):
                    port = DeferredReceiverPort(contained=contained)
                    result = await Engine(port).execute(Intent(kind, {'text': 'one value'}), ROOT)
                    self.assertEqual(result['status'], 'dispatched')
                    # Only the re-read editing declaration authorizes the
                    # already-ready keyboard; preparation alone still cannot.
                    self.assertEqual([(c.primitive.unit, c.prepare) for c in port.commands],
                        [('prepare_receiver', True), ('clipboard_paste' if kind == 'paste' else 'key_sequence', False)])
                    self.assertEqual(port.payloads, [{'text': 'one value'}])
                    self.assertTrue(all(c.target == ROOT and not c.foreground for c in port.commands))
                    self.assertGreaterEqual(sum(e[0] == 'nodes' and e[1].node == port.receiver for e in port.events), 2)
                    if contained:
                        self.assertTrue(all(c.actuator == Actuator('editor') for c in port.commands))
                        self.assertTrue(all(basis for actuator, basis in port.inspections if actuator is not None))
                    self.assertEqual(port.events[-1], ('finish', 'op'))

    async def test_successful_preparation_without_editing_evidence_never_sends_content(self):
        for contained in (False, True):
            for kind in ('insert', 'replace', 'paste'):
                with self.subTest(contained=contained, kind=kind):
                    port = DeferredReceiverPort(contained=contained, exposes=False)
                    with self.assertRaises(ComputerUseError) as caught:
                        await Engine(port).execute(Intent(kind, {'text': 'must not send'}), ROOT)
                    self.assertEqual((caught.exception.code, caught.exception.effect), ('input_unavailable', 'possible'))
                    self.assertIn('text-editing capability', caught.exception.detail)
                    self.assertEqual(port.payloads, [])
                    self.assertEqual(len(port.commands), 1)
                    self.assertTrue(port.commands[0].prepare)
                    self.assertFalse(port.calls.retired)
                    self.assertEqual(port.events[-1], ('finish', 'op'))

    async def test_missing_preparation_capability_does_not_send_a_probe(self):
        port = DeferredReceiverPort()
        port.capabilities = port.capabilities - {'nativeEditorPreparation'}
        with self.assertRaises(ComputerUseError) as caught:
            await Engine(port).execute(Intent('insert', {'text': 'must not send'}), ROOT)
        self.assertEqual(caught.exception.effect, 'none')
        self.assertEqual(port.commands, [])


class ReceiverQueryTests(unittest.IsolatedAsyncioTestCase):
    def query(self, graph=None, **options):
        self.port = ReceiverPort(graph or dict(root=node('AXComboBox', children=['layout']),
                                              layout=node(children=['editor']), editor=editor()))
        return ReceiverQuery(self.port, 'op', ROOT, lambda: 1000, **options)

    async def error(self, query, code):
        with self.assertRaises(ComputerUseError) as caught:
            await query.resolve()
        self.assertEqual((caught.exception.code, caught.exception.effect), (code, 'none'))

    async def test_scalar_value_and_focus_declarations_do_not_hide_an_internal_editor(self):
        for role in ('AXTextField', 'AXComboBox', 'AXIncrementor'):
            with self.subTest(role=role):
                query = self.query(dict(root=node(role, children=['editor'],
                    **{'settable.AXValue': True, 'settable.AXFocused': True}), editor=parented(editor(), 'root')))
                self.assertEqual(await query.resolve(require_editable=True), ReceiverResolution(Actuator('editor')))

    async def test_scalar_editor_does_not_reinterpret_a_readonly_child_as_editable_owner(self):
        query = self.query(dict(root=editor(children=['child']), child=editor(AXEditable=False)))
        with self.assertRaises(ComputerUseError) as caught:
            await query.resolve(require_editable=True)
        self.assertEqual(caught.exception.code, 'input_unavailable')

    async def test_direct_scalar_editor_cannot_assume_unread_contents_are_empty(self):
        query = self.query(dict(root=editor()))
        self.port.edges['root', 'AXChildren'] = dict(targets=[], state='timeout', certainty='confirmed')
        await self.error(query, 'observation_incomplete')

    async def test_focused_internal_editor_can_belong_to_a_focusable_field(self):
        query = self.query(dict(root=node('AXComboBox', children=['editor'], AXEditable=True,
            **{'settable.AXFocused': True}), editor=parented(editor(), 'root')))
        self.assertEqual(await query.resolve(require_editable=True, focused='editor'),
                         ReceiverResolution(Actuator('editor')))
        self.assertTrue(any('AXParent' in dependency.relations for dependency in query.basis))

    async def test_independent_editor_in_a_rich_document_is_not_that_documents_editor(self):
        query = self.query(dict(root=node('AXTextArea', children=['editor'], AXEditable=True,
            **{'settable.AXFocused': True}), editor=parented(editor(), 'root')))
        self.assertEqual(await query.resolve(require_editable=True, focused='editor'), ReceiverResolution())
        self.assertEqual(self.port.events, [('nodes', ROOT, 0)])

    async def test_focusable_field_cannot_choose_between_two_internal_editors(self):
        query = self.query(dict(root=node('AXComboBox', children=['a', 'b'], AXEditable=True,
            **{'settable.AXFocused': True}), a=parented(editor(), 'root'), b=parented(editor(), 'root')))
        with self.assertRaises(ComputerUseError) as caught:
            await query.resolve(require_editable=True, focused='a')
        self.assertEqual(caught.exception.code, 'ambiguous_target')

    async def test_cold_text_control_can_be_prepared_without_claiming_editability(self):
        for role in ('AXTextField', 'AXTextArea', 'AXComboBox', 'AXIncrementor'):
            children = ['unread-document-content'] if role == 'AXTextArea' else []
            query = self.query(dict(root=node(role, children=children, **{'settable.AXFocused': True})))
            resolved = await query.resolve(require_editable=True, allow_preparation=True)
            self.assertEqual(resolved, ReceiverResolution(needs_editing_evidence=True))
            self.assertEqual(self.port.events, [('nodes', ROOT, 0)])

    async def test_cold_scalar_owner_uses_existing_editor_before_preparing_itself(self):
        query = self.query(dict(root=node('AXComboBox', children=['child'],
            **{'settable.AXFocused': True}), child=parented(editor(), 'root')))
        self.assertEqual(await query.resolve(require_editable=True, allow_preparation=True),
            ReceiverResolution(Actuator('child')))

    async def test_focusable_container_or_readonly_control_is_not_a_preparation_candidate(self):
        cases = [(role, {}) for role in ('AXGroup', 'AXUnknown', 'AXStaticText', 'AXRow', 'AXCell', 'AXWebArea')]
        cases += [('AXTextField', dict(AXEditable=False)), ('AXTextField', dict(AXEnabled=False))]
        for role, values in cases:
            query = self.query(dict(root=node(role, **{'settable.AXFocused': True, **values})))
            with self.subTest(role=role, values=values), self.assertRaises(ComputerUseError):
                await query.resolve(require_editable=True, allow_preparation=True)

    async def test_unknown_or_missing_focus_declaration_does_not_authorize_preparation(self):
        for writable in (False, None):
            values = {} if writable is None else {'settable.AXFocused': writable}
            query = self.query(dict(root=node('AXTextField', **values)))
            with self.assertRaises(ComputerUseError):
                await query.resolve(require_editable=True, allow_preparation=True)

    async def test_unique_contained_candidate_keeps_owner_and_selection_basis(self):
        query = self.query(dict(root=node('AXComboBox', children=['layout']), layout=node(children=['editor']),
            editor=node('AXTextField', **{'settable.AXFocused': True})))
        selected = await query.resolve(require_editable=True, allow_preparation=True)
        self.assertEqual(selected, ReceiverResolution(Actuator('editor'), needs_editing_evidence=True))
        self.assertEqual(query.root, ROOT)
        fields = {d.node: d.fields for d in query.basis}
        self.assertIn('settable.AXFocused', fields['editor'])

    async def test_preparation_does_not_pick_one_of_two_independent_editors(self):
        for second in (editor(), node('AXTextField', AXEditable=False), node('AXTextField', **{'settable.AXFocused': True})):
            query = self.query(dict(root=node('AXComboBox', children=['first', 'second']),
                first=node('AXTextField', **{'settable.AXFocused': True}), second=second))
            with self.assertRaises(ComputerUseError) as caught:
                await query.resolve(require_editable=True, allow_preparation=True)
            self.assertEqual(caught.exception.code, 'ambiguous_target')

    async def test_composite_resolves_through_layout_without_changing_requested_owner(self):
        query = self.query()
        self.assertEqual(await query.resolve(), ReceiverResolution(Actuator('editor')))
        self.assertEqual(query.root, ROOT)
        self.assertEqual([call[1].node for call in self.port.events], ['root', 'layout', 'editor'])

    async def test_focused_editor_requires_a_current_parent_path_to_owner(self):
        query = self.query(dict(root=node('AXComboBox'), layout=parented(node(), 'root'),
            editor=parented(editor(), 'layout')))
        result = await query.resolve(require_editable=True, focused='editor')
        self.assertEqual(result, ReceiverResolution(Actuator('editor')))
        self.assertEqual(query.root, ROOT)
        dependencies = {entry.node: entry for entry in query.basis}
        self.assertIn('AXParent', dependencies['editor'].relations)
        self.assertIn('AXParent', dependencies['layout'].relations)
        self.assertTrue(dependencies['layout'].actions)

    async def test_reverse_editor_can_supply_editing_evidence_before_owner_preparation(self):
        query = self.query(dict(root=node('AXComboBox', **{'settable.AXFocused': True}),
            editor=parented(editor(), 'root')))
        self.assertEqual(await query.resolve(require_editable=True, allow_preparation=True, focused='editor'),
            ReceiverResolution(Actuator('editor')))

    async def test_unrelated_focus_does_not_block_owner_preparation_or_add_dependencies(self):
        for role in ('AXTextField', 'AXRow', 'AXCell', 'AXTable', 'AXWindow'):
            query = self.query(dict(root=node('AXComboBox', **{'settable.AXFocused': True}),
                editor=parented(editor(), 'other'), other=parented(node(role), 'root')))
            self.assertEqual(await query.resolve(require_editable=True, allow_preparation=True, focused='editor'),
                ReceiverResolution(needs_editing_evidence=True))
            self.assertEqual({entry.node for entry in query.basis}, {'root'})

    async def test_reverse_path_cannot_cross_independent_or_unreadable_layout(self):
        action, partial = node(), node()
        action['actions']['items'] = [dict(name='AXPress', description='press')]
        partial['actions']['state'] = 'partial'
        for layout in (action, node(AXEditable=True), node(AXEnabled=False), partial):
            query = self.query(dict(root=node('AXComboBox'), editor=parented(editor(), 'layout'),
                layout=parented(layout, 'root')))
            with self.assertRaises(ComputerUseError) as caught:
                await query.resolve(require_editable=True, focused='editor')
            self.assertEqual(caught.exception.code, 'input_unavailable')

    async def test_reverse_path_does_not_hide_a_second_editor(self):
        query = self.query(dict(root=node('AXComboBox', children=['other']),
            editor=parented(editor(), 'root'), other=parented(editor(), 'root')))
        with self.assertRaises(ComputerUseError) as caught:
            await query.resolve(require_editable=True, focused='editor')
        self.assertEqual(caught.exception.code, 'ambiguous_target')

    async def test_reverse_path_cannot_replace_missing_candidate_coverage(self):
        query = self.query(dict(root=node('AXComboBox'), editor=parented(editor(), 'root')))
        self.port.edges[('root', 'AXChildren')] = dict(targets=[], state='partial',
            certainty='confirmed', offset=0, total=1)
        with self.assertRaises(ComputerUseError) as caught:
            await query.resolve(require_editable=True, focused='editor')
        self.assertEqual(caught.exception.code, 'observation_incomplete')

    async def test_missing_cyclic_or_multiple_parents_do_not_establish_ownership(self):
        for parents in ([], ['editor'], ['root', 'other']):
            query = self.query(dict(root=node('AXComboBox', **{'settable.AXFocused': True}),
                editor={**editor(), 'parents': parents}, other=node()))
            resolved = await query.resolve(require_editable=True, allow_preparation=True, focused='editor')
            self.assertEqual(resolved, ReceiverResolution(needs_editing_evidence=True))
            self.assertEqual({entry.node for entry in query.basis}, {'root'})

    async def test_focused_child_of_an_ordinary_form_does_not_make_the_form_an_editor(self):
        query = self.query(dict(root=node('AXGroup'), editor=parented(editor(), 'root')))
        with self.assertRaises(ComputerUseError):
            await query.resolve(require_editable=True, focused='editor')
        self.assertEqual(self.port.events, [('nodes', ROOT, 0)])

    async def test_ordinary_form_is_not_its_only_observed_field(self):
        for role in ('AXGroup', 'AXUnknown', 'AXWindow', 'AXTable', 'AXRow'):
            with self.subTest(role=role):
                query = self.query(dict(root=node(role, children=['editor']), editor=editor()))
                self.assertEqual(await query.resolve(), ReceiverResolution())
                self.assertEqual(len(self.port.events), 1)

    async def test_text_intents_require_an_editor_but_focus_and_key_may_address_other_controls(self):
        for role in ('AXGroup', 'AXUnknown', 'AXWindow', 'AXTable', 'AXRow', 'AXStaticText'):
            query = self.query(dict(root=node(role, children=['editor']), editor=editor()))
            with self.assertRaises(ComputerUseError) as caught:
                await query.resolve(require_editable=True)
            self.assertEqual(caught.exception.code, 'input_unavailable')
            self.assertEqual(len(self.port.events), 1)

    async def test_direct_editable_document_does_not_require_enumerating_its_contents(self):
        for role in ('AXTextArea', 'AXWebArea', 'AXDocument'):
            query = self.query(dict(root=node(role, AXEditable=True, children=['large-document'])))
            self.assertEqual(await query.resolve(require_editable=True), ReceiverResolution())
            self.assertEqual(self.port.events, [('nodes', ROOT, 0)])

    async def test_text_intent_stops_at_an_explicitly_read_only_owner(self):
        query = self.query(dict(root=node('AXComboBox', AXEditable=False, children=['editor']), editor=editor()))
        with self.assertRaises(ComputerUseError) as caught:
            await query.resolve(require_editable=True)
        self.assertEqual(caught.exception.code, 'input_unavailable')
        self.assertEqual(len(self.port.events), 1)

    async def test_explicit_editing_container_can_resolve_its_contained_editor(self):
        for role in ('AXGroup', 'AXUnknown', 'AXCell', 'AXTextField'):
            with self.subTest(role=role):
                query = self.query(dict(root=node(role, AXEditable=True, children=['editor']), editor=editor()))
                self.assertEqual(await query.resolve(), ReceiverResolution(Actuator('editor')))

    async def test_multiple_controls_stay_ambiguous_even_when_one_cannot_be_edited(self):
        for values in ({}, {'AXEnabled': False}, {'AXEditable': False}):
            with self.subTest(values=values):
                query = self.query(dict(root=node('AXComboBox', children=['first', 'second']),
                                        first=editor(), second=editor(**values)))
                await self.error(query, 'ambiguous_target')

    async def test_unknown_editability_cannot_prove_unique_receiver(self):
        query = self.query(dict(root=node('AXComboBox', children=['second']), second=node('AXTextField')))
        self.port.graph['second']['statuses']['settable.AXValue'] = 'timeout'
        await self.error(query, 'observation_incomplete')

    async def test_failed_children_read_cannot_be_treated_as_empty(self):
        for state in ('partial', 'timeout'):
            query = self.query()
            self.port.edges['root', 'AXChildren'] = dict(targets=[], state=state, certainty='confirmed')
            await self.error(query, 'observation_incomplete')

    async def test_continuation_is_read_before_deciding_uniqueness(self):
        query = self.query(dict(root=node('AXComboBox'), first=editor(), second=editor()))
        self.port.edges['root', 'AXChildren'] = dict(targets=['first'], state='partial', certainty='confirmed',
            offset=0, total=2, cursor='next')
        self.port.pages['next'] = dict(source='root', property='AXChildren', targets=['second'], state='partial',
            certainty='confirmed', offset=1, total=2)
        await self.error(query, 'ambiguous_target')
        self.assertIn(('relation', 'next'), self.port.events)

    async def test_conflicting_parent_is_not_overridden_by_forward_children(self):
        query = self.query()
        self.port.graph['editor']['parents'] = ['another-control']
        await self.error(query, 'observation_incomplete')

    async def test_matching_or_missing_reverse_parent_keeps_positive_containment(self):
        for parents in ([], ['layout']):
            query = self.query()
            self.port.graph['editor']['parents'] = parents
            self.assertEqual(await query.resolve(), ReceiverResolution(Actuator('editor')))

    async def test_reverse_parent_can_skip_observed_layout_wrappers_inside_owner(self):
        query = self.query()
        self.port.graph['editor']['parents'] = ['root']
        self.assertEqual(await query.resolve(), ReceiverResolution(Actuator('editor')))
        self.assertEqual(query.root, ROOT)

    async def test_reverse_parent_outside_requested_owner_or_ambiguous_still_refuses(self):
        for parents in (['window'], ['layout', 'root'], ['editor']):
            with self.subTest(parents=parents):
                query = self.query()
                self.port.graph['editor']['parents'] = parents
                await self.error(query, 'observation_incomplete')

    async def test_cycles_and_budgets_report_incompleteness(self):
        query = self.query(dict(root=node('AXComboBox', children=['layout']), layout=node(children=['root'])))
        await self.error(query, 'observation_incomplete')
        for options in (dict(max_depth=0), dict(max_nodes=2)):
            await self.error(self.query(**options), 'observation_incomplete')

    async def test_does_not_enter_separate_controls_or_actionful_wrappers(self):
        for role in ('AXCell', 'AXTable', 'AXWindow', 'AXButton'):
            query = self.query(dict(root=node('AXComboBox', children=['boundary']),
                boundary=node(role, children=['editor']), editor=editor()))
            self.assertEqual(await query.resolve(), ReceiverResolution())
        query = self.query()
        self.port.graph['layout']['actions']['items'] = [dict(name='AXPress', description='Press')]
        self.assertEqual(await query.resolve(), ReceiverResolution())
        query = self.query()
        self.port.graph['layout']['actions']['state'] = 'unavailable'
        await self.error(query, 'observation_incomplete')
