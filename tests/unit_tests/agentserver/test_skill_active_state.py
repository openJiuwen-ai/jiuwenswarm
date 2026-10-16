# coding: utf-8
# pylint: disable=protected-access
"""Tests for skill active-state lifecycle: activation, switch, completion, teardown."""

import asyncio
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    InvokeInputs,
    ToolCallInputs,
)

from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
    is_interrupt_resume_source,
)
from jiuwenswarm.agents.harness.common.rails.skill_active_state import (
    _DEFAULT_SESSION_ID,
    _SESSION_ID_EXTRA_KEY,
    SkillActiveStateRail,
    clear_session_skill_state,
    get_session_active_skill,
    resolve_skill_session_id,
    resolve_stale_invoke_limit,
)
from jiuwenswarm.agents.harness.common.rails.skill_credential_injection_rail import (
    SkillCredentialInjectionRail,
)


@pytest.fixture(autouse=True)
def session_files(tmp_path, monkeypatch):
    import re

    from jiuwenswarm.agents.harness.common.rails import skill_active_state
    from jiuwenswarm.agents.harness.common.rails.permissions.skill_authorization import (
        file_store,
    )

    context_token = file_store._session_file_context.set(None)
    sessions = tmp_path / "sessions"
    # Existing lifecycle cases use these session identifiers as real sessions.
    for sid in set(re.findall(r'officeclaw_[A-Za-z0-9_]+', Path(__file__).read_text())):
        (sessions / sid).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(file_store, "get_agent_sessions_dir", lambda: sessions)
    monkeypatch.setattr(file_store, "get_user_workspace_dir", lambda: tmp_path)
    token = skill_active_state._current_session_var.set(None)
    yield
    skill_active_state._current_session_var.reset(token)
    file_store._session_file_context.reset(context_token)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "corruption"])
async def test_state_write_failure_stops_callback_chain(monkeypatch, failure):
    from openjiuwen.core.runner.callback.errors import AbortError
    from openjiuwen.core.runner.callback.framework import AsyncCallbackFramework

    from jiuwenswarm.agents.harness.common.rails import skill_active_state

    sid = "officeclaw_storage_failure"
    store = skill_active_state._shared_store(sid)
    store.update_active_skill_state(
        sid, lambda _: {"active_skill": "old", "invokes_since_skill_tool": 5}
    )
    monkeypatch.setattr(skill_active_state, "_shared_store", lambda _sid: store)
    if failure == "corruption":
        (store.session_directory(sid) / ".skill_authorization/state.json").write_text("{")
    else:
        def fail_write(*_args, **_kwargs):
            raise TimeoutError("injected lock timeout")
        monkeypatch.setattr(store, "update_active_skill_state", fail_write)

    rail = SkillActiveStateRail(session_id=sid, sessions_root=store.sessions_root())
    ctx = AgentCallbackContext(agent=MagicMock())
    ctx.inputs = InvokeInputs(query="next turn", conversation_id=sid)
    ctx.extra = {}
    later_callback = []

    async def use_credentials(_ctx):
        later_callback.append(True)

    callbacks = AsyncCallbackFramework()
    await callbacks.register("before_invoke", rail.before_invoke, priority=10)
    await callbacks.register("before_invoke", use_credentials, priority=0)
    with pytest.raises(AbortError, match="Skill state storage unavailable"):
        await callbacks.trigger("before_invoke", ctx)
    assert not later_callback


@pytest.mark.asyncio
async def test_missing_session_callback_does_not_recreate_directory(monkeypatch, caplog):
    import shutil

    from jiuwenswarm.agents.harness.common.rails import skill_active_state

    sid = "officeclaw_missing_session"
    store = skill_active_state._shared_store(sid)
    directory = store.session_directory(sid)
    shutil.rmtree(directory)
    monkeypatch.setattr(skill_active_state, "_shared_store", lambda _sid: store)
    rail = SkillActiveStateRail(session_id=sid, sessions_root=store.sessions_root())
    ctx = AgentCallbackContext(agent=MagicMock())
    ctx.inputs = InvokeInputs(query="late request", conversation_id=sid)
    ctx.extra = {}
    await rail.before_invoke(ctx)
    assert not directory.exists()
    assert not [record for record in caplog.records if record.levelno >= 40]


