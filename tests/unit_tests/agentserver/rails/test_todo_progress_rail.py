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
from openjiuwen.core.foundation.llm import ToolMessage
from openjiuwen.core.single_agent.ability_manager import AbilityManager
from openjiuwen.core.sys_operation import LocalWorkConfig, SysOperation, SysOperationCard
from openjiuwen.harness.tools import create_todos_tool

from jiuwenswarm.agents.harness.common.rails import todo_progress_snapshot as snapshot_mod
from jiuwenswarm.agents.harness.common.rails.todo_progress_rail import TodoProgressRail
from jiuwenswarm.agents.harness.common.rails.todo_progress_receipt import _HINTS
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


class _Context:
    """模型上下文替身：只收集 rail 追加的消息。"""

    def __init__(self):
        self.messages = []

    async def add_messages(self, message):
        self.messages.append(message)
        return [message]


@pytest.fixture
def env(tmp_path):
    operation = SysOperation(
        SysOperationCard(id="todo-progress-fs", work_config=LocalWorkConfig(sandbox_root=[str(tmp_path)]))
    )
    workspace = SimpleNamespace(get_node_path=lambda _node: tmp_path)
    create, list_tool, get_tool, modify = create_todos_tool(operation, str(tmp_path), "cn", "main")
    manager = AbilityManager()
    manager.add([t.card for t in (create, list_tool, get_tool, modify)])
    agent = SimpleNamespace(
        deep_config=SimpleNamespace(sys_operation=operation, workspace=workspace, language="cn"),
        card=SimpleNamespace(id="main"),
        ability_manager=manager,
    )
    rail = TodoProgressRail(mode="agent")
    rail.init(agent)
    session = SimpleNamespace(get_session_id=lambda: SID)
    return SimpleNamespace(
        rail=rail, create=create, list_tool=list_tool, modify=modify, manager=manager,
        session=session, todo_file=tmp_path / SID / "todo.json", context=_Context(),
    )


def _tasks(*statuses: str, body: str = "task") -> list[dict]:
    return [
        {"id": f"t{i}", "content": f"{body}{i}", "activeForm": f"{body}{i}", "description": body, "status": s}
        for i, s in enumerate(statuses, 1)
    ]


async def _begin(env, **extra) -> AgentCallbackContext:
    ctx = AgentCallbackContext(agent=object())
    ctx.inputs = InvokeInputs(query="q", conversation_id=SID)
    ctx.session = env.session
    await env.rail.before_invoke(ctx)
    await _model_call(env, **extra)
    return ctx


async def _model_call(env, **extra) -> None:
    mctx = _inner_ctx(env, SimpleNamespace())
    mctx.extra.update(extra)
    await env.rail.before_model_call(mctx)


async def _iteration(env, times: int = 1) -> None:
    for _ in range(times):
        await env.rail.after_react_iteration(_inner_ctx(env, SimpleNamespace()))


def _inner_ctx(env, inputs) -> AgentCallbackContext:
    """与生产一致：内层 ReAct 循环的回调上下文是新建的，extra 不与外层 invoke 共享。"""
    return AgentCallbackContext(agent=object(), inputs=inputs, session=env.session, context=env.context)


async def _tool(env, ictx, tool, name: str, args: dict, call_id: str = "c1", *, started: bool = True,
                skip_before: bool = False):
    tctx = _inner_ctx(
        env, ToolCallInputs(tool_call=SimpleNamespace(id=call_id), tool_name=name, tool_args=args)
    )
    if not skip_before:  # 跳过写入前读取 = 写入前快照不可用，变化判定为 unknown
        await env.rail.before_tool_call(tctx)
    if started:
        tctx.inputs.execution_started = True
        try:
            tctx.inputs.tool_result = await tool.invoke(args, session=env.session)
            tctx.inputs.tool_msg = ToolMessage(content=str(tctx.inputs.tool_result), tool_call_id=call_id)
        except Exception:
            tctx.inputs.tool_result = None  # 与 ability_manager 一致：工具抛错时 tool_result/tool_msg 为空
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


# -- 串行执行 -------------------------------------------------------------

@pytest.mark.asyncio
async def test_before_invoke_serializes_todo_tools_idempotently(env):
    from openjiuwen.core.foundation.tool import ToolCard

    env.manager.add(ToolCard(id="biz", name="biz_tool"))
    assert env.manager.get("todo_modify").parallel_safe is True
    await _begin(env)
    await _begin(env)
    for name in ("todo_create", "todo_list", "todo_get", "todo_modify"):
        assert env.manager.get(name).parallel_safe is False
    assert env.manager.get("biz_tool").parallel_safe is True


