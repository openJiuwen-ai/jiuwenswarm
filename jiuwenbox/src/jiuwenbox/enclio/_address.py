# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Exact endpoint normalization shared by policy compilation and transport."""

import ipaddress
import re

_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.ASCII)
# Fixed security classifications, not deployment endpoints or configurable allow rules.
_IPV4_THIS_NETWORK = ipaddress.ip_network("0.0.0.0/8")
_IPV4_PRIVATE_10_NETWORK = ipaddress.ip_network("10.0.0.0/8")
_IPV4_SHARED_ADDRESS_NETWORK = ipaddress.ip_network("100.64.0.0/10")
_IPV4_LOOPBACK_NETWORK = ipaddress.ip_network("127.0.0.0/8")
_IPV4_LINK_LOCAL_NETWORK = ipaddress.ip_network("169.254.0.0/16")
_IPV4_PRIVATE_172_NETWORK = ipaddress.ip_network("172.16.0.0/12")
_IPV4_IETF_PROTOCOL_ASSIGNMENTS_NETWORK = ipaddress.ip_network("192.0.0.0/24")
_IPV4_DOCUMENTATION_1_NETWORK = ipaddress.ip_network("192.0.2.0/24")
_IPV4_SIX_TO_FOUR_RELAY_NETWORK = ipaddress.ip_network("192.88.99.0/24")
_IPV4_PRIVATE_192_NETWORK = ipaddress.ip_network("192.168.0.0/16")
_IPV4_BENCHMARK_NETWORK = ipaddress.ip_network("198.18.0.0/15")
_IPV4_DOCUMENTATION_2_NETWORK = ipaddress.ip_network("198.51.100.0/24")
_IPV4_DOCUMENTATION_3_NETWORK = ipaddress.ip_network("203.0.113.0/24")
_IPV4_MULTICAST_AND_RESERVED_NETWORK = ipaddress.ip_network("224.0.0.0/3")

_NON_PUBLIC_V4 = (
    _IPV4_THIS_NETWORK,
    _IPV4_PRIVATE_10_NETWORK,
    _IPV4_SHARED_ADDRESS_NETWORK,
    _IPV4_LOOPBACK_NETWORK,
    _IPV4_LINK_LOCAL_NETWORK,
    _IPV4_PRIVATE_172_NETWORK,
    _IPV4_IETF_PROTOCOL_ASSIGNMENTS_NETWORK,
    _IPV4_DOCUMENTATION_1_NETWORK,
    _IPV4_SIX_TO_FOUR_RELAY_NETWORK,
    _IPV4_PRIVATE_192_NETWORK,
    _IPV4_BENCHMARK_NETWORK,
    _IPV4_DOCUMENTATION_2_NETWORK,
    _IPV4_DOCUMENTATION_3_NETWORK,
    _IPV4_MULTICAST_AND_RESERVED_NETWORK,
)

_GLOBAL_V6 = ipaddress.ip_network("2000::/3")
_IPV6_IETF_PROTOCOL_ASSIGNMENTS_NETWORK = ipaddress.ip_network("2001::/23")
_IPV6_DOCUMENTATION_NETWORK = ipaddress.ip_network("2001:db8::/32")
_IPV6_SIX_TO_FOUR_NETWORK = ipaddress.ip_network("2002::/16")
_IPV6_DOCUMENTATION_3FFF_NETWORK = ipaddress.ip_network("3fff::/20")

_NON_PUBLIC_V6 = (
    _IPV6_IETF_PROTOCOL_ASSIGNMENTS_NETWORK,
    _IPV6_DOCUMENTATION_NETWORK,
    _IPV6_SIX_TO_FOUR_NETWORK,
    _IPV6_DOCUMENTATION_3FFF_NETWORK,
)


def public_ip(address: str) -> str:
    if "%" in address:
        raise ValueError("scoped_address")
    value = ipaddress.ip_address(address)
    if value.version == 4:
        valid = not any(value in network for network in _NON_PUBLIC_V4)
    else:
        valid = value in _GLOBAL_V6 and not any(value in network for network in _NON_PUBLIC_V6)
    if not valid:
        raise ValueError("non_public_address")
    return str(value)


def dns_name(address: str) -> str:
    if not address.isascii():
        raise ValueError("invalid_dns_name")
    name = address.lower().removesuffix(".")
    labels = name.split(".")
    if len(name) > 253 or len(labels) < 2 or not all(_LABEL.fullmatch(x) for x in labels):
        raise ValueError("invalid_dns_name")
    # Reject abbreviated, octal, decimal and hexadecimal IPv4 spellings.
    if all(re.fullmatch(r"[0-9]+|0x[0-9a-f]+", label) for label in labels):
        raise ValueError("ambiguous_address")
    return name


def endpoint(kind: str, address: str, port: int) -> tuple[str, str, int]:
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValueError("invalid_port")
    if kind == "dns":
        return kind, dns_name(address), port
    if kind == "ip":
        return kind, public_ip(address), port
    raise ValueError("invalid_endpoint_kind")
