# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""security_lists.rail 单测：deny 直达、ask HITL（独立 resume 键）、记住分支、
fail-closed 兜底、会话免重弹、档位逆映射。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import jiuwenswarm.common.config as config_mod
from jiuwenswarm.agents.harness.common.rails.security_lists import audit
from jiuwenswarm.agents.harness.common.rails.security_lists.models import (
    SecurityListRecord,
    SecurityListsCorruptedError,
)
from jiuwenswarm.agents.harness.common.rails.security_lists.rail import (
    SECURITY_LISTS_RESUME_USER_INPUT_KEY,
    SECURITY_LIST_APPROVED_KEY,
    SECURITY_LIST_DENY_KEY,
    UnifiedSecurityListRail,
)


# ---------------------------------------------------------------------------
# 测试基础设施
# ---------------------------------------------------------------------------


class FakeSession:
    session_id = "sess1"

    def __init__(self, state=None):
        self._state = dict(state or {})

    def get_state(self, key):
        return self._state.get(key)

    def update_state(self, mapping):
        self._state.update(mapping)


def make_ctx(tool_name="bash", args=None, *, session=None, tool_call_id="tc1"):
    tool_call = SimpleNamespace(id=tool_call_id, name=tool_name, arguments=args or {})
    return SimpleNamespace(
        inputs=SimpleNamespace(
            tool_name=tool_name,
            tool_args=args or {},
            tool_call=tool_call,
            tool_result=None,
            tool_msg=None,
        ),
        extra={},
        session=session,
        exception=None,
    )


def composer_of(records=None, error=None, defaults=None):
    if error is not None:
        def _raise(*a, **k):
            raise error
        return SimpleNamespace(collect=_raise, defaults=lambda: {})
    return SimpleNamespace(
        collect=lambda *a, **k: list(records or []),
        defaults=lambda: dict(defaults or {}),
    )


def rail_of(records=None, *, error=None, defaults=None):
    return UnifiedSecurityListRail(
        composer=composer_of(records, error=error, defaults=defaults)
    )


def rec(**kw) -> SecurityListRecord:
    base = dict(
        id="r1",
        type="command",
        pattern="curl*",
        match="glob",
        cells={"*": {"*": "ask"}},
        source="user",
    )
    base.update(kw)
    return SecurityListRecord(**base)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """config 读取 + 审计落盘 + 工作区全部隔离到临时目录。"""
    audit_file = tmp_path / "audit.jsonl"
    monkeypatch.setattr(audit, "_audit_file", lambda: audit_file)
    monkeypatch.setattr(
        config_mod,
        "get_config",
        lambda: {"permissions": {"enabled": True, "permission_mode": "strict"}},
    )
    import jiuwenswarm.common.utils as utils_mod

    monkeypatch.setattr(utils_mod, "get_workspace_dir", lambda: tmp_path)
    return SimpleNamespace(audit_file=audit_file)


def read_events(audit_file):
    if not audit_file.is_file():
        return []
    import json

    return [json.loads(line) for line in audit_file.read_text(encoding="utf-8").splitlines() if line.strip()]


def run(rail, ctx):
    asyncio.run(rail.before_tool_call(ctx))
    return ctx


# ---------------------------------------------------------------------------
# 首检：deny / allow / NO_MATCH
# ---------------------------------------------------------------------------


def test_deny_blocks_tool_and_audits(_isolated):
    rail = rail_of([rec(cells={"*": {"*": "deny"}}, source="builtin", id="b1")])
    ctx = make_ctx(args={"command": "curl http://evil.com"})

    run(rail, ctx)

    assert ctx.extra.get("_skip_tool") is True
    assert "SECURITY_LIST_DENIED" in str(ctx.inputs.tool_result)
    assert ctx.extra.get(SECURITY_LIST_DENY_KEY) == ctx.inputs.tool_result
    assert type(ctx.extra.get("_interrupt_decision")).__name__ == "RejectResult"
    events = read_events(_isolated.audit_file)
    assert len(events) == 1
    assert events[0]["kind"] == audit.AUDIT_HIT
    assert events[0]["resolution"] == "deny"
    assert events[0]["hits"][0]["record_id"] == "b1"
    assert events[0]["mode"] == "default"


