# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Required Worker backend preparation before Runtime consumers start.

SDK code and configuration migrations overlap behind one joined barrier.
Heavy dependencies are imported inside functions, after description cleanup.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import time
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)


def _log_preparation_stage(stage: str, began: float) -> None:
    logger.info("[Worker] preparation stage=%s duration_ms=%.3f", stage,
                (time.monotonic() - began) * 1000)


def _configure_openjiuwen_logging() -> None:
    """Install OpenJiuwen logging after the Worker IPC socket is listening."""
    from jiuwenswarm.common.media_capability_config import (
        migrate_media_capability_switches,
    )
    from jiuwenswarm.common.utils import (
        apply_free_search_runtime_defaults,
        get_env_file,
        get_root_dir,
    )
    from openjiuwen.core.common.logging import LogManager

    migrate_media_capability_switches(get_env_file())
    apply_free_search_runtime_defaults()

    _logging_yaml = get_root_dir() / "config" / "logging.yaml"
    if _logging_yaml.exists():
        from openjiuwen.core.common.logging.log_config import configure_log

        configure_log(str(_logging_yaml))
        return

    try:
        from openjiuwen.core.common.logging.log_config import configure_log_config

        _oj_home = os.environ.get("JIUWENSWARM_HOME") or os.environ.get("HOME") or os.path.expanduser("~")
        _oj_log_dir = f"{_oj_home}/.jiuwenswarm/logs/"
        configure_log_config({
            "backend": "default",
            "level": "INFO",
            "log_path": _oj_log_dir,
            "log_file": "run/jiuwen.log",
            "output": ["console", "file"],
            "structured_output_format": "json",
            "interface_log_file": "interface/jiuwen_interface.log",
            "prompt_builder_interface_log_file": "interface/jiuwen_prompt_builder_interface.log",
            "performance_log_file": "performance/jiuwen_performance.log",
        })
    except Exception as _log_cfg_exc:  # noqa: BLE001
        logger.warning(
            "openjiuwen log config failed; using degraded logging: %s",
            _log_cfg_exc,
        )

    for _lg in LogManager.get_all_loggers().values():
        _lg.set_level(logging.CRITICAL)

    from jiuwenswarm.common.utils import get_logs_dir

    _logs_root = get_logs_dir()
    _logs_root.mkdir(parents=True, exist_ok=True)
    _perm_fmt = logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    _perm_fh = logging.handlers.RotatingFileHandler(
        _logs_root / "permissions.log",
        maxBytes=20 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    _perm_fh.setLevel(logging.INFO)
    _perm_fh.setFormatter(_perm_fmt)
    _perm_sh = logging.StreamHandler()
    _perm_sh.setLevel(logging.INFO)
    _perm_sh.setFormatter(_perm_fmt)

    _sec_logger = logging.getLogger("openjiuwen.harness.security")
    _sec_logger.setLevel(logging.INFO)
    if not _sec_logger.handlers:
        _sec_logger.addHandler(_perm_fh)
        _sec_logger.addHandler(_perm_sh)
    _sec_logger.propagate = False

    _common_logger = logging.getLogger("common")
    _common_logger.setLevel(logging.INFO)

    class _PermissionEngineFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            return "[PermissionEngine]" in record.getMessage()

    _perm_filter = _PermissionEngineFilter()
    _common_fh = logging.handlers.RotatingFileHandler(
        _logs_root / "permissions.log",
        maxBytes=20 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    _common_fh.setLevel(logging.INFO)
    _common_fh.setFormatter(_perm_fmt)
    _common_fh.addFilter(_perm_filter)
    _common_sh = logging.StreamHandler()
    _common_sh.setLevel(logging.INFO)
    _common_sh.setFormatter(_perm_fmt)
    _common_sh.addFilter(_perm_filter)
    _common_logger.addHandler(_common_fh)
    _common_logger.addHandler(_common_sh)
    _common_logger.propagate = False

    _perm_ns_logger = logging.getLogger("jiuwenswarm.agents.harness.common.rails.permissions")
    _perm_ns_logger.setLevel(logging.INFO)
    if not _perm_ns_logger.handlers:
        _perm_ns_logger.addHandler(_perm_fh)
        _perm_ns_logger.addHandler(_perm_sh)
    _perm_ns_logger.propagate = False


def _apply_runtime_entry_patches() -> None:
    from jiuwenswarm.common.native_tokenizer_cache import install_native_tokenizer_cache
    from jiuwenswarm.agents.harness.common.tools.bash_tool_safety import (
        install_shell_tool_safety_hooks,
    )
    from jiuwenswarm.common.llm_provider_compat_patch import apply_provider_compat_patches
    from jiuwenswarm.common.team_workspace_read_guard import (
        install_team_workspace_read_guard,
        preload_bundled_team_descriptions,
    )
    from jiuwenswarm.llm_sse_patch import apply_openai_sse_invoke_patch

    install_native_tokenizer_cache()
    install_shell_tool_safety_hooks()
    apply_provider_compat_patches()
    try:
        install_team_workspace_read_guard()
    except Exception:  # noqa: BLE001
        logger.warning("[TeamWorkspace] optional tool read guard installation failed", exc_info=True)
    try:
        preload_bundled_team_descriptions()
    except Exception:  # noqa: BLE001
        logger.warning("[TeamWorkspace] optional bundled description preload failed", exc_info=True)
    try:
        from jiuwenswarm.common.auth.login_credentials import apply_login_credential_patch

        apply_login_credential_patch()
    except Exception:  # noqa: BLE001 — 补丁装不上不该拖垮启动
        logger.warning("[LoginCredential] 凭据钩子安装失败", exc_info=True)

    def _should_apply_sse_invoke_patch() -> bool:
        try:
            from jiuwenswarm.common.config import get_config

            mode = (
                get_config()
                .get("channels", {})
                .get("xiaoyi", {})
                .get("mode")
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[Worker] 读取 channels.xiaoyi.mode 失败，默认应用 SSE 兼容补丁: %s",
                exc,
            )
            return True
        return str(mode or "").strip() == "xiaoyi_claw"

    if _should_apply_sse_invoke_patch():
        apply_openai_sse_invoke_patch()


def _prepare_worker_configuration() -> None:
    """One writer completes workspace and model migrations before Runtime reads."""
    began = time.monotonic()
    if os.environ.get("JIUWENSWARM_RUNTIME_WORKSPACE_READY") != "1":
        from jiuwenswarm.common.utils import prepare_runtime_workspace

        prepare_runtime_workspace(cleanup_stale_descs=False, migrate_config=True)
    _log_preparation_stage("workspace", began)
    # Persist missing model/model-group/route IDs before any session or agent is
    # restored. Validation happens against the complete candidate before writing.
    began = time.monotonic()
    from jiuwenswarm.common.config import migrate_model_business_ids

    migrate_model_business_ids()
    from jiuwenswarm.common.model_migration import migrate_legacy_model_selections

    migrate_legacy_model_selections()
    _log_preparation_stage("model_migrations", began)


def _preload_sdk_execution_modules() -> None:
    """Import required SDK code only; do not construct clients or read Swarm config."""
    began = time.monotonic()
    from openjiuwen.core.runner import Runner  # noqa: F401
    from openjiuwen.harness.tools.shell import BashTool, PowerShellTool  # noqa: F401
    from openjiuwen.harness import DeepAgent  # noqa: F401
    from openjiuwen.harness.factory import create_deep_agent  # noqa: F401

    _log_preparation_stage("sdk_execution_imports", began)


def preload_shared_execution_code() -> None:
    """Prepare class/tool definitions after the joined configuration barrier.

    Both single-agent profiles use the Deep base. Code/Team-only code remains
    selected by the actual request. This constructs no session Rails or model
    clients and runs before Runner consumers start.
    """
    began = time.monotonic()
    from jiuwenswarm.server.runtime.agent_adapter.agent_adapters import (
        resolve_sdk_choice,
    )

    if resolve_sdk_choice() == "harness":
        import jiuwenswarm.server.runtime.agent_adapter.interface  # noqa: F401
        import jiuwenswarm.server.runtime.agent_adapter.interface_deep  # noqa: F401
    _log_preparation_stage("execution_code", began)


def preload_runtime_backend() -> None:
    """Join independent SDK code/config work before global patches or consumers."""
    if os.environ.get("JIUWENSWARM_RUNTIME_WORKSPACE_READY") != "1":
        # Description indexes may be consumed by SDK module imports. Repair
        # stale description layouts first, then overlap only code imports with
        # user-workspace/configuration writes. SDK imports do not read Swarm
        # config; execution Runtime imports remain after this joined barrier.
        from jiuwenswarm.common.utils import cleanup_stale_openjiuwen_descs

        cleanup_stale_openjiuwen_descs()
    # Configuration migrations use common's injected stores. Install every
    # server provider before either required-preparation branch can consume one.
    from jiuwenswarm.server.runtime.provider_setup import install as install_providers

    began = time.monotonic()
    install_providers()
    _log_preparation_stage("provider_setup", began)
    began = time.monotonic()
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="worker-required") as preparation:
        config = preparation.submit(_prepare_worker_configuration)
        sdk = preparation.submit(_preload_sdk_execution_modules)
        config.result()
        sdk.result()
    _log_preparation_stage("required_parallel_join", began)
    began = time.monotonic()
    _configure_openjiuwen_logging()
    from jiuwenswarm.common.background_logging import install_core_logging_queue, wrap_log_handlers

    install_core_logging_queue()
    wrap_log_handlers()
    _log_preparation_stage("logging", began)
    began = time.monotonic()
    _apply_runtime_entry_patches()
    _log_preparation_stage("runtime_patches", began)
    began = time.monotonic()
    import openjiuwen.core.runner  # noqa: F401
    _log_preparation_stage("runner_import", began)
    began = time.monotonic()
    import jiuwenswarm.extensions.manager  # noqa: F401
    import jiuwenswarm.extensions.registry  # noqa: F401
    _log_preparation_stage("extension_imports", began)
    began = time.monotonic()
    import jiuwenswarm.server.agent_ws_server  # noqa: F401
    _log_preparation_stage("server_import", began)
    # Core persistence is independent of agent mode and optional Rail imports.
    began = time.monotonic()
    import jiuwenswarm.runtime.checkpointer  # noqa: F401
    _log_preparation_stage("checkpointer_import", began)
