# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""TodoProgressRail 测试.

真实的 AgentCallbackContext / InvokeInputs / ToolCallInputs 与真实 todo 工具，
读写临时目录里的真实 todo.json；session、agent 只用提供所需属性的轻量替身。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    InvokeInputs,
    ToolCallInputs,
)
from openjiuwen.core.sys_operation import LocalWorkConfig, SysOperation, SysOperationCard
from openjiuwen.harness.tools import create_todos_tool

from jiuwenswarm.agents.harness.common.rails import todo_progress_snapshot as snapshot_mod
from jiuwenswarm.agents.harness.common.rails.todo_progress_rail import TodoProgressRail
from jiuwenswarm.agents.harness.common.rails.todo_progress_snapshot import read_todo_snapshot

SID = "desktop_test_session_0001"
SENTINEL = "SENTINEL_TODO_BODY_7f3a"


@pytest.fixture
def caplog(caplog):
    """jiuwenswarm 根日志器 propagate=False，把捕获 handler 直接挂上去。"""
    root = logging.getLogger("jiuwenswarm")
    old_level = root.level
    root.addHandler(caplog.handler)
    root.setLevel(logging.DEBUG)
    yield caplog
    root.removeHandler(caplog.handler)
    root.setLevel(old_level)


@pytest.fixture
def env(tmp_path):
    operation = SysOperation(
        SysOperationCard(id="todo-progress-fs", work_config=LocalWorkConfig(sandbox_root=[str(tmp_path)]))
    )
    workspace = SimpleNamespace(get_node_path=lambda _node: tmp_path)
    agent = SimpleNamespace(
        deep_config=SimpleNamespace(sys_operation=operation, workspace=workspace, language="cn"),
        card=SimpleNamespace(id="main"),
    )
    rail = TodoProgressRail(mode="agent")
    rail.init(agent)
    create, list_tool, _get, modify = create_todos_tool(operation, str(tmp_path), "cn", "main")
    session = SimpleNamespace(get_session_id=lambda: SID)
    return SimpleNamespace(
        rail=rail, create=create, list_tool=list_tool, modify=modify,
        session=session, todo_file=tmp_path / SID / "todo.json",
    )


def _tasks(*statuses: str, body: str = "task") -> list[dict]:
    return [
        {"id": f"t{i}", "content": f"{body}{i}", "activeForm": f"{body}{i}", "description": body, "status": s}
        for i, s in enumerate(statuses, 1)
    ]


async def _begin(env) -> AgentCallbackContext:
    ctx = AgentCallbackContext(agent=object())
    ctx.inputs = InvokeInputs(query="q", conversation_id=SID)
    ctx.session = env.session
    await env.rail.before_invoke(ctx)
    await env.rail.before_model_call(_inner_ctx(env, SimpleNamespace()))
    return ctx


async def _iteration(env) -> None:
    await env.rail.after_react_iteration(_inner_ctx(env, SimpleNamespace()))


def _inner_ctx(env, inputs) -> AgentCallbackContext:
    """与生产一致：内层 ReAct 循环的回调上下文是新建的，extra 不与外层 invoke 共享。"""
    return AgentCallbackContext(agent=object(), inputs=inputs, session=env.session)


async def _tool(env, ictx, tool, name: str, args: dict, call_id: str = "c1", *, started: bool = True):
    tctx = _inner_ctx(
        env, ToolCallInputs(tool_call=SimpleNamespace(id=call_id), tool_name=name, tool_args=args)
    )
    await env.rail.before_tool_call(tctx)
    if started:
        tctx.inputs.execution_started = True
        try:
            tctx.inputs.tool_result = await tool.invoke(args, session=env.session)
        except Exception:
            tctx.inputs.tool_result = None  # 与 ability_manager 一致：工具抛错时 tool_result 为空
    await env.rail.after_tool_call(tctx)
    return tctx


async def _end(env, ictx, result_type: str = "answer") -> None:
    ictx.inputs.result = {"result_type": result_type}
    await env.rail.after_invoke(ictx)


def _state(env):
    return env.rail._states[SID]


def _rail_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("[TodoProgress]")]


