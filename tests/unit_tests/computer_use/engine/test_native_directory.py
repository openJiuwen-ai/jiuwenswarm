import unittest
from computer_use.errors import ComputerUseError
from computer_use.observation.directory import select_application, selected_window, select_surface
from computer_use.protocol.directory import decode_directory


def window(id, parent=None, modal=False, accessible=True):
    return dict(window=id, instance='app:'+id, surface='scope:'+id, node='node:'+id if accessible else None,
                title='Same title', kind='window', parent=parent, parentState='known' if parent else 'none',
                modal=modal, nativeWindow=True, accessibility=accessible)


def directory(*surfaces, reasons=()):
    return dict(kind='surfaces', session='session', platform='macos', application='running:1',
                observed=dict(id='obs', startedAt=1, completedAt=2), stopReasons=list(reasons), surfaces=list(surfaces),
                provider=dict(status='alreadyEnabled', attribute=None, writeStatus=None))


class NativeDirectoryTests(unittest.TestCase):
    def test_running_window_catalogue_and_desktop_selectors(self):
        app = dict(id='application:1', displayName='Editor', isRunning=True, path=None, identifier=None,
                   windows=[dict(selector='instance:a', window='a'), dict(selector='instance:b', window='b')])
        value = dict(kind='applications', session='session', platform='windows', scope='runningWindows',
                     observed=dict(id='obs', startedAt=1, completedAt=2), stopReasons=[], applications=[app])
        parsed = decode_directory(value)
        self.assertEqual(select_application(parsed, 'editor'), app)
        self.assertEqual(selected_window(select_application(parsed, 'instance:b'), 'instance:b'), 'b')
        with self.assertRaises(ComputerUseError) as caught:
            select_application(parsed, 'not installed')
        self.assertEqual(caught.exception.code, 'element_not_found')
        self.assertIn('does not discover or launch', caught.exception.detail)
        parsed['stopReasons'] = ['UIA supplement timed out']
        self.assertEqual(select_application(parsed, 'instance:a'), app)
        with self.assertRaises(ComputerUseError) as caught:
            select_application(parsed, 'editor')
        self.assertEqual(caught.exception.code, 'observation_incomplete')
        for change in (dict(windows=app['windows'] * 2), dict(isRunning=False),
                       dict(windows=[dict(selector=app['id'], window='a')])):
            with self.subTest(change=change), self.assertRaises(ValueError):
                decode_directory({**value, 'applications': [{**app, **change}]})
        with self.assertRaises(ValueError):
            decode_directory({**value, 'applications': [app, {**app, 'id': 'application:2'}]})

    def test_application_roots_have_independent_scope_and_no_fabricated_window(self):
        root = dict(kind='menuBar', instance='app-scope', surface='menu-scope', node='menu-root')
        value = dict(kind='roots', session='s', platform='macos', application='running:1',
                     observed=dict(id='roots', startedAt=1, completedAt=2), roots=[root], stopReasons=[])
        parsed = decode_directory(value)
        self.assertEqual(parsed['roots'], [root])
        parsed['roots'][0]['node'] = 'changed'
        self.assertEqual(value['roots'][0]['node'], 'menu-root')
        for duplicate in ({**root, 'node': 'other'}, {**root, 'surface': 'other'}):
            with self.assertRaises(ValueError):
                decode_directory({**value, 'roots': [root, duplicate]})
        with self.assertRaises(ValueError):
            decode_directory({**value, 'roots': [{**root, 'window': 'invented'}]})
        self.assertEqual(decode_directory({**value, 'roots': [], 'stopReasons': ['provider timeout']})['roots'], [])

    def test_application_identity_resolution_does_not_choose_between_processes(self):
        app = dict(id='instance:1', displayName='Editor', isRunning=True, path='/Applications/Editor.app', identifier='local.editor')
        value = dict(kind='applications', session='session', platform='macos',
                     observed=dict(id='obs', startedAt=1, completedAt=2), stopReasons=[], applications=[app])
        for selector in ('instance:1', 'Editor', 'editor', '/Applications/Editor.app', 'local.editor'):
            self.assertEqual(select_application(decode_directory(value), selector), app)
        value['applications'].append({**app, 'id': 'instance:2'})
        with self.assertRaises(ComputerUseError) as error:
            select_application(value, 'local.editor')
        self.assertEqual(error.exception.code, 'ambiguous_target')
        self.assertNotIn('candidates=', error.exception.detail)
        self.assertEqual(select_application(value, 'instance:1'), app)
        value['applications'] = [{**app, 'id': 'local.editor', 'isRunning': False}]
        self.assertFalse(select_application(value, 'Editor')['isRunning'])

    def test_same_title_documents_require_selection_and_keep_root(self):
        value = decode_directory(directory(window('a'), window('b'), window('dialog', 'b', True)))
        with self.assertRaises(ComputerUseError) as error:
            select_surface(value)
        self.assertEqual(error.exception.code, 'ambiguous_target')
        chosen = select_surface(value, 'b')
        self.assertEqual((chosen.instance, chosen.root_window, chosen.surface, chosen.node),
                         ('app:b', 'b', 'scope:dialog', 'node:dialog'))
        self.assertEqual(select_surface(value, 'a').surface, 'scope:a')

    def test_visual_window_is_preserved_without_inventing_a_node(self):
        chosen = select_surface(decode_directory(directory(window('visual', accessible=False))))
        self.assertEqual(chosen.surface, 'scope:visual')
        self.assertIsNone(chosen.node)

    def test_unknown_ownership_or_coverage_never_proves_unique_window(self):
        unknown = window('a')
        unknown['parentState'] = 'unknown'
        for value in (directory(window('a', 'unread')), directory(window('a'), reasons=['AX failed']),
                      directory(window('a'), window('cycle', 'cycle')), directory(unknown)):
            with self.subTest(value=value):
                parsed = decode_directory(value)
                with self.assertRaises(ComputerUseError) as error:
                    select_surface(parsed)
                self.assertEqual(error.exception.code, 'observation_incomplete')
                self.assertEqual(select_surface(parsed, 'a').root_window, 'a')

    def test_ambiguous_modal_and_closed_root_do_not_retarget(self):
        value = directory(window('a'), window('one', 'a', True), window('two', 'a', True))
        with self.assertRaises(ComputerUseError) as error:
            select_surface(value, 'a')
        self.assertEqual(error.exception.code, 'ambiguous_target')
        with self.assertRaises(ComputerUseError) as error:
            select_surface(directory(window('b')), 'a')
        self.assertEqual(error.exception.code, 'stale_target')

    def test_metadata_and_bindings_are_validated_without_dropping_visual_entries(self):
        for modify in (lambda v: v['surfaces'].append(v['surfaces'][0]),
                       lambda v: v['surfaces'][0].update(accessibility=False),
                       lambda v: v['observed'].update(completedAt=0),
                       lambda v: v.update(extra=True)):
            value = directory(window('a'))
            modify(value)
            with self.assertRaises(ValueError):
                decode_directory(value)
        original = directory(window('a'))
        decoded = decode_directory(original)
        decoded['surfaces'][0]['title'] = 'changed'
        self.assertEqual(original['surfaces'][0]['title'], 'Same title')


if __name__ == '__main__':
    unittest.main()
