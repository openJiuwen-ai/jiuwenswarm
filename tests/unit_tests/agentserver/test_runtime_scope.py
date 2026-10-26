# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from dataclasses import dataclass

from jiuwenswarm.server.runtime.runtime_scope import RuntimeScopeKey


@dataclass
class _FakeRequest:
    agent_id: str | None = None
    service_id: str | None = None
    session_id: str | None = None


class _FakeAdapter:
    def __init__(self, service_id: str | None = None, agent_id: str | None = None):
        self._env_service_id = service_id
        self._env_agent_id = agent_id


def test_runtime_scope_key_defaults():
    scope = RuntimeScopeKey()
    assert scope.service_id == "default"
    assert scope.agent_id == "default"
    assert scope.workspace_key == "default"
    assert scope.session_id == ""
    assert scope.tenant() == ("default", "default", "default")


def test_runtime_scope_key_from_ids():
    scope = RuntimeScopeKey.from_ids("svc-a", "agent-b", "sess-1")
    assert scope.service_id == "svc-a"
    assert scope.agent_id == "agent-b"
    assert scope.session_id == "sess-1"
    assert scope.session_key() == ("svc-a", "agent-b", "default", "sess-1")


def test_runtime_scope_key_with_session():
    base = RuntimeScopeKey.from_ids("svc", "agent")
    scoped = base.with_session("chat-42")
    assert scoped.session_id == "chat-42"
    assert scoped.service_id == "svc"
    assert scoped.agent_id == "agent"


def test_runtime_scope_key_from_adapter():
    adapter = _FakeAdapter(service_id="office", agent_id="assistant")
    scope = RuntimeScopeKey.from_adapter(adapter)
    assert scope.tenant() == ("office", "assistant", "default")


def test_runtime_scope_key_from_request():
    import sys
    from types import SimpleNamespace

    fake_mod = SimpleNamespace()

    class FakePool:
        @staticmethod
        def extract_ids(request):
            return (request.agent_id, request.service_id, "default")

    fake_mod.TenantAgentPool = FakePool
    module_key = "jiuwenswarm.server.runtime.tenant_agent_pool"
    previous = sys.modules.get(module_key)
    sys.modules[module_key] = fake_mod
    try:
        req = _FakeRequest(agent_id="a1", service_id="s1")
        scope = RuntimeScopeKey.from_request(req)
        assert scope.tenant() == ("s1", "a1", "default")
    finally:
        if previous is None:
            sys.modules.pop(module_key, None)
        else:
            sys.modules[module_key] = previous
