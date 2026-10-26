# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared helpers for enterprise process-based system tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import websockets
from dotenv import load_dotenv

ENTERPRISE_DIR = Path(__file__).resolve().parent
REPO_ROOT = ENTERPRISE_DIR.parents[2]
ENV_FILE = ENTERPRISE_DIR / ".env"
ENV_EXAMPLE_FILE = ENTERPRISE_DIR / ".env.example"
MOCK_LLM_SCRIPT = ENTERPRISE_DIR / "mock_llm_server.py"
CLAW_MANAGER_SRC = REPO_ROOT / "packages" / "jiuwenclaw-ee" / "claw_manager" / "src"

CHAT_ID = "chat_test"
BOT_ID = "bot_test"
SERVICE_ID = hashlib.md5(f"{CHAT_ID}::{BOT_ID}".encode("utf-8")).hexdigest()
SESSION_ID = "enterprise_sess_001"
GATEWAY_DB_FILENAME = "jiuwenswarm.db"
E2E_UT_LOG_FILENAME = "e2e.log"
DEFAULT_JIUWENCLAW_ID = "enterprise_e2e_001"

_UT_LOG_PATH: Path | None = None


def init_ut_log(run_home: Path) -> Path:
    """Bind structured test diagnostics to ``<run_home>/e2e.log``."""
    global _UT_LOG_PATH
    log_path = run_home / E2E_UT_LOG_FILENAME
    log_path.write_text("", encoding="utf-8")
    _UT_LOG_PATH = log_path
    return log_path


def ut_log(stage: str, /, **fields: object) -> None:
    """Print structured test diagnostics (visible with pytest -s / log_cli)."""
    detail = " ".join(f"{key}={value!r}" for key, value in fields.items())
    line = f"[E2E][{stage}] {detail}"
    print(line, flush=True)
    if _UT_LOG_PATH is not None:
        with _UT_LOG_PATH.open("a", encoding="utf-8") as log_file:
            log_file.write(f"{line}\n")


def pick_free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def pythonpath(*parts: Path) -> str:
    return os.pathsep.join(str(part) for part in parts)


def start_process(
    cmd: list[str],
    *,
    env: dict[str, str],
    log_path: Path,
    new_process_group: bool = False,
) -> subprocess.Popen:
    popen_kwargs: dict[str, object] = {
        "cwd": str(REPO_ROOT),
        "env": env,
        "stderr": subprocess.STDOUT,
        "text": True,
    }
    if new_process_group:
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True

    log_file = log_path.open("w", encoding="utf-8")
    popen_kwargs["stdout"] = log_file
    proc = subprocess.Popen(cmd, **popen_kwargs)  # type: ignore[arg-type]
    log_file.close()
    ut_log("process.start", pid=proc.pid, cmd=" ".join(cmd), log_path=log_path)
    return proc


