"""Exact native hits may be passive members, never independent controls."""
import unittest
from types import SimpleNamespace

from computer_use.errors import ComputerUseError
from computer_use.execution.control_part import ControlPartQuery
from computer_use.execution.engine import Engine
from computer_use.execution.native import (Actuator, Channel, Confirmation, Dispatch, Facts, HostPolicy, InputContext,
    Intent, Knowledge as K, Primitive, SelectionDependency, StepResult, UnitCommand)
from computer_use.execution.transport import ConnectionPort, intent_key
from .test_receiver_query import ReceiverPort
from .test_target_query import ROOT, node


def graph():
    return dict(root=node('AXButton', children=['layout']),
                layout=node('AXGroup', name='Visible section', children=['label']),
                label=node('AXStaticText', name='Go'))


class PartPort(ReceiverPort):
    capabilities = frozenset(('nativeControlParts', 'nativeSelectionBasis'))
    direct = False
    uncertain = False
    prepared = True

    async def inspect(self, operation, target, timeout_ms, *, within=(), actuator=None, basis=()):
        self.events.append(('inspect', target, actuator, basis))
        if actuator is not None: assert basis
        pointer = Primitive('pointer', 'pointer_sequence', Channel.POINTER, K.YES,
            K.YES if actuator and self.prepared else K.UNKNOWN,
            pending=() if actuator and self.prepared else ('receiver',) if actuator else ('geometry',),
            background_preparation=K.YES)
        primitives = (pointer,)
        if self.direct:
            primitives += (Primitive('press', 'native_action', Channel.OBJECT, K.YES, K.YES, action='AXPress'),)
        return Facts(target, 'now', 'button', K.YES, K.YES, K.YES, (), primitives,
                     InputContext(pointer_hit='label'), platform='macos', native_role='AXButton',
                     actions_complete=True, actuator=actuator)

    async def execute(self, operation, step, command, timeout_ms):
        self.events.append(('execute', command))
        if command.prepare:
            self.prepared = True
            return StepResult(command.target, Dispatch.NOT_DISPATCHED, 'prepared', confirmation=Confirmation.CONFIRMED)
        return StepResult(command.target, Dispatch.UNKNOWN if self.uncertain else Dispatch.DISPATCHED,
                          'sent once', error_code='execution_unknown' if self.uncertain else None)


class ControlPartTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_control_members_use_native_sources_without_text_constraints(self):
        for role in ('AXStaticText', 'AXHeading', 'AXImage'):
            data = graph(); data['label']['values']['AXRole'] = role
            port = ReceiverPort(data)
            query = ControlPartQuery(port, 'op', ROOT, lambda: 1000)
            self.assertEqual(await query.resolve('label'), Actuator('label', 'controlPart'))
            self.assertEqual({d.node for d in query.basis}, {'root', 'layout', 'label'})
            self.assertTrue(all(not set(d.fields) & {'AXTitle', 'AXValue', 'AXFocused'} for d in query.basis))
            self.assertTrue(all(d.actions for d in query.basis if d.node != 'root'))

    async def test_hit_does_not_authorize_an_independent_or_incompletely_known_child(self):
        for role in ('AXButton', 'AXLink', 'AXTextField', 'AXCell', 'AXRow', 'AXWindow'):
            data = graph(); data['label']['values']['AXRole'] = role
            self.assertIsNone(await ControlPartQuery(ReceiverPort(data), 'op', ROOT, lambda: 1000).resolve('label'))
        for flag in ('AXEditable', 'settable.AXFocused', 'settable.AXValue', 'settable.AXSelectedText'):
            for state in ('writable', 'unreadable'):
                data = graph()
                if state == 'writable': data['label']['values'][flag] = True
                else: data['label']['statuses'][flag] = 'error'
                self.assertIsNone(await ControlPartQuery(ReceiverPort(data), 'op', ROOT, lambda: 1000).resolve('label'))
        for catalogue in (dict(state='partial', items=[]), dict(state='complete', items=[dict(name='AXShowMenu', description='menu')])):
            data = graph(); data['label']['actions'] = catalogue
            self.assertIsNone(await ControlPartQuery(ReceiverPort(data), 'op', ROOT, lambda: 1000).resolve('label'))

    async def test_generic_containers_are_not_buttons_even_with_one_passive_child(self):
        for role in ('AXGroup', 'AXWindow', 'AXTable', 'AXCell', 'AXRow'):
            data = graph(); data['root']['values']['AXRole'] = role
            port = ReceiverPort(data)
            self.assertIsNone(await ControlPartQuery(port, 'op', ROOT, lambda: 1000).resolve('label'))
            self.assertEqual(len(port.events), 1)

    async def test_actionable_layout_right_click_preserves_pointer_semantics(self):
        data = graph()
        data['root']['values']['AXRole'] = 'AXGroup'
        data['root']['actions']['items'] = [dict(name=a, description=a) for a in ('AXPress', 'AXShowMenu')]
        data['layout']['actions']['items'] = [dict(name=a, description=a) for a in ('AXShowMenu', 'AXScrollToVisible')]
        port = PartPort(data)
        await Engine(port).execute(Intent('click', {'button': 'right'}), ROOT)
        commands = [e[1] for e in port.events if e[0] == 'execute']
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0].primitive.unit, 'pointer_sequence')
        self.assertEqual(commands[0].intent.parameters['button'], 'right')
        self.assertEqual(commands[0].actuator.node, 'label')
        selected = next(e for e in port.events if e[0] == 'inspect' and e[2] is not None)
        self.assertTrue(any(d.node == 'root' and d.actions for d in selected[3]))

    async def test_context_support_does_not_absorb_independent_controls_or_actions(self):
        for action in ('AXPress', 'AXDelete', 'CustomAction'):
            data = graph()
            data['root']['values']['AXRole'] = 'AXGroup'
            data['root']['actions']['items'] = [dict(name=a, description=a) for a in ('AXPress', 'AXShowMenu')]
            data['layout']['actions']['items'] = [dict(name=action, description=action)]
            self.assertIsNone(await ControlPartQuery(ReceiverPort(data), 'op', ROOT, lambda: 1000,
                intent=Intent('click', {'button': 'right'})).resolve('label'))
        data['layout']['actions']['items'] = [dict(name='AXShowMenu', description='menu')]
        self.assertIsNone(await ControlPartQuery(ReceiverPort(data), 'op', ROOT, lambda: 1000,
            intent=Intent('click', {'button': 'left'})).resolve('label'))
        data['label']['values']['AXRole'] = 'AXButton'
        self.assertIsNone(await ControlPartQuery(ReceiverPort(data), 'op', ROOT, lambda: 1000,
            intent=Intent('click', {'button': 'right'})).resolve('label'))

    async def test_unrelated_sibling_is_not_in_the_selection_basis(self):
        data = graph(); data['root']['children'].insert(0, 'independent')
        data['independent'] = node('AXButton')
        query = ControlPartQuery(ReceiverPort(data), 'op', ROOT, lambda: 1000)
        self.assertEqual(await query.resolve('label'), Actuator('label', 'controlPart'))
        self.assertNotIn('independent', {d.node for d in query.basis})

    async def test_parent_can_skip_a_layout_but_cannot_leave_the_requested_control(self):
        for parent in ('root', 'layout', 'outside'):
            data = graph(); data['label']['parents'] = [parent]
            query = ControlPartQuery(ReceiverPort(data), 'op', ROOT, lambda: 1000)
            if parent == 'outside':
                with self.assertRaises(ComputerUseError): await query.resolve('label')
            else: self.assertEqual(await query.resolve('label'), Actuator('label', 'controlPart'))

    async def test_partial_relations_never_justify_a_composed_click(self):
        port = ReceiverPort(graph())
        port.edges['layout', 'AXChildren'] = dict(targets=['label'], state='partial', certainty='confirmed')
        with self.assertRaises(ComputerUseError) as error:
            await ControlPartQuery(port, 'op', ROOT, lambda: 1000).resolve('label')
        self.assertEqual(error.exception.code, 'observation_incomplete')

    async def test_engine_prefers_native_action_and_only_queries_a_needed_pointer_member(self):
        for direct, options, resolve in ((False, {}, True), (True, {}, False), (True, {'button':'right'}, True)):
            port = PartPort(graph()); port.direct = direct
            await Engine(port).execute(Intent('click', options), ROOT)
            self.assertEqual(any(e[0] == 'nodes' for e in port.events), resolve)
            commands = [e[1] for e in port.events if e[0] == 'execute']
            self.assertEqual(len(commands), 1)
            self.assertEqual(commands[0].target, ROOT)
            self.assertEqual(commands[0].actuator, Actuator('label', 'controlPart') if resolve else None)
            self.assertEqual(commands[0].primitive.unit, 'pointer_sequence' if resolve else 'native_action')

    async def test_uncertain_composed_pointer_is_never_replayed(self):
        port = PartPort(graph()); port.uncertain = True
        with self.assertRaises(ComputerUseError): await Engine(port).execute(Intent('click'), ROOT)
        self.assertEqual(sum(e[0] == 'execute' for e in port.events), 1)

    async def test_preparation_rechecks_the_member_without_discarding_its_contract(self):
        port = PartPort(graph()); port.prepared = False
        await Engine(port).execute(Intent('click'), ROOT)
        self.assertEqual([e[2] for e in port.events if e[0] == 'inspect'],
                         [None, Actuator('label', 'controlPart'), Actuator('label', 'controlPart')])
        self.assertEqual([e[1].prepare for e in port.events if e[0] == 'execute'], [True, False])

    async def test_old_driver_cannot_inspect_prepare_or_dispatch_a_control_part(self):
        async def unexpected(*args):
            raise AssertionError('Old driver must not receive a new contract')
        connection = SimpleNamespace(protocol_version=5, session_identity=('s', 1), platform='macos',
            capabilities=frozenset(('nativeElementPointer', 'nativeSelectionBasis')), request=unexpected)
        port = ConnectionPort(connection, HostPolicy())
        intent = Intent('click')
        port._operations['op'] = (connection.session_identity, intent_key(dict(kind='click', parameters={})))
        selected = Actuator('label', 'controlPart')
        with self.assertRaisesRegex(ComputerUseError, 'nativeControlParts'):
            await port.inspect('op', ROOT, 1000, actuator=selected,
                               basis=(SelectionDependency('observed', 'label', ('AXRole',)),))
        pointer = Primitive('p', 'pointer_sequence', Channel.POINTER, K.YES, K.YES)
        for prepare in (False, True):
            with self.assertRaisesRegex(ComputerUseError, 'nativeControlParts'):
                await port.execute('op', 1, UnitCommand(pointer, ROOT, intent, selected, prepare=prepare), 1000)
