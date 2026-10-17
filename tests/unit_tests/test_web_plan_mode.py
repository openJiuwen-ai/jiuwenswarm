"""Web Plan 的后端行为测试（Adapter 选型、集群不含 Plan、审批动作）。"""

from types import SimpleNamespace

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm


def _request(params, channel_id="web"):
    return AgentRequest(
        request_id="req-1",
        channel_id=channel_id,
        session_id="s1",
        params=params,
    )


# ── Adapter 选型：Web 的 work_mode 决定 Deep / Code ─────────────────────────


@pytest.mark.parametrize(
    ("mode", "work_mode", "expected_adapter"),
    [
        ("agent", "work", "agent"),
        ("agent.plan", "work", "agent"),
        ("agent", "code", "code"),
        ("agent.plan", "code", "code"),
    ],
)
def test_web_work_mode_drives_adapter_choice(mode, work_mode, expected_adapter):
    request = _request({"mode": mode, "work_mode": work_mode})

    assert JiuWenSwarm._adapter_mode_for_request(request) == expected_adapter


@pytest.mark.parametrize(
    ("work_mode", "expected_adapter"), [("work", "agent"), ("code", "code")]
)
def test_web_cluster_adapter_follows_work_mode(work_mode, expected_adapter):
    """Web 的 ``team + code`` 与 TUI ``team.code`` 使用同一 CodeAdapter。"""
    request = _request({"mode": "team", "work_mode": work_mode})

    assert JiuWenSwarm._adapter_mode_for_request(request) == expected_adapter


@pytest.mark.parametrize(
    ("raw_mode", "expected_adapter"),
    [
        ("agent", "agent"),
        ("agent.plan", "agent"),
        ("team", "agent"),
        ("code.normal", "code"),
        ("code.plan", "code"),
        ("code.team", "code"),
        ("team.plan", "agent"),
        ("team.plan.normal", "agent"),
        ("team.plan.code", "code"),
    ],
)
def test_legacy_requests_keep_previous_adapter_choice(raw_mode, expected_adapter):
    """TUI 不带 work_mode，Adapter 选型必须与改动前完全一致。"""
    request = _request({"mode": raw_mode}, channel_id="tui")

    assert JiuWenSwarm._adapter_mode_for_request(request) == expected_adapter


# ── 集群不支持 Plan：work / code 集群装配都不得出现 plan 能力 ───────────────


@pytest.mark.parametrize("mode", ["team", "code.team"])
def test_web_team_modes_never_enable_team_plan(mode):
    """Web 集群只会发 ``team``，任何情况下都不打开 Team-level plan。"""
    from openjiuwen.agent_teams.schema.blueprint import TeamAgentSpec
    from jiuwenswarm.agents.harness.team.team_manager import TeamManager

    spec = TeamAgentSpec.model_construct(team_name="t", agents={}, enable_team_plan=False)
    TeamManager.apply_team_plan_mode(spec, request_metadata={"mode": mode})

    assert spec.enable_team_plan is False


@pytest.mark.parametrize("mode", ["team.plan", "team.plan.normal", "team.plan.code"])
def test_team_plan_modes_enable_team_plan(mode):
    from openjiuwen.agent_teams.schema.blueprint import TeamAgentSpec
    from jiuwenswarm.agents.harness.team.team_manager import TeamManager

    spec = TeamAgentSpec.model_construct(team_name="t", agents={}, enable_team_plan=False)
    TeamManager.apply_team_plan_mode(spec, request_metadata={"mode": mode})

    assert spec.enable_team_plan is True


@pytest.mark.parametrize("role", ["leader", "teammate"])
def test_work_team_has_no_plan_rails(role):
    from jiuwenswarm.agents.swarm import registry
    from jiuwenswarm.agents.swarm.config_specs import build_member_capability_specs

    rails, _tools = build_member_capability_specs({}, "team", role)
    rail_types = {spec.type for spec in rails}

    assert registry.CODE_AGENT_MODE not in rail_types
    assert registry.TEAM_PLAN_APPROVAL not in rail_types