def stop_process(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    exit_code = proc.poll()
    if exit_code is not None:
        ut_log("process.already_exited", pid=proc.pid, exit_code=exit_code)
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        return

    ut_log("process.stop", pid=proc.pid)
    proc.terminate()
    try:
        proc.wait(timeout=15)
        return
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


def stop_gateway_gracefully(
    proc: subprocess.Popen | None,
    *,
    gateway_log: Path,
    runtime_log: Path,
    timeout: float = 45.0,
) -> None:
    """触发 Gateway 优雅退出，使其 ``finally`` 执行 ``client.disconnect()`` 清理 AgentServer。"""
    if proc is None:
        return
    if proc.poll() is not None:
        ut_log("process.already_exited", pid=proc.pid, exit_code=proc.returncode)
        return

    ut_log("gateway.graceful_stop.begin", pid=proc.pid)
    try:
        if sys.platform == "win32":
            ut_log("gateway.graceful_stop.os.kill", pid=proc.pid)
            os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
        else:
            ut_log("gateway.graceful_stop.proc.send_signal", pid=proc.pid)
            proc.send_signal(signal.SIGINT)
    except OSError as exc:
        ut_log("gateway.graceful_stop.signal_failed", pid=proc.pid, error=str(exc))
        stop_process(proc)
        return

    shutdown_markers = (
        "[App] Gateway stopped",
        "Access 已 shutdown",
        "Process delete 完成",
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            ut_log("gateway.graceful_stop.exited", pid=proc.pid, exit_code=proc.returncode)
            return
        for log_path in (gateway_log, runtime_log):
            if not log_path.exists():
                continue
            text = log_path.read_text(encoding="utf-8", errors="ignore")
            if any(marker in text for marker in shutdown_markers):
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
                if proc.poll() is not None:
                    ut_log("gateway.graceful_stop.ok", pid=proc.pid)
                    return
                break
        time.sleep(0.3)

    ut_log("gateway.graceful_stop.timeout", pid=proc.pid)
    stop_process(proc)


async def wait_for_log(log_path: Path, needle: str, timeout: float = 120.0) -> None:
    ut_log("wait.log.begin", needle=needle, log_path=log_path, timeout=timeout)
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if log_path.exists():
            text = log_path.read_text(encoding="utf-8", errors="ignore")
            if needle in text:
                ut_log("wait.log.ok", needle=needle, log_path=log_path)
                return
        await asyncio.sleep(0.3)
    log_text = log_path.read_text(encoding="utf-8", errors="ignore") if log_path.exists() else ""
    raise AssertionError(f"Timed out waiting for log line: {needle}\nlog={log_text[-4000:]}")


async def wait_for_http(url: str, timeout: float = 30.0) -> None:
    ut_log("wait.http.begin", url=url, timeout=timeout)
    deadline = asyncio.get_running_loop().time() + timeout
    last_error: Exception | None = None
    while asyncio.get_running_loop().time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:  # noqa: S310
                if resp.status == 200:
                    ut_log("wait.http.ok", url=url, status=resp.status)
                    return
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            await asyncio.sleep(0.2)
    raise AssertionError(f"Timed out waiting for HTTP {url} last_error={last_error}")


async def wait_for_manager_gateway_registered(
    manager_rest_port: int,
    jiuwenclaw_id: str,
    *,
    timeout: float = 120.0,
) -> None:
    url = f"http://127.0.0.1:{manager_rest_port}/api/manager-ws/status"
    ut_log("wait.manager_ws.begin", url=url, jiuwenclaw_id=jiuwenclaw_id, timeout=timeout)
    deadline = asyncio.get_running_loop().time() + timeout
    last_body: dict | None = None
    while asyncio.get_running_loop().time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:  # noqa: S310
                body = json.loads(resp.read().decode("utf-8"))
                last_body = body
                registered = body.get("registered_jiuwenclaw_ids") or []
                if jiuwenclaw_id in registered:
                    ut_log("wait.manager_ws.ok", jiuwenclaw_id=jiuwenclaw_id, registered=registered)
                    return
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
            pass
        await asyncio.sleep(0.5)
    raise AssertionError(
        f"Timed out waiting for gateway registration on Manager WS "
        f"jiuwenclaw_id={jiuwenclaw_id!r} last_body={last_body}"
    )


def subprocess_bootstrap_env() -> dict[str, str]:
    keys = (
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "PYTHONHOME",
        "VIRTUAL_ENV",
        "PATHEXT",
        "PROCESSOR_ARCHITECTURE",
    )
    return {key: value for key, value in os.environ.items() if key in keys and value}


def load_gateway_dotenv() -> None:
    """Load enterprise/.env (or .env.example) into os.environ for Gateway subprocess."""
    dotenv_path = ENV_FILE if ENV_FILE.exists() else ENV_EXAMPLE_FILE
    # override=True: enterprise E2E sqlite 配置优先于仓库根 .env 中的 MySQL 默认值
    load_dotenv(dotenv_path=dotenv_path, override=True)


def shared_gateway_db_path(run_home: Path) -> Path:
    """Gateway 与 AgentServer 共用的 SQLite 文件（位于本次 run 时间戳目录下）。"""
    return (run_home / GATEWAY_DB_FILENAME).resolve()


def gateway_structured_logs_dir(gateway_home: Path) -> Path:
    return gateway_home / ".jiuwenswarm" / "agent" / ".logs"


def agent_structured_logs_dir() -> Path:
    """Process-deploy AgentServer ``LOG_ROOT_PATH`` (see runtime_management_client).

    On Windows this resolves to ``C:/home/app/.logs``; structured files include ``gateway.log``.
    """
    return Path("/home/app/.logs")


def build_manager_env(
    run_home: Path,
    *,
    rest_port: int,
    ws_port: int,
) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": pythonpath(REPO_ROOT, CLAW_MANAGER_SRC),
            "MANAGER_REST_HOST": "127.0.0.1",
            "MANAGER_REST_PORT": str(rest_port),
            "MANAGER_WS_HOST": "127.0.0.1",
            "MANAGER_WS_PORT": str(ws_port),
            "MANAGER_WS_ENABLED": "true",
            "MANAGER_DB_TYPE": "sqlite",
            "MANAGER_SQLITE_PATH": str(run_home / "manager.db"),
        }
    )
    ut_log(
        "env.manager.ready",
        rest_port=rest_port,
        ws_port=ws_port,
        manager_db=env["MANAGER_SQLITE_PATH"],
    )
    return env


