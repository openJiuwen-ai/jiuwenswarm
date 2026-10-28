# coding: utf-8
# pylint: disable=protected-access
"""Tests for skill credential injection and coalesce helpers."""

import asyncio
import json
import os
import unittest
from unittest.mock import MagicMock, patch

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ToolCallInputs

from jiuwenswarm.agents.harness.common.rails.skill_credential_injection_rail import (
    SkillCredentialInjectionRail,
    coalesce_config_skill_envs,
    coalesce_skill_envs,
    match_skill_in_command,
    match_skills_in_command,
)
from jiuwenswarm.agents.harness.common.tools.command_tools import _build_subprocess_env


class TestBuildSubprocessEnv(unittest.TestCase):
    def test_returns_none_when_extra_env_is_none(self):
        assert _build_subprocess_env(None) is None

    def test_returns_none_when_extra_env_is_empty(self):
        assert _build_subprocess_env({}) is None

    def test_merges_extra_env_into_os_environ_copy(self):
        extra = {"MY_TEST_KEY": "test_value_12345"}
        result = _build_subprocess_env(extra)
        assert result is not None
        assert result["MY_TEST_KEY"] == "test_value_12345"
        assert "PATH" in result or "path" in result

    def test_does_not_modify_original_os_environ(self):
        extra = {"ANOTHER_TEST_KEY": "should_not_leak"}
        _build_subprocess_env(extra)
        assert "ANOTHER_TEST_KEY" not in os.environ


class TestCoalesceSkillEnvs(unittest.TestCase):
    def test_empty_incoming_keeps_current(self):
        current = {"hwocr": {"HWOCR_AK": "ak"}}
        assert coalesce_skill_envs({}, current) == current
        assert coalesce_skill_envs(None, current) == current

    def test_catalog_clear_with_skill_key_wins(self):
        current = {"hwocr": {"HWOCR_AK": "ak"}}
        incoming = {"hwocr": {"HWOCR_AK": ""}}
        assert coalesce_skill_envs(incoming, current) == incoming

    def test_config_placeholder_keeps_previous_react_block(self):
        previous = {
            "react": {
                "skill_envs": {"hwocr": {"HWOCR_AK": "ak"}},
                "agent_name": "main",
            }
        }
        yaml_reload = {"react": {"skill_envs": {}, "agent_name": "main"}}
        merged = coalesce_config_skill_envs(yaml_reload, previous)
        assert merged["react"]["skill_envs"]["hwocr"]["HWOCR_AK"] == "ak"

    def test_config_catalog_clear_replaces(self):
        previous = {"react": {"skill_envs": {"hwocr": {"HWOCR_AK": "ak"}}}}
        catalog = {"react": {"skill_envs": {"hwocr": {"HWOCR_AK": ""}}}}
        merged = coalesce_config_skill_envs(catalog, previous)
        assert merged["react"]["skill_envs"]["hwocr"]["HWOCR_AK"] == ""


