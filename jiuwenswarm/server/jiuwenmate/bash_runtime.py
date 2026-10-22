"""Invocation-local adapters for the existing core Bash tool (not a sandbox)."""

from __future__ import annotations

import os
import re
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
WEB_FETCH_PROVIDER = "core.web_fetch"
BASH_PROMPT = (
    "\n\n## 命令使用约定（非技术隔离）\n\n"
    "可以使用 bash 运行必要的脚本和已准备的 CLI，默认工作目录为本次 Session 的私有草稿目录。"
    "普通中间文件直接放在本次 Session 目录；只有 Lark Slides 专用计划、截图等放在其中的 `.lark-slides/`。"
    "最终交付文件放到 Agent 工作空间共享的 `output/`，并用 `share_file` 登记 `output/<文件名>`。"
    "按本次执行注入的目录说明操作文件；不访问其他同事、个人版目录、宿主配置、数据库、群公共消息库，"
    "文件工具使用工作空间相对路径；Bash 只使用本次执行指定的 Session 目录与 `JIUWENMATE_OUTPUT_DIR`。"
    "不要使用其他绝对路径、..、符号链接或脚本绕过原工具的权限拒绝和版本冲突。"
    "sessions/ 中只使用本次 Session 的草稿，不访问其他会话；普通文件操作优先使用文件工具。"
    "不读取、显示或复制 Secret、Token 和认证配置；不全局安装依赖。飞书用户授权只按 Skill 为当前 Agent 的独立 CLI profile 完成，"
    "不借用其他 Agent 或个人版登录态，也不切换到其他飞书用户。"
    "飞书应用级操作默认使用 bot；读取个人日历等用户资源时，按 lark-shared 与 lark-calendar Skill 的说明，"
    "在当前同事独立的 Lark CLI 配置中完成一次用户登录，并显式使用 --as user。"
    "Agent 工具无法在同一轮等待用户授权：按 Skill 指引用非阻塞登录获取验证链接，只把链接交给用户；"
    "用户完成授权并再次发消息后，再核验登录状态并继续，不要启动会被中断的长轮询。"
    "同一同事的用户登录态在其私聊和群聊间共享；群内查询直接在当前群回复，群成员均可见，不另发确认卡片。"
    "不要读取、显示或复制 Token、用户认证文件或 Secret。"
    "只能使用宿主为当前同事准备的飞书应用。命令超时或回执缺失表示结果未知，不自动重复创建文档。"
    "后台启动不表示完成；执行结束时后台命令会清理。文档创建成功与请求者获得访问权须分别核实。"
)
WEB_FETCH_PROMPT = (
    "\n\n## 网页读取\n\n"
    "消息中的 http 或 https 链接，先用 fetch_webpage 读取正文，再据此回答。"
    "读取失败时说明工具返回的原因，不要声称没有联网能力。不要用 bash 去访问网页。"
)


def _without_web_fetch(tools: list[Any]) -> list[Any]:
    return [item for item in tools if not (isinstance(item, dict) and item.get("type") == WEB_FETCH_PROVIDER)]


def with_bash_tool(fields: dict[str, Any]) -> dict[str, Any]:
    tools = list(fields.get("tools") or [])
    if any(item.get("type") in {PROVIDER, "core.bash"} or item.get("name") == "bash" for item in tools):
        raise PersistentAgentError("a plugin conflicts with the built-in bash tool", code="INVALID_CONFIG")
    # Build selected plugin tools together so their actual model-facing names
    # can be checked before core registers any of this Invocation's instances.
    # fetch_webpage is built in the same pass; its model-facing name must not collide.
    nested = [*_without_web_fetch(tools), {"type": WEB_FETCH_PROVIDER}]
    return {**fields, "tools": [{"type": PROVIDER, "params": {"tools": nested}}]}


