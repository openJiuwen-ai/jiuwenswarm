"""Product-level capability-card projection for REA setup."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any


CARD_MANIFEST = Path(__file__).resolve().parent.parent / "resources" / "rea" / "capability-cards.json"
_READY_STATES = {"available", "configured", "installed", "ready", "reused"}
_NEEDS_ACTION_STATES = {
    "cached",
    "discovered",
    "downloaded_not_installed",
    "needs_action",
    "partial",
}


@lru_cache(maxsize=1)
def _load_manifest() -> dict[str, Any]:
    payload = json.loads(CARD_MANIFEST.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {"cards": []}


def _load_cards() -> list[dict[str, Any]]:
    cards = _load_manifest().get("cards")
    return [item for item in cards or [] if isinstance(item, dict)]


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


def _component_state(item: dict[str, Any] | None) -> str:
    if not item:
        return "missing"
    if bool(item.get("available")):
        return "available"
    code = str(item.get("code") or "")
    state = str(item.get("state") or item.get("local_state") or item.get("status") or "")
    inventory = item.get("inventory")
    inventory_state = (
        str(inventory.get("state") or "") if isinstance(inventory, dict) else ""
    )
    if code == "unsupported_host" or state == "unsupported" or inventory_state == "unsupported":
        return "unsupported"
    if state == "failed":
        return "failed"
    if state in _READY_STATES or inventory_state in _READY_STATES:
        return "available"
    if state in _NEEDS_ACTION_STATES or inventory_state in _NEEDS_ACTION_STATES:
        return "needs_action"
    if str(item.get("install_mode") or "") in {"manual", "manual_license", "system"}:
        return "needs_action"
    return "missing"


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
    if any(
        item
        and item.get("reason") in {"provider_missing", "provider_unavailable"}
        for item in applicable
    ):
        return "needs_action"
    if all(
        item
        and item.get("reason") in {"available", "target_required"}
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
    if any(member["state"] == "failed" for member in members):
        return "failed"
    applicable = [member for member in members if member["state"] != "unsupported"]
    if not applicable:
        return "unsupported"
    ready = sum(member["state"] == "available" for member in applicable)
    needs_action = any(member["state"] == "needs_action" for member in applicable)
    incomplete = any(member["state"] in {"missing", "failed"} for member in applicable)
    if ready == len(applicable) and not needs_action and not incomplete:
        return "available"
    if ready > 0:
        return "partial"
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
        if unknown_surface or surface_count_changed or (
            unknown_signature and not allowed_split
        ):
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
    tool_states, availability = _tool_states(binary_session)
    cards: list[dict[str, Any]] = []
    mapped_tools: set[str] = set()
    for definition in _load_cards():
        platforms = definition.get("platforms")
        platform_matches = not isinstance(platforms, list) or platform_id in {
            str(item) for item in platforms
        }
        members: list[dict[str, Any]] = []
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
                mapped_tools.update(confirmation_tools)
                state = _component_state(enhanced.get(component_id))
                scenario_tools.update(
                    name
                    for name in confirmation_tools
                    if str((tool_states.get(name) or {}).get("reason") or "")
                    == "target_required"
                )
                members.append(
                    {
                        "id": member_id,
                        "label": str(member.get("label") or component_id),
                        "kind": "component",
                        "state": state,
                        "tool_names": confirmation_tools,
                    }
                )
                continue
            tool_names = [
                str(item)
                for item in member.get("tool_names") or []
                if str(item).strip()
            ]
            mapped_tools.update(tool_names)
            state = _tool_member_state(tool_names, tool_states)
            scenario_tools.update(
                name
                for name in tool_names
                if str((tool_states.get(name) or {}).get("reason") or "")
                == "target_required"
            )
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

        cards.append(
            {
                "id": str(definition.get("id") or ""),
                "label": str(definition.get("label") or definition.get("id") or ""),
                "description": str(definition.get("description") or ""),
                "state": _card_state(members, platform_matches=platform_matches),
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
