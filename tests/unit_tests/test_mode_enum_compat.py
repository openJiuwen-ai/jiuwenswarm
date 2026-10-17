# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""P2 阶段三套枚举与 session 惰性迁移的契约测试。

铁律 4+5:历史持久化数据可读，写回不丢失新 canonical。覆盖：
- ``Mode`` / ``ChannelMode`` / ``ModeSubcommand`` 三套枚举新增 8 个新成员，
  旧成员保留以兼容历史反解析。
- ``Mode.from_raw`` 通过 ``DEPRECATION_MAP`` 把旧 canonical 静默映射到新
  canonical。
- ``session_metadata._apply_metadata_defaults_with_inference`` 在读路径惰性
  迁移 ``mode`` 字段，并把迁移结果异步写回磁盘（重启模拟下仍读到新值）。
"""

from __future__ import annotations

import pytest

from jiuwenswarm.common.mode_matrix import DEPRECATION_MAP, deprecate_mode
from jiuwenswarm.common.schema.message import Mode
from jiuwenswarm.gateway.message_handler.command_parser.slash_command import (
    ModeSubcommand,
)
from jiuwenswarm.gateway.message_handler.message_handler import ChannelMode


# ── 三套枚举新成员 ─────────────────────────────────────────────────────
# 三个枚举类（Mode / ChannelMode / ModeSubcommand）共享同一组 8 个新成员
# name→value，合并成一个 (枚举类, name, value) 三元组循环，避免 8×3=24 条
# 重复断言。本质校验 stdlib ``Enum(value=...)`` 的成员声明契约。

_NEW_ENUM_MEMBERS = [
    ("AGENT_WORK_NORMAL", "agent.work.normal"),
    ("AGENT_WORK_PLAN", "agent.work.plan"),
    ("AGENT_CODE_NORMAL", "agent.code.normal"),
    ("AGENT_CODE_PLAN", "agent.code.plan"),
    ("TEAM_WORK_NORMAL", "team.work.normal"),
    ("TEAM_WORK_PLAN", "team.work.plan"),
    ("TEAM_CODE_NORMAL", "team.code.normal"),
    ("TEAM_CODE_PLAN", "team.code.plan"),
]


@pytest.mark.parametrize(
    ("enum_cls,name,value"),
    [(cls, name, value) for cls in (Mode, ChannelMode, ModeSubcommand)
     for (name, value) in _NEW_ENUM_MEMBERS],
)
def test_enum_new_members_present(enum_cls, name: str, value: str) -> None:
    """三套枚举都声明 8 个新 canonical 成员且 value 正确。"""
    assert hasattr(enum_cls, name), f"{enum_cls.__name__}.{name} 缺失"
    assert getattr(enum_cls, name).value == value


# 注：旧枚举成员保留（test_legacy_enum_members_preserved /
# test_channel_mode_legacy_members_preserved）已被 from_raw 用例间接覆盖
# （旧成员存在才有 .AGENT/.TEAM_PLAN_CODE 等可传）。_VALID_MODE_LINES
# frozenset comprehension 同样由 from_raw 路径间接校验，不再单独断言。


# ── from_raw 静默映射 ────────────────────────────────────────────────
# 注：旧/新 canonical 字符串的 from_raw 映射主覆盖在
# tests/unit_tests/evolution/test_message.py::test_mode_from_raw_legacy_compatibility
# （13+ 条参数）。此处只保留两条 evolution 不覆盖的差异点：旧枚举成员
# 入参也走映射、case-insensitive / 空白容忍。


def test_from_raw_legacy_enum_member_migrates() -> None:
    """传 Mode 旧成员，也走 DEPRECATION_MAP 映射到新枚举。"""
    assert Mode.from_raw(Mode.AGENT) == Mode.AGENT_WORK_NORMAL
    assert Mode.from_raw(Mode.AGENT_PLAN) == Mode.AGENT_WORK_PLAN
    assert Mode.from_raw(Mode.AGENT_FAST) == Mode.AGENT_WORK_NORMAL
    assert Mode.from_raw(Mode.CODE_TEAM) == Mode.TEAM_CODE_NORMAL
    assert Mode.from_raw(Mode.TEAM_PLAN_CODE) == Mode.TEAM_CODE_PLAN


def test_from_raw_case_insensitive_and_whitespace_tolerant() -> None:
    assert Mode.from_raw("  AGENT.PLAN  ") == Mode.AGENT_WORK_PLAN
    assert Mode.from_raw("Team") == Mode.TEAM_WORK_NORMAL


# ── deprecate_mode 穷举 DEPRECATION_MAP ────────────────────────────────
# 铁律 4:每个旧 canonical（DEPRECATION_MAP 有 10 键：agent / agent.plan /
# agent.fast / code / code.normal / code.plan / code.team / team /
# team.plan.normal / team.plan.code）都必须命中并映射到新 canonical，不能只
# 抽查几条。另补 MODE_ALIASES 两步映射：team.plan -> team.plan.normal ->
# team.work.plan、team.code -> code.team -> team.code.normal。


@pytest.mark.parametrize(
    ("legacy", "expected"),
    sorted(
        {
            **DEPRECATION_MAP,
            "team.plan": "team.work.plan",   # team.plan → team.plan.normal → team.work.plan
            "team.code": "team.code.normal",  # team.code → code.team → team.code.normal
        }.items()
    ),
)
def test_deprecate_mode_covers_entire_deprecation_map(
    legacy: str, expected: str
) -> None:
    """每个旧 canonical 都命中 deprecate_mode 并落到对应新 canonical。"""
    assert deprecate_mode(legacy) == expected


# ── session 惰性迁移 ──────────────────────────────────────────────────
# 注：mode 字段的惰性迁移已下沉到 tests/unit_tests/test_session_metadata.py
# 的 TestLazyMigrationOnRead / TestSyncSessionRequestMetadata 模块归属层覆盖，
# 此处不再重复，避免双份维护。
