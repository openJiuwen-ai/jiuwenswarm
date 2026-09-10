# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""日志脱敏引擎单测（不依赖 packages/jiuwenclaw-ee）。"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from jiuwenswarm.infrastructure.log_masking.engine import (
    LogMaskingEngine,
    _KV_SENSITIVE_PATTERN,
    validate_pattern,
)
from jiuwenswarm.infrastructure.log_masking.probes import LOG_MASKING_PROBE_SAMPLES


@pytest.fixture(autouse=True)
def _reset_engine():
    LogMaskingEngine.reset_for_tests()
    yield
    LogMaskingEngine.reset_for_tests()


def test_builtin_sanitize_masks_email_and_kv():
    engine = LogMaskingEngine.get_instance()
    assert "******" in engine.sanitize("contact user@example.com")
    assert "******" in engine.sanitize("password=mySecret&user=alice")
    assert not engine.uses_external_rules


def test_builtin_pii_has_no_fingerprint():
    engine = LogMaskingEngine.get_instance()
    out = engine.sanitize("mail user@example.com phone 13800138000")
    assert "user@example.com" not in out
    assert "13800138000" not in out
    assert "fp:" not in out


def test_builtin_rule_ids_include_pii_and_kv():
    ids = {r.rule_id for r in LogMaskingEngine.compiled_default_rules()}
    assert {
        "builtin_email",
        "builtin_cn_mobile",
        "builtin_cn_id_card",
        "builtin_kv_sensitive",
        "builtin_data_image",
    } <= ids
    by_id = {r.rule_id: r for r in LogMaskingEngine.compiled_default_rules()}
    for rid in (
        "builtin_email",
        "builtin_cn_mobile",
        "builtin_cn_id_card",
        "builtin_data_image",
    ):
        assert not by_id[rid].with_fingerprint
    assert by_id["builtin_kv_sensitive"].with_fingerprint


def test_compile_masking_rows_reads_with_fingerprint_default_false():
    rules = LogMaskingEngine.compile_masking_rows(
        [
            {
                "id": 1,
                "rule_id": "custom_a",
                "rule_name": "a",
                "pattern": r"AAA-\d+",
                "replacement": "[A]",
                "priority": 1,
                "enabled": True,
            },
            {
                "id": 2,
                "rule_id": "custom_b",
                "rule_name": "b",
                "pattern": r"BBB-\d+",
                "replacement": "[B]",
                "priority": 2,
                "enabled": True,
                "with_fingerprint": True,
            },
        ]
    )
    by_id = {r.rule_id: r for r in rules}
    assert by_id["custom_a"].with_fingerprint is False
    assert by_id["custom_b"].with_fingerprint is True


def test_with_fingerprint_false_masks_without_fp(monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "personal")
    LogMaskingEngine.reload_from_rows(
        [
            {
                "id": 1,
                "rule_id": "sk_plain",
                "rule_name": "sk",
                "pattern": r"\bsk-[A-Za-z0-9]{8,}\b",
                "replacement": "******",
                "priority": 50,
                "enabled": True,
                "with_fingerprint": False,
            }
        ],
        db_authoritative=True,
    )
    out = LogMaskingEngine.get_instance().sanitize("key sk-abcdefghijklmnop")
    assert "sk-abcdefghijklmnop" not in out
    assert "******" in out
    assert "fp:" not in out


def test_builtin_kv_with_fingerprint_even_in_enterprise(monkeypatch):
    """敏感 KV（with_fingerprint=True）企业版同样附指纹。"""
    from jiuwenswarm.infrastructure.utils import fingerprint

    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    LogMaskingEngine.reset_for_tests()
    secret = "mySecretValue"
    out = LogMaskingEngine.get_instance().sanitize(f"api_key={secret}")
    assert secret not in out
    assert f"******(fp:{fingerprint(secret)})" in out