@pytest.mark.asyncio
async def test_serialized_todo_writes_run_in_order_with_real_scheduler(env):
    """真实调度器：同一批里两个业务工具照常并行；随后的 create + 两次 modify 逐个独占执行，
    结果与发出顺序一致。"""
    import asyncio

    from openjiuwen.core.foundation.tool import ToolCard

    env.manager.add(ToolCard(id="biz", name="biz_tool"))
    await _begin(env)
    events: list[str] = []

    async def _run(label, factory):
        events.append(f"start:{label}")
        await asyncio.sleep(0)
        result = await factory()
        events.append(f"end:{label}")
        return result

    def _call(name):
        return SimpleNamespace(name=name, arguments="{}")

    calls = [_call("biz_tool"), _call("biz_tool"), _call("todo_create"), _call("todo_modify"), _call("todo_modify")]
    tasks = [
        _run("biz1", lambda: asyncio.sleep(0)),
        _run("biz2", lambda: asyncio.sleep(0)),
        _run("create", lambda: env.create.invoke({"tasks": _tasks("in_progress", "pending")}, session=env.session)),
        _run("m1", lambda: env.modify.invoke(
            {"action": "update", "todos": [{"id": "t1", "status": "completed"}]}, session=env.session)),
        _run("m2", lambda: env.modify.invoke(
            {"action": "update", "todos": [{"id": "t2", "status": "in_progress"}]}, session=env.session)),
    ]
    results = await AbilityManager._execute_parallel_tool_tasks(calls, tasks, tool_cards=env.manager._tools)
    assert not any(isinstance(r, BaseException) for r in results), results
    assert events[:2] == ["start:biz1", "start:biz2"]  # 业务工具仍并行：第二个在第一个结束前开始
    assert sorted(events[2:4]) == ["end:biz1", "end:biz2"]
    assert events[4:] == ["start:create", "end:create", "start:m1", "end:m1", "start:m2", "end:m2"]
    final = {t.id: t.status.value for t in await read_todo_snapshot(env.rail._tool, SID)}
    assert final == {"t1": "completed", "t2": "in_progress"}


@pytest.mark.asyncio
async def test_missing_todo_tools_logged_once_but_counted_every_run(env, caplog):
    env.manager.remove(["todo_create", "todo_list", "todo_get", "todo_modify"])
    for _ in range(2):
        ictx = await _begin(env)
        await _end(env, ictx)
    errors = [e for e in _events(caplog, "rail_error") if e["category"] == "missing_todo_tools"]
    assert len(errors) == 1 and errors[0]["session"]
    assert [e["rail_errors"] for e in _events(caplog, "run_end")] == [1, 1]


@pytest.mark.asyncio
async def test_serializes_even_when_rail_init_failed_or_no_session_id(env):
    agent = SimpleNamespace(ability_manager=env.manager)  # 缺 deep_config：快照读取不可用
    rail = TodoProgressRail()
    rail.init(agent)
    assert rail._tool is None
    ctx = AgentCallbackContext(agent=object())
    ctx.inputs = InvokeInputs(query="q", conversation_id=SID)
    ctx.session = SimpleNamespace(get_session_id=lambda: "")
    await rail.before_invoke(ctx)
    assert env.manager.get("todo_modify").parallel_safe is False


# -- 回执增强 -------------------------------------------------------------

def _suffix(tctx) -> str:
    original = str(tctx.inputs.tool_result)
    content = tctx.inputs.tool_msg.content
    assert content.startswith(original)  # 原回执保留在前
    return content[len(original):]


@pytest.mark.asyncio
async def test_modify_receipt_appends_list_and_hint(env, caplog):
    ictx = await _begin(env)
    await _tool(env, ictx, env.create, "todo_create", {"tasks": _tasks("in_progress", "pending")}, "c1")
    tctx = await _tool(env, ictx, env.modify, "todo_modify",
                       {"action": "update", "todos": [{"id": "t1", "status": "completed"}]}, "c2")
    suffix = _suffix(tctx)
    assert "当前 todo 列表：" in suffix
    assert "- t1 [completed] task1" in suffix and "- t2 [pending] task2" in suffix
    assert _HINTS["cn"]["start_next"] in suffix
    assert tctx.inputs.tool_msg.tool_call_id == "c2"
    assert isinstance(tctx.inputs.tool_result, dict)  # 界面卡片来源不变
    await _end(env, ictx)
    [end] = _events(caplog, "run_end")
    assert end["receipts"] == 2 and end["receipt_chars_max"] >= len(suffix) and end["receipt_chars"] > 0


