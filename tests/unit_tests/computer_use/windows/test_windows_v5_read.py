"""Portable Windows v5 lifecycle tests; never claim Windows GUI execution.

Set COMPUTER_USE_WINDOWS_TEST_DOTNET to a standalone official .NET 8 SDK root
for the actual C# runtime probe. The full driver is separately cross-compiled
against the official net48 reference assemblies. No toolchain is downloaded
or installed by these tests.
"""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from computer_use.protocol.execution import decode_execution_result
from computer_use.protocol.facts import decode_facts
from computer_use.client.connection import Connection
from computer_use.client.native import native_computer
from computer_use.execution.native import HostPolicy
from computer_use.execution.transport import ConnectionPort
from computer_use.errors import ComputerUseError
from computer_use.native_semantics import READ_RELATIONS
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4] / "jiuwenswarm/cua_engine"
TESTS = Path(__file__).resolve().parent


class WindowsReadFactContractTests(unittest.TestCase):
    def test_raw_properties_unknowns_and_relations_keep_native_identity(self):
        value = dict(kind='nodes', session='session', surface='surface', platform='windows',
            observed=dict(id='stamp', startedAt=1, completedAt=2), roots=['button'],
            nodes=[dict(node='button', requestedFields=['ControlType', 'Name', 'Value.Value'],
                values={'ControlType': 'ControlType.Button', 'Name': 'Open'},
                fieldStatus={'Value.Value': {'state': 'unsupported'}},
                actions=dict(state='complete', items=[dict(name='Invoke.Invoke', description='Invoke.Invoke')], total=1))],
            requestedRelations=['RawViewChildren', 'RawViewParent', 'LabeledBy'],
            relations=[dict(source='button', property=p, targets=['label'] if p == 'RawViewChildren' else [],
                            certainty='confirmed', state='complete') for p in ('RawViewChildren', 'RawViewParent', 'LabeledBy')],
            unread=['label'], stopReasons=['Some related nodes remain unread.'])
        facts = decode_facts(value)
        self.assertEqual(facts['nodes'][0]['values']['ControlType'], 'ControlType.Button')
        self.assertNotIn('Value.Value', facts['nodes'][0]['values'])
        self.assertEqual(facts['unread'], ['label'])
        # These are wire examples, not evidence that UIA returned the fields.
        self.assertEqual(facts['nodes'][0]['actions']['items'][0]['name'], 'Invoke.Invoke')

    def test_relation_continuation_preserves_scope_and_partial_status(self):
        relation = dict(kind='relation', session='session', surface='surface', platform='windows',
            observed=dict(id='stamp', startedAt=1, completedAt=2),
            relation=dict(source='parent', property='RawViewChildren', targets=['child'],
                certainty='confirmed', state='partial', offset=1, total=3, cursor='next'))
        self.assertEqual(decode_execution_result('inspect', relation)['relation']['offset'], 1)
        relation['relation']['state'] = 'complete'
        with self.assertRaises(ValueError):
            decode_execution_result('inspect', relation)


