"""The displayed hierarchy, not the source tree, must account for relations."""
import copy
import json
import re
import unittest
from unittest.mock import patch

from computer_use.observation import native_facts
from computer_use.observation.native_facts import DisplayReferences, normalize_facts
from .test_native_facts import batch, edge, node


def relation_records(result):
    records = {}
    properties, metadata = None, None
    def aliases_in(text):
        for group in text.split(', '):
            ends = group.split('–')
            yield from (str(ref) for ref in range(int(ends[0]), int(ends[-1]) + 1))
    for line in result['text'].splitlines():
        header = re.fullmatch(r'\tRelation (.+) (\{.+\}):', line)
        if header:
            properties, metadata = json.loads(header[1]), json.loads(header[2])
            if isinstance(properties, str):
                properties = [properties]
            continue
        if line.startswith('\t\t') and properties is not None and ' -> ' in line:
            for entry in line.strip().split('; '):
                aliases, targets = entry.split(' -> ', 1)
                value = dict(metadata, targets=json.loads(targets))
                if value.get('total') == 'target-count':
                    value['total'] = len(value['targets'])
                for ref in aliases_in(aliases):
                    for key in properties:
                        if (ref, key) in records:
                            raise AssertionError('relation printed twice')
                        records[(ref, key)] = value
            continue
        properties = None
        match = re.fullmatch(r'\t+([0-9, –]+) Relation ("(?:[^"\\]|\\.)*"): (.+)', line)
        if match is None:
            continue
        aliases, key, value = match.groups()
        for group in aliases.split(', '):
            ends = group.split('–')
            for ref in range(int(ends[0]), int(ends[-1]) + 1):
                identity = (str(ref), json.loads(key))
                if identity in records:
                    raise AssertionError('relation printed twice')
                records[identity] = json.loads(value)
    return records


