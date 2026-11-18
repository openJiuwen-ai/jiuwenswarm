# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""gateway 日志级别热更回归。

update_gateway_log_levels 只经标准库基类 + 文件名映射分类 handler，
独立安装（gateway 自有装配）与共同安装（复用母仓 jiuwenswarm 树装配）
两种形态都必须正确；payload 转换沿用 e2a 字段契约。
"""

from __future__ import annotations

import logging
from logging.handlers import QueueHandler
from pathlib import Path

import pytest

import agent_gateway.common.logging_setup as ls
from agent_gateway.common.logging_setup import (
    apply_logging_config_payload,
    logging_config_row_to_dict,
    update_gateway_log_levels,
)


@pytest.fixture(autouse=True)
def _predictable_levels(monkeypatch, tmp_path):
    """级别解析固定为 INFO 基线，避免共享 config.yaml / 环境变量干扰。"""
    monkeypatch.setattr(ls, "_load_logging_config_from_yaml", lambda: {})
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    for name in ("gateway", "jiuwenswarm"):
        logger = logging.getLogger(name)
        monkeypatch.setattr(logger, "handlers", [])
        monkeypatch.setattr(logger, "level", logging.INFO)
        monkeypatch.setattr(logger, "propagate", False)


def _file_handler(tmp_path: Path, filename: str) -> logging.FileHandler:
    handler = logging.FileHandler(tmp_path / filename)
    handler.setLevel(logging.INFO)
    return handler


def test_update_levels_targets_each_component_file(tmp_path):
    root = logging.getLogger("gateway")
    gateway_log = _file_handler(tmp_path, "gateway.log")
    channel_log = _file_handler(tmp_path, "channel.log")
    agent_log = _file_handler(tmp_path, "agent_server.log")
    full_log = _file_handler(tmp_path, "full.log")
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    for handler in (gateway_log, channel_log, agent_log, full_log, console):
        root.addHandler(handler)

    update_gateway_log_levels(
        console_level="DEBUG", gateway="WARNING", channel="ERROR",
        agent_server="CRITICAL", full="WARNING",
    )

    assert gateway_log.level == logging.WARNING
    assert channel_log.level == logging.ERROR
    assert agent_log.level == logging.CRITICAL
    assert full_log.level == logging.WARNING
    assert console.level == logging.DEBUG
    # 根 logger 取含 console 在内的最低级别，保证组件与控制台日志都能到达各自 handler。
    assert root.level == min(
        logging.WARNING, logging.ERROR, logging.CRITICAL, logging.WARNING, logging.DEBUG
    )


def test_update_levels_shared_install_targets_mother_tree(tmp_path):
    """共同安装形态：gateway 树复用母仓装配，更新须落到母仓树。"""
    gateway_root = logging.getLogger("gateway")
    mother_root = logging.getLogger("jiuwenswarm")
    mother_handler = _file_handler(tmp_path, "gateway.log")
    mother_root.addHandler(mother_handler)

    update_gateway_log_levels(gateway="ERROR")

    assert mother_handler.level == logging.ERROR
    # 根 logger 按组件最低级别放行（channel/agent_server/full 仍为 INFO 基线）。
    assert mother_root.level == logging.INFO
    assert not gateway_root.handlers


def test_update_levels_descends_into_queued_handlers(tmp_path):
    root = logging.getLogger("gateway")
    target = _file_handler(tmp_path, "channel.log")
    queued = QueueHandler(target)
    # 母仓 _QueuedHandler 形态：fan-out 包装暴露 .handlers 列表。
    queued.handlers = [target]
    queued.setLevel(logging.INFO)
    root.addHandler(queued)

    update_gateway_log_levels(channel="ERROR")

    assert target.level == logging.ERROR
    assert queued.level == logging.ERROR


def test_apply_payload_delete_restores_defaults():
    root = logging.getLogger("gateway")
    handler = logging.StreamHandler()
    handler.setLevel(logging.CRITICAL)
    root.addHandler(handler)

    apply_logging_config_payload(None)

    assert handler.level == logging.INFO
    apply_logging_config_payload({"op": "delete"})
    assert handler.level == logging.INFO


def test_apply_payload_builds_kwargs():
    root = logging.getLogger("gateway")
    handler = logging.StreamHandler()
    handler.setLevel(logging.INFO)
    root.addHandler(handler)

    apply_logging_config_payload({"console_level": "WARNING", "full": "ERROR"})

    assert handler.level == logging.WARNING


def test_console_only_downgrade_reaches_console_handler(tmp_path):
    """console 单独下调必须在根闸放行（回归：原 min 不含 console 时被 isEnabledFor 拦截）。"""
    import io

    root = logging.getLogger("gateway")
    stream = logging.StreamHandler(io.StringIO())
    stream.setLevel(logging.INFO)
    # 文件 handler 必须带真实文件名（gateway.log），才会走文件名映射而非 console 分支。
    gateway_log = _file_handler(tmp_path, "gateway.log")
    root.addHandler(stream)
    root.addHandler(gateway_log)

    update_gateway_log_levels(console_level="DEBUG")

    assert root.level == logging.DEBUG
    assert stream.level == logging.DEBUG
    # 文件/组件阈值不受 console 下调影响，仍按自身级别过滤。
    assert gateway_log.level == logging.INFO


def test_row_to_dict_supports_dict_and_object_rows():
    from types import SimpleNamespace

    row = logging_config_row_to_dict({"level": "DEBUG", "gateway": "WARNING"})
    assert row == {
        "level": "DEBUG", "console_level": None, "gateway": "WARNING",
        "channel": None, "agent_server": None, "full": None,
    }
    obj = logging_config_row_to_dict(SimpleNamespace(level="WARN"))
    assert obj["level"] == "WARN"
    assert obj["console_level"] is None