def _events(caplog, event: str) -> list[dict]:
    out = []
    for rec in caplog.records:
        msg = rec.getMessage()
        prefix = f"[TodoProgress] {event} "
        if msg.startswith(prefix):
            out.append(json.loads(msg[len(prefix):]))
    return out


# -- 快照读取 -------------------------------------------------------------

@pytest.mark.asyncio
async def test_snapshot_reads_same_file_as_todo_tools_and_is_stable(env):
    assert await read_todo_snapshot(env.rail._tool, SID) == []
    await env.create.invoke({"tasks": _tasks("in_progress", "pending")}, session=env.session)
    assert env.todo_file.is_file()
    first = await read_todo_snapshot(env.rail._tool, SID)
    second = await read_todo_snapshot(env.rail._tool, SID)
    assert [t.id for t in first] == ["t1", "t2"]
    assert [t.to_dict() for t in first] == [t.to_dict() for t in second]


# -- 写入判定 -------------------------------------------------------------

@pytest.mark.asyncio
async def test_write_outcomes_are_counted_separately(env, caplog):
    ictx = await _begin(env)
    await _tool(env, ictx, env.create, "todo_create", {"tasks": _tasks("in_progress", "pending")}, "c1")
    await _iteration(env)
    # 实际变化
    await _tool(env, ictx, env.modify, "todo_modify",
                {"action": "update", "todos": [{"id": "t1", "status": "completed"}]}, "c2")
    # 重复写相同状态
    await _tool(env, ictx, env.modify, "todo_modify",
                {"action": "update", "todos": [{"id": "t1", "status": "completed"}]}, "c3")
    # 删除不存在的 id：工具成功但列表不变
    await _tool(env, ictx, env.modify, "todo_modify", {"action": "delete", "ids": ["nope"]}, "c4")
    # 工具失败（更新不存在的 id 会抛错），文件不变
    await _tool(env, ictx, env.modify, "todo_modify",
                {"action": "update", "todos": [{"id": "nope", "status": "completed"}]}, "c5")
    await _iteration(env)
    st = _state(env)
    assert (st.writes_ok, st.writes_failed) == (4, 1)
    assert (st.changes, st.noops, st.change_unknown) == (2, 3, 0)
    assert st.iteration == 2 and st.last_change_iter == 2
    assert st.pre_write == {}
    await _end(env, ictx)
    [end] = _events(caplog, "run_end")
    assert end["final"] == {"cancelled": 0, "completed": 1, "in_progress": 0, "pending": 1}
    assert end["had_todos"] is True and end["result_type"] == "answer" and end["rail_errors"] == 0
    assert SID not in env.rail._states


@pytest.mark.asyncio
async def test_change_iteration_only_reset_by_real_change(env):
    ictx = await _begin(env)
    await _tool(env, ictx, env.create, "todo_create", {"tasks": _tasks("in_progress")}, "c1")
    await _iteration(env)  # iter 1：有变化
    await _tool(env, ictx, env.modify, "todo_modify", {"action": "delete", "ids": ["nope"]}, "c2")
    await _iteration(env)  # iter 2：无变化
    st = _state(env)
    assert (st.iteration, st.last_change_iter) == (2, 1)


@pytest.mark.asyncio
async def test_tool_blocked_before_execution_is_not_a_write(env):
    ictx = await _begin(env)
    await _tool(env, ictx, env.create, "todo_create", {"tasks": _tasks("pending")}, "c1", started=False)
    st = _state(env)
    assert (st.writes_ok, st.writes_failed, st.changes, st.noops, st.change_unknown) == (0, 0, 0, 0, 0)
    assert st.pre_write == {}


@pytest.mark.asyncio
async def test_read_tools_are_not_writes(env):
    ictx = await _begin(env)
    await _tool(env, ictx, env.list_tool, "todo_list", {}, "c1")
    st = _state(env)
    assert (st.writes_ok, st.writes_failed) == (0, 0)


# -- 快照不可用 -----------------------------------------------------------

