# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Host-owned output drain barrier, independent of Core's private lease fields."""

from __future__ import annotations

import asyncio
from typing import Any


class OutputHandoff:
    """Keep a stopped interrupt round's reader alive until its owner closes it.

    Core can queue EOF before the host consumes the interaction card. Sending
    an answer to that finishing lease loses the continuation's output. The
    next interrupt dispatch must wait for this reader to release ownership;
    it must never resend input, steal a lease, or abort the previous round.
    """

    def __init__(self, owner: Any, stream: Any) -> None:
        self.owner = owner
        self.stream = stream
        self.drained = asyncio.Event()
        self._iterator = aiter(stream)
        self._close_error: BaseException | None = None

    async def wait(self) -> None:
        await self.drained.wait()
        if self._close_error is not None:
            raise RuntimeError(
                "Previous output owner failed to close"
            ) from self._close_error

    def __aiter__(self) -> OutputHandoff:
        return self

    async def __anext__(self) -> Any:
        return await anext(self._iterator)

    async def close(self, *, abort_active_round: bool = True) -> None:
        try:
            await self.stream.close(abort_active_round=abort_active_round)
        except BaseException as error:
            self._close_error = error
            raise
        finally:
            self.drained.set()