class TestResolveSkillSessionId(unittest.TestCase):
    def _ctx(self, *, conversation_id=None, session_id=None, extra=None):
        ctx = AgentCallbackContext(agent=MagicMock())
        inputs = ToolCallInputs(
            tool_call=MagicMock(),
            tool_name="bash",
            tool_args={"command": "echo hi"},
            tool_result=None,
            tool_msg=None,
        )
        if conversation_id is not None:
            inputs.conversation_id = conversation_id
        ctx.inputs = inputs
        if session_id is not None:
            session = MagicMock()
            session.get_session_id.return_value = session_id
            ctx.session = session
        if extra is not None:
            ctx.extra = extra
        else:
            ctx.extra = {}
        return ctx

    def test_preset_wins(self):
        ctx = self._ctx(conversation_id="from-inputs")
        assert resolve_skill_session_id(ctx, "officeclaw_preset") == "officeclaw_preset"

    def test_inputs_conversation_id(self):
        ctx = self._ctx(conversation_id="officeclaw_from_inputs")
        assert resolve_skill_session_id(ctx) == "officeclaw_from_inputs"

    def test_session_object(self):
        ctx = self._ctx(session_id="officeclaw_from_session")
        assert resolve_skill_session_id(ctx) == "officeclaw_from_session"

    def test_ctx_extra_shared_key(self):
        ctx = self._ctx(extra={_SESSION_ID_EXTRA_KEY: "officeclaw_from_extra"})
        assert resolve_skill_session_id(ctx) == "officeclaw_from_extra"

    def test_fallback_default(self):
        ctx = self._ctx()
        assert resolve_skill_session_id(ctx) == _DEFAULT_SESSION_ID


class TestInterruptResumeSource(unittest.TestCase):
    def test_interrupt_sources(self):
        assert is_interrupt_resume_source("permission_interrupt")
        assert is_interrupt_resume_source("confirm_interrupt")
        assert is_interrupt_resume_source("ask_user_interrupt")
        # Evolution / forward-compat sources are out of OCR/HITL scope.
        assert not is_interrupt_resume_source("evolution_interrupt")
        assert not is_interrupt_resume_source("skill_evolution_approval")
        assert not is_interrupt_resume_source("custom_interrupt")
        assert not is_interrupt_resume_source("user")
        assert not is_interrupt_resume_source("")


class TestSkillActiveStateRailPreset(unittest.TestCase):
    def tearDown(self):
        clear_session_skill_state("officeclaw_preset_sess")
        clear_session_skill_state(_DEFAULT_SESSION_ID)

    def _activate_ctx(self, skill_name="hwocr"):
        ctx = AgentCallbackContext(agent=MagicMock())
        tool_call = MagicMock()
        tool_call.arguments = {"skill_name": skill_name}
        tool_msg = MagicMock()
        tool_msg.metadata = {"skill_name": skill_name}
        inputs = ToolCallInputs(
            tool_call=tool_call,
            tool_name="skill_tool",
            tool_args={"skill_name": skill_name},
            tool_result=None,
            tool_msg=tool_msg,
        )
        ctx.inputs = inputs
        ctx.extra = {}
        return ctx

    def test_preset_session_used_when_tool_inputs_lack_conversation_id(self):
        rail = SkillActiveStateRail(session_id="officeclaw_preset_sess")
        ctx = self._activate_ctx()
        asyncio.run(rail.after_tool_call(ctx))
        assert get_session_active_skill("officeclaw_preset_sess") == "hwocr"
        assert get_session_active_skill(_DEFAULT_SESSION_ID) is None

    def test_before_invoke_binds_extra(self):
        rail = SkillActiveStateRail(session_id="officeclaw_preset_sess")
        ctx = self._activate_ctx()
        ctx.inputs = InvokeInputs(query="hello", conversation_id="officeclaw_preset_sess")
        asyncio.run(rail.before_invoke(ctx))
        assert ctx.extra[_SESSION_ID_EXTRA_KEY] == "officeclaw_preset_sess"


