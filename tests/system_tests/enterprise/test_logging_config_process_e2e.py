# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""logging_config 真实进程 E2E：Manager REST + WS → Gateway 热更新 / GDB 持久化 → AgentServer 冷启动读库。"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from e2e_helpers import (
    DEFAULT_JIUWENCLAW_ID,
    MOCK_LLM_SCRIPT,
    REPO_ROOT,
    agent_structured_logs_dir,
    build_gateway_env,
    build_manager_env,
    build_mock_llm_env,
    gateway_structured_logs_dir,
    init_gateway_home,
    init_server_home,
    init_ut_log,
    manager_logging_api_url,
    pick_free_port,
    read_gdb_logging_row,
    run_web_channel_user_request,
    shared_gateway_db_path,
    start_process,
    stop_gateway_gracefully,
    stop_process,
    ut_log,
    wait_for_http,
    wait_for_log,
    wait_for_manager_gateway_registered,
    web_channel_ws_url,
)

pytestmark = [pytest.mark.integration, pytest.mark.system]

# logging_config 热更新/删除标记写入 LOG_ROOT_PATH 结构化日志（非子进程 stdout 捕获文件）。
# 热更新会把 console_level 抬高，之后 jiuwenclaw.* 的 INFO 不再出现在 gateway_home/gateway.log。
# Gateway 冷加载在 app_gateway 中以 __main__ logger 输出，落在 stdout 捕获的 gateway.log（非 structured gateway.log）。
HOT_RELOAD_MARKER = "[ManagerWsClient] logging_config hot-reload"
GATEWAY_COLD_LOAD_MARKER = "[App] logging levels loaded from Gateway DB"
AGENT_COLD_LOAD_MARKER = "[AgentServer] logging levels loaded from Gateway DB"
DELETE_MARKER = "[ManagerWsClient] logging_config deleted, reverted to code defaults"