def test_code_team_subagents_include_statusline_setup_for_both_roles():
    from jiuwenswarm.agents.swarm.config_specs import build_member_subagent_specs

    for role in ("leader", "teammate"):
        names = [spec.agent_card.name for spec in build_member_subagent_specs({}, "code.team", role)]
        assert names == ["statusline-setup", "explore_agent", "plan_agent"]


def test_plain_work_team_has_only_statusline_setup_subagent():
    from jiuwenswarm.agents.swarm.config_specs import build_member_subagent_specs

    names = [spec.agent_card.name for spec in build_member_subagent_specs({}, "team", "leader")]
    assert names == ["statusline-setup"]


# ── 审批动作：执行 / 跳过 / 下一步 都复用 approve / reject ──────────────────


def _confirm_payload(selected, custom_input=""):
    interactive = JiuWenSwarm._build_interactive_input_from_answers(
        "call_1",
        [{"selected_options": selected, "custom_input": custom_input}],
        "confirm_interrupt",
    )
    return interactive.user_inputs["call_1"]


def test_execute_action_approves_and_defers_execution():
    """Web 的执行：批准退出 plan，并标记本轮到此为止。"""
    payload = _confirm_payload(["plan_execute"])

    assert payload["approved"] is True
    assert payload["plan_execute"] is True


def test_tui_approve_is_unchanged():
    """TUI 发的仍是纯 approve，不带 plan_execute，批准后同一轮继续实现。"""
    payload = _confirm_payload(["approve"])

    assert payload["approved"] is True
    assert "plan_execute" not in payload


def test_skip_action_rejects_and_flags_force_finish():
    payload = _confirm_payload(["plan_skip"])

    assert payload["approved"] is False
    assert payload["plan_skip"] is True


def test_revise_action_rejects_and_carries_feedback():
    payload = _confirm_payload(["plan_revise"], "把迁移拆成两个阶段")

    assert payload["approved"] is False
    assert payload["feedback"] == "把迁移拆成两个阶段"
    assert payload["plan_revise"] is True
    assert "plan_skip" not in payload


def test_tui_reject_is_unchanged():
    """TUI 的 reject 不带 plan_skip / plan_revise，行为与改动前一致。"""
    payload = _confirm_payload(["reject"])

    assert payload["approved"] is False
    assert payload["feedback"] == "用户拒绝"
    assert "plan_skip" not in payload
    assert "plan_revise" not in payload


def test_plan_approval_actions_describe_three_web_buttons():
    from jiuwenswarm.agents.harness.code.prompt.plan_approval import (
        build_plan_approval_actions,
    )

    actions = build_plan_approval_actions("cn")

    assert [a["kind"] for a in actions] == ["execute", "skip", "revise"]
    assert [a["value"] for a in actions] == ["plan_execute", "plan_skip", "plan_revise"]


def test_plan_approval_actions_differ_only_by_label():
    """结构与语言无关：换语言只该换 label，回传取值不能跟着变。"""
    from jiuwenswarm.agents.harness.code.prompt.plan_approval import (
        build_plan_approval_actions,
    )

    cn = build_plan_approval_actions("cn")
    en = build_plan_approval_actions("en")

    assert [{k: v for k, v in a.items() if k != "label"} for a in cn] == [
        {k: v for k, v in a.items() if k != "label"} for a in en
    ]
    assert [a["label"] for a in en] == ["Execute", "Skip", "Next"]


def test_skip_option_values_do_not_capture_generic_words():
    """消费它的 if/elif 是所有确认流共用的，通用词会把别处的"跳过"误判成计划跳过。"""
    from jiuwenswarm.agents.harness.code.prompt.plan_approval import (
        PLAN_SKIP_OPTION_VALUES,
    )

    assert PLAN_SKIP_OPTION_VALUES == frozenset({"plan_skip"})


@pytest.mark.parametrize(
    ("language", "expected_prefix"),
    [("cn", "用户选择跳过"), ("zh", "用户选择跳过"), ("en", "The user skipped"), ("", "用户选择跳过")],
)
def test_plan_skip_feedback_follows_language(language, expected_prefix):
    from jiuwenswarm.agents.harness.code.prompt.plan_approval import plan_skip_feedback

    assert plan_skip_feedback(language).startswith(expected_prefix)


# ── 进入 plan 注入的 <system-reminder> 不得进入会话历史 ─────────────────────