@pytest.mark.asyncio
async def test_create_receipt_appends_hint_only(env):
    ictx = await _begin(env)
    tctx = await _tool(env, ictx, env.create, "todo_create", {"tasks": _tasks("in_progress", "pending")}, "c1")
    suffix = _suffix(tctx)
    assert "当前 todo 列表" not in suffix and _HINTS["cn"]["advance"] in suffix


@pytest.mark.asyncio
@pytest.mark.parametrize("count, update, expected", [
    # todo_create 固定把第一项设为 in_progress、其余为 pending，状态由随后的 modify 构造
    (2, [{"id": "t1", "status": "completed"}], "start_next"),
    (3, [{"id": "t1", "status": "completed"}, {"id": "t2", "status": "in_progress"}], "advance"),
    (2, [{"id": "t1", "status": "completed"}, {"id": "t2", "status": "in_progress"}], "last_item"),
    (2, [{"id": "t1", "status": "completed"}, {"id": "t2", "status": "completed"}], "all_done"),
])
async def test_receipt_hint_by_state(env, count, update, expected):
    ictx = await _begin(env)
    await _tool(env, ictx, env.create, "todo_create", {"tasks": _tasks(*["pending"] * count)}, "c1")
    tctx = await _tool(env, ictx, env.modify, "todo_modify", {"action": "update", "todos": update}, "c2")
    suffix = _suffix(tctx)
    assert suffix.endswith(_HINTS["cn"][expected])


def test_multiple_in_progress_is_not_last_item():
    """todo 工具不允许多个 in_progress，但文件可能被直接改过：这时不提示"仅剩一项"。"""
    from openjiuwen.harness.schema.task import TodoItem

    from jiuwenswarm.agents.harness.common.rails.todo_progress_receipt import pick_hint

    todos = [TodoItem(id=i, content=i, activeForm=i, description=i, status="in_progress") for i in ("a", "b")]
    assert pick_hint(todos, noop=False) == "advance"


@pytest.mark.asyncio
async def test_receipt_hint_empty_list(env):
    ictx = await _begin(env)
    await _tool(env, ictx, env.create, "todo_create", {"tasks": _tasks("pending")}, "c1")
    tctx = await _tool(env, ictx, env.modify, "todo_modify", {"action": "delete", "ids": ["t1"]}, "c2")
    assert _suffix(tctx).endswith(_HINTS["cn"]["empty"])


@pytest.mark.asyncio
async def test_receipt_noop_hint(env):
    ictx = await _begin(env)
    await _tool(env, ictx, env.create, "todo_create", {"tasks": _tasks("in_progress")}, "c1")
    tctx = await _tool(env, ictx, env.modify, "todo_modify", {"action": "delete", "ids": ["nope"]}, "c2")
    assert _suffix(tctx).endswith(_HINTS["cn"]["noop"])


@pytest.mark.asyncio
async def test_failed_write_is_not_enhanced(env):
    ictx = await _begin(env)
    await _tool(env, ictx, env.create, "todo_create", {"tasks": _tasks("in_progress")}, "c1")
    tctx = _inner_ctx(env, ToolCallInputs(tool_call=SimpleNamespace(id="c2"), tool_name="todo_modify", tool_args={}))
    await env.rail.before_tool_call(tctx)
    tctx.inputs.execution_started = True
    with pytest.raises(Exception):
        await env.modify.invoke({"action": "update", "todos": [{"id": "nope", "status": "completed"}]},
                                session=env.session)
    # 工具失败：tool_result 不是成功形态，但给模型的错误回执仍在，只有成功判定能挡住增强
    tctx.inputs.tool_result = None
    error_msg = ToolMessage(content="Error: task not found", tool_call_id="c2")
    tctx.inputs.tool_msg = error_msg
    await env.rail.after_tool_call(tctx)
    assert tctx.inputs.tool_msg is error_msg and _state(env).receipts == 1
    assert _state(env).writes_failed == 1


