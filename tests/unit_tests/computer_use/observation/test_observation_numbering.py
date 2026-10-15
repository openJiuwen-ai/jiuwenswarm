"""Public row numbers are local to a result, never native object identities."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from computer_use.observation.native_facts import DisplayReferences, normalize_facts
from computer_use.observation.numbering import number_snapshots
from computer_use.observation.reader import captured_field, captured_children
from computer_use.observation.snapshot import snapshot_diff
from .test_native_facts import batch, edge, node


def visible(snapshot):
    return [ref for part in snapshot.render(compact=True)['projections'] for ref in part['refs']]


class NumberingTests(unittest.TestCase):
    def snapshot(self):
        label = node('caption', AXRole='AXStaticText', AXValue='Wi-Fi 503')
        label['actions']['items'] = [dict(name='AXShowMenu', description='Show menu')]
        raw = batch([node('window', AXRole='AXWindow'), node('wrapper', AXRole='AXGroup'),
                     node('button', AXRole='AXButton', AXTitle='Button 123'),
                     node('row', AXRole='AXRow', AXSelected=True), node('cell', AXRole='AXCell'), label],
                    [edge('window', 'wrapper', 'button'), edge('wrapper', 'row'),
                     edge('row', 'cell'), edge('cell', 'caption')])
        references = DisplayReferences('session')
        for index in range(60):
            references.reference('earlier-' + str(index))
        return normalize_facts(raw, references, driver_features=('nativeActionIntents',), scope_kind='window')

    def test_final_rows_are_contiguous_after_compression_and_raw_sources_survive(self):
        original = self.snapshot()
        before = deepcopy(original.native)
        old_refs = dict(original.refs)
        numbered, = number_snapshots((original,))
        self.assertEqual(visible(numbered), ['0', '1', '2'])
        self.assertEqual([numbered.refs[ref] for ref in visible(numbered)], ['window', 'row', 'button'])
        self.assertEqual(numbered.refs['5'], 'caption')
        self.assertIn('Show menu [element 5]', numbered.render()['details'])
        self.assertIn('Wi-Fi 503', numbered.render(compact=True)['text'])
        self.assertIn('Button 123', numbered.render(compact=True)['text'])
        self.assertEqual(captured_field(numbered, '5', 'AXValue').value, 'Wi-Fi 503')
        self.assertEqual(numbered.native, before)
        self.assertEqual(original.native, before)
        self.assertEqual(original.refs, old_refs)
        self.assertEqual(set(numbered.refs.values()), set(old_refs.values()))
        self.assertEqual(numbered.driver_features, original.driver_features)
        self.assertEqual(numbered.scope_kind, original.scope_kind)
        self.assertIs(number_snapshots((numbered,))[0], numbered)

    def test_window_and_menus_share_visible_sequence_before_hidden_aliases(self):
        menu = normalize_facts(batch([node('menu', AXRole='AXMenuBar'), node('file', AXRole='AXMenuBarItem', AXTitle='File')],
                                    [edge('menu', 'file')]), DisplayReferences('session'), scope_kind='applicationMenu')
        first, second = number_snapshots((self.snapshot(), menu))
        self.assertEqual(visible(first) + visible(second), ['0', '1', '2', '3', '4'])
        self.assertTrue(set(first.refs).isdisjoint(second.refs))
        self.assertEqual(first.refs['7'], 'caption')
        self.assertEqual(second.scope_kind, 'applicationMenu')
        self.assertEqual(visible(number_snapshots((menu,))[0]), ['0', '1'])

    def test_same_native_object_in_separate_bound_parts_keeps_separate_aliases(self):
        first, second = number_snapshots((self.snapshot(), self.snapshot()))
        self.assertEqual(visible(first) + visible(second), [str(i) for i in range(6)])
        self.assertTrue(set(first.refs).isdisjoint(second.refs))
        self.assertEqual(first.refs['0'], second.refs['3'])

    def test_partial_relations_and_text_continuations_keep_native_carriers(self):
        text = node('text', AXRole='AXTextField', AXValue='preview')
        text['fieldStatus']['AXValue'] = dict(state='truncated', textRef='captured:original', totalUnits=100)
        raw = batch([text, node('root', AXRole='AXGroup')],
                    [edge('root', 'text', state='partial', total=10, offset=2, cursor='cursor:original')], roots=['root'])
        snapshot = normalize_facts(raw, DisplayReferences('session'), driver_features=('nativeNodePages',))
        numbered, = number_snapshots((snapshot,))
        self.assertEqual(visible(numbered), ['0', '1'])
        self.assertEqual(numbered.refs, {'1': 'text', '0': 'root'})
        self.assertEqual(captured_field(numbered, '1', 'AXValue').text_ref, 'captured:original')
        self.assertEqual(captured_children(numbered, '0').cursor, 'cursor:original')
        self.assertIn('app.element("0").read({"offset": 3})', numbered.render(compact=True)['text'])
        self.assertEqual(numbered.native, snapshot.native)

    def test_unread_and_cyclic_relations_use_new_aliases_without_losing_targets(self):
        raw = batch([node('child', AXRole='AXGroup'), node('root', AXRole='AXWindow')],
                    [edge('root', 'child', 'unread'), edge('child', 'root')], roots=['root'])
        raw['unread'] = ['unread']
        original = normalize_facts(raw, DisplayReferences('session'))
        numbered, = number_snapshots((original,))
        self.assertEqual(visible(numbered), ['0', '1'])
        self.assertEqual(numbered.refs, {'1': 'child', '0': 'root', '2': 'unread'})
        self.assertIn('1 contains 0 (cycle)', numbered.render()['details'])
        self.assertIn('0 contains 2 (unread)', numbered.render()['details'])
        self.assertEqual(numbered.native, original.native)

    def test_projection_failure_numbers_the_lossless_fallback_in_its_display_order(self):
        original = self.snapshot()
        with patch('computer_use.observation.native_facts.compose_controls', side_effect=ValueError('fixture')):
            numbered, = number_snapshots((original,))
            self.assertEqual(visible(numbered), [str(i) for i in range(6)])
            self.assertIn('Presentation fallback for 0', numbered.render(compact=True)['text'])
        self.assertEqual(numbered.native, original.native)
        self.assertEqual(numbered.refs['5'], 'button')

    def test_numbers_are_platform_independent(self):
        for platform, role, title in (('windows', 'ControlType', 'Name'), ('linux', 'role', 'name')):
            with self.subTest(platform=platform):
                values = {role: 'ControlType.Button' if platform == 'windows' else 'push button', title: 'Test'}
                raw = batch([node('button', **values)]); raw['platform'] = platform
                references = DisplayReferences('session'); references.reference('past')
                original = normalize_facts(raw, references)
                numbered, = number_snapshots((original,))
                self.assertEqual(visible(numbered), ['0'])
                self.assertEqual(numbered.native, original.native)

    def test_diff_requires_the_same_native_identity_for_shared_numbers(self):
        text = 'App: Test.\nTree: full\n0 window Test\n\t1 text Before\n\t2 text ' + 'Other content ' * 100
        previous = dict(text=text, refs={'0': 'window', '1': 'old', '2': 'other'})
        for text_change in (False, True):
            current = dict(text=text.replace('Before', 'After') if text_change else text,
                           refs={'0': 'window', '1': 'new', '2': 'other'})
            self.assertEqual(snapshot_diff(previous, current), current['text'])
        current = dict(text=text.replace('Before', 'After'), refs=previous['refs'])
        self.assertIn('Tree: diff', snapshot_diff(previous, current))
        self.assertIn('Tree: unchanged', snapshot_diff(previous, previous))


if __name__ == '__main__':
    unittest.main()
