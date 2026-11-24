# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Non-owning hot-reload indexes; the harness owns its agents and rails."""
from __future__ import annotations

from collections.abc import MutableMapping, Sequence
from typing import Any, Callable
import weakref

from openjiuwen.harness_protocol.state import HarnessState


def weak_value(value: Any) -> Callable[[], Any]:
    try:
        return weakref.ref(value)
    except TypeError:
        # Preserve support for legacy, non-weak-referenceable adapters.
        return lambda: value


def weak_pair(value: tuple[Any, Any]) -> Callable[[], Any]:
    agent_ref, rail_ref = weak_value(value[0]), weak_value(value[1])

    def resolve():
        agent, rail = agent_ref(), rail_ref()
        if agent is None or getattr(agent, "state", None) is HarnessState.TERMINATED:
            return None
        return (agent, rail) if rail is not None else None

    return resolve


def weak_mount(value: Any) -> Callable[[], Any]:
    def resolve():
        agent = value.agent
        if agent is None or getattr(agent, "state", None) is HarnessState.TERMINATED:
            return None
        return value

    return resolve


class WeakRegistry(MutableMapping):
    """Resolve live values without extending their owner's lifetime."""

    def __init__(self, reference: Callable = weak_value):
        self._reference = reference
        self._entries: dict[str, Callable] = {}

    def __getitem__(self, key):
        value = self._entries[key]()
        if value is None:
            del self._entries[key]
            raise KeyError(key)
        return value

    def __setitem__(self, key, value):
        self._entries[key] = self._reference(value)

    def __delitem__(self, key):
        del self._entries[key]

    def __iter__(self):
        for key, resolve in list(self._entries.items()):
            if resolve() is None:
                self._entries.pop(key, None)
        return iter(list(self._entries))

    def __len__(self):
        return sum(1 for _ in self)

    def items(self):
        result = []
        for key, resolve in list(self._entries.items()):
            value = resolve()
            if value is not None:
                result.append((key, value))
        return result

    def values(self):
        return [value for _key, value in self.items()]


class WeakRegistryList(Sequence):
    """Ordered live registrations, with the existing list read interface."""

    def __init__(self, values=(), *, reference: Callable = weak_value):
        self._reference = reference
        self._entries = [reference(value) for value in values]

    def append(self, value):
        self._entries = [resolve for resolve in self._entries if resolve() is not None]
        self._entries.append(self._reference(value))

    def __iter__(self):
        values = [resolve() for resolve in self._entries]
        return iter([value for value in values if value is not None])

    def __len__(self):
        return sum(1 for _ in self)

    def __getitem__(self, index):
        return list(self)[index]
