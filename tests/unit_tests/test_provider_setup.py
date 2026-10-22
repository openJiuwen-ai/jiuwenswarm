# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""common 注入点的进程装配守护（P0/P1 评审问题回归）。

P0（worker 启动即崩）与 P1（多域靠 incidental import 惰性注册）同根：
``common.external_providers`` 的注册责任在使用方进程，worker 进程启动链
不加载任何 server runtime 模块。修复为 ``server/runtime/provider_setup``
统一装配（import 全部 7 个实现模块触发自注册），由
``worker.runtime_backend.preload_runtime_backend`` 入口调用。

本文件两层守护：
1. 域完整性——server 侧新增注入域时必须同步 ``provider_setup`` 与本断言，
   防止第 8 个域再靠 incidental import；
2. 冷启动回归——子进程按 worker 真实导入顺序复现 P0 场景。
"""

from __future__ import annotations

import os
import shutil
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
    "cron_store_factory",
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
def test_worker_cold_start_preload_registers_providers(tmp_path) -> None:
    """P0 回归：worker 导入顺序（不加载 server runtime）下 preload 完成装配。"""
    code = (
        "import os\n"
        "os.environ['JIUWENSWARM_RUNTIME_WORKSPACE_READY'] = '1'\n"
        "import jiuwenswarm.server.worker.service\n"
        "from jiuwenswarm.server.worker import runtime_backend\n"
        "from jiuwenswarm.common.external_providers import registered_domains\n"
        "assert registered_domains() == [], 'cold process must have no providers'\n"
        "runtime_backend.preload_runtime_backend()\n"
        "print(sorted(registered_domains()))\n"
    )
    config_dir = tmp_path / "data" / "config"
    config_dir.mkdir(parents=True)
    shutil.copyfile(REPO_ROOT / "jiuwenswarm/resources/config.yaml", config_dir / "config.yaml")
    (config_dir / ".env").write_text("# isolated provider preload regression\n", encoding="utf-8")
    env = {**os.environ, "JIUWENSWARM_DATA_DIR": str(tmp_path / "data"),
           "JIUWENSWARM_HOME": str(tmp_path / "home"), "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
        cwd=REPO_ROOT,
        env=env,
    )
    assert result.returncode == 0, result.stderr[-800:]
    assert str(sorted(SERVER_PROVIDER_DOMAINS)) in result.stdout, result.stdout


@pytest.mark.parametrize("workspace_ready", ["0", "1"])
def test_worker_installs_providers_before_parallel_required_preparation(monkeypatch, workspace_ready) -> None:
    from jiuwenswarm.common import utils
    from jiuwenswarm.server.runtime import provider_setup
    from jiuwenswarm.server.worker import runtime_backend

    events = []
    original_install = provider_setup.install

    def install() -> None:
        assert ("cleanup" in events) is (workspace_ready != "1")
        original_install()
        events.append("providers")

    def prepare(stage: str) -> None:
        assert "providers" in events
        events.append(stage)

    monkeypatch.setenv("JIUWENSWARM_RUNTIME_WORKSPACE_READY", workspace_ready)
    monkeypatch.setattr(utils, "cleanup_stale_openjiuwen_descs", lambda: events.append("cleanup"))
    monkeypatch.setattr(provider_setup, "install", install)
    monkeypatch.setattr(runtime_backend, "_prepare_worker_configuration", lambda: prepare("config"))
    monkeypatch.setattr(runtime_backend, "_preload_sdk_execution_modules", lambda: prepare("sdk"))
    monkeypatch.setattr(runtime_backend, "_configure_openjiuwen_logging", lambda: None)
    monkeypatch.setattr(runtime_backend, "_apply_runtime_entry_patches", lambda: None)
    runtime_backend.preload_runtime_backend()
    assert events.count("providers") == 1
    assert set(events) == ({"providers", "config", "sdk", "cleanup"} if workspace_ready != "1"
                           else {"providers", "config", "sdk"})


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
        "assert 'openjiuwen' not in __import__('sys').modules\n"
        "from jiuwenswarm.common.external_providers import provider\n"
        "lazy = provider('team_binding_store')\n"
        "entry._install_common_providers()\n"
        "assert provider('team_binding_store') is lazy\n"
        "assert lazy.validate_team_name('provider-test') == 'provider-test'\n"
        "from jiuwenswarm.server.runtime import team_binding_store, provider_setup\n"
        "assert provider('team_binding_store') is team_binding_store\n"
        "entry._install_common_providers()\n"
        "assert provider('team_binding_store') is team_binding_store\n"
        "provider_setup.install()\n"
        "assert provider('team_entity_store').__name__.endswith('.team_entity_store')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr[-800:]
    assert str(sorted(SERVER_PROVIDER_DOMAINS)) in result.stdout, result.stdout
