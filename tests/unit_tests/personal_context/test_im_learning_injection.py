"""AS-10：PersonalContextHostAPI 注入 IM 学习源（共享 Connector 视图）。

远端宿主（host_api）在构造 Core 门面时注入 as_im_learning_source 包装的
LazySharedRegistryView：构造期零副作用，首次学习抓取经 ensure_connector
进入 im_hosting 进程级共享注册表，与托管代回链复用同一批 ChannelPlugin。
"""

from __future__ import annotations

import pytest

import jiuwenswarm.server.im.im_hosting.connectors as connectors_module
from jiuwenswarm.server.im.im_connector.learning_source import ConnectorLearningSource
from jiuwenswarm.server.im.im_hosting.connectors import LazySharedRegistryView
from jiuwenswarm.server.personal_context.host_api import PersonalContextHostAPI


def test_host_injects_shared_learning_source(tmp_path):
    """宿主构造的 Core 门面带学习源，且学习源解析共享注册表视图。"""
    host = PersonalContextHostAPI(home=tmp_path)
    source = host._personal_context._im_learning_source  # pylint: disable=protected-access
    assert isinstance(source, ConnectorLearningSource)
    assert isinstance(
        source._registry, LazySharedRegistryView  # pylint: disable=protected-access
    )


def test_host_construction_does_not_build_shared_registry(tmp_path, monkeypatch):
    """宿主构造零副作用：不构建共享注册表（延迟到首次学习抓取）。"""
    monkeypatch.setattr(connectors_module, "_shared_registry", None)
    PersonalContextHostAPI(home=tmp_path)
    assert connectors_module._shared_registry is None


async def test_im_learning_end_to_end_persists_to_im_context_db(tmp_path, monkeypatch):
    """端到端验收：configure 启用 im_learning 后，学习源经共享注册表拉取
    Connector 消息并落盘 im_context.db（AS-10 装配链路完整闭环）。"""
    import asyncio
    import json
    import sqlite3

    import jiuwenswarm.server.personal_context.host_api as host_module
    from jiuwenswarm.server.im.im_connector.connectors.welink import (
        MockWelinkCli,
        WeLinkConnector,
    )

    mock = MockWelinkCli()
    mock.enqueue_history(
        json.dumps(
            {
                "respData": {
                    "chatInfo": [
                        {
                            "sender": "alice",
                            "content": "hello",
                            "serverSendTime": 2000,
                            "msgId": "m2",
                        },
                        {
                            "sender": "bob",
                            "content": "world",
                            "serverSendTime": 1000,
                            "msgId": "m1",
                        },
                    ]
                }
            }
        )
    )

    def fake_build(channel_id: str):
        if channel_id == "welink":
            return WeLinkConnector(mock, self_account="alice")
        return None

    monkeypatch.setattr(connectors_module, "_shared_registry", None)
    monkeypatch.setattr(connectors_module, "build_connector", fake_build)
    # 钉死模型列表与全局 embedding，保证用例不依赖宿主机配置
    monkeypatch.setattr(host_module, "get_default_models", lambda: [])
    monkeypatch.setattr(
        host_module, "_global_embedding_values", lambda: (None, None, None)
    )

    host = PersonalContextHostAPI(home=tmp_path / "pc")
    await host.start()
    try:
        await host.configure(
            {
                "collection_enabled": True,
                "agent_use_enabled": False,
                "strategy_profile": "rules",
                "fetch_services": [],
                "im_learning": {
                    "enabled": True,
                    "targets": [
                        {"channel_id": "welink", "kind": "group", "external_id": "g1"}
                    ],
                    "fetch_interval_seconds": 60,
                },
            }
        )
        core = host._personal_context  # pylint: disable=protected-access
        assert await core.run_im_learning_now() is True
        status = await core.get_im_learning_status()
        assert status["enabled"] is True

        db_path = tmp_path / "pc" / "im" / "im_context.db"
        for _ in range(100):
            if db_path.is_file():
                conn = sqlite3.connect(db_path)
                try:
                    count = conn.execute("SELECT COUNT(*) FROM im_messages").fetchone()[0]
                finally:
                    conn.close()
                if count >= 2:
                    break
            await asyncio.sleep(0.1)
        else:
            pytest.fail(f"im_context.db 未落盘学习消息: exists={db_path.is_file()}")
    finally:
        await host.stop()