def test_reload_from_rows_sets_external_flag():
    LogMaskingEngine.reload_from_rows(
        [
            {
                "id": 1,
                "rule_id": "custom_ord",
                "rule_name": "order",
                "pattern": r"ORD-\d+",
                "replacement": "[ORD]",
                "priority": 50,
                "enabled": True,
            }
        ],
        db_authoritative=True,
    )
    engine = LogMaskingEngine.get_instance()
    assert engine.uses_external_rules
    assert engine.sanitize("order ORD-1234567890 shipped") == "order [ORD] shipped"


def test_reload_from_rows_empty_falls_back_to_builtin():
    LogMaskingEngine.reload_from_rows([])
    engine = LogMaskingEngine.get_instance()
    assert not engine.uses_external_rules
    assert "******" in engine.sanitize("user@example.com")


def test_kv_sensitive_pattern_handles_realistic_samples():
    samples = [
        "'CAT_CAFE_CALLBACK_TOKEN': 'secret-value'",
        '{"api_key": "sk-abc", "note": "ok"}',
        'refresh_token: "eyJhbGciOiJIUzI1NiJ9.payload.sig"',
    ]
    for text in samples:
        assert _KV_SENSITIVE_PATTERN.search(text), text


def test_probe_samples_are_non_empty():
    assert len(LOG_MASKING_PROBE_SAMPLES) >= 5


def test_validate_pattern_rejects_unsafe_structures():
    with pytest.raises(
        ValueError,
        match=r"unsafe nested wildcard|too slow",
    ):
        validate_pattern(r"(.*)*")


def test_validate_pattern_allows_simple_custom_pattern():
    assert validate_pattern(r"abc") == "abc"
    assert validate_pattern(r"\b\d{4,6}\b") == r"\b\d{4,6}\b"


@pytest.mark.parametrize(
    ("env_value", "expected"),
    [
        (None, True),
        ("true", True),
        ("false", False),
    ],
)
def test_log_masking_enabled(monkeypatch, env_value, expected):
    from jiuwenswarm.infrastructure.config import Settings

    monkeypatch.delenv("GATEWAY_LOG_MASKING_ENABLED", raising=False)
    if env_value is None:
        monkeypatch.delenv("LOG_MASK_ENABLED", raising=False)
    else:
        monkeypatch.setenv("LOG_MASK_ENABLED", env_value)
    assert Settings().log_masking_enabled is expected


def test_log_masking_enabled_falls_back_to_gateway_env(monkeypatch):
    from jiuwenswarm.infrastructure.config import Settings

    monkeypatch.delenv("LOG_MASK_ENABLED", raising=False)
    monkeypatch.setenv("GATEWAY_LOG_MASKING_ENABLED", "false")
    assert Settings().log_masking_enabled is False


@pytest.mark.parametrize(
    ("env_value", "expected"),
    [
        (None, True),
        ("true", True),
        ("false", False),
    ],
)
def test_log_to_file_enabled(monkeypatch, env_value, expected):
    from jiuwenswarm.infrastructure.config import Settings

    if env_value is None:
        monkeypatch.delenv("LOG_TO_FILE_ENABLED", raising=False)
    else:
        monkeypatch.setenv("LOG_TO_FILE_ENABLED", env_value)
    assert Settings().log_to_file_enabled is expected


@pytest.mark.asyncio
async def test_reload_log_masking_rule_skips_gdb_in_standalone(monkeypatch):
    """单机版不连 GDB，直接回退内置规则。"""
    import jiuwenswarm.infrastructure.log_masking.engine as engine_mod
    from jiuwenswarm.infrastructure.config import Settings

    monkeypatch.setenv("JIUWENSWARM_EDITION", "personal")
    monkeypatch.setattr(engine_mod, "settings", Settings())

    with (
        patch.object(
            LogMaskingEngine,
            "list_enabled_log_masking_rule_rows",
            new_callable=AsyncMock,
        ) as list_rows,
        patch.object(LogMaskingEngine, "reload_from_rows") as reload_from_rows,
    ):
        await LogMaskingEngine.reload_log_masking_rule()

    list_rows.assert_not_called()
    reload_from_rows.assert_called_once_with([])


