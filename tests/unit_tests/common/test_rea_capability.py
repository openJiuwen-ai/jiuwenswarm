import pytest

from jiuwenswarm.common.rea_capability import (
    _card_state,
    _load_manifest,
    build_capability_cards,
)
from jiuwenswarm.common.rea_mcp_probe import validate_tool_inventory


def _inventory_fixture() -> tuple[dict, list[dict]]:
    contract = _load_manifest()["rea_inventory"]
    groups = []
    tools = []
    for definition in contract["groups"]:
        names = [definition["first_tool"]] + [
            f"{definition['first_tool']}__{index}"
            for index in range(1, definition["count"])
        ]
        groups.append(
            {
                "surface": definition["surface"],
                "names": names,
                "available": True,
                "reason": "available",
                "remediation": "",
            }
        )
        tools.extend({"name": name, "inputSchema": {"type": "object"}} for name in names)
    return {
        "server_identity": {"package": {"version": "6.8.0"}},
        "tool_availability": {"groups": groups},
    }, tools


def _scan(platform_id: str = "windows-x64") -> dict:
    component_ids = [
        "java",
        "ghidra",
        "ida",
        "kuna",
        "hopper",
        "jadx",
        "adb",
        "apktool",
        "jeb",
        "pcap",
        "mitmproxy",
        "firmware",
        "sigrok",
        "pwntools",
        "wabt",
        "wakaru",
        "objdump",
        "rizin",
        "cutter",
        "chrome",
        "gdb",
    ]
    enhanced = []
    for component_id in component_ids:
        manual = component_id in {"ida", "jeb", "firmware", "gdb", "sigrok", "objdump"}
        enhanced.append(
            {
                "id": component_id,
                "available": not manual,
                "install_mode": "manual" if manual else "managed",
                "state": "missing" if manual else "configured",
            }
        )
    return {"platform_id": platform_id, "enhanced": enhanced}


def test_capability_manifest_has_exactly_ten_product_cards() -> None:
    cards = _load_manifest()["cards"]

    assert len(cards) == 10
    assert all(card.get("members") for card in cards)
    assert all("manual_actions" in card for card in cards)


def test_build_capability_cards_reports_member_counts_and_contract() -> None:
    binary_session, tools = _inventory_fixture()
    scan = _scan()
    adb = next(item for item in scan["enhanced"] if item["id"] == "adb")
    adb["available"] = False
    adb["state"] = "missing"
    gdb = next(item for item in scan["enhanced"] if item["id"] == "gdb")
    gdb["code"] = "unsupported_host"
    gdb["state"] = "unsupported"

    cards, diagnostics = build_capability_cards(
        scan,
        binary_session,
        tools,
    )
    by_id = {card["id"]: card for card in cards}

    assert len(cards) == 10
    assert by_id["ghidra"]["state"] == "available"
    assert by_id["ghidra"]["ready_members"] == 2
    assert by_id["ghidra"]["total_members"] == 2
    assert by_id["android"]["ready_members"] == 3
    assert by_id["android"]["total_members"] == 4
    assert by_id["android"]["state"] == "partial"
    assert by_id["apple"]["state"] == "unsupported"
    assert by_id["apple"]["unsupported_members"] == 1
    assert by_id["dynamic"]["total_members"] == 7
    assert by_id["dynamic"]["unsupported_members"] == 1
    assert by_id["dynamic"]["state"] == "partial"
    assert diagnostics["inventory_contract_ok"] is True
    assert diagnostics["tool_count"] == 229
    assert diagnostics["group_count"] == 77
    assert diagnostics["unknown_tool_group_count"] == 0


def test_build_capability_cards_warns_about_new_unknown_tool_group() -> None:
    binary_session, tools = _inventory_fixture()
    binary_session["tool_availability"]["groups"].append(
        {
            "surface": "future-provider",
            "names": ["future_tool"],
            "available": True,
            "reason": "available",
            "remediation": "",
        }
    )
    tools.append({"name": "future_tool", "inputSchema": {"type": "object"}})

    _, diagnostics = build_capability_cards(_scan(), binary_session, tools)

    assert diagnostics["inventory_contract_ok"] is False
    assert diagnostics["unknown_tool_group_count"] == 1
    assert diagnostics["unknown_tool_groups"][0]["surface"] == "future-provider"
    assert "未归类" in diagnostics["inventory_warning"]


