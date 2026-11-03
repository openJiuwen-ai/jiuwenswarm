# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Compaction Ops/Event bridge; native bindings are not installed by production."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging

from openjiuwen.core.context_engine import CompactionCandidate, CompactionError

from jiuwenswarm.extensions_v2.session_compaction.dispatch import CompactMessageCodec, dispatch_compaction
from jiuwenswarm.extensions_v2.event_types.session_compact import SessionCompacted, SessionCompactSkipped

logger = logging.getLogger(__name__)


@dataclass
class _Binding:
    engine: object
    context: object
    busy: object
    after_commit: object
    remove: object = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    epoch: object = None
    processor_types: object = None


class SessionCompactionService:
    """Authorize only known live native sessions; never look up a recent session."""

    def __init__(self, bus, *, policy_epoch=lambda: None, invocation_guard=None):
        self.bus = bus
        self.bindings = {}
        self.tasks = set()
        self.policy_epoch = policy_epoch
        self.invocation_guard = invocation_guard
        self.unsupported = set()
        self.precomputing = set()

    def bind(self, engine, context, *, busy, after_commit=None, processor_types=None):
        """Explicit host binding, currently exercised by independent tests only."""
        sid = context.session_id
        self.unsupported.discard(sid)
        previous = self.bindings.get(sid)
        if previous is not None and previous.engine is engine:
            previous.context = context
            previous.processor_types = processor_types
            return
        if previous is not None:
            previous.remove()
        binding = _Binding(engine, context, busy, after_commit, processor_types=processor_types)
        async def hook(request, continuation):
            if self.bindings.get(sid) is not binding or request.session_id != sid:
                raise CompactionError("COMPACT_SESSION_UNAVAILABLE")
            if request.context_id != "default_context_id":
                raise CompactionError("COMPACT_UNSUPPORTED")
            epoch = self.policy_epoch()
            if binding.epoch != epoch:
                engine.invalidate_compaction_candidates()
                binding.epoch = epoch
            result = await dispatch_compaction(self.bus, binding.context, request, continuation)
            if self.policy_epoch() != epoch:
                raise CompactionError("COMPACT_STALE")
            return result
        binding.remove = engine.register_compaction_hook(hook)
        self.bindings[sid] = binding

    def is_compacting(self, session_id):
        binding = self.bindings.get(session_id)
        return binding is not None and binding.lock.locked()

    async def compact(self, ops, *, instructions=None):
        if self.invocation_guard is None or not self.invocation_guard(ops):
            raise CompactionError("COMPACT_UNBOUND_INVOCATION")
        if ops.session_context is None:
            raise CompactionError("COMPACT_SESSION_UNAVAILABLE")
        session_id = ops.session_context.session_id
        if session_id not in self.bindings and session_id not in self.unsupported:
            # Native bindings are deliberately absent in the interface-only stage.
            raise CompactionError("COMPACT_NOT_INTEGRATED")
        return await self.run(session_id, trigger="plugin", instructions=instructions)

    async def run(self, session_id, *, trigger="manual", instructions=None, processor_types=None):
        binding = self.bindings.get(session_id)
        if binding is None:
            if session_id in self.unsupported:
                raise CompactionError("COMPACT_UNSUPPORTED")
            raise CompactionError("COMPACT_SESSION_UNAVAILABLE")
        if processor_types is None:
            processor_types = binding.processor_types
        if binding.lock.locked() or binding.busy():
            raise CompactionError("COMPACT_BUSY")
        async def execute():
            result = await binding.engine.compact_context(
                session_id=session_id, trigger=trigger, instructions=instructions,
                processor_types=processor_types,
            )
            if isinstance(result, CompactionCandidate):
                if trigger != "precompute" and binding.after_commit is not None:
                    await binding.after_commit(trigger, result)
                codec = CompactMessageCodec()
                views = codec.encode(result.messages)
                for message in views:
                    message.pop("handle", None)
                return SessionCompacted(views, result.tokens_before, result.tokens_after)
            return SessionCompactSkipped(result.skip)
        if trigger == "precompute":
            return await execute()
        async with binding.lock:
            if self.bindings.get(session_id) is not binding or binding.busy():
                raise CompactionError("COMPACT_SESSION_UNAVAILABLE")
            return await execute()

    def unbind(self, session_id):
        binding = self.bindings.pop(session_id, None)
        if binding is not None:
            binding.remove()

    def mark_unsupported(self, session_id):
        self.unbind(session_id)
        self.unsupported.add(session_id)

    def schedule_precompute(self, session_id, *, enabled=False, ratio=0.8):
        """Start optional speculation only after the native input task exits."""
        if not enabled:
            return
        if type(ratio) not in (int, float) or not 0 < ratio < 1:
            raise ValueError("compaction_precompute.ratio must be between zero and one")
        binding = self.bindings.get(session_id)
        if binding is None:
            return
        if session_id in self.precomputing:
            return
        async def prepare():
            if self.bindings.get(session_id) is not binding or binding.busy():
                return
            context = binding.engine.get_context(session_id=session_id)
            if context is None or context.token_counter() is None:
                return
            tokens = context.token_counter().count_messages(context.get_messages())
            if tokens < context.context_window_tokens() * ratio:
                return
            try:
                await self.run(session_id, trigger="precompute")
            except Exception:
                logger.warning("Session compaction precompute failed: session_id=%s", session_id, exc_info=True)
        def start(_):
            if self.bindings.get(session_id) is not binding or session_id in self.precomputing:
                return
            self.precomputing.add(session_id)
            task = asyncio.create_task(prepare())
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
            task.add_done_callback(lambda _: self.precomputing.discard(session_id))
        current = asyncio.current_task()
        if current is not None:
            current.add_done_callback(start)

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
        self.unsupported.clear()
        self.precomputing.clear()
        for sid in list(self.bindings):
            self.unbind(sid)