@pytest.mark.asyncio
async def test_unknown_change_gets_receipt_without_noop_hint(env):
    env.todo_file.parent.mkdir(parents=True)
    env.todo_file.write_text("{broken", encoding="utf-8")
    ictx = await _begin(env)
    tctx = await _tool(env, ictx, env.create, "todo_create", {"tasks": _tasks("in_progress", "pending")}, "c1")
    assert _state(env).change_unknown == 1
    suffix = _suffix(tctx)
    assert suffix.endswith(_HINTS["cn"]["advance"])


@pytest.mark.asyncio
async def test_post_write_snapshot_failure_keeps_original_receipt(env, monkeypatch):
    ictx = await _begin(env)
    await _tool(env, ictx, env.create, "todo_create", {"tasks": _tasks("in_progress")}, "c1")
    tctx = _inner_ctx(env, ToolCallInputs(tool_call=SimpleNamespace(id="c2"), tool_name="todo_modify", tool_args={}))
    await env.rail.before_tool_call(tctx)
    tctx.inputs.execution_started = True
    tctx.inputs.tool_result = await env.modify.invoke(
        {"action": "update", "todos": [{"id": "t1", "status": "completed"}]}, session=env.session)
    original = ToolMessage(content=str(tctx.inputs.tool_result), tool_call_id="c2")
    tctx.inputs.tool_msg = original

    def _fail(_path):
        raise PermissionError("denied")

    monkeypatch.setattr(snapshot_mod, "_read_text", _fail)
    await env.rail.after_tool_call(tctx)
    assert tctx.inputs.tool_msg is original and _state(env).receipts == 1


@pytest.mark.parametrize("language, lang, header", [("cn", "cn", "当前 todo 列表："),
                                                    ("en_US", "en", "Current todo list:")])
def test_receipt_language_and_truncation(language, lang, header):
    from openjiuwen.harness.schema.task import TodoItem

    from jiuwenswarm.agents.harness.common.rails.todo_progress_receipt import receipt_suffix

    def item(i, status, content="长" * 200):
        return TodoItem(id=f"id{i:02d}", content=content, activeForm="x", description="x", status=status)

    # 单条 content 超过 80 字截断
    todos = [item(0, "completed"), item(1, "in_progress", "y")]
    text = receipt_suffix("todo_modify", todos, noop=False, language=language)
    assert header in text and "- id01 [in_progress] y" in text
    assert "长" * 80 not in text and "长" * 79 + "…" in text
    assert text.endswith(_HINTS[lang]["last_item"])
    # 已结束的项省略后仍放得下：未完成项全部列出
    todos = [item(i, "completed") for i in range(10)] + [item(10, "in_progress"), item(11, "pending")]
    text = receipt_suffix("todo_modify", todos, noop=False, language=language)
    assert len(text) <= 600 and "id10" in text and "id11" in text and "id00" not in text
    # 未完成项也放不下：保留靠前的，末尾注明省略数
    todos = [item(0, "in_progress")] + [item(i, "pending") for i in range(1, 20)]
    text = receipt_suffix("todo_modify", todos, noop=False, language=language)
    assert len(text) <= 600 and "id00" in text and "id19" not in text
    assert text.endswith(_HINTS[lang]["advance"])


# -- 执行中提醒 -------------------------------------------------------------

NOOP_ARGS = {"action": "delete", "ids": ["nope"]}  # 工具成功、列表不变


def _injected(env) -> list[str]:
    return [m.content for m in env.context.messages]


async def _leave_unfinished(env, body: str = "task") -> None:
    """上一轮留下 t1 completed、t2 in_progress。"""
    ictx = await _begin(env)
    await _tool(env, ictx, env.create, "todo_create", {"tasks": _tasks("pending", "pending", body=body)}, "c1")
    await _tool(env, ictx, env.modify, "todo_modify",
                {"action": "update", "todos": [{"id": "t1", "status": "completed"},
                                               {"id": "t2", "status": "in_progress"}]}, "c2")
    await _end(env, ictx)


async def _finish_t2(env, ictx, call_id: str, **kw):
    return await _tool(env, ictx, env.modify, "todo_modify",
                       {"action": "update", "todos": [{"id": "t2", "status": "completed"}]}, call_id, **kw)


async def _remind_once(env, ictx) -> None:
    """建两项待办后空转到满间隔，注入一次提醒（迭代 11）。"""
    await _tool(env, ictx, env.create, "todo_create", {"tasks": _tasks("pending", "pending")}, "c0")
    await _iteration(env, 11)
    await _model_call(env)
    assert len(_injected(env)) == 1


