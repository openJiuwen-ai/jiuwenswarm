"""The production encoder and Swift decoder agree on contract-only writes."""
from dataclasses import replace
import json
import os
import subprocess
import unittest

from computer_use.errors import ComputerUseError
from computer_use.execution.native import ApplicationBinding, Intent
from computer_use.execution.policy import Attempts, choose
from computer_use.observation.capture import NativeImages
from computer_use.protocol.execution import decode_begin, decode_unit_command, decode_execution_result
from . import test_native_transport as fixture

CASES = json.loads((fixture.ROOT / 'fixtures/native-unit-commands.json').read_text())
BEGIN_CASES = json.loads((fixture.ROOT / 'fixtures/native-begin-requests.json').read_text())


class UnitCommandSchemaTests(unittest.TestCase):
    def test_unit_readiness_is_distinct_from_support_and_focus_metadata(self):
        from copy import deepcopy
        p = dict(id='command', unit='key_sequence', channel='keyboard', supported='yes',
                 readiness='unknown', pending=['receiver'], backgroundPreparation='yes')
        sample = dict(kind='inputContext', binding=CASES[0]['command']['target'], observed='now', targetLabel='field',
            identity='yes', withinScope='yes', enabled='yes', blockedBy=[], primitives=[p],
            input=dict(foregroundSurface=None, keyboardSurface='window', focusedNode='field', focusAncestors=[],
                       editor=None, editorOwner=None, windowRoute='yes',
                       selectionKnown='unknown', geometryCurrent='unknown'))
        decoded = decode_execution_result('inspect', sample)
        self.assertEqual(decoded['primitives'][0]['readiness'], 'unknown')
        self.assertEqual(decoded['input']['windowRoute'], 'yes')
        for changes in (dict(readiness='yes'), dict(readiness='no', pending=[]),
                        dict(pending=['receiver', 'receiver']), dict(pending=['arbitrary']),
                        dict(requiresReceiver=False), dict(intents=['key']), dict(action=None)):
            bad = deepcopy(sample); bad['primitives'][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                decode_execution_result('inspect', bad)
        sample['primitives'][0].update(readiness='yes', pending=[])
        self.assertEqual(decode_execution_result('inspect', sample)['primitives'][0]['readiness'], 'yes')
        sample['primitives'][0].update(readiness='no', pending=['enabled'])
        self.assertEqual(decode_execution_result('inspect', sample)['primitives'][0]['pending'], ['enabled'])

    def test_complete_intent_and_mode_are_validated_before_begin(self):
        for case in BEGIN_CASES:
            with self.subTest(case=case['name']):
                if case['valid']:
                    self.assertEqual(decode_begin(case['begin']), case['begin'])
                else:
                    with self.assertRaises(ValueError):
                        decode_begin(case['begin'])

    def test_shared_valid_and_invalid_envelopes(self):
        for case in CASES:
            with self.subTest(case=case['name']):
                if case['valid']:
                    self.assertEqual(decode_unit_command(case['command']), case['command'])
                else:
                    with self.assertRaises(ValueError):
                        decode_unit_command(case['command'])


class UnitCommandWireTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.NativeTransportTests.asyncSetUp
    asyncTearDown = fixture.NativeTransportTests.asyncTearDown
    engine = fixture.NativeTransportTests.engine
    requests = fixture.NativeTransportTests.requests

    async def test_old_protocol_five_driver_without_readiness_cannot_begin_input(self):
        with self.assertRaisesRegex(ComputerUseError, 'nativeUnitReadinessV2') as caught:
            await self.engine('legacy-readiness').execute(Intent('action', {'action': 'Press'}), fixture.TARGET)
        self.assertEqual(caught.exception.effect, 'none')
        self.assertEqual([r['method'] for r in self.requests()], ['hello'])

    async def test_legacy_context_or_client_editor_cannot_enter_an_execute_frame(self):
        for key, value in [('expected', {}), ('actuator', dict(node='foreign', relation='containedEditor')),
                           ('intent', dict(kind='key', parameters=dict(key='super+q')))]:
            engine = self.engine()
            with self.assertRaises(ComputerUseError) as caught:
                await engine.native.connection.request('execute', dict(operation='unused', step=1,
                    command={**CASES[0]['command'], key: value}))
            self.assertEqual(caught.exception.code, 'invalid_request')
            self.assertEqual(caught.exception.effect, 'none')
            self.assertFalse(self.journal.exists())

    async def test_invalid_begin_is_rejected_before_connecting_or_acquiring_a_lease(self):
        engine = self.engine()
        for case in BEGIN_CASES:
            if case['valid']:
                continue
            with self.subTest(case=case['name']), self.assertRaises(ComputerUseError) as caught:
                await engine.native.connection.request('begin', case['begin'])
            self.assertEqual(caught.exception.effect, 'none')
            self.assertEqual(caught.exception.code, 'invalid_request')
        self.assertFalse(self.journal.exists())

    async def test_changing_the_step_intent_is_rejected_instead_of_silently_ignored(self):
        engine = self.engine('keyboard-ready')
        await engine.native.connection.connect()
        original = Intent('key', dict(key='Return'))
        operation = await engine.native.begin(original, fixture.TARGET, 2000)
        facts = await engine.native.inspect(operation.token, operation.target, 1000)
        command = choose(facts, original, engine.native.policy, Attempts())
        for changed in (Intent('key', dict(key='super+q')), Intent('paste', dict(text='unexpected'))):
            with self.assertRaises(ComputerUseError) as caught:
                await engine.native.execute(operation.token, 1, replace(command, intent=changed), 1000)
            self.assertEqual(caught.exception.code, 'invalid_request')
            self.assertEqual(caught.exception.effect, 'none')
        self.assertFalse(any(r['method'] == 'execute' for r in self.requests()))
        result = await engine.native.execute(operation.token, 1, command, 1000)
        self.assertEqual(result.dispatch.value, 'dispatched')
        await engine.native.finish(operation.token)

    @unittest.skipUnless(os.environ.get('COMPUTER_USE_NATIVE_COMMAND_PROBE'), 'run via test-macos with the production Swift envelope decoder')
    async def test_actual_encoded_units_and_negative_frames_pass_the_native_decoder(self):
        await self.engine('launch').execute(Intent('launch'), ApplicationBinding('native-v4-fixture', 'installation-observed'))
        for mode, intent in [('normal', Intent('action', dict(action='Press'))),
                ('text-value', Intent('replace', dict(text='replacement'))),
                ('keyboard-prepare', Intent('focus')),
                ('keyboard-prepare', Intent('key', dict(key='Return'))),
                ('keyboard-prepare', Intent('insert', dict(text='Hello 中文 😀'))),
                ('keyboard-prepare', Intent('paste', dict(text='A\tB\nC'))),
                ('keyboard-replace-contained', Intent('replace', dict(text='replacement'))),
                ('keyboard-actuator-recreated', Intent('insert', dict(text='hello')))]:
            await self.engine(mode).execute(intent, fixture.TARGET)
        engine = self.engine('pointer')
        image = await NativeImages(engine.native).window(replace(fixture.TARGET, node=None))
        for intent in (Intent('click', dict(x=1, y=1)), Intent('scroll', dict(x=1, y=1, direction='down', pages=1)),
                       Intent('drag', dict(fromX=1, fromY=1, toX=3, toY=1))):
            await engine.execute(intent, image.target)
        commands = [r['params']['command'] for r in self.requests() if r['method'] == 'execute']
        negatives = [{**c, 'expected': {'focusedNode': 'forged'}} for c in commands]
        frames = commands + negatives + [case['command'] for case in CASES]
        expected = [True] * len(commands) + [False] * len(negatives) + [case['valid'] for case in CASES]
        result = subprocess.run([os.environ['COMPUTER_USE_NATIVE_COMMAND_PROBE'], '--stdio'],
            input=''.join(json.dumps(c) + '\n' for c in frames), text=True, capture_output=True, check=True, timeout=10)
        self.assertEqual([json.loads(line) for line in result.stdout.splitlines()], expected)
        begins = [r['params'] for r in self.requests() if r['method'] == 'begin']
        frames = begins + [case['begin'] for case in BEGIN_CASES]
        expected = [True] * len(begins) + [case['valid'] for case in BEGIN_CASES]
        result = subprocess.run([os.environ['COMPUTER_USE_NATIVE_COMMAND_PROBE'], '--begin-stdio'],
            input=''.join(json.dumps(c) + '\n' for c in frames), text=True, capture_output=True, check=True, timeout=10)
        self.assertEqual([json.loads(line) for line in result.stdout.splitlines()], expected)


if __name__ == '__main__':
    unittest.main()
