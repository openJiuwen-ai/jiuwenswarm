"""Native v5 wire boundary over a real supervised subprocess, without UI input."""
from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from computer_use.client.connection import Connection
from computer_use.errors import ComputerUseError
from computer_use.execution.engine import Engine
from computer_use.execution.native import Actuator, ScrollActuator, Binding, HostPolicy, Intent, SelectionDependency, VirtualReceiver, FocusMember
from computer_use.execution.transport import ConnectionPort
from computer_use.protocol.execution import decode_execution_result
from computer_use.observation.reader import NativeReader
from computer_use.observation.directory import NativeDirectory, select_surface
from computer_use.observation.capture import NativeImages
from computer_use.protocol.captures import capture_metadata

def unit_frames(requests):
    """Replay the two actual wire boundaries through the native unit decoders."""
    frames = []
    for request in requests:
        if request['method'] == 'begin':
            begin = request['params']
        elif request['method'] == 'execute':
            frames.append(dict(begin=begin, command=request['params']['command']))
    return frames


ROOT = Path(__file__).resolve().parent
TARGET = Binding("native-v4-fixture", "instance", None, "surface", "node")


class NativeTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="native-v4-")
        self.journal = Path(self.directory.name) / "wire.jsonl"
        self.connections = []
        self.invalidations = 0

    async def asyncTearDown(self):
        for connection in self.connections:
            connection.close()
            await connection.wait_closed()
        self.directory.cleanup()

    def engine(self, mode="normal", policy=None):
        def invalidated():
            self.invalidations += 1
        connection = Connection(invalidated, dict(command=sys.executable,
            args=[str(ROOT / "fixtures/v5-driver.py"), mode, str(self.journal)], timeoutMs=3000), protocol_version=5)
        self.connections.append(connection)
        return Engine(ConnectionPort(connection, policy or HostPolicy()))

    def requests(self):
        return [request for line in self.journal.read_text().splitlines() if "method" in (request := json.loads(line))]

    async def test_virtual_receiver_echo_and_capability_are_checked_before_input(self):
        for mode in ('keyboard-virtual', 'keyboard-virtual-wrong-echo', 'keyboard-virtual-missing-echo',
                     'keyboard-virtual-missing-capability'):
            engine = self.engine(mode)
            offset = len(self.requests()) if self.journal.exists() else 0
            async def inspect():
                with engine.native.calls.claim(1000) as remaining:
                    async with engine.native.calls.operation(TARGET, remaining, intent=Intent('insert', {'text': 'test'})) as operation:
                        # This test inspects only; no desktop or fixture input is sent.
                        return await engine.native.inspect(operation.token, operation.target, 500,
                            receiver=VirtualReceiver('popup', 'owns'),
                            basis=(SelectionDependency('observed', 'node', ('AXHasPopup',), False, ('AXOwns',)),))
            if mode == 'keyboard-virtual':
                self.assertEqual((await inspect()).receiver, VirtualReceiver('popup', 'owns'))
            elif mode == 'keyboard-virtual-missing-capability':
                with self.assertRaisesRegex(ComputerUseError, 'nativeVirtualFocus'): await inspect()
            elif mode == 'keyboard-virtual-missing-echo':
                with self.assertRaises(ComputerUseError): await inspect()
            else:
                with self.assertRaises(ValueError): await inspect()
            self.assertFalse(any(r['method'] == 'execute' for r in self.requests()[offset:]))

    async def test_focus_member_echo_and_capability_are_checked_before_input(self):
        for mode in ('keyboard-member', 'keyboard-member-wrong-echo', 'keyboard-member-missing-echo',
                     'keyboard-member-missing-capability'):
            engine = self.engine(mode)
            offset = len(self.requests()) if self.journal.exists() else 0
            async def inspect():
                with engine.native.calls.claim(1000) as remaining:
                    async with engine.native.calls.operation(TARGET, remaining, intent=Intent('key', {'key': 'Return'})) as operation:
                        return await engine.native.inspect(operation.token, operation.target, 500,
                            receiver=FocusMember('label'),
                            basis=(SelectionDependency('observed', 'label', ('AXRole',), True, ('AXParent',)),))
            if mode == 'keyboard-member':
                self.assertEqual((await inspect()).receiver, FocusMember('label'))
            elif mode == 'keyboard-member-missing-capability':
                with self.assertRaisesRegex(ComputerUseError, 'nativeFocusMembers'): await inspect()
            elif mode == 'keyboard-member-missing-echo':
                with self.assertRaises(ComputerUseError): await inspect()
            else:
                with self.assertRaises(ValueError): await inspect()
            self.assertFalse(any(r['method'] == 'execute' for r in self.requests()[offset:]))

    async def test_native_scroll_selects_the_page_in_python_and_sends_the_original_intent_once(self):
        result = await self.engine('scroll-native').execute(Intent('scroll', dict(direction='down', pages=2)), TARGET)
        self.assertEqual(result['status'], 'dispatched')
        calls = self.requests()
        selected = [r['params']['query'].get('actuator') for r in calls if r['method'] == 'inspect'
                    and r['params']['query']['kind'] == 'inputContext']
        self.assertEqual(selected, [ScrollActuator('down', 'node', 'bar').wire()])
        commands = [r['params']['command'] for r in calls if r['method'] == 'execute']
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0]['kind'], 'native_scroll')
        self.assertEqual(commands[0]['target']['node'], TARGET.node)
        self.assertEqual(next(r['params']['intent'] for r in calls if r['method'] == 'begin'), dict(kind='scroll', parameters=dict(direction='down', pages=2)))
        self.assertFalse(commands[0]['foreground'])
        self.assertEqual(calls[-1]['method'], 'finish')

    async def test_native_scroll_partial_is_not_retried_and_changed_selection_is_not_sent(self):
        for mode, writes in [('scroll-partial', 1), ('scroll-wrong-echo', 0)]:
            offset = len(self.requests()) if self.journal.exists() else 0
            with self.assertRaises(ComputerUseError):
                await self.engine(mode).execute(Intent('scroll', dict(direction='down', pages=2)), TARGET)
            self.assertEqual(sum(r['method'] == 'execute' for r in self.requests()[offset:]), writes)

    async def test_native_scroll_pattern_dispatches_without_a_page_button_actuator(self):
        engine = self.engine('scroll-pattern')
        result = await engine.execute(Intent('scroll', dict(direction='right', pages=.5)), TARGET)
        self.assertEqual(result['status'], 'dispatched')
        calls = self.requests()
        self.assertEqual([r['method'] for r in calls], ['hello', 'begin', 'inspect', 'execute', 'finish'])
        self.assertNotIn('actuator', calls[2]['params']['query'])
        self.assertEqual(calls[3]['params']['command']['kind'], 'native_scroll')
        self.assertFalse(calls[3]['params']['command']['foreground'])
        # Direct ScrollPattern support must not authorize the different page
        # button selection route on a peer that has not implemented it.
        with self.assertRaisesRegex(ComputerUseError, 'nativeScroll'):
            async with engine.native.calls.open(TARGET, 1000) as lease:
                await engine.native.inspect(lease.token, lease.target, 500,
                    actuator=ScrollActuator('down', 'node', 'bar'))

    async def test_scroll_selection_requires_explicit_native_capability(self):
        engine = self.engine('scroll-missing-capability')
        await engine.native.connection.connect()
        with self.assertRaisesRegex(ComputerUseError, 'nativeScroll'):
            async with engine.native.calls.open(TARGET, 1000) as lease:
                await engine.native.inspect(lease.token, lease.target, 500,
                    actuator=ScrollActuator('down', 'node', 'bar'))
        self.assertEqual([r['method'] for r in self.requests()], ['hello', 'begin', 'finish'])

    @unittest.skipUnless(os.environ.get('COMPUTER_USE_NATIVE_INPUT_PROBE'), 'run via test-macos for the native command decoder')
    async def test_production_scroll_commands_pass_real_native_parameter_decoding(self):
        await self.engine('scroll-native').execute(Intent('scroll', dict(direction='down', pages=2)), TARGET)
        command = unit_frames(self.requests())[0]
        commands, expected = [command], [True]
        for p in ({'direction':'up'}, {'direction':'left','pages':3}, {'direction':'down','pages':0.5},
                  {'direction':'up','pages':False}, {'direction':'right','x':1,'y':2}, {'direction':'back'},
                  {'direction':'down','pages':0}):
            changed = deepcopy(command); changed['begin']['intent']['parameters'] = p
            commands.append(changed)
        expected += [True, True, False, False, False, False, False]
        foreground = deepcopy(command); foreground['command']['foreground'] = True
        commands.append(foreground); expected.append(False)
        result = subprocess.run([os.environ['COMPUTER_USE_NATIVE_INPUT_PROBE'], '--scroll-stdio'],
            input=''.join(json.dumps(c) + '\n' for c in commands), text=True, capture_output=True, check=True, timeout=10)
        self.assertEqual([json.loads(line) for line in result.stdout.splitlines()], expected)

    async def test_intent_to_native_contract_and_canonical_binding(self):
        engine = self.engine()
        result = await engine.execute(Intent("action", dict(action="Press")), TARGET)
        self.assertEqual(result["status"], "dispatched")
        requests = self.requests()
        self.assertEqual([r["method"] for r in requests], ["hello", "begin", "inspect", "execute", "finish"])
        self.assertTrue(all(r["version"] == 5 for r in requests))
        self.assertEqual(requests[2]['params']['query'], dict(kind='inputContext', within=[]))
        command = requests[3]["params"]["command"]
        self.assertEqual(command["target"]["rootWindow"], "root")
        self.assertEqual(command["primitiveId"], "declared-action")
        self.assertEqual(requests[1]["params"]["intent"], dict(kind="action", parameters=dict(action="Press")))
        self.assertFalse(command["foreground"])
        self.assertEqual(set(command), {'kind', 'primitiveId', 'target', 'foreground'})

    async def test_key_preparation_and_dispatch_share_operation_target_and_deadline(self):
        receipt = await self.engine('keyboard-prepare').execute(Intent('key', dict(key='Return')), TARGET)
        self.assertEqual(receipt['status'], 'dispatched')
        calls = self.requests()
        self.assertEqual([r['method'] for r in calls], ['hello', 'begin', 'inspect', 'execute', 'inspect', 'execute', 'finish'])
        commands = [r['params']['command'] for r in calls if r['method'] == 'execute']
        self.assertEqual([c['kind'] for c in commands], ['prepare_receiver', 'key_sequence'])
        self.assertEqual(commands[0]['target'], commands[1]['target'])
        self.assertEqual(commands[0]['primitiveId'], commands[1]['primitiveId'])
        self.assertTrue(all('expected' not in command for command in commands))
        self.assertTrue(all(not c['foreground'] for c in commands))
        self.assertEqual(sum(r['method'] == 'begin' for r in calls), 1)
        self.assertTrue(all(r['params'].get('query', {}).get('kind') != 'nodes' for r in calls))

    async def test_old_write_envelope_is_rejected_before_begin_without_compatibility_replay(self):
        with self.assertRaises(ComputerUseError) as caught:
            await self.engine('legacy-execution').execute(Intent('action', dict(action='Press')), TARGET)
        self.assertEqual(caught.exception.code, 'mode_unsupported')
        self.assertEqual(caught.exception.effect, 'none')
        self.assertIn('nativeBoundContracts', caught.exception.detail)
        self.assertEqual([r['method'] for r in self.requests()], ['hello'])

    async def test_fresh_native_contract_ids_cannot_reset_attempt_limits(self):
        for mode, count in [('keyboard-rotate-preparation', 1), ('keyboard-rotate-unavailable', 1),
                            ('keyboard-rotate-precondition', 2)]:
            with self.subTest(mode=mode):
                offset = len(self.requests()) if self.journal.exists() else 0
                with self.assertRaises(ComputerUseError):
                    await self.engine(mode).execute(Intent('key', dict(key='Return')), TARGET)
                calls = self.requests()[offset:]
                commands = [r['params']['command'] for r in calls if r['method'] == 'execute']
                self.assertEqual(len(commands), count)
                self.assertEqual(calls[-1]['method'], 'finish')

    async def test_editor_churn_does_not_produce_an_unbounded_sequence_of_preparations(self):
        with self.assertRaisesRegex(ComputerUseError, 'receiver kept changing'):
            await self.engine('keyboard-actuator-churn').execute(Intent('insert', dict(text='hello')), TARGET)
        commands = [r['params']['command'] for r in self.requests() if r['method'] == 'execute']
        self.assertEqual([c['primitiveId'] for c in commands], ['key-route:editor-0', 'key-route:editor-1'])
        self.assertTrue(all(c['kind'] == 'prepare_receiver' for c in commands))

    async def test_contained_editor_is_resolved_in_python_but_owner_stays_fixed_on_wire(self):
        for mode in ('keyboard-actuator', 'keyboard-actuator-recreated'):
            offset = len(self.requests()) if self.journal.exists() else 0
            result = await self.engine(mode).execute(Intent('insert', dict(text='Hello 中文 😀')), TARGET)
            self.assertEqual(result['status'], 'dispatched')
            calls = self.requests()[offset:]
            commands = [r['params']['command'] for r in calls if r['method'] == 'execute']
            self.assertEqual([c['kind'] for c in commands], ['prepare_receiver', 'key_sequence'])
            self.assertTrue(all(c['target']['node'] == TARGET.node for c in commands))
            self.assertEqual([c['primitiveId'] for c in commands],
                             ['key-route:editor', 'key-route:editor-new' if mode.endswith('recreated') else 'key-route:editor'])
            inspections = [r['params']['query'] for r in calls
                           if r['method'] == 'inspect' and r['params']['query']['kind'] == 'inputContext']
            selected = [q['actuator'] for q in inspections if q.get('actuator')]
            self.assertEqual(len(selected), 2)
            self.assertTrue(all(q['relation'] == 'containedEditor' for q in selected))
            self.assertTrue(any(q.get('actuator') is None for q in inspections))
            self.assertEqual(sum(r['method'] == 'begin' for r in calls), 1)

    async def test_editor_contract_mismatch_stops_before_dispatch(self):
        for mode in ('keyboard-actuator-wrong-echo', 'keyboard-actuator-wrong-owner'):
            offset = len(self.requests()) if self.journal.exists() else 0
            with self.assertRaises(ComputerUseError) as error:
                await self.engine(mode).execute(Intent('insert', dict(text='hello')), TARGET)
            # Malformed replies retire the peer. Its behavior is unknown, even
            # though the client-side journal proves we sent no input command.
            self.assertEqual(error.exception.effect, 'possible')
            self.assertFalse(any(r['method'] == 'execute' for r in self.requests()[offset:]))

    async def test_partial_editor_input_never_resolves_another_receiver(self):
        with self.assertRaises(ComputerUseError) as error:
            await self.engine('keyboard-actuator-partial').execute(Intent('insert', dict(text='hello')), TARGET)
        self.assertEqual(error.exception.effect, 'partial')
        calls = self.requests()
        dispatched = next(i for i, r in enumerate(calls) if r['method'] == 'execute')
        self.assertEqual([r['method'] for r in calls[dispatched:]], ['execute', 'finish'])

    async def test_selected_editor_requires_native_capability_before_sending_query(self):
        port = self.engine('keyboard-ready').native
        opened = await port.begin(Intent('key', dict(key='Return')), TARGET, 3000)
        try:
            with self.assertRaisesRegex(ComputerUseError, 'nativeActuators'):
                await port.inspect(opened.token, opened.target, 1000, actuator=Actuator('editor'))
        finally:
            await port.finish(opened.token)
        self.assertEqual([r['method'] for r in self.requests()], ['hello', 'begin', 'finish'])

    async def test_default_read_profile_negotiates_editing_queries(self):
        for mode, supported in (('nodes', False), ('keyboard-actuator', True), ('keyboard-virtual', False)):
            offset = len(self.requests()) if self.journal.exists() else 0
            snapshot = await NativeReader(self.engine(mode).native).read(TARGET)
            query = next(r['params']['query'] for r in self.requests()[offset:]
                         if r['method'] == 'inspect' and r['params']['query']['kind'] == 'nodes')
            self.assertEqual('settable.AXFocused' in query['fields'], supported)
            self.assertEqual('AXEditable' in query['fields'], supported)
            self.assertNotIn('AXHasPopup', query['fields'])
            if supported:
                self.assertIs(snapshot.native['nodes'][0]['values']['settable.AXFocused'], False)

    async def test_focus_finishes_after_confirmed_preparation_without_key_sequence(self):
        receipt = await self.engine('keyboard-prepare').execute(Intent('focus'), TARGET)
        self.assertEqual(receipt['status'], 'verified')
        commands = [r['params']['command'] for r in self.requests() if r['method'] == 'execute']
        self.assertEqual([c['kind'] for c in commands], ['prepare_receiver'])
        self.assertEqual(next(r['params']['intent'] for r in self.requests() if r['method'] == 'begin'), dict(kind='focus', parameters={}))

    async def test_insertion_and_paste_preserve_intent_through_preparation(self):
        for intent, text, unit in (('insert', 'Hello 中文 😀', 'key_sequence'),
                                   ('paste', 'one\ttwo\nthree', 'clipboard_paste')):
            offset = len(self.requests()) if self.journal.exists() else 0
            with self.subTest(intent=intent):
                result = await self.engine('keyboard-prepare').execute(Intent(intent, dict(text=text)), TARGET)
                self.assertEqual(result['status'], 'dispatched')
                calls = self.requests()[offset:]
                commands = [r['params']['command'] for r in calls if r['method'] == 'execute']
                self.assertEqual([c['kind'] for c in commands], ['prepare_receiver', unit])
                self.assertEqual([r['params']['intent'] for r in calls if r['method'] == 'begin'], [dict(kind=intent, parameters=dict(text=text))])
                self.assertTrue(all('intent' not in c for c in commands))
                self.assertTrue(all(c['target'] == commands[0]['target'] and not c['foreground'] for c in commands))
                self.assertEqual(sum(r['method'] == 'begin' for r in calls), 1)
                self.assertEqual(sum(r['params'].get('query', {}).get('kind') == 'nodes' for r in calls), 2)

    async def test_text_unit_without_actuator_feature_still_checks_editor_semantics(self):
        with self.assertRaises(ComputerUseError) as caught:
            await self.engine('keyboard-noneditor').execute(Intent('insert', dict(text='wrong')), TARGET)
        self.assertEqual(caught.exception.effect, 'none')
        self.assertFalse(any(r['method'] == 'execute' for r in self.requests()))

    async def test_text_editing_evidence_is_refreshed_after_preparation(self):
        with self.assertRaises(ComputerUseError) as caught:
            await self.engine('keyboard-readonly-after-prepare').execute(Intent('insert', dict(text='wrong')), TARGET)
        self.assertEqual(caught.exception.effect, 'possible')
        commands = [r['params']['command'] for r in self.requests() if r['method'] == 'execute']
        self.assertEqual([c['kind'] for c in commands], ['prepare_receiver'])

    async def test_replacement_resolves_editing_semantics_and_preserves_original_owner(self):
        for mode in ('keyboard-replace', 'keyboard-replace-contained'):
            for text in ('Replace 中文 😀', ''):
                offset = len(self.requests()) if self.journal.exists() else 0
                result = await self.engine(mode).execute(Intent('replace', dict(text=text)), TARGET)
                self.assertEqual(result['status'], 'dispatched')
                calls = self.requests()[offset:]
                commands = [r['params']['command'] for r in calls if r['method'] == 'execute']
                self.assertEqual([c['kind'] for c in commands], ['prepare_receiver', 'key_sequence'])
                self.assertTrue(all(c['target']['node'] == TARGET.node and not c['foreground'] for c in commands))
                self.assertEqual([r['params']['intent'] for r in calls if r['method'] == 'begin'], [dict(kind='replace', parameters=dict(text=text))])
                self.assertTrue(all('intent' not in c for c in commands))
                self.assertTrue(any(r['params'].get('query', {}).get('kind') == 'nodes' for r in calls))
                if mode.endswith('contained'):
                    self.assertTrue(all(c['primitiveId'] == 'key-route:editor' for c in commands))
                    selections = [r['params']['query'].get('actuator') for r in calls
                                  if r['params'].get('query', {}).get('kind') == 'inputContext']
                    self.assertIn(dict(node='editor', relation='containedEditor'), selections)

    async def test_full_replacement_never_selects_an_ordinary_container(self):
        with self.assertRaises(ComputerUseError) as error:
            await self.engine('keyboard-replace-noneditor').execute(Intent('replace', dict(text='hello')), TARGET)
        self.assertEqual(error.exception.effect, 'none')
        self.assertFalse(any(r['method'] == 'execute' for r in self.requests()))

    async def test_contextual_replacement_requires_capability_before_preparation(self):
        with self.assertRaisesRegex(ComputerUseError, 'nativeTextReplacement'):
            await self.engine('keyboard-replace-missing-capability').execute(Intent('replace', dict(text='hello')), TARGET)
        self.assertEqual([r['method'] for r in self.requests()], ['hello', 'begin', 'inspect', 'finish'])

    async def test_selection_effect_cannot_be_replayed_even_without_delivered_text(self):
        for mode, effect in (('keyboard-replace-partial', 'partial'), ('keyboard-replace-unknown', 'possible')):
            offset = len(self.requests()) if self.journal.exists() else 0
            with self.assertRaises(ComputerUseError) as error:
                await self.engine(mode, HostPolicy(foreground_assist=True)).execute(Intent('replace', dict(text='hello')), TARGET)
            self.assertEqual(error.exception.effect, effect)
            calls = self.requests()[offset:]
            start = next(i for i, r in enumerate(calls) if r['method'] == 'execute')
            self.assertEqual([r['method'] for r in calls[start:]], ['execute', 'finish'])

    async def test_confirmed_already_empty_replacement_does_not_claim_dispatch(self):
        result = await self.engine('keyboard-replace-empty').execute(Intent('replace', dict(text='')), TARGET)
        self.assertEqual(result['status'], 'verified')
        self.assertIn('already empty', result['detail'])

    @unittest.skipUnless(os.environ.get('COMPUTER_USE_NATIVE_INPUT_PROBE'), 'run via test-macos with the actual native decoder')
    async def test_encoded_keyboard_intents_are_accepted_by_native_decoder(self):
        for intent in (Intent('focus'), Intent('key', dict(key='super+p')),
                       Intent('insert', dict(text='Hello 中文 😀')), Intent('paste', dict(text='A\tB\nC')),
                       Intent('replace', dict(text='Replace 中文 😀')), Intent('replace', dict(text=''))):
            await self.engine('keyboard-replace' if intent.kind == 'replace' else 'keyboard-prepare').execute(intent, TARGET)
        commands = [r['params']['command'] for r in self.requests() if r['method'] == 'execute']
        self.assertEqual(len(commands), 11)
        result = subprocess.run([os.environ['COMPUTER_USE_NATIVE_INPUT_PROBE'], '--keyboard-stdio'],
            input=''.join(json.dumps(c) + '\n' for c in unit_frames(self.requests())), text=True, capture_output=True, check=True, timeout=10)
        self.assertEqual([json.loads(line) for line in result.stdout.splitlines()], [True] * len(commands))

    async def test_empty_insertion_and_paste_do_not_prepare_or_dispatch(self):
        for intent in ('insert', 'paste'):
            offset = len(self.requests()) if self.journal.exists() else 0
            result = await self.engine('keyboard-prepare').execute(Intent(intent, dict(text='')), TARGET)
            self.assertEqual(result['status'], 'verified')
            self.assertEqual([r['method'] for r in self.requests()[offset:]], ['hello', 'begin', 'inspect', 'finish'])

    async def test_text_capability_is_checked_before_begin(self):
        for intent in ('insert', 'paste'):
            offset = len(self.requests()) if self.journal.exists() else 0
            with self.assertRaisesRegex(ComputerUseError, 'nativeTextInput'):
                await self.engine('keyboard-missing-text-capability').execute(Intent(intent, dict(text='hello')), TARGET)
            self.assertEqual([r['method'] for r in self.requests()[offset:]], ['hello'])

    async def test_partial_text_never_changes_route_or_replays(self):
        for intent, text in (('insert', 'hello'), ('paste', 'line1\nline2')):
            offset = len(self.requests()) if self.journal.exists() else 0
            with self.assertRaises(ComputerUseError) as error:
                await self.engine('keyboard-partial', HostPolicy(foreground_assist=True)).execute(Intent(intent, dict(text=text)), TARGET)
            self.assertEqual(error.exception.effect, 'partial')
            commands = [r['params']['command'] for r in self.requests()[offset:] if r['method'] == 'execute']
            self.assertEqual(len(commands), 1)
            self.assertFalse(commands[0]['foreground'])

    async def test_ready_window_key_does_not_require_ax_input_element(self):
        receipt = await self.engine('keyboard-window').execute(Intent('key', dict(key='super+p')), replace(TARGET, node=None))
        self.assertEqual(receipt['status'], 'dispatched')
        commands = [r['params']['command'] for r in self.requests() if r['method'] == 'execute']
        self.assertEqual([c['kind'] for c in commands], ['key_sequence'])
        self.assertIsNone(commands[0]['target']['node'])
        self.assertNotIn('expected', commands[0])
        self.assertEqual(self.requests()[2]['params']['query'], dict(kind='inputContext', within=[]))

    async def test_malformed_query_is_rejected_before_starting_a_driver(self):
        engine = self.engine()
        with self.assertRaises(ComputerUseError) as error:
            await engine.native.connection.request('inspect', dict(operation='unused', target={},
                query=dict(kind='inputContext')))
        self.assertEqual(error.exception.code, 'invalid_request')
        self.assertEqual(error.exception.effect, 'none')
        self.assertFalse(self.journal.exists())

    async def test_keyboard_capability_is_checked_before_any_preparation(self):
        with self.assertRaisesRegex(ComputerUseError, 'nativeKeyboardIntents'):
            await self.engine('keyboard-missing-capability').execute(Intent('key', dict(key='Return')), TARGET)
        self.assertEqual([r['method'] for r in self.requests()], ['hello'])

    async def test_foreground_preparation_requires_host_opt_in_after_background_failure(self):
        with self.assertRaises(ComputerUseError):
            await self.engine('keyboard-failed-background').execute(Intent('key', dict(key='Return')), TARGET)
        commands = [r['params']['command'] for r in self.requests() if r['method'] == 'execute']
        self.assertEqual([(c['kind'], c['foreground']) for c in commands], [('prepare_receiver', False)])
        offset = len(self.requests())
        receipt = await self.engine('keyboard-failed-background', HostPolicy(foreground_assist=True)).execute(Intent('key', dict(key='Return')), TARGET)
        self.assertEqual(receipt['status'], 'dispatched')
        commands = [r['params']['command'] for r in self.requests()[offset:] if r['method'] == 'execute']
        self.assertEqual([(c['kind'], c['foreground']) for c in commands],
                         [('prepare_receiver', False), ('prepare_receiver', True), ('key_sequence', False)])

    async def test_partial_chord_is_not_replayed_or_sent_via_foreground(self):
        with self.assertRaises(ComputerUseError) as error:
            await self.engine('keyboard-partial', HostPolicy(foreground_assist=True)).execute(Intent('key', dict(key='super+p')), TARGET)
        self.assertEqual(error.exception.effect, 'partial')
        calls = [r for r in self.requests() if r['method'] == 'execute']
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0]['params']['command']['foreground'])

    async def test_unconfirmed_key_release_retires_engine(self):
        engine = self.engine('keyboard-unclean')
        with self.assertRaises(ComputerUseError):
            await engine.execute(Intent('key', dict(key='super+p')), TARGET)
        count = len(self.requests())
        with self.assertRaisesRegex(ComputerUseError, 'retired'):
            await engine.execute(Intent('key', dict(key='Return')), TARGET)
        self.assertEqual(len(self.requests()), count)

    async def test_path_query_binds_a_node_under_one_window_operation(self):
        result = await self.engine('query').execute(Intent('click'), TARGET, path=['A', 'Run'])
        self.assertEqual(result['status'], 'dispatched')
        requests = self.requests()
        self.assertEqual([r['method'] for r in requests], ['hello', 'begin', 'inspect', 'inspect', 'execute', 'finish'])
        self.assertIsNone(requests[1]['params']['target']['node'])
        self.assertEqual(requests[2]['params']['query']['kind'], 'nodes')
        self.assertEqual(requests[3]['params']['query'], dict(kind='inputContext', within=['node', 'group']))
        target = requests[4]['params']['command']['target']
        self.assertEqual(target['node'], 'button')
        self.assertEqual(target['rootWindow'], 'root')
        self.assertEqual(requests[3]['params']['target'], target)

    async def test_path_query_requires_capability_before_begin(self):
        with self.assertRaisesRegex(ComputerUseError, 'nativeWindowScope'):
            await self.engine('query-missing-capability').execute(Intent('click'), TARGET, path=['Run'])
        self.assertEqual([r['method'] for r in self.requests()], ['hello'])

    async def test_partial_query_cannot_dispatch_or_drop_its_constraints(self):
        for mode in ('query-ambiguous', 'query-wrong-constraints'):
            before = len(self.requests()) if self.journal.exists() else 0
            with self.subTest(mode=mode), self.assertRaises(ComputerUseError) as error:
                await self.engine(mode).execute(Intent('click'), TARGET, path=['A', 'Run'])
            self.assertEqual(error.exception.effect, 'none')
            self.assertNotIn('execute', [r['method'] for r in self.requests()[before:]])
            self.assertEqual(self.requests()[-1]['method'], 'finish')

    async def test_path_scope_change_and_uncertain_dispatch_do_not_restart_query(self):
        for mode, effect in (('query-moved', 'none'), ('query-uncertain', 'possible')):
            before = len(self.requests()) if self.journal.exists() else 0
            with self.subTest(mode=mode), self.assertRaises(ComputerUseError) as error:
                await self.engine(mode).execute(Intent('click'), TARGET, path=['A', 'Run'])
            self.assertEqual(error.exception.effect, effect)
            calls = self.requests()[before:]
            self.assertEqual(sum(r['method'] == 'execute' for r in calls), 1)
            self.assertEqual(sum(r['params'].get('query', {}).get('kind') == 'nodes' for r in calls), 1)

    async def test_click_and_menu_cross_wire_as_selected_declared_primitives(self):
        for mode, kind, primitive in (('action-intents', 'click', 'declared-action'),
                                      ('action-intents', 'openMenu', 'context-menu'),
                                      ('action-intents-dropdown', 'click', 'context-menu')):
            with self.subTest(mode=mode, kind=kind):
                engine = self.engine(mode)
                receipt = await engine.execute(Intent(kind), TARGET)
                self.assertEqual(receipt['status'], 'dispatched')
                last = self.requests()[-5:]
                self.assertEqual([r['method'] for r in last], ['hello', 'begin', 'inspect', 'execute', 'finish'])
                command = last[3]['params']['command']
                self.assertEqual(command['kind'], 'native_action')
                self.assertEqual(command['primitiveId'], primitive)
                self.assertEqual(last[1]['params']['intent'], dict(kind=kind, parameters={}))
                self.assertFalse(command['foreground'])

    async def test_incomplete_actions_do_not_prove_dropdown_has_no_press_action(self):
        with self.assertRaises(ComputerUseError) as error:
            await self.engine('action-intents-partial').execute(Intent('click'), TARGET)
        self.assertEqual(error.exception.effect, 'none')
        self.assertNotIn('execute', [r['method'] for r in self.requests()])

    async def test_action_intent_capability_is_checked_before_begin(self):
        with self.assertRaisesRegex(ComputerUseError, 'nativeActionIntents'):
            await self.engine('action-intents-missing-capability').execute(Intent('click'), TARGET)
        self.assertEqual([r['method'] for r in self.requests()], ['hello'])

    async def test_action_semantics_metadata_must_be_valid_before_dispatch(self):
        with self.assertRaises(ComputerUseError):
            await self.engine('action-intents-invalid-role').execute(Intent('click'), TARGET)
        self.assertGreater(self.invalidations, 0)
        self.assertNotIn('execute', [r['method'] for r in self.requests()])

    async def test_uncertain_click_is_not_replayed_as_a_menu_or_pointer(self):
        with self.assertRaises(ComputerUseError) as error:
            await self.engine('action-intents-uncertain').execute(Intent('click'), TARGET)
        self.assertEqual(error.exception.effect, 'possible')
        self.assertEqual(sum(r['method'] == 'execute' for r in self.requests()), 1)

    async def test_native_read_pipeline_uses_a_read_lease_without_input(self):
        reader = NativeReader(self.engine("nodes").native)
        snapshot = await reader.read(TARGET)
        self.assertIn("Native fixture", snapshot.render()["text"])
        requests = self.requests()
        self.assertEqual([r["method"] for r in requests], ["hello", "begin", "inspect", "finish"])
        self.assertEqual(requests[1]["params"]["mode"], "read")
        self.assertEqual(requests[2]["params"]["query"]["kind"], "nodes")
        self.assertEqual(snapshot.refs, {"0": "node"})

    async def test_default_table_profile_requires_the_driver_read_capability(self):
        keys = {'AXRowIndexRange', 'AXColumnIndexRange', 'AXRowCount', 'AXColumnCount', 'AXIndex'}
        for mode, expected in [('nodes', set()), ('nodes-table-unconditional', set()), ('nodes-table-facts', keys)]:
            await NativeReader(self.engine(mode).native).read(TARGET)
            query = next(r['params']['query'] for r in reversed(self.requests()) if r['method'] == 'inspect')
            self.assertEqual(set(query['fields']) & keys, expected)
            self.assertEqual(set(query.get('advertisedFields', ())), expected)
        self.assertNotIn('execute', [r['method'] for r in self.requests()])

    async def test_explicit_native_table_profile_keeps_raw_provider_queries(self):
        await NativeReader(self.engine('nodes-table-facts').native).read(TARGET, fields=['AXRole', 'AXRowCount'])
        query = next(r['params']['query'] for r in reversed(self.requests()) if r['method'] == 'inspect')
        self.assertEqual(query['fields'], ['AXRole', 'AXRowCount'])
        self.assertNotIn('advertisedFields', query)

    async def test_busy_profile_requires_advertised_native_support(self):
        for mode, expected in [('nodes', False), ('nodes-busy-no-feature', False),
                               ('nodes-busy-unadvertised', False), ('nodes-busy', True)]:
            with self.subTest(mode=mode):
                await NativeReader(self.engine(mode).native).read(TARGET)
                query = self.requests()[-2]['params']['query']
                self.assertEqual('AXElementBusy' in query['fields'], expected)
                self.assertEqual('AXElementBusy' in query.get('advertisedFields', ()), expected)

    async def test_additional_content_relations_require_capability_and_do_not_change_explicit_profiles(self):
        for mode, explicit, expected in [('nodes-follow', False, True), ('nodes-follow', True, False),
                                         ('nodes-follow-missing-capability', False, False)]:
            start = len(self.requests()) if self.journal.exists() else 0
            await NativeReader(self.engine(mode).native).read(TARGET, fields=['AXRole'] if explicit else None)
            calls = self.requests()[start:]
            query = next(r['params']['query'] for r in calls if r['method'] == 'inspect')
            self.assertEqual('followRelations' in query, expected)
            if expected:
                self.assertEqual(query['followRelations'][:2], ['AXVisibleRows', 'AXVisibleCells'])
            self.assertFalse(any(r['method'] == 'execute' for r in calls))

    async def test_omitted_relationship_coverage_is_not_published_as_empty(self):
        with self.assertRaisesRegex(ValueError, 'omitted requested relationship coverage'):
            await NativeReader(self.engine('nodes-follow-omitted').native).read(TARGET)
        self.assertFalse(any(r['method'] == 'execute' for r in self.requests()))

    async def test_explicit_follow_request_cannot_bypass_a_missing_driver_capability(self):
        port = self.engine('nodes-follow-missing-capability').native
        async with port.calls.open(TARGET, 3000) as read:
            with self.assertRaisesRegex(ValueError, 'additional native relationships'):
                await port.nodes(read.token, read.target, ['AXRole'], 50, 4, 1000, follow_relations=['AXRows'])
        self.assertFalse(any(r['method'] == 'inspect' for r in self.requests()))

    async def test_discovery_to_raw_tree_never_bootstraps_with_read_app(self):
        port = self.engine('directory').native
        directory = NativeDirectory(port)
        applications = await directory.applications()
        surfaces = await directory.surfaces(applications['applications'][0]['id'])
        target = select_surface(surfaces)
        snapshot = await NativeReader(port).read(target)
        self.assertIn('Native fixture', snapshot.render()['text'])
        self.assertEqual(target, Binding(TARGET.session, 'instance', 'root', 'surface', 'node'))
        calls = self.requests()
        self.assertEqual([r['method'] for r in calls], ['hello', 'begin', 'inspect', 'finish', 'begin', 'inspect', 'finish', 'begin', 'inspect', 'finish'])
        self.assertEqual([r['params']['query']['kind'] for r in calls if r['method'] == 'inspect'], ['applications', 'surfaces', 'nodes'])
        self.assertTrue(all(r['params']['mode'] == 'read' for r in calls if r['method'] == 'begin'))
        self.assertIsNone(calls[1]['params']['target']['surface'])
        self.assertNotIn('readApp', [r['method'] for r in calls])

    async def test_related_object_outside_expanded_nodes_can_be_read_without_input(self):
        reader = NativeReader(self.engine('relations').native)
        snapshot = await reader.read(TARGET, max_nodes=1)
        self.assertEqual(len(snapshot.native['nodes']), 1)
        self.assertIn('outside-parent', snapshot.refs.values())
        related = await reader.read(replace(TARGET, node='outside-parent'))
        self.assertIn('Related outside-parent', related.render()['text'])
        self.assertNotIn('execute', [r['method'] for r in self.requests()])

    async def test_relation_continuation_keeps_source_range_and_previous_observation(self):
        reader = NativeReader(self.engine('relations').native)
        snapshot = await reader.read(TARGET)
        before = deepcopy(snapshot.native)
        relation = snapshot.native['relations'][0]
        page = await reader.read_relation(TARGET, 'AXChildren', cursor=relation['cursor'], limit=1)
        self.assertEqual(page['relation']['targets'], ['child-1'])
        self.assertEqual(page['relation']['offset'], 1)
        last = await reader.read_relation(TARGET, 'AXChildren', cursor=page['relation']['cursor'], limit=1)
        self.assertEqual(last['relation']['targets'], ['child-2'])
        self.assertEqual(last['relation']['offset'], 2)
        self.assertNotIn('cursor', last['relation'])
        self.assertEqual(last['relation']['state'], 'partial', 'A suffix page is not a complete relation')
        self.assertEqual(snapshot.native, before)
        child = await reader.read(replace(TARGET, node=last['relation']['targets'][0]))
        self.assertIn('Related child-2', child.render()['text'])

    async def test_relation_cursor_cannot_be_rebound_to_another_source_or_property(self):
        reader = NativeReader(self.engine('relations').native)
        await reader.read(TARGET)
        for target, property, cursor in ((replace(TARGET, node='outside-parent'), 'AXChildren', 'after-one'),
                                          (TARGET, 'AXParent', 'after-one'), (TARGET, 'AXChildren', 'unknown')):
            with self.assertRaisesRegex(ValueError, 'not observed'):
                await reader.read_relation(target, property, cursor=cursor)
        self.assertFalse(any(r['params'].get('query', {}).get('kind') == 'relation' for r in self.requests()))

    async def test_changed_relation_is_reported_without_restart_or_offset_guessing(self):
        reader = NativeReader(self.engine('relations-stale').native)
        await reader.read(TARGET)
        with self.assertRaises(ComputerUseError) as error:
            await reader.read_relation(TARGET, 'AXChildren', cursor='after-one')
        self.assertEqual(error.exception.code, 'snapshot_expired')
        self.assertEqual(error.exception.effect, 'none')
        calls = [r for r in self.requests() if r['params'].get('query', {}).get('kind') == 'relation']
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.requests()[-1]['method'], 'finish')
        # A new, explicit source read can make progress; it is not an automatic
        # repair of the stale page or mutation of its prior observation.
        page = await reader.read_relation(TARGET, 'AXChildren', limit=1)
        self.assertEqual(page['relation']['offset'], 0)

    async def test_wrong_relation_source_or_continuation_range_is_never_delivered(self):
        for mode in ('relations-wrong-source', 'relations-wrong-range'):
            with self.subTest(mode=mode):
                reader = NativeReader(self.engine(mode).native)
                await reader.read(TARGET)
                with self.assertRaises(ValueError):
                    await reader.read_relation(TARGET, 'AXChildren', cursor='after-one', limit=1)
                self.assertEqual(self.requests()[-1]['method'], 'finish')

    async def test_relation_capability_and_wire_failure_never_become_empty_children(self):
        reader = NativeReader(self.engine('relations-missing-capability').native)
        await reader.read(TARGET)
        with self.assertRaises(ComputerUseError) as error:
            await reader.read_relation(TARGET, 'AXChildren')
        self.assertEqual(error.exception.code, 'mode_unsupported')
        self.assertFalse(any(r['params'].get('query', {}).get('kind') == 'relation' for r in self.requests()))
        reader = NativeReader(self.engine('relations-malformed').native)
        with self.assertRaises(ComputerUseError):
            await reader.read_relation(TARGET, 'AXChildren')
        self.assertIsNone(reader.port.connection.session)

    async def test_directory_capability_missing_sends_only_hello(self):
        with self.assertRaisesRegex(RuntimeError, 'discovery contract'):
            await NativeDirectory(self.engine().native).applications()
        self.assertEqual([r['method'] for r in self.requests()], ['hello'])

    async def test_visual_only_surface_does_not_invent_an_accessibility_node(self):
        target = Binding(TARGET.session, 'instance', 'root', 'surface')
        with self.assertRaises(ComputerUseError) as error:
            await NativeReader(self.engine('directory').native).read(target)
        self.assertEqual(error.exception.code, 'observation_incomplete')
        self.assertFalse(self.journal.exists())

    async def test_directory_rejects_another_application_without_rendering_or_input(self):
        with self.assertRaisesRegex(ValueError, 'session or application'):
            await NativeDirectory(self.engine('directory-wrong-app').native).surfaces('running:app')
        self.assertEqual(self.requests()[-1]['method'], 'finish')

    async def test_native_only_directory_window_is_captured_without_nodes_or_input(self):
        port = self.engine('capture').native
        directory = NativeDirectory(port)
        surfaces = await directory.surfaces('running:app')
        target = select_surface(surfaces, 'root')
        self.assertIsNone(target.node)
        image = await NativeImages(port).window(target)
        self.assertEqual(image.target.capture, capture_metadata(image.image)['id'])
        self.assertEqual(image.target.root_window, target.root_window)
        self.assertIsNone(image.target.node)
        self.assertEqual(tuple(image.coordinates['toNative']), (2, 0, 0, 2, -200, 80))
        self.assertEqual([r['method'] for r in self.requests()], ['hello', 'begin', 'inspect', 'finish', 'begin', 'capture', 'finish'])
        self.assertTrue(all(r['params']['mode'] == 'read' for r in self.requests() if r['method'] == 'begin'))

    async def test_recapture_never_replaces_an_old_image_binding_or_reads_an_element(self):
        images = NativeImages(self.engine('capture').native)
        old = await images.window(TARGET)
        new = await images.window(old.target)
        self.assertNotEqual(old.target.capture, new.target.capture)
        self.assertEqual(old.target.capture, capture_metadata(old.image)['id'])
        self.assertEqual(TARGET.node, 'node')
        self.assertIsNone(TARGET.capture)
        self.assertNotIn('inspect', [r['method'] for r in self.requests()])
        self.assertTrue(all(r['params']['target']['node'] is None and r['params']['target']['capture'] is None
                            for r in self.requests() if r['method'] == 'begin'))

    async def test_capture_binding_and_image_budget_are_checked_before_delivery(self):
        with self.assertRaisesRegex(ValueError, 'kind or scope'):
            await NativeImages(self.engine('capture-wrong-window').native).window(TARGET)
        self.assertEqual(self.requests()[-1]['method'], 'finish')
        with self.assertRaisesRegex(ValueError, 'image budget'):
            await NativeImages(self.engine('capture').native).window(TARGET, max_dimension=2)
        self.assertEqual(self.requests()[-1]['method'], 'finish')
        self.assertNotIn('execute', [r['method'] for r in self.requests()])

    async def test_corrupt_capture_wire_retires_connection_without_delivering_pixels(self):
        for mode in ('capture-wrong-image-scope', 'capture-bad-png'):
            with self.subTest(mode=mode), self.assertRaises(ComputerUseError):
                await NativeImages(self.engine(mode).native).window(TARGET)
        self.assertEqual(self.invalidations, 2)
        self.assertNotIn('execute', [r['method'] for r in self.requests()])

    async def test_capture_capability_missing_never_sends_capture(self):
        with self.assertRaisesRegex(ComputerUseError, 'nativeCapture'):
            await NativeImages(self.engine('directory').native).window(TARGET)
        self.assertEqual([r['method'] for r in self.requests()], ['hello', 'begin', 'finish'])

    async def test_unconfirmed_capture_cleanup_isolates_other_readers_on_same_port(self):
        port = self.engine('capture-cleanup-unknown').native
        with self.assertRaisesRegex(RuntimeError, 'cleanup is unconfirmed'):
            await NativeImages(port).window(TARGET)
        before = len(self.requests())
        with self.assertRaisesRegex(RuntimeError, 'cleanup'):
            await NativeDirectory(port).applications()
        with self.assertRaisesRegex(RuntimeError, 'cleanup'):
            await NativeReader(port).read(TARGET)
        with self.assertRaisesRegex(ComputerUseError, 'retired'):
            await Engine(port).execute(Intent('action', dict(action='Press')), TARGET)
        self.assertEqual(len(self.requests()), before)

    async def test_addressed_replacement_crosses_wire_without_preparing_other_focus(self):
        engine = self.engine("text-value")
        for text in ("replacement 🙂 e\u0301", ""):
            result = await engine.execute(Intent("replace", dict(text=text)), TARGET)
            self.assertEqual(result['status'], 'verified')
        sent = [r['params']['command'] for r in self.requests() if r['method'] == 'execute']
        self.assertEqual(len(sent), 2)
        self.assertTrue(all(c['kind'] == 'native_text_edit' and not c['foreground'] for c in sent))
        self.assertTrue(all('expected' not in c for c in sent))
        self.assertEqual([r['params']['intent']['parameters']['text'] for r in self.requests() if r['method'] == 'begin'], ['replacement 🙂 e\u0301', ''])

    async def test_independent_replacement_skips_receiver_query_with_new_capability(self):
        result = await self.engine('text-value-v5').execute(Intent('replace', dict(text='new value')), TARGET)
        self.assertEqual(result['status'], 'verified')
        calls = self.requests()
        self.assertEqual([r['method'] for r in calls], ['hello', 'begin', 'inspect', 'execute', 'finish'])
        self.assertEqual(calls[3]['params']['command']['kind'], 'native_text_edit')

    async def test_uncertain_addressed_replacement_is_not_changed_to_key_input(self):
        with self.assertRaises(ComputerUseError) as caught:
            await self.engine("text-value-uncertain").execute(Intent("replace", dict(text="once")), TARGET)
        self.assertEqual(caught.exception.effect, 'possible')
        self.assertEqual([r['method'] for r in self.requests()], ['hello', 'begin', 'inspect', 'execute', 'finish'])

    async def test_wrong_native_read_scope_is_not_rendered(self):
        with self.assertRaisesRegex(ValueError, "changed its requested scope"):
            await NativeReader(self.engine("nodes-wrong-root").native).read(TARGET)
        self.assertEqual(self.requests()[-1]["method"], "finish")

    async def test_long_text_pages_use_the_capture_after_read_operation_closed(self):
        reader = NativeReader(self.engine("nodes-text").native)
        snapshot = await reader.read(TARGET)
        page = await reader.read_text(snapshot, "0", "AXValue", offset=19000, length=4096)
        self.assertEqual(page, dict(text="x" * 1000, offset=19000, total=20000, next=None))
        calls = self.requests()
        self.assertEqual([r['method'] for r in calls], ['hello', 'begin', 'inspect', 'finish', 'begin', 'inspect', 'finish'])
        scope = dict(session=TARGET.session, instance=None, rootWindow=None, surface=None, node=None, capture=None)
        self.assertEqual(calls[4]['params'], dict(mode='read', target=scope, intent=dict(kind='read', parameters={})))
        self.assertEqual(calls[5]['params']['target'], scope)
        self.assertEqual(calls[5]['params']['query'], dict(kind='capturedText', textRef='text-capture', offset=19000, length=4096))
        # A dead session must not start another driver for a captured string.
        self.connections[-1].close()
        await self.connections[-1].wait_closed()
        before = len(self.requests())
        with self.assertRaisesRegex(RuntimeError, "session ended"):
            await reader.read_text(snapshot, "0", "AXValue")
        self.assertEqual(len(self.requests()), before)

    async def test_long_text_changed_total_is_not_silently_accepted(self):
        reader = NativeReader(self.engine("nodes-text-changed").native)
        snapshot = await reader.read(TARGET)
        with self.assertRaisesRegex(ValueError, "immutable capture range"):
            await reader.read_text(snapshot, "0", "AXValue")
        self.assertEqual(self.requests()[-1]['method'], 'finish')

    async def test_expired_text_capture_is_not_repaired_by_reading_live_ui(self):
        reader = NativeReader(self.engine('nodes-text-expired').native)
        snapshot = await reader.read(TARGET)
        with self.assertRaises(ComputerUseError) as caught:
            await reader.read_text(snapshot, '0', 'AXValue')
        self.assertEqual((caught.exception.code, caught.exception.effect), ('snapshot_expired', 'none'))
        queries = [r['params']['query']['kind'] for r in self.requests() if r['method'] == 'inspect']
        self.assertEqual(queries, ['nodes', 'capturedText'])
        self.assertEqual(self.requests()[-1]['method'], 'finish')
        self.assertFalse(reader.port.calls.retired)

    async def test_missing_captured_text_capability_has_no_legacy_fallback(self):
        reader = NativeReader(self.engine('nodes-text-missing-capability').native)
        snapshot = await reader.read(TARGET)
        with self.assertRaisesRegex(ComputerUseError, 'nativeCapturedText'):
            await reader.read_text(snapshot, '0', 'AXValue')
        self.assertEqual([r['method'] for r in self.requests()], ['hello', 'begin', 'inspect', 'finish', 'begin', 'finish'])
        self.assertFalse(reader.port.calls.retired)

    async def test_text_page_identity_and_requested_budget_are_checked(self):
        for suffix, message in [('wrong-ref', 'requested capture'), ('wrong-session', 'requested capture'),
                                ('wrong-offset', 'range or budget'), ('over-budget', 'range or budget')]:
            with self.subTest(suffix=suffix):
                reader = NativeReader(self.engine('nodes-text-' + suffix).native)
                snapshot = await reader.read(TARGET)
                with self.assertRaisesRegex(ValueError, message):
                    await reader.read_text(snapshot, '0', 'AXValue', length=2)
                self.assertEqual(self.requests()[-1]['method'], 'finish')

    async def test_malformed_text_page_is_rejected_by_wire_decoder(self):
        reader = NativeReader(self.engine('nodes-text-bad-page').native)
        snapshot = await reader.read(TARGET)
        with self.assertRaisesRegex(ComputerUseError, 'text page'):
            await reader.read_text(snapshot, '0', 'AXValue')
        self.assertTrue(reader.port.calls.retired)
        self.assertNotIn('readElementText', [r['method'] for r in self.requests()])

    async def test_text_read_disconnect_reports_no_input_and_never_restarts(self):
        reader = NativeReader(self.engine('nodes-text-disconnect').native)
        snapshot = await reader.read(TARGET)
        with self.assertRaises(ComputerUseError) as caught:
            await reader.read_text(snapshot, '0', 'AXValue')
        self.assertEqual((caught.exception.code, caught.exception.effect), ('execution_failed', 'none'))
        self.assertIn('read-only request failed', caught.exception.detail)
        self.assertTrue(reader.port.calls.retired)
        self.assertEqual([r['method'] for r in self.requests()], ['hello', 'begin', 'inspect', 'finish', 'begin', 'inspect'])

    async def test_text_page_cleanup_failure_retires_read_and_write_calls(self):
        engine = self.engine('nodes-text-cleanup-unknown')
        reader = NativeReader(engine.native)
        snapshot = await reader.read(TARGET)
        with self.assertRaisesRegex(RuntimeError, 'cleanup is unconfirmed'):
            await reader.read_text(snapshot, '0', 'AXValue')
        before = len(self.requests())
        with self.assertRaisesRegex(RuntimeError, 'retired'):
            await reader.read_text(snapshot, '0', 'AXValue')
        with self.assertRaisesRegex(ComputerUseError, 'retired'):
            await engine.execute(Intent('action', dict(action='AXPress')), TARGET)
        self.assertEqual(len(self.requests()), before)

    async def test_native_capability_is_checked_before_begin(self):
        with self.assertRaisesRegex(ComputerUseError, "nativeOperations"):
            await self.engine("missing-capability").execute(Intent("action", dict(action="Press")), TARGET)
        self.assertEqual([r["method"] for r in self.requests()], ["hello"])

    async def test_uncertain_effect_is_not_replayed(self):
        with self.assertRaises(ComputerUseError) as caught:
            await self.engine("uncertain").execute(Intent("action", dict(action="Press")), TARGET)
        self.assertEqual(caught.exception.effect, "possible")
        self.assertEqual([r["method"] for r in self.requests()].count("execute"), 1)
        self.assertEqual(self.requests()[-1]["method"], "finish")

    async def test_invalid_contract_retires_connection_before_input(self):
        with self.assertRaises(ComputerUseError):
            await self.engine("invalid-contract").execute(Intent("action", dict(action="Press")), TARGET)
        self.assertNotIn("execute", [r["method"] for r in self.requests()])
        self.assertEqual([r["method"] for r in self.requests()].count("hello"), 1)
        self.assertNotIn("finish", [r["method"] for r in self.requests()])
        self.assertGreater(self.invalidations, 0)

    async def test_contradictory_retry_never_reaches_policy(self):
        engine = self.engine("invalid-retry")
        with self.assertRaises(ComputerUseError) as caught:
            await engine.execute(Intent("action", dict(action="Press")), TARGET)
        self.assertEqual(caught.exception.effect, "possible")
        before = len(self.requests())
        with self.assertRaisesRegex(ComputerUseError, "retired"):
            await engine.execute(Intent("action", dict(action="Press")), TARGET)
        self.assertEqual(len(self.requests()), before)
        self.assertEqual([r["method"] for r in self.requests()].count("execute"), 1)


