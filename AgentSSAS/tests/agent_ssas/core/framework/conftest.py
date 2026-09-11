# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""framework 目录 conftest。

为 framework 单元测试提供共享 fixtures,与根 conftest 配合使用。
"""

from __future__ import annotations

import pytest

from agent_ssas.core.framework.storage.memory_store import MemoryStore
from agent_ssas.core.framework.storage.sqlite_store import SQLiteStore


@pytest.fixture()
def sqlite_store() -> SQLiteStore:
    """内存 SQLiteStore,测试结束自动清理。

    使用 :memory: 避免磁盘文件,测试间互不影响。
    """
    store = SQLiteStore(":memory:")
    yield store
    # SQLiteStore.close 为异步方法,此处直接同步关闭底层连接
    try:
        store._conn.close()
    except Exception:
        pass


@pytest.fixture()
def memory_store() -> MemoryStore:
    """MemoryStore 实例,纯内存存储,用于快速单元测试。"""
    return MemoryStore()