@pytest.mark.asyncio
async def test_pre_write_snapshot_unavailable(env, caplog):
    env.todo_file.parent.mkdir(parents=True)
    env.todo_file.write_text("{broken", encoding="utf-8")
    ictx = await _begin(env)  # 首次读取失败：rail_errors=1
    await _tool(env, ictx, env.create, "todo_create", {"tasks": _tasks("in_progress")}, "c1")  # 写入前失败：+1
    await _iteration(env)
    st = _state(env)
    assert (st.rail_errors, st.writes_ok, st.change_unknown, st.changes) == (2, 1, 1, 0)
    assert st.last_change_iter == 0  # 无法判断不重置间隔
    assert st.pre_write == {}
    assert [e["category"] for e in _events(caplog, "rail_error")] == ["invalid_json", "invalid_json"]


@pytest.mark.asyncio
async def test_post_write_snapshot_unavailable(env, monkeypatch):
    await _begin(env)
    tctx = _inner_ctx(env, ToolCallInputs(tool_call=SimpleNamespace(id="c1"), tool_name="todo_create",
                                          tool_args={"tasks": _tasks("pending")}))
    await env.rail.before_tool_call(tctx)
    tctx.inputs.execution_started = True
    tctx.inputs.tool_result = await env.create.invoke({"tasks": _tasks("pending")}, session=env.session)

    def _fail(_path):
        raise PermissionError("denied")

    monkeypatch.setattr(snapshot_mod, "_read_text", _fail)
    await env.rail.after_tool_call(tctx)
    st = _state(env)
    assert (st.writes_ok, st.change_unknown, st.rail_errors) == (1, 1, 1)
    assert st.pre_write == {}


@pytest.mark.asyncio
async def test_end_snapshot_unavailable_still_logs_run_end(env, caplog, monkeypatch):
    ictx = await _begin(env)
    await _tool(env, ictx, env.create, "todo_create", {"tasks": _tasks("pending")}, "c1")

    def _fail(_path):
        raise PermissionError("denied")

    monkeypatch.setattr(snapshot_mod, "_read_text", _fail)
    await _end(env, ictx)
    [end] = _events(caplog, "run_end")
    assert end["final"] == "unknown" and end["rail_errors"] == 1
    assert SID not in env.rail._states


# -- 统计口径 -------------------------------------------------------------

@pytest.mark.asyncio
async def test_had_todos_counts_leftovers_without_writes(env, caplog):
    await env.create.invoke({"tasks": _tasks("in_progress", "pending")}, session=env.session)  # 上一轮留下
    ictx = await _begin(env)
    await _end(env, ictx)
    [end] = _events(caplog, "run_end")
    assert end["had_todos"] is True and end["writes_ok"] == 0
    assert end["final"]["in_progress"] == 1 and end["final"]["pending"] == 1


@pytest.mark.asyncio
async def test_had_todos_stays_true_after_list_cleared(env, caplog):
    ictx = await _begin(env)
    await _tool(env, ictx, env.create, "todo_create", {"tasks": _tasks("pending")}, "c1")
    await _tool(env, ictx, env.modify, "todo_modify", {"action": "delete", "ids": ["t1"]}, "c2")
    await _end(env, ictx)
    [end] = _events(caplog, "run_end")
    assert end["had_todos"] is True and sum(end["final"].values()) == 0


@pytest.mark.asyncio
async def test_model_taken_from_responses(env, caplog):
    from openjiuwen.core.foundation.llm.schema.message import UsageMetadata

    ictx = await _begin(env)
    mctx = _inner_ctx(env, None)
    for usage in (
        UsageMetadata(model_name="DeepSeek_X"),
        {"model_name": "DeepSeek_X"},
        {},  # 响应不带模型名：保留之前取到的
        None,  # 响应没带 usage：不计入，不报错
    ):
        mctx.inputs = SimpleNamespace(response=SimpleNamespace(usage_metadata=usage))
        await env.rail.after_model_call(mctx)
    await _end(env, ictx)
    [end] = _events(caplog, "run_end")
    assert end["model"] == "DeepSeek_X" and end["rail_errors"] == 0


@pytest.mark.asyncio
async def test_subagent_style_invoke_without_session_is_ignored(env, caplog):
    ctx = AgentCallbackContext(agent=object())
    ctx.inputs = InvokeInputs(query="q", conversation_id=SID)
    await env.rail.before_invoke(ctx)
    await env.rail.after_invoke(ctx)
    assert env.rail._states == {}
    assert _events(caplog, "run_end") == [] and _events(caplog, "rail_error") == []