@pytest.mark.asyncio
async def test_periodic_reminder_interval_boundaries(env, caplog):
    ictx = await _begin(env)
    await _tool(env, ictx, env.create, "todo_create", {"tasks": _tasks("pending", "pending")}, "c1")
    await _iteration(env)            # 迭代 1：列表变化
    await _iteration(env, 9)         # 迭代 10：距变化 9 次
    await _model_call(env)
    assert _injected(env) == []
    await _iteration(env)            # 迭代 11：距变化 10 次
    await _model_call(env)
    await _model_call(env)           # 同一迭代内再次调用模型不重复提醒
    assert len(_injected(env)) == 1 and _injected(env)[0].startswith("<system-reminder>")
    # 距上次提醒也要满 10 次
    await _iteration(env, 9)
    await _model_call(env)
    assert len(_injected(env)) == 1
    await _iteration(env)
    await _model_call(env)
    assert len(_injected(env)) == 2
    # 实际变化后重新计数：距变化不满 10 次不提醒，即使距上次提醒已满
    await _tool(env, ictx, env.modify, "todo_modify",
                {"action": "update", "todos": [{"id": "t1", "status": "completed"},
                                               {"id": "t2", "status": "in_progress"}]}, "c2")
    await _iteration(env, 10)
    await _model_call(env)
    assert len(_injected(env)) == 2
    await _iteration(env)
    await _model_call(env)
    assert len(_injected(env)) == 3
    await _end(env, ictx)
    [end] = _events(caplog, "run_end")
    assert (end["reminders"], end["rail_errors"]) == (3, 0)
    assert "restores" not in end
    injects = _events(caplog, "inject")
    assert [e["reminder_seq"] for e in injects] == [1, 2, 3]
    assert injects[0]["iters_since_change"] == 10 and injects[0]["unfinished"] == 2
    assert end["intervals"] == {"followed_changed": 1, "no_write": 2}


@pytest.mark.asyncio
async def test_noop_write_does_not_reset_reminder_count(env):
    ictx = await _begin(env)
    await _tool(env, ictx, env.create, "todo_create", {"tasks": _tasks("pending")}, "c1")
    await _iteration(env, 5)
    await _tool(env, ictx, env.modify, "todo_modify", NOOP_ARGS, "c2")
    await _iteration(env, 5)         # 迭代 10：距变化 9 次
    await _model_call(env)
    assert _injected(env) == []
    await _iteration(env)
    await _model_call(env)
    assert len(_injected(env)) == 1


@pytest.mark.asyncio
async def test_no_reminder_without_unfinished_items(env, caplog):
    ictx = await _begin(env)
    await _tool(env, ictx, env.create, "todo_create", {"tasks": _tasks("pending")}, "c1")
    await _tool(env, ictx, env.modify, "todo_modify",
                {"action": "update", "todos": [{"id": "t1", "status": "completed"}]}, "c2")
    await _iteration(env, 15)
    await _model_call(env)
    await _end(env, ictx)
    assert _injected(env) == [] and _events(caplog, "inject") == []


@pytest.mark.asyncio
async def test_new_turn_does_not_inject_leftover_list(env, caplog):
    await _leave_unfinished(env)
    ictx = await _begin(env)
    for _ in range(3):
        await _model_call(env)
    await _iteration(env, 9)         # 短任务：不满间隔，旧列表不出现
    await _model_call(env)
    await _end(env, ictx)
    assert _injected(env) == []
    end = _events(caplog, "run_end")[-1]
    assert (end["reminders"], end["had_todos"], end["writes_ok"]) == (0, True, 0)


@pytest.mark.asyncio
async def test_leftover_list_reminded_after_interval_in_long_task(env, caplog):
    """新一轮跑满间隔仍没动旧列表时照常提醒，由模型判断沿用、重写还是删除。"""
    await _leave_unfinished(env)
    ictx = await _begin(env)
    await _iteration(env, 10)
    await _model_call(env)
    [note] = _injected(env)
    assert "- t2 [in_progress] task2" in note and "todo_create" in note
    await _tool(env, ictx, env.modify, "todo_modify", {"action": "delete", "ids": ["t1", "t2"]}, "c3")
    await _end(env, ictx)
    end = _events(caplog, "run_end")[-1]
    assert end["intervals"] == {"followed_changed": 1}
    assert end["final"]["pending"] == end["final"]["in_progress"] == 0