class TestCredentialInjection(unittest.TestCase):
    def _make_rail(self, skill_envs=None):
        return SkillCredentialInjectionRail(skill_envs=skill_envs)

    def _make_ctx(self, tool_name="mcp_exec_command", tool_args=None, conversation_id="test-session"):
        ctx = AgentCallbackContext(agent=MagicMock())
        inputs = ToolCallInputs(
            tool_call=MagicMock(),
            tool_name=tool_name,
            tool_args=tool_args or {"command": "echo hello"},
            tool_result=None,
            tool_msg=None,
        )
        inputs.conversation_id = conversation_id
        ctx.inputs = inputs
        return ctx

    @patch(
        "jiuwenswarm.agents.harness.common.rails.skill_credential_injection_rail.get_session_active_skill"
    )
    def test_injects_credentials_into_tool_args(self, mock_get_skill):
        mock_get_skill.return_value = "hwocr"
        rail = self._make_rail(
            skill_envs={"hwocr": {"HWOCR_AK": "ak", "HWOCR_SK": "sk"}}
        )
        ctx = self._make_ctx()
        asyncio.run(rail.before_tool_call(ctx))
        env = ctx.inputs.tool_args["env"]
        assert env["HWOCR_AK"] == "ak"
        assert env["HWOCR_SK"] == "sk"

    @patch(
        "jiuwenswarm.agents.harness.common.rails.skill_credential_injection_rail.get_session_active_skill"
    )
    def test_config_value_overrides_model_env_keys(self, mock_get_skill):
        # 配置权威覆盖：模型 env 已带的凭据 key（典型为历史回写后被模仿
        # 抄写的陈旧值）一律以当前配置值覆盖，防凭据轮换后旧值透传执行；
        # 非技能凭据 key 的模型 env 项不受影响
        mock_get_skill.return_value = "hwocr"
        rail = self._make_rail(
            skill_envs={"hwocr": {"HWOCR_AK": "from_rail", "HWOCR_SK": "from_rail"}}
        )
        ctx = self._make_ctx(
            tool_args={
                "command": "echo hello",
                "env": {"HWOCR_AK": "stale_from_history", "OTHER_KEY": "keep"},
            }
        )
        asyncio.run(rail.before_tool_call(ctx))
        env = ctx.inputs.tool_args["env"]
        assert env["HWOCR_AK"] == "from_rail"
        assert env["HWOCR_SK"] == "from_rail"
        assert env["OTHER_KEY"] == "keep"

    @patch(
        "jiuwenswarm.agents.harness.common.rails.skill_credential_injection_rail.get_session_active_skill"
    )
    def test_injects_from_json_string_tool_args(self, mock_get_skill):
        mock_get_skill.return_value = "hwocr"
        rail = self._make_rail(skill_envs={"hwocr": {"HWOCR_AK": "ak"}})
        ctx = self._make_ctx(tool_args=json.dumps({"command": "echo hello"}))
        asyncio.run(rail.before_tool_call(ctx))
        assert isinstance(ctx.inputs.tool_args, dict)
        assert ctx.inputs.tool_args["env"]["HWOCR_AK"] == "ak"

    @patch(
        "jiuwenswarm.agents.harness.common.rails.skill_credential_injection_rail.get_session_active_skill"
    )
    def test_bash_injects_env_into_tool_args(self, mock_get_skill):
        mock_get_skill.return_value = "hwocr"
        rail = self._make_rail(
            skill_envs={"hwocr": {"HWOCR_AK": "ak", "HWOCR_SK": "sk"}}
        )
        ctx = self._make_ctx(tool_name="bash", tool_args={"command": "& hwocr.exe run"})
        asyncio.run(rail.before_tool_call(ctx))
        env = ctx.inputs.tool_args["env"]
        assert env["HWOCR_AK"] == "ak"
        assert env["HWOCR_SK"] == "sk"

    @patch(
        "jiuwenswarm.agents.harness.common.rails.skill_credential_injection_rail.get_session_active_skill"
    )
    def test_skips_when_no_active_skill(self, mock_get_skill):
        mock_get_skill.return_value = None
        rail = self._make_rail(skill_envs={"hwocr": {"HWOCR_AK": "ak"}})
        ctx = self._make_ctx(tool_args={"command": "echo hello"})
        asyncio.run(rail.before_tool_call(ctx))
        assert "env" not in ctx.inputs.tool_args


