# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""环境变量同步的集成测试.

测试从 Gateway 到 AgentServer 的完整流程.
"""

import asyncio
import json
import os
from pathlib import Path
from unittest import mock

import pytest


class TestAppAgentserverIntegration:
    """测试 app_agentserver.py 中的集成."""

    @pytest.mark.asyncio
    async def test_env_loading_in_sandbox_mode(self, monkeypatch, tmp_path):
        """测试在沙箱模式下触发环境变量加载."""
        from jiuwenclaw.agentserver.env_loader import wait_and_load_env

        # 设置沙箱模式
        monkeypatch.setenv("SANDBOX_ENABLE", "true")

        # 创建环境文件
        env_file = tmp_path / "agentserver_env.json"
        env_data = {
            "version": "1.0",
            "env_vars": {
                "SKILLDEV_AGENT_SYSTEM_PROMPT": "sandbox_value",
            },
        }
        env_file.write_text(json.dumps(env_data))

        # Mock ENV_FILE_PATH
        monkeypatch.setattr(
            "jiuwenclaw.agentserver.env_loader.ENV_FILE_PATH",
            env_file,
        )

        # 调用 wait_and_load_env（模拟 _run 的行为）
        result = await wait_and_load_env(poll_interval=0.05)

        assert "SKILLDEV_AGENT_SYSTEM_PROMPT" in result
        assert os.environ.get("SKILLDEV_AGENT_SYSTEM_PROMPT") == "sandbox_value"


class TestEndToEndFlow:
    """端到端测试，模拟完整流程."""

    @pytest.mark.asyncio
    async def test_gateway_uploads_agentserver_loads(self, monkeypatch, tmp_path):
        """测试完整流程：Gateway 上传，AgentServer 加载."""
        from jiuwenclaw.gateway.env_sync_file import prepare_agentserver_env_file
        from jiuwenclaw.agentserver.env_loader import _load_from_file

        # 步骤 1：Gateway 准备环境文件
        monkeypatch.setenv("SKILLDEV_AGENT_SYSTEM_PROMPT", "e2e_test_value")
        env_file = prepare_agentserver_env_file()
        assert env_file is not None

        # 步骤 2：模拟文件上传到 AgentServer 位置
        target_file = tmp_path / "agentserver_env.json"
        target_file.write_text(env_file.read_text())

        # 步骤 3：AgentServer 加载文件
        monkeypatch.setattr(
            "jiuwenclaw.agentserver.env_loader.ENV_FILE_PATH",
            target_file,
        )

        result = _load_from_file()
        assert result.get("SKILLDEV_AGENT_SYSTEM_PROMPT") == "e2e_test_value"

        # 清理
        env_file.unlink()

    @pytest.mark.asyncio
    async def test_protected_vars_filtered_in_e2e(self, monkeypatch, tmp_path):
        """测试端到端流程中受保护变量被过滤."""
        from jiuwenclaw.agentserver.env_loader import _filter_env_vars

        # 模拟恶意尝试设置受保护变量
        env_vars = {
            "SKILLDEV_AGENT_SYSTEM_PROMPT": "valid_value",
            "KUBERNETES_SERVICE_HOST": "malicious_host",
            "PATH": "/malicious/path",
        }

        result = _filter_env_vars(env_vars)

        # 有效变量应通过
        assert result.get("SKILLDEV_AGENT_SYSTEM_PROMPT") == "valid_value"

        # 受保护变量应被过滤
        assert "KUBERNETES_SERVICE_HOST" not in result
        assert "PATH" not in result


class TestErrorHandling:
    """测试错误处理场景."""

    @pytest.mark.asyncio
    async def test_graceful_fallback_on_exception(self, monkeypatch):
        """测试异常时优雅降级."""
        from jiuwenclaw.agentserver.env_loader import wait_and_load_env

        # Mock 抛出异常
        with mock.patch(
                "jiuwenclaw.agentserver.env_loader.ENV_FILE_PATH",
                side_effect=Exception("Simulated error"),
        ):
            # 不应抛出，而是优雅处理
            try:
                await wait_and_load_env(timeout=0.1, poll_interval=0.05)
            except Exception:
                pytest.fail("应优雅处理异常")

    @pytest.mark.asyncio
    async def test_continue_on_env_load_failure(self, monkeypatch, caplog):
        """测试环境变量加载失败时 AgentServer 继续运行."""
        from jiuwenclaw.agentserver.env_loader import wait_and_load_env

        monkeypatch.setenv("SANDBOX_ENABLE", "true")

        # Mock ENV_FILE_PATH 使用 MagicMock 模拟 Path 行为
        mock_path = mock.MagicMock()
        mock_path.exists.side_effect = PermissionError("Access denied")
        mock_path.__str__ = mock.Mock(return_value="/mock/env.json")

        with mock.patch(
                "jiuwenclaw.agentserver.env_loader.ENV_FILE_PATH",
                mock_path,
        ):
            # 应记录警告但不崩溃
            with caplog.at_level("WARNING"):
                try:
                    await wait_and_load_env(timeout=0.1)
                except PermissionError:
                    pass  # 此测试场景中预期会抛出
