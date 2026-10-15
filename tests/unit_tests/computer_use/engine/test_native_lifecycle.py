import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from computer_use.execution.native import ApplicationBinding, Binding, Cleanup, Intent, OpenedOperation
from computer_use.execution.lifecycle import CallLifecycle


class CallLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.target = Binding('session', 'app', 'window', 'surface', 'node')
        self.events = []
        self.now = 0
        self.begin_error = None
        self.cleanup = Cleanup.COMPLETE
        self.clock = patch('computer_use.execution.lifecycle.time', SimpleNamespace(monotonic=lambda: self.now))
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.calls = CallLifecycle(self)

    async def connect(self, budget):
        self.events.append(('connect', budget))
        self.now += .1
        return self.target

    async def begin_read(self, target, budget):
        self.calls.check_begin('read', 'read', target)
        self.events.append(('begin', budget))
        if self.begin_error:
            raise self.begin_error
        self.now += .2
        return OpenedOperation('operation', target)

    async def begin(self, intent, target, budget):
        self.calls.check_begin(intent.kind, 'write', target)
        self.events.append(('write', intent.kind, budget))
        self.now += .2
        return OpenedOperation('write-operation', target)

    async def finish(self, operation):
        self.events.append(('finish', operation))
        # Cleanup can consume its own allowance without publishing a new read.
        self.now += 2
        return self.cleanup

    async def test_connect_collection_and_projection_share_budget_cleanup_has_own_allowance(self):
        async with self.calls.open(None, 1000) as read:
            self.assertEqual(read.target, self.target)
            self.assertEqual(self.events, [('connect', 1000), ('begin', 900)])
            self.assertLessEqual(read.remaining(), 700)
        self.assertEqual(self.events[-1], ('finish', 'operation'))
        self.assertFalse(self.calls.retired)

    async def test_late_python_projection_is_not_delivered_but_lease_is_finished(self):
        with self.assertRaises(TimeoutError):
            async with self.calls.open(self.target, 1000):
                self.now += 1
        self.assertEqual(self.events[-1], ('finish', 'operation'))
        self.assertFalse(self.calls.busy)
        self.assertFalse(self.calls.retired)

    async def test_cancelled_read_finishes_and_overlapping_read_never_begins(self):
        with self.assertRaises(asyncio.CancelledError):
            async with self.calls.open(self.target, 1000):
                with self.assertRaisesRegex(RuntimeError, 'busy'):
                    async with self.calls.open(self.target, 1000):
                        self.fail('overlap admitted')
                raise asyncio.CancelledError()
        self.assertEqual([call[0] for call in self.events], ['begin', 'finish'])
        self.assertFalse(self.calls.busy)

    async def test_unknown_begin_and_cleanup_retire_scope_without_hiding_primary_error(self):
        self.begin_error = RuntimeError('lost begin')
        with self.assertRaisesRegex(RuntimeError, 'lost begin'):
            async with self.calls.open(self.target, 1000):
                self.fail('unknown lease admitted')
        self.assertTrue(self.calls.retired)

        self.assertEqual([call[0] for call in self.events], ['begin'])
        self.begin_error = None
        self.cleanup = Cleanup.UNKNOWN
        self.calls = CallLifecycle(self)
        original = ValueError('collection failed')
        with self.assertRaises(ValueError) as caught:
            async with self.calls.open(self.target, 1000):
                raise original
        self.assertIs(caught.exception, original)
        self.assertIn('cleanup unconfirmed', original.__notes__[0])
        self.assertTrue(self.calls.retired)

    async def test_multiple_stages_use_one_budget_and_cannot_overlap(self):
        with self.calls.claim(5000) as remaining:
            async with self.calls.operation(None, remaining) as read:
                with self.assertRaisesRegex(RuntimeError, 'active, idle'):
                    async with self.calls.operation(self.target, remaining):
                        self.fail('overlapping native stage admitted')
                self.assertLessEqual(read.bounded(100).remaining(), 100)
            self.assertTrue(self.calls.busy)
            async with self.calls.operation(self.target, remaining):
                pass
        budgets = [call[1] for call in self.events if call[0] == 'begin']
        self.assertEqual(len(budgets), 2)
        self.assertLess(budgets[1], budgets[0] - 2000)
        self.assertFalse(self.calls.busy)

    async def test_budget_exhausted_before_begin_does_not_retire_port(self):
        with self.calls.claim(1000) as remaining:
            self.now += 2
            with self.assertRaises(TimeoutError):
                async with self.calls.operation(self.target, remaining):
                    self.fail('expired read admitted')
        self.assertFalse(self.events)
        self.assertFalse(self.calls.retired)

    async def test_read_and_write_stages_share_a_call_without_upgrading_native_read(self):
        with self.calls.claim(5000) as remaining:
            async with self.calls.operation(None, remaining):
                pass
            target = ApplicationBinding('session', 'observed-installation')
            async with self.calls.operation(target, remaining, intent=Intent('launch')) as stage:
                self.assertEqual(stage.target, target)
                self.assertEqual(stage.token, 'write-operation')
        self.assertEqual([e[0] for e in self.events], ['connect', 'begin', 'finish', 'write', 'finish'])
        self.assertLess(self.events[3][2], self.events[1][1] - 2000)

    async def test_another_task_cannot_borrow_an_active_calls_budget(self):
        with self.calls.claim(5000) as remaining:
            async def intruder():
                async with self.calls.operation(self.target, remaining):
                    self.fail('another task borrowed the claim')
            with self.assertRaisesRegex(RuntimeError, 'owning task'):
                await asyncio.create_task(intruder())
            self.assertFalse(self.events)
            async with self.calls.operation(self.target, remaining):
                pass
        self.assertFalse(self.calls.busy)
