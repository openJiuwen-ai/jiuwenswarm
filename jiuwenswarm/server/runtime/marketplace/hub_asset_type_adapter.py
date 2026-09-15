# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Mapping between stable Jiuwenswarm kinds and evolving Hub type strings."""

from __future__ import annotations

from dataclasses import dataclass

from jiuwenswarm.server.runtime.marketplace.hub_asset_port import HubAssetKind


@dataclass(frozen=True, slots=True)
class HubAssetTypeContract:
    kind: HubAssetKind
    hub_plugin_type: str
    local_package_type: str
    accepted_hub_types: frozenset[str]

    def accepts(self, plugin_type: str, asset_type: str = "") -> bool:
        return resolve_hub_asset_kind(plugin_type, asset_type) == self.kind


_CONTRACTS: dict[HubAssetKind, HubAssetTypeContract] = {
    "agent_template": HubAssetTypeContract(
        kind="agent_template",
        hub_plugin_type="agent-template",
        local_package_type="agent_template",
        accepted_hub_types=frozenset({"agent-template", "agent_template"}),
    ),
    "plugin": HubAssetTypeContract(
        kind="plugin",
        hub_plugin_type="agent-plugin",
        local_package_type="plugin",
        accepted_hub_types=frozenset({"agent-plugin", "plugin"}),
    ),
    "mcp": HubAssetTypeContract(
        kind="mcp",
        hub_plugin_type="agent-mcp",
        local_package_type="mcp",
        accepted_hub_types=frozenset({"agent-mcp"}),
    ),
}

_KIND_BY_PLUGIN_TYPE = {
    alias: contract.kind
    for contract in _CONTRACTS.values()
    for alias in contract.accepted_hub_types
}
_KIND_BY_ASSET_TYPE: dict[str, HubAssetKind] = {
    "agent-template": "agent_template",
    "agent_template": "agent_template",
    "agent-plugin": "plugin",
    "agent-mcp": "mcp",
}


class HubAssetTypeConflictError(ValueError):
    pass


def resolve_hub_asset_kind(
    plugin_type: str, asset_type: str = ""
) -> HubAssetKind | None:
    normalized_plugin = str(plugin_type or "").strip().lower()
    normalized_asset = str(asset_type or "").strip().lower()
    plugin_kind = _KIND_BY_PLUGIN_TYPE.get(normalized_plugin)
    asset_kind = _KIND_BY_ASSET_TYPE.get(normalized_asset)
    if plugin_kind is not None and asset_kind is not None and plugin_kind != asset_kind:
        raise HubAssetTypeConflictError(
            f"conflicting Hub types: plugin_type={plugin_type!r}, asset_type={asset_type!r}"
        )
    if normalized_plugin:
        return plugin_kind
    return asset_kind


def get_hub_asset_type_contract(kind: HubAssetKind) -> HubAssetTypeContract:
    try:
        return _CONTRACTS[kind]
    except KeyError as exc:
        raise ValueError(f"unsupported Hub asset kind: {kind!r}") from exc
