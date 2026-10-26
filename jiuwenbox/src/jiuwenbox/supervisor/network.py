# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Network isolation via iptables rules inside an unshared network namespace.

This module configures iptables rules within a sandbox network namespace.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import shutil
import socket
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache

from jiuwenbox.logging_config import configure_logging
from jiuwenbox.models.policy import (
    NetworkMode,
    NetworkPolicy,
    NetworkRulePolicy,
    NetworkUplinkPolicy,
)

configure_logging()
logger = logging.getLogger(__name__)

IP_BINARY = "ip"
IPTABLES_BINARY = "iptables"
IP6TABLES_BINARY = "ip6tables"
IPTABLES_LEGACY_BINARY = "iptables-legacy"
IP6TABLES_LEGACY_BINARY = "ip6tables-legacy"
IPTABLES_NFT_BINARY = "iptables-nft"
IP6TABLES_NFT_BINARY = "ip6tables-nft"


class NetworkSetupError(RuntimeError):
    """Raised when required network isolation setup fails."""


def _format_command_error(cmd: list[str], result: subprocess.CompletedProcess) -> str:
    details = [
        f"Command '{' '.join(cmd)}' failed with exit code {result.returncode}.",
    ]
    if result.stderr:
        details.append(f"stderr: {result.stderr.strip()}")
    if result.stdout:
        details.append(f"stdout: {result.stdout.strip()}")
    return " ".join(details)


@dataclass
class ResolvedNetworkRules:
    """Pre-resolved iptables rules ready to apply."""

    allowed_ips: list[str] = field(default_factory=list)
    blocked_ips: list[str] = field(default_factory=list)
    allowed_ports: list[int] = field(default_factory=list)
    blocked_ports: list[int] = field(default_factory=list)
    default_deny: bool = True


