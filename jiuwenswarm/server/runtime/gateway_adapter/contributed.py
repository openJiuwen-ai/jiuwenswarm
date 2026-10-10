# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Register contributed Gateway methods with collision and consent checks."""

from jiuwenswarm.common.schema.message import ReqMethod, _contributed_req_methods
from jiuwenswarm.common.utils import logger
from jiuwenswarm.extensions import overrides
from jiuwenswarm.server.runtime.gateway_adapter.base import AdapterRegistry, GatewayAdapter

ADAPTER_KIND = "adapters"

_HOST_METHODS: frozenset[str] = frozenset(method.value for method in ReqMethod)

_CONTRIBUTED: dict[str, GatewayAdapter] = {}


def register_gateway_adapter(adapter: GatewayAdapter) -> None:
    """Register methods, requiring consent before replacing host methods."""
    methods = frozenset(adapter.methods)
    if not methods:
        raise ValueError(
            f"{type(adapter).__name__} declares no method. A contributed adapter is "
            f"reached by method name, so it must declare at least one."
        )
    source = adapter.source or "an unnamed package"
    declared = frozenset(adapter.replaces)

    taken = sorted(methods & frozenset(_CONTRIBUTED))
    if taken:
        rival = _CONTRIBUTED[taken[0]]
        raise ValueError(
            f"method {taken[0]!r} is already answered by an adapter contributed by "
            f"{rival.source or 'an unnamed package'}"
        )

    unanswered = sorted(declared - methods)
    if unanswered:
        raise ValueError(
            f"{source} declares that its adapter replaces {', '.join(unanswered)}, which "
            f"it does not answer. An adapter is reached by method name, so a replacement "
            f"must declare the methods it answers."
        )
    absent = sorted(declared - _HOST_METHODS)
    if absent:
        raise ValueError(
            f"{source} declares that its adapter replaces {', '.join(absent)}, which the "
            f"host does not answer. There is nothing to replace."
        )

    for method in sorted(methods & _HOST_METHODS):
        key = overrides.override_key(ADAPTER_KIND, method)
        if method not in declared:
            permitted = (
                f" {overrides.CONFIG_KEY} already permits it. The declaration is the "
                f"missing half."
                if overrides.override_permitted(ADAPTER_KIND, method)
                else ""
            )
            raise ValueError(
                f"{source} contributes an adapter answering {method!r}, which the host "
                f"already answers, without declaring that it replaces it. Add {method} to "
                f"the adapter's replaces.{permitted}"
            )
        overrides.require_override_permitted(ADAPTER_KIND, method, source=source)
        logger.warning(
            "[adapters] %s answers %s instead of the host; %s permits it",
            source,
            key,
            overrides.CONFIG_KEY,
        )

    for method in sorted(methods - _HOST_METHODS):
        logger.info("[adapters] %s contributes method %s", source, method)

    for method in methods:
        _CONTRIBUTED[method] = adapter
    _contributed_req_methods.update(methods - _HOST_METHODS)


def unregister_gateway_adapter(adapter: GatewayAdapter) -> None:
    """Withdraw the adapter's methods from future mounts."""
    for method in [key for key, value in _CONTRIBUTED.items() if value is adapter]:
        del _CONTRIBUTED[method]
        _contributed_req_methods.discard(method)
    logger.info(
        "[adapters] %s no longer contributes an adapter",
        adapter.source or "an unnamed package",
    )


def contributed_adapters() -> tuple[GatewayAdapter, ...]:
    """Contributed adapters, in the order their packages declared them."""
    return tuple(dict.fromkeys(_CONTRIBUTED.values()))


def mount_contributed_adapters(registry: AdapterRegistry) -> None:
    """Mount contributed adapters after built-ins; late additions need a restart."""
    for adapter in contributed_adapters():
        registry.register(adapter)