def build_gateway_env(
    run_home: Path,
    gateway_home: Path,
    server_home: Path,
    mock_llm_port: int,
    web_port: int,
    gateway_port: int,
    *,
    jiuwenclaw_id: str = DEFAULT_JIUWENCLAW_ID,
    manager_http_url: str | None = None,
    manager_config_public_host: str | None = None,
    manager_config_receiver_enabled: bool = False,
) -> dict[str, str]:
    load_gateway_dotenv()
    env = os.environ.copy()
    logs_dir = gateway_structured_logs_dir(gateway_home)
    enabled = manager_config_receiver_enabled
    env.update(
        {
            "HOME": str(gateway_home),
            "AGENT_SERVER_HOME": str(server_home),
            "API_BASE": f"http://127.0.0.1:{mock_llm_port}/v1",
            "WEB_PORT": str(web_port),
            "GATEWAY_PORT": str(gateway_port),
            "GATEWAY_SQLITE_PATH": str(shared_gateway_db_path(run_home)),
            "EXTENSION_DIRS": str(
                REPO_ROOT / "packages" / "jiuwenclaw-ee" / "gateway" / "extensions"
            ),
            "PYTHONPATH": str(REPO_ROOT),
            "AGENT_SERVER_LAUNCHER_SCRIPT": str(ENTERPRISE_DIR / "agentserver_launcher.py"),
            "AGENT_SERVER_LOG_FILE": str(server_home / "agentserver.log"),
            "OPENJIUWEN_RUNTIME_LOG_FILE": str(gateway_home / "runtime_sdk.log"),
            "JIUWENCLAW_ID": jiuwenclaw_id,
            "LOG_ROOT_PATH": str(logs_dir),
            "GATEWAY_CONFIG_RECEIVER_ENABLED": "true" if enabled else "false",
        }
    )
    if manager_http_url is not None:
        env["GATEWAY_MANAGER_HTTP_URL"] = manager_http_url
    if manager_config_public_host is not None:
        env["GATEWAY_CONFIG_PUBLIC_HOST"] = manager_config_public_host
    ut_log(
        "env.gateway.ready",
        gateway_home=gateway_home,
        server_home=server_home,
        web_port=web_port,
        gateway_port=gateway_port,
        jiuwenclaw_id=jiuwenclaw_id,
        manager_config_receiver_enabled=env["GATEWAY_CONFIG_RECEIVER_ENABLED"],
        manager_http_url=env.get("GATEWAY_MANAGER_HTTP_URL"),
        manager_config_public_host=env.get("GATEWAY_CONFIG_PUBLIC_HOST"),
        gateway_sqlite_path=env["GATEWAY_SQLITE_PATH"],
        log_root_path=env["LOG_ROOT_PATH"],
    )
    return env


def build_mock_llm_env() -> dict[str, str]:
    """Mock LLM 不加载 enterprise/.env，仅保留系统 PATH 等。"""
    return subprocess_bootstrap_env()


def workspace_config_dir(home: Path) -> Path:
    """Agent/Gateway 工作区配置目录：``<HOME>/.jiuwenswarm/config``。"""
    return home / ".jiuwenswarm" / "config"


