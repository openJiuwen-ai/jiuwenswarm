# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Tests for host-route-aware sandbox uplink allocation."""

import ipaddress

import pytest

from jiuwenbox.supervisor import network
from jiuwenbox.supervisor.network import (
    _allocate_uplink_block as allocate_uplink_block,
)
from jiuwenbox.supervisor.network import (
    _conflicting_route_networks as conflicting_route_networks,
)
from jiuwenbox.supervisor.network import (
    _policy_capture_holes as policy_capture_holes,
)


# Representative ``ip -4 route show table all`` fixture with
# an explicit non-main policy table. Table 2022 covers IPv4 except 1.1.1.1 and 127.0.0.1.
POLICY_CAPTURE_ROUTES = """\
0.0.0.0/8 via 198.18.0.2 dev policy0 table 2022
1.0.0.0/16 via 198.18.0.2 dev policy0 table 2022
1.1.0.0/24 via 198.18.0.2 dev policy0 table 2022
1.1.1.0 via 198.18.0.2 dev policy0 table 2022
1.1.1.2/31 via 198.18.0.2 dev policy0 table 2022
1.1.1.4/30 via 198.18.0.2 dev policy0 table 2022
1.1.1.8/29 via 198.18.0.2 dev policy0 table 2022
1.1.1.16/28 via 198.18.0.2 dev policy0 table 2022
1.1.1.32/27 via 198.18.0.2 dev policy0 table 2022
1.1.1.64/26 via 198.18.0.2 dev policy0 table 2022
1.1.1.128/25 via 198.18.0.2 dev policy0 table 2022
1.1.2.0/23 via 198.18.0.2 dev policy0 table 2022
1.1.4.0/22 via 198.18.0.2 dev policy0 table 2022
1.1.8.0/21 via 198.18.0.2 dev policy0 table 2022
1.1.16.0/20 via 198.18.0.2 dev policy0 table 2022
1.1.32.0/19 via 198.18.0.2 dev policy0 table 2022
1.1.64.0/18 via 198.18.0.2 dev policy0 table 2022
1.1.128.0/17 via 198.18.0.2 dev policy0 table 2022
1.2.0.0/15 via 198.18.0.2 dev policy0 table 2022
1.4.0.0/14 via 198.18.0.2 dev policy0 table 2022
1.8.0.0/13 via 198.18.0.2 dev policy0 table 2022
1.16.0.0/12 via 198.18.0.2 dev policy0 table 2022
1.32.0.0/11 via 198.18.0.2 dev policy0 table 2022
1.64.0.0/10 via 198.18.0.2 dev policy0 table 2022
1.128.0.0/9 via 198.18.0.2 dev policy0 table 2022
2.0.0.0/7 via 198.18.0.2 dev policy0 table 2022
4.0.0.0/6 via 198.18.0.2 dev policy0 table 2022
8.0.0.0/5 via 198.18.0.2 dev policy0 table 2022
16.0.0.0/4 via 198.18.0.2 dev policy0 table 2022
32.0.0.0/3 via 198.18.0.2 dev policy0 table 2022
64.0.0.0/3 via 198.18.0.2 dev policy0 table 2022
96.0.0.0/4 via 198.18.0.2 dev policy0 table 2022
112.0.0.0/5 via 198.18.0.2 dev policy0 table 2022
120.0.0.0/6 via 198.18.0.2 dev policy0 table 2022
124.0.0.0/7 via 198.18.0.2 dev policy0 table 2022
126.0.0.0/8 via 198.18.0.2 dev policy0 table 2022
127.0.0.0 via 198.18.0.2 dev policy0 table 2022
127.0.0.2/31 via 198.18.0.2 dev policy0 table 2022
127.0.0.4/30 via 198.18.0.2 dev policy0 table 2022
127.0.0.8/29 via 198.18.0.2 dev policy0 table 2022
127.0.0.16/28 via 198.18.0.2 dev policy0 table 2022
127.0.0.32/27 via 198.18.0.2 dev policy0 table 2022
127.0.0.64/26 via 198.18.0.2 dev policy0 table 2022
127.0.0.128/25 via 198.18.0.2 dev policy0 table 2022
127.0.1.0/24 via 198.18.0.2 dev policy0 table 2022
127.0.2.0/23 via 198.18.0.2 dev policy0 table 2022
127.0.4.0/22 via 198.18.0.2 dev policy0 table 2022
127.0.8.0/21 via 198.18.0.2 dev policy0 table 2022
127.0.16.0/20 via 198.18.0.2 dev policy0 table 2022
127.0.32.0/19 via 198.18.0.2 dev policy0 table 2022
127.0.64.0/18 via 198.18.0.2 dev policy0 table 2022
127.0.128.0/17 via 198.18.0.2 dev policy0 table 2022
127.1.0.0/16 via 198.18.0.2 dev policy0 table 2022
127.2.0.0/15 via 198.18.0.2 dev policy0 table 2022
127.4.0.0/14 via 198.18.0.2 dev policy0 table 2022
127.8.0.0/13 via 198.18.0.2 dev policy0 table 2022
127.16.0.0/12 via 198.18.0.2 dev policy0 table 2022
127.32.0.0/11 via 198.18.0.2 dev policy0 table 2022
127.64.0.0/10 via 198.18.0.2 dev policy0 table 2022
127.128.0.0/9 via 198.18.0.2 dev policy0 table 2022
128.0.0.0/1 via 198.18.0.2 dev policy0 table 2022
default via 192.168.66.1 dev wlx200db0c1d3af proto dhcp src 192.168.66.208 metric 20100
default via 7.249.252.1 dev wlx90de800c7a71 proto dhcp src 7.249.252.54 metric 20600
7.249.252.0/23 dev wlx90de800c7a71 proto kernel scope link src 7.249.252.54 metric 600
172.17.0.0/16 dev docker0 proto kernel scope link src 172.17.0.1 linkdown
192.168.66.0/24 dev wlx200db0c1d3af proto kernel scope link src 192.168.66.208 metric 100
198.18.0.0/30 dev policy0 proto kernel scope link src 198.18.0.1
local 7.249.252.54 dev wlx90de800c7a71 table local proto kernel scope host src 7.249.252.54
broadcast 7.249.253.255 dev wlx90de800c7a71 table local proto kernel scope link src 7.249.252.54
local 127.0.0.0/8 dev lo table local proto kernel scope host src 127.0.0.1
local 127.0.0.1 dev lo table local proto kernel scope host src 127.0.0.1
broadcast 127.255.255.255 dev lo table local proto kernel scope link src 127.0.0.1
local 172.17.0.1 dev docker0 table local proto kernel scope host src 172.17.0.1
broadcast 172.17.255.255 dev docker0 table local proto kernel scope link src 172.17.0.1 linkdown
local 192.168.66.208 dev wlx200db0c1d3af table local proto kernel scope host src 192.168.66.208
broadcast 192.168.66.255 dev wlx200db0c1d3af table local proto kernel scope link src 192.168.66.208
local 198.18.0.1 dev policy0 table local proto kernel scope host src 198.18.0.1
broadcast 198.18.0.3 dev policy0 table local proto kernel scope link src 198.18.0.1
"""