def refresh_builtin_tools(fields: dict[str, Any]) -> dict[str, Any]:
    """Refresh host tools and command guidance without republishing user drafts."""
    tools = fields.get("tools")
    updated: list[Any] | Any = tools
    if isinstance(tools, list):
        updated = []
        for item in tools:
            if isinstance(item, dict) and item.get("type") == "jiuwenmate.personal_calendar":
                continue
            if not isinstance(item, dict) or item.get("type") != PROVIDER:
                updated.append(item)
                continue
            params = dict(item.get("params") or {})
            nested = [child for child in (params.get("tools") or [])
                      if not (isinstance(child, dict) and child.get("type") == "jiuwenmate.personal_calendar")]
            if not any(isinstance(child, dict) and child.get("type") == WEB_FETCH_PROVIDER for child in nested):
                nested.append({"type": WEB_FETCH_PROVIDER})
            params["tools"] = nested
            updated.append({**item, "params": params})
    prompt = fields.get("system_prompt") or ""
    if isinstance(updated, list) and any(isinstance(item, dict) and item.get("type") == PROVIDER for item in updated):
        # Replace only the host-generated section; keep the persona and other sections.
        prompt, replaced = re.subn(
            r"(?ms)^## 命令使用约定（非技术隔离）\n.*?(?=^## |^<!-- jm-feishu-cli -->|\Z)",
            lambda _: BASH_PROMPT.lstrip("\n") + "\n\n",
            prompt,
            count=1,
        )
        if not replaced:
            prompt += BASH_PROMPT
    if WEB_FETCH_PROMPT not in prompt:
        prompt += WEB_FETCH_PROMPT
    return {**fields, "tools": updated, "system_prompt": prompt}


def bind_bash_runtime(context, *, office: Path, cli) -> None:
    session_workspace = office / "sessions" / context.execution.session_id
    output_dir = office / "output"
    context.extras[_KEY] = (session_workspace, output_dir, cli, context.execution, [])


def _process_key(execution) -> str:
    return f"jm:{execution.namespace_id}:{execution.agent_id}:{execution.execution_id}"


async def close_bash_runtime(context) -> None:
    if context is not None and _KEY in context.extras:
        execution = context.extras[_KEY][3]
        for tool in context.extras[_KEY][4]:
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
    def __init__(self, shell, session_workspace, output_dir, cli, execution):
        self._shell = shell
        self._session_workspace = session_workspace
        self._output_dir = output_dir
        self._cli = cli
        self._execution = execution

    def _params(self, cwd, environment):
        env = dict(environment or {})
        child = self._cli.shell_environment(self._execution.namespace_id, self._execution.agent_id)
        managed = {"PATH", "LARKSUITE_CLI_CONFIG_DIR", "LARKSUITE_CLI_DATA_DIR"}
        env.update({key: value for key, value in child.items() if key in managed})
        env["JIUWENMATE_OUTPUT_DIR"] = str(self._output_dir)
        env.update({key: "" for key in os.environ if key.startswith(("LARK_", "FEISHU_", "LARKSUITE_"))
                    and key not in managed})
        directory = str(self._session_workspace) if not cwd else os.fspath(cwd)
        if not os.path.isabs(directory):
            directory = str(self._session_workspace / directory)
        return {"cwd": directory, "environment": env}

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
    def __init__(self, operation, session_workspace, execution, language):
        super().__init__(operation, language=language)
        self._session_workspace, self._execution = session_workspace, execution

    def _inputs(self, inputs):
        # Core's large-output persistence uses a shared temporary directory.
        # Keep this adapter's output inline and bounded instead of sharing files.
        directory = inputs.get("workdir") or str(self._session_workspace)
        if not os.path.isabs(directory):
            directory = str(self._session_workspace / directory)
        return {**inputs, "workdir": directory, "max_output_chars": 0}

    def _build_history_path(self, session):
        return str(self._session_workspace / ".bash-history.json")

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
    session_workspace, output_dir, cli, execution, tools = context.extras[_KEY]
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
    shell = _Shell(_OwnedShell(config), session_workspace, output_dir, cli, execution)
    tool = _PersistentBash(_Operation(parent or operation, shell), session_workspace, execution, context.language)
    tools.append(tool)
    from openjiuwen.harness.manifest import ensure_builtin_elements_registered

    ensure_builtin_elements_registered()
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
