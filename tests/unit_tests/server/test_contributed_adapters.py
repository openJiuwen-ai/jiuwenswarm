# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""A package can contribute a gateway adapter, and replace a built-in one.

The stub package under ``contributed_adapters/echo_session`` is loaded through
``ExtensionLoader``, the loader a deployment uses, so what is under test is the
path a real package takes rather than a direct call.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import ClassVar

import pytest

from jiuwenswarm.common.e2a.agent_compat import e2a_to_agent_request
from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
from jiuwenswarm.common.schema.message import ReqMethod, parse_req_method
from jiuwenswarm.extensions import overrides
from jiuwenswarm.extensions.loader import ExtensionLoader
from jiuwenswarm.server.runtime.gateway_adapter import AdapterRegistry, SessionAdapter
from jiuwenswarm.server.runtime.gateway_adapter.base import GatewayAdapter
from jiuwenswarm.server.runtime.gateway_adapter.contributed import (
    ADAPTER_KIND,
    contributed_adapters,
    mount_contributed_adapters,
    register_gateway_adapter,
    unregister_gateway_adapter,
)

STUB_ROOT = Path(__file__).parent / "contributed_adapters" / "echo_session"
LIST = ReqMethod.SESSION_LIST.value
PIN = ReqMethod.SESSION_PIN.value
PING = "echo.ping"


@pytest.fixture(autouse=True)
def no_contributed_adapters():
    """Leave the registry as it was found, whatever a test registered."""
    yield
    for adapter in contributed_adapters():
        unregister_gateway_adapter(adapter)


@pytest.fixture
def permit_everything(monkeypatch):
    """Stand in for an operator who listed every element in configuration."""
    monkeypatch.setattr(
        overrides,
        "allowed_overrides",
        lambda: frozenset({f"{ADAPTER_KIND}.{method.value}" for method in ReqMethod}
                          | set(overrides.PROTECTED)),
    )


def _load_stub() -> None:
    asyncio.run(ExtensionLoader(registry=object()).load_extension(STUB_ROOT))


def _mounted() -> tuple[AdapterRegistry, SessionAdapter]:
    """The host's mount: its own adapters first, contributed ones after."""
    registry = AdapterRegistry()
    builtin = SessionAdapter()
    registry.register(builtin)
    mount_contributed_adapters(registry)
    return registry, builtin


class _Adapter(GatewayAdapter):
    """A contributed adapter declared by a test rather than by a package."""

    def __init__(self, methods, replaces=(), source="pkg"):
        self.methods = frozenset(methods)
        self.replaces = frozenset(replaces)
        self.source = source


# ── the two sides of the acceptance criterion ──


def test_the_built_in_adapter_answers_until_a_package_is_loaded():
    assert contributed_adapters() == ()
    registry, builtin = _mounted()
    assert registry.get(LIST) is builtin


def test_a_loaded_package_answers_instead_of_the_built_in_adapter(permit_everything):
    _load_stub()

    assert sorted(adapter.source for adapter in contributed_adapters()) == [
        "echo-session 0.0.1",
        "echo-session 0.0.1",
    ]
    registry, builtin = _mounted()
    # Mounted after the built-ins, so the permitted replacement wins its method.
    answering = registry.get(LIST)
    assert answering is not builtin
    assert answering.source == "echo-session 0.0.1"
    assert "loaded_extension" in type(answering).__module__


def test_a_contributed_adapter_answers_a_method_no_built_in_answers(permit_everything):
    assert _mounted()[0].get(PING) is None
    _load_stub()
    assert _mounted()[0].get(PING).source == "echo-session 0.0.1"


def test_only_registered_methods_cross_the_e2a_boundary():
    with pytest.raises(ValueError):
        parse_req_method(PING)

    adapter = _Adapter({PING})
    register_gateway_adapter(adapter)
    env = e2a_from_agent_fields(request_id="r", req_method=PING)
    request = e2a_to_agent_request(env)
    assert request.req_method.value == PING
    assert _mounted()[0].get(request.req_method.value) is adapter
    assert parse_req_method(LIST) is ReqMethod.SESSION_LIST

    unregister_gateway_adapter(adapter)
    with pytest.raises(ValueError):
        e2a_to_agent_request(env)


