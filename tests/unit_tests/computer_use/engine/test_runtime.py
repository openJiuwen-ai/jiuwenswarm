import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from computer_use import Computer
from computer_use.client import images
from computer_use.client.host import (
    set_action_handler, set_observation_handler, set_execution_guard,
    set_diagnostic_handler, set_text_page_handler,
)
from computer_use.errors import ComputerUseError
from computer_use.mcp.runtime import Runtime
from computer_use.mcp.session import ExecutionSession
from computer_use.protocol.session import decode_hello
from computer_use.client.connection import Connection

FIXTURE = Path(__file__).parent / "fixtures/v5-driver.py"


def text(result):
    if not isinstance(result, dict):
        result = result.model_dump()
    return '\n'.join(block['text'] for block in result['content'] if block['type'] == 'text')


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cua-slim-runtime-')
        self.journal = Path(self.temp.name) / 'wire.jsonl'
        self.output = []
        self.delivery = images._DELIVERY_REQUIRED
        def factory():
            computer = Computer()
            computer._controller.transport.driver = dict(
                command=sys.executable,
                args=[str(FIXTURE), 'observation-sdk', str(self.journal)],
                timeoutMs=3000,
            )
            return computer
        self.runtime = Runtime(self.output.append, factory)
        self.id = 0

    async def asyncTearDown(self):
        await self.runtime.close()
        for setter in (set_action_handler, set_observation_handler, set_execution_guard,
                       set_diagnostic_handler, set_text_page_handler):
            setter(None)
        images._DELIVERY_REQUIRED = self.delivery
        self.temp.cleanup()

    async def runCode(self, code, **history):
        self.id += 1
        policy = dict(resetText=False, resetImages=False, incremental=False)
        policy.update(history)
        return await self.runtime.execute(dict(
            code=code, executionId=self.id,
            history=policy,
        ))

    def requests(self):
        if not self.journal.exists():
            return []
        return [row for line in self.journal.read_text().splitlines()
                if 'method' in (row := json.loads(line))]

    async def test_persistent_namespace_and_observation_receipt_deduplication(self):
        first = await self.runCode('app=computer.app("Native fixture")\nstate=await app.read()\nprint(state)')
        self.assertNotIn('isError', first)
        self.assertEqual(text(first).count('Tree: full'), 1)
        result = await self.runCode('receipt=await app.element("1").typeText("hello")\nprint(receipt)')
        self.assertNotIn('isError', result)
        self.assertEqual(text(result).count('Action [dispatched]'), 1)
        self.assertNotIn('Tree:', text(result))
        earlier = await self.runCode('print(receipt)')
        self.assertIn('Earlier action [dispatched]', text(earlier))

    async def test_full_and_retained_history_diff_still_work(self):
        first = await self.runCode('app=computer.app("Native fixture")\nawait app.read()', incremental=True)
        second = await self.runCode('await app.read()', incremental=True)
        self.assertIn('Tree: full', text(first))
        self.assertIn('Tree: unchanged', text(second))
        full = await self.runCode('await app.read({"full":True})', incremental=True)
        self.assertIn('Tree: full', text(full))

    async def test_image_must_reach_model_before_coordinate_input(self):
        first = await self.runCode('app=computer.app("Native fixture")\nstate=await app.read()\nprintImage(state.screenshot)\nawait app.click(0,0)')
        self.assertTrue(first.get('isError'))
        self.assertIn('has not been returned to the model', text(first))
        self.assertEqual(sum(block['type']=='image' for block in first['content']), 1)
        self.assertFalse(any(row['method']=='begin' and row['params']['mode']=='write'
                             for row in self.requests()))

    async def test_unawaited_work_drains_and_old_callbacks_cannot_send_input(self):
        result = await self.runCode('import asyncio\npending=asyncio.create_task(computer.listApps())\nawait asyncio.sleep(0)')
        self.assertTrue(result.get('isError'))
        self.assertIn('not awaited', text(result))
        self.assertTrue(self.runtime.task_namespace['pending'].done())
        count = len(self.requests())
        await self.runCode('import asyncio\nasync def later():\n    await asyncio.sleep(0.03)\n    return await computer.listApps()\nlate=asyncio.create_task(later())')
        with self.assertRaisesRegex(RuntimeError, 'ended tool execution'):
            await self.runtime.task_namespace['late']
        self.assertEqual(len(self.requests()), count)

    async def test_partial_output_and_minimal_namespace(self):
        result = await self.runCode('value=123\nprint("kept")\nraise ValueError("stop")')
        self.assertTrue(result.get('isError'))
        self.assertIn('kept', text(result))
        self.assertIn('Earlier statements may have executed', text(result))
        self.assertEqual(text(await self.runCode('print(value)')), '123')
        self.assertFalse({'libraryRoot', 'loadPackage', 'learningModule', 'decisionModule'}
                         & self.runtime.task_namespace.keys())


