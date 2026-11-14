# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""TTFT CPU 热点缓存：产物目录解析、A2X SSL 上下文、多模态 api_key 读取。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from jiuwenswarm.agents.harness.common.tools import multimodal_config
from jiuwenswarm.agents.harness.team.a2x.client import transport
from jiuwenswarm.server.runtime.agent_adapter import interface_deep
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)


def test_artifact_dir_resolves_absolute_paths_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    interface_deep._RESOLVED_ARTIFACT_DIRS.clear()
    calls = {"n": 0}
    real_resolve = Path.resolve

    def _counting(self, *args, **kwargs):
        calls["n"] += 1
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", _counting)
    expected = str(real_resolve(tmp_path))
    assert interface_deep._resolve_artifact_dir(str(tmp_path)) == expected
    assert interface_deep._resolve_artifact_dir(str(tmp_path)) == expected
    assert calls["n"] == 1
    interface_deep._RESOLVED_ARTIFACT_DIRS.clear()


def test_artifact_dir_does_not_cache_relative_paths() -> None:
    interface_deep._RESOLVED_ARTIFACT_DIRS.clear()
    assert interface_deep._resolve_artifact_dir("rel/dir") == str(Path("rel/dir").resolve())
    assert interface_deep._RESOLVED_ARTIFACT_DIRS == {}


def test_deepresearch_output_dir_matches_resolved_paths(tmp_path: Path) -> None:
    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    adapter._project_dir = None
    adapter._workspace_dir = str(tmp_path)
    assert adapter._deepresearch_artifact_output_dir() == str(tmp_path.resolve() / "projects")
    assert adapter._deepresearch_artifact_output_dir(str(tmp_path)) == str(tmp_path.resolve())


@pytest.mark.asyncio
async def test_a2x_transports_share_ssl_context(monkeypatch: pytest.MonkeyPatch) -> None:
    transport._SSL_CONTEXTS.clear()
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    calls = {"n": 0}
    real_create = transport.httpx.create_ssl_context

    def _counting(*args, **kwargs):
        calls["n"] += 1
        return real_create(*args, **kwargs)

    monkeypatch.setattr(transport.httpx, "create_ssl_context", _counting)
    first = transport.AsyncHTTPTransport("http://127.0.0.1:9")
    second = transport.AsyncHTTPTransport("http://127.0.0.1:9")
    try:
        assert calls["n"] == 1
        assert transport._shared_ssl_context() is transport._shared_ssl_context()
    finally:
        await first.aclose()
        await second.aclose()
        transport._SSL_CONTEXTS.clear()


def test_a2x_sync_transport_uses_shared_ssl_context(monkeypatch: pytest.MonkeyPatch) -> None:
    transport._SSL_CONTEXTS.clear()
    seen = {}
    real_client = transport.httpx.Client

    def _client(*args, **kwargs):
        seen["verify"] = kwargs.get("verify")
        return real_client(*args, **kwargs)

    monkeypatch.setattr(transport.httpx, "Client", _client)
    sync = transport.HTTPTransport("http://127.0.0.1:9")
    try:
        assert seen["verify"] is transport._shared_ssl_context()
    finally:
        sync.close()
        transport._SSL_CONTEXTS.clear()


def _ca_count(context) -> int:
    return context.cert_store_stats()["x509_ca"]


def test_a2x_ssl_context_reloads_rotated_cert_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import certifi

    transport._SSL_CONTEXTS.clear()
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    bundle_text = Path(certifi.where()).read_text(encoding="ascii")
    bundle = tmp_path / "ca.pem"
    bundle.write_text(bundle_text, encoding="ascii")
    monkeypatch.setenv("SSL_CERT_FILE", str(bundle))
    before = transport._shared_ssl_context()
    assert transport._shared_ssl_context() is before
    end_marker = "-----END CERTIFICATE-----"
    bundle.write_text(bundle_text[: bundle_text.index(end_marker) + len(end_marker)] + "\n", encoding="ascii")
    stat = bundle.stat()
    os.utime(bundle, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    after = transport._shared_ssl_context()
    assert after is not before
    assert _ca_count(before) > 1 and _ca_count(after) == 1
    transport._SSL_CONTEXTS.clear()


def test_a2x_ssl_context_rebuilt_after_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    transport._SSL_CONTEXTS.clear()
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    now = [1000.0]
    monkeypatch.setattr(transport.time, "monotonic", lambda: now[0])
    first = transport._shared_ssl_context()
    now[0] += transport._SSL_CONTEXT_TTL_S - 1
    assert transport._shared_ssl_context() is first
    now[0] += 2
    assert transport._shared_ssl_context() is not first
    transport._SSL_CONTEXTS.clear()


def test_a2x_ssl_context_follows_cert_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    transport._SSL_CONTEXTS.clear()
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    default_context = transport._shared_ssl_context()
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path))
    assert transport._shared_ssl_context() is not default_context
    transport._SSL_CONTEXTS.clear()


def test_raw_model_api_key_reads_merged_config_without_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    from jiuwenswarm.common import config as config_module

    merged = {"models": {"vision": {"model_config": {"api_key": "${VISION_KEY}"}}}}
    monkeypatch.setattr(config_module, "get_merged_config_dict", lambda: merged)

    def _no_copy():
        raise AssertionError("get_config_raw deep-copies the whole tree")

    monkeypatch.setattr(config_module, "get_config_raw", _no_copy)
    assert multimodal_config._raw_model_api_key("vision") == "${VISION_KEY}"