class TestSkillActiveLifecycle(unittest.TestCase):
    sid = "officeclaw_hitl_sess"

    def tearDown(self):
        clear_session_skill_state(self.sid)

    def _rail(self):
        return SkillActiveStateRail(session_id=self.sid)

    def _activate(self, rail, skill_name="hwocr"):
        ctx = AgentCallbackContext(agent=MagicMock())
        tool_call = MagicMock()
        tool_call.arguments = {"skill_name": skill_name}
        tool_msg = MagicMock()
        tool_msg.metadata = {"skill_name": skill_name}
        ctx.inputs = ToolCallInputs(
            tool_call=tool_call,
            tool_name="skill_tool",
            tool_args={"skill_name": skill_name},
            tool_result=None,
            tool_msg=tool_msg,
        )
        ctx.extra = {}
        asyncio.run(rail.after_tool_call(ctx))

    def _invoke_ctx(self, *, query="hello"):
        ctx = AgentCallbackContext(agent=MagicMock())
        ctx.inputs = InvokeInputs(
            query=query,
            conversation_id=self.sid,
        )
        ctx.extra = {}
        return ctx

    def test_after_invoke_does_not_clear(self):
        rail = self._rail()
        self._activate(rail)
        asyncio.run(rail.after_invoke(self._invoke_ctx()))
        assert get_session_active_skill(self.sid) == "hwocr"

    def test_permission_resume_before_invoke_keeps_active(self):
        rail = self._rail()
        self._activate(rail)
        asyncio.run(rail.before_invoke(self._invoke_ctx(query="")))
        assert get_session_active_skill(self.sid) == "hwocr"

    def test_new_user_task_before_invoke_keeps_active(self):
        # 生命周期修正：新用户任务不再清空 active_skill。
        rail = self._rail()
        self._activate(rail)
        asyncio.run(rail.before_invoke(self._invoke_ctx(query="下一题")))
        assert get_session_active_skill(self.sid) == "hwocr"

    def test_switch_to_another_skill_replaces_active(self):
        # 切换到另一个 skill：旧 skill 清空，仅新 skill 激活。
        rail = self._rail()
        self._activate(rail, "hwocr")
        assert get_session_active_skill(self.sid) == "hwocr"
        self._activate(rail, "ppt-creation")
        assert get_session_active_skill(self.sid) == "ppt-creation"

    def test_session_teardown_clears_active(self):
        # 会话结束：clear_session_skill_state（adapter 淘汰 / teardown）清空。
        rail = self._rail()
        self._activate(rail)
        assert get_session_active_skill(self.sid) == "hwocr"
        clear_session_skill_state(self.sid)
        assert get_session_active_skill(self.sid) is None

    def test_resume_then_inject_bash(self):
        rail = self._rail()
        inject = SkillCredentialInjectionRail(
            skill_envs={"hwocr": {"HWOCR_AK": "ak", "HWOCR_SK": "sk"}},
            preset_session_id=self.sid,
        )
        self._activate(rail)
        # Simulate interrupted invoke ending without clearing.
        asyncio.run(rail.after_invoke(self._invoke_ctx()))
        # Permission resume.
        asyncio.run(rail.before_invoke(self._invoke_ctx(query="")))
        bash_ctx = AgentCallbackContext(agent=MagicMock())
        bash_ctx.inputs = ToolCallInputs(
            tool_call=MagicMock(),
            tool_name="bash",
            tool_args={"command": "hwocr.exe general-text"},
            tool_result=None,
            tool_msg=None,
        )
        bash_ctx.extra = {}
        asyncio.run(inject.before_tool_call(bash_ctx))
        assert bash_ctx.inputs.tool_args["env"]["HWOCR_AK"] == "ak"

    def test_skill_complete_clears(self):
        rail = self._rail()
        self._activate(rail)
        ctx = AgentCallbackContext(agent=MagicMock())
        tool_call = MagicMock()
        tool_call.arguments = {"skill_name": "hwocr"}
        tool_msg = MagicMock()
        tool_msg.metadata = {}
        ctx.inputs = ToolCallInputs(
            tool_call=tool_call,
            tool_name="skill_complete",
            tool_args={"skill_name": "hwocr"},
            tool_result=None,
            tool_msg=tool_msg,
        )
        ctx.extra = {}
        asyncio.run(rail.after_tool_call(ctx))
        assert get_session_active_skill(self.sid) is None

    def test_skill_complete_name_mismatch_keeps_active_and_warns(self):
        # 激活名 ≠ 完成名：不清空当前激活态，但记录 warn 便于排查
        rail = self._rail()
        self._activate(rail, "hwocr")
        ctx = AgentCallbackContext(agent=MagicMock())
        tool_call = MagicMock()
        tool_call.arguments = {"skill_name": "ppt-creation"}
        tool_msg = MagicMock()
        tool_msg.metadata = {}
        ctx.inputs = ToolCallInputs(
            tool_call=tool_call,
            tool_name="skill_complete",
            tool_args={"skill_name": "ppt-creation"},
            tool_result=None,
            tool_msg=tool_msg,
        )
        ctx.extra = {}
        with self.assertLogs(
            "jiuwenswarm.agents.harness.common.rails.skill_active_state",
            level="WARNING",
        ) as captured:
            asyncio.run(rail.after_tool_call(ctx))
        assert get_session_active_skill(self.sid) == "hwocr"
        assert any("ignored" in line for line in captured.output)