def _network_strings(route_output: str) -> set[str]:
    return {str(item) for item in conflicting_route_networks(route_output)}


def _ipv4_without(holes: list[ipaddress.IPv4Network]) -> list[ipaddress.IPv4Network]:
    networks = [ipaddress.ip_network("0.0.0.0/0")]
    for hole in holes:
        updated: list[ipaddress.IPv4Network] = []
        for item in networks:
            if hole.subnet_of(item):
                updated.extend(item.address_exclude(hole))
            else:
                updated.append(item)
        networks = updated
    return networks


def test_policy_capture_preserves_only_real_conflicts_and_holes(monkeypatch):
    conflicts = _network_strings(POLICY_CAPTURE_ROUTES)

    assert {"1.1.1.1/32", "127.0.0.1/32"}.issubset(conflicts)
    assert {
        "7.249.252.0/23",
        "172.17.0.0/16",
        "192.168.66.0/24",
        "198.18.0.0/30",
    }.issubset(conflicts)
    assert "100.64.0.0/10" not in conflicts

    monkeypatch.setattr(
        network,
        "_route_networks",
        lambda: conflicting_route_networks(POLICY_CAPTURE_ROUTES),
    )
    *_, subnet = allocate_uplink_block("policy-routing-sandbox", "")
    assert ipaddress.ip_network(subnet).subnet_of(ipaddress.ip_network("100.64.0.0/10"))