def test_component_missing_is_not_overridden_by_advertised_tool_contract() -> None:
    binary_session, tools = _inventory_fixture()
    cards, _ = build_capability_cards(_scan("macos-arm64"), binary_session, tools)
    dynamic = next(card for card in cards if card["id"] == "dynamic")
    gdb = next(member for member in dynamic["members"] if member["id"] == "gdb")

    assert gdb["state"] == "needs_action"
    assert dynamic["state"] == "partial"


def test_repartitioning_known_tool_groups_is_not_an_unknown_group_warning() -> None:
    binary_session, tools = _inventory_fixture()
    groups = binary_session["tool_availability"]["groups"]
    wasm = next(group for group in groups if "inspect_wasm_artifact" in group["names"])
    sqlite = next(group for group in groups if "inspect_sqlite_database" in group["names"])
    sqlite["names"].append(wasm["names"][0])
    groups.remove(wasm)

    _, diagnostics = build_capability_cards(_scan(), binary_session, tools)

    assert diagnostics["inventory_contract_ok"] is True
    assert diagnostics["unknown_tool_group_count"] == 0
    assert diagnostics["changed_tool_group_count"] == 1
    assert diagnostics["group_topology_variable"] is True


def test_provider_availability_split_is_not_an_unknown_tool_group() -> None:
    binary_session, tools = _inventory_fixture()
    groups = binary_session["tool_availability"]["groups"]
    pcap = next(group for group in groups if group["surface"] == "pcap-provider")
    capture, client = pcap["names"]
    pcap["names"] = [capture]
    groups.append(
        {
            "surface": "pcap-provider",
            "names": [client],
            "available": True,
            "reason": "available",
            "remediation": "",
        }
    )

    _, diagnostics = build_capability_cards(_scan("macos-arm64"), binary_session, tools)

    assert diagnostics["inventory_contract_ok"] is True
    assert diagnostics["unknown_tool_group_count"] == 0
    assert diagnostics["changed_tool_group_count"] == 1
    assert diagnostics["group_topology_variable"] is True


def test_windows_keeps_strict_tool_group_diagnostics() -> None:
    binary_session, tools = _inventory_fixture()
    groups = binary_session["tool_availability"]["groups"]
    pcap = next(group for group in groups if group["surface"] == "pcap-provider")
    capture, client = pcap["names"]
    pcap["names"] = [capture]
    groups.append(
        {
            "surface": "pcap-provider",
            "names": [client],
            "available": True,
            "reason": "available",
            "remediation": "",
        }
    )

    _, diagnostics = build_capability_cards(_scan("windows-x64"), binary_session, tools)

    assert diagnostics["inventory_contract_ok"] is False
    assert diagnostics["unknown_tool_group_count"] == 1


@pytest.mark.parametrize(
    ("members", "expected"),
    [
        ([{"state": "available"}], "available"),
        ([{"state": "available"}, {"state": "missing"}], "partial"),
        ([{"state": "needs_action"}], "needs_action"),
        ([{"state": "unsupported"}], "unsupported"),
        ([{"state": "failed"}], "failed"),
        ([{"state": "missing"}], "not_installed"),
    ],
)
def test_card_state_mapping(members: list[dict], expected: str) -> None:
    assert _card_state(members, platform_matches=True) == expected


def test_tool_inventory_requires_tools_list_and_session_union_to_match() -> None:
    binary_session, tools = _inventory_fixture()
    result = validate_tool_inventory(tools, binary_session)
    assert result["inventory_verified"] is True
    assert result["availability_tool_count"] == 229

    tools.pop()
    with pytest.raises(RuntimeError, match="inventory mismatch"):
        validate_tool_inventory(tools, binary_session)
