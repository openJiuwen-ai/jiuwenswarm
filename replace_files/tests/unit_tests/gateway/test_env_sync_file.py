# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""env_sync_file 模块的单元测试."""

import json
import os
from pathlib import Path
from unittest import mock

import pytest

from jiuwenclaw.common.env_schema import ALLOWED_ENV_KEYS
from jiuwenclaw.gateway.env_sync_file import (
    collect_syncable_env,
    prepare_agentserver_env_file,
)


class TestPrepareAgentserverEnvFile:
    """测试 prepare_agentserver_env_file 函数."""

    def test_create_valid_env_file(self, monkeypatch):
        """测试创建有效的环境文件."""
        monkeypatch.setenv("SKILLDEV_AGENT_SYSTEM_PROMPT", "file_value")

        env_file = prepare_agentserver_env_file()
        assert env_file is not None
        assert env_file.exists()

        # 验证内容
        data = json.loads(env_file.read_text())
        assert data["version"] == "1.0"
        assert data["source"] == "gateway"
        assert "generated_at" in data
        assert data["env_vars"]["SKILLDEV_AGENT_SYSTEM_PROMPT"] == "file_value"

        # 清理
        env_file.unlink()


class TestUploadEnvToAgentserver:
    """测试 upload_env_to_agentserver 函数."""

    @pytest.mark.asyncio
    async def test_upload_success(self, monkeypatch):
        """测试成功上传."""
        from jiuwenclaw.gateway.env_sync_file import upload_env_to_agentserver

        monkeypatch.setenv("SKILLDEV_AGENT_SYSTEM_PROMPT", "upload_value")

        # Mock sandbox_client
        mock_client = mock.AsyncMock()
        mock_client.upload_file = mock.AsyncMock(return_value=None)

        result = await upload_env_to_agentserver(
            mock_client,
            sandbox_id="test_sandbox_123",
        )

        assert result is True
        mock_client.upload_file.assert_called_once()
        call_args = mock_client.upload_file.call_args
        assert call_args.kwargs["sandbox_id"] == "test_sandbox_123"
        assert call_args.kwargs["remote_path"] == "agentserver_env.json"

    @pytest.mark.asyncio
    async def test_upload_failure(self, monkeypatch):
        """测试上传失败处理."""
        from jiuwenclaw.gateway.env_sync_file import upload_env_to_agentserver

        monkeypatch.setenv("SKILLDEV_AGENT_SYSTEM_PROMPT", "value")

        # Mock 抛出异常的 sandbox_client
        mock_client = mock.AsyncMock()
        mock_client.upload_file = mock.AsyncMock(side_effect=Exception("Upload failed"))

        result = await upload_env_to_agentserver(mock_client, sandbox_id="test")

        assert result is False

    @pytest.mark.asyncio
    async def test_skip_upload_when_no_vars(self, monkeypatch):
        """测试没有变量时跳过上传."""
        from jiuwenclaw.gateway.env_sync_file import upload_env_to_agentserver

        # 清除白名单中所有变量
        for key in list(ALLOWED_ENV_KEYS):
            monkeypatch.delenv(key, raising=False)

        mock_client = mock.AsyncMock()

        result = await upload_env_to_agentserver(mock_client, sandbox_id="test")

        assert result is False
        mock_client.upload_file.assert_not_called()


class TestIntegration:
    """完整流程的集成测试."""

    def test_end_to_end_env_collection_and_file_creation(self, monkeypatch):
        """测试从环境变量收集到文件创建的完整流程."""
        # 设置环境
        monkeypatch.setenv("SKILLDEV_AGENT_SYSTEM_PROMPT", "integration_test")

        # 收集
        collected = collect_syncable_env()
        assert "SKILLDEV_AGENT_SYSTEM_PROMPT" in collected

        # 准备文件
        env_file = prepare_agentserver_env_file()
        assert env_file is not None

        # 验证结构
        data = json.loads(env_file.read_text())
        assert data["env_vars"]["SKILLDEV_AGENT_SYSTEM_PROMPT"] == "integration_test"

        # 清理
        env_file.unlink()
