"""Web 进程的文件日志目录必须落在 paths 契约的 ``logs/agent`` 桶。

旧实现把 ``ws-dev.log`` 写到 ``<数据根>/profile/agent/.logs``；该目录在 profile
布局收敛后由 ``migrate_profile_data_root_layout`` 并入 ``logs/agent`` 并剪除，
于是 ``_setup_logger`` 打开文件时可能拿到 ``FileNotFoundError``，web 文件日志
静默缺失。回归点：

1. ``_get_logs_dir()`` 必须等于 ``paths.get_agent_logs_dir()``（不再自行拼接）；
2. ``_setup_logger()`` 把 ``ws-dev.log`` 写进目标桶；
3. 目录在 mkdir 与 open 之间被剪除时重试一次即可成功；
4. 重试仍失败时向上抛出，由调用方降级为控制台日志。
"""

from __future__ import annotations

import logging

import pytest

from jiuwenswarm.channels.web import app_web
from jiuwenswarm.common.paths import get_agent_logs_dir, get_user_workspace_dir


def _drop_added_handlers(logger: logging.Logger, before: list[logging.Handler]) -> None:
    """_setup_logger 会给模块 logger 挂文件 handler，测试结束前摘掉。"""
    for handler in logger.handlers[:]:
        if handler in before:
            continue
        handler.close()
        logger.removeHandler(handler)


def test_get_logs_dir_follows_agent_logs_contract() -> None:
    logs_dir = app_web._get_logs_dir()

    assert logs_dir == get_agent_logs_dir()
    assert logs_dir == get_user_workspace_dir() / "logs" / "agent"
    # 旧布局（profile/agent/.logs）已废弃，不能再被当作 web 日志目录。
    assert logs_dir != app_web._get_agent_root_dir() / ".logs"


def test_setup_logger_writes_ws_dev_log_into_bucket(tmp_path) -> None:
    logs_root = tmp_path / "logs" / "agent"
    logger = logging.getLogger(app_web.__name__)
    before = list(logger.handlers)

    try:
        result = app_web._setup_logger(logs_root, "INFO")
        assert result is logger
        logger.info("web log probe")
        for handler in logger.handlers:
            handler.flush()
        written = (logs_root / "ws-dev.log").read_text(encoding="utf-8")
        assert written.strip().endswith("web log probe")
    finally:
        _drop_added_handlers(logger, before)


def test_setup_logger_retries_once_when_directory_is_pruned(tmp_path, monkeypatch) -> None:
    """并发布局归位把目录搬走/剪除时，重建后重试一次即可成功。"""

    logs_root = tmp_path / "logs" / "agent"
    real_file_handler = logging.FileHandler
    calls = {"count": 0}

    class _PrunedOnceHandler(real_file_handler):
        def __init__(self, filename, *args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                raise FileNotFoundError(2, "No such file or directory", str(filename))
            super().__init__(filename, *args, **kwargs)

    monkeypatch.setattr(logging, "FileHandler", _PrunedOnceHandler)
    logger = logging.getLogger(app_web.__name__)
    before = list(logger.handlers)

    try:
        app_web._setup_logger(logs_root, "INFO")
        assert calls["count"] == 2, "首次 ENOENT 后必须重建目录并重试一次"
        assert (logs_root / "ws-dev.log").is_file()
    finally:
        _drop_added_handlers(logger, before)


def test_setup_logger_propagates_when_retry_also_fails(tmp_path, monkeypatch) -> None:
    """两次都失败时不吞异常，交给调用方降级并告警。"""

    def _always_missing(filename, *args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", str(filename))

    monkeypatch.setattr(logging, "FileHandler", _always_missing)
    logger = logging.getLogger(app_web.__name__)
    before = list(logger.handlers)

    try:
        with pytest.raises(FileNotFoundError):
            app_web._setup_logger(tmp_path / "logs" / "agent", "INFO")
    finally:
        _drop_added_handlers(logger, before)