class TestStaleInvokeExpiry(unittest.TestCase):
    """改动 3：连续 N 轮 invoke 无 skill_tool 调用 → 过期清空 active_skill。"""

    sid = "officeclaw_stale_sess"

    def tearDown(self):
        clear_session_skill_state(self.sid)

    def _rail(self, limit=5):
        return SkillActiveStateRail(
            session_id=self.sid, stale_invoke_limit=limit
        )

    def _activate(self, rail, skill_name="hwocr"):
        ctx = AgentCallbackContext(agent=MagicMock())
        tool_call = MagicMock()
        tool_call.arguments = {"skill_name": skill_name}
        tool_msg = MagicMock()
        tool_msg.metadata = {"skill_name": skill_name}
        ctx.inputs = ToolCallInputs(
            tool_call=tool_call,
            tool_name="skill_tool",
            tool_args={"skill_name": skill_name},
            tool_result=None,
            tool_msg=tool_msg,
        )
        ctx.extra = {}
        asyncio.run(rail.after_tool_call(ctx))

    def _invoke(self, rail, query="q", source=None):
        ctx = AgentCallbackContext(agent=MagicMock())
        if source:
            run_context = MagicMock()
            run_context.extra = {"chat_send_source": source}
            ctx.inputs = InvokeInputs(
                query=query,
                conversation_id=self.sid,
                run_context=run_context,
            )
        else:
            ctx.inputs = InvokeInputs(query=query, conversation_id=self.sid)
        ctx.extra = {}
        asyncio.run(rail.before_invoke(ctx))

    def test_expires_after_limit(self):
        rail = self._rail(limit=5)
        self._activate(rail)
        for i in range(5):
            self._invoke(rail, query=f"q{i}")
        # 连续 5 轮无 skill_tool 调用，第 5 轮结束仍存活
        assert get_session_active_skill(self.sid) == "hwocr"
        # 第 6 轮开始时清空
        self._invoke(rail, query="q6")
        assert get_session_active_skill(self.sid) is None

    def test_hitl_resume_invoke_not_counted(self):
        # CR-3a：HITL 恢复轮（同一任务的继续）不计入过期计数——
        # 密集审批/ask_user 交互不会误清空 active_skill
        rail = self._rail(limit=3)
        self._activate(rail)
        self._invoke(rail)  # count=1
        self._invoke(rail)  # count=2
        # 连续多轮 HITL 恢复：计数冻结
        for i in range(5):
            self._invoke(
                rail, query=f"resume{i}", source="permission_interrupt"
            )
        assert get_session_active_skill(self.sid) == "hwocr"
        # 恢复 normal 轮后从冻结值继续累计
        self._invoke(rail)  # count=3
        assert get_session_active_skill(self.sid) == "hwocr"
        self._invoke(rail)  # count>=3 → 清空
        assert get_session_active_skill(self.sid) is None

    def test_evolution_source_still_counted(self):
        # 非 HITL 恢复 source（如 evolution）照常计数
        rail = self._rail(limit=2)
        self._activate(rail)
        self._invoke(rail, source="evolution_interrupt")  # count=1
        self._invoke(rail, source="evolution_interrupt")  # count=2
        self._invoke(rail)  # count>=2 → 清空
        assert get_session_active_skill(self.sid) is None

    def test_skill_tool_call_resets_counter(self):
        rail = self._rail(limit=3)
        self._activate(rail)
        self._invoke(rail)
        self._invoke(rail)
        # 重新 skill_tool 调用 → 计数清零，重新累计
        self._activate(rail)
        self._invoke(rail)
        self._invoke(rail)
        assert get_session_active_skill(self.sid) == "hwocr"
        self._invoke(rail)
        assert get_session_active_skill(self.sid) == "hwocr"
        self._invoke(rail)
        assert get_session_active_skill(self.sid) is None

    def test_switch_resets_counter(self):
        rail = self._rail(limit=2)
        self._activate(rail, "hwocr")
        self._invoke(rail)
        # 切换技能 → 新技能计数从零开始
        self._activate(rail, "ppt-creation")
        self._invoke(rail)
        self._invoke(rail)
        assert get_session_active_skill(self.sid) == "ppt-creation"
        self._invoke(rail)
        assert get_session_active_skill(self.sid) is None

    def test_limit_nonpositive_disables_expiry(self):
        rail = self._rail(limit=0)
        self._activate(rail)
        for _ in range(10):
            self._invoke(rail)
        assert get_session_active_skill(self.sid) == "hwocr"