@dataclass
class LoggingConfigProcessStack:
    """Mock LLM + Claw Manager + Gateway 真实进程栈。"""

    run_home: Path
    gateway_home: Path
    server_home: Path
    jiuwenclaw_id: str = DEFAULT_JIUWENCLAW_ID
    mock_proc: object | None = field(default=None, repr=False)
    manager_proc: object | None = field(default=None, repr=False)
    gateway_proc: object | None = field(default=None, repr=False)
    mock_llm_port: int = 0
    manager_rest_port: int = 0
    manager_ws_port: int = 0
    web_port: int = 0
    gateway_port: int = 0

    @property
    def gateway_log(self) -> Path:
        return self.gateway_home / "gateway.log"

    @property
    def runtime_log(self) -> Path:
        return self.gateway_home / "runtime_sdk.log"

    @property
    def agentserver_log(self) -> Path:
        return self.server_home / "agentserver.log"

    @property
    def mock_log(self) -> Path:
        return self.run_home / "mock_llm.log"

    @property
    def manager_log(self) -> Path:
        return self.run_home / "manager.log"

    @property
    def structured_gateway_log(self) -> Path:
        return gateway_structured_logs_dir(self.gateway_home) / "gateway.log"

    @property
    def structured_channel_log(self) -> Path:
        return gateway_structured_logs_dir(self.gateway_home) / "channel.log"

    @property
    def structured_agent_process_gateway_log(self) -> Path:
        # app_agentserver 冷加载走 jiuwenclaw.utils logger，写入 Agent 进程的 gateway.log（非 agent_server.log）。
        return agent_structured_logs_dir() / "gateway.log"

    @property
    def gdb_path(self) -> Path:
        return shared_gateway_db_path(self.run_home)

    def logging_api(self) -> str:
        return manager_logging_api_url(self.manager_rest_port, self.jiuwenclaw_id)

    async def start(self) -> None:
        self.mock_llm_port = pick_free_port()
        self.manager_rest_port = pick_free_port()
        self.manager_ws_port = pick_free_port()
        self.web_port = pick_free_port()
        self.gateway_port = pick_free_port()

        init_gateway_home(self.gateway_home, self.mock_llm_port)
        init_server_home(self.server_home)

        mock_env = build_mock_llm_env()
        manager_env = build_manager_env(
            self.run_home,
            rest_port=self.manager_rest_port,
            ws_port=self.manager_ws_port,
        )

        self.mock_proc = start_process(
            [
                sys.executable,
                str(MOCK_LLM_SCRIPT),
                "--port",
                str(self.mock_llm_port),
                "--stream-token-count",
                "5",
                "--stream-token-interval",
                "0.05",
            ],
            env=mock_env,
            log_path=self.mock_log,
        )
        await wait_for_http(f"http://127.0.0.1:{self.mock_llm_port}/health", timeout=30)
        ut_log("mock_llm.ready", port=self.mock_llm_port, log_path=self.mock_log)

        self.manager_proc = start_process(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "jiuwenclaw_manager.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.manager_rest_port),
            ],
            env=manager_env,
            log_path=self.manager_log,
        )
        await wait_for_http(f"http://127.0.0.1:{self.manager_rest_port}/api/health", timeout=60)
        ut_log("manager.ready", rest_port=self.manager_rest_port, ws_port=self.manager_ws_port)

        await self.start_gateway()

    async def start_gateway(self, *, console_info_suppressed: bool = False) -> None:
        """启动 Gateway 并等待就绪。

        ``console_info_suppressed=True`` 时从 GDB 冷加载的 ``console_level`` 会抬高控制台阈值，
        INFO 级 ``WebChannel 已启动`` 只写入结构化 ``channel.log``，不再出现在 stdout ``gateway.log``。
        """
        gateway_env = build_gateway_env(
            self.run_home,
            self.gateway_home,
            self.server_home,
            self.mock_llm_port,
            self.web_port,
            self.gateway_port,
            jiuwenclaw_id=self.jiuwenclaw_id,
            manager_http_url=f"http://127.0.0.1:{self.manager_rest_port}",
            manager_config_public_host="127.0.0.1",
            manager_config_receiver_enabled=True,
        )
        self.gateway_proc = start_process(
            [sys.executable, "-m", "jiuwenswarm.gateway.app_gateway", "--port", str(self.web_port)],
            env=gateway_env,
            log_path=self.gateway_log,
            new_process_group=True,
        )
        ut_log(
            "gateway.started",
            web_port=self.web_port,
            web_channel_ws=web_channel_ws_url(self.web_port),
            log_path=self.gateway_log,
        )
        await wait_for_log(self.gateway_log, "using extension AgentServerClient", timeout=180)
        web_channel_log = (
            self.structured_channel_log if console_info_suppressed else self.gateway_log
        )
        await wait_for_log(web_channel_log, "WebChannel 已启动", timeout=180)
        await wait_for_manager_gateway_registered(
            self.manager_rest_port,
            self.jiuwenclaw_id,
            timeout=120,
        )
        ut_log("gateway.ready", gateway_log=self.gateway_log)

    async def stop(self) -> None:
        stop_gateway_gracefully(
            self.gateway_proc,
            gateway_log=self.gateway_log,
            runtime_log=self.runtime_log,
        )
        stop_process(self.manager_proc)
        stop_process(self.mock_proc)
        self.gateway_proc = None
        self.manager_proc = None
        self.mock_proc = None
        ut_log(
            "teardown.keep",
            run_home=self.run_home,
            gateway_home=self.gateway_home,
            server_home=self.server_home,
            gateway_log=self.gateway_log,
            agentserver_log=self.agentserver_log,
            manager_log=self.manager_log,
            mock_log=self.mock_log,
        )

    async def restart_gateway(self) -> None:
        stop_gateway_gracefully(
            self.gateway_proc,
            gateway_log=self.gateway_log,
            runtime_log=self.runtime_log,
        )
        self.gateway_proc = None
        await self.start_gateway(console_info_suppressed=True)


