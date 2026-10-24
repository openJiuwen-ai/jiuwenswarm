"""Image and addressed Element gestures share the native operation pipeline."""
from dataclasses import replace
import json
import os
import subprocess
import unittest

from computer_use.errors import ComputerUseError
from computer_use.execution.native import HostPolicy, Intent
from computer_use.observation.capture import NativeImages
from . import test_native_transport as fixture
TARGET = fixture.TARGET


class NativePointerTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.NativeTransportTests.asyncSetUp
    asyncTearDown = fixture.NativeTransportTests.asyncTearDown
    engine = fixture.NativeTransportTests.engine
    requests = fixture.NativeTransportTests.requests

    async def captured(self, mode='pointer', policy=None):
        engine = self.engine(mode, policy)
        image = await NativeImages(engine.native).window(replace(TARGET, node=None))
        return engine, image.target

    async def test_click_scroll_drag_keep_the_exact_observed_image(self):
        for intent in (Intent('click', dict(x=1, y=1, button='right', count=2)),
                       Intent('scroll', dict(x=1, y=1, direction='down', pages=0.5)),
                       Intent('drag', dict(fromX=1, fromY=1, toX=3, toY=1))):
            offset = len(self.requests()) if self.journal.exists() else 0
            engine, target = await self.captured()
            result = await engine.execute(intent, target)
            self.assertEqual(result['status'], 'dispatched')
            calls = self.requests()[offset:]
            commands = [r['params']['command'] for r in calls if r['method'] == 'execute']
            self.assertEqual([c['kind'] for c in commands], ['prepare_receiver', 'pointer_sequence'])
            self.assertTrue(all(c['target']['capture'] == target.capture and c['target']['node'] is None for c in commands))
            self.assertEqual([r['params']['intent'] for r in calls if r['method'] == 'begin' and r['params']['mode'] == 'write'],
                             [dict(kind=intent.kind, parameters=dict(intent.parameters))])
            self.assertEqual(set(commands[-1]), {'kind', 'primitiveId', 'target', 'foreground'})
            self.assertEqual(sum(r['method'] == 'capture' for r in calls), 1)
            self.assertFalse(any(r['params'].get('query', {}).get('kind') == 'nodes' for r in calls))

    async def test_stale_geometry_refuses_without_new_capture_or_input(self):
        engine, target = await self.captured('pointer-stale')
        offset = len(self.requests())
        with self.assertRaises(ComputerUseError) as caught:
            await engine.execute(Intent('click', dict(x=1, y=1)), target)
        self.assertEqual(caught.exception.code, 'snapshot_expired')
        self.assertEqual([r['method'] for r in self.requests()[offset:]], ['begin', 'inspect', 'finish'])

    async def test_partial_drag_never_retries_with_a_new_window_or_route(self):
        engine, target = await self.captured('pointer-partial', HostPolicy(foreground_assist=True))
        offset = len(self.requests())
        with self.assertRaises(ComputerUseError) as caught:
            await engine.execute(Intent('drag', dict(fromX=1, fromY=1, toX=3, toY=1)), target)
        self.assertEqual(caught.exception.effect, 'partial')
        self.assertEqual([r['method'] for r in self.requests()[offset:]], ['begin', 'inspect', 'execute', 'finish'])

    async def test_foreground_assistance_is_only_pre_dispatch_after_background_failure(self):
        engine, target = await self.captured('pointer-background-unavailable', HostPolicy(foreground_assist=True))
        await engine.execute(Intent('click', dict(x=1, y=1)), target)
        commands = [r['params']['command'] for r in self.requests() if r['method'] == 'execute']
        self.assertEqual([(c['kind'], c['foreground']) for c in commands],
            [('prepare_receiver', False), ('prepare_receiver', True), ('pointer_sequence', False)])

    async def test_old_driver_cannot_receive_a_visual_write(self):
        engine, target = await self.captured('pointer-missing-capability')
        offset = len(self.requests())
        with self.assertRaisesRegex(ComputerUseError, 'nativePointer'):
            await engine.execute(Intent('click', dict(x=1, y=1)), target)
        self.assertEqual(len(self.requests()), offset)

    async def test_element_click_preserves_object_binding_and_sends_no_coordinates(self):
        for options in ({}, dict(button='right', count=2), dict(button='middle')):
            offset = len(self.requests()) if self.journal.exists() else 0
            result = await self.engine('pointer-element').execute(Intent('click', options), TARGET)
            self.assertEqual(result['status'], 'dispatched')
            calls = self.requests()[offset:]
            commands = [r['params']['command'] for r in calls if r['method'] == 'execute']
            self.assertEqual([c['kind'] for c in commands], ['prepare_receiver', 'pointer_sequence'])
            self.assertTrue(all(c['target']['node'] == TARGET.node and c['target']['capture'] is None for c in commands))
            self.assertFalse(any(r['method'] == 'capture' or r['params'].get('query', {}).get('kind') == 'nodes' for r in calls))
            self.assertEqual(next(r['params']['intent']['parameters'] for r in calls if r['method'] == 'begin'), options)

    async def test_native_action_is_preferred_and_pointer_metadata_cannot_block_it(self):
        for mode in ('pointer-element-object', 'pointer-element-object-no-geometry'):
            offset = len(self.requests()) if self.journal.exists() else 0
            await self.engine(mode).execute(Intent('click'), TARGET)
            commands = [r['params']['command'] for r in self.requests()[offset:] if r['method'] == 'execute']
            self.assertEqual([c['kind'] for c in commands], ['native_action'])
        offset = len(self.requests())
        await self.engine('pointer-element-object').execute(Intent('click', dict(button='right', count=2)), TARGET)
        commands = [r['params']['command'] for r in self.requests()[offset:] if r['method'] == 'execute']
        self.assertEqual([c['kind'] for c in commands], ['prepare_receiver', 'pointer_sequence'])

    async def test_uncertain_element_action_or_partial_pointer_is_not_replayed(self):
        for mode, kind in [('pointer-element-object-unknown', 'native_action'), ('pointer-element-partial', 'pointer_sequence')]:
            offset = len(self.requests()) if self.journal.exists() else 0
            with self.assertRaises(ComputerUseError) as error:
                await self.engine(mode, HostPolicy(foreground_assist=True)).execute(Intent('click'), TARGET)
            self.assertIn(error.exception.effect, ('possible', 'partial'))
            self.assertEqual([r['params']['command']['kind'] for r in self.requests()[offset:] if r['method'] == 'execute'], [kind])

    async def test_old_driver_cannot_prepare_or_send_an_element_pointer_unit(self):
        with self.assertRaisesRegex(ComputerUseError, 'nativeElementPointer'):
            await self.engine('pointer-element-missing-capability').execute(Intent('click'), TARGET)
        self.assertEqual([r['method'] for r in self.requests()], ['hello', 'begin', 'inspect', 'finish'])

    @unittest.skipUnless(os.environ.get('COMPUTER_USE_NATIVE_POINTER_PROBE'), 'run via test-macos with the actual native decoder')
    async def test_encoded_pointer_intents_are_accepted_by_native_decoder(self):
        engine, target = await self.captured()
        for intent in (Intent('click', dict(x=1, y=1, button='middle', count=2)),
                       Intent('scroll', dict(x=1, y=1, direction='right', pages=0.25)),
                       Intent('drag', dict(fromX=1, fromY=1, toX=3, toY=1))):
            await engine.execute(intent, target)
        commands = [r['params']['command'] for r in self.requests() if r['method'] == 'execute']
        self.assertGreaterEqual(len(commands), 3)
        result = subprocess.run([os.environ['COMPUTER_USE_NATIVE_POINTER_PROBE'], '--pointer-stdio'],
            input=''.join(json.dumps(c) + '\n' for c in fixture.unit_frames(self.requests())), text=True, capture_output=True, check=True, timeout=10)
        self.assertEqual([json.loads(line) for line in result.stdout.splitlines()], [True] * len(commands))

    @unittest.skipUnless(os.environ.get('COMPUTER_USE_NATIVE_POINTER_PROBE'), 'run via test-macos with the actual native decoder')
    async def test_encoded_element_clicks_pass_the_same_native_decoder(self):
        for parameters in ({}, dict(button='right', count=2), dict(button='middle')):
            await self.engine('pointer-element').execute(Intent('click', parameters), TARGET)
        frames = fixture.unit_frames(self.requests())
        result = subprocess.run([os.environ['COMPUTER_USE_NATIVE_POINTER_PROBE'], '--pointer-stdio'],
            input=''.join(json.dumps(c) + '\n' for c in frames), text=True, capture_output=True, check=True, timeout=10)
        self.assertEqual([json.loads(line) for line in result.stdout.splitlines()], [True] * len(frames))
