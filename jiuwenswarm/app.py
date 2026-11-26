# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
"""Orchestrate the recovery sidecar, AgentServer and Gateway in one command.

Runs ``jiuwenswarm.server.app_agentserver`` then ``agent_gateway.app_gateway`` with the same
environment as a normal CLI launch. Web RPC handlers live in ``app_web_handlers``.

Supports ``--dotenv <path>`` for multi-instance isolation.
"""

from __future__ import annotations
import signal
import sys
import threading
import os
import uuid
import logging
import subprocess

from jiuwenswarm.dotenv_early import parse_dotenv_early, get_parsed_dotenv, load_dotenv_runtime
parse_dotenv_early("jiuwenswarm-app")

# --- Now safe to import jiuwenswarm modules ---
from jiuwenswarm.common.debug_dump import install_async_dump_handler
from jiuwenswarm.common.process_supervision import SUPERVISOR_PID_ENV, supervise
from jiuwenswarm.common.system_plugin_recovery import (
    SystemPluginRecovery,
    begin_system_plugin_recovery_session,
)
from jiuwenswarm.common.paths import get_gateway_config_dir
from jiuwenswarm.common.utils import (
    apply_free_search_runtime_defaults,
    cleanup_stale_openjiuwen_descs,
    ensure_gateway_config,
    get_env_file,
    prepare_local_gateway_config,
    prepare_runtime_workspace,
)

# Record the parsed dotenv path for subprocess spawning
_parsed_dotenv_path = get_parsed_dotenv()
logger = logging.getLogger(__name__)


cleanup_stale_openjiuwen_descs()
prepare_runtime_workspace(cleanup_stale_descs=False)

_env_file = get_env_file()
load_dotenv_runtime(dotenv_path=_env_file, override=True)
ensure_gateway_config()
apply_free_search_runtime_defaults()


def _stop_optional_recovery_sidecar(process: subprocess.Popen) -> None:
    """End the optional sidecar without affecting the supervised backend stack."""
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            logger.warning("[SystemPluginRecovery] could not kill recovery sidecar", exc_info=True)
    except OSError:
        logger.warning("[SystemPluginRecovery] could not stop recovery sidecar", exc_info=True)