@unittest.skipIf(sys.version_info < (3, 12), "The MCP execution worker requires Python 3.12 or newer")
class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cua-slim-session-')
        self.previous = os.environ.get('COMPUTER_USE_LOG_DIR')
        os.environ['COMPUTER_USE_LOG_DIR'] = self.temp.name
        self.session = ExecutionSession(800)

    async def asyncTearDown(self):
        await self.session.close()
        if self.previous is None:
            os.environ.pop('COMPUTER_USE_LOG_DIR', None)
        else:
            os.environ['COMPUTER_USE_LOG_DIR'] = self.previous
        self.temp.cleanup()

    async def test_worker_persists_and_reset_clears_state(self):
        self.assertEqual(text(await self.session.execute('answer=40\nprint(answer)')), '40')
        second = await self.session.execute('answer+=2\nprint(answer)', title='保留状态')
        self.assertEqual(text(second), '42')
        self.assertTrue(Path(second['_meta']['computer-use']['logPath']).exists())
        self.assertEqual(second['_meta']['computer-use']['title'], '保留状态')
        self.assertNotIn('isError', await self.session.reset())
        self.assertEqual(text(await self.session.execute('print("answer" in globals())')), 'False')

    async def test_raw_stdio_cannot_pollute_mcp_results(self):
        result = await self.session.execute('import builtins,sys\nsys.stdout.write("raw")\nsys.stderr.write("raw")\nbuiltins.print("package",7,sep="/")\nprint("done")')
        self.assertEqual(text(result), 'package/7\ndone')

    async def test_watchdog_stops_infinite_code_without_replaying(self):
        result = await self.session.execute('print("before loop")\nwhile True: pass')
        self.assertTrue(result.get('isError'))
        self.assertIn('before loop', text(result))
        self.assertIn('without native command progress', text(result))
        self.assertEqual(text(await self.session.execute('print("alive")')), 'alive')

    async def test_overlap_refused_and_cancellation_retires_worker(self):
        cancel = asyncio.Event()
        pending = self.session.execute('import asyncio\nawait asyncio.sleep(10)', signal=cancel)
        await asyncio.sleep(0.1)
        self.assertIn('Another call is running', text(await self.session.execute('print("unexpected")')))
        cancel.set()
        self.assertIn('Call cancelled', text(await pending))
        self.assertEqual(text(await self.session.execute('print("asyncio" in globals())')), 'False')

    async def test_removed_extension_metadata_is_rejected_before_execution(self):
        params = StdioServerParameters(command=sys.executable, args=['-m', 'computer_use.mcp'],
                                       env={'PYTHONPATH':os.environ['PYTHONPATH']})
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                initialized = await client.initialize()
                advertised = initialized.capabilities.experimental
                for key in ('computer-use-learning', 'computer-use/decision', 'computer-use-experience'):
                    self.assertNotIn(key, advertised)
                    result = await client.call_tool('computer_use', {'code':'marker=True'}, meta={key:{}})
                    self.assertTrue(result.isError)
                    self.assertIn('No code was executed', text(result))
                result = await client.call_tool('computer_use', {'code':'print("marker" in globals())'})
                self.assertFalse(result.isError)
                self.assertEqual(text(result), 'False')


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_methods_fail_without_starting_driver(self):
        connection = Connection(lambda: None)
        try:
            for method in ('readApp','findElement','click','typeText','readDesktop'):
                with self.assertRaises(ComputerUseError) as captured:
                    await connection.request(method, {}, {'timeoutMs':100})
                self.assertEqual(captured.exception.effect, 'none')
                self.assertIsNone(connection._child)
        finally:
            connection.close()
            await connection.wait_closed()

    def test_old_versions_are_rejected_and_native_handshake_stays_strict(self):
        for version in (3, True, 5.0):
            with self.assertRaises(ValueError):
                Connection(lambda: None, protocol_version=version)
        reply = dict(version=5, session='native', id=0,
                     result=dict(platform='macos', desktopMode='shared', maxTimeoutMs=30000,
                                 capabilities=['cancel','requestDedup','nativeOperations']))
        self.assertEqual(decode_hello(reply,'shared')['session'], 'native')
        for change in ({'version':3}, {'id':1}):
            with self.assertRaises(ValueError):
                decode_hello({**reply, **change},'shared')
        with self.assertRaises(ValueError):
            decode_hello(reply,'exclusive')


if __name__ == '__main__':
    unittest.main()