def test_allow_passthrough(_isolated):
    rail = rail_of([rec(cells={"*": {"*": "allow"}})])
    ctx = make_ctx(args={"command": "curl http://evil.com"})

    run(rail, ctx)

    assert "_skip_tool" not in ctx.extra
    assert read_events(_isolated.audit_file) == []


def test_no_match_passthrough(_isolated):
    rail = rail_of([])
    ctx = make_ctx(args={"command": "curl http://evil.com"})

    run(rail, ctx)

    assert "_skip_tool" not in ctx.extra


def test_no_targets_passthrough(_isolated):
    rail = rail_of([rec()])
    ctx = make_ctx(tool_name="nonexistent_tool", args={})

    run(rail, ctx)

    assert "_skip_tool" not in ctx.extra


def test_mode_isolation_in_rail(_isolated):
    """记录只有 full_access 格：当前 default 档不命中（格子模式隔离）。"""
    rail = rail_of([rec(cells={"full_access": {"*": "deny"}})])
    ctx = make_ctx(args={"command": "curl http://evil.com"})

    run(rail, ctx)

    assert "_skip_tool" not in ctx.extra


# ---------------------------------------------------------------------------
# 首检：ask → HITL（独立 resume 键）
# ---------------------------------------------------------------------------


def test_ask_raises_interrupt_with_isolated_resume_key(_isolated):
    from openjiuwen.core.runner.callback import AbortError

    rail = rail_of([rec(id="r_curl")])
    ctx = make_ctx(args={"command": "curl http://evil.com"})

    with pytest.raises(AbortError) as exc_info:
        asyncio.run(rail.before_tool_call(ctx))

    request = exc_info.value.cause.request
    assert request.metadata["resume_user_input_key"] == SECURITY_LISTS_RESUME_USER_INPUT_KEY
    assert request.metadata["source"] == "security_lists"
    assert request.metadata["hits"][0]["record_id"] == "r_curl"
    assert "curl*" in request.message
    events = read_events(_isolated.audit_file)
    assert [e["resolution"] for e in events] == ["ask"]


def test_ask_auto_confirmed_passthrough(_isolated):
    from openjiuwen.core.single_agent.interrupt.state import INTERRUPT_AUTO_CONFIRM_KEY

    rail = rail_of([rec(id="r_curl")])
    session = FakeSession({INTERRUPT_AUTO_CONFIRM_KEY: {"security_list:r_curl": True}})
    ctx = make_ctx(args={"command": "curl http://evil.com"}, session=session)

    run(rail, ctx)

    assert "_skip_tool" not in ctx.extra


# ---------------------------------------------------------------------------
# fail-closed 兜底
# ---------------------------------------------------------------------------


def test_fail_closed_on_corrupted_store(_isolated):
    rail = rail_of(error=SecurityListsCorruptedError("broken"))
    ctx = make_ctx(args={"command": "curl http://evil.com"})

    run(rail, ctx)

    assert ctx.extra.get("_skip_tool") is True
    assert "fail-closed" in str(ctx.inputs.tool_result)
    events = read_events(_isolated.audit_file)
    assert len(events) == 1
    assert events[0]["kind"] == audit.AUDIT_FALLBACK
    assert "SecurityListsCorruptedError" in events[0]["reason"]


# ---------------------------------------------------------------------------
# 复活分支
# ---------------------------------------------------------------------------


def resume_ctx(payload, *, session=None, args=None):
    ctx = make_ctx(args=args or {"command": "curl http://evil.com"}, session=session)
    ctx.extra[SECURITY_LISTS_RESUME_USER_INPUT_KEY] = {"tc1": payload}
    return ctx


