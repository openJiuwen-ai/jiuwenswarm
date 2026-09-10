# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""setup_logger 集成：text/json/dual 路由 + filter 挂载。"""
import logging
import os
from pathlib import Path

from jiuwenswarm.common import utils
from jiuwenswarm.common.utils import (
    IdentityFieldFilter,
    UserVisibleTagFilter,
    setup_logger,
    update_log_levels,
)


def _file_handler_names():
    return [
        os.path.basename(h.baseFilename)
        for h in utils._iter_log_output_handlers()
        if hasattr(h, "baseFilename")
    ]


def test_text_mode_creates_log_files(monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_LOG_FORMAT", "text")
    setup_logger()
    names = _file_handler_names()
    assert "gateway.log" in names
    assert not any(n.endswith(".json") for n in names)


def test_json_mode_creates_json_files(monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_LOG_FORMAT", "json")
    setup_logger()
    names = _file_handler_names()
    assert "gateway.json" in names
    assert "full.json" in names


def test_dual_mode_creates_both(monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_LOG_FORMAT", "dual")
    setup_logger()
    names = _file_handler_names()
    assert "gateway.log" in names and "gateway.json" in names


def test_queue_handler_has_identity_and_privacy_filters(monkeypatch):
    """Identity / 脱敏必须挂在 QueueHandler（emit 线程），不能挂在 listener 文件 handler。"""
    from jiuwenswarm.infrastructure.log_masking import SensitiveDataFilter

    monkeypatch.setenv("JIUWENSWARM_LOG_FORMAT", "text")
    root = setup_logger()
    assert len(root.handlers) == 1
    queue_handler = root.handlers[0]
    assert type(queue_handler).__name__ == "QueueHandler"
    assert any(isinstance(f, IdentityFieldFilter) for f in queue_handler.filters)
    assert any(isinstance(f, SensitiveDataFilter) for f in queue_handler.filters)
    for h in utils._iter_log_output_handlers():
        if hasattr(h, "baseFilename"):
            assert not any(isinstance(f, IdentityFieldFilter) for f in h.filters)
            assert not any(isinstance(f, SensitiveDataFilter) for f in h.filters)


def test_text_file_handlers_have_user_visible_filter(monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_LOG_FORMAT", "text")
    setup_logger()
    file_handlers = [
        h for h in utils._iter_log_output_handlers() if hasattr(h, "baseFilename")
    ]
    assert file_handlers, "期望至少有一个文件 handler"
    assert any(isinstance(f, UserVisibleTagFilter) for f in file_handlers[0].filters)


def test_root_uses_queue_handler(monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_LOG_FORMAT", "text")
    root = setup_logger()
    assert len(root.handlers) == 1
    assert type(root.handlers[0]).__name__ == "QueueHandler"
    assert utils._log_listener is not None


def test_update_log_levels_targets_listener_handlers(monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_LOG_FORMAT", "text")
    setup_logger()
    update_log_levels(agent_server="DEBUG", console_level="WARNING")
    agent_handlers = [
        h
        for h in utils._iter_log_output_handlers()
        if hasattr(h, "baseFilename")
        and Path(h.baseFilename).name == "agent_server.log"
    ]
    assert agent_handlers
    assert agent_handlers[0].level == logging.DEBUG
    console_handlers = [
        h
        for h in utils._iter_log_output_handlers()
        if isinstance(h, logging.StreamHandler) and not hasattr(h, "baseFilename")
    ]
    assert console_handlers
    assert console_handlers[0].level == logging.WARNING


def test_end_to_end_text_log_has_identity_and_user_tag(monkeypatch, tmp_path):
    """端到端：通过已配置的 logger 记录一条 critical 消息，gateway.log 应含
    user_id=null（IdentityFieldFilter 无 provider）与 [USER]（UserVisibleTagFilter）。
    覆盖 filter→formatter→handler→文件写入 的完整链路，捕获格式串拼写错误等接线 bug。
    """
    monkeypatch.setenv("JIUWENSWARM_LOG_FORMAT", "text")
    monkeypatch.setenv("LOG_ROOT_PATH", str(tmp_path))
    setup_logger()
    gw_logger = logging.getLogger("jiuwenswarm.gateway.routing")
    gw_logger.info("hello-end-to-end", extra={"user_visible": "critical"})
    utils.flush_queued_logs()
    gw_handler = next(
        (
            h
            for h in utils._iter_log_output_handlers()
            if hasattr(h, "baseFilename") and h.baseFilename.endswith("gateway.log")
        ),
        None,
    )
    assert gw_handler is not None, "gateway.log handler not found"
    content = Path(gw_handler.baseFilename).read_text(encoding="utf-8")
    assert "hello-end-to-end" in content
    assert "[USER]" in content
    assert "user_id=null" in content