class TestCredentialInjectionUsesPreset(unittest.TestCase):
    def tearDown(self):
        clear_session_skill_state("officeclaw_inject_sess")

    def test_injects_when_active_skill_keyed_by_preset(self):
        active = SkillActiveStateRail(session_id="officeclaw_inject_sess")
        inject = SkillCredentialInjectionRail(
            skill_envs={"hwocr": {"HWOCR_AK": "ak", "HWOCR_SK": "sk"}},
            preset_session_id="officeclaw_inject_sess",
        )

        activate_ctx = AgentCallbackContext(agent=MagicMock())
        tool_call = MagicMock()
        tool_call.arguments = {"skill_name": "hwocr"}
        tool_msg = MagicMock()
        tool_msg.metadata = {"skill_name": "hwocr"}
        activate_ctx.inputs = ToolCallInputs(
            tool_call=tool_call,
            tool_name="skill_tool",
            tool_args={"skill_name": "hwocr"},
            tool_result=None,
            tool_msg=tool_msg,
        )
        activate_ctx.extra = {}
        asyncio.run(active.after_tool_call(activate_ctx))

        bash_ctx = AgentCallbackContext(agent=MagicMock())
        bash_ctx.inputs = ToolCallInputs(
            tool_call=MagicMock(),
            tool_name="bash",
            tool_args={"command": "hwocr.exe general-text"},
            tool_result=None,
            tool_msg=None,
        )
        bash_ctx.extra = {}
        asyncio.run(inject.before_tool_call(bash_ctx))
        env = bash_ctx.inputs.tool_args["env"]
        assert env["HWOCR_AK"] == "ak"
        assert env["HWOCR_SK"] == "sk"


class TestResolveStaleInvokeLimit(unittest.TestCase):
    """CR-3b：react.skill_stale_invoke_limit 配置解析。"""

    def test_missing_returns_none(self):
        assert resolve_stale_invoke_limit(None) is None
        assert resolve_stale_invoke_limit({}) is None
        assert resolve_stale_invoke_limit({"react": {}}) is None
        assert resolve_stale_invoke_limit({"react": {"other": 1}}) is None
        assert resolve_stale_invoke_limit("not-a-dict") is None

    def test_valid_value(self):
        assert (
            resolve_stale_invoke_limit(
                {"react": {"skill_stale_invoke_limit": 10}}
            )
            == 10
        )
        assert (
            resolve_stale_invoke_limit(
                {"react": {"skill_stale_invoke_limit": "7"}}
            )
            == 7
        )

    def test_zero_and_negative_passthrough(self):
        # <=0 原样透传（显式禁用兜底）
        assert (
            resolve_stale_invoke_limit({"react": {"skill_stale_invoke_limit": 0}})
            == 0
        )
        assert (
            resolve_stale_invoke_limit({"react": {"skill_stale_invoke_limit": -1}})
            == -1
        )

    def test_invalid_falls_back_to_default(self):
        assert (
            resolve_stale_invoke_limit(
                {"react": {"skill_stale_invoke_limit": "abc"}}
            )
            is None
        )

    def test_react_section_form_accepted(self):
        # N2：reload 路径传入的 config 本身即 react 子 dict，顶层 key 直接命中
        assert (
            resolve_stale_invoke_limit({"skill_stale_invoke_limit": 10}) == 10
        )
        assert resolve_stale_invoke_limit({"other": 1}) is None

    def test_full_config_form_takes_precedence(self):
        # 完整 agent 配置形态：react 子 dict 优先于顶层同名 key
        assert (
            resolve_stale_invoke_limit(
                {
                    "skill_stale_invoke_limit": 99,
                    "react": {"skill_stale_invoke_limit": 7},
                }
            )
            == 7
        )

    def test_rail_stale_limit_property(self):
        # N2：公开只读属性，供装配点检测配置变化（默认 5 / 传参 / 禁用 0）
        assert SkillActiveStateRail().stale_invoke_limit == 5
        assert (
            SkillActiveStateRail(stale_invoke_limit=10).stale_invoke_limit == 10
        )
        assert (
            SkillActiveStateRail(stale_invoke_limit=0).stale_invoke_limit == 0
        )

    def test_rail_stale_limit_inplace_update(self):
        # N2：reload 原位更新（不重建实例）；None 归一化为默认值
        rail = SkillActiveStateRail(stale_invoke_limit=3)
        rail.update_stale_invoke_limit(10)
        assert rail.stale_invoke_limit == 10
        rail.update_stale_invoke_limit(None)
        assert rail.stale_invoke_limit == 5
        rail.update_stale_invoke_limit(0)
        assert rail.stale_invoke_limit == 0