class RelationAccountingTests(unittest.TestCase):
    def test_removed_layout_cannot_stand_in_for_the_published_parent(self):
        wire = batch([node('window', AXRole='AXWindow'), node('layout', AXRole='AXGroup'),
                      node('button', AXRole='AXButton', AXTitle='Save')],
                     [edge('window', 'layout'), edge('layout', 'button'),
                      edge('button', 'layout', property='AXParent')])
        snapshot = normalize_facts(wire, DisplayReferences('session'))
        result = snapshot.render()
        self.assertIn('0 window\n\t2 button', result['text'])
        records = relation_records(result)
        self.assertEqual(records[('2', 'AXParent')]['targets'], ['1'])
        self.assertEqual(records[('0', 'AXChildren')]['targets'], ['1'])
        self.assertEqual(records[('1', 'AXChildren')]['targets'], ['2'])
        self.assertEqual(result['refs'], {'0': 'window', '1': 'layout', '2': 'button'})

    def test_complete_relation_details_and_unknown_absence_remain_visible(self):
        wire = batch([node('root', AXRole='AXWindow'), node('child', AXRole='AXButton')],
                     [edge('root', 'child', detail='provider-specific completeness condition'),
                      edge('root', property='VendorRelation', state='absent', certainty='unknown', total=0,
                           detail='no value, uncertain scope')])
        snapshot = normalize_facts(wire, DisplayReferences('session'))
        result = snapshot.render()
        self.assertIn('provider-specific completeness condition', result['text'])
        record = relation_records(result)[('0', 'VendorRelation')]
        self.assertEqual((record['state'], record['certainty'], record['total']), ('absent', 'unknown', 0))
        self.assertEqual(record['detail'], 'no value, uncertain scope')

    def test_shared_duplicate_cyclic_and_unread_targets_keep_original_order(self):
        wire = batch([node('root', AXRole='AXWindow'), node('child', AXRole='AXGroup')],
                     [edge('root', 'child', 'unread', 'child'), edge('child', 'root')])
        wire['unread'] = ['unread']
        snapshot = normalize_facts(wire, DisplayReferences('session'))
        before = copy.deepcopy(snapshot.native)
        result = snapshot.render()
        records = relation_records(result)
        self.assertEqual(records[('0', 'AXChildren')]['targets'], ['1', '2', '1'])
        self.assertEqual(records[('1', 'AXChildren')]['targets'], ['0'])
        self.assertEqual(snapshot.native, before)

    def test_exact_visible_children_parent_and_window_relations_need_no_duplicate_edges(self):
        wire = batch([node('window', AXRole='AXWindow'), node('button', AXRole='AXButton', AXTitle='Save')],
                     [edge('window', 'button', total=1, offset=0), edge('button', total=0),
                      edge('button', 'window', property='AXParent'), edge('button', 'window', property='AXWindow')])
        result = normalize_facts(wire, DisplayReferences('session')).render()
        explicit = relation_records(result)
        self.assertNotIn(('0', 'AXChildren'), explicit)
        self.assertNotIn(('1', 'AXChildren'), explicit)
        self.assertNotIn(('1', 'AXParent'), explicit)
        self.assertNotIn(('1', 'AXWindow'), explicit)
        self.assertIn('0 window\n\t1 button', result['text'])
        sources = {(s['source'], s['property']): s['representation'] for s in result['relationSources']}
        self.assertEqual(sources[('button', 'AXParent')], 'containment')
        self.assertEqual(len(sources), len(wire['relations']))

    def test_a_field_failure_does_not_turn_complete_children_into_a_tree_gap(self):
        root = node('window', AXRole='AXWindow')
        root['requestedFields'].append('AXHelp')
        root['fieldStatus']['AXHelp'] = dict(state='timeout', detail='one property unavailable')
        wire = batch([root, node('button', AXRole='AXButton', AXTitle='Save')],
                     [edge('window', 'button'), edge('button')])
        result = normalize_facts(wire, DisplayReferences('session')).render()
        self.assertIn('AXHelp: timeout', result['text'])
        self.assertIn('1 button Save', result['text'])
        self.assertNotIn('Children: partial', result['text'])
        self.assertNotIn('Inspect subtree:', result['text'])
        self.assertNotIn(('0', 'AXChildren'), relation_records(result))

    def test_outline_nesting_cannot_replace_native_sibling_relationships(self):
        wire = batch([node('window', AXRole='AXWindow'), node('outline', AXRole='AXOutline'),
                      node('folder', AXRole='AXRow', AXTitle='Folder', AXExpanded=True, AXDisclosureLevel=0),
                      node('item', AXRole='AXRow', AXTitle='Item', AXDisclosureLevel=1)],
                     [edge('window', 'outline'), edge('outline', 'folder', 'item'), edge('folder'), edge('item'),
                      edge('item', 'outline', property='AXParent'), edge('item', 'folder', property='AXDisclosedByRow')])
        result = normalize_facts(wire, DisplayReferences('session')).render()
        records = relation_records(result)
        self.assertEqual(records[('1', 'AXChildren')]['targets'], ['2', '3'])
        self.assertEqual(records[('2', 'AXChildren')]['targets'], [])
        self.assertEqual(records[('3', 'AXParent')]['targets'], ['1'])
        self.assertEqual(records[('3', 'AXDisclosedByRow')]['targets'], ['2'])
        self.assertRegex(result['text'], r'\n\t\t\t3 row Item')

    def test_all_non_structural_metadata_can_be_recovered_from_grouped_output(self):
        for certainty in ('confirmed', 'inferred', 'unknown', 'conflicting'):
            with self.subTest(certainty=certainty):
                relation = edge('root', 'b', 'a', property='VendorRelation', state='partial',
                                certainty=certainty, offset=3, total=8, cursor='opaque:next',
                                detail='reason\n777 button\u2028still detail')
                wire = batch([node('root', AXRole='AXWindow'), node('a', AXRole='AXButton'),
                              node('b', AXRole='AXButton')], [relation])
                snapshot = normalize_facts(wire, DisplayReferences('session'))
                result = snapshot.render()
                self.assertNotIn('\n777 button', result['text'])
                explicit = relation_records(result)
                aliases = {token: ref for ref, token in snapshot.refs.items()}
                for source in wire['relations']:
                    expected = {k: v for k, v in source.items() if k not in ('source', 'property', 'targets')}
                    expected['targets'] = [aliases[token] for token in source['targets']]
                    self.assertEqual(explicit[(aliases[source['source']], source['property'])], expected)

    def test_fallback_has_one_actual_destination_per_original_relation(self):
        wire = batch([node('window', AXRole='AXWindow'), node('button', AXRole='AXButton')],
                     [edge('window', 'button'), edge('button', 'window', property='AXParent')])
        snapshot = normalize_facts(wire, DisplayReferences('session'))
        with patch.object(native_facts, 'render_tree', side_effect=ValueError('broken formatting')):
            result = snapshot.render()
        self.assertEqual({(s['source'], s['property']) for s in result['relationSources']},
                         {(s['source'], s['property']) for s in wire['relations']})
        self.assertTrue(all(s['representation'] == 'uncompressed' for s in result['relationSources']))
        self.assertIn('Relation "AXParent"', result['text'])

    def test_relation_names_cannot_break_related_root_headers_or_invent_nodes(self):
        property_name = 'Vendor\n777 button\u2028relation'
        wire = batch([node('root', AXRole='AXWindow'), node('related', AXRole='AXButton')],
                     [edge('root', 'related', property=property_name)])
        result = normalize_facts(wire, DisplayReferences('session')).render()
        self.assertNotIn('\n777 button', result['text'])
        self.assertEqual(relation_records(result)[('0', property_name)]['targets'], ['1'])

    def test_a_projection_cannot_borrow_nodes_from_another_native_root(self):
        wire = batch([node('first', AXRole='AXWindow'), node('second', AXRole='AXWindow')],
                     [edge('first'), edge('second')], roots=['first', 'second'])
        snapshot = normalize_facts(wire, DisplayReferences('session'))
        render = native_facts.render_tree
        def incorrect(root, context):
            result = render(root, context)
            if root['ref'] == '0':
                result['roots'].append(dict(ref='1', role='window', children=[]))
            return result
        with patch.object(native_facts, 'render_tree', side_effect=incorrect):
            result = snapshot.render()
        self.assertEqual(result['projections'][0]['presentation'], 'uncompressed')
        self.assertNotIn('presentation', result['projections'][1])
        self.assertEqual(result['refs'], {'0': 'first', '1': 'second'})

    def test_windows_and_linux_use_the_same_post_projection_relation_rules(self):
        for platform, role_key, group_role, window_role, button_role, name_key, children, parent in (
            ('windows', 'ControlType', 'ControlType.Group', 'ControlType.Window', 'ControlType.Button', 'Name',
             'RawViewChildren', 'RawViewParent'),
            ('linux', 'role', 'panel', 'frame', 'push button', 'name', 'children', 'parent'),
        ):
            with self.subTest(platform=platform):
                wire = batch([node('w', **{role_key: window_role}), node('g', **{role_key: group_role}),
                              node('b', **{role_key: button_role, name_key: 'Save'})],
                             [edge('w', 'g', property=children), edge('g', 'b', property=children),
                              edge('b', 'g', property=parent)])
                wire['platform'] = platform
                snapshot = normalize_facts(wire, DisplayReferences('session'))
                result = snapshot.render()
                self.assertEqual(relation_records(result)[('2', parent)]['targets'], ['1'])
                self.assertEqual(snapshot.native, wire)


if __name__ == '__main__':
    unittest.main()
