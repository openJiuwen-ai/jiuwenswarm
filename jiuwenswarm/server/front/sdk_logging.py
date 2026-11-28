# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Configure SDK logging at its first control-plane use, before any SDK logger."""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)
_lock = threading.Lock()
_configured = False


def ensure_configured() -> None:
    global _configured
    with _lock:
        if not _configured:
            _configure_openjiuwen_logging()
            _configured = True


def _configure_openjiuwen_logging() -> None:
    """Bind OpenJiuwen logging to the writable workspace before Front serves.

    Front handles control-plane requests (``history.get`` among them) whose
    todo snapshot lazily imports OpenJiuwen. Without an explicit configuration
    OpenJiuwen falls back to its relative default ``./logs/``, resolved against
    the process CWD. AgentOS sandboxes run with CWD=/ on a read-only root, so
    logger creation raises and the history stream aborts before its done frame.

    Both branches are guarded: a missing or malformed ``logging.yaml`` (or an
    unwritable path inside it) must not crash Front startup, so the workspace
    fallback config is tried next and only if that also fails does the function
    give up with a warning, leaving Front's default logging intact.

    The fallback config keeps OpenJiuwen at ``INFO``; do not silence its
    loggers here, or control-plane failures (``history.get`` included) would
    disappear from Front's logs.
    """
    from jiuwenswarm.common.utils import get_config_dir, get_logs_dir

    logging_yaml = get_config_dir() / "logging.yaml"
    if logging_yaml.exists():
        try:
            from openjiuwen.core.common.logging.log_config import configure_log

            configure_log(str(logging_yaml))
            return
        except Exception:  # noqa: BLE001
            logger.warning(
                "[AgentServer] openjiuwen logging.yaml config failed; "
                "trying workspace fallback",
                exc_info=True,
            )

    try:
        from openjiuwen.core.common.logging.log_config import configure_log_config

        configure_log_config({
            "backend": "default",
            "level": "INFO",
            "log_path": f"{get_logs_dir() / 'core'}/",
            "log_file": "run/jiuwen.log",
            "output": ["file"],
            "structured_output_format": "json",
            "interface_log_file": "interface/jiuwen_interface.log",
            "prompt_builder_interface_log_file": "interface/jiuwen_prompt_builder_interface.log",
            "performance_log_file": "performance/jiuwen_performance.log",
        })
    except Exception:  # noqa: BLE001
        logger.warning(
            "[AgentServer] openjiuwen log config failed; Front keeps default logging",
            exc_info=True,
        )