# -- 异常与日志脱敏 ---------------------------------------------------------

_BAD_FILES = {
    "broken_json": ("{\"id\": \"" + SENTINEL, "invalid_json"),
    "root_not_array": (json.dumps({"content": SENTINEL}), "invalid_record"),
    "record_not_object": (json.dumps([SENTINEL]), "invalid_record"),
    "missing_id": (json.dumps([{"content": SENTINEL, "status": "pending"}]), "invalid_record"),
    "duplicate_id": (json.dumps([{"id": "a", "status": "pending", "content": SENTINEL}] * 2), "invalid_record"),
    "bad_status": (json.dumps([{"id": "a", "status": SENTINEL, "content": SENTINEL}]), "invalid_record"),
    "pydantic_type": (json.dumps([{"id": "a", "status": "pending", "depends_on": SENTINEL,
                                   "activeForm": SENTINEL, "description": SENTINEL}]), "invalid_record"),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(_BAD_FILES))
async def test_bad_files_do_not_raise_and_do_not_leak(env, caplog, case):
    text, category = _BAD_FILES[case]
    env.todo_file.parent.mkdir(parents=True)
    env.todo_file.write_text(text, encoding="utf-8")
    ictx = await _begin(env)
    await _end(env, ictx)
    errors = _events(caplog, "rail_error")
    assert [e["category"] for e in errors] == [category, category]
    for e in errors:
        assert set(e) <= {"hook", "category", "exc_type", "detail", "frames", "session"}
    [end] = _events(caplog, "run_end")
    assert end["final"] == "unknown" and end["rail_errors"] == 2
    assert all(SENTINEL not in m for m in _rail_lines(caplog))


@pytest.mark.asyncio
async def test_unexpected_error_logs_frames_only(env, caplog, monkeypatch):

    def _boom(_path):
        raise RuntimeError(SENTINEL)

    env.todo_file.parent.mkdir(parents=True)
    env.todo_file.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(snapshot_mod, "_read_text", _boom)
    ictx = await _begin(env)
    errors = _events(caplog, "rail_error")
    assert errors[0]["category"] == "unexpected" and errors[0]["exc_type"] == "RuntimeError"
    assert errors[0]["frames"] and all(f.count(":") == 2 for f in errors[0]["frames"])
    assert _state(env).rail_errors == 1
    assert all(SENTINEL not in m for m in _rail_lines(caplog))
    await _end(env, ictx)


@pytest.mark.asyncio
async def test_normal_path_has_no_rail_errors(env, caplog):
    ictx = await _begin(env)
    await _tool(env, ictx, env.create, "todo_create", {"tasks": _tasks("in_progress", "pending")}, "c1")
    await _tool(env, ictx, env.modify, "todo_modify",
                {"action": "update", "todos": [{"id": "t1", "status": "completed"},
                                               {"id": "t2", "status": "in_progress"}]}, "c2")
    await _tool(env, ictx, env.modify, "todo_modify",
                {"action": "update", "todos": [{"id": "t2", "status": "completed"}]}, "c3")
    await _end(env, ictx)
    assert _events(caplog, "rail_error") == []
    [end] = _events(caplog, "run_end")
    assert end["rail_errors"] == 0 and end["final"]["completed"] == 2


def test_init_failure_disables_rail(caplog):
    rail = TodoProgressRail()
    rail.init(SimpleNamespace())  # 缺 deep_config
    assert rail._tool is None
    assert _events(caplog, "rail_error")[0]["hook"] == "init"



# -- 统计脚本 -------------------------------------------------------------

@pytest.fixture
def stats(monkeypatch):
    import importlib.util
    import sys

    script = Path(__file__).resolve().parents[4] / "scripts" / "todo_progress_stats.py"
    spec = importlib.util.spec_from_file_location("todo_progress_stats", script)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)  # dataclass 需要按模块名回查
    spec.loader.exec_module(module)
    return module