def main() -> None:
    import argparse
    import json

    parser = argparse.ArgumentParser(
        prog="jiuwenswarm-app",
        description="Start JiuWenSwarm recovery, AgentServer and Gateway services.",
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
    parser.add_argument("--keep-worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    # Route SIGTERM through the same shutdown path as Ctrl-C.
    #
    # Python's default SIGTERM disposition kills this process outright, so the
    # ``finally`` teardown in ``supervise()`` never runs and the AgentServer /
    # Gateway children keep running as orphans holding their ports. That is
    # exactly what happens when a launcher stops this process by PID (e.g.
    # ``jiuwenswarm-start --stop <name>``); Ctrl-C never showed the bug because
    # SIGINT reaches the whole foreground process group, children included.
    # Mirrors the same call in ``start_services.main()``.
    signal.signal(signal.SIGTERM, signal.default_int_handler)

    install_async_dump_handler("app")

    # Handle --name: check if bootstrap .env was loaded successfully
    # (parse_dotenv_early() already processed it at module import time)
    dotenv_path = _parsed_dotenv_path
    if args.name and dotenv_path is None:
        # Early parsing failed - error was already printed
        raise SystemExit(1)

    # gateway 专属配置：config/config-gateway.yaml（get_gateway_config_file），
    # env 注入 config 目录（gateway 子进程读取 <env>/config-gateway.yaml）。
    gateway_config_dir = get_gateway_config_dir()

    python = sys.executable

    # Build subprocess commands – in frozen (PyInstaller) mode use flags
    # instead of -m which won't work with a bundled executable.
    if getattr(sys, "frozen", False):
        agent_cmd = [python, "--desktop-run-agent"]
        gateway_cmd = [python, "--desktop-run-gateway"]
        recovery_cmd = [python, "--extension-recovery-sidecar"]
    else:
        agent_cmd = [python, "-m", "jiuwenswarm.server.app_agentserver"]
        gateway_cmd = [python, "-m", "agent_gateway.app_gateway"]
        recovery_cmd = [python, "-m", "jiuwenswarm.common.system_plugin_recovery_sidecar"]

    # Pass --dotenv to subprocesses for multi-instance isolation
    if dotenv_path is not None:
        agent_cmd.extend(["--dotenv", str(dotenv_path)])
        gateway_cmd.extend(["--dotenv", str(dotenv_path)])

    if "JIUWENSWARM_START_CMD" not in os.environ:
        try:
            os.environ["JIUWENSWARM_START_CMD"] = json.dumps(sys.argv[:])
        except (TypeError, ValueError, OverflowError):
            os.environ["JIUWENSWARM_START_CMD"] = json.dumps([str(a) for a in sys.argv[:]])

    # Take the child environment snapshot only after recording the original
    # launcher command. UpgradeExecutor uses it to build a valid restart.
    recovery_session_id = uuid.uuid4().hex
    os.environ["JIUWENSWARM_RECOVERY_SESSION_ID"] = recovery_session_id

    def _prepare_recovery_history() -> None:
        # The first config read imports openjiuwen.symphony. Do it together with
        # the startup record in a daemon thread so the backend children start
        # immediately; each child also creates the record when it wins the race.
        try:
            from jiuwenswarm.common.config import warm_up_config

            warm_up_config()
            begin_system_plugin_recovery_session()
        except Exception:  # noqa: BLE001 - recovery history must not block service startup
            logger.warning("[SystemPluginRecovery] could not create startup record", exc_info=True)

    threading.Thread(
        target=_prepare_recovery_history, name="recovery-history", daemon=True,
    ).start()

    child_env = {
        **os.environ,
        "JIUWENSWARM_RUNTIME_WORKSPACE_READY": "1",
        SUPERVISOR_PID_ENV: str(os.getpid()),
        "JIUWENSWARM_RECOVERY_SESSION_ID": recovery_session_id,
    }
    gateway_env = child_env.copy()
    prepare_local_gateway_config(gateway_env)
    gateway_env.setdefault("JIUWENSWARM_GATEWAY_CONFIG_DIR", str(gateway_config_dir))
    agent_env = {
        key: value
        for key, value in child_env.items()
        if key != "JIUWENSWARM_GATEWAY_CONFIG_DIR"
    }
    _popen_kwargs: dict = {"env": child_env}
    if os.name != "nt":
        # Own process group per child so a crash respawn can killpg the dead
        # child's lingering descendants (see process_supervision). Ctrl-C and
        # SIGTERM still tear everything down through this supervisor's handlers.
        _popen_kwargs["start_new_session"] = True
    gateway_popen_kwargs = {**_popen_kwargs, "env": gateway_env}
    agent_popen_kwargs = {**_popen_kwargs, "env": agent_env}

    from jiuwenswarm.common.process_supervision import application_shutdown
    from jiuwenswarm.server.supervisor.shutdown import shutdown_application_worker

    recovery: SystemPluginRecovery | None = None

    def _recovery() -> SystemPluginRecovery:
        nonlocal recovery
        if recovery is None:
            recovery = SystemPluginRecovery()
        return recovery

    def _retire_recovery_worker() -> None:
        if shutdown_application_worker():
            return
        # A Worker that could not drain must not take the Web front down with
        # it: keep supervision alive, start the replacements, and surface the
        # incomplete drain instead of aborting the whole application.
        logger.error(
            "[SystemPluginRecovery] Worker shutdown incomplete; restarting services anyway"
        )

    def _backend_restart_requested() -> bool:
        try:
            return _recovery().consume_backend_restart(recovery_session_id)
        except Exception:  # noqa: BLE001 - recovery must not stop supervision
            logger.warning("[SystemPluginRecovery] could not read restart request", exc_info=True)
            return False

    recovery_process: subprocess.Popen | None = None
    try:
        recovery_process = subprocess.Popen(recovery_cmd, **_popen_kwargs)
    except OSError:
        logger.warning("[SystemPluginRecovery] could not start recovery sidecar", exc_info=True)
    try:
        exit_code = supervise(
            {"agentserver": agent_cmd, "gateway": gateway_cmd},
            _popen_kwargs,
            restart_requested=_backend_restart_requested,
            restart_names=("gateway", "agentserver"),
            before_restart=_retire_recovery_worker,
            command_popen_kwargs={
                "agentserver": agent_popen_kwargs,
                "gateway": gateway_popen_kwargs,
            },
        )
    finally:
        if recovery_process is not None:
            _stop_optional_recovery_sidecar(recovery_process)
    # Only a completed Front teardown permits final Worker shutdown.
    # An explicit recovery already retired its Worker before starting replacements.
    with application_shutdown():
        if not args.keep_worker and not shutdown_application_worker():
            raise SystemExit(1)
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
