# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentSSAS 测试根 conftest。

设置临时 SSAS_HOME 等全局 fixtures,隔离测试存储,避免污染本地环境。
所有测试通过 ssas_home fixture 获取临时存储根目录,由 AgentSSASConfig 消费。

存储路径约定(参考代码架构路径):
- 阶段一(agent_ssas_core)测试:$TEMP/agent_ssas/agent_ssas_core/<test>/
- 阶段二(backend_client)测试:$TEMP/agent_ssas/backend_client/<test>/
每个测试函数有独立的子目录(基于 pytest tmp_path),避免 SQLite WAL 连接
锁定导致的数据累积问题。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_ssas.core.framework.config.settings import AgentSSASConfig


def _determine_sub_dir(request: pytest.FixtureRequest) -> str:
    """根据测试模块路径确定架构子目录名。

    按测试模块所属的代码架构层级分配子目录:
    - tests/agent_ssas_core/   → "agent_ssas_core"
    - tests/backend_client/    → "backend_client"
    - 其他                     → "other"
    """
    module_path = request.node.fspath if hasattr(request.node, "fspath") else ""
    module_str = str(module_path)

    if "agent_ssas_core" in module_str:
        return "agent_ssas_core"
    elif "backend_client" in module_str:
        return "backend_client"
    return "other"


@pytest.fixture()
def ssas_home(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """测试存储根目录。

    基于 pytest 的 tmp_path(每个测试函数唯一),加上架构子目录前缀,
    路径形式为 $TEMP/agent_ssas/<sub_dir>/<test_name>/。
    每个测试有独立目录,避免 SQLite WAL 连接锁定导致的数据累积。

    设置 SSAS_HOME、JIUWENSWARM_DATA_DIR、JIUWENSWARM_HOME 三个环境变量
    指向同一目录,保证测试存储隔离,不污染用户主目录。

    返回: 存储根目录 Path 对象。
    """
    sub_dir = _determine_sub_dir(request)
    home = tmp_path / "agent_ssas" / sub_dir
    home.mkdir(parents=True, exist_ok=True)
    # 统一设置三个环境变量,避免回退到 ~/.jiuwenswarm
    monkeypatch.setenv("SSAS_HOME", str(home))
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(home))
    monkeypatch.setenv("JIUWENSWARM_HOME", str(home))
    return home


@pytest.fixture()
def ssas_config(ssas_home: Path) -> AgentSSASConfig:
    """基于临时 ssas_home 的 AgentSSASConfig。

    显式注入 ssas_home 字段,保证配置完全隔离。

    返回: AgentSSASConfig 实例,ssas_home 指向临时目录。
    """
    return AgentSSASConfig(ssas_home=str(ssas_home))


@pytest.fixture()
def base_common_kwargs() -> dict[str, Any]:
    """基础 common 层字段,供事件工厂复用。

    包含三层结构 common 层的默认字段值,调用方可覆盖特定字段。
    """
    return {
        "source": "AgentSSASSecurityRail",
        "timestamp": 1715000000.0,
        "interaction_seq": 0,
        "session_id": "test-session",
        "conversation_id": "test-session",
        "agent_id": "test-agent",
        "trace_id": "test-trace",
        "context_id": "test-ctx",
        "llm_call_seq": -1,
        "tool_call_seq": 0,
        "subsession_id": "",
        "tool_call_id": "call-001",
    }
