# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Orchestrate AgentServer + Gateway in two processes (split layout, one command).

Runs ``jiuwenclaw.app_agentserver`` then ``jiuwenclaw.app_gateway`` with the same
environment as a normal CLI launch. Web RPC handlers live in ``app_web_handlers``.
"""

from __future__ import annotations

import subprocess
import sys
import time

from dotenv import load_dotenv

from jiuwenclaw.utils import (
    get_user_workspace_dir,
    get_env_file,
    prepare_workspace,
    cleanup_team_files,
    update_config,
    get_multi_tenant_user_workspace_dir,
)


_workspace_dir = get_user_workspace_dir()
_config_file = _workspace_dir / "config" / "config.yaml"
# 多租户路径：service_default/agent_default/agent/jiuwenclaw_workspace
_multi_tenant_workspace = get_multi_tenant_user_workspace_dir("default", "default")
if _multi_tenant_workspace:
    _new_workspace = _multi_tenant_workspace / "agent" / "jiuwenclaw_workspace"
else:
    _new_workspace = _workspace_dir / "agent" / "jiuwenclaw_workspace"
_old_workspace = _workspace_dir / "agent" / "workspace"

# 始终清理 Team 旧版本遗留文件（幂等操作，在 prepare_workspace 之前执行）
cleanup_team_files(_workspace_dir)

# Initialize if config doesn't exist, or if legacy workspace exists but new doesn't (migration)
if not _config_file.exists() or (_old_workspace.exists() and not _new_workspace.exists()):
    prepare_workspace(overwrite=False)
else:
    update_config()

load_dotenv(dotenv_path=get_env_file())


def main() -> None:
    python = sys.executable

    agent = subprocess.Popen([python, "-m", "jiuwenclaw.app_agentserver"])
    gateway = None
    try:
        time.sleep(0.4)
        gateway = subprocess.Popen([python, "-m", "jiuwenclaw.app_gateway"])
    except Exception:
        if agent.poll() is None:
            agent.terminate()
        raise

    procs: list[subprocess.Popen] = [agent] + ([gateway] if gateway else [])

    def _terminate_all() -> None:
        for p in procs:
            if p.poll() is None:
                p.terminate()
        deadline = time.time() + 12
        while time.time() < deadline:
            if all(p.poll() is not None for p in procs):
                break
            time.sleep(0.1)
        for p in procs:
            if p.poll() is None:
                p.kill()

    exit_code = 0
    try:
        while True:
            if agent.poll() is not None:
                exit_code = agent.returncode or 0
                break
            if gateway is not None and gateway.poll() is not None:
                exit_code = gateway.returncode or 0
                break
            time.sleep(0.25)
    except KeyboardInterrupt:
        exit_code = 130
    finally:
        _terminate_all()

    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
