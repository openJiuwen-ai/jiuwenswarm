"""One call budget, stage ownership and cleanup state for native reads and writes."""
import asyncio
import time
from collections.abc import Callable
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, replace

from ..errors import ComputerUseError
from .native import Cleanup, OperationTarget


@dataclass(frozen=True)
class OperationLease:
    token: str
    target: OperationTarget
    remaining: Callable[[], int]

    def bounded(self, timeout_ms: int):
        """Reserve time for the other components without extending the call."""
        if type(timeout_ms) is not int or timeout_ms < 1:
            raise ValueError('Invalid observation component budget')
        deadline = time.monotonic() + timeout_ms / 1000

        def remaining():
            value = min(self.remaining(), int((deadline - time.monotonic()) * 1000))
            if value < 1:
                raise TimeoutError('Native observation component budget exhausted')
            return value

        return replace(self, remaining=remaining)


class CallLifecycle:

    def __init__(self, port):
        self.port = port
        self.busy = self.retired = False
        self._remaining = None
        self._active = False
        self._owner = None
        self._admission = None

    @contextmanager
    def claim(self, timeout_ms):
        if type(timeout_ms) is not int or not 1 <= timeout_ms <= 120000:
            raise ValueError('Invalid native call time budget')
        if self.retired:
            raise RuntimeError('Native session is retired; prior admission or cleanup was unconfirmed')
        if self.busy:
            raise RuntimeError('Another native call is busy')
        self.busy = True
        self._owner = asyncio.current_task()
        deadline = time.monotonic() + timeout_ms / 1000

        def remaining():
            value = int((deadline - time.monotonic()) * 1000)
            if value < 1:
                raise TimeoutError('Native call budget exhausted')
            return value

        self._remaining = remaining
        try:
            yield remaining
        finally:
            self._remaining = None
            self._owner = None
            self.busy = False

    def validate_claim(self, remaining):
        invalid_call_claim = (self.retired or self._active or remaining is not self._remaining or not self.busy
                              or self._owner is not asyncio.current_task())
        if invalid_call_claim:
            raise RuntimeError('Native stage requires its active, idle call claim and owning task')

    def check_begin(self, intent, mode, target):
        """A claimed call admits only the exact native stage it is opening.

        The permission is consumed before the asynchronous request. A second
        task cannot borrow an owner's budget or insert a write between reads.
        This is local sequencing, not a replacement for native final guards.
        """
        if self.retired or self.busy and self._admission != (asyncio.current_task(), intent, mode, target):
            raise ComputerUseError(
                dict(
                    code='execution_unknown' if self.retired else 'invalid_request', effect='none',
                    target='native call',
                    detail='The native session is retired.' if self.retired else 'Another native call owns this stage.',
                    next='close the old driver and observe a new session' if self.retired else 'await the active call'))
        self._admission = None

    @asynccontextmanager
    async def open(self, target, timeout_ms):
        with self.claim(timeout_ms) as remaining:
            async with self.operation(target, remaining) as read:
                yield read

    @asynccontextmanager
    async def operation(self, target, remaining, *, intent=None):
        """One native read or write stage; ownership never upgrades in place."""
        self.validate_claim(remaining)
        if intent is not None and target is None:
            raise ValueError('A write operation needs an observed target')
        self._active = True
        operation = None
        primary_error = None
        try:
            if target is None:
                target = await self.port.connect(remaining())
            # Exhaustion before begin is a known local refusal, not unknown
            # admission requiring retirement of an otherwise usable port.
            budget = remaining()
            try:
                self._admission = (asyncio.current_task(), intent.kind if intent else 'read',
                                   'write' if intent else 'read', target)
                opened = (await self.port.begin(intent, target, budget)
                          if intent is not None else await self.port.begin_read(target, budget))
                operation = opened.token
                target = target.canonical(opened.target)
            except BaseException as error:
                # Unknown admission cannot be repaired by starting another
                # directory/window stage on this same connection.
                if not isinstance(error, ComputerUseError) or error.effect != 'none':
                    self.retired = True
                raise
            finally:
                self._admission = None
            yield OperationLease(operation, target, remaining)
            if intent is None:
                remaining()
        except BaseException as error:
            primary_error = error
            raise
        finally:
            try:
                if operation is not None and await self.port.finish(operation) != Cleanup.COMPLETE:
                    self.retired = True
                    raise RuntimeError('Native operation cleanup is unconfirmed')
            except BaseException as error:
                self.retired = True
                if primary_error is None:
                    raise
                primary_error.add_note('Native operation cleanup unconfirmed: ' + str(error))
            finally:
                self._active = False
