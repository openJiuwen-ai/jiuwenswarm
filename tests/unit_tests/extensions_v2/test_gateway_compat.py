"""Only verified missing upstream modules may defer a mixed contract case."""

from types import ModuleType

import pytest

from tests.unit_tests.extensions_v2 import gateway_compat


def test_known_missing_gateway_module_is_deferred(monkeypatch):
    name = "agent_gateway.channel_manager.web.webui_handlers"

    def missing_module(requested):
        assert requested == name
        raise ModuleNotFoundError(name=name)

    monkeypatch.setattr(gateway_compat, "import_module", missing_module)
    with pytest.raises(pytest.skip.Exception, match="e95c5c2"):
        gateway_compat.require_gateway_module(name)


@pytest.mark.parametrize("requested,missing", [
    ("agent_gateway.extensions_v2.webui.unknown_typo", "agent_gateway.extensions_v2.webui.unknown_typo"),
    ("agent_gateway.channel_manager.web.webui_handlers", "agent_gateway.unrelated_dependency"),
    ("agent_gateway.channel_manager.web.webui_handlers", "non_gateway_dependency"),
    ("agent_gateway.channel_manager.web.webui_handlers", "agent_gateway"),
])
def test_unknown_or_transitive_import_failure_is_not_deferred(monkeypatch, requested, missing):
    failure = ModuleNotFoundError(name=missing)

    def missing_module(name):
        assert name == requested
        raise failure

    monkeypatch.setattr(gateway_compat, "import_module", missing_module)
    with pytest.raises(ModuleNotFoundError) as caught:
        gateway_compat.require_gateway_module(requested)
    assert caught.value is failure


def test_available_gateway_module_is_returned(monkeypatch):
    module = ModuleType("agent_gateway.extensions_v2.webui.models")
    monkeypatch.setattr(gateway_compat, "import_module", lambda name: module)
    assert gateway_compat.require_gateway_module(module.__name__) is module
