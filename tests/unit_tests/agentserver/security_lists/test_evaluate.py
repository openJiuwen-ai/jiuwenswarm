# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""security_lists.evaluate 单测：deny 跨来源优先、固定分层、同源最严、模式隔离。"""
from __future__ import annotations

from types import SimpleNamespace

from jiuwenswarm.agents.harness.common.rails.security_lists.evaluate import evaluate
from jiuwenswarm.agents.harness.common.rails.security_lists.models import (
    SecurityListRecord,
)


def rec(pattern="C:/data", *, source="user", cells=None, list_type="file_path",
        match="prefix", enabled=True, rid="r1") -> SecurityListRecord:
    return SecurityListRecord(
        id=rid, type=list_type, pattern=pattern, match=match, enabled=enabled,
        cells=cells if cells is not None else {"*": {"read": "allow"}}, source=source,
    )


def composer_of(records, defaults=None):
    return SimpleNamespace(
        collect=lambda *a, **k: list(records),
        defaults=lambda: dict(defaults or {}),
    )


def run(records, *, mode="default", op="read", target="C:/data/sub/f.txt",
        list_type="file_path", defaults=None):
    return evaluate(
        list_type, target, op=op, mode=mode,
        composer=composer_of(records, defaults),
    )


# ---------------------------------------------------------------------------
# deny 全局一票否决
# ---------------------------------------------------------------------------


def test_deny_from_any_source_wins_over_allow():
    records = [
        rec(source="user_approval", cells={"*": {"read": "allow"}}, rid="a"),
        rec(source="user", cells={"*": {"read": "allow"}}, rid="b"),
        rec(source="cloud", cells={"*": {"read": "deny"}}, rid="c"),
    ]
    verdict = run(records)
    assert verdict is not None and verdict.action == "deny"
    assert verdict.record.id == "c" and verdict.source == "cloud"


def test_builtin_deny_beats_user_approval_allow():
    records = [
        rec(source="user_approval", cells={"*": {"read": "allow"}}, rid="a"),
        rec(source="builtin", cells={"*": {"read": "deny", "write": "deny", "exec": "deny"}}, rid="b"),
    ]
    verdict = run(records)
    assert verdict is not None and verdict.action == "deny" and verdict.source == "builtin"


# ---------------------------------------------------------------------------
# 固定分层 user_approval > user > builtin > cloud
# ---------------------------------------------------------------------------


def test_user_approval_layer_beats_user_layer():
    records = [
        rec(source="user", cells={"*": {"read": "ask"}}, rid="u"),
        rec(source="user_approval", cells={"*": {"read": "allow"}}, rid="ua"),
    ]
    verdict = run(records)
    assert verdict.action == "allow" and verdict.source == "user_approval"


def test_user_layer_beats_builtin_layer():
    records = [
        rec(source="builtin", cells={"*": {"read": "allow"}}, rid="b"),
        rec(source="user", cells={"*": {"read": "ask"}}, rid="u"),
    ]
    verdict = run(records)
    assert verdict.action == "ask" and verdict.source == "user"


def test_builtin_layer_beats_cloud_layer():
    records = [
        rec(source="cloud", cells={"*": {"read": "ask"}}, rid="c"),
        rec(source="builtin", cells={"*": {"read": "allow"}}, rid="b"),
    ]
    verdict = run(records)
    assert verdict.action == "allow" and verdict.source == "builtin"


def test_cloud_only():
    verdict = run([rec(source="cloud", cells={"*": {"read": "ask"}})])
    assert verdict.action == "ask" and verdict.source == "cloud"


def test_same_layer_strictest_wins():
    records = [
        rec(source="user", cells={"*": {"read": "allow"}}, rid="u1"),
        rec(source="user", cells={"*": {"read": "ask"}}, rid="u2"),
    ]
    verdict = run(records)
    assert verdict.action == "ask" and verdict.record.id == "u2"


# ---------------------------------------------------------------------------
# 模式隔离（格子语义经 resolve_cell）
# ---------------------------------------------------------------------------


def test_mode_specific_cell_only_applies_in_its_mode():
    records = [rec(cells={"full_access": {"read": "deny"}})]
    assert run(records, mode="default") is None
    assert run(records, mode="auto_approve") is None
    verdict = run(records, mode="full_access")
    assert verdict is not None and verdict.action == "deny"


def test_general_cell_applies_to_unspecialized_modes():
    records = [rec(cells={"*": {"read": "ask"}})]
    for mode in ("default", "auto_approve", "full_access", "*"):
        verdict = run(records, mode=mode)
        assert verdict is not None and verdict.action == "ask", mode


def test_approval_mode_cell_filtered_by_current_mode():
    records = [rec(source="user_approval", cells={"default": {"read": "allow"}})]
    assert run(records, mode="auto_approve") is None
    verdict = run(records, mode="default")
    assert verdict is not None and verdict.action == "allow"


def test_legacy_modeless_approval_is_global():
    records = [rec(source="user_approval", cells={"*": {"read": "allow"}})]
    for mode in ("default", "full_access"):
        verdict = run(records, mode=mode)
        assert verdict is not None and verdict.action == "allow", mode


def test_new_mode_inherits_general_cell():
    records = [rec(cells={"*": {"read": "deny"}})]
    # 未来新增模式（未特化）回落通用格
    verdict = run(records, mode="some_future_mode")
    assert verdict is not None and verdict.action == "deny"


# ---------------------------------------------------------------------------
# 其他
# ---------------------------------------------------------------------------


def test_no_match_returns_none():
    assert run([rec(pattern="D:/other")]) is None
    assert run([]) is None


def test_disabled_record_skipped():
    assert run([rec(enabled=False, cells={"*": {"read": "deny"}})]) is None


def test_record_without_opinion_skipped():
    # 命中 pattern 但 cells 在当前 mode/op 下无表态 → NO_MATCH
    assert run([rec(cells={"default": {"write": "deny"}})], op="read") is None


# ---------------------------------------------------------------------------
# v3 兜底档 defaults（白名单模式）
# ---------------------------------------------------------------------------


def test_default_used_when_nothing_matches():
    verdict = run([], defaults={"*": {"file_path": "deny"}})
    assert verdict is not None
    assert verdict.action == "deny" and verdict.source == "default"
    assert verdict.record is None      # 兜底档不是记录，没有可"记住"的对象


def test_default_does_not_override_matching_allow():
    """白名单核心性质：命中 allow 记录时兜底 deny **不生效**。

    若退化成"兜底 deny 一票否决"，白名单会变成全拒。
    """
    verdict = run(
        [rec(pattern="C:/data", cells={"*": {"read": "allow"}})],
        defaults={"*": {"file_path": "deny"}},
    )
    assert verdict is not None and verdict.action == "allow"
    assert verdict.source == "user"


def test_matching_deny_beats_default_allow():
    verdict = run(
        [rec(pattern="C:/data", cells={"*": {"read": "deny"}})],
        defaults={"*": {"file_path": "allow"}},
    )
    assert verdict is not None and verdict.action == "deny"


def test_no_defaults_keeps_no_match():
    assert run([]) is None
    assert run([], defaults={}) is None


def test_default_is_mode_scoped():
    defaults = {"full_access": {"file_path": "deny"}}
    assert run([], mode="default", defaults=defaults) is None
    verdict = run([], mode="full_access", defaults=defaults)
    assert verdict is not None and verdict.action == "deny"
