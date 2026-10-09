# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Backward-compatible path for the Zen free-model cache.

The implementation lives in :mod:`jiuwenswarm.runtime.opencode_zen` so Gateway
and AgentServer share one in-memory cache without Gateway importing AgentServer.
This module object is that implementation.
"""

from __future__ import annotations

import sys

import jiuwenswarm.runtime.opencode_zen as _opencode_zen

sys.modules[__name__] = _opencode_zen
