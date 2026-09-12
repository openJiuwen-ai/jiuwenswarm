# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""面向 agent 的三方 Agent 管理工具封装。

无独立后端管理器：安装/启动/停止直接操作本地进程，
状态唯一收口于 ``~/.jiuwenswarm/third_agents.json``；
web 通道 ``third_agents.list`` 复用本模块的读取函数，保证逻辑唯一来源。

注册表 ``third_agents.yaml`` 仅为内置预设（安装时只需 name）；
注册表外的三方 Agent 同样允许安装，由调用方提供 start_cmd/port 等参数。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import signal
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml
from openjiuwen.core.foundation.tool import LocalFunction, Tool, ToolCard

from jiuwenswarm.server.runtime.user_registry.user_store import get_user

logger = logging.getLogger(__name__)

_INSTALL_TIMEOUT_SEC = 300
_START_READY_TIMEOUT_SEC = 60
_STOP_CMD_TIMEOUT_SEC = 30
_STOP_WAIT_SEC = 5
_POLL_INTERVAL_SEC = 2.0


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------

def _registry_path() -> Path:
    # 包内 jiuwenswarm/resources/third_agents.yaml
    return Path(__file__).resolve().parents[4] / "resources" / "third_agents.yaml"


def _load_registry() -> list[dict[str, Any]]:
    """加载可安装三方 Agent 注册表；文件缺失/损坏时返回空列表。"""
    try:
        data = yaml.safe_load(_registry_path().read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("[third_agent] registry unavailable: %s", exc)
        return []
    agents = data.get("agents") if isinstance(data, dict) else None
    if not isinstance(agents, list):
        return []
    return [a for a in agents if isinstance(a, dict) and a.get("name")]


def _find_registry_entry(name: str) -> dict[str, Any] | None:
    target = str(name or "").strip().lower()
    if not target:
        return None
    for entry in _load_registry():
        if str(entry.get("name", "")).strip().lower() == target:
            return entry
        if str(entry.get("display_name", "")).strip().lower() == target:
            return entry
    return None


# ---------------------------------------------------------------------------
# 用户凭据占位符（install_third_agent 指定 user 时替换 start_cmd 中的占位符）
# ---------------------------------------------------------------------------

# start_cmd 支持 {api_key} / {api_base} / {model} 占位符，
# 例如 "mycli --api-key {api_key} --base-url {api_base} --model {model} serve"。
_USER_CRED_PLACEHOLDER_FIELDS = ("api_key", "api_base", "model")


def _apply_user_credentials(
    start_cmd: str, user_record: dict[str, Any]
) -> tuple[str, list[str]]:
    """用用户凭据替换 start_cmd 中的占位符，返回 (替换后的命令, 命中的占位符列表)。"""
    replaced: list[str] = []
    for field in _USER_CRED_PLACEHOLDER_FIELDS:
        placeholder = "{" + field + "}"
        if placeholder in start_cmd:
            start_cmd = start_cmd.replace(placeholder, str(user_record.get(field) or ""))
            replaced.append(placeholder)
    return start_cmd, replaced


# ---------------------------------------------------------------------------
# 状态文件（唯一状态收口，原子写）
# ---------------------------------------------------------------------------

def _state_file() -> Path:
    return Path.home() / ".jiuwenswarm" / "third_agents.json"


def _read_state() -> list[dict[str, Any]]:
    """读取已安装状态；文件缺失/损坏时视为空列表。"""
    try:
        data = json.loads(_state_file().read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _write_state(items: list[dict[str, Any]]) -> None:
    path = _state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _upsert_state(entry: dict[str, Any]) -> None:
    items = [e for e in _read_state() if e.get("name") != entry.get("name")]
    items.append(entry)
    _write_state(items)


def _remove_state(name: str) -> None:
    _write_state([e for e in _read_state() if e.get("name") != name])


# ---------------------------------------------------------------------------
# 运行状态探测
# ---------------------------------------------------------------------------

def _pid_alive(pid: Any) -> bool | None:
    """探测 pid 是否存活；平台不支持或权限不足时返回 None（不确定）。"""
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return None
    if pid_int <= 0:
        return None
    try:
        os.kill(pid_int, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return None
    return True


def _port_open(port: Any) -> bool:
    try:
        port_int = int(port)
    except (TypeError, ValueError):
        return False
    try:
        with socket.create_connection(("127.0.0.1", port_int), timeout=1):
            return True
    except OSError:
        return False


def _detect_running(entry: dict[str, Any]) -> bool:
    """端口可连且 pid 未明确退出（pid 探测不确定时以端口为准）。"""
    if not _port_open(entry.get("port")):
        return False
    return _pid_alive(entry.get("pid")) is not False


def _entry_url(entry: dict[str, Any]) -> str:
    web_path = str(entry.get("web_path") or "/")
    if not web_path.startswith("/"):
        web_path = "/" + web_path
    return f"http://localhost:{entry.get('port')}{web_path}"


# ---------------------------------------------------------------------------
# 公共查询（web RPC third_agents.list 复用）
# ---------------------------------------------------------------------------

def list_installed_third_agents() -> list[dict[str, Any]]:
    """返回已安装三方 Agent 卡片数据，状态为实时探测结果（不回写文件）。"""
    items: list[dict[str, Any]] = []
    for entry in _read_state():
        items.append({
            "name": entry.get("name"),
            "display_name": entry.get("display_name") or entry.get("name"),
            "status": "running" if _detect_running(entry) else "stopped",
            "url": _entry_url(entry),
            "installed_at": entry.get("installed_at"),
        })
    return items


async def handle_third_agents_list(params: dict) -> dict:
    """web 通道 ``third_agents.list`` 的 handler（只读）。"""
    return {"agents": await asyncio.to_thread(list_installed_third_agents)}


# ---------------------------------------------------------------------------
# 进程操作
# ---------------------------------------------------------------------------

def _agent_log_path(name: str) -> Path:
    log_dir = Path.home() / ".jiuwenswarm" / "third-agent-logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f"{name}.log"


def _run_cmd(cmd: str, timeout: int) -> subprocess.CompletedProcess:
    # 命令以参数列表形式执行（shell=False），避免 shell 注入
    return subprocess.run(
        shlex.split(cmd), timeout=timeout,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )


def _start_process(reg_entry: dict[str, Any]) -> int | None:
    """后台启动 start_cmd，返回进程 pid；失败返回 None。"""
    name = str(reg_entry["name"])
    try:
        cmd = shlex.split(str(reg_entry["start_cmd"]))
        if not cmd:
            raise ValueError("empty start_cmd")
        # 子进程继承日志 fd 后即可关闭父进程句柄，with 保证异常路径也成对释放
        with open(_agent_log_path(name), "ab") as log_file:
            process = subprocess.Popen(
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,  # 独立进程组，便于整体停止且不受父进程退出影响
            )
    except Exception as exc:
        logger.warning("[third_agent] start %s failed: %s", name, exc)
        return None
    return process.pid


def _wait_port_ready(port: Any, timeout_sec: float) -> bool:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if _port_open(port):
            return True
        time.sleep(_POLL_INTERVAL_SEC)
    return False


def _stop_process(state_entry: dict[str, Any], reg_entry: dict[str, Any] | None) -> None:
    """停止进程：优先 stop_cmd（注册表或状态文件），兜底 kill 进程组（TERM → KILL）。"""
    stop_cmd = str(
        (reg_entry or {}).get("stop_cmd") or state_entry.get("stop_cmd") or ""
    ).strip()
    if stop_cmd:
        try:
            _run_cmd(stop_cmd, _STOP_CMD_TIMEOUT_SEC)
        except Exception as exc:
            logger.warning("[third_agent] stop_cmd failed: %s", exc)
        deadline = time.monotonic() + _STOP_WAIT_SEC
        while time.monotonic() < deadline:
            if _pid_alive(state_entry.get("pid")) is False:
                return
            if not _port_open(state_entry.get("port")):
                return
            time.sleep(0.5)

    pid = state_entry.get("pid")
    if _pid_alive(pid) is not True:
        return
    try:
        os.killpg(os.getpgid(int(pid)), signal.SIGTERM)
    except OSError:
        try:
            os.kill(int(pid), signal.SIGTERM)
        except OSError:
            return
    deadline = time.monotonic() + _STOP_WAIT_SEC
    while time.monotonic() < deadline:
        if _pid_alive(pid) is not True:
            return
        time.sleep(0.5)
    try:
        os.killpg(os.getpgid(int(pid)), signal.SIGKILL)
    except OSError:
        try:
            os.kill(int(pid), signal.SIGKILL)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 工具集
# ---------------------------------------------------------------------------

@dataclass
class ThirdAgentSpec:
    """install_third_agent 入参的具名封装（字段与工具 input_params 一一对应）。"""

    name: Any = ""
    display_name: Any = ""
    install_cmd: Any = ""
    start_cmd: Any = ""
    stop_cmd: Any = ""
    port: Any = 0
    web_path: Any = ""
    user: Any = ""


class ThirdAgentToolkit:
    """把三方 Agent 的安装/查询/卸载暴露成模型工具（本地进程形态）。"""

    # ----- install -----

    async def install_third_agent(self, name: str, **kwargs: Any) -> dict[str, Any]:
        spec = ThirdAgentSpec(name=name, **{
            k: v for k, v in kwargs.items() if k in ThirdAgentSpec.__dataclass_fields__
        })
        return await asyncio.to_thread(self._install, spec)

    @staticmethod
    def _entry_from_params(spec: ThirdAgentSpec) -> dict[str, Any] | None:
        """用调用方参数构造注册条目（注册表外的 Agent）；缺少关键信息时返回 None。"""
        reg_name = str(spec.name or "").strip().lower()
        start_cmd = str(spec.start_cmd or "").strip()
        try:
            port_int = int(spec.port)
        except (TypeError, ValueError):
            port_int = 0
        if not reg_name or not start_cmd or not 0 < port_int < 65536:
            return None
        return {
            "name": reg_name,
            "display_name": str(spec.display_name or "").strip() or reg_name,
            "install_cmd": str(spec.install_cmd or "").strip(),
            "start_cmd": start_cmd,
            "stop_cmd": str(spec.stop_cmd or "").strip(),
            "port": port_int,
            "web_path": str(spec.web_path or "").strip() or "/",
        }

    def _install(self, spec: ThirdAgentSpec) -> dict[str, Any]:
        reg_entry = _find_registry_entry(spec.name)
        state = _read_state()
        reg_name = str((reg_entry or {}).get("name") or str(spec.name or "").strip().lower())
        existing = next((e for e in state if e.get("name") == reg_name), None)
        if reg_entry is None:
            # 注册表外的 Agent 也允许安装：优先用调用方提供的参数，
            # 其次复用状态文件中已保存的启动信息（重启/幂等场景）。
            reg_entry = self._entry_from_params(spec)
            if reg_entry is None and existing is not None:
                reg_entry = self._entry_from_params(ThirdAgentSpec(
                    name=existing.get("name"),
                    display_name=existing.get("display_name"),
                    start_cmd=existing.get("start_cmd"),
                    stop_cmd=existing.get("stop_cmd"),
                    port=existing.get("port"),
                    web_path=existing.get("web_path"),
                ))
            if reg_entry is None:
                return {
                    "success": False,
                    "detail": (
                        f"{spec.name} 不在内置注册表中。安装注册表外的 Agent 需要同时提供 "
                        f"start_cmd（启动命令）与 port（web 服务端口）；"
                        f"可选提供 display_name / install_cmd / stop_cmd / web_path。"
                    ),
                }

        # 指定用户时，用该用户的模型凭据替换 start_cmd 中的
        # {api_key}/{api_base}/{model} 占位符。reg_entry 可能来自注册表
        # 缓存/状态文件，替换前先拷贝，避免污染原始条目。
        user_name = str(spec.user or "").strip()
        cred_note = ""
        if user_name:
            user_record = get_user(user_name)
            if user_record is None:
                return {
                    "success": False,
                    "detail": (
                        f"用户 {user_name} 不存在。请先在用户管理中创建该用户，"
                        f"或不指定 user 直接安装。"
                    ),
                }
            reg_entry = dict(reg_entry)
            start_cmd, replaced = _apply_user_credentials(
                str(reg_entry.get("start_cmd") or ""), user_record
            )
            reg_entry["start_cmd"] = start_cmd
            if not replaced:
                cred_note = (
                    f"注意：start_cmd 中未找到 {{api_key}}/{{api_base}}/{{model}} "
                    f"占位符，用户 {user_name} 的凭据未注入。"
                )

        reg_name = str(reg_entry["name"])
        display_name = str(reg_entry.get("display_name") or reg_name)
        if existing is not None:
            if _detect_running(existing):
                return {
                    "success": True,
                    "already_installed": True,
                    "detail": f"{display_name} 已安装且正在运行。",
                    "url": _entry_url(existing),
                }
            # 已安装但进程已停：直接重新拉起
            return self._start_and_record(
                reg_entry, display_name, restarted=True, user=user_name, cred_note=cred_note
            )

        # install_cmd 为空时跳过安装步骤（程序可能已存在于系统中），直接启动。
        install_cmd = str(reg_entry.get("install_cmd") or "").strip()
        if install_cmd:
            try:
                result = _run_cmd(install_cmd, _INSTALL_TIMEOUT_SEC)
            except subprocess.TimeoutExpired:
                return {"success": False, "detail": f"安装命令执行超时（{_INSTALL_TIMEOUT_SEC} 秒）"}
            except Exception as exc:  # noqa: BLE001
                return {"success": False, "detail": f"安装命令执行失败：{exc}"}
            if result.returncode != 0:
                output = (result.stdout or "").strip()[-500:]
                return {"success": False, "detail": f"安装命令失败（exit {result.returncode}）：{output}"}

        return self._start_and_record(
            reg_entry, display_name, restarted=False, user=user_name, cred_note=cred_note
        )

    @staticmethod
    def _start_and_record(
        reg_entry: dict[str, Any],
        display_name: str,
        *,
        restarted: bool,
        user: str = "",
        cred_note: str = "",
    ) -> dict[str, Any]:
        pid = _start_process(reg_entry)
        if pid is None:
            return {"success": False, "detail": f"{display_name} 启动失败，无法创建进程"}

        if not _wait_port_ready(reg_entry.get("port"), _START_READY_TIMEOUT_SEC):
            _stop_process({"pid": pid, "port": reg_entry.get("port")}, None)
            log_path = _agent_log_path(str(reg_entry["name"]))
            return {
                "success": False,
                "detail": f"{display_name} 启动后端口 {reg_entry.get('port')} 未就绪（超时 "
                          f"{int(_START_READY_TIMEOUT_SEC)} 秒），已停止进程。日志：{log_path}",
            }

        state_entry = {
            "name": str(reg_entry["name"]),
            "display_name": display_name,
            "port": reg_entry.get("port"),
            "web_path": reg_entry.get("web_path") or "/",
            "pid": pid,
            # 注册表外的 Agent 重启/卸载时无注册表可查，启动与停止信息需落入状态文件
            "start_cmd": str(reg_entry.get("start_cmd") or ""),
            "stop_cmd": str(reg_entry.get("stop_cmd") or ""),
            "status": "running",
            "installed_at": datetime.now(timezone.utc).isoformat(),
        }
        if user:
            # 记录安装时使用的用户，便于追溯凭据来源（凭据本身已随 start_cmd 落盘）
            state_entry["user"] = user
        _upsert_state(state_entry)
        action = "已重新启动" if restarted else "安装完成"
        detail = f"{display_name} {action}。"
        if user:
            detail += f"（使用用户 {user} 的模型凭据）"
        if cred_note:
            detail += cred_note
        return {
            "success": True,
            "already_installed": restarted,
            "detail": detail,
            "url": _entry_url(state_entry),
        }

    # ----- list -----

    async def list_third_agents(self) -> dict[str, Any]:
        items = await asyncio.to_thread(list_installed_third_agents)
        detail = "当前未安装任何三方 Agent。" if not items else ""
        return {"success": True, "items": items, "detail": detail}

    # ----- uninstall -----

    async def uninstall_third_agent(self, name: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._uninstall, name)

    @staticmethod
    def _uninstall(name: str) -> dict[str, Any]:
        reg_entry = _find_registry_entry(name)
        target = (reg_entry or {}).get("name") or str(name or "").strip().lower()
        state = _read_state()
        existing = next((e for e in state if e.get("name") == target), None)
        if existing is None:
            return {"success": False, "detail": f"未安装 {name}。"}
        _stop_process(existing, reg_entry)
        _remove_state(str(existing["name"]))
        display_name = existing.get("display_name") or existing.get("name")
        return {"success": True, "detail": f"{display_name} 已卸载（已停止并移除管理）。"}

    # ----- 注册 -----

    def get_tools(self) -> list[Tool]:
        """Return third-party-agent management tools for agent registration."""

        def make_tool(name: str, description: str, input_params: dict, func: Callable[..., Any]) -> Tool:
            # 统一用 LocalFunction 包装，保持与现有 toolkit 注册方式一致。
            card = ToolCard(
                id=name,
                name=name,
                description=description,
                input_params=input_params,
            )
            return LocalFunction(card=card, func=func)

        return [
            make_tool(
                name="install_third_agent",
                description=(
                    "Install and start a third-party agent application as a local "
                    "background process, and return its web access URL. Agents in the "
                    "built-in registry (e.g. openClaw) only need `name`. Any other "
                    "agent can be installed as well: provide its `start_cmd` and "
                    "`port` (optionally `display_name`, `install_cmd`, `stop_cmd`, "
                    "`web_path`). Installing an already-installed agent is idempotent "
                    "(restarts it if the process has exited). Optionally pass `user` "
                    "(a user created in the user management UI) to configure the "
                    "agent with that user's model credentials: the placeholders "
                    "{api_key}, {api_base} and {model} in `start_cmd` are replaced "
                    "with the user's credentials before the process starts."
                ),
                input_params={
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": (
                                "Agent name, e.g. 'openclaw'. Used as the unique key "
                                "(case-insensitive match against the registry and "
                                "installed state)."
                            ),
                        },
                        "user": {
                            "type": "string",
                            "description": (
                                "Optional. Username of a managed user (created in the "
                                "user management UI). When provided, the user's model "
                                "credentials replace the {api_key}, {api_base} and "
                                "{model} placeholders in `start_cmd`."
                            ),
                        },
                        "display_name": {
                            "type": "string",
                            "description": (
                                "Display name shown in the management UI. Defaults to "
                                "`name`. Only used for agents not in the registry."
                            ),
                        },
                        "install_cmd": {
                            "type": "string",
                            "description": (
                                "Shell command to install the agent, e.g. "
                                "'npm install -g openclaw'. Optional; skipped when "
                                "empty. Only used for agents not in the registry."
                            ),
                        },
                        "start_cmd": {
                            "type": "string",
                            "description": (
                                "Shell command to start the agent's web service, e.g. "
                                "'openclaw gateway'. Required for agents not in the "
                                "registry. May contain the placeholders {api_key}, "
                                "{api_base} and {model}, which are replaced with the "
                                "credentials of the user given in `user`."
                            ),
                        },
                        "stop_cmd": {
                            "type": "string",
                            "description": (
                                "Shell command to stop the agent, e.g. 'openclaw "
                                "gateway stop'. Optional; when empty the process "
                                "group is killed by pid."
                            ),
                        },
                        "port": {
                            "type": "integer",
                            "description": (
                                "Port of the agent's web service, used for readiness "
                                "probing and the access URL. Required for agents not "
                                "in the registry."
                            ),
                        },
                        "web_path": {
                            "type": "string",
                            "description": (
                                "Web access path, combined into "
                                "http://localhost:{port}{web_path}. Defaults to '/'."
                            ),
                        },
                    },
                    "required": ["name"],
                },
                func=self.install_third_agent,
            ),
            make_tool(
                name="list_third_agents",
                description=(
                    "List installed third-party agent applications with running "
                    "status and web access URLs."
                ),
                input_params={"type": "object", "properties": {}},
                func=self.list_third_agents,
            ),
            make_tool(
                name="uninstall_third_agent",
                description=(
                    "Stop an installed third-party agent application and remove it "
                    "from management. The program itself is NOT deleted from the system."
                ),
                input_params={
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Installed agent name, e.g. 'openclaw'.",
                        },
                    },
                    "required": ["name"],
                },
                func=self.uninstall_third_agent,
            ),
        ]


def get_third_agent_tools() -> list[Tool]:
    """构建三方 Agent 管理工具集（无状态，可直接注册到任意 adapter）。"""
    return ThirdAgentToolkit().get_tools()


__all__ = [
    "ThirdAgentToolkit",
    "get_third_agent_tools",
    "list_installed_third_agents",
    "handle_third_agents_list",
]
