"""进程级共享 Connector 注册表测试（AS-10：托管链与学习链复用实例）。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Optional

import jiuwenswarm.server.im.im_hosting.connectors as connectors_module
from jiuwenswarm.server.im.im_hosting.connectors import (
    ensure_connector,
    get_shared_registry,
)
from jiuwenswarm.server.im.im_hosting.service import HostingPollService


def _fake_plugin(channel_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        meta=SimpleNamespace(id=channel_id), name=f"fake-{channel_id}"
    )


def test_shared_registry_is_singleton(monkeypatch):
    monkeypatch.setattr(connectors_module, "_shared_registry", None)
    first = get_shared_registry()
    assert get_shared_registry() is first


def test_ensure_connector_lazy_builds_and_registers(monkeypatch):
    """共享注册表初始为空时，CLI 后装的通道经 ensure_connector 懒注册。"""
    monkeypatch.setattr(connectors_module, "_shared_registry", None)
    built: list[str] = []
    available: set[str] = set()

    def fake_build(channel_id: str) -> Optional[object]:
        built.append(channel_id)
        return _fake_plugin(channel_id) if channel_id in available else None

    monkeypatch.setattr(connectors_module, "build_connector", fake_build)

    # 初始无任何 CLI：共享注册表为空
    assert ensure_connector("welink") is None
    assert get_shared_registry().get("welink") is None

    # CLI 后装：ensure_connector 懒构建并注册进共享注册表
    available.add("welink")
    first = ensure_connector("welink")
    assert first is not None
    assert built[-1] == "welink"
    count_after_build = built.count("welink")
    # 后续调用复用共享实例，不再重建
    again = ensure_connector("welink")
    assert again is first
    ensure_connector("welink")
    assert built.count("welink") == count_after_build
    # 学习链（personal_context）拿到的是同一批实例
    assert get_shared_registry().get("welink") is first


def test_ensure_connector_returns_none_without_cli(monkeypatch):
    monkeypatch.setattr(connectors_module, "_shared_registry", None)
    monkeypatch.setattr(connectors_module, "build_connector", lambda channel_id: None)
    assert ensure_connector("welink") is None
    assert "welink" not in get_shared_registry()


def test_hosting_service_plugin_uses_shared_registry(monkeypatch):
    """HostingPollService 默认（未注入 connectors）走共享注册表。"""
    monkeypatch.setattr(connectors_module, "_shared_registry", None)
    sentinel = _fake_plugin("welink")
    monkeypatch.setattr(
        "jiuwenswarm.server.im.im_hosting.service.ensure_connector",
        lambda channel_id: sentinel if channel_id == "welink" else None,
    )
    service = HostingPollService()
    assert service._plugin("welink") is sentinel
    assert service._plugin("feishu") is None

    # 显式注入 connectors 时优先注入表（测试通道，行为不变）
    injected = _fake_plugin("feishu")
    service_injected = HostingPollService(connectors={"feishu": injected})  # type: ignore[arg-type]
    assert service_injected._plugin("feishu") is injected


def test_lazy_shared_registry_view_defers_and_shares(monkeypatch):
    """学习链视图：构造不触发构建，首次访问经 ensure_connector 进共享注册表。"""
    monkeypatch.setattr(connectors_module, "_shared_registry", None)
    built: list[str] = []
    available: set[str] = {"welink"}

    def fake_build(channel_id: str) -> Optional[object]:
        built.append(channel_id)
        return _fake_plugin(channel_id) if channel_id in available else None

    monkeypatch.setattr(connectors_module, "build_connector", fake_build)

    view = connectors_module.LazySharedRegistryView()
    # 构造与查询未命中前，不构建共享注册表
    assert connectors_module._shared_registry is None

    plugin = view.require("welink")
    assert plugin is not None
    # 与托管链共享同一实例：注册进共享注册表，且后续 ensure_connector 复用
    assert get_shared_registry().get("welink") is plugin
    assert ensure_connector("welink") is plugin
    assert built.count("welink") == 1

    # 未配置 CLI 的通道：get 返回 None，require 抛 KeyError（对齐 ConnectorRegistry 语义）
    assert view.get("dingtalk") is None
    try:
        view.require("dingtalk")
    except KeyError:
        pass
    else:
        raise AssertionError("require should raise KeyError for unregistered channel")
