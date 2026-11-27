# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Gateway Web channel normalisation helpers.

Provides :func:`register_forward_method` which allows callers (e.g.
``app_gateway._run``) to dynamically add extra methods to the Web channel
forward set at startup time, before the forward-handler closure is built.
"""
from __future__ import annotations

_extra_forward: set[str] = set()
_extra_no_local: set[str] = set()


def register_forward_method(method: str, *, skip_local: bool = False) -> None:
    """Register *method* as a Web channel forwarded method.

    If *skip_local* is True the method is also added to the 'no local handler'
    set (i.e. no local ack is sent; the response comes entirely from the
    upstream Agent / MessageHandler).
    """
    _extra_forward.add(method)
    if skip_local:
        _extra_no_local.add(method)


def get_extra_forward_methods() -> frozenset[str]:
    """Return the set of extra forwarded methods registered so far."""
    return frozenset(_extra_forward)


def get_extra_no_local_methods() -> frozenset[str]:
    """Return the set of extra no-local-handler methods registered so far."""
    return frozenset(_extra_no_local)
