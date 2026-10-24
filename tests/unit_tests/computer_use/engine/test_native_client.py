"""Public Computer/App/Element through production v5 JSONL and execution code."""
import asyncio
import gc
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
import weakref
from unittest.mock import patch

from computer_use import App, Computer, Element
from computer_use.client import images
from computer_use.client.connection import Connection
from computer_use.client.native import NativeController
from computer_use.client.observation_data import observation_data
from computer_use.errors import ComputerUseError

ROOT = Path(__file__).resolve().parent


class NativeClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='native-client-')
        self.journal = Path(self.temp.name) / 'wire.jsonl'
        self.connections = []
        self.delivery_required = images._DELIVERY_REQUIRED
        images._DELIVERY_REQUIRED = False

    async def asyncTearDown(self):
        images._DELIVERY_REQUIRED = self.delivery_required
        for connection in self.connections:
            connection.close()
            await connection.wait_closed()
        self.temp.cleanup()

    def computer(self, mode='observation-sdk'):
        driver = dict(command=sys.executable,
            args=[str(ROOT / 'fixtures/v5-driver.py'), mode, str(self.journal)], timeoutMs=3000)
        with patch('computer_use.client.native.Connection',
                   side_effect=lambda invalidate, **options: Connection(invalidate, driver, **options)):
            computer = Computer()
        connection = computer._controller.transport
        self.connections.append(connection)
        return computer

    async def test_default_constructor_uses_v5_and_stays_lazy(self):
        from computer_use.client import computer as public_handles
        self.assertFalse(hasattr(public_handles, 'Controller'))
        computer = Computer()
        self.assertIsInstance(computer._controller, NativeController)
        self.assertEqual(computer._controller.transport.protocol_version, 5)
        self.assertIsNone(computer._controller.transport._child)
        self.connections.append(computer._controller.transport)

    def test_default_wire_never_imports_legacy_adapters_or_renderer(self):
        code = f'''import asyncio, sys
class NoLegacy:
    def find_spec(self, name, path=None, target=None):
        if name in {{"legacy_client", "legacy_observation_store",
                     "computer_use.client.observation_store", "computer_use.client.results"}}:
            raise AssertionError("Legacy module imported by native session: " + name)
sys.meta_path.insert(0, NoLegacy())
from computer_use import Computer
async def main():
    c = Computer()
    connection = c._controller.transport
    connection.driver = {{"command": sys.executable,
        "args": [{str(ROOT / 'fixtures/v5-driver.py')!r}, "directory", {str(self.journal)!r}],
        "timeoutMs": 3000}}
    try:
        await c.listApps()
        assert connection.protocol_version == 5
    finally:
        connection.close()
        await connection.wait_closed()
asyncio.run(main())
'''
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([row['method'] for row in self.requests()], ['hello', 'begin', 'inspect', 'finish'])

    def requests(self):
        if not self.journal.exists():
            return []
        return [value for line in self.journal.read_text().splitlines() if 'method' in (value := json.loads(line))]

    def writes(self):
        return [r for r in self.requests() if r['method'] == 'begin' and r['params']['mode'] == 'write']

    async def observe(self, mode='observation-sdk'):
        computer = self.computer(mode)
        app = computer.app('Native fixture')
        state = await app.read()
        return computer, app, state

    async def test_same_public_classes_and_lazy_calls_use_only_v5(self):
        computer = self.computer()
        pending = computer.listApps()
        await asyncio.sleep(0)
        self.assertFalse(self.requests())
        apps = await pending
        self.assertEqual((apps[0].id, apps[0].isRunning), ('running:app', True))
        app = computer.app(apps[0].id)
        state = await app.read()
        element = app.element('1')
        self.assertIsInstance(computer, Computer)
        self.assertIsInstance(app, App)
        self.assertIsInstance(element, Element)
        self.assertIn('Input', state.text)
        self.assertIn('Apply', state.text)
        self.assertEqual(self.requests()[0]['version'], 5)
        self.assertLessEqual({r['method'] for r in self.requests()}, {'hello', 'begin', 'inspect', 'capture', 'finish'})

    async def test_observation_details_are_immutable_captured_data_not_another_native_read(self):
        computer, app, observation = await self.observe()
        before = self.requests()
        details = observation.details
        self.assertIn('Native actions:', details)
        self.assertNotIn('Native actions:', observation.text)
        self.assertEqual(repr(observation), observation.text)
        self.assertEqual(observation['details'], details)
        self.assertEqual(self.requests(), before)
        with self.assertRaises(AttributeError):
            observation.details = 'changed'
        await app.read()
        computer._controller.transport.close()
        await computer._controller.transport.wait_closed()
        before = self.requests()
        self.assertEqual(observation.details, details)
        self.assertEqual(self.requests(), before)
        self.assertFalse(self.writes())

    async def test_details_render_keeps_observation_scope_for_tool_delivery(self):
        delivered = []
        with patch('computer_use.client.native.observation_completed', side_effect=delivered.append):
            _, app, _ = await self.observe()
            await app.element('1').read()
        self.assertEqual([event['view'] for event in delivered], ['window', 'subtree'])
        self.assertTrue(all(isinstance(event['state'].details, str) for event in delivered))

    async def test_read_restarts_numbers_and_saved_elements_keep_native_bindings(self):
        computer, app, observation = await self.observe()
        field, button = app.element('1'), app.element('2')
        old_details = observation.details
        subtree = await button.read()
        self.assertIn('0 button', subtree.text)
        self.assertEqual(set(observation_data(subtree)['refs']), {'0'})
        self.assertEqual(computer._controller.state(app).refs['0']['binding'].node, 'button')
        for expired in ('1', '2'):
            with self.assertRaises(ComputerUseError):
                app.element(expired)
        latest_button = app.element('0')
        await field.typeText('saved field', {'replace': True})
        await button.click()
        await latest_button.performAction('AXPress')
        self.assertEqual([r['params']['target']['node'] for r in self.writes()], ['field', 'button', 'button'])
        await app.read()
        self.assertEqual((await app.element('1').readText()).text, 'saved field')
        await latest_button.click()
        self.assertEqual(self.writes()[-1]['params']['target']['node'], 'button', 'Old 0 must not become the window')
        self.assertEqual(observation.details, old_details)

    async def test_private_query_does_not_replace_public_numbers(self):
        computer, app, _ = await self.observe()
        await app.element('2').read()
        mapping = dict(computer._controller.state(app).refs)
        page = await app.find([{'role': 'textField', 'name': 'Input'}]).readText()
        self.assertEqual(page.text, 'A🙂e\u0301\r\nZ')
        self.assertIn('Captured text: query result', repr(page))
        self.assertEqual(dict(computer._controller.state(app).refs), mapping)
        await app.element('0').click()
        self.assertEqual(self.writes()[-1]['params']['target']['node'], 'button')

    async def test_separate_apps_number_independently_in_one_native_session(self):
        computer, app, _ = await self.observe()
        await app.element('2').read()
        other = computer.app('Native fixture')
        await other.read()
        self.assertEqual(computer._controller.state(app).refs['0']['binding'].node, 'button')
        self.assertEqual(computer._controller.state(other).refs['0']['binding'].node, 'node')
        self.assertEqual(set(computer._controller.state(other).refs), {'0', '1', '2'})

    async def test_failed_first_read_keeps_launched_application_without_relaunch(self):
        computer = self.computer('observation-launch-directory-once')
        app = computer.app('Native fixture')
        with self.assertRaises(ComputerUseError) as caught:
            await app.read()
        self.assertEqual(caught.exception.effect, 'possible')
        self.assertIn('Directory read failed', caught.exception.detail)
        state = await app.read()
        self.assertIn('Native fixture', state.text)
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(sum(r['params'].get('query', {}).get('kind') == 'applications' for r in self.requests()), 1)
        self.assertEqual(computer._controller.state(app).selection.application, 'running:launched')

    async def test_first_window_closing_during_read_never_selects_another_root_on_retry(self):
        app = self.computer('observation-launch-root-gone').app('Native fixture')
        with self.assertRaises(ComputerUseError):
            await app.read()
        with self.assertRaises(ComputerUseError) as caught:
            await app.read()
        self.assertEqual(caught.exception.code, 'stale_target')
        self.assertEqual(caught.exception.effect, 'none')
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(sum(r['params'].get('query', {}).get('kind') == 'applications' for r in self.requests()), 1)

    async def test_cancelled_first_observation_keeps_window_selection(self):
        computer = self.computer()
        app = computer.app('Native fixture')
        with patch.object(computer._controller.reader, '_observe', side_effect=asyncio.CancelledError()), \
                self.assertRaises(asyncio.CancelledError):
            await app.read()
        await app.read()
        self.assertEqual(computer._controller.state(app).selection.root.root_window, 'root')
        self.assertEqual(sum(r['params'].get('query', {}).get('kind') == 'applications' for r in self.requests()), 1)

    async def test_launched_application_binding_cannot_survive_driver_replacement(self):
        computer = self.computer('observation-launch-directory-once')
        app = computer.app('Native fixture')
        with self.assertRaises(ComputerUseError):
            await app.read()
        transport = computer._controller.transport
        transport.close()
        await transport.wait_closed()
        count = len(self.requests())
        with self.assertRaises(ComputerUseError) as caught:
            await app.read()
        self.assertEqual(caught.exception.code, 'stale_target')
        self.assertEqual(caught.exception.effect, 'none')
        self.assertEqual(len(self.requests()), count)

    async def test_element_replace_insert_submit_and_click_do_not_read_after_action(self):
        _, app, _ = await self.observe()
        field, button = app.element('1'), app.element('2')
        mark = len(self.requests())
        self.assertEqual((await field.typeText('hello', {'replace': True})).status, 'dispatched')
        await field.typeText('!')
        await field.pressKey('Return')
        await button.click()
        await button.openMenu()
        await button.performAction('AXShowMenu')
        writes = self.writes()
        self.assertEqual([r['params']['intent']['kind'] for r in writes], ['replace', 'insert', 'key', 'click', 'openMenu', 'action'])
        self.assertEqual([r['params']['target']['node'] for r in writes], ['field'] * 3 + ['button'] * 3)
        self.assertTrue(all(r['params']['mode'] == 'write' for r in self.requests()[mark:] if r['method'] == 'begin'))
        self.assertNotIn('capture', [r['method'] for r in self.requests()[mark:]])
        await app.read()
        self.assertEqual((await app.element('1').readText()).text, 'hello!')

    async def test_projection_failure_preserves_image_and_original_input_binding(self):
        computer = self.computer()
        app = computer.app('Native fixture')
        with patch('computer_use.observation.native_facts.compose_controls', side_effect=ValueError('projection fixture')):
            state = await app.read()
        self.assertIn('Presentation fallback', state.text)
        self.assertIn('"Input"', state.text)
        self.assertIsNotNone(state.screenshot)
        self.assertEqual(set(observation_data(state)['refs']), {'0', '1', '2'})
        self.assertFalse(self.writes(), 'Recovering a view must not perform input')
        field = app.element('1')
        self.assertEqual((await field.readText()).text, 'A🙂e\u0301\r\nZ')
        await field.typeText('from uncompressed view', {'replace': True})
        self.assertEqual(self.writes()[-1]['params']['target']['node'], 'field')
        self.assertNotIn('capture', [r['method'] for r in self.requests()
            if r['id'] > self.writes()[-1]['id']])
        await app.read()
        self.assertEqual((await app.element('1').readText()).text, 'from uncompressed view')

    async def test_query_write_resolves_and_dispatches_in_the_same_operation(self):
        _, app, _ = await self.observe()
        mark = len(self.requests())
        query = app.find([{'role': 'textField', 'name': 'Input'}])
        pending = query.typeText('from query', {'replace': True})
        await asyncio.sleep(0)
        self.assertEqual(len(self.requests()), mark)
        await pending
        calls = self.requests()[mark:]
        self.assertEqual([r['method'] for r in calls].count('begin'), 1)
        self.assertEqual(calls[0]['params']['mode'], 'write')
        self.assertIsNone(calls[0]['params']['target']['node'])
        inspect = next(r for r in calls if r['params'].get('query', {}).get('kind') == 'inputContext')
        self.assertEqual(inspect['params']['query']['within'], ['node'])
        self.assertEqual(inspect['params']['target']['node'], 'field')
        self.assertEqual(calls[-1]['method'], 'finish')
        self.assertNotIn('findElement', [r['method'] for r in calls])

    async def test_short_read_text_is_immutable_and_pages_without_rpc(self):
        _, app, _ = await self.observe()
        field = app.element('1')
        mark = len(self.requests())
        first = await field.readText({'length': 2})
        second = await field.readText({'offset': first.next, 'length': 2})
        self.assertEqual((first.text, first.next, first.total), ('A', 1, 8))
        self.assertEqual((second.text, second.next), ('🙂', 3))
        self.assertEqual(len(self.requests()), mark)
        with self.assertRaises(ComputerUseError) as caught:
            await field.readText({'offset': 2})
        self.assertEqual((caught.exception.code, caught.exception.effect), ('invalid_request', 'none'))
        await field.typeText('changed', {'replace': True})
        await app.read()
        self.assertEqual((await field.readText()).text, 'A🙂e\u0301\r\nZ')
        self.assertEqual((await app.element('1').readText()).text, 'changed')

    async def test_query_read_text_captures_selected_value_without_input(self):
        _, app, _ = await self.observe()
        query = app.find([{'role': 'textField', 'name': 'Input'}])
        self.assertEqual((await query.readText()).text, 'A🙂e\u0301\r\nZ')
        self.assertFalse(self.writes())

    async def test_multi_window_catalogue_allows_explicit_selection(self):
        _, app, catalogue = await self.observe('observation-sdk-multiple')
        self.assertIn('0 "Native fixture"', catalogue.text)
        self.assertIn('1 "Second document"', catalogue.text)
        self.assertIsNone(catalogue.screenshot)
        self.assertIsNotNone(catalogue.overview)
        self.assertEqual([r['params']['target']['surface'] for r in self.requests() if r['method'] == 'capture'],
                         ['surface', 'second-surface'])
        self.assertFalse(self.writes())
        second = app.window('1')
        await second.read()
        await second.element('1').typeText('second', {'replace': True})
        self.assertEqual(self.writes()[-1]['params']['target']['rootWindow'], 'second-root')
        self.assertEqual(self.writes()[-1]['params']['target']['node'], 'field-second')
        self.assertIs(second.window('1'), second)
        with self.assertRaises(ComputerUseError):
            await app.pressKey('Return')

    async def test_element_subtree_read_preserves_window_coordinate_binding(self):
        _, app, state = await self.observe()
        subtree = await app.element('1').read()
        self.assertIn('Input', subtree.text)
        self.assertIsNone(subtree.screenshot)
        await app.click(0.5, 0.5)
        write = self.writes()[-1]['params']
        self.assertEqual(write['target']['capture'], 'image-1')
        self.assertIsNone(write['target']['node'])

    async def test_coordinates_need_the_current_delivered_image(self):
        images._DELIVERY_REQUIRED = True
        _, app, first = await self.observe()
        with self.assertRaises(ComputerUseError):
            await app.click(0.5, 0.5)
        images.image_delivered(first.screenshot)
        await app.click(0.5, 0.5)
        second = await app.read()
        images.image_delivered(first.screenshot)
        with self.assertRaises(ComputerUseError):
            await app.click(0.5, 0.5)
        images.image_delivered(second.screenshot)
        await app.click(0.5, 0.5)
        self.assertEqual([r['params']['target']['capture'] for r in self.writes()], ['image-1', 'image-2'])
        with self.assertRaises(ComputerUseError):
            await app.click(4, 0)

    async def test_closed_session_does_not_reconnect_old_elements(self):
        _, app, _ = await self.observe()
        field = app.element('1')
        self.connections[-1].close()
        await self.connections[-1].wait_closed()
        mark = len(self.requests())
        with self.assertRaises(ComputerUseError) as caught:
            await field.typeText('do not send')
        self.assertEqual((caught.exception.code, caught.exception.effect), ('stale_target', 'none'))
        self.assertEqual(len(self.requests()), mark)

    async def test_uncertain_input_is_not_retried_or_followed_by_observation(self):
        _, app, _ = await self.observe('observation-sdk-uncertain')
        mark = len(self.requests())
        with self.assertRaises(ComputerUseError) as caught:
            await app.element('1').typeText('only once', {'replace': True})
        self.assertEqual(caught.exception.effect, 'possible')
        calls = self.requests()[mark:]
        self.assertEqual([r['method'] for r in calls].count('execute'), 1)
        self.assertEqual([r['method'] for r in calls].count('begin'), 1)
        self.assertNotIn('capture', [r['method'] for r in calls])

    async def test_missing_global_menu_capability_searches_only_the_window_without_legacy_fallback(self):
        computer, app, _ = await self.observe()
        mark = len(self.requests())
        with self.assertRaises(ComputerUseError) as caught:
            await app.read({'menuPath': ['File']})
        self.assertEqual((caught.exception.code, caught.exception.effect), ('observation_incomplete', 'none'))
        self.assertIn('no readable native menu bar', caught.exception.detail)
        self.assertFalse(self.writes())
        self.assertFalse(any(r['params'].get('query', {}).get('kind') == 'roots' for r in self.requests()[mark:]))

    async def test_menu_summary_registers_separate_scopes_in_one_observation(self):
        _, app, observed = await self.observe('observation-sdk-menu')
        self.assertIn('Application menu (separate from the working window)', observed.text)
        state = app._controller.state(app)
        refs = {snapshot['binding'].node: ref for ref, snapshot in state.refs.items()}
        menu = app.element(refs['file'])
        self.assertEqual(state.refs[refs['file']]['binding'].surface, 'menu-surface')
        self.assertEqual(state.refs[refs['file']]['binding'].root_window, 'root')
        self.assertEqual(state.refs[refs['field']]['binding'].surface, 'surface')
        self.assertEqual(set(observation_data(observed)['refs']), set(state.refs))
        self.assertEqual(len(observation_data(observed)['roots']), 2)
        read = await menu.read()
        self.assertIn('Open…', read.text)
        self.assertIn('Application UI root', read.text)
        self.assertIsNone(read.screenshot)
        self.assertFalse(self.writes())
        # The final directory check covers collection of both independent roots.
        queries = [r['params']['query']['kind'] for r in self.requests() if r['method'] == 'inspect']
        self.assertEqual(queries[-2:], ['surfaces', 'nodes'])

    async def test_menu_path_leaf_find_and_subtree_preserve_window_input_and_capture(self):
        _, app, original = await self.observe('observation-sdk-menu')
        menu = await app.read({'menuPath': ['File', 'Recent']})
        self.assertIn('Document', menu.text)
        self.assertNotIn('Open…', menu.text)
        self.assertIsNone(menu.screenshot)
        found = await app.find([{'role': 'menuItem', 'name': 'Document'}]).read()
        self.assertIn('Document', found.text)
        state = app._controller.state(app)
        captured_menu = next(value for value in state.refs.values() if value['binding'].node == 'document')
        self.assertEqual(captured_menu['scope_kind'], 'applicationMenu')
        leaf = await app.read({'menuPath': ['File', 'Open…']})
        self.assertIn('Open…', leaf.text)
        self.assertIs(app._controller.state(app).observation.image.image, original.screenshot)
        self.assertFalse(self.writes())
        await app.pressKey('Return')
        await app.click(0.5, 0.5)
        self.assertEqual([r['params']['target']['surface'] for r in self.writes()], ['surface', 'surface'])
        self.assertEqual(self.writes()[-1]['params']['target']['capture'], 'image-1')
        await app.read()
        self.assertEqual(state.query_scope_kind, 'window')
        self.assertEqual(captured_menu['scope_kind'], 'applicationMenu')
        await app.find([{'role': 'textField', 'name': 'Input'}]).typeText('window', {'replace': True})
        dispatched = [r for r in self.requests() if r['method'] == 'execute'][-1]
        self.assertEqual(dispatched['params']['command']['target']['node'], 'field')

    async def test_windowless_menu_is_readable_and_keeps_the_running_application_identity(self):
        _, app, observed = await self.observe('observation-sdk-menu-windowless')
        self.assertIn('No working window is bound', observed.text)
        self.assertIsNone(observed.screenshot)
        self.assertIsNone(app._controller.state(app).selection)
        await app.read({'menuPath': ['File']})
        found = await app.find([{'role': 'menuItem', 'name': 'Open…'}]).read()
        self.assertIn('Open…', found.text)
        scopes = [r['params']['target'] for r in self.requests() if r['method'] == 'begin' and r['params']['target']['surface']]
        self.assertTrue(scopes)
        self.assertTrue(all(t['rootWindow'] is None and t['surface'] == 'menu-surface' for t in scopes))
        lookups = [r for r in self.requests() if r['params'].get('query', {}).get('kind') == 'applications']
        self.assertEqual([r['params']['query'] for r in lookups], [{'kind': 'applications'}])
        with self.assertRaises(ComputerUseError):
            await app.pressKey('Return')
        self.assertFalse(self.writes())

    async def test_menu_lookup_reports_ambiguity_unknown_names_and_missing_paths_without_input(self):
        for suffix, path, code in [('-ambiguous', ['File'], 'ambiguous_target'),
                                  ('-unknown-title', ['File'], 'observation_incomplete'),
                                  ('', ['File', 'Missing'], 'element_not_found'),
                                  ('', ['File', 'Open…', 'Anything'], 'element_not_found')]:
            with self.subTest(suffix=suffix, path=path):
                app = self.computer('observation-sdk-menu' + suffix).app('Native fixture')
                with self.assertRaises(ComputerUseError) as caught:
                    await app.read({'menuPath': path})
                self.assertEqual((caught.exception.code, caught.exception.effect), (code, 'none'))
        self.assertFalse(self.writes())

    async def test_unavailable_menu_does_not_hide_the_window_or_publish_fake_menu_refs(self):
        _, app, observed = await self.observe('observation-sdk-menu-unavailable')
        self.assertIn('Input', observed.text)
        self.assertIn('menus: observation_incomplete', observed.text)
        self.assertNotIn('Application menu (', observed.text)
        self.assertIsNotNone(observed.screenshot)
        self.assertEqual(set(observation_data(observed)['refs']), {'0', '1', '2'})

    async def test_menu_collection_is_included_in_final_root_recheck(self):
        app = self.computer('observation-sdk-menu-root-closed').app('Native fixture')
        with self.assertRaises(ComputerUseError) as caught:
            await app.read()
        self.assertEqual(caught.exception.code, 'stale_target')
        self.assertFalse(app._controller.state(app).refs)
        self.assertFalse(self.writes())

    async def test_menu_root_contract_checks_application_and_does_not_enable_menu_writes(self):
        app = self.computer('observation-sdk-menu-wrong-app').app('Native fixture')
        with self.assertRaisesRegex(ValueError, 'application'):
            await app.read()
        _, app, _ = await self.observe('observation-sdk-menu')
        await app.read({'menuPath': ['File', 'Open…']})
        state = app._controller.state(app)
        ref = next(ref for ref, value in state.refs.items() if value['binding'].node == 'open')
        with self.assertRaises(ComputerUseError) as caught:
            await app.element(ref).click()
        self.assertEqual((caught.exception.code, caught.exception.effect), ('input_unavailable', 'none'))
        self.assertFalse(any(r['method'] == 'execute' for r in self.requests()))

    async def test_windowless_menu_binding_does_not_reconnect_after_session_end(self):
        _, app, _ = await self.observe('observation-sdk-menu-windowless')
        self.connections[-1].close()
        await self.connections[-1].wait_closed()
        mark = len(self.requests())
        with self.assertRaises(ComputerUseError) as caught:
            await app.read({'menuPath': ['File']})
        self.assertEqual((caught.exception.code, caught.exception.effect), ('stale_target', 'none'))
        self.assertEqual(len(self.requests()), mark)

    async def test_child_continuation_reads_one_batch_and_preserves_the_window_capture(self):
        _, app, original = await self.observe('observation-sdk-pages')
        self.assertIn('"offset": 2', original.text)
        element = app.element('0')
        mark = len(self.requests())
        page = await element.read({'offset': 2})
        self.assertIn('Item 129', page.text)
        self.assertNotIn('Item 130\n', page.text)
        self.assertIn('"offset": 130', page.text)
        self.assertNotIn('Item 129', original.text)
        self.assertIs(app._controller.state(app).observation.image.image, original.screenshot)
        calls = self.requests()[mark:]
        self.assertEqual([r['method'] for r in calls], ['begin', 'inspect', 'finish'])
        self.assertEqual(calls[1]['params']['query']['childrenPage'], {'cursor': 'children:2', 'limit': 128})
        self.assertEqual(calls[0]['params']['mode'], 'read')
        second = await element.read({'offset': 130})
        self.assertIn('Item 257', second.text)
        last = await element.read({'offset': 258})
        self.assertIn('Item 299', last.text)
        self.assertIn('end of direct children', last.text)
        self.assertNotIn('Continue:', last.text)
        self.assertEqual(self.writes(), [])

    async def test_explicit_first_page_and_new_element_use_their_observed_cursor(self):
        _, app, _ = await self.observe('observation-sdk-pages')
        element = app.element('0')
        first = await element.read()
        self.assertIn('"offset": 128', first.text)
        # Another App observation must not overwrite this Element's page source.
        await app.read()
        page = await element.read({'offset': 128})
        self.assertIn('Item 255', page.text)
        new_element = app.element('0')
        last = await new_element.read({'offset': 256})
        self.assertIn('Item 299', last.text)

    async def test_invalid_continuation_does_not_guess_an_offset_or_refind_a_query(self):
        _, app, _ = await self.observe('observation-sdk-pages')
        for element, offset in ((app.element('0'), 1), (app.element('1'), 128), (app.find(['Items']), 128)):
            mark = len(self.requests())
            with self.assertRaises(ComputerUseError) as caught:
                await element.read({'offset': offset})
            self.assertEqual((caught.exception.code, caught.exception.effect), ('snapshot_expired', 'none'))
            self.assertEqual(len(self.requests()), mark)

    async def test_changed_relation_remains_a_readable_gap_and_is_not_a_successful_empty_page(self):
        _, app, original = await self.observe('observation-sdk-pages-changed')
        element = app.element('0')
        page = await element.read({'offset': 2})
        self.assertIn('native relation changed', page.text)
        self.assertIn('Items', page.text)
        self.assertNotIn('Continue:', page.text)
        self.assertIn('Item 0', original.text)
        with self.assertRaises(ComputerUseError):
            await element.read({'offset': 2})
        self.assertEqual(self.writes(), [])

    async def test_expired_or_wrong_page_is_not_repaired_with_a_fresh_read(self):
        for mode, exception in (('expired', ComputerUseError), ('wrong-range', ValueError)):
            _, app, _ = await self.observe('observation-sdk-pages-' + mode)
            mark = len(self.requests())
            with self.assertRaises(exception):
                await app.element('0').read({'offset': 2})
            self.assertEqual([r['method'] for r in self.requests()[mark:]], ['begin', 'inspect', 'finish'])

    async def test_old_driver_never_receives_the_new_page_query(self):
        _, app, _ = await self.observe('observation-sdk-no-pages')
        mark = len(self.requests())
        with self.assertRaises(ComputerUseError) as caught:
            await app.element('1').read()
        self.assertEqual(caught.exception.code, 'mode_unsupported')
        self.assertEqual([r['method'] for r in self.requests()[mark:]], ['begin', 'finish'])

    async def test_image_callback_does_not_keep_its_own_pixels_alive(self):
        computer, app, state = await self.observe()
        pixels = weakref.ref(state.screenshot)
        del app, state
        gc.collect()
        self.assertIsNone(pixels())


if __name__ == '__main__':
    unittest.main()
