# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""env_loader 模块的单元测试."""

import asyncio
import json
import os
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from jiuwenclaw.common.env_schema import (
    ALLOWED_ENV_KEYS,
    PROTECTED_ENV_KEYS,
)
from jiuwenclaw.agentserver.env_loader import (
    _filter_env_vars,
    _load_from_file,
    _load_local_fallback,
    load_env_from_file,
    wait_and_load_env,
    ENV_FILE_PATH,
)


class TestLoadLocalFallback:
    """测试 _load_local_fallback 函数."""

    def test_load_existing_local_vars(self, monkeypatch):
        """测试加载已存在的本地环境变量."""
        monkeypatch.setenv("SKILLDEV_AGENT_SYSTEM_PROMPT", "local_value")
        result = _load_local_fallback()
        assert result.get("SKILLDEV_AGENT_SYSTEM_PROMPT") == "local_value"

    def test_skip_missing_local_vars(self, monkeypatch):
        """测试跳过缺失的环境变量."""
        monkeypatch.delenv("SKILLDEV_AGENT_SYSTEM_PROMPT", raising=False)
        result = _load_local_fallback()
        assert "SKILLDEV_AGENT_SYSTEM_PROMPT" not in result

    def test_empty_local_env(self, monkeypatch):
        """测试没有匹配本地环境变量的情况."""
        # 清除白名单中所有环境变量
        for key in list(ALLOWED_ENV_KEYS):
            monkeypatch.delenv(key, raising=False)
        result = _load_local_fallback()
        assert result == {}


class TestLoadFromFile:
    """测试 _load_from_file 函数."""

    def test_load_valid_env_file(self, tmp_path, monkeypatch):
        """测试加载有效的环境文件."""
        env_file = tmp_path / "agentserver_env.json"
        env_data = {
            "version": "1.0",
            "source": "gateway",
            "env_vars": {
                "SKILLDEV_AGENT_SYSTEM_PROMPT": "file_value",
            },
        }
        env_file.write_text(json.dumps(env_data))

        # Mock ENV_FILE_PATH
        monkeypatch.setattr(
            "jiuwenclaw.agentserver.env_loader.ENV_FILE_PATH",
            env_file,
        )

        result = _load_from_file()
        assert result.get("SKILLDEV_AGENT_SYSTEM_PROMPT") == "file_value"
        assert os.environ.get("SKILLDEV_AGENT_SYSTEM_PROMPT") == "file_value"

    def test_invalid_json_returns_fallback(self, tmp_path, monkeypatch):
        """测试无效 JSON 返回降级结果."""
        env_file = tmp_path / "agentserver_env.json"
        env_file.write_text("invalid json")

        monkeypatch.setattr(
            "jiuwenclaw.agentserver.env_loader.ENV_FILE_PATH",
            env_file,
        )

        result = _load_from_file()
        # 应返回空字典或降级结果
        assert isinstance(result, dict)

    def test_missing_file_returns_fallback(self, tmp_path, monkeypatch):
        """测试缺失文件返回降级结果."""
        nonexistent_file = tmp_path / "nonexistent.json"

        monkeypatch.setattr(
            "jiuwenclaw.agentserver.env_loader.ENV_FILE_PATH",
            nonexistent_file,
        )

        # 文件不存在，应优雅处理并返回空字典
        assert not nonexistent_file.exists()
        result = load_env_from_file()
        assert result == {}


class TestLoadEnvFromFile:
    """测试 load_env_from_file 函数."""

    def test_file_not_exists(self, tmp_path, monkeypatch):
        """测试文件不存在的情况."""
        nonexistent_file = tmp_path / "nonexistent.json"
        monkeypatch.setattr(
            "jiuwenclaw.agentserver.env_loader.ENV_FILE_PATH",
            nonexistent_file,
        )

        result = load_env_from_file()
        assert result == {}

    def test_file_exists(self, tmp_path, monkeypatch):
        """测试文件存在的情况."""
        env_file = tmp_path / "agentserver_env.json"
        env_data = {
            "version": "1.0",
            "env_vars": {
                "SKILLDEV_AGENT_SYSTEM_PROMPT": "test_prompt",
            },
        }
        env_file.write_text(json.dumps(env_data))

        monkeypatch.setattr(
            "jiuwenclaw.agentserver.env_loader.ENV_FILE_PATH",
            env_file,
        )

        result = load_env_from_file()
        assert "SKILLDEV_AGENT_SYSTEM_PROMPT" in result


