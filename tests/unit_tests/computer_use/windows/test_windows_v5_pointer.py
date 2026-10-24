"""Public Python -> production C# pointer runtime, using explicit fake OS I/O."""
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
from computer_use.errors import ComputerUseError
from computer_use.execution.native import HostPolicy
from computer_use.execution.transport import ConnectionPort
from computer_use.protocol.execution import decode_execution_result
from .test_windows_v5_execution import compile_probe


@unittest.skipUnless(os.environ.get('COMPUTER_USE_WINDOWS_TEST_DOTNET'), 'standalone .NET SDK not configured')
class WindowsPointerRuntimeTests(unittest.TestCase):
    def test_pointer_contracts_cleanup_and_public_window_api(self):
        with tempfile.TemporaryDirectory(prefix='windows-v5-pointer-probe-') as temporary:
            out = Path(temporary)
            sdk, env = compile_probe(self, out, 'NativePointerTests')
            run = subprocess.run([str(sdk / 'dotnet'), str(out / 'probe.dll')],
                                 capture_output=True, text=True, env=env, timeout=30)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            report = json.loads(run.stdout)
            self.assertGreaterEqual(report['assertions'], 65)
            self.assertEqual(decode_execution_result('inspect', report['facts'])['input']['pointerRoute'], 'no')
            self.assertEqual(decode_execution_result('execute', report['receipt'])['cleanup'], 'failed')

            async def public_chain(setting):
                journal = out / f'{setting}.jsonl'
                connection = Connection(lambda: None, dict(command=str(sdk / 'dotnet'),
                    args=[str(out / 'probe.dll'), '--serve', str(journal), setting], timeoutMs=5000), protocol_version=5)
                try:
                    computer = native_computer(ConnectionPort(connection, HostPolicy(foreground_assist=setting == 'assist')))
                    app = computer.app((await computer.listApps())[0].id)
                    self.assertIsNotNone((await app.read()).screenshot)
                    actions = [lambda: app.click(0, 0, dict(button='right', count=2)),
                               lambda: app.scroll(0, 0, 'left', 0.5),
                               lambda: app.drag(dict(fromX=0, fromY=0, toX=1, toY=1))]
                    for action in actions:
                        if setting == 'strict':
                            with self.assertRaises(ComputerUseError) as error:
                                await action()
                            self.assertEqual(error.exception.effect, 'none')
                        else:
                            await action()
                    # Independent addressed actions retain their original route.
                    await app.element('0').click()
                    await computer.read()
                    with self.assertRaises(ComputerUseError) as error:
                        await computer.click(0, 0)
                    self.assertEqual(error.exception.effect, 'none')
                finally:
                    connection.close()
                    await connection.wait_closed()
                effects = json.loads(Path(str(journal) + '.effects.json').read_text())
                self.assertEqual(effects['acquired'], effects['released'])
                self.assertEqual(effects['activated'], int(setting == 'assist'))
                self.assertEqual(effects['batches'], 0 if setting == 'strict' else 29)
                self.assertEqual(effects['objectSent'], 1)
                requests = [json.loads(line) for line in journal.read_text().splitlines()]
                self.assertLessEqual({r['method'] for r in requests if 'method' in r},
                                    {'hello', 'begin', 'inspect', 'capture', 'execute', 'finish'})
                commands = [r['params']['command'] for r in requests if r.get('method') == 'execute']
                self.assertEqual([c['kind'] for c in commands],
                    (['prepare_receiver'] if setting == 'assist' else []) +
                    (['pointer_sequence'] * 3 if setting != 'strict' else []) + ['native_action'])
                self.assertEqual([c['kind'] for c in commands if c['foreground']],
                                 ['prepare_receiver'] if setting == 'assist' else [])
                self.assertTrue(all(c['target']['capture'] is not None and c['target']['node'] is None
                                    for c in commands if c['kind'] != 'native_action'))

            with patch.dict(os.environ, {'COMPUTER_USE_DESKTOP': 'shared'}):
                for setting in ('strict', 'assist', 'ready'):
                    asyncio.run(public_chain(setting))


if __name__ == '__main__':
    unittest.main()
