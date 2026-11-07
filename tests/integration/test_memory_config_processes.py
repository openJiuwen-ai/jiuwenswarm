# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Verify Gateway writes and execution reads in separate processes with SQLite."""

import os
from pathlib import Path
import subprocess
import sys


_PROCESS_SCRIPT = r'''
import asyncio
import sys

async def main():
    from agent_gateway.enterprise.db.database import Database
    if sys.argv[1] == 'read':
        from jiuwenswarm.enterprise.config.config_provider import DefaultConfigProvider, AgentConfigContext
        from jiuwenswarm.agents.harness.common.memory import config as memory
        memory._load_config = lambda: {'task_memory': {'enabled': False}}
        result = await DefaultConfigProvider().load_agent_config(AgentConfigContext(), base={'memory': {'mode': 'local'}})
        expected = sys.argv[2] == 'saved'
        assert result.config['memory']['mode'] == ('cloud' if expected else 'local'), result.config
        assert memory.get_task_memory_config()['enabled'] is expected, memory.get_task_memory_config()
    else:
        from agent_gateway.storage_assembly.setup import _create_persistent, create_memory_config_repository, create_enterprise_record_repositories
        from agent_gateway.config.memory.access import set_memory_config_repository
        from agent_gateway.config.enterprise.access import set_enterprise_record_repositories
        from agent_gateway.enterprise.config.manager_config.core.application_config.memory_config import MemoryConfigService
        from agent_gateway.enterprise.config.manager_config.core.application_config.task_memory_config import TaskMemoryConfigService
        store = _create_persistent()
        set_memory_config_repository(create_memory_config_repository(store))
        set_enterprise_record_repositories(create_enterprise_record_repositories(store))
        if sys.argv[1] == 'write':
            await MemoryConfigService().upsert(body={'mode': 'cloud'})
            await TaskMemoryConfigService().upsert({'enabled': True, 'llm_model': 'test'})
        else:
            await MemoryConfigService().delete()
            await TaskMemoryConfigService().delete()
        await store.close()
    await Database.release()
    print('PASS', *sys.argv[1:])

asyncio.run(main())
'''


def test_memory_configuration_survives_process_boundaries(tmp_path):
    root = Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    env.update({
        "JIUWENSWARM_EDITION": "enterprise",
        "JIUWENSWARM_CONFIG_SOURCE": "enterprise",
        "GATEWAY_DB_TYPE": "sqlite",
        "GATEWAY_SQLITE_PATH": str(tmp_path / "gateway.db"),
        "JIUWENSWARM_DATA_DIR": str(tmp_path / "data"),
        "AGENTOS_GATEWAY_LOG_DIR": str(tmp_path / "gateway-logs"),
        "PYTHONPATH": str(root),
    })
    for args in [("write",), ("read", "saved"), ("delete",), ("read", "deleted")]:
        result = subprocess.run(
            [sys.executable, "-c", _PROCESS_SCRIPT, *args],
            cwd=root, env=env, capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "PASS " + " ".join(args) in result.stdout
