# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""IdentityFieldFilter 测试。"""
import logging
import pytest
from jiuwenswarm.extensions.identity_provider.types import IdentityInfo
from jiuwenswarm.extensions.identity_provider.store import IdentityStore
from jiuwenswarm.common.utils import IdentityFieldFilter


@pytest.fixture(autouse=True)
def _reset():
    IdentityStore.set_test_state(None, False)
    yield
    IdentityStore.set_test_state(None, False)


def _rec():
    return logging.LogRecord("test.logger", logging.INFO, "", 1, "msg", (), None)


def test_filter_adds_identity_fields():
    IdentityStore.set_test_state(IdentityInfo(user_id="u1", domain_id="d1", app_id="a1"))
    r = _rec()
    assert IdentityFieldFilter().filter(r) is True
    assert r.user_id == "u1" and r.domain_id == "d1" and r.app_id == "a1"


def test_filter_handles_none_identity():
    IdentityStore.set_test_state(None)
    r = _rec()
    assert IdentityFieldFilter().filter(r) is True
    assert r.user_id is None and r.domain_id is None and r.app_id is None


def test_filter_handles_partial_identity():
    IdentityStore.set_test_state(IdentityInfo(user_id="u1", domain_id=None, app_id="a1"))
    r = _rec()
    assert IdentityFieldFilter().filter(r) is True
    assert r.user_id == "u1" and r.domain_id is None and r.app_id == "a1"
