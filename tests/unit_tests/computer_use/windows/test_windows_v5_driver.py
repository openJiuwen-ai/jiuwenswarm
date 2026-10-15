"""Run the production Windows session dispatcher over real stdio.

The portable fixture substitutes only the UIA provider and Framework serializer.
This proves dispatch/lifecycle behaviour, not Windows GUI or Framework JSON.
"""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[4] / "jiuwenswarm/cua_engine"
TESTS = Path(__file__).resolve().parent


@unittest.skipUnless(os.environ.get('COMPUTER_USE_WINDOWS_TEST_DOTNET'), 'standalone .NET SDK not configured')
class WindowsDriverTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='windows-v5-driver-')
        cls.addClassCleanup(cls.temporary.cleanup)
        out = Path(cls.temporary.name)
        sdk = Path(os.environ['COMPUTER_USE_WINDOWS_TEST_DOTNET'])
        compiler = sorted((sdk / 'sdk').glob('*/Roslyn/bincore/csc.dll'))[-1]
        refs = sorted((sdk / 'packs/Microsoft.NETCore.App.Ref').glob('*/ref/net8.0'))[-1]
        runtime = sorted((sdk / 'shared/Microsoft.NETCore.App').glob('8.*'))[-1]
        cls.env = dict(os.environ, DOTNET_ROOT=str(sdk), DOTNET_CLI_HOME=str(out / 'cli-home'),
                       DOTNET_CLI_TELEMETRY_OPTOUT='1', DOTNET_SKIP_FIRST_TIME_EXPERIENCE='1',
                       DOTNET_NOLOGO='1', COMPUTER_USE_DESKTOP='shared')
        cls.command = [str(sdk / 'dotnet'), str(out / 'probe.dll')]
        command = [str(sdk / 'dotnet'), 'exec', str(compiler), '/nologo', '/noconfig', '/nostdlib+',
                   '/langversion:5', '/target:exe', '/warnaserror+', '/nowarn:0649', f'/out:{out / "probe.dll"}']
        command += ['/reference:' + str(p) for p in sorted(refs.glob('*.dll'))]
        command += [str(TESTS / name.removeprefix('Tests/')) if name.startswith('Tests/') else str(ROOT / 'backends/windows' / name) for name in (
            'Wire.cs', 'Driver.cs', 'NativeRuntime.cs', 'NativeExecution.cs', 'NativeKeyboard.cs', 'NativeText.cs',
            'NativeCaptureGeometry.cs', 'NativePointerCapture.cs', 'NativePointer.cs', 'NativeScroll.cs', 'NativeCapturedText.cs',
            'NativeClipboard.cs', 'NativePaste.cs', 'Tests/NativeDriverProtocolTests.cs')]
        compiled = subprocess.run(command, capture_output=True, text=True, env=cls.env, timeout=60)
        if compiled.returncode:
            raise AssertionError(compiled.stdout + compiled.stderr)
        (out / 'probe.runtimeconfig.json').write_text(json.dumps(dict(runtimeOptions=dict(
            tfm='net8.0', framework=dict(name='Microsoft.NETCore.App', version=runtime.name)))))

    async def start(self, mode='normal'):
        self.temp = tempfile.TemporaryDirectory(prefix='windows-session-')
        self.addCleanup(self.temp.cleanup)
        self.journal = Path(self.temp.name) / 'journal'
        self.process = await asyncio.create_subprocess_exec(*self.command, mode, str(self.journal), env=self.env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        self.addAsyncCleanup(self.close)
        self.next_id = 1

    async def close(self):
        if self.process.returncode is None:
            self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
                self.fail('dispatcher did not release its operation on EOF')
        errors = (await self.process.stderr.read()).decode()
        self.assertEqual(self.process.returncode, 0, errors)

    def events(self):
        return self.journal.read_text().splitlines() if self.journal.exists() else []

    async def wait_event(self, event):
        async with asyncio.timeout(5):
            while event not in self.events():
                await asyncio.sleep(.01)

    async def send(self, frame):
        self.process.stdin.write((json.dumps(frame) + '\n').encode())
        await self.process.stdin.drain()

    async def receive(self):
        line = await asyncio.wait_for(self.process.stdout.readline(), 5)
        self.assertTrue(line, 'dispatcher stopped without a response')
        return json.loads(line)

    async def exchange(self, frame):
        await self.send(frame)
        return await self.receive()

    async def hello(self, version=5, mode='shared'):
        response = await self.exchange(dict(version=version, id=0, method='hello', params=dict(desktopMode=mode)))
        if 'result' in response:
            self.session = response['session']
        return response

    def frame(self, method, params, *, timeout=10000):
        value = dict(version=5, session=self.session, id=self.next_id, method=method, params=params, timeoutMs=timeout)
        self.next_id += 1
        return value

    async def call(self, method, params, **kw):
        return await self.exchange(self.frame(method, params, **kw))

    def error(self, value, code):
        self.assertEqual(value['version'], 5)
        self.assertEqual(value['error']['code'], code, value)
        self.assertNotIn('context', value['error'])
        return value['error']

    def target(self):
        return dict(session=self.session, instance=None, rootWindow=None, surface=None, node=None, capture=None)

    async def begin(self):
        response = await self.call('begin', dict(mode='read', target=self.target(), intent=dict(kind='read', parameters={})))
        return response['result']['operation']

    def inspect(self, operation, **kw):
        return self.frame('inspect', dict(operation=operation, target=self.target(), query=dict(kind='applications')), **kw)

    async def test_version_boundary_and_failed_initialization_do_not_create_a_session(self):
        await self.start('fail-once')
        for version in (3, 4, 6):
            self.error(await self.hello(version), 'version_mismatch')
        self.error(await self.hello(mode='exclusive'), 'mode_mismatch')
        self.assertEqual(self.events(), [])
        failed = await self.hello()
        self.assertIn('fixture initialization failed', failed['error']['detail'])
        response = await self.hello()
        self.assertIn('nativeOperations', response['result']['capabilities'])
        self.assertIn('nativeDirectory', response['result']['capabilities'])
        self.assertNotIn('structuredObservations', response['result']['capabilities'])
        self.error(await self.hello(), 'invalid_request')
        self.assertEqual(self.events(), ['factory:1', 'factory:2'])
        operation = await self.begin()
        self.assertEqual((await self.call('finish', dict(operation=operation)))['result']['cleanup'], 'complete')

    async def test_only_native_commands_dispatch_and_replies_are_deduplicated(self):
        await self.start()
        await self.hello()
        legacy = ('listApps', 'readApp', 'readDesktop', 'findElement', 'readElementText', 'click', 'scroll', 'drag',
                  'typeText', 'paste', 'pressKey', 'clickElement', 'openElementMenu', 'performElementAction',
                  'pressKeyElement', 'focusElement', 'typeTextElement', 'pasteElement', 'hello', 'cancel', 'unknown')
        operation = await self.begin()
        for method in legacy:
            failure = self.error(await self.call(method, dict(legacyPayload=True)), 'invalid_request')
            self.assertIn('not supported by protocol v5', failure['detail'])
            self.assertEqual(failure['effect'], 'none')
        frame = self.inspect(operation)
        first = await self.exchange(frame)
        self.assertEqual(first['result']['applications'][0]['displayName'], 'Protocol fixture')
        self.assertEqual(await self.exchange(frame), first)
        conflict = dict(frame, timeoutMs=9000)
        self.error(await self.exchange(conflict), 'request_conflict')
        self.error(await self.exchange(dict(frame, version=3)), 'version_mismatch')
        self.error(await self.exchange(dict(frame, session='wrong')), 'session_mismatch')
        self.assertEqual(self.events().count('read:1'), 1)
        self.assertFalse(any(e.startswith('read:2') for e in self.events()))
        await self.call('finish', dict(operation=operation))
        for _ in range(70):
            self.error(await self.call('readApp', {}), 'invalid_request')
        self.error(await self.exchange(frame), 'request_expired')
        # Expired replies cannot re-execute a read or consume a new operation.
        new = await self.begin()
        await self.call('finish', dict(operation=new))

    async def test_cancel_and_busy_preserve_the_existing_operation_until_finish(self):
        await self.start('slow')
        await self.hello()
        operation = await self.begin()
        pending = self.inspect(operation)
        await self.send(pending)
        await self.wait_event('waiting')
        self.error(await self.call('begin', dict(mode='read', target=self.target(), intent=dict(kind='read', parameters={}))), 'busy')
        self.error(await self.exchange(pending), 'in_progress')
        await self.send(dict(version=5, session=self.session, cancel=pending['id']))
        cancelled = await self.receive()
        self.assertEqual(cancelled['id'], pending['id'])
        self.assertEqual(self.error(cancelled, 'cancelled')['effect'], 'none')
        self.assertEqual(await self.exchange(pending), cancelled)
        closed = await self.call('finish', dict(operation=operation))
        self.assertEqual(closed['result']['cleanup'], 'complete')
        self.assertEqual(self.events().count('read:1'), 1)
        self.assertEqual(self.events().count('released'), 1)
        new = await self.begin()
        await self.call('finish', dict(operation=new))

    async def test_eof_cancels_running_native_request_and_releases_its_operation(self):
        await self.start('slow')
        await self.hello()
        operation = await self.begin()
        await self.send(self.inspect(operation))
        await self.wait_event('waiting')
        self.process.stdin.close()
        await asyncio.wait_for(self.process.wait(), 5)
        self.assertEqual(self.events().count('released'), 1)
        self.assertEqual(self.events()[-1], 'closed')

    async def test_request_deadline_is_reported_without_losing_explicit_cleanup(self):
        await self.start('slow')
        await self.hello()
        operation = await self.begin()
        expired = await self.exchange(self.inspect(operation, timeout=80))
        self.assertEqual(self.error(expired, 'deadline_exceeded')['effect'], 'none')
        closed = await self.call('finish', dict(operation=operation))
        self.assertEqual(closed['result']['cleanup'], 'complete')
        self.assertEqual(self.events().count('read:1'), 1)
        self.assertEqual(self.events().count('released'), 1)