async def _pause_after(env, idle: int) -> None:
    """建列表后空转 idle 次迭代，以问卷/审批暂停结束本次执行。"""
    ictx = await _begin(env)
    await _tool(env, ictx, env.create, "todo_create", {"tasks": _tasks("pending", "pending")}, "c1")
    await _iteration(env)            # 迭代 1：列表变化
    await _iteration(env, idle)
    await _end(env, ictx, "interrupt")


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", ["_resume_continuation", "_resumed_from_hitl"])
async def test_resume_continues_counts_from_pause(env, caplog, marker):
    await _pause_after(env, 7)       # 暂停时距变化 7 次
    ictx = await _begin(env, **{marker: True})
    await _iteration(env, 2)
    await _model_call(env)
    assert _injected(env) == []      # 累计 9 次
    await _iteration(env)
    await _model_call(env)
    assert len(_injected(env)) == 1  # 累计 10 次
    assert _events(caplog, "inject")[0]["iters_since_change"] == 10
    await _end(env, ictx)
    assert env.rail._carry == {}


@pytest.mark.asyncio
async def test_counts_not_carried_into_a_new_turn(env):
    await _pause_after(env, 7)
    ictx = await _begin(env)         # 没有续跑标记：用户发了新消息
    await _iteration(env, 3)
    await _model_call(env)
    assert _injected(env) == []
    await _end(env, ictx)
    assert env.rail._carry == {}     # 暂停时的计数已丢弃，不会留给之后的续跑


@pytest.mark.asyncio
async def test_pause_again_before_model_call_keeps_carried_counts(env, caplog):
    """续跑只执行了已批准的工具、没调模型就又暂停时，计数原样留到下次续跑。"""
    await _pause_after(env, 7)       # 暂停时距变化 7 次、距检查 8 次
    ictx = await _begin(env, _resumed_from_hitl=True)
    await _end(env, ictx, "interrupt")  # 还有工具等审批
    assert env.rail._carry[SID] == (7, 8)
    ictx = await _begin(env, _resumed_from_hitl=True)
    await _iteration(env, 3)
    await _model_call(env)
    assert _events(caplog, "inject")[0]["iters_since_change"] == 10
    await _end(env, ictx)


@pytest.mark.asyncio
async def test_change_in_unfinished_iteration_resets_carried_count(env):
    ictx = await _begin(env)
    await _tool(env, ictx, env.create, "todo_create", {"tasks": _tasks("pending")}, "c1")
    await _iteration(env, 12)
    await _tool(env, ictx, env.modify, "todo_modify",
                {"action": "update", "todos": [{"id": "t1", "status": "completed"}]}, "c2")
    await _end(env, ictx, "interrupt")  # 改完列表、迭代还没收尾就暂停
    assert env.rail._carry[SID][0] == 0


def test_carried_counts_are_capped(env):
    from jiuwenswarm.agents.harness.common.rails.todo_progress_rail import _CARRY_MAX, _RunState

    for i in range(_CARRY_MAX + 5):
        env.rail._keep_counts_for_resume(_RunState(session_id=f"s{i}"))
    assert len(env.rail._carry) == _CARRY_MAX and "s0" not in env.rail._carry


@pytest.mark.asyncio
async def test_reminder_check_reads_once_per_interval(env, monkeypatch):
    ictx = await _begin(env)
    await _tool(env, ictx, env.create, "todo_create", {"tasks": _tasks("pending")}, "c1")
    await _tool(env, ictx, env.modify, "todo_modify",
                {"action": "update", "todos": [{"id": "t1", "status": "completed"}]}, "c2")
    await _iteration(env)
    reads = []
    real_read = snapshot_mod._read_text
    monkeypatch.setattr(snapshot_mod, "_read_text", lambda path: reads.append(path) or real_read(path))
    await _iteration(env, 10)
    for _ in range(3):               # 满间隔但没有未完成项：只读一次
        await _model_call(env)
    assert len(reads) == 1
    await _iteration(env, 9)
    await _model_call(env)
    assert len(reads) == 1
    await _iteration(env)
    await _model_call(env)
    assert len(reads) == 2 and _injected(env) == []


@pytest.mark.asyncio
async def test_reminder_read_failure_logged_once_per_interval(env, caplog, monkeypatch):
    ictx = await _begin(env)
    await _tool(env, ictx, env.create, "todo_create", {"tasks": _tasks("pending")}, "c1")
    await _iteration(env, 11)

    def _fail(_path):
        raise PermissionError("denied")

    monkeypatch.setattr(snapshot_mod, "_read_text", _fail)
    for _ in range(3):
        await _model_call(env)
    assert _state(env).rail_errors == 1
    assert [e["hook"] for e in _events(caplog, "rail_error")] == ["before_model_call"]
    assert _injected(env) == []


