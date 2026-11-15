# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Prepare Swarm snapshots through the existing native Checkpointer contract."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from typing import Any
import uuid
import weakref

from openjiuwen.core.session.checkpointer.base import Checkpointer
from openjiuwen.core.session.agent import Session as NativeSession

OWNER_KEY = "jiuwenswarm_context_checkpoint_owner_v1"
_prepared = ContextVar("swarm_prepared_context_checkpoint", default=None)
_by_session = weakref.WeakKeyDictionary()
_by_key = weakref.WeakValueDictionary()


@dataclass
class _Binding:
    key: tuple[str, str]
    token: str
    session: Any
    engine: Any
    lock: asyncio.Lock
    loop: Any


def bind_context_checkpoint(context_engine: Any, session: Any) -> _Binding | None:
    """Bind one live host Session without retaining it past its owner's lifetime."""
    if not isinstance(session, NativeSession):
        return None
    previous = _by_session.get(session)
    if previous is not None and previous.engine() is context_engine:
        return previous
    key = (str(session.get_session_id()), str(session.get_agent_id()))
    active = _by_key.get(key)
    loop = asyncio.get_running_loop()
    lock = active.lock if active is not None and active.loop is loop else asyncio.Lock()
    binding = _Binding(key, uuid.uuid4().hex, weakref.ref(session), weakref.ref(context_engine), lock, loop)
    _by_session[session] = binding
    _by_key[key] = binding
    session.update_state({OWNER_KEY: {"session_id": key[0], "agent_id": key[1], "token": binding.token}})
    return binding


@contextmanager
def prepared_context_checkpoint(binding: _Binding | None):
    """Avoid preparing twice when the host explicitly saves and commits a window."""
    token = _prepared.set(binding.token if binding is not None else None)
    try:
        yield
    finally:
        _prepared.reset(token)


@asynccontextmanager
async def context_checkpoint_lock(binding: _Binding | None):
    if binding is None:
        yield
    else:
        async with binding.lock:
            yield


class _CheckpointState:
    def __init__(self, state: Any):
        self.value = deepcopy(state.dump())

    def dump(self) -> dict[str, Any]:
        return self.value

    def get_state(self, *, copied: bool = True, **kwargs: Any) -> dict[str, Any]:
        from openjiuwen.core.session.state.base import GLOBAL_STATE_KEY, AGENT_STATE_KEY

        value = {GLOBAL_STATE_KEY: self.value["global_state"], AGENT_STATE_KEY: self.value["agent_state"]}
        return deepcopy(value) if copied else value

    def get_global(self, key: Any = None) -> Any:
        value = self.value.get("global_state", {})
        return value if key is None else value.get(key)


class _CheckpointSession:
    """Freeze this commit while the original Session can continue receiving state."""

    def __init__(self, session: Any):
        self.delegate = session
        self.frozen_state = _CheckpointState(session.state())

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    def state(self) -> _CheckpointState:
        return self.frozen_state


class ContextCheckpointer(Checkpointer):
    """Delegate native storage, adding host preparation at both agent commits."""

    def __init__(self, delegate: Checkpointer):
        self.delegate = delegate

    def __getattr__(self, name: str) -> Any:
        # Preserve the existing runtime's storage shutdown/disposal lookup.
        return getattr(self.delegate, name)

    async def _commit(self, native_session: Any, *, interrupted: bool) -> Any:
        from jiuwenswarm.server.runtime.session.history_replay import HistoryReplayError

        method = self.delegate.interrupt_agent_execute if interrupted else self.delegate.post_agent_execute
        key = (str(native_session.session_id()), str(native_session.agent_id()))
        owner = native_session.state().get_global(OWNER_KEY)
        if not isinstance(owner, dict) or (owner.get("session_id"), owner.get("agent_id")) != key:
            return await method(native_session)
        if _prepared.get() == owner.get("token"):
            return await method(_CheckpointSession(native_session))
        binding = _by_key.get(key)
        if binding is None:
            # Persisted owner tokens outlive the Worker that created them.
            # A native commit before this process binds a window is still valid.
            return await method(native_session)
        if binding.token != owner.get("token"):
            raise HistoryReplayError("The context checkpoint owner is no longer active", code="HISTORY_RESTORE_CHANGED")
        async with binding.lock:
            return await self._commit_bound(native_session, method, binding, owner)

    async def _commit_bound(self, native_session: Any, method: Any, binding: _Binding,
                            owner: dict[str, Any]) -> Any:
        from jiuwenswarm.server.runtime.session.context_snapshot import SNAPSHOT_KEY, prepare_context_snapshot
        from jiuwenswarm.server.runtime.session.history_replay import HistoryReplayError

        if _by_key.get(binding.key) is not binding:
            raise HistoryReplayError("The context checkpoint owner changed", code="HISTORY_RESTORE_CHANGED")
        session = binding.session()
        engine = binding.engine()
        if session is None or engine is None:
            raise HistoryReplayError("The context checkpoint owner was released", code="HISTORY_RESTORE_CHANGED")
        if session.get_state(OWNER_KEY) != owner:
            raise HistoryReplayError("The context checkpoint Session changed", code="HISTORY_RESTORE_CHANGED")
        previous_reference = session.get_state(SNAPSHOT_KEY)
        previous_context = session.get_state("context")
        try:
            states = await engine.save_contexts(session)
            states = await prepare_context_snapshot(context_engine=engine, session=session, states=states)
            session.update_state({"context": None})
            session.update_state({"context": states})
            if _by_key.get(binding.key) is not binding:
                raise HistoryReplayError("The context checkpoint owner changed", code="HISTORY_RESTORE_CHANGED")
            return await method(_CheckpointSession(native_session))
        except BaseException:
            session.update_state({SNAPSHOT_KEY: None, "context": None})
            session.update_state({SNAPSHOT_KEY: previous_reference, "context": previous_context})
            raise

    async def pre_agent_execute(self, session: Any, inputs: Any) -> Any:
        return await self.delegate.pre_agent_execute(session, inputs)

    async def post_agent_execute(self, session: Any) -> Any:
        return await self._commit(session, interrupted=False)

    async def interrupt_agent_execute(self, session: Any) -> Any:
        return await self._commit(session, interrupted=True)

    async def pre_agent_team_execute(self, session: Any, inputs: Any) -> Any:
        return await self.delegate.pre_agent_team_execute(session, inputs)

    async def post_agent_team_execute(self, session: Any) -> Any:
        return await self.delegate.post_agent_team_execute(session)

    async def pre_workflow_execute(self, session: Any, inputs: Any) -> Any:
        return await self.delegate.pre_workflow_execute(session, inputs)

    async def post_workflow_execute(self, session: Any, result: Any, exception: Any) -> Any:
        return await self.delegate.post_workflow_execute(session, result, exception)

    async def session_exists(self, session_id: str) -> bool:
        return await self.delegate.session_exists(session_id)

    async def release(self, session_id: str) -> Any:
        return await self.delegate.release(session_id)

    def graph_store(self) -> Any:
        return self.delegate.graph_store()