def write_workspace_env(home: Path, mock_llm_port: int) -> None:
    config_dir = workspace_config_dir(home)
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / ".env").write_text(
        "\n".join(
            [
                f"API_BASE=http://127.0.0.1:{mock_llm_port}/v1",
                "API_KEY=mock-key",
                "MODEL_PROVIDER=OpenAI",
                "MODEL_NAME=mock-model",
                "LLM_SSL_VERIFY=false",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def init_gateway_home(home: Path, mock_llm_port: int) -> None:
    """初始化 Gateway HOME，并写入 workspace config/.env（供 Gateway 读取）。"""
    from jiuwenswarm.common.utils import prepare_workspace, set_user_home

    set_user_home(home)
    prepare_workspace(overwrite=False)
    write_workspace_env(home, mock_llm_port)
    ut_log(
        "setup.gateway_home",
        home=home,
        workspace=home / ".jiuwenswarm",
        config_env=workspace_config_dir(home) / ".env",
    )


def init_server_home(home: Path) -> None:
    """初始化 AgentServer HOME 目录结构；LLM 配置由 Runtime _agent_env_vars 注入。"""
    from jiuwenswarm.common.utils import prepare_workspace, set_user_home

    set_user_home(home)
    prepare_workspace(overwrite=False)
    ut_log("setup.server_home", home=home, workspace=home / ".jiuwenswarm")


def web_channel_ws_url(web_port: int, web_path: str = "/ws") -> str:
    return f"ws://127.0.0.1:{web_port}{web_path}"


def manager_logging_api_url(manager_rest_port: int, jiuwenclaw_id: str) -> str:
    return f"http://127.0.0.1:{manager_rest_port}/api/v1/instances/{jiuwenclaw_id}/logging"


async def read_gdb_logging_row(db_path: Path, jiuwenclaw_id: str):
    import aiosqlite

    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM logging_config WHERE jiuwenclaw_id = ?",
            (jiuwenclaw_id,),
        ) as cursor:
            row = await cursor.fetchone()
    if row is None:
        return None
    return dict(row)


async def recv_json(ws, timeout: float = 180.0) -> dict:
    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
    return json.loads(raw)


async def run_web_channel_user_request(ws_url: str) -> dict:
    """WebSocket client: connect to Gateway WebChannel, send one user request, wait for response."""
    frames: list[dict] = []
    req_id = "req-enterprise-e2e"
    request = {
        "type": "req",
        "id": req_id,
        "method": "chat.send",
        "params": {
            "session_id": SESSION_ID,
            "content": "say hello",
            "mode": "agent.fast",
            "service_id": SERVICE_ID,
        },
    }

    ut_log(
        "web_channel.begin",
        ws_url=ws_url,
        req_id=req_id,
        session_id=SESSION_ID,
        service_id=SERVICE_ID,
    )

    async with websockets.connect(ws_url, max_size=10_000_000) as ws:
        ack = await recv_json(ws, timeout=30)
        if ack.get("event") != "connection.ack":
            raise AssertionError(f"Expected connection.ack, got: {ack}")
        ut_log("web_channel.connected", event=ack.get("event"))

        await ws.send(json.dumps(request, ensure_ascii=False))
        ut_log("web_channel.sent", req_id=req_id, params=request["params"])

        accepted = False
        completed = False
        response_events: list[str] = []
        deadline = asyncio.get_running_loop().time() + 120.0

        while asyncio.get_running_loop().time() < deadline:
            remaining = max(5.0, deadline - asyncio.get_running_loop().time())
            msg = await recv_json(ws, timeout=remaining)
            ut_log("web.recv_msg", msg=msg)
            frames.append(msg)

            if msg.get("type") == "res" and msg.get("id") == req_id:
                assert msg.get("ok") is True, f"chat.send rejected: {msg}"
                accepted = True
                ut_log("web_channel.accepted", req_id=req_id, payload=msg.get("payload"))
                continue

            if msg.get("type") != "event":
                continue

            event_name = str(msg.get("event") or "")
            payload = msg.get("payload") or {}
            if event_name:
                response_events.append(event_name)

            if event_name == "chat.error":
                raise AssertionError(f"chat.error: {payload}")
            if event_name == "chat.final":
                completed = True
                break
            if event_name == "chat.processing_status" and payload.get("is_complete"):
                completed = True
                break

        assert accepted, "chat.send was not accepted by Gateway WebChannel"
        assert completed, f"no terminal chat response, last_frames={frames[-5:]}"

    result = {
        "accepted": accepted,
        "completed": completed,
        "response_events": response_events,
        "frame_count": len(frames),
    }
    ut_log("web_channel.done", **result)
    return result