def test_resume_approve_once(_isolated):
    rail = rail_of([rec(id="r_curl")])
    ctx = resume_ctx({"approved": True, "feedback": "", "auto_confirm": False})

    run(rail, ctx)

    assert "_skip_tool" not in ctx.extra
    assert type(ctx.extra.get("_interrupt_decision")).__name__ == "ApproveResult"
    events = read_events(_isolated.audit_file)
    assert [e["resolution"] for e in events] == ["approve_once"]


def test_resume_reject(_isolated):
    rail = rail_of([rec(id="r_curl")])
    ctx = resume_ctx({"approved": False, "feedback": "不允许", "auto_confirm": False})

    run(rail, ctx)

    assert ctx.extra.get("_skip_tool") is True
    assert ctx.inputs.tool_result == "不允许"
    events = read_events(_isolated.audit_file)
    assert [e["resolution"] for e in events] == ["user_reject"]


def test_resume_deny_still_wins(_isolated):
    """确认期间配置变更为 deny：用户批准也不能放行（deny 一票否决）。"""
    rail = rail_of([rec(id="r_curl", cells={"*": {"*": "deny"}}, source="cloud")])
    ctx = resume_ctx({"approved": True, "auto_confirm": False})

    run(rail, ctx)

    assert ctx.extra.get("_skip_tool") is True
    assert "SECURITY_LIST_DENIED" in str(ctx.inputs.tool_result)


def test_resume_invalid_payload_reinterrupts(_isolated):
    from openjiuwen.core.runner.callback import AbortError

    rail = rail_of([rec(id="r_curl")])
    ctx = resume_ctx("not-a-payload")

    with pytest.raises(AbortError):
        asyncio.run(rail.before_tool_call(ctx))


def test_resume_permanent_remember_command(_isolated, monkeypatch):
    from jiuwenswarm.agents.harness.common.rails.permissions import permissions_persist

    captured = {}
    monkeypatch.setattr(
        permissions_persist,
        "persist_merged_allow_rule_snapshot",
        lambda payload: captured.setdefault("payload", payload) is None or True,
    )
    rail = rail_of([rec(id="r_curl")])
    ctx = resume_ctx({"approved": True, "auto_confirm": True, "persist_allow": True})

    run(rail, ctx)

    assert "_skip_tool" not in ctx.extra
    overrides = captured["payload"]["approval_overrides"]
    assert len(overrides) == 1
    entry = overrides[0]
    assert entry["match_type"] == "command"
    assert entry["pattern"] == "curl*"  # glob 记录沿用原 pattern
    assert entry["action"] == "allow"
    assert entry["mode"] == "default"
    assert entry["created_at"]
    events = read_events(_isolated.audit_file)
    kinds = {(e["kind"], e.get("resolution")) for e in events}
    assert (audit.AUDIT_HIT, "permanent_allow") in kinds
    assert (audit.AUDIT_CHANGE, None) in kinds


def test_resume_session_remember_file_path(_isolated, monkeypatch):
    from jiuwenswarm.agents.harness.common.rails.permissions import permissions_persist

    captured = {}
    monkeypatch.setattr(
        permissions_persist,
        "get_permissions_with_session_overlay",
        lambda session_id=None: {},
    )
    monkeypatch.setattr(
        permissions_persist,
        "persist_session_allow_rule",
        lambda permissions, session_id=None: captured.update(
            permissions=permissions, session_id=session_id
        ) is None or True,
    )
    rail = rail_of([
        rec(
            id="r_data",
            type="file_path",
            pattern="C:/data",
            match="prefix",
            cells={"*": {"write": "ask"}},
        )
    ])
    ctx = resume_ctx(
        {"approved": True, "auto_confirm": True, "persist_allow": False},
        session=FakeSession(),
        args={"path": "C:/data/x.txt", "content": "hi"},
    )
    ctx.inputs.tool_name = "write_file"
    ctx.inputs.tool_args = {"path": "C:/data/x.txt", "content": "hi"}
    ctx.inputs.tool_call.name = "write_file"
    ctx.inputs.tool_call.arguments = ctx.inputs.tool_args

    run(rail, ctx)

    assert "_skip_tool" not in ctx.extra
    assert captured["session_id"] == "sess1"
    paths = captured["permissions"]["file_guard"]["paths"]
    assert len(paths) == 1
    entry = paths[0]
    assert entry["path"] == "C:/data"
    assert entry["write"] == "allow"
    assert "read" not in entry  # 只放行的命中轴
    assert entry["mode"] == "default"
    events = read_events(_isolated.audit_file)
    assert any(e.get("resolution") == "session_allow" for e in events)