def test_main_defaults_are_skipped_but_static_and_direct_routes_conflict():
    conflicts = _network_strings("""\
default via 192.0.2.1 dev eth0
default via 192.0.2.1 dev eth0 table main
default via 192.0.2.1 dev eth0 table 254
10.0.0.0/8 via 192.0.2.2 dev eth0
172.16.0.0/12 dev eth1 scope link
default via 198.18.0.2 dev policy0 table 2022
""")

    assert conflicts == {"10.0.0.0/8", "172.16.0.0/12"}


def test_typed_routes_are_conflicts_and_block_uplink_candidates(monkeypatch):
    route_output = """\
blackhole 100.64.0.0/10 table main
prohibit 10.200.0.0/16 table main
local 192.0.2.1 dev lo table local
"""
    conflicts = _network_strings(route_output)

    assert conflicts == {"100.64.0.0/10", "10.200.0.0/16", "192.0.2.1/32"}

    monkeypatch.setattr(
        network, "_route_networks", lambda: list(map(ipaddress.ip_network, conflicts))
    )
    *_, subnet = allocate_uplink_block("typed-route-sandbox", "")
    allocated = ipaddress.ip_network(subnet)
    assert not allocated.overlaps(ipaddress.ip_network("100.64.0.0/10"))
    assert not allocated.overlaps(ipaddress.ip_network("10.200.0.0/16"))


def test_typed_default_is_a_conflict():
    assert _network_strings("blackhole default table main") == {"0.0.0.0/0"}


def test_sparse_or_mixed_policy_routes_remain_conflicts():
    conflicts = _network_strings("""\
10.0.0.0/8 via 192.0.2.1 dev policy2 table 100
0.0.0.0/1 via 198.18.0.2 dev policy0 table 2022
128.0.0.0/1 via 198.18.0.2 dev policy1 table 2022
""")

    assert conflicts == {"10.0.0.0/8", "0.0.0.0/1", "128.0.0.0/1"}


def test_incomplete_global_capture_fails_closed():
    incomplete = POLICY_CAPTURE_ROUTES.replace(
        "64.0.0.0/3 via 198.18.0.2 dev policy0 table 2022\n", ""
    )

    assert "0.0.0.0/8" in _network_strings(incomplete)


@pytest.mark.parametrize(
    "holes",
    [
        [ipaddress.ip_network("203.0.113.0/31")],
        [ipaddress.ip_network(f"203.0.113.{index}/32") for index in range(17)],
    ],
)
def test_capture_with_broad_or_excessive_holes_fails_closed(holes):
    assert policy_capture_holes(_ipv4_without(holes)) is None


@pytest.mark.parametrize("destination", ["not-a-network", "::/0"])
def test_malformed_route_destination_fails_closed(destination):
    with pytest.raises(network.NetworkSetupError, match="Failed to parse IPv4 route"):
        conflicting_route_networks(f"{destination} via 198.18.0.2 dev policy0 table 2022")


def test_malformed_route_fields_cannot_qualify_as_policy_capture():
    conflicts = _network_strings("default via dev policy0 table 2022")

    assert conflicts == {"0.0.0.0/0"}


@pytest.mark.parametrize(
    "route_line",
    [
        "blackhole 10.0.0.0/8 table",
        "prohibit 10.0.0.0/8 table table",
        "throw 10.0.0.0/8 table 100 table 200",
        "nexthop via 192.0.2.1 dev eth0",
    ],
)
def test_malformed_typed_route_or_orphan_nexthop_fails_closed(route_line):
    with pytest.raises(network.NetworkSetupError, match="Failed to parse IPv4 route"):
        conflicting_route_networks(route_line)
