"""Real C# object-unit lifecycle via fake providers; no claim of Windows GUI."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from computer_use.client.connection import Connection
from computer_use.client.native import native_computer
from computer_use.execution.native import HostPolicy
from computer_use.execution.transport import ConnectionPort
from computer_use.protocol.execution import decode_execution_result
from computer_use.errors import ComputerUseError

ROOT = Path(__file__).resolve().parents[4] / "jiuwenswarm/cua_engine"
TESTS = Path(__file__).resolve().parent


def compile_probe(test, out, main):
    sdk = Path(os.environ['COMPUTER_USE_WINDOWS_TEST_DOTNET'])
    compilers = sorted((sdk / 'sdk').glob('*/Roslyn/bincore/csc.dll'))
    refs = sorted((sdk / 'packs/Microsoft.NETCore.App.Ref').glob('*/ref/net8.0'))
    runtimes = sorted((sdk / 'shared/Microsoft.NETCore.App').glob('8.*'))
    test.assertTrue(compilers and refs and runtimes)
    env = dict(os.environ, DOTNET_ROOT=str(sdk), DOTNET_CLI_HOME=str(out / 'home'),
               DOTNET_CLI_TELEMETRY_OPTOUT='1', DOTNET_SKIP_FIRST_TIME_EXPERIENCE='1')
    command = [str(sdk / 'dotnet'), 'exec', str(compilers[-1]), '/nologo', '/noconfig', '/nostdlib+',
               '/langversion:5', '/target:exe', '/warnaserror+', '/nowarn:0649',
               f'/main:ComputerUse.Windows.{main}', f'/out:{out / "probe.dll"}']
    command += ['/reference:' + str(p) for p in sorted(refs[-1].glob('*.dll'))]
    command += [str(TESTS / p.removeprefix('Tests/')) if p.startswith('Tests/') else str(ROOT / 'backends/windows' / p) for p in (
        'Wire.cs', 'NativeRuntime.cs', 'NativeExecution.cs', 'NativeKeyboard.cs', 'NativeText.cs', 'NativeCapturedText.cs', 'NativeReadValues.cs', 'NativeClipboard.cs', 'NativePaste.cs',
        'NativeCaptureGeometry.cs', 'NativePointerCapture.cs', 'NativePointer.cs', 'NativeScroll.cs', 'Tests/NativeScrollTests.cs', 'Tests/NativePointerTests.cs',
        'Tests/NativeReadProtocolTests.cs', 'Tests/NativeObjectExecutionTests.cs', 'Tests/NativeKeyboardTests.cs',
        'Tests/NativeTextTests.cs', 'Tests/NativePasteTests.cs', 'Tests/NativeDesktopTests.cs')]
    compiled = subprocess.run(command, capture_output=True, text=True, env=env, timeout=60)
    test.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
    (out / 'probe.runtimeconfig.json').write_text(json.dumps(dict(runtimeOptions=dict(
        tfm='net8.0', framework=dict(name='Microsoft.NETCore.App', version=runtimes[-1].name)))))
    return sdk, env


@unittest.skipUnless(os.environ.get('COMPUTER_USE_WINDOWS_TEST_DOTNET'), 'standalone .NET SDK not configured')
class WindowsScrollRuntimeTests(unittest.TestCase):
    def test_addressed_scrolling_guards_and_dispatch(self):
        with tempfile.TemporaryDirectory(prefix='windows-v5-scroll-probe-') as temporary:
            out = Path(temporary)
            sdk, env = compile_probe(self, out, 'NativeScrollTests')
            result = subprocess.run([str(sdk / 'dotnet'), str(out / 'probe.dll')],
                                    capture_output=True, text=True, env=env, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads(result.stdout)
            self.assertGreaterEqual(report['assertions'], 15)
            self.assertEqual(decode_execution_result('inspect', report['facts'])['primitives'][0]['unit'], 'native_scroll')
            self.assertEqual(decode_execution_result('execute', report['receipt'])['dispatch'], 'notDispatched')


@unittest.skipUnless(os.environ.get('COMPUTER_USE_WINDOWS_TEST_DOTNET'), 'standalone .NET SDK not configured')
class WindowsObjectRuntimeTests(unittest.TestCase):
    def test_production_units_and_public_python_chain(self):
        with tempfile.TemporaryDirectory(prefix='windows-v5-object-probe-') as temporary:
            out = Path(temporary)
            sdk, env = compile_probe(self, out, 'NativeObjectExecutionTests')
            run = subprocess.run([str(sdk / 'dotnet'), str(out / 'probe.dll')],
                                 capture_output=True, text=True, env=env, timeout=30)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            report = json.loads(run.stdout)
            self.assertGreaterEqual(report['assertions'], 30)
            self.assertEqual(decode_execution_result('inspect', report['facts'])['enabled'], 'yes')
            self.assertEqual(decode_execution_result('execute', report['receipt'])['dispatch'], 'dispatched')
            self.assertEqual(decode_execution_result('capture', report['windowCapture'])['kind'], 'capture')
            self.assertEqual(decode_execution_result('capture', report['desktopCapture'])['kind'], 'desktopCapture')
            journal = out / 'wire.jsonl'

            async def public_chain():
                connection = Connection(lambda: None, dict(command=str(sdk / 'dotnet'),
                    args=[str(out / 'probe.dll'), '--serve', str(journal)], timeoutMs=5000), protocol_version=5)
                try:
                    computer = native_computer(ConnectionPort(connection, HostPolicy()))
                    app = computer.app((await computer.listApps())[0].id)
                    state = await app.read()
                    self.assertIsNotNone(state.screenshot)
                    field = app.element('0')
                    await field.typeText('新值 😀', dict(replace=True))
                    self.assertIn('新值 😀', (await app.read(dict(full=True))).text)
                    await app.element('0').click()
                    await app.element('0').performAction('Toggle.Toggle')
                    with self.assertRaises(ComputerUseError):
                        await app.element('0').typeText('insert')
                    with self.assertRaises(ComputerUseError):
                        await app.element('0').pressKey('Return')
                    self.assertIsNotNone((await computer.read()).screenshot)
                finally:
                    connection.close()
                    await connection.wait_closed()

            with patch.dict(os.environ, {'COMPUTER_USE_DESKTOP': 'shared'}):
                asyncio.run(public_chain())
            effects = json.loads(Path(str(journal) + '.effects.json').read_text())
            self.assertEqual(effects['text'], '新值 😀')
            self.assertEqual(effects['sent'], 3)
            self.assertEqual(effects['acquired'], effects['released'])
            requests = [json.loads(line) for line in journal.read_text().splitlines()]
            self.assertEqual(len([r for r in requests if r.get('method') == 'execute']), 3)
            self.assertLessEqual({r['method'] for r in requests if 'method' in r},
                                 {'hello', 'begin', 'inspect', 'capture', 'execute', 'finish'})


@unittest.skipUnless(os.environ.get('COMPUTER_USE_WINDOWS_TEST_DOTNET'), 'standalone .NET SDK not configured')
class WindowsKeyboardRuntimeTests(unittest.TestCase):
    def test_production_keyboard_guards_and_public_python_policy(self):
        with tempfile.TemporaryDirectory(prefix='windows-v5-keyboard-probe-') as temporary:
            out = Path(temporary)
            sdk, env = compile_probe(self, out, 'NativeKeyboardTests')
            run = subprocess.run([str(sdk / 'dotnet'), str(out / 'probe.dll')],
                                 capture_output=True, text=True, env=env, timeout=30)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            report = json.loads(run.stdout)
            self.assertGreaterEqual(report['assertions'], 35)
            self.assertEqual(decode_execution_result('inspect', report['facts'])['kind'], 'inputContext')
            self.assertEqual(decode_execution_result('execute', report['receipt'])['cleanup'], 'failed')

            async def public_chain(assist):
                journal = out / ('assist.jsonl' if assist else 'strict.jsonl')
                connection = Connection(lambda: None, dict(command=str(sdk / 'dotnet'),
                    args=[str(out / 'probe.dll'), '--serve', str(journal), 'assist' if assist else 'strict'],
                    timeoutMs=5000), protocol_version=5)
                try:
                    computer = native_computer(ConnectionPort(connection, HostPolicy(foreground_assist=assist)))
                    app = computer.app((await computer.listApps())[0].id)
                    await app.read()
                    # Independent object writes never require a foreground key route.
                    await app.element('0').typeText('addressed replacement', dict(replace=True))
                    await app.element('0').click()
                    before = json.loads(Path(str(journal) + '.effects.json').read_text())
                    self.assertEqual(before['objectSent'], 2)
                    self.assertEqual((before['activated'], before['focused'], before['sent']), (0, 0, 0))
                    if assist:
                        await app.element('0').focus()
                        await app.element('0').pressKey('ctrl+p')
                        await app.pressKey('Escape')
                    else:
                        for action in (app.element('0').focus, lambda: app.element('0').pressKey('Return'),
                                       lambda: app.pressKey('ctrl+p')):
                            with self.assertRaises(ComputerUseError) as error:
                                await action()
                            self.assertEqual(error.exception.effect, 'none')
                finally:
                    connection.close()
                    await connection.wait_closed()
                effects = json.loads(Path(str(journal) + '.effects.json').read_text())
                self.assertEqual(effects['acquired'], effects['released'])
                self.assertEqual((effects['activated'], effects['focused'], effects['sent']),
                                 (1, 1, 2) if assist else (0, 0, 0))
                self.assertEqual(effects['foreground'], assist)
                requests = [json.loads(line) for line in journal.read_text().splitlines()]
                self.assertLessEqual({r['method'] for r in requests if 'method' in r},
                                     {'hello', 'begin', 'inspect', 'capture', 'execute', 'finish'})
                if assist:
                    steps = [r['params']['command'] for r in requests if r.get('method') == 'execute']
                    self.assertEqual([c['kind'] for c in steps],
                                     ['native_text_edit', 'native_action', 'prepare_receiver', 'key_sequence', 'key_sequence'])
                    self.assertEqual([c['kind'] for c in steps if c['foreground']], ['prepare_receiver'])
                    self.assertIsNone(steps[-1]['target']['node'])

            with patch.dict(os.environ, {'COMPUTER_USE_DESKTOP': 'shared'}):
                asyncio.run(public_chain(False))
                asyncio.run(public_chain(True))


@unittest.skipUnless(os.environ.get('COMPUTER_USE_WINDOWS_TEST_DOTNET'), 'standalone .NET SDK not configured')
class WindowsTextRuntimeTests(unittest.TestCase):
    def test_production_text_recipe_and_public_python_chain(self):
        with tempfile.TemporaryDirectory(prefix='windows-v5-text-probe-') as temporary:
            out = Path(temporary)
            sdk, env = compile_probe(self, out, 'NativeTextTests')
            run = subprocess.run([str(sdk / 'dotnet'), str(out / 'probe.dll')],
                                 capture_output=True, text=True, env=env, timeout=30)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            report = json.loads(run.stdout)
            self.assertGreaterEqual(report['assertions'], 45)
            self.assertEqual(decode_execution_result('inspect', report['facts'])['kind'], 'inputContext')
            self.assertEqual(decode_execution_result('execute', report['receipt'])['dispatch'], 'dispatched')

            async def public_chain(assist):
                journal = out / ('assist.jsonl' if assist else 'strict.jsonl')
                connection = Connection(lambda: None, dict(command=str(sdk / 'dotnet'),
                    args=[str(out / 'probe.dll'), '--serve', str(journal), 'assist' if assist else 'strict'],
                    timeoutMs=5000), protocol_version=5)
                try:
                    computer = native_computer(ConnectionPort(connection, HostPolicy(foreground_assist=assist)))
                    app = computer.app((await computer.listApps())[0].id)
                    await app.read()
                    if assist:
                        await app.element('0').typeText('新值 😀', dict(replace=True))
                        await app.element('0').typeText('末')
                        await app.typeText('尾')
                        await app.element('0').pressKey('Return')
                        self.assertIn('新值 😀末尾', (await app.read(dict(full=True))).text)
                    else:
                        for action in (lambda: app.element('0').typeText('wrong'),
                                       lambda: app.element('0').typeText('wrong', dict(replace=True)),
                                       lambda: app.typeText('wrong')):
                            with self.assertRaises(ComputerUseError) as error:
                                await action()
                            self.assertEqual(error.exception.effect, 'none')
                finally:
                    connection.close()
                    await connection.wait_closed()
                effects = json.loads(Path(str(journal) + '.effects.json').read_text())
                self.assertEqual(effects['acquired'], effects['released'])
                self.assertEqual(effects['text'], '新值 😀末尾' if assist else 'initial')
                self.assertEqual(effects['objectSent'], 0)
                self.assertEqual((effects['activated'], effects['focused'], effects['selected'], effects['submitted']),
                                 (1, 1, 1, 1) if assist else (0, 0, 0, 0))
                requests = [json.loads(line) for line in journal.read_text().splitlines()]
                self.assertLessEqual({r['method'] for r in requests if 'method' in r},
                                    {'hello', 'begin', 'inspect', 'capture', 'execute', 'finish'})
                steps = [r['params']['command'] for r in requests if r.get('method') == 'execute']
                self.assertEqual([c['kind'] for c in steps],
                                 ['prepare_receiver'] + ['key_sequence'] * 4 if assist else [])
                if assist:
                    self.assertIsNone(steps[-2]['target']['node'])
                    self.assertEqual([c['kind'] for c in steps if c['foreground']], ['prepare_receiver'])

            with patch.dict(os.environ, {'COMPUTER_USE_DESKTOP': 'shared'}):
                asyncio.run(public_chain(False))
                asyncio.run(public_chain(True))


if __name__ == '__main__':
    unittest.main()