@pytest.mark.asyncio
async def test_write_log_failure_is_rail_error(env, monkeypatch):
    ictx = await _begin(env)

    def _boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(TodoProgressRail, "_log_write", staticmethod(_boom))
    await _tool(env, ictx, env.create, "todo_create", {"tasks": _tasks("pending")}, "c1")
    st = _state(env)
    assert (st.writes_ok, st.changes, st.rail_errors) == (1, 1, 1)
    assert st.pre_write == {}


# -- 提醒区间与 write 日志 -------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("writes, expected", [
    (["noop", "changed"], "followed_changed"),
    (["unknown"], "followed_unknown"),
    (["unknown", "changed"], "followed_changed"),
    (["changed", "unknown"], "followed_changed"),
    (["noop"], "followed_noop"),
    ([], "no_write"),
])
async def test_interval_outcome_takes_strongest_write(env, caplog, writes, expected):
    ictx = await _begin(env)
    await _remind_once(env, ictx)
    for i, kind in enumerate(writes):
        if kind == "changed":
            await _finish_t2(env, ictx, f"w{i}")
        else:  # unknown：跳过写入前读取，变化无法判定
            await _tool(env, ictx, env.modify, "todo_modify", NOOP_ARGS, f"w{i}", skip_before=kind == "unknown")
    await _end(env, ictx)
    end = _events(caplog, "run_end")[-1]
    assert end["intervals"] == {expected: 1}
    assert [w["change"] for w in _events(caplog, "write")[1:]] == writes


@pytest.mark.asyncio
async def test_write_log_fields_and_after_reminder(env, caplog):
    ictx = await _begin(env)
    await _remind_once(env, ictx)
    [created] = _events(caplog, "write")
    assert (created["reminder_seq"], created["after_reminder"], created["iters_since_inject"]) == (0, False, None)
    await _tool(env, ictx, env.modify, "todo_modify", NOOP_ARGS, "w1")
    await _iteration(env)
    await _finish_t2(env, ictx, "w2")
    await _end(env, ictx)
    first, second = _events(caplog, "write")[1:]
    assert set(first) == {"run_id", "tool", "reminder_seq", "iteration", "result", "change",
                          "after_reminder", "iters_since_inject", "receipt_chars"}
    assert (first["after_reminder"], second["after_reminder"]) == (True, False)
    assert (first["iters_since_inject"], second["iters_since_inject"]) == (0, 1)
    assert first["reminder_seq"] == second["reminder_seq"] == 1
    assert first["run_id"] == _events(caplog, "run_end")[-1]["run_id"]
    assert first["result"] == "ok" and first["receipt_chars"] > 0


@pytest.mark.asyncio
async def test_failed_and_blocked_writes_in_write_log(env, caplog):
    ictx = await _begin(env)
    await _tool(env, ictx, env.create, "todo_create", {"tasks": _tasks("pending")}, "c1")
    await _tool(env, ictx, env.modify, "todo_modify",
                {"action": "update", "todos": [{"id": "nope", "status": "completed"}]}, "c2")
    await _tool(env, ictx, env.modify, "todo_modify", NOOP_ARGS, "c3", started=False)  # 执行前被拦下
    await _end(env, ictx)
    assert [(w["result"], w["receipt_chars"] > 0) for w in _events(caplog, "write")] == \
        [("ok", True), ("failed", False)]


@pytest.mark.asyncio
async def test_injected_text_reaches_model_but_not_logs(env, caplog):
    await _leave_unfinished(env, body=SENTINEL)
    ictx = await _begin(env)
    await _iteration(env, 10)
    await _model_call(env)
    await _finish_t2(env, ictx, "w1")
    await _end(env, ictx)
    assert len(_injected(env)) == 1 and SENTINEL in _injected(env)[0]
    assert _events(caplog, "inject") and _events(caplog, "write")
    assert not any(SENTINEL in line for line in _rail_lines(caplog))