def test_plan_activation_reminder_is_hidden_from_history():
    """历史里必须是用户原文，否则刷新页面会把提示词当成用户提问显示出来。"""
    from jiuwenswarm.server.agent_ws_server import (
        _inject_plan_mode_activation_reminder,
    )
    from jiuwenswarm.server.runtime.agent_adapter.interface import (
        _history_user_content,
    )

    request = _request({"mode": "agent.plan", "work_mode": "work", "query": "帮我设计登录模块"})
    _inject_plan_mode_activation_reminder(request)

    assert "<system-reminder>" in request.params["query"]
    assert _history_user_content(request.params, request.params["query"]) == "帮我设计登录模块"


def test_history_falls_back_to_query_without_plan_reminder():
    """普通请求没有这个键，历史内容仍是 ``query`` 本身。"""
    from jiuwenswarm.server.runtime.agent_adapter.interface import (
        _history_user_content,
    )

    assert _history_user_content({"mode": "agent"}, "你好") == "你好"


# ── 开关 Plan 不换 agent 实例：缓存键把单 agent 的 plan 子模式并回普通态 ─────


@pytest.mark.parametrize(
    ("mode", "plan_sub_mode", "normal_sub_mode"),
    [
        ("agent", "plan", None),
        ("code", "plan", "normal"),
    ],
)
def test_plan_toggle_keeps_the_same_agent_cache_key(mode, plan_sub_mode, normal_sub_mode):
    """开关 Plan 必须命中同一个 agent，否则换实例会丢掉内存里的对话上下文。"""
    from jiuwenswarm.server.runtime.agent_manager import _make_agent_cache_key

    plan_key = _make_agent_cache_key(mode, plan_sub_mode, "D:/proj")
    normal_key = _make_agent_cache_key(mode, normal_sub_mode, "D:/proj")

    assert plan_key == normal_key


def test_team_sub_mode_is_not_collapsed():
    """集群与单 agent 仍然是两个实例：只有 plan 子模式参与并轨。"""
    from jiuwenswarm.server.runtime.agent_manager import _make_agent_cache_key

    assert _make_agent_cache_key("code", "team", "") != _make_agent_cache_key("code", "normal", "")


def test_project_dir_still_separates_agents():
    from jiuwenswarm.server.runtime.agent_manager import _make_agent_cache_key

    assert _make_agent_cache_key("code", "plan", "D:/a") != _make_agent_cache_key("code", "plan", "D:/b")


# ── plan 状态要写在"真正跑这一轮的那个 session"上 ───────────────────────────


class _FakeAgentFacade:
    """只实现 ``_open_plan_state_session`` 用到的入口。"""

    def __init__(self, live_instance=None, root_instance=None, started_instance=None):
        self._live_instance = live_instance
        self._root_instance = root_instance
        self._started_instance = started_instance
        self.ensure_instance_calls = 0
        self.ensure_live_calls = 0

    def get_live_session_instance(self, session_id):  # noqa: ARG002
        return self._live_instance

    async def ensure_live_session_instance(self, session_id):  # noqa: ARG002
        self.ensure_live_calls += 1
        return self._started_instance

    async def ensure_instance(self):
        self.ensure_instance_calls += 1
        return self._root_instance


@pytest.mark.asyncio
async def test_plan_state_session_prefers_the_running_session():
    """命中长命 session：``load_state`` 的缓存挂在 session 对象上，写别的对象等于没写。"""
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

    live_session = SimpleNamespace(get_session_id=lambda: "s1")
    live_agent = SimpleNamespace(_interaction_session=live_session)
    agent = _FakeAgentFacade(live_instance=live_agent)

    deep_agent, session, live = await AgentWebSocketServer._open_plan_state_session(agent, "s1")

    assert (deep_agent, session, live) == (live_agent, live_session, True)
    assert agent.ensure_instance_calls == 0
    assert agent.ensure_live_calls == 0


