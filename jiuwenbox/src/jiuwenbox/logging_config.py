# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Shared logging configuration for jiuwenbox."""

from __future__ import annotations

import logging

LOG_FORMAT = "[%(asctime)s] %(levelname)s %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
UVICORN_LOGGER_NAMES = ("uvicorn", "uvicorn.error", "uvicorn.access")


def _timestamp_formatter() -> logging.Formatter:
    return logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)


def _set_handler_formatters(logger: logging.Logger, formatter: logging.Formatter) -> None:
    for handler in logger.handlers:
        handler.setFormatter(formatter)


def patch_uvicorn_logging() -> None:
    """Patch uvicorn's default LOGGING_CONFIG and rename ``uvicorn.error`` logger.

    Uvicorn uses the logger name ``uvicorn.error`` for normal server lifecycle
    messages (not errors). Rename it to ``uvicorn`` for clearer log output, and
    apply jiuwenbox's timestamped format to the default formatter.

    No-op when uvicorn is not installed (e.g. the sandbox runner runs on a bare
    system Python without the project deps); the import is swallowed so the
    runner can still start and log via :func:`configure_logging`'s basicConfig.
    """
    # uvicorn 仅 box-server (``uvicorn jiuwenbox.server.app:app``) 需要并安装;
    # 沙箱 runner (``jiuwenbox.supervisor.win_exec``) 是最小子进程, 运行在裸
    # 系统 Python (dev 下 JIUWENBOX_RUNNER_PYTHON 探测 C:\Python3* 候选, 无
    # 项目依赖), 既不跑 uvicorn 也不需要其 LOGGING_CONFIG. 此函数被
    # ``configure_logging`` 在导入期无条件调用, 若硬 import uvicorn, runner
    # 启动即 ModuleNotFoundError 退出 → 沙箱 phase=error → bash/read_file 工具
    # 连不上 per-sandbox 网关报 WinError 10061. 缺包时跳过, 不影响 box-server
    try:
        from uvicorn.config import LOGGING_CONFIG
    except ImportError:
        return

    LOGGING_CONFIG["formatters"]["default"]["fmt"] = LOG_FORMAT
    LOGGING_CONFIG["formatters"]["default"]["datefmt"] = LOG_DATE_FORMAT
    logging.getLogger("uvicorn.error").name = "uvicorn"


def configure_logging(level: int = logging.INFO) -> None:
    """Configure process logging with jiuwenbox's default timestamped format."""
    logging.basicConfig(level=level, format=LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
    formatter = _timestamp_formatter()
    _set_handler_formatters(logging.getLogger(), formatter)
    for logger_name in UVICORN_LOGGER_NAMES:
        _set_handler_formatters(logging.getLogger(logger_name), formatter)
    patch_uvicorn_logging()