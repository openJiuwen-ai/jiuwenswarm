"""The production encoder and native decoder share request conformance cases."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import unittest

from computer_use.execution.native import Actuator, ScrollActuator, SelectionDependency, ApplicationBinding, Binding, HostPolicy, VirtualReceiver, FocusMember
from computer_use.execution.transport import ConnectionPort
from computer_use.protocol.execution import decode_inspect_query

ROOT = Path(__file__).resolve().parent
CASES = json.loads((ROOT / 'fixtures/native-inspect-queries.json').read_text())
PROBE = os.environ.get('COMPUTER_USE_NATIVE_QUERY_PROBE')


class NativeQueriesTests(unittest.TestCase):
    def test_shared_request_cases(self):
        for case in CASES:
            with self.subTest(case['name']):
                if case['valid']:
                    result = decode_inspect_query(case['query'])
                    self.assertEqual(result, case['query'])
                    self.assertIsNot(result, case['query'])
                else:
                    with self.assertRaises(ValueError):
                        decode_inspect_query(case['query'])

    def test_constraints_are_bounded_and_owned(self):
        with self.assertRaises(ValueError):
            decode_inspect_query(dict(kind='inputContext', within=[str(n) for n in range(17)]))
        original = dict(kind='inputContext', within=['root'])
        result = decode_inspect_query(original)
        result['within'].append('group')
        self.assertEqual(original['within'], ['root'])

    @unittest.skipUnless(PROBE, 'run via test-macos for the compiled native decoder')
    def test_actual_port_queries_pass_the_native_decoder(self):
        class Captured(Exception):
            pass

        class RecordingConnection:
            protocol_version = 5
            session_identity = ('test-session', 1)
            platform = 'macos'
            capabilities = frozenset(('nativeControlParts', 'nativeSelectionBasis', 'nativeAdvertisedFields', 'nativeFollowRelations', 'nativeRelationPages', 'nativeVirtualFocus', 'nativeFocusMembers'))

            async def request(self, method, params, options):
                self.query = params['query']
                raise Captured

        async def encoded_queries():
            connection = RecordingConnection()
            port = ConnectionPort(connection, HostPolicy())
            port._operations['operation'] = (connection.session_identity, 'read-only query probe')
            queries = []
            for node, within, actuator in (('node', (), None), (None, (), None),
                    ('node', ('root', 'group'), None), ('node', (), Actuator('editor')),
                    ('node', (), Actuator('label', 'controlPart')), ('node', (), ScrollActuator('page', 'area', 'bar'))):
                try:
                    await port.inspect('operation', Binding('test-session', 'instance', 'root', 'surface', node),
                        1000, within=within, actuator=actuator,
                        basis=(SelectionDependency('observed', 'label', ('AXRole',), True),)
                            if actuator is not None and actuator.relation == 'controlPart' else ())
                except Captured:
                    queries.append(connection.query)
            try:
                await port.inspect('operation', ApplicationBinding('test-session', 'installation-observed'), 1000)
            except Captured:
                queries.append(connection.query)
            try:
                await port.captured_text('operation', Binding('test-session', None, None, None),
                                         'captured-field', 3, 4096, 1000)
            except Captured:
                queries.append(connection.query)
            node_target = Binding('test-session', 'instance', 'root', 'surface', 'node')
            try:
                await port.inspect('operation', node_target, 1000, receiver=FocusMember('label'),
                    basis=(SelectionDependency('observed', 'label', ('AXRole',), True, ('AXParent',)),))
            except Captured:
                queries.append(connection.query)
            for actuator in (None, Actuator('editor')):
                try:
                    await port.inspect('operation', node_target, 1000, actuator=actuator,
                        receiver=VirtualReceiver('popup', 'owns'),
                        basis=(SelectionDependency('observed', 'node', ('AXRole', 'AXHasPopup'), False, ('AXOwns',)),))
                except Captured:
                    queries.append(connection.query)
            port._continuations['child-page'] = ('test-session', 'surface', 'node', 'AXChildren', 128, 300)
            for page in (None, dict(cursor=None, limit=128), dict(cursor='child-page', limit=128)):
                try:
                    await port.nodes('operation', node_target, ['AXRole'], 1000, 40, 1000, children_page=page)
                except Captured:
                    queries.append(connection.query)
            port._continuations['row-page'] = ('test-session', 'surface', 'node', 'AXRows', 128, 300)
            for cursor in (None, 'row-page'):
                try:
                    await port.nodes('operation', node_target, ['AXRole'], 1000, 40, 1000,
                        relation_page=dict(property='AXRows', cursor=cursor, limit=128))
                except Captured:
                    queries.append(connection.query)
            try:
                await port.nodes('operation', node_target, ['AXRole', 'AXRowCount'], 1000, 40, 1000,
                                 advertised_fields=['AXRowCount'])
            except Captured:
                queries.append(connection.query)
            for kind in ('surfaces', 'roots'):
                try:
                    await port.directory('operation', Binding('test-session', None, None, None),
                                         dict(kind=kind, application='running:app'), 1000)
                except Captured:
                    queries.append(connection.query)
            try:
                await port.nodes('operation', node_target, ['AXRole'], 40, 3, 1000,
                                 follow_relations=['AXVisibleCells', 'AXRows'])
            except Captured:
                queries.append(connection.query)
            return queries

        queries = asyncio.run(encoded_queries())
        encoded_count = len(queries)
        queries += [case['query'] for case in CASES]
        result = subprocess.run([PROBE, '--stdio'], input=''.join(json.dumps(q) + '\n' for q in queries),
                                text=True, capture_output=True, check=True, timeout=10)
        self.assertEqual([json.loads(line) for line in result.stdout.splitlines()],
                         [True] * encoded_count + [case['valid'] for case in CASES])