class TestMatchSkillInCommand(unittest.TestCase):
    KNOWN = ("hwocr", "ppt-creation", "mx-finance-data", "delayed-restart-app")

    def test_subdir_script_form(self):
        # 内置技能主流形态：脚本在 <skill>/scripts/ 子目录下
        cmd = "python3 /root/.jiuwenswarm/agent/skills/hwocr/scripts/run.py --x 1"
        assert match_skill_in_command(cmd, self.KNOWN) == "hwocr"

    def test_root_script_form_windows_backslash(self):
        cmd = (
            "python %USERPROFILE%\\.jiuwenswarm\\agent\\skills\\"
            "delayed-restart-app\\launch_delayed_restart.py --pid 123"
        )
        assert match_skill_in_command(cmd, self.KNOWN) == "delayed-restart-app"

    def test_segment_boundary_no_partial_match(self):
        assert match_skill_in_command("python3 /opt/mx-finance-data-x/run.py", self.KNOWN) is None

    def test_pip_install_not_matched(self):
        cmd = "pip3 install -r /opt/skills/hwocr/requirements.txt"
        assert match_skill_in_command(cmd, self.KNOWN) is None

    def test_cat_ls_not_matched(self):
        assert match_skill_in_command("cat /opt/hwocr/SKILL.md", self.KNOWN) is None
        assert match_skill_in_command("ls /opt/hwocr", self.KNOWN) is None

    def test_bare_or_cd_relative_not_matched(self):
        # 静态不可解析形态 → 匹配 None（rail 侧回退按 active 注入）
        assert match_skill_in_command("python3 app.py", self.KNOWN) is None
        assert match_skill_in_command("cd /opt/hwocr && python3 run.py", self.KNOWN) is None

    def test_case_insensitive_mixed_separators(self):
        assert match_skill_in_command(
            "python3 D:\\Skills\\HWOCR\\scripts\\Run.PY", self.KNOWN
        ) == "hwocr"

    def test_node_mjs(self):
        assert match_skill_in_command(
            "node skills/ppt-creation/components/build.mjs", self.KNOWN
        ) == "ppt-creation"

    def test_quoted_path_with_spaces(self):
        assert match_skill_in_command(
            'python "C:\\My Skills\\hwocr\\scripts\\run.py"', self.KNOWN
        ) == "hwocr"

    def test_python_dash_c_not_matched(self):
        assert match_skill_in_command('python -c "print(1)"', self.KNOWN) is None
        assert match_skill_in_command("python -c \"open('f.py')\"", self.KNOWN) is None

    def test_bare_filename_without_path_separator_not_matched(self):
        # 无路径分隔符的 .py 文件名（copy hwocr.py dest.py / python3 app.py）
        # 无技能段可扫，不命中
        assert match_skill_in_command("copy hwocr.py dest.py", self.KNOWN) is None
        assert match_skill_in_command("python3 app.py", self.KNOWN) is None

    def test_quoted_absolute_interpreter_with_backslash_script(self):
        # history.json 实录形态：带引号绝对路径解释器 + 反斜杠脚本路径 +
        # 中文参数。旧实现因解释器前缀失配（P1），本用例固化修复。
        known = self.KNOWN + ("mx-finance-search",)
        cmd = (
            '"C:\\Users\\Administrator\\AppData\\Local\\Python\\bin\\python3.exe" '
            '"D:\\Object\\skill-setConfig\\relay-claw\\office-claw-skills\\'
            'mx-finance-search\\scripts\\get_data.py" 查询 --no-save 2>&1'
        )
        assert match_skill_in_command(cmd, known) == "mx-finance-search"

    def test_backslash_unquoted_path_matched(self):
        cmd = (
            "python3 D:\\Object\\skill-setConfig\\relay-claw\\office-claw-skills\\"
            "mx-finance-search\\scripts\\get_data.py 查询 --no-save"
        )
        assert (
            match_skill_in_command(cmd, ("mx-finance-search",)) == "mx-finance-search"
        )

    def test_false_positives_fixed(self):
        # 验收方实测的三条误报检查固化
        known = ("mx-finance-search",)
        assert (
            match_skill_in_command(
                'pip3 install -r "D:\\Object\\skills\\mx-finance-search\\requirements.txt"',
                known,
            )
            is None
        )
        assert (
            match_skill_in_command(
                'cat "D:\\Object\\skills\\mx-finance-search\\SKILL.md"', known
            )
            is None
        )
        assert match_skill_in_command('python -c "print(1)"', known) is None

    def test_flag_tolerant(self):
        assert match_skill_in_command(
            "python3 -u /opt/hwocr/scripts/run.py", self.KNOWN
        ) == "hwocr"

    def test_multiple_skills_ordered_distinct(self):
        cmd = (
            "python3 /a/hwocr/x.py && python3 /b/ppt-creation/y.py "
            "&& python3 /c/hwocr/z.py"
        )
        assert match_skills_in_command(cmd, self.KNOWN) == ["hwocr", "ppt-creation"]

    def test_readonly_commands_not_matched(self):
        # CR-2：只读命令引用技能脚本（参数位）不命中——cat/echo/grep/
        # head/tail/diff/ls 读脚本调试是技能开发的日常形态
        for cmd in (
            "cat /opt/skills/hwocr/run.py",
            "echo see /opt/skills/hwocr/run.py",
            "grep -n 'def main' /opt/skills/hwocr/run.py",
            "head -5 /opt/skills/hwocr/run.py",
            "tail -5 /opt/skills/hwocr/run.py",
            "diff /opt/skills/hwocr/run.py /opt/skills/hwocr/run.py.bak",
            "ls /opt/skills/hwocr/scripts",
        ):
            assert match_skill_in_command(cmd, self.KNOWN) is None, cmd

    def test_sed_quoted_pattern_not_matched(self):
        # CR-2：sed 替换串中的技能路径（穿单引号形态）不命中
        cmd = "sed 's|/opt/skills/hwocr/run.py|X|' cfg"
        assert match_skill_in_command(cmd, self.KNOWN) is None

    def test_argument_position_reference_not_matched(self):
        # CR-1 场景2：主跑 active 技能、参数位引用另一技能脚本——
        # 执行位只有 hwocr，ppt-creation 不进 matched
        cmd = (
            "python3 /opt/skills/hwocr/scripts/run.py "
            "--ref /opt/skills/ppt-creation/sample.py"
        )
        assert match_skills_in_command(cmd, self.KNOWN) == ["hwocr"]

    def test_wrapper_prefix_and_direct_execution_matched(self):
        # wrapper 前缀（VAR=x / sudo / nohup / timeout N）后仍能识别执行位；
        # 首 token 即脚本的直执行形态同样命中
        for cmd in (
            "EM_KEY=x python3 /opt/skills/hwocr/scripts/run.py",
            "sudo python3 /opt/skills/hwocr/scripts/run.py",
            "nohup python3 /opt/skills/hwocr/scripts/run.py &",
            "timeout 30 python3 /opt/skills/hwocr/scripts/run.py",
            "./skills/hwocr/run.py --x 1",
        ):
            assert match_skill_in_command(cmd, self.KNOWN) == "hwocr", cmd

    def test_redirect_suffix_not_split(self):
        # 2>&1 重定向的 & 不得被子命令拆分误伤（拆坏会导致执行位丢失）
        cmd = "python3 /opt/skills/hwocr/scripts/run.py 查询 --no-save 2>&1"
        assert match_skill_in_command(cmd, self.KNOWN) == "hwocr"

    def test_multiline_compound_commands_matched(self):
        # N1：换行与 ; 同为顺序执行分隔符，多行复合命令必须逐行识别
        assert match_skills_in_command(
            "python3 /a/hwocr/x.py\nnode /b/ppt-creation/y.js", self.KNOWN
        ) == ["hwocr", "ppt-creation"]
        # cd 行无执行位，第二行脚本正常命中
        assert match_skills_in_command(
            "cd /a/hwocr\nnode /b/ppt-creation/y.js", self.KNOWN
        ) == ["ppt-creation"]
        # \r\n 残留形态同样逐行识别
        assert match_skills_in_command(
            "python3 /a/hwocr/x.py\r\nnode /b/ppt-creation/y.js", self.KNOWN
        ) == ["hwocr", "ppt-creation"]