def test_a_replacement_leaves_the_other_methods_of_that_adapter_alone(permit_everything):
    _load_stub()
    registry, builtin = _mounted()
    assert len(builtin.methods) > 1
    for method in builtin.methods - {LIST}:
        assert registry.get(method) is builtin, method


# ── collision and precedence ──


def test_a_collision_without_a_declaration_is_refused(permit_everything):
    with pytest.raises(ValueError) as excinfo:
        register_gateway_adapter(_Adapter({LIST}))
    assert "replaces" in str(excinfo.value)
    assert LIST in str(excinfo.value)
    assert contributed_adapters() == ()


def test_a_declaration_the_operator_did_not_permit_is_refused(monkeypatch):
    monkeypatch.setattr(overrides, "allowed_overrides", frozenset)
    with pytest.raises(ValueError) as excinfo:
        register_gateway_adapter(_Adapter({LIST}, replaces={LIST}))
    assert overrides.CONFIG_KEY in str(excinfo.value)
    assert f"{ADAPTER_KIND}.{LIST}" in str(excinfo.value)
    assert contributed_adapters() == ()


def test_one_permitted_method_does_not_carry_a_second_one(monkeypatch):
    monkeypatch.setattr(
        overrides, "allowed_overrides", lambda: frozenset({f"{ADAPTER_KIND}.{LIST}"})
    )
    with pytest.raises(ValueError) as excinfo:
        register_gateway_adapter(_Adapter({LIST, PIN}, replaces={LIST, PIN}))
    assert PIN in str(excinfo.value)
    assert contributed_adapters() == ()


def test_a_declaration_naming_a_method_the_adapter_does_not_answer_is_refused(permit_everything):
    with pytest.raises(ValueError) as excinfo:
        register_gateway_adapter(_Adapter({PING}, replaces={LIST}))
    assert LIST in str(excinfo.value)


def test_replacing_a_method_the_host_does_not_answer_is_refused(permit_everything):
    with pytest.raises(ValueError) as excinfo:
        register_gateway_adapter(_Adapter({PING}, replaces={PING}))
    assert "nothing to replace" in str(excinfo.value)


def test_an_adapter_declaring_no_method_is_refused(permit_everything):
    with pytest.raises(ValueError):
        register_gateway_adapter(_Adapter(set()))


def test_a_second_package_cannot_claim_a_contributed_method(permit_everything):
    register_gateway_adapter(_Adapter({PING}, source="first"))
    with pytest.raises(ValueError) as excinfo:
        register_gateway_adapter(_Adapter({PING}, source="second"))
    assert "first" in str(excinfo.value)
    assert _mounted()[0].get(PING).source == "first"


def test_a_withdrawn_declaration_leaves_the_next_mount_built_in(permit_everything):
    _load_stub()
    for adapter in contributed_adapters():
        unregister_gateway_adapter(adapter)
    assert contributed_adapters() == ()
    registry, builtin = _mounted()
    assert registry.get(LIST) is builtin
    assert registry.get(PING) is None


# ── the elements no operator can open up ──


def test_no_adapter_is_protected():
    # PROTECTED is for an element that decides whether a package's own code may
    # act. An adapter answers a gateway RPC and gates nothing a loaded package
    # does not already have, so no entry here names this kind.
    assert not [key for key in overrides.PROTECTED if key.startswith(f"{ADAPTER_KIND}.")]


class _ProtectedAdapter(GatewayAdapter):
    methods: ClassVar[frozenset[str]] = frozenset({LIST})
    replaces: ClassVar[frozenset[str]] = frozenset({LIST})
    source: ClassVar[str] = "pkg"


def test_a_protected_adapter_method_would_be_refused_whatever_configuration_says(
    monkeypatch, permit_everything
):
    # The kind carries no entry today. This drives the path an entry would take,
    # so adding one is enough to make it refuse.
    key = f"{ADAPTER_KIND}.{LIST}"
    monkeypatch.setitem(overrides.PROTECTED, key, "it is the gate under test")
    with pytest.raises(ValueError) as excinfo:
        register_gateway_adapter(_ProtectedAdapter())
    assert key in str(excinfo.value)
    assert not overrides.override_permitted(ADAPTER_KIND, LIST)