@pytest.mark.asyncio
async def test_reload_log_masking_rule_reads_gdb_in_enterprise(monkeypatch):
    """企业版 GDB 冷启动只 reload 引擎。"""
    import jiuwenswarm.infrastructure.log_masking.engine as engine_mod
    from jiuwenswarm.infrastructure.config import Settings

    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.setenv("JIUWENCLAW_ID", "sp-test")
    monkeypatch.setattr(engine_mod, "settings", Settings())

    with (
        patch.object(
            LogMaskingEngine,
            "list_enabled_log_masking_rule_rows",
            new_callable=AsyncMock,
            return_value=[],
        ) as list_rows,
        patch.object(LogMaskingEngine, "reload_from_rows") as reload_from_rows,
    ):
        await LogMaskingEngine.reload_log_masking_rule()

    list_rows.assert_awaited_once()
    reload_from_rows.assert_called_once_with([], db_authoritative=False)


@pytest.mark.asyncio
async def test_reload_log_masking_rule_db_authoritative_when_rows_present(monkeypatch):
    import jiuwenswarm.infrastructure.log_masking.engine as engine_mod
    from jiuwenswarm.infrastructure.config import Settings

    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.setenv("JIUWENCLAW_ID", "sp-test")
    monkeypatch.setattr(engine_mod, "settings", Settings())
    rows = [{"rule_id": "r1", "pattern": r"X", "replacement": "Y", "enabled": True}]

    with (
        patch.object(
            LogMaskingEngine,
            "list_enabled_log_masking_rule_rows",
            new_callable=AsyncMock,
            return_value=rows,
        ),
        patch.object(LogMaskingEngine, "reload_from_rows") as reload_from_rows,
    ):
        await LogMaskingEngine.reload_log_masking_rule()

    reload_from_rows.assert_called_once_with(rows, db_authoritative=True)


@pytest.mark.asyncio
async def test_reload_log_masking_rule_loads_gdb_without_jiuwenclaw_id(monkeypatch):
    """企业版不依赖 ``JIUWENCLAW_ID``，直接读本网关 DB。"""
    import jiuwenswarm.infrastructure.log_masking.engine as engine_mod
    from jiuwenswarm.infrastructure.config import Settings

    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.delenv("JIUWENCLAW_ID", raising=False)
    monkeypatch.delenv("JIUWENSWARM_ID", raising=False)
    monkeypatch.setattr(engine_mod, "settings", Settings())
    LogMaskingEngine.reset_for_tests()

    with (
        patch.object(
            LogMaskingEngine,
            "list_enabled_log_masking_rule_rows",
            new_callable=AsyncMock,
            return_value=[],
        ) as list_rows,
        patch.object(LogMaskingEngine, "reload_from_rows") as reload_from_rows,
    ):
        await LogMaskingEngine.reload_log_masking_rule()

    list_rows.assert_awaited_once()
    reload_from_rows.assert_called_once_with([], db_authoritative=False)


@pytest.mark.asyncio
async def test_reload_from_gateway_db_loads_rows(monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.setenv("JIUWENCLAW_ID", "sp-1")
    import jiuwenswarm.infrastructure.log_masking.engine as engine_mod
    from jiuwenswarm.infrastructure.config import Settings

    monkeypatch.setattr(engine_mod, "settings", Settings())
    rows = [
        {
            "id": 2,
            "jiuwenclaw_id": "sp-1",
            "rule_id": "custom_phone",
            "rule_name": "phone",
            "pattern": r"1[3-9]\d{9}",
            "replacement": "[PHONE]",
            "priority": 80,
            "enabled": True,
        }
    ]
    with patch.object(
        LogMaskingEngine,
        "list_enabled_log_masking_rule_rows",
        new_callable=AsyncMock,
        return_value=rows,
    ):
        await LogMaskingEngine.reload_log_masking_rule()
    engine = LogMaskingEngine.get_instance()
    assert engine.uses_external_rules
    assert "[PHONE]" in engine.sanitize("call 13800138000 now")