def test_resume_session_remember_sets_auto_confirm(_isolated, monkeypatch):
    from openjiuwen.core.single_agent.interrupt.state import INTERRUPT_AUTO_CONFIRM_KEY

    from jiuwenswarm.agents.harness.common.rails.permissions import permissions_persist

    monkeypatch.setattr(
        permissions_persist,
        "get_permissions_with_session_overlay",
        lambda session_id=None: {},
    )
    monkeypatch.setattr(
        permissions_persist,
        "persist_session_allow_rule",
        lambda permissions, session_id=None: True,
    )
    rail = rail_of([rec(id="r_curl")])
    session = FakeSession()
    ctx = resume_ctx(
        {"approved": True, "auto_confirm": True, "persist_allow": False},
        session=session,
    )

    run(rail, ctx)

    config = session.get_state(INTERRUPT_AUTO_CONFIRM_KEY)
    assert config.get("security_list:r_curl") is True


def test_resume_domain_remember_falls_back_to_approve_once(_isolated):
    """domain 无审批存储通道：记住请求退化为仅本次放行 + 会话免重弹。"""
    rail = rail_of([
        rec(
            id="r_evil",
            type="domain",
            pattern="evil.com",
            match="exact",
            cells={"*": {"*": "ask"}},
        )
    ])
    ctx = make_ctx(
        tool_name="mcp_fetch_webpage",
        args={"url": "https://evil.com/x"},
        session=FakeSession(),
    )
    ctx.extra[SECURITY_LISTS_RESUME_USER_INPUT_KEY] = {
        "tc1": {"approved": True, "auto_confirm": True, "persist_allow": True}
    }

    run(rail, ctx)

    assert "_skip_tool" not in ctx.extra
    events = read_events(_isolated.audit_file)
    assert any(e.get("resolution") == "approve_once" for e in events)


# ---------------------------------------------------------------------------
# 档位逆映射
# ---------------------------------------------------------------------------


def test_current_permission_profile_mapping():
    from jiuwenswarm.common.permission_profile import current_permission_profile

    assert current_permission_profile({"enabled": False}) == "full_access"
    assert current_permission_profile({"enabled": True, "permission_mode": "strict"}) == "default"
    assert current_permission_profile({"enabled": True, "permission_mode": "normal"}) == "auto_approve"
    assert current_permission_profile({}) == "auto_approve"
    assert current_permission_profile("bad") == "default"


# ---------------------------------------------------------------------------
# 引擎预检归一化（名单结果 × 引擎结果）
# ---------------------------------------------------------------------------


class FakeLevel:
    """最小 PermissionLevel 替身（只用到 .value）。"""

    def __init__(self, value: str) -> None:
        self.value = value


class FakeEngine:
    def __init__(self, level_name: str | None, rule: str = "engine_rule") -> None:
        self.level_name = level_name
        self.rule = rule
        self.calls: list[tuple[str, dict]] = []

    def check_tool_permission_directly(self, tool_name, tool_args):
        self.calls.append((tool_name, dict(tool_args)))
        if self.level_name is None:
            return None, None
        return FakeLevel(self.level_name), self.rule


