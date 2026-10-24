"""A broken display transform must not erase acquired facts or input targets."""
import asyncio
import copy
import json
import unittest
from unittest.mock import patch

from computer_use.errors import ComputerUseError
from computer_use.observation import native_facts
from computer_use.observation.native_facts import DisplayReferences, normalize_facts
from computer_use.observation.reader import captured_field
from .test_native_facts import batch, edge, node


class ProjectionRecoveryTests(unittest.TestCase):
    def snapshot(self):
        field = node('field', AXRole='AXTextField', AXTitle='输入\n777 native element',
                     AXValue='preview🙂', AXEnabled=False, AXSelected=False, VendorNumber=0,
                     VendorEmpty='', VendorObject={'key': [False, 0, '', None]},
                     **{'settable.AXValue': True})
        field['requestedFields'].extend(['AXHelp', 'AXDescription'])
        field['fieldStatus'].update(AXValue=dict(state='truncated', textRef='text:original', totalUnits=100),
                                   AXHelp=dict(state='timeout', detail='provider slow'),
                                   AXDescription=dict(state='absent'))
        field['actions'] = dict(state='partial', total=2, items=[dict(name='AXShowMenu',
            descriptionStatus=dict(state='error', detail='description unavailable'))])
        other = node('menu', AXRole='AXMenuBar', AXTitle='Independent menu')
        wire = batch([node('window', AXRole='AXWindow', AXTitle='Window'), field, other],
                     [edge('window', 'field'), edge('field', 'window', 'unread',
                           property='AXLinkedUIElements', state='partial', total=4, cursor='original:cursor')],
                     roots=['window', 'menu'])
        wire['unread'] = ['unread']
        return normalize_facts(wire, DisplayReferences('session'))

    def test_failed_root_retains_every_raw_record_and_other_roots_stay_compressed(self):
        snapshot = self.snapshot()
        before = copy.deepcopy(snapshot.native)
        compose = native_facts.compose_controls
        def broken(root, **kwargs):
            if root['role'] == 'window':
                raise ValueError('bad projection\nnot a new node')
            return compose(root, **kwargs)
        with patch.object(native_facts, 'compose_controls', side_effect=broken):
            result = snapshot.render()
        recovered, normal = result['projections']
        self.assertEqual(recovered['presentation'], 'uncompressed')
        self.assertEqual(recovered['issue']['error'], 'ValueError')
        self.assertNotIn('presentation', normal)
        self.assertIn('Independent menu', normal['text'])
        self.assertEqual(result['refs'], snapshot.refs)
        self.assertEqual(snapshot.native, before)
        self.assertEqual(captured_field(snapshot, '1', 'AXValue').text_ref, 'text:original')
        # Independently decode the fallback, checking exact scalar values,
        # statuses, actions and ordered relation references, including missing
        # and cyclic targets. No assertion depends on its JSON spacing.
        records, current = {}, None
        for line in recovered['text'].splitlines()[1:]:
            if not line.startswith('\t'):
                current = line.split(' ', 1)[0]
                records[current] = dict(values={}, statuses={}, relations={})
                continue
            key, value = line[1:].split(': ', 1)
            if key == 'Native actions':
                records[current]['actions'] = json.loads(value)
            elif key.startswith('Relation '):
                records[current]['relations'][json.loads(key[9:])] = json.loads(value)
            elif key.endswith(' status'):
                records[current]['statuses'][json.loads(key[:-7])] = json.loads(value)
            else:
                records[current]['values'][json.loads(key)] = json.loads(value)
        reverse = {token: reference for reference, token in snapshot.refs.items()}
        for source in before['nodes'][:2]:
            record = records[reverse[source['node']]]
            self.assertEqual(record['values'], source['values'])
            self.assertEqual(record['statuses'], source['fieldStatus'])
            self.assertEqual(record['actions'], source['actions'])
            for relation in before['relations']:
                if relation['source'] == source['node']:
                    expected = {k: v for k, v in relation.items() if k not in ('source', 'property', 'targets')}
                    expected['targets'] = [reverse[token] for token in relation['targets']]
                    self.assertEqual(record['relations'][relation['property']], expected)

    def test_formatter_failure_uses_raw_graph_and_keeps_current_capture_metadata(self):
        snapshot = self.snapshot()
        before = copy.deepcopy(snapshot.native)
        with patch.object(native_facts, 'render_tree', side_effect=TypeError('format error')):
            result = snapshot.render()
        self.assertEqual(len(result['projections']), 2)
        self.assertTrue(all(p['presentation'] == 'uncompressed' for p in result['projections']))
        self.assertEqual(result['textRefs'], snapshot.text_refs)
        self.assertEqual({(s['source'], s['property']) for s in result['relationSources']},
                         {(s['source'], s['property']) for s in snapshot.native['relations']})
        self.assertTrue(all(s['representation'] == 'uncompressed' for s in result['relationSources']))
        self.assertEqual(snapshot.native, before)
        self.assertNotIn('\n777 native element', result['text'])

    def test_cancellation_exhaustion_and_operation_errors_are_not_presentation_failures(self):
        failure = ComputerUseError(dict(code='stale_target', effect='none', target='test',
                                        detail='wrong identity', next='read again'))
        for error in (asyncio.CancelledError(), MemoryError(), TimeoutError(), failure):
            with self.subTest(error=type(error).__name__):
                with patch.object(native_facts, 'render_tree', side_effect=error):
                    with self.assertRaises(type(error)):
                        self.snapshot().render()

    def test_protocol_and_source_identity_failures_cannot_publish_a_fallback(self):
        snapshot = self.snapshot()
        malformed = copy.deepcopy(snapshot.native)
        malformed['relations'][0]['targets'] = ['unregistered']
        with patch.object(native_facts, 'render_tree') as render:
            with self.assertRaises(ValueError):
                normalize_facts(malformed, DisplayReferences('session'))
            del snapshot.refs['1']
            with self.assertRaisesRegex(ValueError, 'identity before presentation'):
                snapshot.render()
            render.assert_not_called()

    def test_same_fallback_preserves_windows_and_linux_native_properties(self):
        for platform, values in (
            ('windows', {'ControlType': 'ControlType.Edit', 'Name': '字段',
                         'Value.Value': 'text', 'Value.IsReadOnly': False}),
            ('linux', {'role': 'entry', 'name': '字段', 'value': 'text', 'editable': True}),
        ):
            with self.subTest(platform=platform):
                wire = batch([node('field', **values)])
                wire['platform'] = platform
                snapshot = normalize_facts(wire, DisplayReferences('session'))
                with patch.object(native_facts, 'compose_controls', side_effect=ValueError('fixture')):
                    output = snapshot.render()
                lines = output['text'].splitlines()
                restored = {json.loads(k): json.loads(v) for line in lines if line.startswith('\t"')
                            for k, v in (line[1:].split(': ', 1),)}
                self.assertEqual(restored, values)
                self.assertEqual(output['refs'], {'0': 'field'})


if __name__ == '__main__':
    unittest.main()
