# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""IdentityStore（contextvar 版）测试。"""
import contextvars
import pytest

from jiuwenswarm.extensions.identity_provider.types import IdentityInfo
from jiuwenswarm.extensions.identity_provider.base import IdentityProviderBase
from jiuwenswarm.extensions.identity_provider.store import IdentityStore


class MockProvider(IdentityProviderBase):
    def __init__(self, identity=None, should_fail=False):
        self._identity = identity
        self._should_fail = should_fail
        self.fetch_called = False

    async def fetch_identity(self) -> IdentityInfo:
        self.fetch_called = True
        if self._should_fail:
            raise RuntimeError("Mock fetch failed")
        return self._identity or IdentityInfo()

    async def on_fetch_failed(self, error: Exception) -> IdentityInfo | None:
        return IdentityInfo(user_id="fallback-user")


@pytest.fixture(autouse=True)
def _reset_identity_context():
    """每个测试前重置当前 context 的身份/标记，并清 provider。"""
    IdentityStore.set_test_state(None, False)
    IdentityStore.unregister_provider()
    yield
    IdentityStore.set_test_state(None, False)
    IdentityStore.unregister_provider()


def test_get_identity_none_by_default():
    assert IdentityStore.get_identity() is None
    assert IdentityStore.is_fetched() is False


def test_set_and_get_identity():
    info = IdentityInfo(user_id="u1", domain_id="d1", app_id="a1")
    IdentityStore.set_identity(info)
    got = IdentityStore.get_identity()
    assert got is info
    assert got.user_id == "u1"


def test_clear_resets_identity():
    token = IdentityStore.set_identity(IdentityInfo(user_id="u1"))
    assert IdentityStore.get_identity().user_id == "u1"
    IdentityStore.clear(token)
    assert IdentityStore.get_identity() is None


def test_clear_with_bad_token_is_silent():
    # clear(None) 或无效 token 不应抛异常
    IdentityStore.clear(None)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_fetch_and_store_success_returns_token():
    provider = MockProvider(IdentityInfo(user_id="u1", domain_id="d1"))
    IdentityStore.register_provider(provider)
    token = await IdentityStore.fetch_and_store()
    assert token is not None
    assert provider.fetch_called is True
    assert IdentityStore.get_identity().user_id == "u1"
    assert IdentityStore.is_fetched() is True


@pytest.mark.asyncio
async def test_fetch_and_store_no_provider_returns_none():
    token = await IdentityStore.fetch_and_store()
    assert token is None
    assert IdentityStore.is_fetched() is True
    assert IdentityStore.get_identity() is None


@pytest.mark.asyncio
async def test_fetch_and_store_failure_uses_fallback():
    provider = MockProvider(should_fail=True)
    IdentityStore.register_provider(provider)
    token = await IdentityStore.fetch_and_store()
    assert token is not None
    assert provider.fetch_called is True
    assert IdentityStore.get_identity().user_id == "fallback-user"


def test_concurrent_contexts_isolated():
    """两个 context 各自 set 互不串台，主 context 不被污染。"""
    ctx_a = contextvars.copy_context()
    ctx_b = contextvars.copy_context()

    def setter(uid):
        IdentityStore.set_identity(IdentityInfo(user_id=uid))
        return IdentityStore.get_identity().user_id

    a = ctx_a.run(setter, "A")
    b = ctx_b.run(setter, "B")
    assert a == "A" and b == "B"
    assert IdentityStore.get_identity() is None  # 主 context 未被污染
