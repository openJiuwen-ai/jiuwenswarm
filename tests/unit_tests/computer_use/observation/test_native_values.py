import copy
import unittest

from computer_use.observation.native_facts import DisplayReferences, normalize_facts
from computer_use.observation.native_values import display_native_value
from computer_use.protocol.facts import decode_facts
from .test_native_facts import batch, node, edge


class NativeValueTests(unittest.TestCase):
    def test_dates_preserve_epoch_timezone_and_submicrosecond_fraction(self):
        for seconds, expected in [(0, '2001-01-01T00:00:00Z'), (-0.25, '2000-12-31T23:59:59.75Z'),
                (0.1234567, '2001-01-01T00:00:00.1234567Z'),
                (-1e-30, '2000-12-31T23:59:59.' + '9' * 30 + 'Z')]:
            raw = dict(nativeType='CFDate', absoluteTime=seconds)
            self.assertEqual(display_native_value('macos', raw, {}), expected)
            wire = batch([node('date', AXRole='AXGroup', AXValue=raw)])
            before = copy.deepcopy(wire)
            snapshot = normalize_facts(wire, DisplayReferences('session'))
            self.assertIn(expected, snapshot.render(compact=True)['text'])
            self.assertIn('"nativeType":"CFDate"', snapshot.render(compact=True)['details'])
            self.assertEqual(snapshot.native, before)
            self.assertEqual(wire, before)

    def test_unknown_or_out_of_range_dates_remain_explicit_raw_facts(self):
        for raw in [dict(nativeType='CFDate', absoluteTime=1e100), dict(nativeType='CFDate', absoluteTime=True),
                    dict(nativeType='CFDate', absoluteTime=0, extra='unknown')]:
            self.assertIsNone(display_native_value('macos', raw, {}))
            rendered = normalize_facts(batch([node('date', AXValue=raw)]), DisplayReferences('session')).render(compact=True)
            self.assertIn('CFDate', rendered['details'])
        self.assertIsNone(display_native_value('windows', dict(nativeType='CFDate', absoluteTime=0), {}))

    def test_object_values_retain_target_reference_without_inventing_selection_or_parentage(self):
        raw = dict(nativeType='AXUIElement', node='choice')
        wire = batch([node('group', AXRole='AXRadioGroup', AXValue=raw),
                      node('choice', AXRole='AXRadioButton', AXTitle='Month', AXValue=1)],
                     [edge('group', 'choice')])
        snapshot = normalize_facts(wire, DisplayReferences('session'))
        rendered = snapshot.render(compact=True)
        self.assertIn('Value: element 1', rendered['text'])
        self.assertIn('Month', rendered['text'])
        self.assertIn('"nativeType":"AXUIElement","node":"choice"', rendered['details'])
        self.assertEqual(rendered['refs']['1'], 'choice')
        self.assertNotIn('selected', snapshot.roots[0]['children'][0])
        self.assertEqual(snapshot.native, wire)
        self.assertNotIn('0', snapshot.value_fields, 'A referenced object is not captured input text')

    def test_unread_value_reference_is_exposed_and_bound_but_not_expanded(self):
        wire = batch([node('group', AXRole='AXRadioGroup', AXValue=dict(nativeType='AXUIElement', node='outside'))])
        wire['unread'] = ['outside']
        snapshot = normalize_facts(wire, DisplayReferences('session'))
        rendered = snapshot.render(compact=True)
        self.assertIn('Value: element 1', rendered['text'])
        self.assertEqual(rendered['refs']['1'], 'outside')
        self.assertEqual(snapshot.roots[0]['children'], [])
        wire['unread'] = []
        with self.assertRaisesRegex(ValueError, 'undeclared identity'):
            decode_facts(wire)


if __name__ == '__main__':
    unittest.main()
