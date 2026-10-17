# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Cross-layer contract tests for the ``plan_entry_source`` literal.

The ``plan_entry_source`` field is a string-literal contract shared between:

- **Backend**: :mod:`jiuwenswarm.common.schema.chat_send` defines
  :data:`PLAN_ENTRY_SOURCES` (the legal values); the
  :func:`AgentWebSocketServer._is_explicit_plan_entry_request` predicate only
  accepts these values as a one-shot "explicit plan entry" marker (防重入闸门).
- **TUI frontend**: ``app-state.ts`` serializes ``pendingPlanEntrySource`` into
  the ``plan_entry_source`` field of ``chat.send`` (via ``core/plan-entry-source.ts``
  constant ``PLAN_ENTRY_SOURCE_SLASH_COMMAND``).
- **Web frontend**: ``useWebSocket.ts`` ``resolvePlanEntryPayload`` emits
  ``plan_entry_source: plan_toggle`` (via
  ``features/planMode/planEntrySource.ts`` constant
  ``PLAN_ENTRY_SOURCE_PLAN_TOGGLE``).

There is no shared schema (Python <-> TS). 本模块通过正则解析两端 .ts 文件的
``export const PLAN_ENTRY_SOURCE_*`` 字面量，与后端 Python 常量做 ``==`` 比对，
确保 Python 端 rename 会在 pytest 里直接 fail（前端断裂可感知）。TS 端运行时
行为由 ``run-tests.mjs`` / ``test:wire-mode`` 覆盖；本模块只做跨文件字面量比对。
"""

# pylint: disable=protected-access

from __future__ import annotations

import re
from pathlib import Path

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.chat_send import (
    PLAN_ENTRY_SOURCES,
    PLAN_ENTRY_SOURCE_PLAN_TOGGLE,
    PLAN_ENTRY_SOURCE_SLASH_COMMAND,
)
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_TUI_TS = _REPO_ROOT / "jiuwenswarm" / "channels" / "tui" / "frontend" / "src" / "core" / "plan-entry-source.ts"
_WEB_TS = _REPO_ROOT / "jiuwenswarm" / "channels" / "web" / "frontend" / "src" / "features" / "planMode" / "planEntrySource.ts"


def _extract_ts_const(path: Path, name: str) -> str:
    """从 .ts 文件正则抽 ``export const NAME = "literal" as const`` 的字面量。

    兼容 TUI 双引号与 Web 单引号；``as const`` 后缀可选。
    """
    text = path.read_text(encoding="utf-8")
    pattern = rf"export const {re.escape(name)}\s*=\s*['\"]([^'\"]+)['\"]"
    m = re.search(pattern, text)
    assert m is not None, f"{name} 未在 {path} 找到（export const 字面量）"
    return m.group(1)


def _request(params: dict, channel_id: str = "web") -> AgentRequest:
    return AgentRequest(
        request_id="req-contract",
        channel_id=channel_id,
        session_id="s-contract",
        params=params,
    )


# ── 后端常量值与前端 .ts 字面量跨文件契约 ───────────────────────────────────


def test_slash_command_literal_matches_tui_ts() -> None:
    """``PLAN_ENTRY_SOURCE_SLASH_COMMAND`` 必须与 TUI .ts 字面量一致。

    解析 TUI ``core/plan-entry-source.ts`` 的
    ``export const PLAN_ENTRY_SOURCE_SLASH_COMMAND`` 字面量，与后端 Python
    常量 ``==`` 比对。改 Python 端字面量而不同步改 TS 端会在本断言直接 fail
    （否则防重入闸门对 TUI ``/plan`` 失效）。
    """
    tui_literal = _extract_ts_const(_TUI_TS, "PLAN_ENTRY_SOURCE_SLASH_COMMAND")
    assert tui_literal == PLAN_ENTRY_SOURCE_SLASH_COMMAND


def test_plan_toggle_literal_matches_web_ts() -> None:
    """``PLAN_ENTRY_SOURCE_PLAN_TOGGLE`` 必须与 Web .ts 字面量一致。

    解析 Web ``features/planMode/planEntrySource.ts`` 的
    ``export const PLAN_ENTRY_SOURCE_PLAN_TOGGLE`` 字面量，与后端 Python
    常量 ``==`` 比对。改 Python 端字面量而不同步改 TS 端会在本断言直接 fail
    （否则防重入闸门对 Web 手动打开 Plan 开关失效）。
    """
    web_literal = _extract_ts_const(_WEB_TS, "PLAN_ENTRY_SOURCE_PLAN_TOGGLE")
    assert web_literal == PLAN_ENTRY_SOURCE_PLAN_TOGGLE


def test_tui_ts_also_pins_plan_toggle_for_cross_end_audit() -> None:
    """TUI .ts 也声明 ``PLAN_ENTRY_SOURCE_PLAN_TOGGLE``（跨端对齐用）。

    TUI 实际不产出 ``plan_toggle``，但常量对齐 Web/后端。本断言保证 TUI 端
    ``PLAN_ENTRY_SOURCE_PLAN_TOGGLE`` 字面量与后端一致，避免跨端审计时断裂。
    """
    tui_literal = _extract_ts_const(_TUI_TS, "PLAN_ENTRY_SOURCE_PLAN_TOGGLE")
    assert tui_literal == PLAN_ENTRY_SOURCE_PLAN_TOGGLE


def test_web_ts_also_pins_slash_command_for_cross_end_audit() -> None:
    """Web .ts 也声明 ``PLAN_ENTRY_SOURCE_SLASH_COMMAND``（跨端对齐用）。

    Web 实际不产出 ``slash_command``，但常量对齐 TUI/后端。本断言保证 Web 端
    ``PLAN_ENTRY_SOURCE_SLASH_COMMAND`` 字面量与后端一致，避免跨端审计时断裂。
    """
    web_literal = _extract_ts_const(_WEB_TS, "PLAN_ENTRY_SOURCE_SLASH_COMMAND")
    assert web_literal == PLAN_ENTRY_SOURCE_SLASH_COMMAND


def test_plan_entry_sources_set_contains_both_literals() -> None:
    """``PLAN_ENTRY_SOURCES`` 必须覆盖两个已知 entry source，不多不少。"""
    assert PLAN_ENTRY_SOURCES == frozenset(
        {PLAN_ENTRY_SOURCE_SLASH_COMMAND, PLAN_ENTRY_SOURCE_PLAN_TOGGLE}
    )


# ── 后端防重入闸门行为契约 ────────────────────────────────────────────────────


@pytest.mark.parametrize("source", ["slash_command", "plan_toggle"])
def test_explicit_plan_entry_accepts_known_sources(source: str) -> None:
    """``_is_explicit_plan_entry_request`` 只对已知字面量返 True。

    覆盖 TUI ``/plan``（``slash_command``）与 Web 手动打开开关
    （``plan_toggle``）两条路径。
    """
    request = _request({"plan_entry_source": source})

    assert AgentWebSocketServer._is_explicit_plan_entry_request(request) is True


@pytest.mark.parametrize("source", ["unknown", "", "SLASH_COMMAND", "slash"])
def test_explicit_plan_entry_rejects_unknown_sources(source: str) -> None:
    """未知字面量、空串、大小写偏差都不得通过闸门。

    防止"只要是 plan 请求就当成显式进入"——否则 ``plan.mode_exited`` 丢包时，
    用户下一条消息会被静默拖回 plan。
    """
    request = _request({"plan_entry_source": source})

    assert AgentWebSocketServer._is_explicit_plan_entry_request(request) is False


def test_explicit_plan_entry_rejects_missing_field() -> None:
    """``plan_entry_source`` 缺失时不得视为显式进入。"""
    request = _request({"mode": "agent.plan"})

    assert AgentWebSocketServer._is_explicit_plan_entry_request(request) is False


def test_explicit_plan_entry_rejects_non_dict_params() -> None:
    """``request.params`` 不是 dict 时（None / list）不得视为显式进入。"""
    request_none = _request(None)  # type: ignore[arg-type]
    request_list = _request(["slash_command"])  # type: ignore[list-item]

    assert AgentWebSocketServer._is_explicit_plan_entry_request(request_none) is False
    assert AgentWebSocketServer._is_explicit_plan_entry_request(request_list) is False


# ── 后端集合与前端常量同名字面量契约（文档化 + 静态断言） ─────────────────────


def test_backend_set_is_superset_of_frontend_known_literals() -> None:
    """后端 ``PLAN_ENTRY_SOURCES`` 必须包含前端两端各自产出的字面量。

    - TUI 端只产出 ``slash_command``（``PLAN_ENTRY_SOURCE_SLASH_COMMAND``）。
    - Web 端只产出 ``plan_toggle``（``PLAN_ENTRY_SOURCE_PLAN_TOGGLE``）。

    若前端新增第三种 entry source，后端集合必须同步扩；本断言守住"前端产出
    的字面量必然被后端认得"的契约。
    """
    tui_known = {PLAN_ENTRY_SOURCE_SLASH_COMMAND}
    web_known = {PLAN_ENTRY_SOURCE_PLAN_TOGGLE}

    assert tui_known.issubset(PLAN_ENTRY_SOURCES)
    assert web_known.issubset(PLAN_ENTRY_SOURCES)