@pytest.mark.asyncio
async def test_plan_state_session_starts_live_session_on_first_turn():
    """第一轮还没有 live session 时，先绑即将 invoke 的那份，再写 plan。"""
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

    live_session = SimpleNamespace(get_session_id=lambda: "s1")
    started_agent = SimpleNamespace(_interaction_session=live_session)
    agent = _FakeAgentFacade(started_instance=started_agent)

    deep_agent, session, live = await AgentWebSocketServer._open_plan_state_session(agent, "s1")

    assert (deep_agent, session, live) == (started_agent, live_session, True)
    assert agent.ensure_live_calls == 1
    assert agent.ensure_instance_calls == 0


@pytest.mark.asyncio
async def test_plan_state_session_falls_back_before_the_first_turn(monkeypatch):
    """agent 没法启动长命 session 时，才退回一次性 session 读 checkpointer。"""
    import openjiuwen.core.single_agent as single_agent
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

    created = SimpleNamespace(pre_run_calls=0)

    async def _pre_run(inputs=None):  # noqa: ARG001
        created.pre_run_calls += 1

    temp_session = SimpleNamespace(pre_run=_pre_run)
    monkeypatch.setattr(
        single_agent,
        "create_agent_session",
        lambda **kwargs: temp_session,  # noqa: ARG005
    )
    root_agent = SimpleNamespace(card=object())
    agent = _FakeAgentFacade(root_instance=root_agent)

    deep_agent, session, live = await AgentWebSocketServer._open_plan_state_session(agent, "s1")

    assert (deep_agent, session, live) == (root_agent, temp_session, False)
    assert created.pre_run_calls == 1
    assert agent.ensure_live_calls == 1
    assert agent.ensure_instance_calls == 1


# ── 常挂 work plan rail：非 plan 态不得多露工具 ─────────────────────────────


class _FakePromptBuilder:
    language = "cn"

    def __init__(self):
        self.removed = []
        self.added = []

    def remove_section(self, name):
        self.removed.append(name)

    def add_section(self, section):
        self.added.append(section)


async def _work_rail_visible_tools(plan_mode: str, tool_names: list[str]) -> list[str]:
    from jiuwenswarm.agents.harness.work.rails.work_agent_mode_rail import (
        WorkAgentModeRail,
    )

    rail = WorkAgentModeRail(language="cn")
    rail.system_prompt_builder = _FakePromptBuilder()
    rail.attachment_manager = None
    rail._agent = SimpleNamespace(
        load_state=lambda session: SimpleNamespace(  # noqa: ARG005
            plan_mode=SimpleNamespace(mode=plan_mode)
        )
    )
    ctx = SimpleNamespace(
        session=object(),
        inputs=SimpleNamespace(tools=[SimpleNamespace(name=name) for name in tool_names]),
        extra={},
    )

    await rail.before_model_call(ctx)
    return [getattr(tool, "name", "") for tool in ctx.inputs.tools]


@pytest.mark.asyncio
async def test_work_rail_hides_plan_tools_outside_plan_mode():
    """rail 常挂之后，TUI / IM / cron 的普通 work agent 工具列表必须零变化。"""
    visible = await _work_rail_visible_tools(
        "normal",
        ["read_file", "switch_mode", "enter_plan_mode", "exit_plan_mode"],
    )

    assert visible == ["read_file"]


@pytest.mark.asyncio
async def test_work_rail_keeps_plan_tools_in_plan_mode():
    visible = await _work_rail_visible_tools(
        "plan",
        ["read_file", "switch_mode", "enter_plan_mode", "exit_plan_mode", "send_file_to_user"],
    )

    # plan 态按 work 白名单过滤：调研与计划文件工具留下，副作用工具挡掉。
    assert "exit_plan_mode" in visible
    assert "read_file" in visible
    assert "send_file_to_user" not in visible


# ── work plan 白名单：不含代码型子 agent，也不含副作用工具 ──────────────────


# ── 退出 plan 的判据要跟着 openjiuwen 的文案走，不能抄一份 ──────────────────