class NativeSchemaTests(unittest.TestCase):
    def test_capture_rejects_retargeting_stale_pixels_and_invalid_transforms(self):
        value = dict(kind='capture', binding=dict(session='s', instance='i', rootWindow='r', surface='v', node=None, capture=None),
            observed=dict(id='read', startedAt=1, completedAt=4),
            image=dict(id='pixels', kind='window', scope='v', startedAt=2, completedAt=3, width=1, height=1,
                png='iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR4nGNgAAIAAAUAAXpeqz8AAAAASUVORK5CYII='),
            coordinates=dict(space='window-pixels', origin='top-left', nativeSpace='screen-points', toNative=[1, 0, 0, 1, -100, 50]))
        self.assertEqual(capture_metadata(decode_execution_result('capture', value)['image'])['id'], 'pixels')
        for part, key, invalid in (
                ('binding', 'node', 'element'), ('binding', 'capture', 'old-capture'),
                ('binding', 'instance', None), ('binding', 'rootWindow', None),
                ('image', 'scope', 'another-window'), ('image', 'startedAt', 0),
                ('image', 'height', 2), ('coordinates', 'toNative', [0, 0, 0, 1, 0, 0]),
                ('coordinates', 'toNative', [1e308, 0, 0, 1e308, 0, 0]),
                ('coordinates', 'origin', 'bottom-left')):
            broken = deepcopy(value)
            broken[part][key] = invalid
            with self.subTest(part=part, key=key, value=invalid), self.assertRaises(ValueError):
                decode_execution_result('capture', broken)

    def test_unbound_discovery_cannot_gain_a_window_implicitly(self):
        target = Binding(TARGET.session, None, None, None)
        with self.assertRaises(ValueError):
            target.canonical(Binding(TARGET.session, None, 'invented', None))
        malformed = dict(session=TARGET.session, instance=None, rootWindow='invented', surface=None, node=None, capture=None)
        with self.assertRaisesRegex(ValueError, 'unbound discovery'):
            decode_execution_result('begin', dict(operation='op', target=malformed, remainingMs=1000))

    def test_strict_cleanup_and_begin_types(self):
        for value in (dict(cleanup="complete", extra=1), dict(cleanup=True), dict(cleanup="maybe")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                decode_execution_result("finish", value)
        with self.assertRaises(ValueError):
            decode_execution_result("begin", dict(operation="op", target={}, remainingMs=True))


if __name__ == "__main__":
    unittest.main()
