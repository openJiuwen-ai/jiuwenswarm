# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""AgentWebSocketServer 连接触发身份获取（contextvar 版）。"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
from jiuwenswarm.extensions.identity_provider.types import IdentityInfo
from jiuwenswarm.extensions.identity_provider.store import IdentityStore
from jiuwenswarm.extensions.identity_provider.base import IdentityProviderBase


@pytest.fixture(autouse=True)
def _reset():
    IdentityStore.set_test_state(None, False)
    IdentityStore.unregister_provider()
    yield
    IdentityStore.set_test_state(None, False)
    IdentityStore.unregister_provider()


class _Provider(IdentityProviderBase):
    def __init__(self):
        self.fetch_called = False

    async def fetch_identity(self) -> IdentityInfo:
        self.fetch_called = True
        return IdentityInfo(user_id="conn-user", domain_id="conn-domain")


@pytest.mark.asyncio
async def test_fetch_and_store_called_on_connection():
    """模拟连接 handler 内部调用 fetch_and_store，身份写入当前 context。"""
    provider = _Provider()
    IdentityStore.register_provider(provider)

    # 模拟 _connection_handler 内部这一步
    token = await IdentityStore.fetch_and_store()
    assert token is not None
    assert provider.fetch_called is True
    assert IdentityStore.get_identity().user_id == "conn-user"
    # 清理（模拟 finally）
    IdentityStore.clear(token)
    assert IdentityStore.get_identity() is None


@pytest.mark.asyncio
async def test_connection_continues_without_provider():
    token = await IdentityStore.fetch_and_store()
    assert token is None
    assert IdentityStore.get_identity() is None


@pytest.mark.asyncio
async def test_identity_propagates_to_child_task():
    """asyncio.create_task 自动 copy_context，子任务能读到身份。"""
    IdentityStore.register_provider(_Provider())
    await IdentityStore.fetch_and_store()

    seen: list[str] = []

    async def child():
        seen.append(IdentityStore.get_identity().user_id)

    await asyncio.create_task(child())
    assert seen == ["conn-user"]
