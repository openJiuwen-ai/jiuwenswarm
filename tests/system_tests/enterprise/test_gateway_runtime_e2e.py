# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Enterprise Gateway + Runtime Management end-to-end system test."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from e2e_helpers import (
    MOCK_LLM_SCRIPT,
    REPO_ROOT,
    SERVICE_ID,
    SESSION_ID,
    build_gateway_env,
    build_mock_llm_env,
    init_gateway_home,
    init_server_home,
    init_ut_log,
    pick_free_port,
    run_web_channel_user_request,
    shared_gateway_db_path,
    start_process,
    stop_gateway_gracefully,
    stop_process,
    ut_log,
    wait_for_http,
    wait_for_log,
    web_channel_ws_url,
)

pytestmark = [pytest.mark.integration, pytest.mark.system]


@pytest.mark.asyncio
@pytest.mark.skip(reason="skip ci")
async def test_gateway_runtime_process_deploy_and_chat(
    enterprise_run_dirs: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
):
    run_home, gateway_home, server_home = enterprise_run_dirs
    init_ut_log(run_home)
    mock_llm_port = pick_free_port()
    web_port = pick_free_port()
    gateway_port = pick_free_port()
    ut_log(
        "setup.ports",
        run_home=run_home,
        gateway_home=gateway_home,
        server_home=server_home,
        mock_llm_port=mock_llm_port,
        web_port=web_port,
        gateway_port=gateway_port,
        service_id=SERVICE_ID,
        session_id=SESSION_ID,
    )

    monkeypatch.setenv("HOME", str(gateway_home))
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    init_gateway_home(gateway_home, mock_llm_port)
    init_server_home(server_home)

    from jiuwenswarm.common.utils import set_user_home

    set_user_home(gateway_home)

    gateway_env = build_gateway_env(
        run_home, gateway_home, server_home, mock_llm_port, web_port, gateway_port
    )
    mock_llm_env = build_mock_llm_env()

    mock_log = run_home / "mock_llm.log"
    gateway_log = gateway_home / "gateway.log"
    agentserver_log = server_home / "agentserver.log"

    mock_proc = start_process(
        [
            sys.executable,
            str(MOCK_LLM_SCRIPT),
            "--port",
            str(mock_llm_port),
            "--stream-token-count",
            "5",
            "--stream-token-interval",
            "0.05",
        ],
        env=mock_llm_env,
        log_path=mock_log,
    )
    gateway_proc = None
    try:
        await wait_for_http(f"http://127.0.0.1:{mock_llm_port}/health", timeout=30)
        ut_log("mock_llm.ready", port=mock_llm_port, log_path=mock_log)
        print(f"gateway_env: {gateway_env}")
        gateway_proc = start_process(
            [sys.executable, "-m", "jiuwenswarm.gateway.app_gateway", "--port", str(web_port)],
            env=gateway_env,
            log_path=gateway_log,
            new_process_group=True,
        )
        ut_log(
            "gateway.started",
            web_port=web_port,
            web_channel_ws=web_channel_ws_url(web_port),
            log_path=gateway_log,
        )

        await wait_for_log(gateway_log, "using extension AgentServerClient", timeout=180)
        await wait_for_log(gateway_log, "WebChannel 已启动", timeout=180)
        ut_log("gateway.ready", gateway_log=gateway_log)

        web_ws_url = web_channel_ws_url(web_port)
        chat_result = await run_web_channel_user_request(web_ws_url)

        gateway_text = gateway_log.read_text(encoding="utf-8", errors="ignore")
        mock_text = mock_log.read_text(encoding="utf-8", errors="ignore")
        process_deploy_ok = "Process deploy 成功" in gateway_text or "Process deploy" in gateway_text
        mock_llm_called = "POST /v1/chat/completions" in mock_text
        mock_llm_responded = (
            "mock token1" in mock_text
            or "Non-stream response content:" in mock_text
            or "Streamed token: mock token1" in mock_text
        )
        ut_log(
            "assert.summary",
            process_deploy_ok=process_deploy_ok,
            mock_llm_called=mock_llm_called,
            mock_llm_responded=mock_llm_responded,
            chat_result=chat_result,
            mock_log=mock_log,
            gateway_log=gateway_log,
        )
        assert process_deploy_ok
        assert mock_llm_called
        assert mock_llm_responded
        assert chat_result["accepted"] is True
        assert chat_result["completed"] is True
        assert agentserver_log.exists(), f"AgentServer log not found: {agentserver_log}"
        agent_text = agentserver_log.read_text(encoding="utf-8", errors="ignore")
        assert "Streamed token: mock token1" in mock_text, "Mock LLM should stream SSE tokens"
        assert "[LLM] <<< response: content_len=0" not in agent_text, (
            "Agent LLM response was empty; mock likely returned non-SSE JSON for a stream request"
        )
        shared_db = shared_gateway_db_path(run_home)
        assert shared_db.exists(), f"Shared gateway sqlite not created: {shared_db}"
        server_env_file = server_home / ".jiuwenswarm" / "config" / ".env"
        if server_env_file.exists():
            assert f"127.0.0.1:{mock_llm_port}" not in server_env_file.read_text(encoding="utf-8")
        ut_log(
            "test.pass",
            session_id=SESSION_ID,
            service_id=SERVICE_ID,
            run_home=run_home,
            gateway_home=gateway_home,
            server_home=server_home,
            agentserver_log=agentserver_log,
        )
    finally:
        runtime_log = gateway_home / "runtime_sdk.log"
        stop_gateway_gracefully(
            gateway_proc,
            gateway_log=gateway_log,
            runtime_log=runtime_log,
        )
        stop_process(mock_proc)
        ut_log(
            "teardown.keep",
            run_home=run_home,
            gateway_home=gateway_home,
            server_home=server_home,
            mock_log=mock_log,
            gateway_log=gateway_log,
            agentserver_log=agentserver_log,
        )
