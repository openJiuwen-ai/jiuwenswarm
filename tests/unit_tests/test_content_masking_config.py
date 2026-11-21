# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""企业版内容脱敏配置：下发覆盖、删除回退、本端只读与 poll 应用。"""

from __future__ import annotations

import pytest

import jiuwenswarm.gateway.channel_manager.web.sensitive_mask as mask
from jiuwenswarm.gateway.config_poll import appliers
from jiuwenswarm.gateway.config_poll.appliers import TableApplyContext

_YAML_RULES = [{"title": "YAML", "pattern": r"\d{4}"}]


def _yaml_config(enabled: bool) -> dict:
    return {"sensitive_mask": {"enabled": enabled, "rules": list(_YAML_RULES)}}


@pytest.fixture(autouse=True)
def _reset_enterprise_state(monkeypatch):
    monkeypatch.setattr(mask, "_ENTERPRISE_CONFIG", None)
    monkeypatch.setattr(mask, "_ENTERPRISE_AUTHORITATIVE", False)
    mask.reset_sensitive_mask_state()
    yield
    mask.reset_sensitive_mask_state()


def test_enterprise_row_overrides_and_delete_falls_back(monkeypatch) -> None:
    monkeypatch.setattr(mask, "is_enterprise", lambda: True)
    monkeypatch.setattr(mask, "get_merged_config_dict", lambda: _yaml_config(False))

    mask.apply_content_masking_row(
        {"enabled": True, "rules": [{"title": "DB", "pattern": "abc"}]}
    )
    assert mask.get_sensitive_mask_config() == {
        "enabled": True,
        "rules": [{"title": "DB", "pattern": "abc"}],
    }

    mask.apply_content_masking_row(None)
    assert mask.get_sensitive_mask_config() == {
        "enabled": False,
        "rules": _YAML_RULES,
    }


def test_enterprise_row_accepts_json_string(monkeypatch) -> None:
    monkeypatch.setattr(mask, "is_enterprise", lambda: True)
    monkeypatch.setattr(mask, "get_merged_config_dict", lambda: _yaml_config(False))

    mask.apply_content_masking_row(
        {"enabled": True, "rules": '[{"title": "DB", "pattern": "abc"}]'}
    )
    assert mask.get_sensitive_mask_config() == {
        "enabled": True,
        "rules": [{"title": "DB", "pattern": "abc"}],
    }


def test_personal_ignores_enterprise_row(monkeypatch) -> None:
    monkeypatch.setattr(mask, "is_enterprise", lambda: False)
    monkeypatch.setattr(mask, "get_merged_config_dict", lambda: _yaml_config(True))

    mask.apply_content_masking_row({"enabled": False, "rules": []})
    assert mask.get_sensitive_mask_config() == {
        "enabled": True,
        "rules": _YAML_RULES,
    }


def test_enterprise_set_conf_is_read_only(monkeypatch) -> None:
    monkeypatch.setattr(mask, "is_enterprise", lambda: True)

    with pytest.raises(mask.SensitiveMaskConfigReadOnly):
        mask.set_sensitive_mask_config(True, [])


async def test_poll_applier_forwards_row(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(
        mask,
        "apply_content_masking_row",
        lambda row: captured.setdefault("row", row),
    )

    await appliers.apply_content_masking_config_table(
        TableApplyContext(rows=[{"enabled": True, "rules": []}])
    )

    assert captured["row"] == {"enabled": True, "rules": []}


def test_rule_count_limit() -> None:
    with pytest.raises(ValueError):
        mask.validate_sensitive_mask_rules(
            [{"title": f"r{i}", "pattern": "a"} for i in range(33)]
        )


def test_nested_quantifier_rejected_only_in_strict_mode() -> None:
    rules = [{"title": "redos", "pattern": "(a+)+"}]
    # 个人版存量规则校验保持原样，不被新增的严格模式误杀。
    mask.validate_sensitive_mask_rules(rules)
    with pytest.raises(ValueError, match="正则可能卡死"):
        mask.validate_sensitive_mask_rules(rules, strict=True)


def test_strict_mode_keeps_common_patterns() -> None:
    rules = [
        {"title": "邮箱", "pattern": r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[A-Za-z]{2,}\b"},
        {"title": "手机号", "pattern": r"(?<!\d)1[3-9]\d{9}(?!\d)"},
        {"title": "API Key", "pattern": r"(?:sk|rk|pk|ak)-[A-Za-z0-9]{8,}"},
        # 字符类里的 + 不是量词，严格模式不应误杀。
        {"title": "符号", "pattern": r"([+*-])+"},
    ]
    assert mask.validate_sensitive_mask_rules(rules, strict=True) == rules


def test_select_row_prefers_largest_revision() -> None:
    row = mask.select_content_masking_row(
        [
            {"id": 1, "revision": 1, "enabled": False, "rules": []},
            {"id": 2, "revision": 3, "enabled": True, "rules": []},
        ]
    )
    assert row is not None
    assert row["id"] == 2
