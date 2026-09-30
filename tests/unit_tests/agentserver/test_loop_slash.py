# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``/loop`` 斜杠解析与入口惰性的单元测试。

覆盖 loop_slash.parse_loop_slash 的 token 边界、参数语法（--verify / 
--max-iterations）与 LoopEngine 的 adapter mode 推导。运行时流式适配
（run_loop_stream）依赖 AgentRuntime，由集成场景验证，不在单测范围。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

# 与同目录其他测试一致的加载方式：按文件路径加载模块，规避包级 __init__ 副作用
_MODULE_PATH = (
    Path(__file__).resolve().parents[3]
    / "jiuwenswarm"
    / "server"
    / "runtime"
    / "agent_adapter"
    / "loop_slash.py"
)
_spec = importlib.util.spec_from_file_location("_loop_slash_under_test", _MODULE_PATH)
_loop_slash = importlib.util.module_from_spec(_spec)
sys.modules.setdefault(_spec.name, _loop_slash)
_spec.loader.exec_module(_loop_slash)  # type: ignore[union-attr]

parse_loop_slash = _loop_slash.parse_loop_slash


class TestParseLoopSlash:
    """文本解析：命中 / 非命中 / 参数语法三类。"""

    def test_basic_task(self) -> None:
        r = parse_loop_slash("/loop 修复登录 bug")
        assert r is not None
        assert r["result_type"] == "loop_stream"
        assert r["task"] == "修复登录 bug"
        assert r["verify_cmd"] is None
        assert r["max_iterations"] == 3

    def test_verify_double_quoted(self) -> None:
        r = parse_loop_slash('/loop --verify "pytest -q" 让测试全绿')
        assert r is not None
        assert r["verify_cmd"] == "pytest -q"
        assert r["task"] == "让测试全绿"

    def test_verify_single_quoted(self) -> None:
        r = parse_loop_slash("/loop --verify 'bash verify.sh' 写一篇文章")
        assert r is not None
        assert r["verify_cmd"] == "bash verify.sh"
        assert r["task"] == "写一篇文章"

    def test_max_iterations(self) -> None:
        r = parse_loop_slash("/loop --max-iterations 5 处理数据")
        assert r is not None
        assert r["max_iterations"] == 5
        assert r["verify_cmd"] is None

    def test_all_flags_combined(self) -> None:
        r = parse_loop_slash(
            "/loop --verify 'npm test' --max-iterations 2 修复构建失败")
        assert r is not None
        assert r["verify_cmd"] == "npm test"
        assert r["max_iterations"] == 2
        assert r["task"] == "修复构建失败"

    def test_bare_loop_returns_none(self) -> None:
        # 空任务：不拦截，按普通消息继续
        assert parse_loop_slash("/loop") is None

    def test_prefix_words_not_triggered(self) -> None:
        # token 边界：/loops /loopback 等前缀词是普通文本
        assert parse_loop_slash("/loops 介绍一下这个功能") is None
        assert parse_loop_slash("/loopback 请测试") is None

    def test_not_at_start_not_triggered(self) -> None:
        assert parse_loop_slash("普通消息 /loop") is None
        assert parse_loop_slash("请解释 /loop 命令") is None

    def test_other_slash_commands_untouched(self) -> None:
        assert parse_loop_slash("/goal set xxx") is None
        assert parse_loop_slash("/review 1234") is None
        assert parse_loop_slash("") is None
        assert parse_loop_slash("普通消息") is None

    def test_empty_after_flag_extraction(self) -> None:
        # 参数全部剔除后任务为空：不拦截
        assert parse_loop_slash('/loop --verify "pytest"') is None


class TestAdapterModeInference:
    """LoopEngine._adapter_mode：canonical mode → adapter 级模式推导。"""

    @staticmethod
    def _engine(mode: str):
        from jiuwenswarm.channels.loop_cli.app import LoopEngine, LoopOptions

        opts = LoopOptions(task="t", cwd=".", mode=mode)
        return LoopEngine(opts, log=lambda *a, **k: None)

    def test_agent_family(self) -> None:
        assert self._engine("agent.work.normal")._adapter_mode() == "agent"
        assert self._engine("agent")._adapter_mode() == "agent"

    def test_code_family(self) -> None:
        # code.normal（旧 canonical）→ CodeAdapter；agent.code.* 首段是 agent，
        # 属 DeepAdapter 的 code 剖面（runtime 侧按 code profile 处理），
        # 两条入口语义等价，均可执行代码任务
        assert self._engine("code.normal")._adapter_mode() == "code"
        assert self._engine("code.plan")._adapter_mode() == "code"
        assert self._engine("agent.code.plan")._adapter_mode() == "agent"

    def test_team_family(self) -> None:
        assert self._engine("team.plan")._adapter_mode() == "team"
        assert self._engine("team.code.normal")._adapter_mode() == "team"

    def test_unknown_falls_back_to_code(self) -> None:
        assert self._engine("weird-mode")._adapter_mode() == "code"
        assert self._engine("")._adapter_mode() == "code"
