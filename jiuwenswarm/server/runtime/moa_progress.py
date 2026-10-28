# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Emit transient MoA phase notices without exposing internal model work."""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import contextmanager
from typing import Any, AsyncIterator, Iterator, Literal
from uuid import uuid4

from openjiuwen.core.foundation.llm.system_one import JevSystemOneClient
from openjiuwen.core.session.stream import OutputSchema
from openjiuwen.harness.moa import LlmVerifierProgressScorer

from jiuwenswarm.common.utils import logger


class _ProgressMailbox:
    """Reserve terminal capacity when admitting a start; never block a model."""

    def __init__(self) -> None:
        self.pending: deque[dict[str, str]] = deque()
        self.active: set[str] = set()
        self.changed = asyncio.Event()
        self.closed = False

    def record(self, operation_id: str, phase: str, status: str) -> None:
        if self.closed:
            return
        if status == "started":
            if len(self.pending) + len(self.active) + 2 > 128:
                return
            self.active.add(operation_id)
        elif operation_id not in self.active:
            return
        else:
            self.active.remove(operation_id)
        self.pending.append({"operation_id": operation_id, "phase": phase, "status": status})
        self.changed.set()

    def close(self) -> None:
        self.closed = True
        self.pending.clear()
        self.active.clear()
        self.changed.set()


class MoaProgress:
    """Invocation notices owned by the existing runtime and output consumer."""

    def __init__(self) -> None:
        self.mailbox: _ProgressMailbox | None = None

    def open(self) -> _ProgressMailbox:
        if self.mailbox is None:
            self.mailbox = _ProgressMailbox()
        return self.mailbox

    def close(self) -> None:
        if self.mailbox is not None:
            self.mailbox.close()
            self.mailbox = None

    async def stream(self, source: AsyncIterator[Any]) -> AsyncIterator[Any]:
        """Merge optional notices without making the runner await their delivery."""
        mailbox = self.open()
        output: asyncio.Queue[Any] = asyncio.Queue(maxsize=1)
        done = False
        error: BaseException | None = None

        async def read_output() -> None:
            nonlocal done, error
            try:
                async for chunk in source:
                    await output.put(chunk)
                    mailbox.changed.set()
            except BaseException as exc:
                error = exc
            finally:
                done = True
                mailbox.changed.set()

        reader = asyncio.create_task(read_output(), name="moa-output-reader")
        try:
            while True:
                mailbox.changed.clear()
                # Preserve normal output order and give it priority over hints.
                if not output.empty():
                    yield output.get_nowait()
                elif done:
                    if error is not None:
                        raise error
                    return
                elif mailbox.pending:
                    yield OutputSchema(type="chat.moa_status", index=0, payload=mailbox.pending.popleft())
                else:
                    await mailbox.changed.wait()
        finally:
            mailbox.close()
            if self.mailbox is mailbox:
                self.mailbox = None
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)


@contextmanager
def _operation(progress: MoaProgress | None, phase: Literal["reference", "verification"]) -> Iterator[None]:
    """Capture the mailbox so a late finish cannot enter a later execution."""
    mailbox = progress.mailbox if progress is not None else None
    operation_id = uuid4().hex

    def record(status: str) -> None:
        try:
            if mailbox is not None:
                mailbox.record(operation_id, phase, status)
        except Exception:
            logger.debug("MoA status delivery skipped", exc_info=False)

    status = "completed"
    try:
        record("started")
        yield
    except asyncio.CancelledError:
        status = "cancelled"
        raise
    except BaseException:
        status = "failed"
        raise
    finally:
        record(status)


class ReferenceProgressModel:
    """Preserve reference model configuration while observing consultation calls."""

    def __init__(self, model: Any, progress: MoaProgress | None = None) -> None:
        self._model = model
        self._progress = progress

    def __getattr__(self, name: str) -> Any:
        return getattr(self._model, name)

    async def invoke(self, messages: list[Any], *, tools: list[Any] | None = None) -> Any:
        with _operation(self._progress, "reference"):
            return await self._model.invoke(messages=messages, tools=tools)


class _ProgressScore:
    def __init__(self, scorer: Any, progress: MoaProgress | None) -> None:
        self._scorer = scorer
        self._progress = progress

    async def __call__(self, problem: str, steps: tuple[str, ...], n_evaluations: int) -> float:
        with _operation(self._progress, "verification"):
            return await self._scorer(problem, steps, n_evaluations)


class ProgressLlmVerifierScorer(LlmVerifierProgressScorer):
    """Observe one complete verifier checkpoint, including repeated evaluations."""

    progress: MoaProgress | None = None

    def for_invoke(self, problem: str, n_evaluations: int | None = None) -> _ProgressScore:
        return _ProgressScore(super().for_invoke(problem, n_evaluations=n_evaluations), self.progress)


class ProgressJevSystemOneClient(JevSystemOneClient):
    """Keep native JEV ownership and retries inside one transient notice."""

    progress: MoaProgress | None = None

    async def system_one(
        self,
        state: Any,
        questions: dict[str, Any],
        *,
        model: str | None = None,
        request_timeout: float | None = None,
    ) -> Any:
        with _operation(self.progress, "verification"):
            return await super().system_one(
                state=state,
                questions=questions,
                model=model,
                request_timeout=request_timeout,
            )
