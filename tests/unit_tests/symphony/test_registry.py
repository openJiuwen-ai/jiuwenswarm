import pytest

from jiuwenswarm.extensions.registry import ExtensionRegistry


class _CallbackFramework:
    @staticmethod
    def register_sync(*args, **kwargs):
        return None

    async def trigger(self, *args, **kwargs):
        return None


def setup_function():
    ExtensionRegistry.reset_instance()


def teardown_function():
    ExtensionRegistry.reset_instance()


def test_extension_registry_registers_rpc_handlers():
    registry = ExtensionRegistry.create_instance(
        callback_framework=_CallbackFramework(),
        config={},
        logger=object(),
    )

    async def handler(params, *, request=None):
        return {"params": params, "request_id": getattr(request, "request_id", "")}

    registry.register_rpc_handler("symphony.plan", handler)

    assert registry.get_rpc_handler("symphony.plan") is handler
    assert registry.get_rpc_handler("symphony.missing") is None
    assert registry.list_rpc_methods() == ["symphony.plan"]


def test_extension_registry_rejects_invalid_rpc_method():
    registry = ExtensionRegistry.create_instance(
        callback_framework=_CallbackFramework(),
        config={},
        logger=object(),
    )

    with pytest.raises(ValueError):
        registry.register_rpc_handler("", lambda params: params)