@pytest.mark.parametrize("language", ["cn", "en_US"])
def test_reminder_is_capped_and_marked_internal(language):
    from openjiuwen.harness.schema.task import TodoItem

    from jiuwenswarm.agents.harness.common.rails.todo_progress_reminder import NOTE_MAX, build_reminder

    todos = [
        TodoItem(id=f"id{i:02d}", content="长" * 200, activeForm="x", description="x",
                 status="completed" if i < 5 else "pending")
        for i in range(40)
    ]
    reminder = build_reminder(todos, 12, language)
    assert len(reminder) <= NOTE_MAX
    assert reminder.startswith("<system-reminder>") and reminder.endswith("</system-reminder>")
    assert "id05" in reminder        # 列表放不下时先省略已关闭项
    assert "12" in reminder and "todo_create" in reminder


# -- 与上下文压缩的衔接 -----------------------------------------------------

@pytest.mark.asyncio
async def test_compression_reinjection_reads_same_todo_file(tmp_path):
    """按 DeepAgent 的真实构造取内层 ReAct 的上下文：压缩回注与 rail 读的是同一个文件。"""
    from openjiuwen.core.context_engine.processor.forked.compressor.support.util import (
        build_compressor_reinjected_state_message,
    )
    from openjiuwen.core.single_agent import AgentCard
    from openjiuwen.harness.deep_agent import DeepAgent
    from openjiuwen.harness.schema.config import DeepAgentConfig
    from openjiuwen.harness.workspace.workspace import Workspace, WorkspaceNode

    operation = SysOperation(
        SysOperationCard(id="todo-reinject-fs", work_config=LocalWorkConfig(sandbox_root=[str(tmp_path)]))
    )
    agent = DeepAgent(AgentCard(name="main"))
    agent.configure(DeepAgentConfig(workspace=Workspace(root_path=str(tmp_path)), sys_operation=operation,
                                    language="cn"))
    rail = TodoProgressRail()
    rail.init(agent)
    session = SimpleNamespace(get_session_id=lambda: SID, get_state=lambda *_a, **_k: None)
    context = await agent._create_react_agent().context_engine.create_context(session=session)

    # 回注按 <workspace_dir>/todo/<session_id>/todo.json 读，与 rail 的读取路径逐项一致
    assert context.session_id() == SID
    reinject_path = Path(context.workspace_dir()) / "todo" / SID / "todo.json"
    assert reinject_path.resolve() == Path(rail._tool._get_file_path(SID)).resolve()

    create, _, _, modify = create_todos_tool(
        operation, str(agent.deep_config.workspace.get_node_path(WorkspaceNode.TODO)), "cn", agent.card.id)
    await create.invoke({"tasks": _tasks("pending", "pending", "pending")}, session=session)
    await modify.invoke({"action": "update", "todos": [{"id": "t1", "status": "completed"},
                                                       {"id": "t2", "status": "in_progress"}]}, session=session)
    message = build_compressor_reinjected_state_message(
        source_messages=[], messages_to_keep=[], context=context, config=SimpleNamespace(),
        builder_names=["todo"], session_state={},
    )
    assert message is not None
    snapshot = await read_todo_snapshot(rail._tool, SID)
    assert [t.status.value for t in snapshot] == ["completed", "in_progress", "pending"]
    for item in snapshot:
        line = f"- [{item.status.value}] {item.id}: {item.content}"
        assert (line in message.content) == (item.status.value != "completed"), line


def test_dialogue_compression_keeps_current_round_notes():
    """本轮用户消息之后的写入回执与执行中提醒都在 Dialogue 压缩的保留范围内。"""
    from openjiuwen.core.context_engine.processor.forked.compressor.dialogue_compressor import (
        DialogueCompressor,
        DialogueCompressorConfig,
    )
    from openjiuwen.core.foundation.llm import AssistantMessage, UserMessage
    from openjiuwen.harness.schema.task import TodoItem

    from jiuwenswarm.agents.harness.common.rails.todo_progress_reminder import build_reminder

    todos = [TodoItem(id="t1", content="a", activeForm="a", description="a", status="in_progress")]
    earlier = [UserMessage(content="上一轮的问题"), AssistantMessage(content="上一轮的回答")]
    current = [
        UserMessage(content="本轮的问题"),
        AssistantMessage(content="", tool_calls=[{"id": "c1", "type": "function",
                                                  "function": {"name": "todo_modify", "arguments": "{}"}}]),
        ToolMessage(content="Successfully updated 1 task(s)", tool_call_id="c1"),
        UserMessage(content=build_reminder(todos, 10, "cn")),
    ]
    span = DialogueCompressor(DialogueCompressorConfig())._build_span(earlier + current)
    assert span.protected_tail == current
    assert span.messages_to_compress == earlier