class TestAdoptDefaultActiveSkill(unittest.TestCase):
    def tearDown(self):
        clear_session_skill_state("officeclaw_adopt_sess")
        clear_session_skill_state("officeclaw_other_sess")
        clear_session_skill_state(_DEFAULT_SESSION_ID)

    def _activate_under_default(self):
        default_rail = SkillActiveStateRail()
        ctx = AgentCallbackContext(agent=MagicMock())
        tool_call = MagicMock()
        tool_call.arguments = {"skill_name": "hwocr"}
        tool_msg = MagicMock()
        tool_msg.metadata = {"skill_name": "hwocr"}
        ctx.inputs = ToolCallInputs(
            tool_call=tool_call,
            tool_name="skill_tool",
            tool_args={"skill_name": "hwocr"},
            tool_result=None,
            tool_msg=tool_msg,
        )
        ctx.extra = {}
        asyncio.run(default_rail.after_tool_call(ctx))
        assert get_session_active_skill(_DEFAULT_SESSION_ID) is None
        return default_rail, ctx

    def test_adopt_unowned_orphan_for_hitl_session(self):
        from jiuwenswarm.agents.harness.common.rails.skill_active_state import (
            adopt_default_active_skill,
        )

        self._activate_under_default()
        assert adopt_default_active_skill("officeclaw_adopt_sess") is None
        assert get_session_active_skill("officeclaw_adopt_sess") is None
        assert get_session_active_skill(_DEFAULT_SESSION_ID) is None

    def test_inject_does_not_adopt_default_orphan(self):
        """Injection rail is read-only: must not migrate default -> foreign session."""
        self._activate_under_default()
        inject = SkillCredentialInjectionRail(
            skill_envs={"hwocr": {"HWOCR_AK": "ak"}},
            preset_session_id="officeclaw_adopt_sess",
        )
        bash_ctx = AgentCallbackContext(agent=MagicMock())
        bash_ctx.inputs = ToolCallInputs(
            tool_call=MagicMock(),
            tool_name="bash",
            tool_args={"command": "hwocr.exe general-text"},
            tool_result=None,
            tool_msg=None,
        )
        bash_ctx.extra = {}
        asyncio.run(inject.before_tool_call(bash_ctx))
        assert "env" not in bash_ctx.inputs.tool_args
        assert get_session_active_skill(_DEFAULT_SESSION_ID) is None
        assert get_session_active_skill("officeclaw_adopt_sess") is None

    def test_no_process_local_orphan_state(self):
        from jiuwenswarm.agents.harness.common.rails import skill_active_state

        assert not hasattr(skill_active_state, "_sessions")
        assert not hasattr(skill_active_state, "_get_or_create_state")
        assert get_session_active_skill("officeclaw_other_sess") is None

    def test_before_invoke_adopts_unowned_orphan(self):
        self._activate_under_default()
        rail = SkillActiveStateRail(session_id="officeclaw_adopt_sess")
        invoke_ctx = AgentCallbackContext(agent=MagicMock())
        invoke_ctx.inputs = InvokeInputs(
            query="",
            conversation_id="officeclaw_adopt_sess",
        )
        invoke_ctx.extra = {}
        asyncio.run(rail.before_invoke(invoke_ctx))
        assert get_session_active_skill("officeclaw_adopt_sess") is None
        assert get_session_active_skill(_DEFAULT_SESSION_ID) is None

        inject = SkillCredentialInjectionRail(
            skill_envs={"hwocr": {"HWOCR_AK": "ak"}},
            preset_session_id="officeclaw_adopt_sess",
        )
        bash_ctx = AgentCallbackContext(agent=MagicMock())
        bash_ctx.inputs = ToolCallInputs(
            tool_call=MagicMock(),
            tool_name="bash",
            tool_args={"command": "hwocr.exe general-text"},
            tool_result=None,
            tool_msg=None,
        )
        bash_ctx.extra = {}
        asyncio.run(inject.before_tool_call(bash_ctx))
        assert "env" not in bash_ctx.inputs.tool_args


if __name__ == "__main__":
    unittest.main()
