"""Product-level capability-card projection for REA setup."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any


CARD_MANIFEST = Path(__file__).resolve().parent.parent / "resources" / "rea" / "capability-cards.json"
COMPONENT_MANIFEST = Path(__file__).resolve().parent.parent / "resources" / "rea" / "component-manifest.json"
_READY_STATES = {"available", "configured", "installed", "ready", "reused"}
_NEEDS_ACTION_STATES = {
    "cached",
    "discovered",
    "downloaded_not_installed",
    "needs_action",
    "partial",
}
_MANUAL_INSTALL_MODES = {"manual", "manual_license", "system"}
_COMPONENT_FUNCTIONAL_TOOLS: dict[str, list[str]] = {
    "adb": ["inspect_adb_client"],
    "apktool": ["inspect_apktool_client"],
    "chrome": ["list_browser_targets"],
    "cutter": ["list_cutter_sessions"],
    "frida": ["list_frida_devices"],
}
_RIZIN_PERSISTENT_DEBUG_TOOLS = {
    "start_rizin_debug_session",
    "rizin_debug_command",
    "rizin_debug_session_status",
    "close_rizin_debug_session",
}


@lru_cache(maxsize=1)
def _load_manifest() -> dict[str, Any]:
    payload = json.loads(CARD_MANIFEST.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {"cards": []}


def _load_cards() -> list[dict[str, Any]]:
    cards = _load_manifest().get("cards")
    return [item for item in cards or [] if isinstance(item, dict)]


@lru_cache(maxsize=1)
def _prerequisite_components() -> frozenset[str]:
    """Return components that are dependencies rather than card capabilities."""

    try:
        payload = json.loads(COMPONENT_MANIFEST.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return frozenset()
    requirement_components = {
        "java-full-jdk": "java",
        "java-full-jdk-21": "java",
    }
    prerequisites: set[str] = set()
    for component in payload.get("components") or []:
        if not isinstance(component, dict):
            continue
        for requirement in component.get("requires") or []:
            component_id = requirement_components.get(str(requirement))
            if component_id:
                prerequisites.add(component_id)
    return frozenset(prerequisites)


def _tool_states(binary_session: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    availability = binary_session.get("tool_availability")
    availability = availability if isinstance(availability, dict) else {}
    groups = availability.get("groups")
    states: dict[str, dict[str, Any]] = {}
    for group in groups or []:
        if not isinstance(group, dict):
            continue
        shared = {
            "available": bool(group.get("available")),
            "reason": str(group.get("reason") or ""),
            "remediation": str(group.get("remediation") or ""),
            "surface": str(group.get("surface") or ""),
        }
        for name in group.get("names") or []:
            tool_name = str(name)
            previous = states.get(tool_name)
            if previous is None or (shared["available"] and not previous["available"]):
                states[tool_name] = shared
    return states, availability


def _provider_evidence(
    binary_session: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, set[str]]]:
    """Map live analysis providers to the tools they can serve."""

    provider_states: dict[str, dict[str, Any]] = {}
    tool_providers: dict[str, set[str]] = {}
    candidates = binary_session.get("analysis_provider_candidates")
    for candidate in candidates or []:
        if not isinstance(candidate, dict):
            continue
        provider = candidate.get("provider")
        provider = provider if isinstance(provider, dict) else {}
        provider_id = str(provider.get("id") or "").strip()
        if not provider_id:
            continue
        availability = candidate.get("availability")
        availability = availability if isinstance(availability, dict) else {}
        provider_states[provider_id] = availability
        for capability in candidate.get("capabilities") or []:
            if not isinstance(capability, dict):
                continue
            for group in capability.get("groups") or []:
                if not isinstance(group, dict):
                    continue
                for operation in group.get("operations") or []:
                    name = str(operation or "").strip()
                    if name:
                        tool_providers.setdefault(name, set()).add(provider_id)
    return provider_states, tool_providers


def _unavailable_component_state(item: dict[str, Any]) -> str:
    """Map unavailable components without redefining the fixed status taxonomy."""

    return (
        "needs_action"
        if str(item.get("install_mode") or "") in _MANUAL_INSTALL_MODES
        else "missing"
    )


def _actual_tool_states(
    tool_states: dict[str, dict[str, Any]],
    provider_states: dict[str, dict[str, Any]],
    tool_providers: dict[str, set[str]],
    functional_probes: dict[str, dict[str, Any]] | None = None,
    platform_id: str = "",
) -> dict[str, dict[str, Any]]:
    """Resolve target-dependent tools against live provider availability."""

    actual: dict[str, dict[str, Any]] = {}
    for name, state in tool_states.items():
        resolved = dict(state)
        if state.get("reason") == "target_required":
            providers = tool_providers.get(name) or set()
            availability = [
                provider_states[provider_id]
                for provider_id in providers
                if provider_id in provider_states
            ]
            if availability:
                if any(item.get("status") == "available" for item in availability):
                    resolved["available"] = True
                    resolved["actual_reason"] = "available"
                elif all(item.get("code") == "unsupported_host" for item in availability):
                    resolved["actual_reason"] = "unsupported_host"
                else:
                    resolved["actual_reason"] = "provider_unavailable"
            else:
                resolved["actual_reason"] = "target_required"
        else:
            resolved["actual_reason"] = resolved.get("reason") or (
                "available" if resolved.get("available") else "missing"
            )
        probe = (functional_probes or {}).get(name)
        if isinstance(probe, dict):
            resolved["functional_verified"] = True
            if probe.get("ok") is True:
                resolved["available"] = True
                resolved["actual_reason"] = "available"
            else:
                resolved["available"] = False
                resolved["actual_reason"] = str(
                    probe.get("reason") or "functional_probe_failed"
                )
                resolved["functional_error"] = str(probe.get("error") or "")
        if (
            platform_id.startswith("windows-")
            and name in _RIZIN_PERSISTENT_DEBUG_TOOLS
        ):
            resolved["available"] = False
            resolved["actual_reason"] = "unsupported_host"
            resolved["platform_error"] = (
                "Rizin persistent debug sessions require a Linux TTY; "
                "use execute_rizin_command for one-shot CLI analysis on Windows"
            )
        actual[name] = resolved
    return actual


def _has_live_tool(
    tool_names: list[str],
    tool_states: dict[str, dict[str, Any]],
) -> bool:
    """Whether the live MCP can currently serve at least one listed tool."""

    return any(bool((tool_states.get(name) or {}).get("available")) for name in tool_names)


def _all_tools_unsupported(
    tool_names: list[str],
    tool_states: dict[str, dict[str, Any]],
) -> bool:
    states = [tool_states.get(name) for name in tool_names]
    return bool(states) and all(
        item is not None
        and (
            item.get("reason") == "unsupported_host"
            or item.get("actual_reason") == "unsupported_host"
        )
        for item in states
    )


def _component_state(
    item: dict[str, Any] | None,
    confirmation_tools: list[str] | None = None,
    tool_states: dict[str, dict[str, Any]] | None = None,
    provider_states: dict[str, dict[str, Any]] | None = None,
    *,
    available_on_any_tool: bool = False,
) -> str:
    if not item:
        return "missing"
    code = str(item.get("code") or "")
    state = str(item.get("state") or item.get("local_state") or item.get("status") or "")
    inventory = item.get("inventory")
    inventory_state = (
        str(inventory.get("state") or "") if isinstance(inventory, dict) else ""
    )
    if code == "unsupported_host" or state == "unsupported" or inventory_state == "unsupported":
        return "unsupported"
    if item.get("requires_install_evidence") is True and not (
        bool(item.get("available"))
        or state in _READY_STATES
        or inventory_state in _READY_STATES
    ):
        return _unavailable_component_state(item)
    tools = confirmation_tools or []
    states = tool_states or {}
    functional_states = [
        states.get(name)
        for name in tools
        if isinstance(states.get(name), dict)
        and (states.get(name) or {}).get("functional_verified") is True
    ]
    if functional_states:
        if available_on_any_tool and any(
            item.get("available") for item in functional_states
        ):
            return "available"
        if any(
            item.get("actual_reason") == "unsupported_host"
            for item in (states.get(name) for name in tools)
            if isinstance(item, dict)
        ):
            if any(item.get("available") for item in functional_states):
                return "partial"
        if any(item.get("available") for item in functional_states):
            return "available"
        if any(item.get("actual_reason") == "unsupported_host" for item in functional_states):
            return "unsupported"
        return _unavailable_component_state(item)
    provider = (provider_states or {}).get(str(item.get("id") or ""))
    if provider is not None:
        status = str(provider.get("status") or "")
        code = str(provider.get("code") or "")
        if status == "available":
            return "available"
        if code == "unsupported_host" or status == "unsupported_host":
            return "unsupported"
        return _unavailable_component_state(item)
    live_tools = sum(bool((states.get(name) or {}).get("available")) for name in tools)
    if tools:
        if live_tools == len(tools):
            return "available"
        if available_on_any_tool and live_tools:
            return "available"
        if live_tools:
            return "partial"
    if (
        (not tools and bool(item.get("available")))
        or state in _READY_STATES
        or inventory_state in _READY_STATES
    ):
        if not tools:
            return "available"
        if _all_tools_unsupported(tools, states):
            return "unsupported"
        return _unavailable_component_state(item)
    if state == "failed":
        return "failed"
    if state in _NEEDS_ACTION_STATES or inventory_state in _NEEDS_ACTION_STATES:
        return _unavailable_component_state(item)
    return _unavailable_component_state(item)


def _tool_member_state(
    tool_names: list[str],
    tool_states: dict[str, dict[str, Any]],
) -> str:
    if not tool_names:
        return "missing"
    items = [tool_states.get(name) for name in tool_names]
    if any(item is None for item in items):
        return "missing"
    applicable = [item for item in items if item and item.get("reason") != "unsupported_host"]
    if not applicable:
        return "unsupported"
    if any(item and item.get("actual_reason") in {"provider_missing", "provider_unavailable"} for item in applicable):
        return "missing"
    if all(
        item and item.get("available")
        for item in applicable
    ):
        return "available"
    return "missing"


def _card_state(
    members: list[dict[str, Any]],
    *,
    platform_matches: bool,
) -> str:
    if not platform_matches:
        return "unsupported"
    if not members or all(member["state"] == "unsupported" for member in members):
        return "unsupported"
    capability_members = [
        member for member in members if not member.get("prerequisite")
    ]
    relevant = capability_members or members
    ready = sum(member["state"] == "available" for member in relevant)
    if ready == len(relevant):
        return "available"
    if ready > 0:
        return "partial"
    if any(member["state"] == "failed" for member in relevant):
        return "failed"
    needs_action = any(member["state"] == "needs_action" for member in relevant)
    if needs_action:
        return "needs_action"
    return "not_installed"


def _inventory_diagnostics(
    binary_session: dict[str, Any],
    tools: list[dict[str, Any]],
    mapped_tools: set[str],
    *,
    allow_surface_split: bool = False,
) -> dict[str, Any]:
    contract = _load_manifest().get("rea_inventory")
    contract = contract if isinstance(contract, dict) else {}
    availability = binary_session.get("tool_availability")
    availability = availability if isinstance(availability, dict) else {}
    groups = [
        item for item in availability.get("groups") or [] if isinstance(item, dict)
    ]
    options_by_signature: dict[tuple[str, str], int] = {}
    expected_counts_by_surface: dict[str, int] = {}
    for item in contract.get("groups") or []:
        if not isinstance(item, dict):
            continue
        surface = str(item.get("surface") or "")
        first_tool = str(item.get("first_tool") or "")
        count = int(item.get("count") or 0)
        options_by_signature[(surface, first_tool)] = count
        expected_counts_by_surface[surface] = (
            expected_counts_by_surface.get(surface, 0) + count
        )
    unknown_groups: list[dict[str, Any]] = []
    changed_groups: list[dict[str, Any]] = []
    actual_counts_by_surface: dict[str, int] = {}
    for group in groups:
        surface = str(group.get("surface") or "")
        actual_counts_by_surface[surface] = (
            actual_counts_by_surface.get(surface, 0)
            + len(group.get("names") or [])
        )
    for group in groups:
        names = [str(name) for name in group.get("names") or []]
        signature = (
            str(group.get("surface") or ""),
            names[0] if names else "",
        )
        expected_count = options_by_signature.get(signature)
        detail = {
            "surface": signature[0],
            "first_tool": signature[1],
            "count": len(names),
        }
        unknown_signature = expected_count is None
        unknown_surface = signature[0] not in expected_counts_by_surface
        surface_count_changed = (
            actual_counts_by_surface.get(signature[0])
            != expected_counts_by_surface.get(signature[0])
        )
        allowed_split = (
            allow_surface_split
            and unknown_signature
            and not unknown_surface
            and not surface_count_changed
        )
        unexpected_signature = unknown_signature and not allowed_split
        if unknown_surface or surface_count_changed or unexpected_signature:
            unknown_groups.append({**detail, "names": names[:8]})
        elif expected_count is not None and expected_count != len(names):
            changed_groups.append({**detail, "expected_count": expected_count})

    known_tools = {
        str(tool.get("name") or tool.get("id") or "")
        for tool in tools
        if isinstance(tool, dict)
    }
    expected_tool_count = int(contract.get("tool_count") or 0)
    server_identity = binary_session.get("server_identity")
    server_identity = server_identity if isinstance(server_identity, dict) else {}
    package = server_identity.get("package")
    package = package if isinstance(package, dict) else {}
    actual_version = str(package.get("version") or "")
    expected_version = str(contract.get("version") or "")
    contract_ok = (
        not contract
        or (
            expected_tool_count == len(known_tools)
            and expected_version in {"", actual_version}
            and not unknown_groups
        )
    )
    return {
        "rea_version": actual_version,
        "expected_rea_version": expected_version,
        "tool_count": len(known_tools),
        "expected_tool_count": expected_tool_count,
        "group_count": len(groups),
        "expected_group_count": int(contract.get("group_count") or 0),
        "availability_tool_count": sum(
            len(group.get("names") or []) for group in groups
        ),
        "mapped_tool_count": len(known_tools & mapped_tools),
        "core_tool_count": len(known_tools - mapped_tools),
        "unknown_tool_group_count": len(unknown_groups),
        "unknown_tool_groups": unknown_groups[:12],
        "changed_tool_group_count": len(changed_groups),
        "changed_tool_groups": changed_groups[:12],
        "group_topology_variable": bool(changed_groups),
        "inventory_contract_ok": contract_ok,
        "inventory_warning": (
            ""
            if contract_ok
            else "存在未归类的 REA 工具组或与 6.8.0 工具合同不一致"
        ),
    }


def build_capability_cards(
    scan: dict[str, Any],
    binary_session: dict[str, Any],
    tools: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Project REA's live inventory into the fixed product capability cards."""

    platform_id = str(scan.get("platform_id") or "")
    enhanced = {
        str(item.get("id")): item
        for item in scan.get("enhanced") or []
        if isinstance(item, dict) and item.get("id")
    }
    raw_tool_states, availability = _tool_states(binary_session)
    provider_states, tool_providers = _provider_evidence(binary_session)
    functional_probes = binary_session.get("functional_probes")
    functional_probes = (
        functional_probes if isinstance(functional_probes, dict) else {}
    )
    tool_states = _actual_tool_states(
        raw_tool_states,
        provider_states,
        tool_providers,
        functional_probes,
        platform_id,
    )
    cards: list[dict[str, Any]] = []
    mapped_tools: set[str] = set()
    for definition in _load_cards():
        platforms = definition.get("platforms")
        platform_matches = not isinstance(platforms, list) or platform_id in {
            str(item) for item in platforms
        }
        members: list[dict[str, Any]] = []
        member_items: dict[str, dict[str, Any] | None] = {}
        prerequisites = _prerequisite_components()
        scenario_tools: set[str] = set()
        for member in definition.get("members") or []:
            if not isinstance(member, dict):
                continue
            member_id = str(member.get("id") or "")
            if member.get("kind") == "component":
                component_id = str(member.get("component_id") or member_id)
                confirmation_tools = [
                    str(item)
                    for item in member.get("tool_names") or []
                    if str(item).strip()
                ]
                if not confirmation_tools:
                    confirmation_tools = list(
                        _COMPONENT_FUNCTIONAL_TOOLS.get(component_id, [])
                    )
                mapped_tools.update(confirmation_tools)
                member_items[member_id] = enhanced.get(component_id)
                state = _component_state(
                    enhanced.get(component_id),
                    confirmation_tools,
                    tool_states,
                    provider_states,
                    available_on_any_tool=bool(
                        member.get("available_on_any_tool")
                    ),
                )
                for name in confirmation_tools:
                    reason = str((tool_states.get(name) or {}).get("reason") or "")
                    if reason == "target_required":
                        scenario_tools.add(name)
                members.append(
                    {
                        "id": member_id,
                        "label": str(member.get("label") or component_id),
                        "kind": "component",
                        "state": state,
                        "tool_names": confirmation_tools,
                        "prerequisite": component_id in prerequisites,
                    }
                )
                continue
            tool_names = [
                str(item)
                for item in member.get("tool_names") or []
                if str(item).strip()
            ]
            mapped_tools.update(tool_names)
            member_items[member_id] = None
            required_component = str(member.get("requires_component") or "").strip()
            required_state = "available"
            if required_component:
                required_state = _component_state(
                    enhanced.get(required_component),
                    [],
                    tool_states,
                    provider_states,
                )
            if required_state == "available":
                state = _tool_member_state(tool_names, tool_states)
            elif required_state == "unsupported":
                state = "unsupported"
            else:
                state = "missing"
            for name in tool_names:
                reason = str((tool_states.get(name) or {}).get("reason") or "")
                if reason == "target_required":
                    scenario_tools.add(name)
            members.append(
                {
                    "id": member_id,
                    "label": str(member.get("label") or member_id),
                    "kind": "tool_group",
                    "state": state,
                    "tool_names": tool_names,
                }
            )

        status_tools = [
            str(item)
            for item in definition.get("tool_names") or []
            if str(item).strip()
        ]
        mapped_tools.update(status_tools)
        tool_items = []
        for name in status_tools:
            state = tool_states.get(name)
            if state is None:
                tool_items.append(
                    {
                        "name": name,
                        "available": False,
                        "reason": "missing",
                        "remediation": "",
                        "surface": "",
                    }
                )
                continue
            tool_items.append({"name": name, **state})
            if state.get("reason") == "target_required":
                scenario_tools.add(name)

        if not platform_matches:
            for member in members:
                member["state"] = "unsupported"
        elif status_tools and not _has_live_tool(status_tools, tool_states):
            # Component discovery alone is not enough: the registered MCP must
            # expose a usable tool for this capability card.
            for member in members:
                if member["state"] == "available":
                    if _all_tools_unsupported(status_tools, tool_states):
                        member["state"] = "unsupported"
                        continue
                    item = member_items.get(str(member.get("id") or ""))
                    mode = str((item or {}).get("install_mode") or "")
                    member["state"] = (
                        "needs_action"
                        if mode in {"manual", "manual_license", "system"}
                        else "missing"
                    )

        card_state = _card_state(members, platform_matches=platform_matches)

        cards.append(
            {
                "id": str(definition.get("id") or ""),
                "label": str(definition.get("label") or definition.get("id") or ""),
                "description": str(definition.get("description") or ""),
                "state": card_state,
                "ready_members": sum(member["state"] == "available" for member in members),
                "total_members": len(members),
                "unsupported_members": sum(
                    member["state"] == "unsupported" for member in members
                ),
                "scenario_required": len(scenario_tools),
                "manual_actions": list(definition.get("manual_actions") or []),
                "members": members,
                "tools": tool_items,
            }
        )

    diagnostics = _inventory_diagnostics(
        binary_session,
        tools,
        mapped_tools,
        allow_surface_split=platform_id.startswith("macos-"),
    )
    diagnostics["availability_group_count"] = len(
        availability.get("groups") or []
    )
    return cards, diagnostics
