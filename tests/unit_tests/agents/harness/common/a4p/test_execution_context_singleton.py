# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Flat and package execution_context must share one store."""

from __future__ import annotations

import jiuwenswarm.agents.harness.common.a4p.execution_context as pkg
import jiuwenswarm.agents.harness.common.a4p_execution_context as flat


def test_flat_and_package_share_authorization_store() -> None:
    assert (
        pkg.AUTHORIZATION_EXECUTION_CONTEXTS
        is flat.AUTHORIZATION_EXECUTION_CONTEXTS
    )