def rail_with_engine(level_name=None, records=None, *, rule="engine_rule"):
    engine = FakeEngine(level_name, rule)
    rail = UnifiedSecurityListRail(
        composer=composer_of(records),
        engine_provider=lambda: engine,
    )
    return rail, engine


def test_ask_with_engine_deny_blocks_without_prompt(_isolated):
    """归一化：名单 ask + 引擎 deny → 直接拒绝且不弹窗（消除"先问用户再拒绝"）。"""
    rail, engine = rail_with_engine("deny", [rec(id="r_curl")], rule="cmd_hard_deny")
    ctx = make_ctx(args={"command": "curl http://evil.com"})

    run(rail, ctx)  # 未抛 AbortError 即证明没有发起 HITL 弹窗

    assert ctx.extra.get("_skip_tool") is True
    assert "SECURITY_LIST_DENIED" in str(ctx.inputs.tool_result)
    assert "cmd_hard_deny" in str(ctx.inputs.tool_result)
    assert ctx.extra.get(SECURITY_LIST_DENY_KEY) == ctx.inputs.tool_result
    assert type(ctx.extra.get("_interrupt_decision")).__name__ == "RejectResult"
    assert engine.calls == [("bash", {"command": "curl http://evil.com"})]
    events = read_events(_isolated.audit_file)
    assert [e["resolution"] for e in events] == ["engine_deny_precheck"]


def test_ask_with_engine_ask_merges_into_single_prompt(_isolated):
    """归一化：名单 ask + 引擎 ask → 只弹一次，且提示合并引擎规则。"""
    from openjiuwen.core.runner.callback import AbortError

    rail, _engine = rail_with_engine("ask", [rec(id="r_curl")], rule="shell_ask_rule")
    ctx = make_ctx(args={"command": "curl http://evil.com"})

    with pytest.raises(AbortError) as exc_info:
        asyncio.run(rail.before_tool_call(ctx))

    request = exc_info.value.cause.request
    assert request.metadata["source"] == "security_lists"
    assert "权限引擎" in request.message and "shell_ask_rule" in request.message
    events = read_events(_isolated.audit_file)
    assert [e["resolution"] for e in events] == ["ask"]


def test_ask_with_engine_allow_prompts_without_engine_line(_isolated):
    """引擎不拒绝时仍按名单 ask 弹一次，且不附加引擎提示行。"""
    from openjiuwen.core.runner.callback import AbortError

    rail, _engine = rail_with_engine("allow", [rec(id="r_curl")])
    ctx = make_ctx(args={"command": "curl http://evil.com"})

    with pytest.raises(AbortError) as exc_info:
        asyncio.run(rail.before_tool_call(ctx))

    assert "权限引擎" not in exc_info.value.cause.request.message


def test_ask_without_engine_falls_back_to_serial(_isolated):
    """未注入引擎（装配差异）→ 退化为既有串行判定：照常弹窗。"""
    from openjiuwen.core.runner.callback import AbortError

    rail = rail_of([rec(id="r_curl")])
    ctx = make_ctx(args={"command": "curl http://evil.com"})

    with pytest.raises(AbortError):
        asyncio.run(rail.before_tool_call(ctx))


def test_ask_with_engine_error_falls_back_to_serial(_isolated):
    """引擎查询异常 → 退化为串行判定，不因预检失败而中断功能。"""
    from openjiuwen.core.runner.callback import AbortError

    def _boom():
        raise RuntimeError("engine unavailable")

    rail = UnifiedSecurityListRail(
        composer=composer_of([rec(id="r_curl")]), engine_provider=_boom
    )
    ctx = make_ctx(args={"command": "curl http://evil.com"})

    with pytest.raises(AbortError):
        asyncio.run(rail.before_tool_call(ctx))


