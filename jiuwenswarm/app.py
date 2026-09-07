# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
"""Orchestrate AgentServer + Gateway in two processes (split layout, one command).

Runs ``jiuwenswarm.server.app_agentserver`` then ``jiuwenswarm.gateway.app_gateway`` with the same
environment as a normal CLI launch. Web RPC handlers live in ``app_web_handlers``.

Supports ``--dotenv <path>`` for multi-instance isolation.
"""

from __future__ import annotations
import subprocess
import sys
import time
import os

from jiuwenswarm.dotenv_early import parse_dotenv_early, get_parsed_dotenv, load_dotenv_runtime
parse_dotenv_early("jiuwenswarm-app")

# --- Now safe to import jiuwenswarm modules ---
from jiuwenswarm.common.debug_dump import install_async_dump_handler
from jiuwenswarm.common.utils import (
    cleanup_team_files,
    ensure_builtin_skills_installed,
    get_env_file,
    get_user_workspace_dir,
    prepare_workspace,
    reset_free_search_runtime_flags,
)

# Record the parsed dotenv path for subprocess spawning
_parsed_dotenv_path = get_parsed_dotenv()


_workspace_dir = get_user_workspace_dir()
_config_file = _workspace_dir / "config" / "config.yaml"
_new_workspace = _workspace_dir / "agent" / "workspace"
_old_workspace = _workspace_dir / "agent" / "jiuwenclaw_workspace"

# 始终清理 Team 旧版本遗留文件（幂等操作，在 prepare_workspace 之前执行）
cleanup_team_files(_workspace_dir)

from jiuwenswarm.common.config_split import maybe_extract_user_overlay

maybe_extract_user_overlay()
# Initialize if config doesn't exist, or if legacy workspace exists but new doesn't (migration)
if not _config_file.exists() or (_old_workspace.exists() and not _new_workspace.exists()):
    prepare_workspace(overwrite=False)

# 无条件补装缺失的内置技能（幂等，解决升级后技能不补装问题）
ensure_builtin_skills_installed()

load_dotenv_runtime(dotenv_path=get_env_file(), override=True)
reset_free_search_runtime_flags()


def main() -> None:
    import argparse
    import json

    parser = argparse.ArgumentParser(
        prog="jiuwenswarm-app",
        description="Start JiuWenSwarm AgentServer + Gateway (split layout, one command).",
    )
    parser.add_argument(
        "--dotenv",
        metavar="<path>",
        help="Load environment from .env file (processed at startup, not used here).",
    )
    parser.add_argument(
        "--name",
        metavar="<name>",
        help="Start a named instance from instances.yaml.",
    )
    args = parser.parse_args()

    install_async_dump_handler("app")

    # Handle --name: check if bootstrap .env was loaded successfully
    # (parse_dotenv_early() already processed it at module import time)
    dotenv_path = _parsed_dotenv_path
    if args.name and dotenv_path is None:
        # Early parsing failed - error was already printed
        raise SystemExit(1)

    python = sys.executable

    # Build subprocess commands – in frozen (PyInstaller) mode use flags
    # instead of -m which won't work with a bundled executable.
    if getattr(sys, "frozen", False):
        agent_cmd = [python, "--desktop-run-agent"]
        gateway_cmd = [python, "--desktop-run-gateway"]
    else:
        agent_cmd = [python, "-m", "jiuwenswarm.server.app_agentserver"]
        gateway_cmd = [python, "-m", "jiuwenswarm.gateway.app_gateway"]

    # Pass --dotenv to subprocesses for multi-instance isolation
    if dotenv_path is not None:
        agent_cmd.extend(["--dotenv", str(dotenv_path)])
        gateway_cmd.extend(["--dotenv", str(dotenv_path)])

    _popen_kwargs: dict = {}

    if "JIUWENSWARM_START_CMD" not in os.environ:
        try:
            os.environ["JIUWENSWARM_START_CMD"] = json.dumps(sys.argv[:])
        except (TypeError, ValueError, OverflowError):
            os.environ["JIUWENSWARM_START_CMD"] = json.dumps([str(a) for a in sys.argv[:]])

    agent = subprocess.Popen(agent_cmd, **_popen_kwargs)
    gateway = None
    try:
        gateway = subprocess.Popen(gateway_cmd, **_popen_kwargs)
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
