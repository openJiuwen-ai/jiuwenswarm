# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""冻结/恢复 checkpointer 一致性校验：底层 _kv_store 不同时告警。"""

from __future__ import annotations

import logging
from types import SimpleNamespace

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)


def test_checkpointer_same_instance_does_not_warn(caplog):
    cp = SimpleNamespace(_kv_store=object())

    with caplog.at_level(logging.WARNING):
        JiuWenSwarmDeepAdapter._validate_checkpointer_consistency(cp, cp)

    assert "checkpointer consistency mismatch" not in caplog.text


def test_checkpointer_same_kv_store_does_not_warn(caplog):
    shared_kv = object()
    freeze_cp = SimpleNamespace(_kv_store=shared_kv)
    recover_cp = SimpleNamespace(_kv_store=shared_kv)

    with caplog.at_level(logging.WARNING):
        JiuWenSwarmDeepAdapter._validate_checkpointer_consistency(
            freeze_cp, recover_cp
        )

    assert "checkpointer consistency mismatch" not in caplog.text


def test_checkpointer_none_kv_store_does_not_warn(caplog):
    freeze_cp = SimpleNamespace(_kv_store=None)
    recover_cp = SimpleNamespace(_kv_store=None)

    with caplog.at_level(logging.WARNING):
        JiuWenSwarmDeepAdapter._validate_checkpointer_consistency(
            freeze_cp, recover_cp
        )

    assert "checkpointer consistency mismatch" not in caplog.text


def test_checkpointer_different_kv_store_logs_warning(caplog):
    freeze_cp = SimpleNamespace(_kv_store=object())
    recover_cp = SimpleNamespace(_kv_store=object())

    with caplog.at_level(logging.WARNING):
        JiuWenSwarmDeepAdapter._validate_checkpointer_consistency(
            freeze_cp, recover_cp
        )

    assert "checkpointer consistency mismatch" in caplog.text
