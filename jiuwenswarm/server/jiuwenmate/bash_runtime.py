"""Invocation-local adapters for the existing core Bash tool (not a sandbox)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from openjiuwen.core.sys_operation import LocalWorkConfig, SysOperation, SysOperationCard
from openjiuwen.core.sys_operation.base import OperationMode
from openjiuwen.core.sys_operation.local.shell_operation import ShellOperation
from openjiuwen.core.sys_operation.shell_process_registry import (
    consume_shell_session_cancelled, kill_shell_processes_for_session,
    reset_shell_session_id, set_shell_session_id,
)
from openjiuwen.harness.schema.build_context import parent_sys_operation
from openjiuwen.harness.schema.deep_agent_spec import BuiltinToolSpec, register_tool_provider
from openjiuwen.harness.tools.shell.bash import BashTool

from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError
from jiuwenswarm.server.jiuwenmate.lark_cli_runtime import _kill_children, _stop_process

_KEY = "jiuwenmate.bash_runtime"
PROVIDER = "jiuwenmate.bash"
BASH_PROMPT = (
    "\n\n## 命令使用约定（非技术隔离）\n\n"
    "可以使用 bash 运行必要的脚本和已准备的 CLI，默认工作目录为当前同事的办公目录。"
    "只在当前办公目录内操作文件；不访问其他同事、个人版目录、宿主配置、数据库、群公共消息库，"
    "不用绝对路径、..、符号链接或脚本绕过原工具的权限拒绝和版本冲突。"
    "sessions/ 中只使用本次 Session 的草稿，不访问其他会话；普通文件操作优先使用文件工具。"
    "不读取、显示或复制 Secret、Token 和认证配置；不全局安装依赖，不登录或切换飞书身份。"
    "飞书业务命令显式使用 --as bot，不使用 --as user，不借用个人版用户认证。"
    "只能使用宿主为当前同事准备的飞书应用。命令超时或回执缺失表示结果未知，不自动重复创建文档。"
    "后台启动不表示完成；执行结束时后台命令会清理。文档创建成功与请求者获得访问权须分别核实。"
)


def with_bash_tool(fields: dict[str, Any]) -> dict[str, Any]:
    tools = list(fields.get("tools") or [])
    if any(item.get("type") in {PROVIDER, "core.bash"} or item.get("name") == "bash" for item in tools):
        raise PersistentAgentError("a plugin conflicts with the built-in bash tool", code="INVALID_CONFIG")
    # Build selected plugin tools together so their actual model-facing names
    # can be checked before core registers any of this Invocation's instances.
    declaration = {"type": PROVIDER}
    if tools:
        declaration["params"] = {"tools": tools}
    return {**fields, "tools": [declaration]}


def bind_bash_runtime(context, *, office: Path, cli) -> None:
    context.extras[_KEY] = (office, cli, context.execution, [])


def _process_key(execution) -> str:
    return f"jm:{execution.namespace_id}:{execution.agent_id}:{execution.execution_id}"


async def close_bash_runtime(context) -> None:
    if context is not None and _KEY in context.extras:
        execution = context.extras[_KEY][2]
        for tool in context.extras[_KEY][3]:
            await tool.aclose()
        key = _process_key(execution)
        kill_shell_processes_for_session(key)
        consume_shell_session_cancelled(key)


class _OwnedShell(ShellOperation):
    def __init__(self, config):
        super().__init__("shell", OperationMode.LOCAL, "JiuwenMate Invocation shell", config)
        self.processes = []

    async def _create_subprocess(self, command, cwd, env, *, shell_type="auto", background=False, stream=False):
        process = await super()._create_subprocess(command, cwd, env, shell_type=shell_type,
                                                   background=background, stream=stream)
        if os.name == "nt":
            kill = process.kill

            def kill_tree():
                _kill_children(process.pid)
                kill()

            # Affect only this newly created handle: core's timeout/cancel path
            # otherwise kills cmd.exe alone, leaving its CLI/script running.
            process.kill = kill_tree
        self.processes = [item for item in self.processes if item.returncode is None]
        self.processes.append(process)
        return process

    async def aclose(self):
        for process in self.processes:
            if process.returncode is None:
                await _stop_process(process)
        self.processes.clear()


class _Shell:
    def __init__(self, shell, office, cli, execution):
        self._shell, self._office, self._cli, self._execution = shell, office, cli, execution

    def _params(self, cwd, environment):
        env = dict(environment or {})
        child = self._cli.shell_environment(self._execution.namespace_id, self._execution.agent_id)
        managed = {"PATH", "LARKSUITE_CLI_CONFIG_DIR", "LARKSUITE_CLI_DATA_DIR"}
        env.update({key: value for key, value in child.items() if key in managed})
        env.update({key: "" for key in os.environ if key.startswith(("LARK_", "FEISHU_", "LARKSUITE_"))
                    and key not in managed})
        return {"cwd": str(self._office) if not cwd else cwd, "environment": env}

    async def execute_cmd(self, command, *, cwd=None, timeout=300, environment=None, options=None, shell_type="auto"):
        return await self._shell.execute_cmd(command, timeout=timeout, options=options, shell_type=shell_type,
                                              **self._params(cwd, environment))

    async def execute_cmd_stream(self, command, *, cwd=None, timeout=300, environment=None, options=None, shell_type="auto"):
        async for chunk in self._shell.execute_cmd_stream(command, timeout=timeout, options=options,
                shell_type=shell_type, **self._params(cwd, environment)):
            yield chunk

    async def execute_cmd_background(self, command, *, cwd=None, environment=None, grace=3.0, shell_type="auto"):
        return await self._shell.execute_cmd_background(command, grace=grace, shell_type=shell_type,
                                                         **self._params(cwd, environment))


class _Operation:
    def __init__(self, parent, shell):
        self._parent, self._shell = parent, shell

    def shell(self):
        return self._shell

    def fs(self):
        return self._parent.fs()


class _PersistentBash(BashTool):
    def __init__(self, operation, office, execution, language):
        super().__init__(operation, language=language)
        self._office, self._execution = office, execution

    def _inputs(self, inputs):
        # Core's large-output persistence uses a shared temporary directory.
        # Keep this adapter's output inline and bounded instead of sharing files.
        directory = inputs.get("workdir") or str(self._office)
        if not os.path.isabs(directory):
            directory = str(self._office / directory)
        return {**inputs, "workdir": directory, "max_output_chars": 0}

    def _build_history_path(self, session):
        return str(self._office / "sessions" / self._execution.session_id / ".bash-history.json")

    async def aclose(self):
        await self._operation.shell()._shell.aclose()

    @staticmethod
    def _bounded(output):
        for key in ("content", "text"):
            if isinstance(output.data, dict) and isinstance(output.data.get(key), str) and len(output.data[key]) > 20000:
                output.data[key] = output.data[key][:20000] + "\n[输出已截断]"
        if isinstance(output.error, str):
            output.error = output.error[:20000]
        return output

    async def invoke(self, inputs, **kwargs):
        token = set_shell_session_id(_process_key(self._execution))
        try:
            return self._bounded(await super().invoke(self._inputs(inputs), **kwargs))
        finally:
            reset_shell_session_id(token)

    async def stream(self, inputs, **kwargs):
        token = set_shell_session_id(_process_key(self._execution))
        try:
            async for output in super().stream(self._inputs(inputs), **kwargs):
                yield self._bounded(output)
        finally:
            reset_shell_session_id(token)


def build_bash_tool(params: dict[str, Any], context):
    if set(params) - {"tools"} or _KEY not in context.extras:
        raise ValueError("Bash requires a trusted JiuwenMate Invocation")
    office, cli, execution, tools = context.extras[_KEY]
    parent = parent_sys_operation(context)
    if parent is not None and parent.mode != OperationMode.LOCAL:
        raise ValueError("JiuwenMate Bash requires the local operation")
    # With enable_sys_operation alone, core creates its default after tool providers.
    # Use an Invocation-local default in that case; explicit parent policy is copied.
    config = LocalWorkConfig() if parent is None else parent._run_config.model_copy(deep=True)
    if not isinstance(config, LocalWorkConfig):
        raise ValueError("Unsupported Bash operation config")
    if config.shell_allowlist is not None:
        config.shell_allowlist = [*config.shell_allowlist, "lark-cli", "lark-cli.cmd"]
    operation = SysOperation(SysOperationCard(id=f"jm-bash:{execution.execution_id}", work_config=config))
    shell = _Shell(_OwnedShell(config), office, cli, execution)
    tool = _PersistentBash(_Operation(parent or operation, shell), office, execution, context.language)
    tools.append(tool)
    selected = []
    names = {"bash"}
    for declaration in params.get("tools", []):
        spec = BuiltinToolSpec.model_validate(declaration)
        if spec.type == PROVIDER:
            raise PersistentAgentError("recursive bash tool declaration", code="INVALID_CONFIG")
        built = spec.build(language=context.language, context=context)
        for child in (built if isinstance(built, list) else [built]):
            if child is None:
                continue
            card = getattr(child, "card", child)
            if card.name in names:
                raise PersistentAgentError(f"duplicate tool name: {card.name}", code="INVALID_CONFIG")
            names.add(card.name)
            selected.append(child)
    return [*selected, tool] if selected else tool


register_tool_provider(PROVIDER, build_bash_tool)