class TestWaitAndLoadEnv:
    """测试 wait_and_load_env 函数."""

    @pytest.mark.asyncio
    async def test_file_already_exists(self, tmp_path, monkeypatch):
        """测试文件已存在（无需等待）的情况."""
        env_file = tmp_path / "agentserver_env.json"
        env_data = {
            "version": "1.0",
            "env_vars": {
                "SKILLDEV_AGENT_SYSTEM_PROMPT": "immediate",
            },
        }
        env_file.write_text(json.dumps(env_data))

        monkeypatch.setattr(
            "jiuwenclaw.agentserver.env_loader.ENV_FILE_PATH",
            env_file,
        )

        result = await wait_and_load_env(poll_interval=0.1)
        assert "SKILLDEV_AGENT_SYSTEM_PROMPT" in result

    @pytest.mark.asyncio
    async def test_wait_for_file_creation(self, tmp_path, monkeypatch):
        """测试等待文件创建."""
        env_file = tmp_path / "agentserver_env.json"

        monkeypatch.setattr(
            "jiuwenclaw.agentserver.env_loader.ENV_FILE_PATH",
            env_file,
        )

        # 延迟创建文件
        async def delayed_create():
            await asyncio.sleep(0.2)
            env_data = {
                "version": "1.0",
                "env_vars": {
                    "SKILLDEV_AGENT_SYSTEM_PROMPT": "delayed",
                },
            }
            env_file.write_text(json.dumps(env_data))

        # 并发运行
        task = asyncio.create_task(delayed_create())
        result = await wait_and_load_env(poll_interval=0.05)
        await task

        assert "SKILLDEV_AGENT_SYSTEM_PROMPT" in result

    @pytest.mark.asyncio
    async def test_infinite_wait_by_default(self, tmp_path, monkeypatch):
        """测试默认无限等待（无超时）."""
        env_file = tmp_path / "agentserver_env.json"

        monkeypatch.setattr(
            "jiuwenclaw.agentserver.env_loader.ENV_FILE_PATH",
            env_file,
        )

        # 默认参数不应超时
        # 0.3秒后创建文件
        async def create_file():
            await asyncio.sleep(0.3)
            env_data = {"version": "1.0", "env_vars": {}}
            env_file.write_text(json.dumps(env_data))

        task = asyncio.create_task(create_file())
        # 不应抛出超时异常
        result = await wait_and_load_env(poll_interval=0.05)
        await task

        assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_timeout_with_fallback(self, tmp_path, monkeypatch):
        """测试超时后降级到本地环境变量."""
        env_file = tmp_path / "agentserver_env.json"

        monkeypatch.setattr(
            "jiuwenclaw.agentserver.env_loader.ENV_FILE_PATH",
            env_file,
        )

        # 设置本地环境变量用于降级
        monkeypatch.setenv("SKILLDEV_AGENT_SYSTEM_PROMPT", "fallback_value")

        # 文件永不创建，应超时并降级
        result = await wait_and_load_env(timeout=0.1, poll_interval=0.05)

        # 应返回降级结果
        assert isinstance(result, dict)


class TestHotReloadEnvVars:
    """测试进程内环境变量热更新."""

    def test_env_var_hot_update_in_process(self, tmp_path, monkeypatch):
        """测试环境变量在当前进程中更新生效."""
        # 首先清除目标环境变量
        monkeypatch.delenv("SKILLDEV_AGENT_SYSTEM_PROMPT", raising=False)

        env_file = tmp_path / "agentserver_env.json"
        env_data = {
            "version": "1.0",
            "source": "gateway",
            "env_vars": {
                "SKILLDEV_AGENT_SYSTEM_PROMPT": "hot_reloaded_value",
            },
        }
        env_file.write_text(json.dumps(env_data))

        monkeypatch.setattr(
            "jiuwenclaw.agentserver.env_loader.ENV_FILE_PATH",
            env_file,
        )

        # 加载前环境变量不应存在
        assert os.getenv("SKILLDEV_AGENT_SYSTEM_PROMPT") is None

        # 加载环境变量
        result = _load_from_file()

        # 加载后环境变量应在当前进程中更新
        assert os.getenv("SKILLDEV_AGENT_SYSTEM_PROMPT") == "hot_reloaded_value"
        assert result.get("SKILLDEV_AGENT_SYSTEM_PROMPT") == "hot_reloaded_value"

    def test_env_var_overwrite_existing(self, tmp_path, monkeypatch):
        """测试新环境变量值覆盖旧值."""
        # 设置初始值
        monkeypatch.setenv("SKILLDEV_AGENT_SYSTEM_PROMPT", "old_value")

        env_file = tmp_path / "agentserver_env.json"
        env_data = {
            "version": "1.0",
            "source": "gateway",
            "env_vars": {
                "SKILLDEV_AGENT_SYSTEM_PROMPT": "new_hot_reloaded_value",
            },
        }
        env_file.write_text(json.dumps(env_data))

        monkeypatch.setattr(
            "jiuwenclaw.agentserver.env_loader.ENV_FILE_PATH",
            env_file,
        )

        # 加载前验证旧值
        assert os.getenv("SKILLDEV_AGENT_SYSTEM_PROMPT") == "old_value"

        # 加载新环境变量
        result = _load_from_file()

        # 加载后环境变量应更新为新值
        assert os.getenv("SKILLDEV_AGENT_SYSTEM_PROMPT") == "new_hot_reloaded_value"
        assert result.get("SKILLDEV_AGENT_SYSTEM_PROMPT") == "new_hot_reloaded_value"


