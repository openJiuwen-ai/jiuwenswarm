#!/usr/bin/env python3
"""L2 hot-load worker — runs in a dedicated subprocess.

Owns all openjiuwen runtime side effects (Runner.resource_mgr, DeepAgent).
Prints one JSON object on the last stdout line for the parent controller.

Exit codes:
    0  pass
    1  fail (package load/unload errors)
    2  skip (environment / import / setup problems)
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any


def write_stdout(text: str) -> None:
    """product output to fd 1."""
    os.write(1, text.encode("utf-8"))


def _summarize_refs(record: Any) -> str:
    counts: dict[str, int] = {}
    for ref in record.refs:
        counts[ref.kind.value] = counts.get(ref.kind.value, 0) + 1
    return ", ".join(f"{kind}={n}" for kind, n in sorted(counts.items())) or "无"


def _result(
    status: str,
    *,
    errors: list[list[str]] | None = None,
    notes: list[str] | None = None,
    skip_reason: str = "",
    skip_fix: str = "",
) -> dict[str, Any]:
    return {
        "status": status,
        "errors": errors or [],
        "notes": notes or [],
        "skip_reason": skip_reason,
        "skip_fix": skip_fix,
    }


def _build_rail_smoke_inputs(event: Any) -> Any:
    """Build benign typed inputs for one rail callback event."""
    from openjiuwen.core.single_agent.rail.base import (
        AgentCallbackEvent,
        InvokeInputs,
        ModelCallInputs,
        SteeringDrainInputs,
        TaskIterationInputs,
        ToolCallInputs,
        UserMessageInputs,
    )

    if event in (AgentCallbackEvent.BEFORE_INVOKE, AgentCallbackEvent.AFTER_INVOKE):
        return InvokeInputs(query="template validation", result={})
    if event in (
        AgentCallbackEvent.BEFORE_MODEL_CALL,
        AgentCallbackEvent.AFTER_MODEL_CALL,
        AgentCallbackEvent.ON_MODEL_EXCEPTION,
    ):
        return ModelCallInputs(messages=[], tools=[], response={})
    if event in (
        AgentCallbackEvent.BEFORE_TOOL_CALL,
        AgentCallbackEvent.AFTER_TOOL_CALL,
        AgentCallbackEvent.ON_TOOL_EXCEPTION,
    ):
        return ToolCallInputs(tool_name="", tool_args={}, tool_result={})
    if event in (
        AgentCallbackEvent.BEFORE_TASK_ITERATION,
        AgentCallbackEvent.AFTER_TASK_ITERATION,
    ):
        return TaskIterationInputs(
            iteration=1,
            loop_event=None,
            query="template validation",
            result={},
        )
    if event == AgentCallbackEvent.ON_USER_MESSAGE:
        return UserMessageInputs(parts=["template validation"])
    if event == AgentCallbackEvent.BEFORE_STEERING_DRAIN:
        return SteeringDrainInputs(pending=1)
    return {}


async def _smoke_rail_callbacks(
    rail: Any,
    *,
    identity: str,
    agent: Any,
    session: Any,
) -> list[list[str]]:
    """Invoke every registered rail callback once with a benign runtime context."""
    from openjiuwen.core.single_agent.rail.base import AgentCallbackContext

    errors: list[list[str]] = []
    for event, callback in rail.get_callbacks().items():
        if not callable(callback):
            continue
        ctx = AgentCallbackContext(
            agent=agent,
            event=event,
            inputs=_build_rail_smoke_inputs(event),
            session=session,
        )
        if event.value.startswith("on_") and event.value.endswith("_exception"):
            ctx.exception = RuntimeError("template validation smoke exception")
        try:
            result = callback(ctx)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            errors.append(
                [
                    f"Rail callback 冒烟失败: {identity}.{event.value}: {exc}",
                    "",
                ]
            )
    return errors


async def _hot_load(pkg: Path) -> dict[str, Any]:
    try:
        from openjiuwen.core.runner import Runner
        from openjiuwen.core.session.agent import create_agent_session
        from openjiuwen.core.single_agent.schema.agent_card import AgentCard
        from openjiuwen.core.sys_operation import (
            LocalWorkConfig,
            OperationMode,
            SysOperationCard,
        )
        from openjiuwen.harness.deep_agent import DeepAgent
        from openjiuwen.harness.rails.skills.skill_use_rail import SkillUseRail
        from openjiuwen.harness.schema.config import DeepAgentConfig
        from openjiuwen.harness.workspace.workspace import Workspace
    except ImportError as exc:
        return _result(
            "skip",
            skip_reason=f"当前 Python 环境未安装 openjiuwen（{exc}）",
            skip_fix="改用 JiuwenSwarm 的 venv python 重跑；这不是包的问题",
        )

    work_dir = Path(tempfile.mkdtemp(prefix="validate_agent_template_"))
    agent = None
    sys_op_id: str | None = None
    errors: list[list[str]] = []
    notes: list[str] = []

    try:
        # 第一段：搭最简 agent。这里失败属于校验环境问题，不是包的问题。
        try:
            sys_op_id = f"validate_tpl_{uuid.uuid4().hex[:8]}"
            Runner.resource_mgr.add_sys_operation(
                SysOperationCard(
                    id=sys_op_id,
                    mode=OperationMode.LOCAL,
                    work_config=LocalWorkConfig(work_dir=str(work_dir)),
                )
            )
            sys_operation = Runner.resource_mgr.get_sys_operation(sys_op_id)
            if sys_operation is None:
                raise RuntimeError(f"SysOperation 注册后取不回来: {sys_op_id}")
            agent = DeepAgent(
                AgentCard(
                    id="template-validator",
                    name="template-validator",
                    description="agent template dry-run validator",
                )
            )
            agent.configure(
                DeepAgentConfig(
                    workspace=Workspace(root_path=str(work_dir / "workspace")),
                    sys_operation=sys_operation,
                    language="cn",
                    auto_create_workspace=True,
                    enable_task_loop=False,
                    enable_skill_discovery=True,
                    # 热加载 skill 要求 host 上已存在 SkillUseRail，加载器不会自动创建
                    rails=[
                        SkillUseRail(
                            skills_dir=[], skill_mode="all", include_tools=False
                        )
                    ],
                )
            )
            await agent.ensure_initialized()
            if not hasattr(agent, "load_agent_template"):
                raise RuntimeError(
                    "当前 openjiuwen 版本的 DeepAgent 没有 load_agent_template 方法"
                )
        except Exception as exc:
            return _result(
                "skip",
                skip_reason=f"校验环境搭建失败: {exc}",
                skip_fix="这不是包的问题，请检查 openjiuwen 运行环境",
            )

        # 第二段：真实加载链。这里失败才是包的问题。
        try:
            record = await agent.load_agent_template(str(pkg))
        except Exception as exc:
            errors.append([f"load_agent_template 失败: {exc}", ""])
            return _result("fail", errors=errors)

        notes.append(f"已绑定 {len(record.refs)} 项：{_summarize_refs(record)}")

        # 第三段：对已绑定 Tool / Rail 做最小运行时冒烟。
        test_session = create_agent_session(
            session_id=f"validate_template_{uuid.uuid4().hex[:8]}",
            card=agent.card,
            close_stream_on_post_run=False,
        )
        await test_session.pre_run(inputs={"query": "template validation"})
        try:
            for ref in record.refs:
                if ref.kind.value == "tool":
                    tool = Runner.resource_mgr.get_tool(
                        ref.identity, session=test_session
                    )
                    if tool is None:
                        errors.append(
                            [f"Tool 已绑定但无法取得实例: {ref.identity}", ""]
                        )
                        continue
                    try:
                        result = await tool.invoke({}, session=test_session)
                    except Exception as exc:
                        errors.append(
                            [f"Tool.invoke 冒烟失败: {ref.identity}: {exc}", ""]
                        )
                        continue
                    if not isinstance(result, dict):
                        errors.append(
                            [
                                f"Tool.invoke 必须返回 dict: {ref.identity}，实际 {type(result).__name__}",
                                "",
                            ]
                        )
                elif ref.kind.value == "rail":
                    rail = agent.find_rail_by_name(ref.identity)
                    if rail is None:
                        errors.append(
                            [f"Rail 已绑定但无法取得实例: {ref.identity}", ""]
                        )
                        continue
                    callbacks = rail.get_callbacks()
                    if not callbacks:
                        errors.append(
                            [f"Rail 未注册任何有效 callback: {ref.identity}", ""]
                        )
                        continue
                    for event, callback in callbacks.items():
                        if not callable(callback):
                            errors.append(
                                [
                                    f"Rail callback 不可调用: {ref.identity}.{event.value}",
                                    "",
                                ]
                            )
                    callback_errors = await _smoke_rail_callbacks(
                        rail,
                        identity=ref.identity,
                        agent=agent,
                        session=test_session,
                    )
                    errors.extend(callback_errors)
                    if not callback_errors:
                        notes.append(
                            f"Rail callback 冒烟通过: {ref.identity}（{len(callbacks)} 个）"
                        )
        finally:
            await test_session.post_run()

        try:
            unloaded = await agent.unload_extension(record)
        except Exception as exc:
            errors.append([f"unload_extension 失败: {exc}", ""])
            return _result("fail", errors=errors, notes=notes)

        if len(unloaded) != len(record.refs):
            errors.append(
                [
                    f"unload 不完整：绑定 {len(record.refs)} 项，卸载 {len(unloaded)} 项",
                    "",
                ]
            )
            return _result("fail", errors=errors, notes=notes)

        if errors:
            return _result("fail", errors=errors, notes=notes)

        return _result("pass", notes=notes)
    finally:
        if sys_op_id is not None:
            try:
                from openjiuwen.core.runner import Runner

                Runner.resource_mgr.remove_sys_operation(sys_op_id)
            except Exception as exc:
                logging.getLogger(__name__).debug(
                    "cleanup remove_sys_operation failed: %s", exc
                )
        shutdown = getattr(agent, "shutdown", None)
        if shutdown is not None:
            try:
                await shutdown()
            except Exception as exc:
                logging.getLogger(__name__).debug(
                    "cleanup agent.shutdown failed: %s", exc
                )
        shutil.rmtree(work_dir, ignore_errors=True)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if len(sys.argv) != 2:
        write_stdout(
            json.dumps(
                _result(
                    "skip",
                    skip_reason="Usage: validate_hot_load_worker.py <package-dir>",
                    skip_fix="由 validate_template.py 以子进程方式调用",
                ),
                ensure_ascii=False,
            )
            + "\n"
        )
        return 2

    pkg = Path(sys.argv[1]).expanduser().resolve()
    if not pkg.is_dir():
        write_stdout(
            json.dumps(
                _result(
                    "skip",
                    skip_reason=f"包目录不存在: {pkg}",
                    skip_fix="检查传入路径",
                ),
                ensure_ascii=False,
            )
            + "\n"
        )
        return 2

    try:
        result = asyncio.run(_hot_load(pkg))
    except Exception as exc:
        result = _result(
            "skip",
            skip_reason=f"热加载校验无法执行: {exc}",
            skip_fix="这不是包的问题，请检查运行环境",
        )

    write_stdout(json.dumps(result, ensure_ascii=False) + "\n")
    status = result.get("status")
    if status == "pass":
        return 0
    if status == "fail":
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
