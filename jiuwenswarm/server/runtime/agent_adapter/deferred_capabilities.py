# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Callable capability exports resolved on first actual use, including Frozen."""

from importlib import import_module
from typing import Any, Callable


def deferred(module: str, symbol: str) -> Callable[..., Any]:
    target = None

    def invoke(*args, **kwargs):
        nonlocal target
        if target is None:
            target = getattr(import_module(module), symbol)
        return target(*args, **kwargs)

    invoke.__name__ = symbol
    return invoke