class TestSubprocessInheritance:
    """测试子进程继承更新后的环境变量."""

    def test_subprocess_inherits_updated_env(self, tmp_path, monkeypatch):
        """测试子进程继承更新后的环境变量."""
        import subprocess
        import sys

        # 清除并设置测试环境变量
        monkeypatch.delenv("SKILLDEV_AGENT_SYSTEM_PROMPT", raising=False)

        env_file = tmp_path / "agentserver_env.json"
        env_data = {
            "version": "1.0",
            "source": "gateway",
            "env_vars": {
                "SKILLDEV_AGENT_SYSTEM_PROMPT": "inherited_value",
            },
        }
        env_file.write_text(json.dumps(env_data))

        monkeypatch.setattr(
            "jiuwenclaw.agentserver.env_loader.ENV_FILE_PATH",
            env_file,
        )

        # 在主进程中加载环境变量
        _load_from_file()

        # 验证主进程有该环境变量
        assert os.getenv("SKILLDEV_AGENT_SYSTEM_PROMPT") == "inherited_value"

        # 创建简单的 Python 脚本在子进程中检查环境变量
        test_script = """
import os
import sys
value = os.getenv('SKILLDEV_AGENT_SYSTEM_PROMPT', 'NOT_SET')
print(f'ENV_VALUE:{value}')
sys.exit(0 if value == 'inherited_value' else 1)
"""

        # 运行子进程并检查是否继承环境变量
        result = subprocess.run(
            [sys.executable, "-c", test_script],
            capture_output=True,
            text=True,
            env=os.environ.copy(),  # 继承当前环境
        )

        # 检查子进程输出
        assert result.returncode == 0
        assert "ENV_VALUE:inherited_value" in result.stdout

    def test_subprocess_inherits_multiple_envs(self, tmp_path, monkeypatch):
        """测试子进程继承多个更新的环境变量."""
        import subprocess
        import sys

        # 首先清除测试环境变量
        monkeypatch.delenv("SKILLDEV_AGENT_SYSTEM_PROMPT", raising=False)
        monkeypatch.delenv("LOG_ROOT_PATH", raising=False)

        env_file = tmp_path / "agentserver_env.json"
        env_data = {
            "version": "1.0",
            "source": "gateway",
            "env_vars": {
                "SKILLDEV_AGENT_SYSTEM_PROMPT": "multi_test_value",
                "LOG_ROOT_PATH": "/test/log/path",
            },
        }
        env_file.write_text(json.dumps(env_data))

        monkeypatch.setattr(
            "jiuwenclaw.agentserver.env_loader.ENV_FILE_PATH",
            env_file,
        )

        # 加载环境变量
        _load_from_file()

        # 验证主进程有这些环境变量
        assert os.getenv("SKILLDEV_AGENT_SYSTEM_PROMPT") == "multi_test_value"
        assert os.getenv("LOG_ROOT_PATH") == "/test/log/path"

        # 验证多个环境变量的测试脚本
        test_script = """
import os
import sys

prompt = os.getenv('SKILLDEV_AGENT_SYSTEM_PROMPT', 'NOT_SET')
log_path = os.getenv('LOG_ROOT_PATH', 'NOT_SET')

print(f'PROMPT:{prompt}')
print(f'LOG_PATH:{log_path}')

success = (prompt == 'multi_test_value' and log_path == '/test/log/path')
sys.exit(0 if success else 1)
"""

        # 运行子进程
        result = subprocess.run(
            [sys.executable, "-c", test_script],
            capture_output=True,
            text=True,
            env=os.environ.copy(),
        )

        assert result.returncode == 0
        assert "PROMPT:multi_test_value" in result.stdout
        assert "LOG_PATH:/test/log/path" in result.stdout