def test_stats_self_test(stats):
    """脚本内置样例：结束快照 unknown、有效分母为 0、多文件重复、各比率与中位数。"""
    stats.self_test()


@pytest.mark.asyncio
async def test_stats_script_parses_rail_output(env, caplog, stats):
    ictx = await _begin(env)
    await _tool(env, ictx, env.create, "todo_create", {"tasks": _tasks("in_progress", "pending")}, "c1")
    await _end(env, ictx)
    groups = stats.aggregate(stats.parse_lines(_rail_lines(caplog)))
    s = groups["unknown"]  # 测试中没有模型调用，model 记为 unknown
    assert (s.runs, s.residual_num, s.residual_den, s.writes_ok, s.changes) == (1, 1, 1, 1, 1)


@pytest.mark.asyncio
async def test_stats_counts_each_run_once_across_log_files(env, caplog, stats, tmp_path):
    for _ in range(2):
        ictx = await _begin(env)
        await _tool(env, ictx, env.create, "todo_create", {"tasks": _tasks("in_progress", "pending")}, "c1")
        await _end(env, ictx)
    ends = _events(caplog, "run_end")
    assert len({e["run_id"] for e in ends}) == 2
    # 同一批日志行同时写进分类日志、full.log 和桌面端汇总日志
    lines = "".join(f"{line}\n" for line in _rail_lines(caplog))
    log_dir = tmp_path / "logs"
    for name in ("gateway.log", "full.log", "system.log"):
        (log_dir / name).parent.mkdir(parents=True, exist_ok=True)
        (log_dir / name).write_text(lines, encoding="utf-8")
    records = []
    for f in stats._iter_files([str(log_dir), str(log_dir / "full.log")]):  # 目录与其中文件重叠传入
        records.extend(stats.parse_lines(f.read_text(encoding="utf-8").splitlines()))
    assert len(records) == 8
    d = stats.dedupe(records)
    assert (len(d.records), d.duplicates, d.without_run_id) == (2, 6, 0)
    s = stats.aggregate(d.records)["unknown"]
    assert (s.runs, s.writes_ok, s.residual_den) == (2, 2, 2)


@pytest.mark.asyncio
async def test_stats_counts_unknown_end_even_without_todos(env, caplog, stats):
    env.todo_file.parent.mkdir(parents=True)
    env.todo_file.write_text("{broken", encoding="utf-8")
    ictx = await _begin(env)
    await _end(env, ictx)
    [end] = _events(caplog, "run_end")
    assert end["final"] == "unknown" and end["had_todos"] is False and end["rail_errors"] == 2
    s = stats.aggregate(stats.parse_lines(_rail_lines(caplog)))["unknown"]
    assert (s.end_unknown, s.residual_den) == (1, 0)


@pytest.mark.asyncio
async def test_unreadable_path_is_unknown_not_empty(env, caplog, stats):
    ictx = await _begin(env)
    await _tool(env, ictx, env.create, "todo_create", {"tasks": _tasks("in_progress", "pending")}, "c1")
    env.todo_file.unlink()
    env.todo_file.mkdir()  # 路径还在但读不了：不能当作空列表
    await _end(env, ictx)
    [end] = _events(caplog, "run_end")
    assert end["final"] == "unknown" and end["rail_errors"] == 1
    assert [e["category"] for e in _events(caplog, "rail_error")] == ["read_failed"]
    s = stats.aggregate(stats.parse_lines(_rail_lines(caplog)))["unknown"]
    assert (s.residual_den, s.end_unknown, s.end_unknown_answer) == (0, 1, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("result_type", ["error", "interrupt"])
async def test_stats_counts_unknown_end_for_non_answer_runs(env, caplog, stats, result_type):
    env.todo_file.parent.mkdir(parents=True)
    env.todo_file.write_text("{broken", encoding="utf-8")
    ictx = await _begin(env)
    await _end(env, ictx, result_type)
    [end] = _events(caplog, "run_end")
    assert end["final"] == "unknown" and end["result_type"] == result_type
    s = stats.aggregate(stats.parse_lines(_rail_lines(caplog)))["unknown"]
    assert (s.end_unknown, s.end_unknown_answer, s.residual_den) == (1, 0, 0)
