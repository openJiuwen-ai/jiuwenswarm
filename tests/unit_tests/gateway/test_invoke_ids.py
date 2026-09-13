# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""EE invoke_ids：默认 service_id / agent_id 拼接。"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
import types
from pathlib import Path

_EXT_DIR = (
    Path(__file__).resolve().parents[3]
    / "packages"
    / "jiuwenclaw-ee"
    / "gateway"
    / "extensions"
    / "runtime_management_extension"
)
_PKG = "_ee_runtime_management_ext"
if _PKG not in sys.modules:
    _pkg = types.ModuleType(_PKG)
    _pkg.__path__ = [str(_EXT_DIR)]
    _pkg.__package__ = _PKG
    sys.modules[_PKG] = _pkg


def _load(name: str):
    full = f"{_PKG}.{name}"
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(full, _EXT_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    module.__package__ = _PKG
    sys.modules[full] = module
    spec.loader.exec_module(module)
    return module


_invoke_mod = _load("invoke_ids")
_default_invoke_ids = _invoke_mod._default_invoke_ids
_default_workspace_key = _invoke_mod._default_workspace_key
coalesce_invoke_ids = _invoke_mod.coalesce_invoke_ids
_md5_invoke_id = _invoke_mod._md5_invoke_id
apply_invoke_ids_to_envelope = _invoke_mod.apply_invoke_ids_to_envelope


def test_default_invoke_ids_concatenates_group_bot_user() -> None:
    svc, ag = _default_invoke_ids("grp", "bot", "user")
    assert svc == "grpbot"
    assert ag == "grpbotuser"


def test_default_workspace_key_concatenates_raw_bot() -> None:
    assert _default_workspace_key("grp", "bot-1", "user") == "grpbot-1user"


def test_coalesce_and_hash_invoke_ids() -> None:
    svc, ag, ws = coalesce_invoke_ids(group_id="g", bot_id="b", user_id="u")
    assert (svc, ag, ws) == ("gb", "gbu", "gbu")
    hs, ha, hw = _md5_invoke_id(svc), _md5_invoke_id(ag), _md5_invoke_id(ws)
    assert hs == hashlib.md5(b"gb").hexdigest()
    assert ha == hashlib.md5(b"gbu").hexdigest()
    assert hw == hashlib.md5(b"gbu").hexdigest()
    # 始终再哈希（不透传）
    assert (
        _md5_invoke_id(hs),
        _md5_invoke_id(ha),
        _md5_invoke_id(hw),
    ) != (hs, ha, hw)


def test_coalesce_keeps_literal_default_when_explicit() -> None:
    """顶层显式 agent_id=default 视为已配置（不再当占位覆盖）。"""
    svc, ag, ws = coalesce_invoke_ids(
        group_id="__none__",
        bot_id="bot-1",
        user_id="user1",
        service_id=None,
        agent_id="default",
        workspace_key="default",
    )
    assert svc == "__none__bot-1"
    assert ag == "default"
    assert ws == "default"


def test_coalesce_fills_only_empty_ids() -> None:
    svc, ag, ws = coalesce_invoke_ids(
        group_id="g",
        bot_id="b",
        user_id="u",
        service_id="custom-svc",
        agent_id="custom-ag",
        workspace_key="custom-wk",
    )
    assert (svc, ag, ws) == ("custom-svc", "custom-ag", "custom-wk")


def test_apply_invoke_ids_to_envelope_sets_workspace() -> None:
    from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
    from jiuwenswarm.common.request_identity import apply_routing_metadata
    from jiuwenswarm.common.schema.message import ReqMethod

    env = e2a_from_agent_fields(
        request_id="req-1",
        channel_id="web",
        session_id="sess-1",
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "hi"},
        is_stream=False,
        user_id="user",
        metadata=apply_routing_metadata(
            {},
            {"user_id": "user", "group_id": "grp", "bot_id": "bot"},
        ),
    )
    apply_invoke_ids_to_envelope(env)
    assert env.service_id == hashlib.md5(b"grpbot").hexdigest()
    assert env.agent_id == hashlib.md5(b"grpbotuser").hexdigest()
    assert env.workspace_key == hashlib.md5(b"grpbotuser").hexdigest()


def test_apply_invoke_ids_keeps_explicit_envelope_agent_id() -> None:
    from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
    from jiuwenswarm.common.request_identity import apply_routing_metadata
    from jiuwenswarm.common.schema.message import ReqMethod

    env = e2a_from_agent_fields(
        request_id="req-1",
        channel_id="web",
        session_id="sess-1",
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "hi"},
        is_stream=False,
        user_id="user1",
        metadata=apply_routing_metadata(
            {},
            {"user_id": "user1", "group_id": "__none__", "bot_id": "bot-1"},
        ),
    )
    env.agent_id = "custom-ag"
    env.workspace_key = ""
    apply_invoke_ids_to_envelope(env)
    assert env.service_id == hashlib.md5(b"__none__bot-1").hexdigest()
    assert env.agent_id == hashlib.md5(b"custom-ag").hexdigest()
    assert env.workspace_key == hashlib.md5(b"__none__bot-1user1").hexdigest()


def test_installed_skill_resolve_uses_invoke_ids_extension(monkeypatch) -> None:
    import sys

    from jiuwenswarm.agents.harness.common.installed_skill import resolve_final_tenant_ids

    pkg = types.ModuleType("openjiuwen_runtime_management_extension")
    pkg.__path__ = []
    mod = types.ModuleType("openjiuwen_runtime_management_extension.invoke_ids")

    def _stub_default_invoke_ids(group_id: str, bot_id: str, user_id: str) -> tuple[str, str]:
        return f"svc-{group_id}", f"ag-{group_id}{bot_id}{user_id}"

    mod._default_invoke_ids = _stub_default_invoke_ids
    monkeypatch.setitem(sys.modules, "openjiuwen_runtime_management_extension", pkg)
    monkeypatch.setitem(sys.modules, "openjiuwen_runtime_management_extension.invoke_ids", mod)

    svc, ag = resolve_final_tenant_ids(group_id="g", bot_id="b", user_id="u")
    assert svc == hashlib.md5(b"svc-g").hexdigest()
    assert ag == hashlib.md5(b"ag-gbu").hexdigest()