def resolve_domains(domains: list[str]) -> list[str]:
    """Resolve domain names to IP addresses.

    Supports wildcard domains like '*.example.com' by stripping the
    wildcard prefix and resolving the base domain.
    """
    ips: list[str] = []
    for domain in domains:
        # Strip wildcard prefix for resolution
        clean = domain.lstrip("*.")
        try:
            results = socket.getaddrinfo(clean, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
            for family, _, _, _, sockaddr in results:
                ip = sockaddr[0]
                if ip not in ips:
                    ips.append(ip)
            logger.debug("Resolved %s -> %s", domain, ips[-len(results):])
        except socket.gaierror:
            logger.warning("Failed to resolve domain: %s", domain)
    return ips


def normalize_ips(values: list[str]) -> list[str]:
    """Normalize IP/CIDR entries into iptables-ready values."""
    resolved: list[str] = []
    for value in values:
        try:
            normalized = str(ipaddress.ip_network(value, strict=False))
            if normalized not in resolved:
                resolved.append(normalized)
        except ValueError:
            logger.warning("Ignoring invalid IP/CIDR rule: %s", value)
    return resolved


def _rule_policy_is_empty(rule: NetworkRulePolicy) -> bool:
    """Return True when a rule policy defines no allow/block entries.

    Callers use this to decide whether the operator has opted out of all
    network restrictions. The ``default`` field (``deny``/``allow``) is not
    treated as a "rule" because it only matters once at least one
    allow/block entry is configured.
    """
    return not (
        rule.allowed_domains
        or rule.blocked_domains
        or rule.allowed_ips
        or rule.blocked_ips
        or rule.allowed_ports
        or rule.blocked_ports
    )


def policy_has_network_rules(policy: NetworkPolicy) -> bool:
    """Return True when the policy declares any explicit egress/ingress rule.

    A policy that only sets ``mode`` (and leaves both ``egress`` and
    ``ingress`` empty) is considered to have no rules. In that case the
    server runtime can skip iptables programming entirely, so hosts that
    do not ship ``iptables``/``iptables-nft`` still work.
    """
    return not (_rule_policy_is_empty(policy.egress) and _rule_policy_is_empty(policy.ingress))


def build_network_rules(policy: NetworkRulePolicy) -> ResolvedNetworkRules:
    """Resolve domains/IPs and build a direction-agnostic network rule set."""
    rules = ResolvedNetworkRules(
        default_deny=(policy.default == "deny"),
        allowed_ports=list(policy.allowed_ports),
        blocked_ports=list(policy.blocked_ports),
    )

    rules.blocked_ips = [
        *normalize_ips(policy.blocked_ips),
        *resolve_domains(policy.blocked_domains),
    ]
    rules.allowed_ips = [
        *normalize_ips(policy.allowed_ips),
        *resolve_domains(policy.allowed_domains),
    ]

    # Remove entries that appear in both allowed and blocked (blocked wins).
    blocked_set = set(rules.blocked_ips)
    rules.allowed_ips = [ip for ip in rules.allowed_ips if ip not in blocked_set]

    return rules


def _existing_binaries(paths: tuple[str, ...]) -> list[str]:
    result: list[str] = []
    for path in paths:
        if path in result:
            continue
        # ``shutil.which`` resolves both absolute paths and bare command
        # names through ``$PATH``. Filtering both keeps callers from trying
        # to ``exec`` a binary that simply is not installed on the host
        # (otherwise ``subprocess.run`` would raise ``FileNotFoundError``
        # later, which bypasses the ``NetworkSetupError`` handlers wired
        # up by the server runtime).
        if not shutil.which(path):
            continue
        result.append(path)
    return result


def _iptables_candidates(ip_version: int) -> list[str]:
    if ip_version == 6:
        return _existing_binaries((
            IP6TABLES_BINARY,
            IP6TABLES_NFT_BINARY,
            IP6TABLES_LEGACY_BINARY,
        ))
    return _existing_binaries((
        IPTABLES_BINARY,
        IPTABLES_NFT_BINARY,
        IPTABLES_LEGACY_BINARY,
    ))


def _run_iptables_binary(
    binary: str,
    args: list[str],
    *,
    check: bool = True,
    namespace: str | None = None,
) -> subprocess.CompletedProcess:
    cmd = [binary] + args
    if namespace:
        cmd = [IP_BINARY, "netns", "exec", namespace, *cmd]
    logger.debug("%s: %s", binary, " ".join(cmd))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        # ``iptables`` (or ``ip`` when running inside a netns) is not on
        # ``$PATH``. Surface this as a regular ``NetworkSetupError`` so the
        # graceful handlers in the server runtime can downgrade it to a
        # warning instead of crashing sandbox creation.
        raise NetworkSetupError(
            f"Required binary '{exc.filename or cmd[0]}' is not installed; "
            f"cannot run '{' '.join(cmd)}'."
        ) from exc
    if check and result.returncode != 0:
        raise NetworkSetupError(_format_command_error(cmd, result))
    return result


@lru_cache(maxsize=128)
def _select_iptables_binary(
    ip_version: int,
    namespace: str | None = None,
    table: str = "filter",
) -> str:
    """Return a working iptables backend for the target namespace.

    Some distributions default iptables to the nf_tables backend,
    while older or constrained kernels only support legacy xtables. Probe both
    and keep the sandbox creation failure explicit if neither backend works.
    """
    failures: list[str] = []
    probe_args = (
        ["-t", "nat", "-L", "POSTROUTING", "-n"]
        if table == "nat"
        else ["-L", "OUTPUT", "-n"]
    )
    for binary in _iptables_candidates(ip_version):
        result = _run_iptables_binary(
            binary,
            probe_args,
            check=False,
            namespace=namespace,
        )
        if result.returncode == 0:
            return binary
        failures.append(_format_command_error(
            [IP_BINARY, "netns", "exec", namespace, binary, *probe_args]
            if namespace
            else [binary, *probe_args],
            result,
        ))

    family = "IPv6" if ip_version == 6 else "IPv4"
    target = f" in netns {namespace}" if namespace else ""
    detail = " ".join(failures) if failures else "No iptables binary found."
    raise NetworkSetupError(f"No working {family} iptables {table} backend{target}. {detail}")


def run_iptables(
    args: list[str],
    check: bool = True,
    namespace: str | None = None,
    ip_version: int = 4,
) -> subprocess.CompletedProcess:
    """Run an iptables/ip6tables command, picking the available backend."""
    binary = _select_iptables_binary(ip_version, namespace)
    return _run_iptables_binary(binary, args, check=check, namespace=namespace)


def _ip_version(value: str) -> int:
    """Return 4 or 6 for an IP/CIDR rule value."""
    return ipaddress.ip_network(value, strict=False).version


def _run_iptables_for_ip(
    args: list[str],
    ip_value: str,
    check: bool = True,
    namespace: str | None = None,
) -> subprocess.CompletedProcess:
    """Run a firewall rule in the table matching the IP/CIDR version."""
    ip_version = _ip_version(ip_value)
    if ip_version == 6 and not _ip6tables_available(namespace):
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    return run_iptables(
        args,
        check=check,
        namespace=namespace,
        ip_version=ip_version,
    )


def _run_iptables_both(
    args: list[str],
    check: bool = True,
    namespace: str | None = None,
) -> None:
    """Run a protocol-agnostic firewall rule for both IPv4 and IPv6."""
    run_iptables(args, check=check, namespace=namespace, ip_version=4)
    if _ip6tables_available(namespace):
        run_iptables(args, check=check, namespace=namespace, ip_version=6)


@lru_cache(maxsize=128)
def _ip6tables_available(namespace: str | None = None) -> bool:
    """Return whether ip6tables can manage rules in the target namespace."""
    try:
        result = run_iptables(
            ["-L", "OUTPUT", "-n"],
            check=False,
            namespace=namespace,
            ip_version=6,
        )
    except (OSError, NetworkSetupError) as exc:
        logger.warning("Skipping IPv6 firewall rules because ip6tables is unavailable: %s", exc)
        return False

    if result.returncode == 0:
        return True

    target = f" in netns {namespace}" if namespace else ""
    stderr = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
    logger.warning("Skipping IPv6 firewall rules%s because ip6tables is unavailable: %s", target, stderr)
    return False


def _run_ip(
    args: list[str],
    check: bool = True,
    namespace: str | None = None,
) -> subprocess.CompletedProcess:
    """Run an ip command."""
    cmd = [IP_BINARY] + args
    if namespace:
        cmd = [IP_BINARY, "netns", "exec", namespace, *cmd]
    logger.debug("ip: %s", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


NETNS_NAME_PREFIX = "jbx-"


def netns_name_for_sandbox(sandbox_id: str) -> str:
    """Return the deterministic network namespace name for a sandbox."""
    return f"{NETNS_NAME_PREFIX}{sandbox_id}"


def namespace_exists(namespace: str) -> bool:
    """Check whether a named network namespace already exists."""
    result = subprocess.run(
        [IP_BINARY, "netns", "list"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return False

    return any(line.split(maxsplit=1)[0] == namespace for line in result.stdout.splitlines())


def create_named_namespace(namespace: str) -> None:
    """Create a persistent named network namespace."""
    subprocess.run([IP_BINARY, "netns", "add", namespace], check=True, capture_output=True, text=True)


def delete_named_namespace(namespace: str) -> None:
    """Delete a persistent named network namespace."""
    subprocess.run(
        [IP_BINARY, "netns", "delete", namespace],
        check=True,
        capture_output=True,
        text=True,
    )


def setup_loopback(namespace: str | None = None) -> None:
    """Bring up the loopback interface inside the network namespace."""
    _run_ip(["link", "set", "lo", "up"], namespace=namespace)


@dataclass
class UplinkHandle:
    host_if: str
    sandbox_if: str
    subnet: str
    uplink_if: str
    nat: bool
    management_ports: list[int] = field(default_factory=list)
    forward_rules: list[list[str]] = field(default_factory=list)


_nat_refcounts: dict[tuple[str, str], int] = {}
_mgmt_refcounts: dict[tuple[str, int], int] = {}
# Preferred pools for auto-selection; each sandbox receives a /30 inside the first pool
# that yields a block not overlapping existing routes.
_UPLINK_DEFAULT_POOLS: tuple[str, ...] = (
    "100.64.0.0/10",
    "10.200.0.0/16",
    "10.201.0.0/16",
    "10.202.0.0/16",
    "10.203.0.0/16",
    "172.30.0.0/16",
    "172.31.0.0/16",
    "192.168.240.0/20",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
)
_LINK_LOCAL_NETWORK = ipaddress.ip_network("169.254.0.0/16")
_MAX_UPLINK_BLOCK_SCAN = 4096
_UPLINK_BLOCK_PREFIX = 30
_SKIP_ROUTE_PREFIXES = frozenset({
    "default",
    "unreachable",
    "blackhole",
    "prohibit",
    "throw",
    "local",
    "broadcast",
    "multicast",
    "anycast",
    "nexthop",
})


def _interface_names(sandbox_id: str) -> tuple[str, str]:
    digest = hashlib.sha256(sandbox_id.encode()).hexdigest()[:8]
    return f"jwbH{digest}", f"jwbS{digest}"


def _route_networks() -> list[ipaddress.IPv4Network]:
    result = subprocess.run(
        [IP_BINARY, "-4", "route", "show", "table", "all"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise NetworkSetupError("Failed to inspect IPv4 routes for uplink subnet selection")

    networks: list[ipaddress.IPv4Network] = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if not parts or parts[0] in _SKIP_ROUTE_PREFIXES:
            continue
        try:
            networks.append(ipaddress.ip_network(parts[0], strict=False))
        except ValueError:
            continue
    return networks


def _resolve_uplink_pools(subnet: str) -> list[ipaddress.IPv4Network]:
    if subnet.strip():
        pool = ipaddress.ip_network(subnet.strip(), strict=False)
        if pool.prefixlen > _UPLINK_BLOCK_PREFIX:
            raise NetworkSetupError(
                f"Uplink subnet {subnet} is too small; need prefix length /{_UPLINK_BLOCK_PREFIX} or shorter"
            )
        return [pool]

    return [ipaddress.ip_network(candidate, strict=False) for candidate in _UPLINK_DEFAULT_POOLS]


def _uplink_search_region(
    pool: ipaddress.IPv4Network,
    sandbox_id: str,
    slot: int,
) -> ipaddress.IPv4Network:
    """Narrow large pools to a /16-sized window before scanning /30 blocks."""
    if pool.prefixlen >= 16:
        return pool

    hash_val = int(hashlib.sha256(f"{sandbox_id}:{slot}".encode()).hexdigest(), 16)
    slash16_subnets = list(pool.subnets(new_prefix=16))
    return slash16_subnets[hash_val % len(slash16_subnets)]


def _is_usable_uplink_block(
    block: ipaddress.IPv4Network,
    pool: ipaddress.IPv4Network,
    routes: list[ipaddress.IPv4Network],
) -> bool:
    if block.prefixlen != _UPLINK_BLOCK_PREFIX:
        return False
    if not block.subnet_of(pool):
        return False
    if block.overlaps(_LINK_LOCAL_NETWORK):
        return False
    return not any(block.overlaps(route) for route in routes)


def _allocate_uplink_block(
    sandbox_id: str,
    subnet: str,
    slot: int = 0,
) -> tuple[ipaddress.IPv4Address, ipaddress.IPv4Address, int, str]:
    """Pick a /30 block and return gateway, sandbox IP, prefix length, and block CIDR."""
    pools = _resolve_uplink_pools(subnet)
    routes = _route_networks()

    for pool in pools:
        region = _uplink_search_region(pool, sandbox_id, slot)
        blocks = list(region.subnets(new_prefix=_UPLINK_BLOCK_PREFIX))
        if not blocks:
            continue

        hash_val = int(hashlib.sha256(f"{sandbox_id}:{slot}".encode()).hexdigest(), 16)
        start_idx = hash_val % len(blocks)
        scan_limit = min(len(blocks), _MAX_UPLINK_BLOCK_SCAN)
        for offset in range(scan_limit):
            block = blocks[(start_idx + offset) % len(blocks)]
            if not _is_usable_uplink_block(block, pool, routes):
                continue
            gateway = block.network_address + 1
            sandbox_ip = block.network_address + 2
            return gateway, sandbox_ip, _UPLINK_BLOCK_PREFIX, str(block)

    raise NetworkSetupError(
        f"No available /{_UPLINK_BLOCK_PREFIX} IPv4 block for sandbox uplink "
        f"(sandbox_id={sandbox_id!r})"
    )


def resolve_uplink_subnet(subnet: str) -> str:
    """Return the configured uplink pool, or the first default pool when auto-selecting."""
    return str(_resolve_uplink_pools(subnet)[0])


def resolve_uplink_interface(interface: str) -> str:
    if interface.strip():
        return interface.strip()
    result = subprocess.run(
        [IP_BINARY, "route", "show", "default"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise NetworkSetupError("Failed to inspect default route for uplink interface")
    for line in result.stdout.splitlines():
        parts = line.split()
        if "dev" in parts:
            return parts[parts.index("dev") + 1]
    raise NetworkSetupError("No default route interface found for uplink")


def ensure_ip_forward() -> None:
    path = "/proc/sys/net/ipv4/ip_forward"
    try:
        with open(path, "w", encoding="ascii") as handle:
            handle.write("1\n")
    except OSError as exc:
        raise NetworkSetupError(f"Failed to enable IPv4 forwarding at {path}") from exc


def run_iptables_nat(
    args: list[str],
    *,
    check: bool = True,
    namespace: str | None = None,
    ip_version: int = 4,
) -> subprocess.CompletedProcess:
    binary = _select_iptables_binary(ip_version, namespace, table="nat")
    return _run_iptables_binary(
        binary,
        ["-t", "nat", *args],
        check=check,
        namespace=namespace,
    )


def _iptables_rule_exists(
    args: list[str],
    *,
    namespace: str | None = None,
    ip_version: int = 4,
    table: str | None = None,
) -> bool:
    check_args = ["-C", *args]
    if table == "nat":
        result = run_iptables_nat(check_args, check=False, namespace=namespace, ip_version=ip_version)
    else:
        result = run_iptables(check_args, check=False, namespace=namespace, ip_version=ip_version)
    return result.returncode == 0


def _ensure_iptables_rule(
    args: list[str],
    *,
    namespace: str | None = None,
    ip_version: int = 4,
    table: str | None = None,
) -> None:
    if _iptables_rule_exists(args, namespace=namespace, ip_version=ip_version, table=table):
        return
    insert_args = ["-I", *args]
    if table == "nat":
        run_iptables_nat(insert_args, namespace=namespace, ip_version=ip_version)
    else:
        run_iptables(insert_args, namespace=namespace, ip_version=ip_version)


def _remove_iptables_rule(
    args: list[str],
    *,
    namespace: str | None = None,
    ip_version: int = 4,
    table: str | None = None,
) -> None:
    delete_args = ["-D", *args]
    if table == "nat":
        run_iptables_nat(delete_args, check=False, namespace=namespace, ip_version=ip_version)
    else:
        run_iptables(delete_args, check=False, namespace=namespace, ip_version=ip_version)


def _acquire_nat(subnet: str, uplink_if: str) -> None:
    key = (subnet, uplink_if)
    if _nat_refcounts.get(key, 0) == 0:
        _ensure_iptables_rule(
            ["POSTROUTING", "-s", subnet, "-o", uplink_if, "-j", "MASQUERADE"],
            table="nat",
        )
    _nat_refcounts[key] = _nat_refcounts.get(key, 0) + 1


def _release_nat(subnet: str, uplink_if: str) -> None:
    key = (subnet, uplink_if)
    count = _nat_refcounts.get(key, 0)
    if count <= 1:
        _nat_refcounts.pop(key, None)
        _remove_iptables_rule(
            ["POSTROUTING", "-s", subnet, "-o", uplink_if, "-j", "MASQUERADE"],
            table="nat",
        )
        return
    _nat_refcounts[key] = count - 1


def _acquire_management_port_block(subnet: str, port: int) -> None:
    key = (subnet, port)
    if _mgmt_refcounts.get(key, 0) == 0:
        _ensure_iptables_rule(
            ["INPUT", "-s", subnet, "-p", "tcp", "--dport", str(port), "-j", "REJECT"],
        )
    _mgmt_refcounts[key] = _mgmt_refcounts.get(key, 0) + 1


def _release_management_port_block(subnet: str, port: int) -> None:
    key = (subnet, port)
    count = _mgmt_refcounts.get(key, 0)
    if count <= 1:
        _mgmt_refcounts.pop(key, None)
        _remove_iptables_rule(
            ["INPUT", "-s", subnet, "-p", "tcp", "--dport", str(port), "-j", "REJECT"],
        )
        return
    _mgmt_refcounts[key] = count - 1


def setup_network_uplink(
    namespace: str,
    sandbox_id: str,
    uplink: NetworkUplinkPolicy,
    *,
    management_ports: list[int] | None = None,
) -> UplinkHandle:
    """Attach a veth uplink and optional NAT for an isolated sandbox netns."""
    ensure_ip_forward()
    uplink_if = resolve_uplink_interface(uplink.interface)
    host_if, sandbox_if = _interface_names(sandbox_id)
    last_error: Exception | None = None
    subnet = ""

    for slot in range(64):
        gateway, sandbox_ip, prefix_len, subnet = _allocate_uplink_block(
            sandbox_id,
            uplink.subnet,
            slot,
        )
        host_addr = f"{gateway}/{prefix_len}"
        sandbox_addr = f"{sandbox_ip}/{prefix_len}"
        try:
            _run_ip(["link", "del", host_if], check=False)
            _run_ip([
                "link", "add", host_if, "type", "veth",
                "peer", "name", sandbox_if, "netns", namespace,
            ])
            _run_ip(["addr", "add", host_addr, "dev", host_if])
            _run_ip(["link", "set", host_if, "up"])
            _run_ip(["addr", "add", sandbox_addr, "dev", sandbox_if], namespace=namespace)
            _run_ip(["link", "set", sandbox_if, "up"], namespace=namespace)
            _run_ip([
                "route", "add", "default", "via", str(gateway), "dev", sandbox_if,
            ], namespace=namespace)
            break
        except (subprocess.CalledProcessError, NetworkSetupError) as exc:
            last_error = exc
            _run_ip(["link", "del", host_if], check=False)
            continue
    else:
        raise NetworkSetupError(
            f"Failed to allocate uplink addresses for sandbox {sandbox_id}: {last_error}"
        ) from last_error

    forward_rules = [
        ["FORWARD", "-i", host_if, "-j", "ACCEPT"],
        [
            "FORWARD", "-o", host_if,
            "-m", "state", "--state", "RELATED,ESTABLISHED", "-j", "ACCEPT",
        ],
    ]
    applied_forward_rules: list[list[str]] = []
    nat_acquired = False
    protected_ports: list[int] = []
    try:
        for rule in forward_rules:
            _ensure_iptables_rule(rule)
            applied_forward_rules.append(rule)

        if uplink.nat:
            _acquire_nat(subnet, uplink_if)
            nat_acquired = True

        for port in management_ports or []:
            _acquire_management_port_block(subnet, port)
            protected_ports.append(port)
    except Exception:
        for port in reversed(protected_ports):
            _release_management_port_block(subnet, port)
        if nat_acquired:
            _release_nat(subnet, uplink_if)
        for rule in reversed(applied_forward_rules):
            _remove_iptables_rule(rule)
        _run_ip(["link", "del", host_if], check=False)
        raise

    logger.info(
        "Network uplink applied for sandbox %s: host_if=%s sandbox_if=%s subnet=%s uplink_if=%s nat=%s",
        sandbox_id,
        host_if,
        sandbox_if,
        subnet,
        uplink_if,
        uplink.nat,
    )
    return UplinkHandle(
        host_if=host_if,
        sandbox_if=sandbox_if,
        subnet=subnet,
        uplink_if=uplink_if,
        nat=uplink.nat,
        management_ports=protected_ports,
        forward_rules=applied_forward_rules,
    )


def teardown_network_uplink(handle: UplinkHandle) -> None:
    for rule in reversed(handle.forward_rules):
        _remove_iptables_rule(rule)
    if handle.nat:
        _release_nat(handle.subnet, handle.uplink_if)
    for port in handle.management_ports:
        _release_management_port_block(handle.subnet, port)
    _run_ip(["link", "del", handle.host_if], check=False)


def _apply_egress_rules(rules: ResolvedNetworkRules, namespace: str | None = None) -> None:
    """Apply outbound network rules inside the current namespace."""
    _run_iptables_both(["-A", "OUTPUT", "-o", "lo", "-j", "ACCEPT"], namespace=namespace)
    # Allow established/related connections
    _run_iptables_both(["-A", "OUTPUT", "-m", "state", "--state",
                        "ESTABLISHED,RELATED", "-j", "ACCEPT"], namespace=namespace)

    for port in rules.blocked_ports:
        _run_iptables_both(
            ["-A", "OUTPUT", "-p", "tcp", "--dport", str(port), "-j", "DROP"],
            namespace=namespace,
        )

    if not rules.default_deny:
        for ip in rules.blocked_ips:
            _run_iptables_for_ip(["-A", "OUTPUT", "-d", ip, "-j", "DROP"], ip, namespace=namespace)
        return

    # Allow DNS (needed for domain resolution within the sandbox)
    _run_iptables_both(["-A", "OUTPUT", "-p", "udp", "--dport", "53", "-j", "ACCEPT"], namespace=namespace)
    _run_iptables_both(["-A", "OUTPUT", "-p", "tcp", "--dport", "53", "-j", "ACCEPT"], namespace=namespace)

    # Block explicitly blocked IPs first (higher priority)
    for ip in rules.blocked_ips:
        _run_iptables_for_ip(["-A", "OUTPUT", "-d", ip, "-j", "DROP"], ip, namespace=namespace)

    # Allow traffic to resolved IPs on allowed ports
    for ip in rules.allowed_ips:
        if rules.allowed_ports:
            for port in rules.allowed_ports:
                _run_iptables_for_ip([
                    "-A", "OUTPUT", "-d", ip, "-p", "tcp",
                    "--dport", str(port), "-j", "ACCEPT",
                ], ip, namespace=namespace)
        else:
            _run_iptables_for_ip(["-A", "OUTPUT", "-d", ip, "-j", "ACCEPT"], ip, namespace=namespace)

    # Default drop for everything else
    _run_iptables_both(["-A", "OUTPUT", "-j", "DROP"], namespace=namespace)

    logger.info(
        "Network egress rules applied: %d allowed IPs, %d blocked IPs, allowed ports %s, blocked ports %s",
        len(rules.allowed_ips), len(rules.blocked_ips), rules.allowed_ports, rules.blocked_ports,
    )


def _apply_ingress_rules(rules: ResolvedNetworkRules, namespace: str | None = None) -> None:
    """Apply inbound network rules inside the current namespace."""
    _run_iptables_both(["-A", "INPUT", "-m", "state", "--state",
                        "ESTABLISHED,RELATED", "-j", "ACCEPT"], namespace=namespace)

    for ip in rules.blocked_ips:
        _run_iptables_for_ip(["-A", "INPUT", "-s", ip, "-j", "DROP"], ip, namespace=namespace)
    for port in rules.blocked_ports:
        _run_iptables_both(
            ["-A", "INPUT", "-p", "tcp", "--dport", str(port), "-j", "DROP"],
            namespace=namespace,
        )

    if not rules.default_deny:
        _run_iptables_both(["-A", "INPUT", "-j", "ACCEPT"], namespace=namespace)
        return

    if rules.allowed_ips and rules.allowed_ports:
        for ip in rules.allowed_ips:
            for port in rules.allowed_ports:
                _run_iptables_for_ip([
                    "-A", "INPUT", "-s", ip, "-p", "tcp",
                    "--dport", str(port), "-j", "ACCEPT",
                ], ip, namespace=namespace)
    elif rules.allowed_ips:
        for ip in rules.allowed_ips:
            _run_iptables_for_ip(["-A", "INPUT", "-s", ip, "-j", "ACCEPT"], ip, namespace=namespace)
    elif rules.allowed_ports:
        for port in rules.allowed_ports:
            _run_iptables_both(
                ["-A", "INPUT", "-p", "tcp", "--dport", str(port), "-j", "ACCEPT"],
                namespace=namespace,
            )

    _run_iptables_both(["-A", "INPUT", "-j", "DROP"], namespace=namespace)

    logger.info(
        "Network ingress rules applied: %d allowed IPs, %d blocked IPs, allowed ports %s, blocked ports %s",
        len(rules.allowed_ips),
        len(rules.blocked_ips),
        rules.allowed_ports,
        rules.blocked_ports,
    )


def apply_iptables_rules(
    egress_rules: ResolvedNetworkRules,
    ingress_rules: ResolvedNetworkRules,
    namespace: str | None = None,
) -> None:
    """Apply iptables rules inside the current (unshared) network namespace."""
    setup_loopback(namespace=namespace)
    _apply_egress_rules(egress_rules, namespace=namespace)
    _apply_ingress_rules(ingress_rules, namespace=namespace)


def setup_network_isolation(policy: NetworkPolicy, namespace: str | None = None) -> None:
    """Top-level entry point for network isolation setup.

    Called from within the sandbox namespace after bwrap --unshare-net or
    from the host against a pre-created named namespace.
    """
    if policy.mode == NetworkMode.HOST:
        logger.info("Network mode is 'host', skipping isolation")
        return

    egress_rules = build_network_rules(policy.egress)
    ingress_rules = build_network_rules(policy.ingress)
    apply_iptables_rules(egress_rules, ingress_rules, namespace=namespace)
