# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""common 注入点的进程装配守护（P0/P1 评审问题回归）。

P0（worker 启动即崩）与 P1（多域靠 incidental import 惰性注册）同根：
``common.external_providers`` 的注册责任在使用方进程，worker 进程启动链
不加载任何 server runtime 模块。修复为 ``server/runtime/provider_setup``
统一装配（import 全部 7 个实现模块触发自注册），由
``worker.lifecycle._preload_runtime_backend`` 入口调用。

本文件两层守护：
1. 域完整性——server 侧新增注入域时必须同步 ``provider_setup`` 与本断言，
   防止第 8 个域再靠 incidental import；
2. 冷启动回归——子进程按 worker 真实导入顺序复现 P0 场景。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# server 侧全部自注册域（与 provider_setup.install 的 import 清单一一对应）。
# 新增域：实现模块末尾自注册 + provider_setup 补 import + 本清单补条目。
SERVER_PROVIDER_DOMAINS = {
    "mcp_state",
    "mcp_registry",
    "mcp_credential",
    "a2ui",
    "team_binding_store",
    "team_entity_store",
    "session_metadata",
}


def test_provider_setup_covers_all_server_domains() -> None:
    """域完整性：install 后 7 域齐备（幂等，单测进程已注册亦成立）。"""
    from jiuwenswarm.common.external_providers import registered_domains
    from jiuwenswarm.server.runtime import provider_setup

    provider_setup.install()
    missing = SERVER_PROVIDER_DOMAINS - set(registered_domains())
    assert not missing, (
        f"server 侧注入域未装配: {sorted(missing)} —— "
        "请同步 provider_setup.install 的 import 清单"
    )


@pytest.mark.slow
def test_worker_cold_start_preload_registers_providers() -> None:
    """P0 回归：worker 导入顺序（不加载 server runtime）下 preload 完成装配。"""
    code = (
        "import os\n"
        "os.environ['JIUWENSWARM_RUNTIME_WORKSPACE_READY'] = '1'\n"
        "import jiuwenswarm.server.worker.service\n"
        "import jiuwenswarm.server.worker.lifecycle as lc\n"
        "from jiuwenswarm.common.external_providers import registered_domains\n"
        "assert registered_domains() == [], 'cold process must have no providers'\n"
        "lc._preload_runtime_backend()\n"
        "print(sorted(registered_domains()))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr[-800:]
    assert str(sorted(SERVER_PROVIDER_DOMAINS)) in result.stdout, result.stdout


@pytest.mark.slow
def test_front_process_config_path_registers_providers() -> None:
    """P1 回归：Front（主进程）控制面的 config 路径完成装配。

    Front 链（front/router → control/config_service → config_panel handlers）
    零自然装配——app_agentserver._install_common_providers 是唯一装配点。
    子进程按 Front 真实加载顺序复现：装配前 config 面板 handler 触发的
    provider 域不可用，装配后 7 域齐备。
    """
    code = (
        "import jiuwenswarm.server.front.router as router\n"
        "router._load_config_service()\n"
        "from jiuwenswarm.common.external_providers import registered_domains\n"
        "assert registered_domains() == [], 'cold front process must have no providers'\n"
        "import jiuwenswarm.server.app_agentserver as entry\n"
        "entry._install_common_providers()\n"
        "print(sorted(registered_domains()))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr[-800:]
    assert str(sorted(SERVER_PROVIDER_DOMAINS)) in result.stdout, result.stdout