@pytest.mark.asyncio
@pytest.mark.skip(reason="skip ci")
async def test_logging_config_process_hot_reload_gdb_and_cold_start(
    enterprise_run_dirs: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
):
    """logging_config 真实进程 E2E：热更新、GDB 持久化、冷启动读库、删除恢复。

    进程栈：Mock LLM + Claw Manager + Gateway（Manager WS 已连接）+ Process deploy AgentServer。

    链路：
        Manager REST PUT /logging
            → Manager WS config.push
            → Gateway manager_config_receiver 热更新
            → Gateway DB（jiuwenswarm.db）持久化 logging_config

    验证阶段：
        ① 热更新：PUT 后 Gateway 结构化日志出现 HOT_RELOAD_MARKER
        ② GDB 持久化：共享 SQLite 中 logging_config 行字段正确
        ③ Agent 冷启动：chat.send 触发 Process deploy，Agent 从 GDB 加载日志级别
        ④ Gateway 冷启动：重启 Gateway 后从 GDB 加载日志级别
        ⑤ 删除恢复：DELETE /logging 清除 GDB 行，Gateway 恢复代码默认级别，GET 返回 404
    """
    run_home, gateway_home, server_home = enterprise_run_dirs
    init_ut_log(run_home)

    monkeypatch.setenv("HOME", str(gateway_home))
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    # 启动 Mock LLM → Claw Manager → Gateway（Gateway 注册到 Manager WS）
    stack = LoggingConfigProcessStack(
        run_home=run_home,
        gateway_home=gateway_home,
        server_home=server_home,
    )
    await stack.start()

    try:
        # ① 热更新：Manager REST PUT → WS config.push → Gateway 即时生效
        async with httpx.AsyncClient(timeout=60.0) as http:
            upsert_resp = await http.put(
                stack.logging_api(),
                json={
                    "level": "INFO",
                    "gateway": "DEBUG",
                    "console_level": "WARNING",
                    "agent_server": "DEBUG",
                    "full": "ERROR",
                },
            )
            assert upsert_resp.status_code == 200, upsert_resp.text
            upsert_body = upsert_resp.json()
            assert upsert_body["code"] == 200
            assert upsert_body["data"]["gateway"] == "DEBUG"
            assert upsert_body["data"]["agent_server"] == "DEBUG"

        await wait_for_log(stack.structured_gateway_log, HOT_RELOAD_MARKER, timeout=60)

        # ② GDB 持久化：Gateway 与 AgentServer 共用的 jiuwenswarm.db 写入 logging_config
        gdb_row = await read_gdb_logging_row(stack.gdb_path, stack.jiuwenclaw_id)
        assert gdb_row is not None, f"logging_config row missing in {stack.gdb_path}"
        assert gdb_row["jiuwenclaw_id"] == stack.jiuwenclaw_id
        assert gdb_row["level"] == "INFO"
        assert gdb_row["gateway"] == "DEBUG"
        assert gdb_row["agent_server"] == "DEBUG"
        assert gdb_row["full"] == "ERROR"
        ut_log(
            "assert.gdb.persisted",
            gdb_path=stack.gdb_path,
            level=gdb_row["level"],
            gateway=gdb_row["gateway"],
            agent_server=gdb_row["agent_server"],
        )

        # ③ Agent 冷启动：首次 chat.send 触发 Process deploy，子进程从 GDB 读日志级别
        chat_result = await run_web_channel_user_request(web_channel_ws_url(stack.web_port))
        assert chat_result["accepted"] is True
        assert chat_result["completed"] is True
        assert stack.agentserver_log.exists(), f"AgentServer log not found: {stack.agentserver_log}"
        await wait_for_log(stack.structured_agent_process_gateway_log, AGENT_COLD_LOAD_MARKER, timeout=120)
        gateway_text = stack.gateway_log.read_text(encoding="utf-8", errors="ignore")
        assert "Process deploy 成功" in gateway_text or "Process deploy" in gateway_text
        ut_log(
            "assert.agent.cold_start",
            agentserver_log=stack.agentserver_log,
            structured_agent_process_gateway_log=stack.structured_agent_process_gateway_log,
        )

        # ④ Gateway 冷启动：重启后从 GDB 加载，不依赖 Manager WS 推送
        await stack.restart_gateway()
        await wait_for_log(stack.gateway_log, GATEWAY_COLD_LOAD_MARKER, timeout=120)
        ut_log("assert.gateway.cold_start", gateway_log=stack.gateway_log)

        # ⑤ 删除恢复：DELETE 清除 GDB 行，Gateway 回退代码默认级别
        async with httpx.AsyncClient(timeout=60.0) as http:
            delete_resp = await http.delete(stack.logging_api())
            assert delete_resp.status_code == 200, delete_resp.text

        await wait_for_log(stack.structured_gateway_log, DELETE_MARKER, timeout=60)
        gdb_after_delete = await read_gdb_logging_row(stack.gdb_path, stack.jiuwenclaw_id)
        assert gdb_after_delete is None, "logging_config row should be removed from GDB"

        async with httpx.AsyncClient(timeout=60.0) as http:
            missing_resp = await http.get(stack.logging_api())
            assert missing_resp.status_code == 404

        ut_log(
            "test.pass",
            run_home=run_home,
            jiuwenclaw_id=stack.jiuwenclaw_id,
            gdb_path=stack.gdb_path,
        )
    finally:
        await stack.stop()