@unittest.skipUnless(os.environ.get('COMPUTER_USE_WINDOWS_TEST_DOTNET'), 'standalone .NET SDK not configured')
class WindowsCSharpLifecycleTests(unittest.TestCase):
    def test_production_runtime_contract_probe(self):
        sdk = Path(os.environ['COMPUTER_USE_WINDOWS_TEST_DOTNET'])
        compilers = list((sdk / 'sdk').glob('*/Roslyn/bincore/csc.dll'))
        refs = list((sdk / 'packs/Microsoft.NETCore.App.Ref').glob('*/ref/net8.0'))
        runtimes = list((sdk / 'shared/Microsoft.NETCore.App').glob('8.*'))
        self.assertTrue(compilers and refs and runtimes, 'a complete .NET 8 SDK is required')
        with tempfile.TemporaryDirectory(prefix='windows-v5-read-probe-') as temporary:
            out = Path(temporary)
            command = [str(sdk / 'dotnet'), 'exec', str(sorted(compilers)[-1]), '/nologo', '/noconfig', '/nostdlib+',
                       '/langversion:5', '/target:exe', '/warnaserror+', '/nowarn:0649', f'/out:{out / "probe.dll"}']
            command += ['/reference:' + str(p) for p in sorted(refs[-1].glob('*.dll'))]
            command += [str(TESTS / p.removeprefix('Tests/')) if p.startswith('Tests/') else str(ROOT / 'backends/windows' / p) for p in
                        ('Wire.cs', 'NativeRuntime.cs', 'NativeExecution.cs', 'NativeKeyboard.cs', 'NativeText.cs',
                         'NativeCaptureGeometry.cs', 'NativePointerCapture.cs', 'NativePointer.cs', 'NativeScroll.cs',
                         'NativeCapturedText.cs', 'NativeReadValues.cs', 'NativeClipboard.cs', 'NativePaste.cs', 'Tests/NativeReadProtocolTests.cs')]
            env = dict(os.environ, DOTNET_ROOT=str(sdk), DOTNET_CLI_HOME=str(out / 'cli-home'),
                       DOTNET_CLI_TELEMETRY_OPTOUT='1', DOTNET_SKIP_FIRST_TIME_EXPERIENCE='1', DOTNET_NOLOGO='1')
            compiled = subprocess.run(command, capture_output=True, text=True, env=env, timeout=60)
            self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
            (out / 'probe.runtimeconfig.json').write_text(json.dumps(dict(runtimeOptions=dict(
                tfm='net8.0', framework=dict(name='Microsoft.NETCore.App', version=sorted(runtimes)[-1].name)))))
            result = subprocess.run([str(sdk / 'dotnet'), str(out / 'probe.dll')], capture_output=True,
                                    text=True, env=env, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads(result.stdout)
            self.assertGreaterEqual(report['assertions'], 45)
            self.assertEqual(decode_execution_result('inspect', report['directory'])['platform'], 'windows')
            self.assertEqual(decode_execution_result('finish', report['finish'])['cleanup'], 'complete')
            journal = out / 'public-api.jsonl'
            async def public_read():
                connection = Connection(lambda: None, dict(command=str(sdk / 'dotnet'),
                    args=[str(out / 'probe.dll'), '--serve', str(journal)], timeoutMs=3000), protocol_version=5)
                try:
                    computer = native_computer(ConnectionPort(connection, HostPolicy()))
                    apps = await computer.listApps()
                    app = computer.app(apps[0].id)
                    self.assertIn('Protocol fixture', (await app.read()).text)
                    self.assertIn('nativeCapturedText', connection.capabilities)
                    self.assertIn('nativeTableFacts', connection.capabilities)
                    self.assertIn('nativeFollowRelations', connection.capabilities)
                    old_element = app.element('0')
                    self.assertIn('Protocol fixture', (await app.element('0').read()).text)
                    async def captured(element):
                        chunks, offset = [], 0
                        while True:
                            page = await element.readText(dict(offset=offset, length=4096))
                            chunks.append(page.text)
                            if page.next is None:
                                return ''.join(chunks), page.total
                            offset = page.next
                    text, total = await captured(old_element)
                    expected = '\u03b1' * 16383 + '🙂e\u0301\r\n' + 'b' * 8192
                    self.assertEqual(text, expected + ':v1', 'Continuation must retain the original observation')
                    self.assertEqual(total, len(text.encode('utf-16-le')) // 2)
                    self.assertEqual((await captured(app.element('0')))[0], expected + ':v2')
                    with self.assertRaises(ComputerUseError):
                        await app.element('0').click()
                finally:
                    connection.close()
                    await connection.wait_closed()
            with patch.dict(os.environ, {'COMPUTER_USE_DESKTOP': 'shared'}):
                asyncio.run(public_read())
            requests = [json.loads(line) for line in journal.read_text().splitlines()]
            pages = [r['params']['query']['childrenPage'] for r in requests
                     if r.get('method') == 'inspect' and 'childrenPage' in r['params'].get('query', {})]
            self.assertEqual(pages, [dict(cursor=None, limit=128)])
            self.assertFalse(any(r.get('method') in ('execute', 'readApp') for r in requests))
            node_queries = [r['params']['query'] for r in requests if r.get('method') == 'inspect'
                            and r['params']['query']['kind'] == 'nodes']
            self.assertEqual(len(node_queries), 2)
            for query in node_queries:
                self.assertEqual(query['followRelations'], list(READ_RELATIONS['windows']))
                self.assertIn('Grid.RowCount', query['advertisedFields'])
                self.assertIn('GridItem.RowSpan', query['fields'])
            text_queries = [r['params'] for r in requests if r.get('method') == 'inspect'
                            and r['params']['query']['kind'] == 'capturedText']
            self.assertGreater(len(text_queries), 2)
            self.assertTrue(all(p['target']['surface'] is None and p['target']['node'] is None for p in text_queries))
            self.assertEqual(len({p['query']['textRef'] for p in text_queries}), 2)


if __name__ == '__main__':
    unittest.main()
