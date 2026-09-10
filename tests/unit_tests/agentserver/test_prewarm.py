"""Unit tests for jiuwenswarm.server.runtime.prewarm.

覆盖：
- WarmupModelClient.invoke/stream 返回值正确性 + generate_image/speech/video raise
- _build_warmup_config_base 对 defaults/default/react 三条路径的 client_provider 覆盖
  （含 model_client_config 缺失时回写修复验证）
- warmup_import_and_checkpointer (阶段1/2) 失败时的降级行为（不抛异常、仅告警）
- warmup_deep_agent_query (阶段3) 各阶段失败时的降级行为（不抛异常、仅告警）
- _cleanup_prewarm_agent 在 agent=None / adapter 无 close 方法时的安全跳过
- run_startup_warmup 向后兼容（依次调 phase12 + phase3）
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jiuwenswarm.server.runtime.prewarm import (
    WarmupModelClient,
    _build_warmup_config_base,
    _cleanup_prewarm_agent,
    run_startup_warmup,
    warmup_import_and_checkpointer,
    warmup_deep_agent_query,
)


# ---------------------------------------------------------------------------
# WarmupModelClient
# ---------------------------------------------------------------------------


class TestWarmupModelClient:
    def test_invoke_returns_warmup_ok_message(self) -> None:
        client = WarmupModelClient(model_config={}, model_client_config={})
        msg = asyncio.run(client.invoke([]))
        assert msg.content == "warmup ok"

    def test_stream_yields_single_warmup_ok_chunk(self) -> None:
        client = WarmupModelClient(model_config={}, model_client_config={})

        async def _collect() -> list[Any]:
            chunks = []
            async for chunk in client.stream([]):
                chunks.append(chunk)
            return chunks

        chunks = asyncio.run(_collect())
        assert len(chunks) == 1
        assert chunks[0].content == "warmup ok"

    @pytest.mark.parametrize("method_name", ["generate_image", "generate_speech", "generate_video"])
    def test_multimodal_methods_raise_not_implemented(self, method_name: str) -> None:
        client = WarmupModelClient(model_config={}, model_client_config={})
        method = getattr(client, method_name)
        with pytest.raises(NotImplementedError):
            asyncio.run(method())

    def test_constructor_skips_validation_and_stores_refs(self) -> None:
        mc = {"model_name": "glm-5.2"}
        mcc = {"client_provider": "warmup", "api_base": "stub"}
        client = WarmupModelClient(model_config=mc, model_client_config=mcc)
        assert client.model_config is mc
        assert client.model_client_config is mcc


# ---------------------------------------------------------------------------
# _build_warmup_config_base
# ---------------------------------------------------------------------------


def _entry(provider: str = "OpenAI", with_mcc: bool = True) -> dict[str, Any]:
    entry: dict[str, Any] = {"is_default": True}
    if with_mcc:
        entry["model_client_config"] = {"client_provider": provider, "api_key": "stub"}
    return entry


class TestBuildWarmupConfigBase:
    def test_defaults_list_client_provider_overwritten(self) -> None:
        cfg = {"models": {"defaults": [_entry("OpenAI"), _entry("DashScope")]}}
        with patch("jiuwenswarm.server.runtime.prewarm.get_config", return_value=cfg):
            out = _build_warmup_config_base()
        providers = [e["model_client_config"]["client_provider"] for e in out["models"]["defaults"]]
        assert providers == ["warmup", "warmup"]
        # 原始 config 不被修改（deepcopy）
        assert cfg["models"]["defaults"][0]["model_client_config"]["client_provider"] == "OpenAI"

    def test_legacy_default_single_entry_overwritten(self) -> None:
        cfg = {"models": {"default": {"model_client_config": {"client_provider": "OpenAI"}}}}
        with patch("jiuwenswarm.server.runtime.prewarm.get_config", return_value=cfg):
            out = _build_warmup_config_base()
        assert out["models"]["default"]["model_client_config"]["client_provider"] == "warmup"

    def test_react_section_client_provider_overwritten(self) -> None:
        cfg = {"react": {"model_client_config": {"client_provider": "OpenAI"}}}
        with patch("jiuwenswarm.server.runtime.prewarm.get_config", return_value=cfg):
            out = _build_warmup_config_base()
        assert out["react"]["model_client_config"]["client_provider"] == "warmup"

    def test_defaults_entry_without_model_client_config_is_backfilled(self) -> None:
        """model_client_config 缺失时应回写 warmup，避免 mock 不生效。"""
        cfg = {"models": {"defaults": [_entry(with_mcc=False)]}}
        with patch("jiuwenswarm.server.runtime.prewarm.get_config", return_value=cfg):
            out = _build_warmup_config_base()
        assert out["models"]["defaults"][0]["model_client_config"]["client_provider"] == "warmup"

    def test_legacy_default_without_model_client_config_skipped_safely(self) -> None:
        cfg = {"models": {"default": {}}}
        with patch("jiuwenswarm.server.runtime.prewarm.get_config", return_value=cfg):
            out = _build_warmup_config_base()
        # 不应抛异常，default 段保持原样（无 model_client_config）
        assert out["models"]["default"] == {}

    def test_react_without_model_client_config_skipped_safely(self) -> None:
        cfg = {"react": {}}
        with patch("jiuwenswarm.server.runtime.prewarm.get_config", return_value=cfg):
            out = _build_warmup_config_base()
        assert out["react"] == {}

    def test_empty_config_does_not_raise(self) -> None:
        with patch("jiuwenswarm.server.runtime.prewarm.get_config", return_value={}):
            out = _build_warmup_config_base()
        assert "models" in out  # setdefault 保证存在


# ---------------------------------------------------------------------------
# _cleanup_prewarm_agent
# ---------------------------------------------------------------------------


class TestCleanupPrewarmAgent:
    @pytest.mark.asyncio
    async def test_none_agent_returns_safely(self) -> None:
        await _cleanup_prewarm_agent(None)  # 不应抛异常

    @pytest.mark.asyncio
    async def test_agent_without_adapter_returns_safely(self) -> None:
        agent = MagicMock()
        agent._adapter = None
        await _cleanup_prewarm_agent(agent)

    @pytest.mark.asyncio
    async def test_adapter_without_close_method_skipped_safely(self) -> None:
        agent = MagicMock()
        adapter = MagicMock()
        adapter.close = None  # close 不可调用
        agent._adapter = adapter
        await _cleanup_prewarm_agent(agent)  # 不应抛异常

    @pytest.mark.asyncio
    async def test_adapter_close_invoked_when_callable(self) -> None:
        agent = MagicMock()
        adapter = MagicMock()
        adapter.close = AsyncMock()
        agent._adapter = adapter
        await _cleanup_prewarm_agent(agent)
        adapter.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_close_exception_does_not_propagate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        agent = MagicMock()
        adapter = MagicMock()
        adapter.close = AsyncMock(side_effect=RuntimeError("boom"))
        agent._adapter = adapter
        calls: list[str] = []
        monkeypatch.setattr(
            "jiuwenswarm.server.runtime.prewarm.logger.warning",
            lambda *a, **k: calls.append(a[0]),
        )
        await _cleanup_prewarm_agent(agent)
        assert any("cleanup prewarm agent failed" in c for c in calls)


# ---------------------------------------------------------------------------
# warmup_import_and_checkpointer (阶段1/2) 降级行为
# ---------------------------------------------------------------------------


def _patch_phase12(monkeypatch: pytest.MonkeyPatch, *, import_exc=None, cp_exc=None) -> None:
    warm_iface = AsyncMock()
    if import_exc:
        warm_iface.side_effect = import_exc
    monkeypatch.setattr(
        "jiuwenswarm.server.agent_ws_server._warm_interface_deep_module", warm_iface, raising=False
    )
    cp = AsyncMock()
    if cp_exc:
        cp.side_effect = cp_exc
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep.ensure_persistent_checkpointer",
        cp,
        raising=False,
    )


@pytest.mark.asyncio
async def test_phase12_import_failure_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_phase12(monkeypatch, import_exc=RuntimeError("import boom"))
    await warmup_import_and_checkpointer()  # 不抛即通过


@pytest.mark.asyncio
async def test_phase12_checkpointer_failure_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_phase12(monkeypatch, cp_exc=RuntimeError("cp boom"))
    await warmup_import_and_checkpointer()


@pytest.mark.asyncio
async def test_phase12_ok_completes(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_phase12(monkeypatch)
    await warmup_import_and_checkpointer()  # 全程不抛即通过


# ---------------------------------------------------------------------------
# warmup_deep_agent_query (阶段3) 降级行为
# ---------------------------------------------------------------------------


def _patch_phase3(monkeypatch: pytest.MonkeyPatch, *, jiuwen_exc=None, create_exc=None,
                  process_exc=None) -> MagicMock:
    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)
    fake_agent = MagicMock()
    fake_agent.create_instance = AsyncMock()
    if create_exc:
        fake_agent.create_instance.side_effect = create_exc
    fake_agent.process_message = AsyncMock()
    if process_exc:
        fake_agent.process_message.side_effect = process_exc
    fake_agent._adapter = MagicMock()
    fake_agent._adapter.close = None

    fake_jws = MagicMock()
    if jiuwen_exc:
        fake_jws.side_effect = jiuwen_exc
    else:
        fake_jws.return_value = fake_agent
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface.JiuWenSwarm", fake_jws, raising=False
    )
    import jiuwenswarm.server.runtime.prewarm as prewarm_mod
    monkeypatch.setattr(prewarm_mod, "get_config", lambda: {}, raising=False)
    return fake_agent


@pytest.mark.asyncio
async def test_phase3_skipped_on_enterprise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    # 不应再走到 JiuWenSwarm 构造；若误导入/调用会在本地环境炸 import。
    await warmup_deep_agent_query(timeout_s=1.0)

@pytest.mark.asyncio
async def test_phase3_jiuwen_constructor_failure_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_phase3(monkeypatch, jiuwen_exc=RuntimeError("ctor boom"))
    await warmup_deep_agent_query(timeout_s=1.0)  # 不抛即通过


@pytest.mark.asyncio
async def test_phase3_create_instance_failure_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_phase3(monkeypatch, create_exc=RuntimeError("create boom"))
    await warmup_deep_agent_query(timeout_s=1.0)


@pytest.mark.asyncio
async def test_phase3_query_failure_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_phase3(monkeypatch, process_exc=RuntimeError("query boom"))
    await warmup_deep_agent_query(timeout_s=1.0)


@pytest.mark.asyncio
async def test_phase3_query_timeout_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _slow(*_a, **_kw):
        await asyncio.sleep(10)

    fake_agent = _patch_phase3(monkeypatch)
    fake_agent.process_message = _slow
    await warmup_deep_agent_query(timeout_s=0.05)  # 不抛即通过


@pytest.mark.asyncio
async def test_phase3_ok_completes(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_agent = _patch_phase3(monkeypatch)
    await warmup_deep_agent_query(timeout_s=1.0)
    fake_agent.create_instance.assert_awaited_once()
    fake_agent.process_message.assert_awaited_once()


# ---------------------------------------------------------------------------
# run_startup_warmup 向后兼容（调拆分后的两函数）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_startup_warmup_backcompat_calls_both_phases(monkeypatch: pytest.MonkeyPatch) -> None:
    """run_startup_warmup 应依次调用 phase12 + phase3。"""
    import jiuwenswarm.server.runtime.prewarm as prewarm_mod
    phase12 = AsyncMock()
    phase3 = AsyncMock()
    monkeypatch.setattr(prewarm_mod, "warmup_import_and_checkpointer", phase12, raising=False)
    monkeypatch.setattr(prewarm_mod, "warmup_deep_agent_query", phase3, raising=False)
    await run_startup_warmup(timeout_s=1.0)
    phase12.assert_awaited_once()
    phase3.assert_awaited_once()