def test_exit_plan_markers_track_openjiuwen_templates():
    """上游改措辞时必须自动跟随，否则这个判断会静默失效。"""
    from openjiuwen.harness.tools import agent_mode_tools
    from jiuwenswarm.agents.harness.code.rails import code_agent_mode_rail as rail_mod

    for template in agent_mode_tools._EXIT_PLAN_EMPTY_MSG.values():
        rendered = template.format(plan_path="/tmp/p.md")
        assert rendered.startswith(rail_mod._EXIT_PLAN_RESULT_OPENINGS)
    for template in agent_mode_tools._EXIT_PLAN_WITH_CONTENT_PREFIX.values():
        rendered = template.format(plan_path="/tmp/p.md")
        assert rendered.startswith(rail_mod._EXIT_PLAN_RESULT_OPENINGS)
        assert any(heading in rendered for heading in rail_mod._EXIT_PLAN_BODY_HEADINGS)


def test_exit_plan_markers_fall_back_when_upstream_constants_vanish(monkeypatch):
    """常量被改名 / 删除时退回兜底值，而不是推出一组空判据。"""
    from openjiuwen.harness.tools import agent_mode_tools
    from jiuwenswarm.agents.harness.code.rails import code_agent_mode_rail as rail_mod

    monkeypatch.delattr(agent_mode_tools, "_EXIT_PLAN_WITH_CONTENT_PREFIX")

    openings, headings = rail_mod._derive_exit_plan_markers()

    assert openings == rail_mod._FALLBACK_EXIT_PLAN_RESULT_OPENINGS
    assert headings == rail_mod._FALLBACK_EXIT_PLAN_BODY_HEADINGS


# ── 防重入闸门：只认一次性的 plan_entry_source ──────────────────────────────


@pytest.mark.parametrize("source", ["slash_command", "plan_toggle"])
def test_explicit_plan_entry_requires_one_shot_marker(source):
    """TUI 的 /plan 与 Web 手动打开开关都带一次性标记，都算显式进入。"""
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

    request = _request(
        {"mode": "agent.plan", "work_mode": "work", "plan_entry_source": source}
    )

    assert AgentWebSocketServer._is_explicit_plan_entry_request(request) is True


def test_web_plan_request_without_marker_is_not_explicit_entry():
    """否则 plan.mode_exited 丢包时，用户下一条消息会被静默拖回 plan。"""
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

    request = _request({"mode": "agent.plan", "work_mode": "work"})

    assert AgentWebSocketServer._is_explicit_plan_entry_request(request) is False


# ── work 普通消息不该为了同步 plan 状态强建 root agent ──────────────────────


def test_plain_work_turn_without_plan_trace_skips_state_sync():
    """IM / 定时任务 / CLI / Web work 的绝大多数会话从未开过 Plan。"""
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

    request = _request({"mode": "agent", "work_mode": "work"})

    assert AgentWebSocketServer._session_may_hold_plan_state(request, "s1") is False


def test_previous_plan_mode_in_metadata_still_triggers_state_sync():
    """跨重启也要能把停在 plan 里的会话切回普通模式并通知前端复位。"""
    from jiuwenswarm.server.agent_ws_server import (
        _SESSION_PREVIOUS_MODE_KEY,
        AgentWebSocketServer,
    )

    request = _request(
        {
            "mode": "agent",
            "work_mode": "work",
            _SESSION_PREVIOUS_MODE_KEY: "agent.plan",
        }
    )

    assert AgentWebSocketServer._session_may_hold_plan_state(request, "s1") is True


def test_in_memory_plan_mark_triggers_state_sync():
    from jiuwenswarm.server.agent_ws_server import (
        AgentWebSocketServer,
        _plan_active_sessions,
    )

    request = _request({"mode": "agent", "work_mode": "work"})
    _plan_active_sessions.add("s-marked")
    try:
        assert AgentWebSocketServer._session_may_hold_plan_state(request, "s-marked") is True
    finally:
        _plan_active_sessions.discard("s-marked")


def test_work_plan_whitelist_excludes_side_effect_tools():
    from jiuwenswarm.agents.harness.work.prompt.work_plan_prompts import (
        WORK_PLAN_ALLOWED_TOOLS,
    )

    allowed = set(WORK_PLAN_ALLOWED_TOOLS)

    assert {"ask_user", "read_file", "write_file", "exit_plan_mode"} <= allowed
    # 配了子 agent 时要能用 task_tool 做只读调研。
    assert "task_tool" in allowed
    for forbidden in ("send_file_to_user", "cron_create", "switch_mode"):
        assert forbidden not in allowed
