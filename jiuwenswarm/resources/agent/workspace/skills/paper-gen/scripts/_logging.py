# -*- coding: utf-8 -*-
"""_logging.py — paper-gen 统一日志配置。

替代裸 print()：带 timestamp + level + logger name + 颜色（终端）。
- 默认 stream handler → stderr（被 paper-gen forward 到主 stderr）
- 可选 file handler → output_dir/paper_gen.log（落地可 grep / tail）
- 格式: 2026-09-07 12:34:56 [INFO   ] paper_gen.main_flow: Stage 1/3 启动

调用：
    from _logging import setup_logging, get_logger
    setup_logging(log_file=Path("out/run1/paper_gen.log"), level=logging.INFO)
    log = get_logger(__name__)
    log.info("...")
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any


# 格式：ISO timestamp + level + logger name + message
# level 左对齐 7 字符（INFO  / DEBUG / WARN  / ERROR）
_LOG_FORMAT = "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# ANSI 颜色（终端可见；log 文件不用）
_LEVEL_COLORS = {
    "DEBUG":    "\033[36m",  # cyan
    "INFO":     "\033[32m",  # green
    "WARNING":  "\033[33m",  # yellow
    "ERROR":    "\033[31m",  # red
    "CRITICAL": "\033[35m",  # magenta
}
_RESET = "\033[0m"


class _ColorFormatter(logging.Formatter):
    """给 stream handler 加颜色，file handler 用标准 formatter。"""

    def format(self, record: logging.LogRecord) -> str:
        color = _LEVEL_COLORS.get(record.levelname, "")
        msg = super().format(record)
        if color and sys.stderr.isatty():
            # 只在 levelname 那里上色
            return msg.replace(record.levelname, f"{color}{record.levelname}{_RESET}", 1)
        return msg


# 防止 setup_logging() 多次调用导致重复 handler
_INITIALIZED = False


def _configure_utf8_console() -> None:
    """Make forwarded Chinese stage logs readable on Windows consoles.

    Child stages already receive ``PYTHONUTF8=1``.  The paper-gen parent must
    use the same encoding before it writes those decoded messages to stderr.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def setup_logging(
    *, level: int = logging.INFO, log_file: Path | None = None,
    root_name: str = "paper_gen",
) -> logging.Logger:
    """配置 paper-gen 全局日志。

    Args:
        level:    log level（默认 INFO；想看 LLM raw 用 DEBUG）
        log_file: 可选落盘路径（output_dir/paper_gen.log）；None 只走 stderr
        root_name: root logger 名（默认 "paper_gen"；子模块用 __name__ 自动加后缀）

    Returns:
        root logger 实例（方便 caller 立即 logger.info(...)）
    """
    global _INITIALIZED
    _configure_utf8_console()
    root = logging.getLogger(root_name)
    root.setLevel(level)
    # 不 propagate 到 root（避免被 jiuwenswarm 自己的 logger 二次输出）
    root.propagate = False

    # 清掉旧 handler（重复 setup 时不堆叠）
    for h in list(root.handlers):
        root.removeHandler(h)

    # 1) Stream handler → stderr
    stream_h = logging.StreamHandler(sys.stderr)
    stream_h.setLevel(level)
    stream_h.setFormatter(_ColorFormatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
    root.addHandler(stream_h)

    # 2) File handler → 落盘
    if log_file is not None:
        log_file = Path(log_file)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_h = logging.FileHandler(str(log_file), encoding="utf-8")
        file_h.setLevel(level)
        file_h.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
        root.addHandler(file_h)

    _INITIALIZED = True
    return root


def get_logger(name: str | None = None) -> logging.Logger:
    """取子 logger。name 传 __name__ 自动获得层级（如 paper_gen.main_flow）。"""
    if name is None:
        name = "paper_gen"
    if not _INITIALIZED:
        # 兜底：未 setup 时自动设一个最简的（避免静默丢日志）
        setup_logging()
    return logging.getLogger(name if name.startswith("paper_gen") else f"paper_gen.{name}")


# 常用快捷（写起来短）
def log_info(msg: str, *args: Any) -> None:
    get_logger().info(msg, *args)


def log_warning(msg: str, *args: Any) -> None:
    get_logger().warning(msg, *args)


def log_error(msg: str, *args: Any) -> None:
    get_logger().error(msg, *args)


def log_debug(msg: str, *args: Any) -> None:
    get_logger().debug(msg, *args)