def test_allow_or_no_match_skips_engine_precheck(_isolated):
    """名单 allow / NO_MATCH 时不做引擎预检（不额外调用引擎）。"""
    engine = FakeEngine("deny")
    rail = UnifiedSecurityListRail(
        composer=composer_of([rec(cells={"*": {"*": "allow"}})]),
        engine_provider=lambda: engine,
    )
    ctx = make_ctx(args={"command": "curl http://evil.com"})

    run(rail, ctx)

    assert engine.calls == []
    assert "_skip_tool" not in ctx.extra


def test_resume_approve_marks_call_for_engine_passthrough(_isolated):
    """用户批准后打 tool_call_id 标记，供权限引擎层免二次弹窗。"""
    rail = rail_of([rec(id="r_curl")])
    ctx = resume_ctx({"approved": True, "feedback": "", "auto_confirm": False})

    run(rail, ctx)

    assert ctx.extra.get(SECURITY_LIST_APPROVED_KEY) == "tc1"


def test_resume_engine_deny_beats_user_approval(_isolated):
    """确认期间引擎规则变严：用户批准也不能击穿引擎 deny，且不打批准标记。"""
    rail, _engine = rail_with_engine(
        "deny", [rec(id="r_curl")], rule="engine_hard_deny"
    )
    ctx = resume_ctx({"approved": True, "auto_confirm": False})

    run(rail, ctx)

    assert ctx.extra.get("_skip_tool") is True
    assert SECURITY_LIST_APPROVED_KEY not in ctx.extra
    events = read_events(_isolated.audit_file)
    assert [e["resolution"] for e in events] == ["engine_deny_precheck"]


# ---------------------------------------------------------------------------
# v3 兜底档（白名单模式）——Verdict.record 为 None 的路径
# ---------------------------------------------------------------------------


def test_default_deny_blocks_unlisted(_isolated):
    """白名单：未列出的命令被兜底档拦下（record 为 None 也不能崩）。"""
    rail = rail_of([], defaults={"*": {"command": "deny"}})
    ctx = make_ctx(args={"command": "curl http://unlisted.example"})

    run(rail, ctx)

    assert ctx.extra.get("_skip_tool") is True
    assert "SECURITY_LIST_DENIED" in str(ctx.inputs.tool_result)
    assert "兜底" in str(ctx.inputs.tool_result)
    events = read_events(_isolated.audit_file)
    assert events[0]["resolution"] == "deny"
    assert events[0]["hits"][0]["source"] == "default"


def test_default_deny_does_not_block_listed_allow(_isolated):
    """白名单核心性质在 rail 层同样成立：命中的 allow 记录不被兜底 deny 抹掉。"""
    rail = rail_of(
        [rec(pattern="curl*", cells={"*": {"*": "allow"}})],
        defaults={"*": {"command": "deny"}},
    )
    ctx = make_ctx(args={"command": "curl http://allowed.example"})

    run(rail, ctx)

    assert "_skip_tool" not in ctx.extra


def test_default_ask_prompts_and_cannot_be_remembered(_isolated, monkeypatch):
    """兜底档没有"记录"可记住：永久记住必须跳过，绝不能写出 pattern='*' 的规则。"""
    from jiuwenswarm.agents.harness.common.rails.permissions import permissions_persist

    captured = {}
    monkeypatch.setattr(
        permissions_persist,
        "persist_merged_allow_rule_snapshot",
        lambda payload: captured.setdefault("payload", payload) is None or True,
    )
    rail = rail_of([], defaults={"*": {"command": "ask"}})
    ctx = resume_ctx({"approved": True, "auto_confirm": False, "persist_allow": True})

    run(rail, ctx)

    assert "payload" not in captured                      # 无对象可持久化
    assert "_skip_tool" not in ctx.extra                  # 用户批准 → 放行
    events = read_events(_isolated.audit_file)
    assert [e["resolution"] for e in events] == ["approve_once"]
