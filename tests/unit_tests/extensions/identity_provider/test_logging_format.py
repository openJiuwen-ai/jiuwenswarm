# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""日志格式含身份字段测试。"""
import json
import logging
import pytest
from jiuwenswarm.extensions.identity_provider.types import IdentityInfo
from jiuwenswarm.extensions.identity_provider.store import IdentityStore
from jiuwenswarm.common.utils import IdentityFieldFilter, IdentityTextFormatter, JsonUserVisibleFormatter


@pytest.fixture(autouse=True)
def _reset():
    IdentityStore.set_test_state(None, False)
    yield
    IdentityStore.set_test_state(None, False)


def test_json_output_with_identity():
    IdentityStore.set_test_state(IdentityInfo(user_id="u1", domain_id="d1", app_id="a1"))
    r = logging.LogRecord("test.logger", logging.INFO, "", 42, "msg", (), None)
    IdentityFieldFilter().filter(r)
    obj = json.loads(JsonUserVisibleFormatter().format(r))
    assert obj["user_id"] == "u1" and obj["domain_id"] == "d1" and obj["app_id"] == "a1"
    assert obj["lineno"] == 42


def test_json_output_with_null_identity():
    IdentityStore.set_test_state(None)
    r = logging.LogRecord("test.logger", logging.INFO, "", 42, "msg", (), None)
    IdentityFieldFilter().filter(r)
    obj = json.loads(JsonUserVisibleFormatter().format(r))
    assert obj["user_id"] is None and obj["domain_id"] is None and obj["app_id"] is None


def test_text_output_with_identity():
    IdentityStore.set_test_state(IdentityInfo(user_id="u1", domain_id="d1", app_id="a1"))
    fmt = IdentityTextFormatter(fmt="%(levelname)s %(identity)s%(name)s:%(lineno)d: %(message)s")
    r = logging.LogRecord("test.logger", logging.INFO, "", 42, "msg", (), None)
    IdentityFieldFilter().filter(r)
    out = fmt.format(r)
    assert "user_id=u1" in out and "domain_id=d1" in out and "app_id=a1" in out


def test_text_output_null_identity():
    IdentityStore.set_test_state(None)
    fmt = IdentityTextFormatter(fmt="%(levelname)s %(identity)s%(name)s: %(message)s")
    r = logging.LogRecord("t", logging.INFO, "", 0, "msg", (), None)
    IdentityFieldFilter().filter(r)
    out = fmt.format(r)
    assert "user_id=null" in out and "domain_id=null" in out and "app_id=null" in out


def test_identity_built_before_sensitive_filter(monkeypatch):
    """IdentityFieldFilter 先拼 identity，SensitiveDataFilter 再脱敏 msg + identity。"""
    from jiuwenswarm.infrastructure.log_masking import SensitiveDataFilter
    from jiuwenswarm.infrastructure.log_masking.engine import LogMaskingEngine

    IdentityStore.set_test_state(None)
    LogMaskingEngine.reset_for_tests()
    LogMaskingEngine.reload_from_rows(
        [
            {
                "id": 6,
                "rule_id": "custom_app",
                "rule_name": "app_id=",
                "pattern": "app_id=",
                "replacement": "app_id=test",
                "priority": 0,
                "enabled": True,
            },
        ],
        db_authoritative=True,
    )
    monkeypatch.setattr(
        "jiuwenswarm.infrastructure.log_masking.filter.is_enterprise",
        lambda: True,
    )

    r = logging.LogRecord("t", logging.INFO, "", 0, "hello app_id=foo", (), None)
    IdentityFieldFilter().filter(r)
    assert "app_id=null" in r.identity

    SensitiveDataFilter().filter(r)

    assert "app_id=testnull" in r.identity
    assert "app_id=testfoo" in r.getMessage()

    fmt = IdentityTextFormatter(fmt="%(identity)s%(message)s")
    out = fmt.format(r)
    assert "app_id=testnull" in out
    assert "app_id=testfoo" in out
    LogMaskingEngine.reset_for_tests()


def test_identity_masked_with_builtin_rules_before_db(monkeypatch):
    """企业版在 GDB 冷加载前即可用内置规则脱敏 identity（不必等 uses_external_rules）。"""
    from jiuwenswarm.infrastructure.log_masking import SensitiveDataFilter
    from jiuwenswarm.infrastructure.log_masking.engine import LogMaskingEngine
    from jiuwenswarm.infrastructure.utils import fingerprint

    IdentityStore.set_test_state(IdentityInfo(user_id="alice", domain_id="d1", app_id="a1"))
    LogMaskingEngine.reset_for_tests()
    assert not LogMaskingEngine.get_instance().uses_external_rules
    monkeypatch.setattr(
        "jiuwenswarm.infrastructure.log_masking.filter.is_enterprise",
        lambda: True,
    )

    r = logging.LogRecord("t", logging.INFO, "", 0, "ok", (), None)
    IdentityFieldFilter().filter(r)
    SensitiveDataFilter().filter(r)

    assert "user_id=******(fp:" in r.identity
    assert f"fp:{fingerprint('alice')}" in r.identity
    assert r.user_id == f"******(fp:{fingerprint('alice')})"
    assert "domain_id=d1" in r.identity
    assert "app_id=a1" in r.identity
    LogMaskingEngine.reset_for_tests()


def test_identity_field_filter_sets_identity_attr():
    IdentityStore.set_test_state(IdentityInfo(user_id="u1", domain_id="d1", app_id="a1"))
    r = logging.LogRecord("t", logging.INFO, "", 0, "msg", (), None)
    IdentityFieldFilter().filter(r)
    assert r.identity == " user_id=u1 domain_id=d1 app_id=a1 "