class TestSkillGateAndFallback(unittest.TestCase):
    def _make_rail(self):
        return SkillCredentialInjectionRail(
            skill_envs={
                "hwocr": {"HWOCR_AK": "ak", "HWOCR_SK": "sk"},
                "ppt-creation": {"PPT_TOKEN": "tok", "PPT_SK": "sk"},
            }
        )

    def _make_ctx(self, tool_name="bash", tool_args=None):
        ctx = AgentCallbackContext(agent=MagicMock())
        tool_call = MagicMock()
        tool_call.id = "call-1"
        inputs = ToolCallInputs(
            tool_call=tool_call,
            tool_name=tool_name,
            tool_args=tool_args or {"command": "echo hello"},
            tool_result=None,
            tool_msg=None,
        )
        inputs.conversation_id = "gate-session"
        ctx.inputs = inputs
        ctx.extra = {}
        return ctx

    @patch(
        "jiuwenswarm.agents.harness.common.rails.skill_credential_injection_rail.get_session_active_skill"
    )
    def test_matched_active_subdir_injects(self, mock_get_skill):
        mock_get_skill.return_value = "hwocr"
        rail = self._make_rail()
        ctx = self._make_ctx(
            tool_args={"command": "python3 /opt/skills/hwocr/scripts/run.py"}
        )
        asyncio.run(rail.before_tool_call(ctx))
        assert ctx.inputs.tool_args["env"]["HWOCR_AK"] == "ak"
        assert ctx.inputs.tool_args["env"]["HWOCR_SK"] == "sk"
        assert not ctx.extra.get("_skip_tool")

    @patch(
        "jiuwenswarm.agents.harness.common.rails.skill_credential_injection_rail.get_session_active_skill"
    )
    def test_no_active_gate_blocks_and_guides(self, mock_get_skill):
        # 原始故障的新表现：未激活直接重跑技能脚本 → 从静默缺 key 失败
        # 变为可自愈的拦截 + 指引
        mock_get_skill.return_value = None
        rail = self._make_rail()
        ctx = self._make_ctx(
            tool_args={"command": "python3 /opt/skills/hwocr/scripts/run.py"}
        )
        asyncio.run(rail.before_tool_call(ctx))
        assert ctx.extra.get("_skip_tool") is True
        assert 'skill_tool(skill_name="hwocr")' in str(ctx.inputs.tool_result)
        assert ctx.inputs.tool_msg is not None
        assert "env" not in ctx.inputs.tool_args

    @patch(
        "jiuwenswarm.agents.harness.common.rails.skill_credential_injection_rail.get_session_active_skill"
    )
    def test_active_other_skill_blocked(self, mock_get_skill):
        # 跨技能保护核心用例：active=A 时直跑 B 的脚本 → 拒绝且不注入 A 凭据
        mock_get_skill.return_value = "hwocr"
        rail = self._make_rail()
        ctx = self._make_ctx(
            tool_args={"command": "node /opt/skills/ppt-creation/build.mjs"}
        )
        asyncio.run(rail.before_tool_call(ctx))
        assert ctx.extra.get("_skip_tool") is True
        assert "env" not in ctx.inputs.tool_args

    @patch(
        "jiuwenswarm.agents.harness.common.rails.skill_credential_injection_rail.get_session_active_skill"
    )
    def test_escape_hatch_explicit_env_allows(self, mock_get_skill):
        # 逃生口：显式带了目标技能凭据 key → 放行且不注入 active 凭据；
        # 配置权威覆盖：只刷新模型已带入的同名 key（陈旧值 → 当前配置值），
        # 不补全模型未提供的 key（跨技能隔离不变）
        mock_get_skill.return_value = "hwocr"
        rail = self._make_rail()
        ctx = self._make_ctx(
            tool_args={
                "command": "python3 /opt/skills/ppt-creation/scripts/build.py",
                "env": {"PPT_TOKEN": "stale_explicit", "OTHER_KEY": "keep"},
            }
        )
        asyncio.run(rail.before_tool_call(ctx))
        assert not ctx.extra.get("_skip_tool")
        assert ctx.inputs.tool_args["env"]["PPT_TOKEN"] == "tok"
        assert ctx.inputs.tool_args["env"]["OTHER_KEY"] == "keep"
        assert "PPT_SK" not in ctx.inputs.tool_args["env"]
        assert "HWOCR_AK" not in ctx.inputs.tool_args["env"]

    @patch(
        "jiuwenswarm.agents.harness.common.rails.skill_credential_injection_rail.get_session_active_skill"
    )
    def test_cd_relative_falls_back_to_active(self, mock_get_skill):
        # 静态不可解析（cd + 裸文件名）→ 回退按 active 注入（零回归）
        mock_get_skill.return_value = "hwocr"
        rail = self._make_rail()
        ctx = self._make_ctx(
            tool_args={"command": "cd /opt/skills/hwocr && python3 run.py"}
        )
        asyncio.run(rail.before_tool_call(ctx))
        assert not ctx.extra.get("_skip_tool")
        assert ctx.inputs.tool_args["env"]["HWOCR_AK"] == "ak"

    @patch(
        "jiuwenswarm.agents.harness.common.rails.skill_credential_injection_rail.get_session_active_skill"
    )
    def test_pip_install_with_active_falls_back(self, mock_get_skill):
        # 防误报回归：pip3 install 不触发闸门
        mock_get_skill.return_value = "hwocr"
        rail = self._make_rail()
        ctx = self._make_ctx(
            tool_args={
                "command": "pip3 install -r /opt/skills/hwocr/requirements.txt"
            }
        )
        asyncio.run(rail.before_tool_call(ctx))
        assert not ctx.extra.get("_skip_tool")
        assert ctx.inputs.tool_args["env"]["HWOCR_AK"] == "ak"

    @patch(
        "jiuwenswarm.agents.harness.common.rails.skill_credential_injection_rail.get_session_active_skill"
    )
    def test_no_credential_skill_falls_back(self, mock_get_skill):
        # 引用的技能无凭据（不在 skill_envs）→ 匹配不到 → 回退注入 active
        mock_get_skill.return_value = "hwocr"
        rail = self._make_rail()
        ctx = self._make_ctx(
            tool_args={
                "command": "python3 /opt/skills/gitcode-api/scripts/cli.py --help"
            }
        )
        asyncio.run(rail.before_tool_call(ctx))
        assert not ctx.extra.get("_skip_tool")
        assert ctx.inputs.tool_args["env"]["HWOCR_AK"] == "ak"

    @patch(
        "jiuwenswarm.agents.harness.common.rails.skill_credential_injection_rail.get_session_active_skill"
    )
    def test_gate_with_json_string_tool_args(self, mock_get_skill):
        mock_get_skill.return_value = None
        rail = self._make_rail()
        ctx = self._make_ctx(
            tool_args=json.dumps(
                {"command": "python3 /opt/skills/hwocr/scripts/run.py"}
            )
        )
        asyncio.run(rail.before_tool_call(ctx))
        assert ctx.extra.get("_skip_tool") is True
        assert isinstance(ctx.inputs.tool_args, dict)
        assert 'skill_tool(skill_name="hwocr")' in str(ctx.inputs.tool_result)

    @patch(
        "jiuwenswarm.agents.harness.common.rails.skill_credential_injection_rail.get_session_active_skill"
    )
    def test_gate_message_english(self, mock_get_skill):
        mock_get_skill.return_value = None
        rail = self._make_rail()
        ctx = self._make_ctx(
            tool_args={"command": "python3 /opt/skills/hwocr/scripts/run.py"}
        )
        ctx.extra["language"] = "en"
        asyncio.run(rail.before_tool_call(ctx))
        assert ctx.extra.get("_skip_tool") is True
        message = str(ctx.inputs.tool_result)
        assert "skill_tool" in message
        assert "credential" in message.lower()

    @patch(
        "jiuwenswarm.agents.harness.common.rails.skill_credential_injection_rail.get_session_active_skill"
    )
    def test_compound_multi_skill_blocked_with_split_guidance(self, mock_get_skill):
        # CR-1 场景1：复合命令执行多个有凭据技能脚本——单激活语义下
        # 「激活后重跑」会乒乓循环，指引必须改为拆分命令
        mock_get_skill.return_value = None
        rail = self._make_rail()
        ctx = self._make_ctx(
            tool_args={
                "command": "python3 /a/hwocr/x.py && node /b/ppt-creation/y.js"
            }
        )
        asyncio.run(rail.before_tool_call(ctx))
        assert ctx.extra.get("_skip_tool") is True
        message = str(ctx.inputs.tool_result)
        assert "hwocr" in message and "ppt-creation" in message
        assert "拆分" in message
        assert "env" not in ctx.inputs.tool_args

    @patch(
        "jiuwenswarm.agents.harness.common.rails.skill_credential_injection_rail.get_session_active_skill"
    )
    def test_multiline_multi_skill_blocked_with_split_guidance(self, mock_get_skill):
        # N1：多行复合命令逐行识别——多技能执行位全部命中，拆分指引拦截
        mock_get_skill.return_value = None
        rail = self._make_rail()
        ctx = self._make_ctx(
            tool_args={
                "command": "python3 /a/hwocr/x.py\nnode /b/ppt-creation/y.js"
            }
        )
        asyncio.run(rail.before_tool_call(ctx))
        assert ctx.extra.get("_skip_tool") is True
        message = str(ctx.inputs.tool_result)
        assert "hwocr" in message and "ppt-creation" in message
        assert "拆分" in message
        assert "env" not in ctx.inputs.tool_args

    @patch(
        "jiuwenswarm.agents.harness.common.rails.skill_credential_injection_rail.get_session_active_skill"
    )
    def test_active_with_argument_reference_injects(self, mock_get_skill):
        # CR-1 场景2：主跑 active 技能、参数位引用另一技能脚本 → 正常注入
        mock_get_skill.return_value = "hwocr"
        rail = self._make_rail()
        ctx = self._make_ctx(
            tool_args={
                "command": (
                    "python3 /opt/skills/hwocr/scripts/run.py "
                    "--ref /opt/skills/ppt-creation/sample.py"
                )
            }
        )
        asyncio.run(rail.before_tool_call(ctx))
        assert not ctx.extra.get("_skip_tool")
        assert ctx.inputs.tool_args["env"]["HWOCR_AK"] == "ak"


if __name__ == "__main__":
    unittest.main(verbosity=2)
