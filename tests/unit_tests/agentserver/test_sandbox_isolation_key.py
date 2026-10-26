"""``_sandbox_isolation_custom_id`` 必须把 box-server endpoint 折进 key.

box-server 端口是运行时分配且可变 (重启后旧端口被占 → bootstrap 换端口)。若
endpoint 不进 isolation key, registry 按 key 去重时新 card 会与钉着旧端口的旧
card 撞同一 key 被锁死 → 整进程 create_sandbox 全打旧死端口 (WinError 10061)。
端口进 key 后, 端口变化 → 新 key → 新 card 注册 → 旧 card 被取代而非复用。
"""

from __future__ import annotations

import re

from jiuwenswarm.server.runtime.agent_adapter import sysop_builder
from jiuwenswarm.server.runtime.agent_adapter.sysop_builder import (
    _sandbox_isolation_custom_id,
)

_EP_SUFFIX = re.compile(r"_ep_[0-9a-f]{8}$")


def _has_ep_suffix(key: str) -> bool:
    return bool(_EP_SUFFIX.search(key))


def test_endpoint_appended_to_normal_key(tmp_path):
    """普通分支: 有 endpoint → ``project_<digest>_ep_<8hex>``."""
    key = _sandbox_isolation_custom_id(
        str(tmp_path), sandbox_url="http://127.0.0.1:8000"
    )
    assert key.startswith("project_")
    assert _has_ep_suffix(key)


def test_no_endpoint_no_suffix(tmp_path):
    """普通分支: 无 endpoint → 纯 ``project_<digest>``, 无 _ep_ 后缀."""
    key = _sandbox_isolation_custom_id(str(tmp_path))
    assert key.startswith("project_")
    assert not _has_ep_suffix(key)


def test_default_when_no_project_dir_and_no_endpoint(monkeypatch):
    """resolved=None (cwd 不可用) + 无 endpoint → ``project_default`` (保持旧行为)."""
    monkeypatch.setattr(sysop_builder, "_resolve_project_dir", lambda _pd: None)
    key = _sandbox_isolation_custom_id(None)
    assert key == "project_default"


def test_default_with_endpoint_gets_suffix(monkeypatch):
    """resolved=None + 有 endpoint → ``project_default_ep_<8hex>``."""
    monkeypatch.setattr(sysop_builder, "_resolve_project_dir", lambda _pd: None)
    key = _sandbox_isolation_custom_id(None, sandbox_url="http://127.0.0.1:8000")
    assert key.startswith("project_default_ep_")


def test_different_endpoint_yields_different_key(tmp_path):
    """端口变 → key 变 (核心: 防止旧端口 card 被去重锁死)."""
    k1 = _sandbox_isolation_custom_id(str(tmp_path), sandbox_url="http://127.0.0.1:8000")
    k2 = _sandbox_isolation_custom_id(str(tmp_path), sandbox_url="http://127.0.0.1:8001")
    assert k1 != k2


def test_endpoint_normalization_stable(tmp_path):
    """trailing slash / 大小写归一 → 同一 endpoint 同一 key (不误分裂)."""
    base = "http://127.0.0.1:8000"
    k_plain = _sandbox_isolation_custom_id(str(tmp_path), sandbox_url=base)
    k_slash = _sandbox_isolation_custom_id(str(tmp_path), sandbox_url=base + "/")
    k_upper = _sandbox_isolation_custom_id(str(tmp_path), sandbox_url=base.upper())
    assert k_plain == k_slash == k_upper


def test_enterprise_endpoint_in_digest(monkeypatch, tmp_path):
    """enterprise 分支: endpoint 折进 digest; 端口变 → key 变."""
    monkeypatch.setattr(sysop_builder, "is_enterprise", lambda: True)
    shared = str(tmp_path)
    k1 = _sandbox_isolation_custom_id(
        str(tmp_path), shared_dir=shared, sandbox_url="http://127.0.0.1:8000"
    )
    k2 = _sandbox_isolation_custom_id(
        str(tmp_path), shared_dir=shared, sandbox_url="http://127.0.0.1:8001"
    )
    assert k1.startswith("workspace_project_")
    assert k1 != k2


def test_enterprise_endpoint_normalization_stable(monkeypatch, tmp_path):
    """enterprise 分支同样对 trailing slash / 大小写归一."""
    monkeypatch.setattr(sysop_builder, "is_enterprise", lambda: True)
    shared = str(tmp_path)
    base = "http://127.0.0.1:8000"
    k_plain = _sandbox_isolation_custom_id(str(tmp_path), shared_dir=shared, sandbox_url=base)
    k_slash = _sandbox_isolation_custom_id(str(tmp_path), shared_dir=shared, sandbox_url=base + "/")
    assert k_plain == k_slash
