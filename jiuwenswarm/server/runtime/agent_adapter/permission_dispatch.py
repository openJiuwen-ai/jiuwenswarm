# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Root approval dispatch transactions; the queue alone owns permission cards."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, MutableMapping
from dataclasses import dataclass
from typing import Any

from openjiuwen.core.session.interaction.interactive_input import InteractiveInput

from jiuwenswarm.server.runtime.agent_adapter.permission_continuation import prepare_nonpermission_resume
from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue_rail import (
    put_root_nonpermission_resume_in_inputs,
)

from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue import (
    RootPermissionAnswer, RootPermissionQueue, RootPermissionQueueError,
)
from jiuwenswarm.agents.harness.common.rails.permissions.tool_invocation_key import ToolInvocationKeyV1

ROOT_PERMISSION_ANSWER_KEY = "_jiuwenswarm_root_permission_answer"
ROOT_PERMISSION_HANDOFF_KEY = "_jiuwenswarm_root_permission_handoff"


@dataclass(slots=True)
class RootPermissionDispatchHandoff:
    lock: asyncio.Lock
    root_session_id: str
    answer: RootPermissionAnswer | None = None
    accepted: bool = False
    closed: bool = False
    superseded_answer: RootPermissionAnswer | None = None


class RootPermissionDispatch:
    def __init__(self, queue: RootPermissionQueue) -> None:
        self.queue = queue
        self.lock = asyncio.Lock()
        self.handoff: RootPermissionDispatchHandoff | None = None

    def prepare_resume(
        self, inputs: dict[str, Any], *, root_session_id: str, loop_session: Any,
    ) -> dict[str, Any]:
        """Reserve the exact head or prepare an ordinary SDK ask continuation."""
        self.queue.raise_if_quarantined(root_session_id)
        query = inputs.get("query")
        if not isinstance(query, InteractiveInput):
            return put_root_nonpermission_resume_in_inputs(inputs, None)
        if loop_session is not None:
            get_session_id = getattr(loop_session, "get_session_id", None)
            loop_session_id = (
                str(get_session_id() or "") if callable(get_session_id) else ""
            )
            if ((loop_session_id or "default").strip() or "default") != root_session_id:
                raise RootPermissionQueueError("permission_queue_session_mismatch")
        try:
            answer = self.queue.reserve_answer(
                root_session_id,
                query,
            )
        except RootPermissionQueueError as exc:
            if str(exc) != "permission_queue_empty":
                raise
            if self.queue.has_live(root_session_id=root_session_id):
                raise RootPermissionQueueError(
                    "nonpermission_resume_permission_conflict"
                ) from exc
            return prepare_nonpermission_resume(
                loop_session,
                inputs,
                query,
                root_session_id=root_session_id,
            )
        prepared = put_root_nonpermission_resume_in_inputs(inputs, None)
        prepared["query"] = answer.interactive_input
        prepared[ROOT_PERMISSION_ANSWER_KEY] = answer
        return prepared

    def has_live(self, root_session_id: str | None) -> bool:
        return self.queue.has_live(root_session_id=root_session_id) or self.lock.locked() or bool(
            self.handoff is not None and not self.handoff.closed
            and (root_session_id is None or self.handoff.root_session_id == root_session_id)
        )

    async def acquire(self, root_session_id: str) -> RootPermissionDispatchHandoff:
        await self.lock.acquire()
        return RootPermissionDispatchHandoff(self.lock, root_session_id)

    def abort_preparation(self, handoff: RootPermissionDispatchHandoff) -> None:
        if isinstance(handoff.answer, RootPermissionAnswer):
            self.queue.release_answer(handoff.answer)
        if self.handoff is handoff:
            self.handoff = None
        if handoff.lock.locked():
            handoff.lock.release()
        handoff.closed = True

    def publish_cutover(
        self,
        handoff: RootPermissionDispatchHandoff,
    ) -> bool:
        if not handoff.lock.locked():
            raise RootPermissionQueueError("permission_dispatch_mutex_missing")
        if not self.queue.begin_cutover(
            root_session_id=handoff.root_session_id
        ):
            return False
        previous = self.handoff
        if isinstance(previous, RootPermissionDispatchHandoff) and (
            previous.root_session_id == handoff.root_session_id
        ):
            if previous.accepted and isinstance(
                previous.answer, RootPermissionAnswer
            ):
                handoff.superseded_answer = previous.answer
        return True

    async def start_cutover(
        self,
        root_session_id: str,
    ) -> RootPermissionDispatchHandoff | None:
        handoff = await self.acquire(root_session_id)
        if self.publish_cutover(handoff):
            return handoff
        handoff.closed = True
        handoff.lock.release()
        return None

    async def complete_cutover(
        self,
        handoff: RootPermissionDispatchHandoff,
        *,
        discard: Callable[[tuple[ToolInvocationKeyV1, ...]], Awaitable[bool]],
        discard_confirmed: bool,
        keep_lock: bool,
    ) -> bool:
        try:
            if not discard_confirmed:
                return False
            if isinstance(handoff.superseded_answer, RootPermissionAnswer):
                self.queue.consume_accepted_answer_if_quarantined(
                    handoff.superseded_answer
                )
                self.close_claimed(
                    handoff.superseded_answer.card.key
                )
            frozen_keys = self.queue.cutover_continuation_scope(
                root_session_id=handoff.root_session_id
            )
            if frozen_keys and not await discard(frozen_keys):
                return False
            self.queue.discard_continuation(
                frozen_keys,
                root_session_id=handoff.root_session_id,
            )
            return True
        finally:
            handoff.superseded_answer = None
            if not keep_lock:
                handoff.closed = True
                if handoff.lock.locked():
                    handoff.lock.release()
            elif not discard_confirmed:
                # The caller's preparation exception path clears the owner and
                # releases this retained lock without admitting a fresh input.
                handoff.closed = True

    def close_claimed(
        self,
        key: ToolInvocationKeyV1,
    ) -> None:
        """Retire the accepted dispatch owner once the exact callback wins CAS."""

        handoff = self.handoff
        answer = (
            handoff.answer
            if isinstance(handoff, RootPermissionDispatchHandoff)
            else None
        )
        if isinstance(answer, RootPermissionAnswer) and answer.card.key == key:
            handoff.closed = True
            self.handoff = None

    def release(self, inputs: Any) -> None:
        if not isinstance(inputs, MutableMapping):
            return
        answer = inputs.pop(ROOT_PERMISSION_ANSWER_KEY, None)
        handoff = inputs.pop(ROOT_PERMISSION_HANDOFF_KEY, None)
        if isinstance(answer, RootPermissionAnswer):
            self.queue.release_answer(answer)
        if isinstance(handoff, RootPermissionDispatchHandoff):
            if handoff.lock.locked():
                handoff.lock.release()
            handoff.closed = True

    def finalize(self, inputs: Any) -> None:
        """Close this request's exact handoff after callback/cutover wins its CAS."""

        if not isinstance(inputs, MutableMapping):
            return
        handoff = inputs.pop(ROOT_PERMISSION_HANDOFF_KEY, None)
        inputs.pop(ROOT_PERMISSION_ANSWER_KEY, None)
        if not isinstance(handoff, RootPermissionDispatchHandoff):
            return
        if not handoff.accepted:
            if handoff.lock.locked():
                handoff.lock.release()
            handoff.closed = True
            return
        if self.handoff is not handoff:
            return
        answer = handoff.answer
        current = (
            self.queue.get(answer.card.key)
            if isinstance(answer, RootPermissionAnswer)
            else None
        )
        if current is None:
            handoff.closed = True
            self.handoff = None

    @staticmethod
    def prepare(inputs: dict[str, Any], handoff: RootPermissionDispatchHandoff) -> dict[str, Any]:
        answer = inputs.get(ROOT_PERMISSION_ANSWER_KEY)
        if answer is not None and not isinstance(answer, RootPermissionAnswer):
            raise RootPermissionQueueError("permission_queue_answer_invalid")
        handoff.answer = answer
        inputs[ROOT_PERMISSION_HANDOFF_KEY] = handoff
        return inputs

    async def send(self, request: Any, send_input: Callable[[Any], Awaitable[Any]]) -> bool:
        """Hold the reservation until the selected SDK runtime accepts the input."""
        answer = request.inputs.pop(ROOT_PERMISSION_ANSWER_KEY, None)
        handoff = request.inputs.pop(ROOT_PERMISSION_HANDOFF_KEY, None)
        if answer is not None and not isinstance(answer, RootPermissionAnswer):
            raise RootPermissionQueueError("permission_queue_answer_invalid")
        if not isinstance(handoff, RootPermissionDispatchHandoff):
            raise RootPermissionQueueError("permission_dispatch_handoff_missing")
        if handoff.answer is not answer:
            raise RootPermissionQueueError("permission_dispatch_handoff_mismatch")
        try:
            await send_input(request)
        except BaseException:
            if answer is not None:
                self.queue.release_answer(answer)
            if handoff.lock.locked():
                handoff.lock.release()
            handoff.closed = True
            raise
        handoff.accepted = True
        current = self.queue.get(answer.card.key) if isinstance(answer, RootPermissionAnswer) else None
        if current is not None and current.state == "resuming":
            self.handoff = handoff
        else:
            handoff.closed = True
        if handoff.lock.locked():
            handoff.lock.release()
        return answer is not None
