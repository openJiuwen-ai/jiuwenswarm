"""Desktop read through the production API, lifecycle and strict v5 transport."""
import asyncio
import gc
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import weakref

from computer_use.client import images
from computer_use.client.connection import Connection
from computer_use.client.native import native_computer
from computer_use.errors import ComputerUseError
from computer_use.execution.native import Binding, HostPolicy
from computer_use.execution.transport import ConnectionPort
from computer_use.observation.desktop_reader import NativeDesktopReader
from computer_use.protocol.captures import capture_metadata
from computer_use.protocol.execution import decode_capture_request

ROOT = Path(__file__).resolve().parent
CASES = json.loads((ROOT / 'fixtures/native-capture-requests.json').read_text())
PROBE = os.environ.get('COMPUTER_USE_NATIVE_CAPTURE_PROBE')


class CaptureContractsTests(unittest.TestCase):
    def test_shared_scope_and_budget_cases(self):
        for case in CASES:
            with self.subTest(case['name']):
                if case['valid']:
                    result = decode_capture_request(case['request'])
                    self.assertEqual(result, case['request'])
                    self.assertIsNot(result['target'], case['request']['target'])
                else:
                    with self.assertRaises(ValueError): decode_capture_request(case['request'])

    @unittest.skipUnless(PROBE, 'run via test-macos for the compiled native decoder')
    def test_production_encoder_and_shared_cases_pass_native_decoder(self):
        class Captured(Exception): pass
        class RecordingConnection:
            protocol_version = 5
            session_identity = ('s', 1)
            async def request(self, method, params, options):
                self.requested = params
                raise Captured
        async def requests():
            connection = RecordingConnection()
            port = ConnectionPort(connection, HostPolicy())
            port._operations['op'] = (connection.session_identity, 'read')
            values = []
            for kind, target in [('desktop', Binding('s', None, None, None)),
                                 ('window', Binding('s', 'i', 'w', 'surface'))]:
                try: await port.capture('op', target, 320, 1000, kind=kind)
                except Captured: values.append(connection.requested)
            return values
        encoded = asyncio.run(requests())
        result = subprocess.run([PROBE, '--stdio'],
            input=''.join(json.dumps(value) + '\n' for value in encoded + [case['request'] for case in CASES]),
            text=True, capture_output=True, check=True, timeout=10)
        self.assertEqual([json.loads(line) for line in result.stdout.splitlines()],
            [True] * len(encoded) + [case['valid'] for case in CASES])


class NativeDesktopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='native-desktop-')
        self.journal = Path(self.temp.name) / 'wire.jsonl'
        self.connections = []

    async def asyncTearDown(self):
        for connection in self.connections:
            connection.close()
            await connection.wait_closed()
        self.temp.cleanup()

    def computer(self, mode='desktop'):
        connection = Connection(lambda: None, dict(command=sys.executable,
            args=[str(ROOT / 'fixtures/v5-driver.py'), mode, str(self.journal)], timeoutMs=3000), protocol_version=5)
        self.connections.append(connection)
        return native_computer(ConnectionPort(connection, HostPolicy(os.environ.get('COMPUTER_USE_DESKTOP', 'shared'))))

    def requests(self):
        return [r for line in self.journal.read_text().splitlines() if 'method' in (r := json.loads(line))] if self.journal.exists() else []

    async def test_public_read_is_session_scoped_read_only_and_renders_untitled_windows(self):
        computer = self.computer()
        state = await computer.read({'full': True})
        requests = self.requests()
        self.assertEqual([r['method'] for r in requests], ['hello', 'begin', 'capture', 'finish'])
        self.assertEqual(requests[1]['params']['intent'], dict(kind='read', parameters={}))
        self.assertEqual(requests[1]['params']['mode'], 'read')
        self.assertEqual(requests[2]['params']['kind'], 'desktop')
        self.assertTrue(all(v is None for k,v in requests[2]['params']['target'].items() if k != 'session'))
        self.assertIn('<untitled>', state.text)
        self.assertIn('app="running:app"', state.text)
        self.assertIn('not element references', state.text)
        self.assertEqual(capture_metadata(state.screenshot)['kind'], 'desktop')
        store = computer._controller.desktop
        self.assertEqual(capture_metadata(state.screenshot)['id'], store._view['desktopRef'])
        self.assertIn('desktop', images.image_label(state.screenshot).lower())
        self.assertIsNone(state.overview)

    async def test_partial_and_no_image_observations_preserve_directory_and_gaps(self):
        for mode, has_image in [('desktop-partial', True), ('desktop-no-image', False)]:
            with self.subTest(mode):
                computer = self.computer(mode)
                state = await computer.read()
                self.assertEqual(state.screenshot is not None, has_image)
                self.assertIn('Document', state.text)
                self.assertIn('unavailable', state.text)
                if has_image:
                    self.assertIn('2 more windows', state.text)
                    self.assertIn('catalogue is incomplete', state.text)
                else: self.assertIn('window catalogue only', state.text)

    async def test_old_picture_is_not_rebound_and_callbacks_do_not_retain_images(self):
        computer = self.computer()
        old = await computer.read()
        old_id = capture_metadata(old.screenshot)['id']
        images.image_delivered(old.screenshot)
        current = await computer.read()
        self.assertNotEqual(old_id, capture_metadata(current.screenshot)['id'])
        self.assertFalse(computer._controller.desktop.current(old.screenshot))
        self.assertIsNone(computer._controller.desktop._delivered)
        images.image_delivered(old.screenshot)
        self.assertIsNone(computer._controller.desktop._delivered)
        images.image_delivered(current.screenshot)
        self.assertIs(computer._controller.desktop._delivered['state'], computer._controller.desktop._view)
        ref = weakref.ref(old.screenshot)
        del old
        gc.collect()
        self.assertIsNone(ref())

    async def test_invalid_response_or_wrong_scope_cannot_publish_desktop(self):
        for mode in ('wrong-scope', 'wrong-id', 'wrong-binding', 'wrong-mode', 'bad-png',
                     'bad-canvas', 'missing-image', 'image-time', 'window-kind', 'error'):
            with self.subTest(mode):
                computer = self.computer('desktop-' + mode)
                with self.assertRaises((ValueError, RuntimeError, ComputerUseError)) as failure:
                    await computer.read()
                if isinstance(failure.exception, ComputerUseError):
                    self.assertEqual(failure.exception.effect, 'none', 'Malformed observation is not possible user input')
                self.assertIsNone(computer._controller.desktop._view)
                self.assertFalse(any(r['method'] == 'execute' for r in self.requests()))

    async def test_cleanup_failure_blocks_publication_and_following_calls(self):
        computer = self.computer('desktop-cleanup-unknown')
        with self.assertRaisesRegex((RuntimeError, ComputerUseError), 'cleanup'):
            await computer.read()
        self.assertIsNone(computer._controller.desktop._view)
        before = len(self.requests())
        with self.assertRaisesRegex((RuntimeError, ComputerUseError), 'retired'):
            await computer.read()
        self.assertEqual(len(self.requests()), before)

    async def test_deadline_and_cancellation_do_not_publish_late_pixels(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                computer = self.computer('desktop-delay')
                reader = computer._controller.desktop_reader
                # Exclude interpreter startup from the deliberately short budget.
                await computer._controller.port.connect(1000)
                task = asyncio.create_task(reader.read(timeout_ms=1000 if cancel else 40))
                if cancel:
                    await asyncio.sleep(0.04)
                    task.cancel()
                with self.assertRaises((asyncio.CancelledError, TimeoutError, RuntimeError, ComputerUseError)):
                    await task
                self.assertIsNone(computer._controller.desktop._view)
                self.assertFalse(any(r['method'] == 'execute' for r in self.requests()))

    async def test_budget_validation_and_legacy_capability(self):
        computer = self.computer()
        reader = computer._controller.desktop_reader
        for dimension in (True, 0, 4097, 1.5):
            with self.assertRaises(ValueError): await reader.read(max_dimension=dimension)
        self.assertFalse(self.requests())
        with self.assertRaisesRegex(ValueError, 'budget'):
            await reader.read(max_dimension=2)
        other = self.computer('desktop-legacy-capture')
        before = len(self.requests())
        with self.assertRaisesRegex(ComputerUseError, 'nativeCaptureKinds'):
            await other.read()
        self.assertNotIn('capture', [r['method'] for r in self.requests()[before:]])

    async def test_desktop_and_app_observations_have_independent_bindings(self):
        computer = self.computer('observation-sdk')
        app = computer.app('Native fixture')
        window = await app.read()
        element = app.element('1')
        old_target = element._snapshot['binding']
        desktop = await computer.read()
        self.assertEqual(element._snapshot['binding'], old_target)
        self.assertEqual(images.image_metadata(window.screenshot)['kind'], 'window')
        self.assertEqual(images.image_metadata(desktop.screenshot)['kind'], 'desktop')
        self.assertEqual((await element.typeText('hello', {'replace': True})).status, 'dispatched')
        self.assertTrue(computer._controller.desktop.current(desktop.screenshot))
