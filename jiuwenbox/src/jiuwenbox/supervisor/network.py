# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Network isolation via iptables rules inside an unshared network namespace.

This module configures iptables rules within a sandbox network namespace.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import ipaddress
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
from dataclasses import dataclass, field
from functools import lru_cache

from jiuwenbox import perf_trace
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
# Seconds passed to iptables ``-w`` / iptables-restore ``--wait``. Unset means
# the default; a bounded wait keeps xtables lock contention from failing
# sandbox creation without hanging the create path forever.
ENV_IPTABLES_WAIT_SECONDS = "JIUWENBOX_IPTABLES_WAIT_SECONDS"
DEFAULT_IPTABLES_WAIT_SECONDS = 5


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


_domain_ip_cache: dict[str, tuple[str, ...]] = {}


def _resolve_one_domain(domain: str) -> tuple[str, ...]:
    """Resolve one domain, reusing answers for later sandboxes."""
    clean = domain.lstrip("*.")
    cached = _domain_ip_cache.get(clean)
    if cached is not None:
        return cached
    found: list[str] = []
    try:
        results = socket.getaddrinfo(clean, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror:
        logger.warning("Failed to resolve domain: %s", domain)
        _domain_ip_cache[clean] = ()
        return ()
    for _family, _, _, _, sockaddr in results:
        ip = sockaddr[0]
        if ip not in found:
            found.append(ip)
    resolved = tuple(found)
    _domain_ip_cache[clean] = resolved
    logger.debug("Resolved %s -> %s", domain, resolved)
    return resolved


def resolve_domains(domains: list[str]) -> list[str]:
    """Resolve domain names to IP addresses.

    Supports wildcard domains like '*.example.com' by stripping the
    wildcard prefix and resolving the base domain.
    """
    ips: list[str] = []
    for domain in domains:
        for ip in _resolve_one_domain(domain):
            if ip not in ips:
                ips.append(ip)
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
    # ``default: allow`` never installs allowed_* entries. Skip that DNS
    # work so sandbox create does not block on unused domain lookups.
    if rules.default_deny:
        rules.allowed_ips = [
            *normalize_ips(policy.allowed_ips),
            *resolve_domains(policy.allowed_domains),
        ]
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


def _iptables_wait_seconds() -> int:
    """Return how long iptables should wait for ``/run/xtables.lock``.

    A missing or blank ``JIUWENBOX_IPTABLES_WAIT_SECONDS`` uses the default.
    Non-integer or negative values also fall back, so a bad setting does not
    fail sandbox creation.
    """
    raw = os.environ.get(ENV_IPTABLES_WAIT_SECONDS)
    if raw is None or not raw.strip():
        return DEFAULT_IPTABLES_WAIT_SECONDS
    try:
        value = int(raw.strip())
    except ValueError:
        logger.warning(
            "%s=%r is not an integer; using default %ds",
            ENV_IPTABLES_WAIT_SECONDS,
            raw,
            DEFAULT_IPTABLES_WAIT_SECONDS,
        )
        return DEFAULT_IPTABLES_WAIT_SECONDS
    if value < 0:
        logger.warning(
            "%s=%r must be >= 0; using default %ds",
            ENV_IPTABLES_WAIT_SECONDS,
            raw,
            DEFAULT_IPTABLES_WAIT_SECONDS,
        )
        return DEFAULT_IPTABLES_WAIT_SECONDS
    return value


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
    cmd = [binary, "-w", str(_iptables_wait_seconds()), *args]
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


@lru_cache(maxsize=8)
def _cached_iptables_binary(ip_version: int, table: str) -> str:
    """Probe iptables once per process on the host.

    The selected binary is a PATH entry. Re-probing inside every new
    netns repeats ``ip netns exec`` without changing the answer.
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
            namespace=None,
        )
        if result.returncode == 0:
            return binary
        failures.append(_format_command_error([binary, *probe_args], result))

    family = "IPv6" if ip_version == 6 else "IPv4"
    detail = " ".join(failures) if failures else "No iptables binary found."
    raise NetworkSetupError(f"No working {family} iptables {table} backend. {detail}")


def _select_iptables_binary(
    ip_version: int,
    namespace: str | None = None,
    table: str = "filter",
) -> str:
    """Return a working iptables backend.

    ``namespace`` is accepted for existing callers. Selection itself runs
    on the host and is cached for the process.
    """
    del namespace
    return _cached_iptables_binary(ip_version, table)


_select_iptables_binary.cache_clear = _cached_iptables_binary.cache_clear  # type: ignore[attr-defined]


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


def _ip6tables_available(namespace: str | None = None) -> bool:
    """Return whether ip6tables works. Probed once on the host."""
    del namespace
    return _ip6tables_available_on_host()


@lru_cache(maxsize=1)
def _ip6tables_available_on_host() -> bool:
    """Return whether the host ip6tables backend can list the filter table."""
    try:
        result = run_iptables(
            ["-L", "OUTPUT", "-n"],
            check=False,
            namespace=None,
            ip_version=6,
        )
    except (OSError, NetworkSetupError) as exc:
        logger.warning("Skipping IPv6 firewall rules because ip6tables is unavailable: %s", exc)
        return False

    if result.returncode == 0:
        return True

    stderr = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
    logger.warning("Skipping IPv6 firewall rules because ip6tables is unavailable: %s", stderr)
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


def _run_ip_batch(lines: list[str], *, namespace: str | None = None) -> None:
    """Apply several ``ip`` commands in one process."""
    if not lines:
        return
    cmd = [IP_BINARY]
    if namespace:
        cmd.extend(["-n", namespace])
    cmd.extend(["-batch", "-"])
    script = "\n".join(lines) + "\n"
    logger.debug("ip batch namespace=%s: %s", namespace or "host", " ; ".join(lines))
    try:
        result = subprocess.run(
            cmd,
            input=script,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise NetworkSetupError(
            f"Required binary '{exc.filename or IP_BINARY}' is not installed."
        ) from exc
    if result.returncode != 0:
        raise NetworkSetupError(_format_command_error(cmd, result))


def _iptables_restore_binary(ip_version: int) -> str | None:
    binary = _select_iptables_binary(ip_version, table="filter")
    return shutil.which(f"{os.path.basename(binary)}-restore")


def _can_iptables_restore(ip_version: int) -> bool:
    try:
        return _iptables_restore_binary(ip_version) is not None
    except NetworkSetupError:
        return False


def _restore_script(tables: dict[str, list[str]]) -> str:
    parts: list[str] = []
    for table, lines in tables.items():
        if not lines:
            continue
        parts.append(f"*{table}")
        parts.extend(lines)
        parts.append("COMMIT")
    if not parts:
        return ""
    return "\n".join(parts) + "\n"


def _run_iptables_restore(
    script: str,
    *,
    ip_version: int,
    namespace: str | None,
    noflush: bool = False,
) -> None:
    """Program a ruleset with one iptables-restore process."""
    if not script:
        return
    binary = _iptables_restore_binary(ip_version)
    if binary is None:
        raise NetworkSetupError(f"No iptables-restore binary for IPv{ip_version}")
    cmd = [binary, "--wait", str(_iptables_wait_seconds())]
    if noflush:
        cmd.append("--noflush")
    if namespace:
        cmd = [IP_BINARY, "netns", "exec", namespace, *cmd]
    logger.debug("iptables-restore ipv%s namespace=%s", ip_version, namespace or "host")
    try:
        result = subprocess.run(
            cmd,
            input=script,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise NetworkSetupError(
            f"Required binary '{exc.filename or cmd[0]}' is not installed."
        ) from exc
    if result.returncode != 0:
        raise NetworkSetupError(_format_command_error(cmd, result))


NETNS_NAME_PREFIX = "jbx-"
# Host/sandbox veth names are ``jwbH`` / ``jwbS`` plus sha256(sandbox_id)[:8].
_JWB_VETH_RE = re.compile(r"^jwb[HS][0-9a-f]{8}$")
# Set to a non-empty value to skip the startup/shutdown orphan sweep. Intended
# for tests that must not touch the host network namespace.
ENV_SKIP_ORPHAN_NET_CLEANUP = "JIUWENBOX_SKIP_ORPHAN_NET_CLEANUP"


def netns_name_for_sandbox(sandbox_id: str) -> str:
    """Return the deterministic network namespace name for a sandbox."""
    return f"{NETNS_NAME_PREFIX}{sandbox_id}"


def namespace_exists(namespace: str) -> bool:
    """Check whether a named network namespace already exists.

    ``ip netns`` keeps one file per namespace under ``/run/netns``. A stat
    avoids listing every namespace on each sandbox create.
    """
    for root in ("/run/netns", "/var/run/netns"):
        if os.path.exists(os.path.join(root, namespace)):
            return True
    return False


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


def rename_named_namespace(old: str, new: str) -> None:
    """Point ``new`` at the live namespace currently named ``old``.

    Processes already joined to the namespace keep their network. ``ip netns``
    tracks names as bind mounts under ``/run/netns``, so the rename bind-mounts
    that file onto the new name and drops the old mount.
    """
    if old == new:
        return
    old_path = ""
    for root in ("/run/netns", "/var/run/netns"):
        candidate = os.path.join(root, old)
        if os.path.exists(candidate):
            old_path = candidate
            break
    if not old_path:
        raise NetworkSetupError(f"network namespace {old!r} does not exist")
    new_path = os.path.join(os.path.dirname(old_path), new)
    if os.path.exists(new_path):
        delete_named_namespace(new)
    descriptor = os.open(new_path, os.O_CREAT | os.O_RDWR, 0o644)
    os.close(descriptor)
    try:
        subprocess.run(
            ["/bin/mount", "--bind", old_path, new_path],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["/bin/umount", old_path],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise NetworkSetupError(
            f"failed to rename network namespace {old!r} to {new!r}: {exc.stderr}"
        ) from exc
    try:
        os.remove(old_path)
    except OSError:
        logger.debug("Leftover netns name %s after rename", old_path, exc_info=True)


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
    sandbox_ip: str
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


_cached_routes: list[ipaddress.IPv4Network] | None = None
_route_lock = threading.Lock()


def _invalidate_route_cache() -> None:
    global _cached_routes
    with _route_lock:
        _cached_routes = None


def _remember_route(subnet: str) -> None:
    network = ipaddress.ip_network(subnet, strict=False)
    with _route_lock:
        routes = _load_route_cache()
        if network not in routes:
            routes.append(network)


def _load_route_cache() -> list[ipaddress.IPv4Network]:
    """Return the process-local route cache. Caller holds ``_route_lock``."""
    global _cached_routes
    if _cached_routes is not None:
        return _cached_routes
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
    _cached_routes = networks
    return networks


def _route_networks() -> list[ipaddress.IPv4Network]:
    with _route_lock:
        return list(_load_route_cache())


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
        if region.prefixlen > _UPLINK_BLOCK_PREFIX:
            continue
        block_count = 1 << (_UPLINK_BLOCK_PREFIX - region.prefixlen)
        if block_count <= 0:
            continue

        hash_val = int(hashlib.sha256(f"{sandbox_id}:{slot}".encode()).hexdigest(), 16)
        start_idx = hash_val % block_count
        scan_limit = min(block_count, _MAX_UPLINK_BLOCK_SCAN)
        host_bits = 32 - _UPLINK_BLOCK_PREFIX
        region_base = int(region.network_address)
        for offset in range(scan_limit):
            index = (start_idx + offset) % block_count
            block = ipaddress.IPv4Network((
                region_base + (index << host_bits),
                _UPLINK_BLOCK_PREFIX,
            ))
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


_cached_uplink_interface: str | None = None


def resolve_uplink_interface(interface: str) -> str:
    if interface.strip():
        return interface.strip()
    global _cached_uplink_interface
    if _cached_uplink_interface:
        return _cached_uplink_interface
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
            _cached_uplink_interface = parts[parts.index("dev") + 1]
            return _cached_uplink_interface
    raise NetworkSetupError("No default route interface found for uplink")


def _is_usable_egress_ipv4(value: str) -> bool:
    try:
        address = ipaddress.IPv4Address(value)
    except ValueError:
        return False
    return not (address.is_loopback or address.is_unspecified or address.is_link_local)


def resolve_host_egress_ipv4() -> str | None:
    """Best-effort IPv4 egress address for the current network namespace.

    Prefer the kernel-selected ``src`` from ``ip -4 route get 1.1.1.1``.
    Fall back to the first usable global address when route lookup fails.
    Returns ``None`` when no usable address can be resolved.
    """
    try:
        route = subprocess.run(
            [IP_BINARY, "-4", "route", "get", "1.1.1.1"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        logger.debug("Failed to resolve host egress IPv4 via route get: %s", exc)
        route = None

    if route is not None and route.returncode == 0:
        parts = route.stdout.split()
        if "src" in parts:
            candidate = parts[parts.index("src") + 1]
            if _is_usable_egress_ipv4(candidate):
                return candidate

    try:
        addresses = subprocess.run(
            [IP_BINARY, "-4", "-o", "addr", "show", "scope", "global"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        logger.debug("Failed to list host global IPv4 addresses: %s", exc)
        return None

    if addresses.returncode != 0:
        logger.debug(
            "Failed to list host global IPv4 addresses: %s",
            addresses.stderr.strip() or addresses.stdout.strip(),
        )
        return None

    for line in addresses.stdout.splitlines():
        parts = line.split()
        if "inet" not in parts:
            continue
        inet_index = parts.index("inet")
        if inet_index + 1 >= len(parts):
            continue
        candidate = parts[inet_index + 1].split("/", 1)[0]
        if _is_usable_egress_ipv4(candidate):
            return candidate

    logger.debug("No usable host egress IPv4 address found")
    return None


# runc bind-mounts /proc/sys read-only (readonlyPaths). Clearing that list
# needs Docker's ``systempaths=unconfined``, which is absent before Docker 25.
# A read-only proc sysctl still accepts the value set by ``docker run --sysctl``.
_PROC_SYS = "/proc/sys"
_IP_FORWARD = "/proc/sys/net/ipv4/ip_forward"
_MS_REMOUNT = 32
_MS_BIND = 4096
_proc_sys_lock = threading.Lock()
_proc_sys_writable = False


def reset_proc_sys_mount_state() -> None:
    """Forget a previous successful /proc/sys remount. Used by tests."""
    global _proc_sys_writable
    _proc_sys_writable = False


def ip_forward_path() -> str:
    """Path written when IPv4 forwarding is still off."""
    return _IP_FORWARD


def _ip_forward_enabled(path: str = _IP_FORWARD) -> bool:
    try:
        with open(path, encoding="ascii") as handle:
            return handle.read().strip() == "1"
    except OSError:
        return False


def _write_ip_forward(path: str = _IP_FORWARD) -> None:
    with open(path, "w", encoding="ascii") as handle:
        handle.write("1\n")


def _remount_proc_sys_rw() -> None:
    """Undo the runtime's read-only bind of /proc/sys. Once per process."""
    global _proc_sys_writable
    with _proc_sys_lock:
        if _proc_sys_writable:
            return
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.mount.argtypes = [
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_ulong,
            ctypes.c_void_p,
        ]
        libc.mount.restype = ctypes.c_int
        target = _PROC_SYS.encode()
        # MS_BIND|MS_REMOUNT clears the read-only flag on this bind mount only.
        if libc.mount(target, target, None, _MS_REMOUNT | _MS_BIND, None) != 0:
            err = ctypes.get_errno()
            raise NetworkSetupError(
                f"Failed to remount {_PROC_SYS} read-write: {os.strerror(err)}"
            )
        _proc_sys_writable = True


def ensure_ip_forward() -> None:
    """Turn on IPv4 forwarding in the current netns.

    When ``--sysctl net.ipv4.ip_forward=1`` already set the value, this is a
    single read. The write path only runs if forwarding is still off, and a
    read-only ``/proc/sys`` is remounted once before that write.
    """
    if _ip_forward_enabled():
        return
    try:
        _write_ip_forward()
        return
    except OSError as exc:
        if exc.errno != errno.EROFS:
            raise NetworkSetupError(f"Failed to enable IPv4 forwarding at {_IP_FORWARD}") from exc
    _remount_proc_sys_rw()
    try:
        _write_ip_forward()
    except OSError as exc:
        raise NetworkSetupError(f"Failed to enable IPv4 forwarding at {_IP_FORWARD}") from exc


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


_host_firewall_lock = threading.Lock()


def _insert_line(args: list[str]) -> str:
    return "-I " + " ".join(args)


def apply_uplink_firewall(handle: UplinkHandle) -> None:
    """Install host FORWARD, NAT, and management-port rules for one uplink.

    New rules go in through one ``iptables-restore --noflush`` when that
    binary exists. Refcounts match the previous per-rule insert path.
    """
    ports = list(handle.management_ports)
    with _host_firewall_lock:
        if not _can_iptables_restore(4):
            for rule in handle.forward_rules:
                _ensure_iptables_rule(rule)
            if handle.nat:
                _acquire_nat(handle.subnet, handle.uplink_if)
            for port in ports:
                _acquire_management_port_block(handle.subnet, port)
            return

        filter_lines = [_insert_line(rule) for rule in handle.forward_rules]
        nat_lines: list[str] = []
        nat_key = (handle.subnet, handle.uplink_if)
        if handle.nat and _nat_refcounts.get(nat_key, 0) == 0:
            nat_lines.append(
                f"-I POSTROUTING -s {handle.subnet} -o {handle.uplink_if} -j MASQUERADE"
            )
        for port in ports:
            if _mgmt_refcounts.get((handle.subnet, port), 0) == 0:
                filter_lines.append(
                    f"-I INPUT -s {handle.subnet} -p tcp --dport {port} -j REJECT"
                )
        script = _restore_script({"filter": filter_lines, "nat": nat_lines})
        if script:
            _run_iptables_restore(script, ip_version=4, namespace=None, noflush=True)
        if handle.nat:
            _nat_refcounts[nat_key] = _nat_refcounts.get(nat_key, 0) + 1
        for port in ports:
            key = (handle.subnet, port)
            _mgmt_refcounts[key] = _mgmt_refcounts.get(key, 0) + 1


def setup_network_uplink(
    namespace: str,
    sandbox_id: str,
    uplink: NetworkUplinkPolicy,
    *,
    management_ports: list[int] | None = None,
    apply_firewall: bool = True,
) -> UplinkHandle:
    """Attach a veth uplink and optional NAT for an isolated sandbox netns."""
    ensure_ip_forward()
    uplink_if = resolve_uplink_interface(uplink.interface)
    host_if, sandbox_if = _interface_names(sandbox_id)
    last_error: Exception | None = None
    subnet = ""
    gateway: ipaddress.IPv4Address | None = None
    sandbox_ip: ipaddress.IPv4Address | None = None

    for slot in range(64):
        with perf_trace.span("uplink.allocate", sandbox_id):
            gateway, sandbox_ip, prefix_len, subnet = _allocate_uplink_block(
                sandbox_id,
                uplink.subnet,
                slot,
            )
        host_addr = f"{gateway}/{prefix_len}"
        sandbox_addr = f"{sandbox_ip}/{prefix_len}"
        try:
            with perf_trace.span("uplink.veth_host", sandbox_id):
                _run_ip_batch([
                    f"link add {host_if} type veth peer name {sandbox_if} netns {namespace}",
                    f"addr add {host_addr} dev {host_if}",
                    f"link set {host_if} up",
                ])
            with perf_trace.span("uplink.veth_netns", sandbox_id):
                _run_ip_batch([
                    "link set lo up",
                    f"addr add {sandbox_addr} dev {sandbox_if}",
                    f"link set {sandbox_if} up",
                    f"route add default via {gateway} dev {sandbox_if}",
                ], namespace=namespace)
            break
        except (subprocess.CalledProcessError, NetworkSetupError) as exc:
            last_error = exc
            _invalidate_route_cache()
            _run_ip(["link", "del", host_if], check=False)
            continue
    else:
        raise NetworkSetupError(
            f"Failed to allocate uplink addresses for sandbox {sandbox_id}: {last_error}"
        ) from last_error

    _remember_route(subnet)
    forward_rules = [
        ["FORWARD", "-i", host_if, "-j", "ACCEPT"],
        [
            "FORWARD", "-o", host_if,
            "-m", "state", "--state", "RELATED,ESTABLISHED", "-j", "ACCEPT",
        ],
    ]
    handle = UplinkHandle(
        host_if=host_if,
        sandbox_if=sandbox_if,
        subnet=subnet,
        uplink_if=uplink_if,
        nat=uplink.nat,
        sandbox_ip=str(sandbox_ip),
        management_ports=list(management_ports or []),
        forward_rules=forward_rules,
    )
    if apply_firewall:
        try:
            apply_uplink_firewall(handle)
        except Exception:
            _run_ip(["link", "del", host_if], check=False)
            raise

    logger.info(
        "Network uplink applied for sandbox %s: host_if=%s sandbox_if=%s "
        "subnet=%s sandbox_ip=%s uplink_if=%s nat=%s",
        sandbox_id,
        host_if,
        sandbox_if,
        subnet,
        sandbox_ip,
        uplink_if,
        uplink.nat,
    )
    return handle


def teardown_network_uplink(handle: UplinkHandle) -> None:
    for rule in reversed(handle.forward_rules):
        _remove_iptables_rule(rule)
    if handle.nat:
        _release_nat(handle.subnet, handle.uplink_if)
    for port in handle.management_ports:
        _release_management_port_block(handle.subnet, port)
    _run_ip(["link", "del", handle.host_if], check=False)


def is_jiuwenbox_veth_name(name: str) -> bool:
    """Return True for host-side ``jwbH********`` / ``jwbS********`` veth names."""
    return bool(_JWB_VETH_RE.fullmatch(name))


def parse_ip_o_link_names(stdout: str) -> list[str]:
    """Parse ``ip -o link show`` names, stripping the ``@ifN`` veth peer suffix."""
    names: list[str] = []
    for line in stdout.splitlines():
        parts = line.split(":", 2)
        if len(parts) < 2:
            continue
        raw = parts[1].strip()
        if not raw:
            continue
        names.append(raw.split("@", 1)[0])
    return names


def parse_netns_list_names(stdout: str) -> list[str]:
    """Parse ``ip netns list`` names (first token; ignores ``(id: N)``)."""
    names: list[str] = []
    for line in stdout.splitlines():
        token = line.split(maxsplit=1)[0] if line.strip() else ""
        if token:
            names.append(token)
    return names


def parse_netns_pids(stdout: str) -> list[int]:
    """Parse ``ip netns pids`` output into PIDs."""
    pids: list[int] = []
    for line in stdout.splitlines():
        token = line.strip()
        if token.isdigit():
            pids.append(int(token))
    return pids


def parse_forward_rule_args(stdout: str, interfaces: set[str]) -> list[list[str]]:
    """Extract ``iptables -S FORWARD`` rules that reference ``interfaces``."""
    rules: list[list[str]] = []
    if not interfaces:
        return rules
    for line in stdout.splitlines():
        parts = line.split()
        if len(parts) < 3 or parts[0] != "-A" or parts[1] != "FORWARD":
            continue
        if any(token in interfaces for token in parts[2:]):
            rules.append(parts[1:])
    return rules


@dataclass
class OrphanNetworkCleanupResult:
    """Counts from a best-effort sweep of leftover jiuwenbox host network state."""

    namespaces: list[str] = field(default_factory=list)
    interfaces: list[str] = field(default_factory=list)
    killed_pids: int = 0
    deleted_namespaces: int = 0
    deleted_interfaces: int = 0
    deleted_forward_rules: int = 0
    skipped: bool = False


def cleanup_orphaned_uplinks(*, reason: str = "manual") -> OrphanNetworkCleanupResult:
    """Remove leftover jiuwenbox veths / netns after a crash or SIGKILL.

    Isolated sandboxes create kernel objects (``jwbH*`` / ``jwbS*`` veths and
    ``jbx-*`` named netns) that survive the jiuwenbox-server process. Per-sandbox
    ``teardown_network_uplink`` only runs if lifespan shutdown completes; this
    sweep is the recovery path for ungraceful stops.

    Assumes a single jiuwenbox instance per host network namespace. Safe to
    call when nothing is leftover. Never raises: every step is best-effort.
    """
    result = OrphanNetworkCleanupResult()
    if os.environ.get(ENV_SKIP_ORPHAN_NET_CLEANUP, "").strip():
        result.skipped = True
        logger.debug("Skipping orphan network cleanup (%s): %s is set", reason, ENV_SKIP_ORPHAN_NET_CLEANUP)
        return result

    try:
        result.namespaces = [
            name
            for name in _list_named_namespace_names()
            if name.startswith(NETNS_NAME_PREFIX)
        ]
        result.interfaces = [
            name
            for name in _list_host_link_names()
            if is_jiuwenbox_veth_name(name)
        ]
    except FileNotFoundError:
        logger.debug("Skipping orphan network cleanup (%s): ip binary is missing", reason)
        return result
    except Exception:
        logger.warning("Failed to list leftover jiuwenbox network objects (%s)", reason, exc_info=True)
        return result

    if not result.namespaces and not result.interfaces:
        logger.debug("Orphan network cleanup (%s): nothing leftover", reason)
        return result

    logger.info(
        "Orphan network cleanup (%s): leftover netns=%s veth=%s",
        reason,
        result.namespaces,
        result.interfaces,
    )

    for namespace in result.namespaces:
        result.killed_pids += _kill_namespace_processes(namespace)

    # Deleting the host veth removes both ends and is what clears ``ifconfig``.
    for iface in result.interfaces:
        if _delete_host_link(iface):
            result.deleted_interfaces += 1

    result.deleted_forward_rules = _delete_stale_forward_rules(set(result.interfaces))

    for namespace in result.namespaces:
        if _delete_named_namespace_best_effort(namespace):
            result.deleted_namespaces += 1

    leftover = [
        name
        for name in _list_host_link_names_best_effort()
        if is_jiuwenbox_veth_name(name)
    ]
    for iface in leftover:
        if _delete_host_link(iface):
            result.deleted_interfaces += 1

    logger.info(
        "Orphan network cleanup (%s) finished: deleted_veth=%d deleted_netns=%d "
        "killed_pids=%d deleted_forward_rules=%d",
        reason,
        result.deleted_interfaces,
        result.deleted_namespaces,
        result.killed_pids,
        result.deleted_forward_rules,
    )
    return result


def _list_named_namespace_names() -> list[str]:
    result = subprocess.run(
        [IP_BINARY, "netns", "list"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    return parse_netns_list_names(result.stdout)


def _list_host_link_names() -> list[str]:
    result = _run_ip(["-o", "link", "show"], check=False)
    if result.returncode != 0:
        return []
    return parse_ip_o_link_names(result.stdout)


def _list_host_link_names_best_effort() -> list[str]:
    try:
        return _list_host_link_names()
    except Exception:
        return []


def _kill_namespace_processes(namespace: str) -> int:
    try:
        result = subprocess.run(
            [IP_BINARY, "netns", "pids", namespace],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return 0
    if result.returncode != 0:
        return 0
    killed = 0
    for pid in parse_netns_pids(result.stdout):
        try:
            os.kill(pid, signal.SIGKILL)
            killed += 1
        except OSError:
            continue
    return killed


def _delete_host_link(name: str) -> bool:
    try:
        result = _run_ip(["link", "del", name], check=False)
    except FileNotFoundError:
        return False
    except Exception:
        logger.debug("Failed to delete leftover veth %s", name, exc_info=True)
        return False
    return result.returncode == 0


def _delete_named_namespace_best_effort(namespace: str) -> bool:
    try:
        result = subprocess.run(
            [IP_BINARY, "netns", "delete", namespace],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return False
    except Exception:
        logger.debug("Failed to delete leftover netns %s", namespace, exc_info=True)
        return False
    return result.returncode == 0


def _delete_stale_forward_rules(interfaces: set[str]) -> int:
    if not interfaces:
        return 0
    deleted = 0
    try:
        listed = run_iptables(["-S", "FORWARD"], check=False)
    except Exception:
        logger.debug("Failed to list FORWARD rules during orphan cleanup", exc_info=True)
        return 0
    if listed.returncode != 0:
        return 0
    for rule in parse_forward_rule_args(listed.stdout, interfaces):
        try:
            _remove_iptables_rule(rule)
            deleted += 1
        except Exception:
            logger.debug("Failed to drop stale FORWARD rule %s", rule, exc_info=True)
    return deleted


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


def _append_family(
    v4: list[str],
    v6: list[str],
    args: list[str],
    ip_value: str | None = None,
) -> None:
    line = "-A " + " ".join(args)
    if ip_value is None:
        v4.append(line)
        v6.append(line)
        return
    if _ip_version(ip_value) == 6:
        v6.append(line)
    else:
        v4.append(line)


def _render_direction_rules(
    v4: list[str],
    v6: list[str],
    egress: ResolvedNetworkRules,
    ingress: ResolvedNetworkRules,
) -> None:
    """Build the same filter rules the per-command path used to append."""
    _append_family(v4, v6, ["OUTPUT", "-o", "lo", "-j", "ACCEPT"])
    _append_family(
        v4, v6,
        ["OUTPUT", "-m", "state", "--state", "ESTABLISHED,RELATED", "-j", "ACCEPT"],
    )
    for port in egress.blocked_ports:
        _append_family(
            v4, v6,
            ["OUTPUT", "-p", "tcp", "--dport", str(port), "-j", "DROP"],
        )
    if not egress.default_deny:
        for ip in egress.blocked_ips:
            _append_family(v4, v6, ["OUTPUT", "-d", ip, "-j", "DROP"], ip)
    else:
        _append_family(v4, v6, ["OUTPUT", "-p", "udp", "--dport", "53", "-j", "ACCEPT"])
        _append_family(v4, v6, ["OUTPUT", "-p", "tcp", "--dport", "53", "-j", "ACCEPT"])
        for ip in egress.blocked_ips:
            _append_family(v4, v6, ["OUTPUT", "-d", ip, "-j", "DROP"], ip)
        for ip in egress.allowed_ips:
            if egress.allowed_ports:
                for port in egress.allowed_ports:
                    _append_family(
                        v4, v6,
                        [
                            "OUTPUT", "-d", ip, "-p", "tcp",
                            "--dport", str(port), "-j", "ACCEPT",
                        ],
                        ip,
                    )
            else:
                _append_family(v4, v6, ["OUTPUT", "-d", ip, "-j", "ACCEPT"], ip)
        _append_family(v4, v6, ["OUTPUT", "-j", "DROP"])

    _append_family(
        v4, v6,
        ["INPUT", "-m", "state", "--state", "ESTABLISHED,RELATED", "-j", "ACCEPT"],
    )
    for ip in ingress.blocked_ips:
        _append_family(v4, v6, ["INPUT", "-s", ip, "-j", "DROP"], ip)
    for port in ingress.blocked_ports:
        _append_family(
            v4, v6,
            ["INPUT", "-p", "tcp", "--dport", str(port), "-j", "DROP"],
        )
    if not ingress.default_deny:
        _append_family(v4, v6, ["INPUT", "-j", "ACCEPT"])
        return

    if ingress.allowed_ips and ingress.allowed_ports:
        for ip in ingress.allowed_ips:
            for port in ingress.allowed_ports:
                _append_family(
                    v4, v6,
                    [
                        "INPUT", "-s", ip, "-p", "tcp",
                        "--dport", str(port), "-j", "ACCEPT",
                    ],
                    ip,
                )
    elif ingress.allowed_ips:
        for ip in ingress.allowed_ips:
            _append_family(v4, v6, ["INPUT", "-s", ip, "-j", "ACCEPT"], ip)
    elif ingress.allowed_ports:
        for port in ingress.allowed_ports:
            _append_family(
                v4, v6,
                ["INPUT", "-p", "tcp", "--dport", str(port), "-j", "ACCEPT"],
            )
    _append_family(v4, v6, ["INPUT", "-j", "DROP"])


def _ipv6_restore_required(
    egress_rules: ResolvedNetworkRules,
    ingress_rules: ResolvedNetworkRules,
) -> bool:
    """Return whether the IPv6 filter table would differ from default ACCEPT.

    ``default: allow`` with no blocked ports and no IPv6 destinations only
    emits ACCEPT rules. The chain policy is already ACCEPT, so running
    ip6tables-restore does not change what the sandbox can reach.
    """
    for rules in (egress_rules, ingress_rules):
        if rules.default_deny or rules.blocked_ports:
            return True
        for ip in rules.blocked_ips:
            if _ip_version(ip) == 6:
                return True
    return False


def apply_iptables_rules(
    egress_rules: ResolvedNetworkRules,
    ingress_rules: ResolvedNetworkRules,
    namespace: str | None = None,
    *,
    bring_up_loopback: bool = True,
) -> None:
    """Apply iptables rules inside the current (unshared) network namespace."""
    if bring_up_loopback:
        setup_loopback(namespace=namespace)
    use_restore = _can_iptables_restore(4) and (
        not _ip6tables_available(None) or _can_iptables_restore(6)
    )
    if not use_restore:
        _apply_egress_rules(egress_rules, namespace=namespace)
        _apply_ingress_rules(ingress_rules, namespace=namespace)
        return

    v4: list[str] = []
    v6: list[str] = []
    _render_direction_rules(v4, v6, egress_rules, ingress_rules)
    _run_iptables_restore(
        _restore_script({"filter": v4}),
        ip_version=4,
        namespace=namespace,
    )
    restore_v6 = _ipv6_restore_required(egress_rules, ingress_rules)
    if v6 and restore_v6 and _ip6tables_available(None):
        _run_iptables_restore(
            _restore_script({"filter": v6}),
            ip_version=6,
            namespace=namespace,
        )
    logger.info(
        "Network filter rules restored in %s: egress blocked=%d ingress blocked=%d",
        namespace or "current",
        len(egress_rules.blocked_ips),
        len(ingress_rules.blocked_ips),
    )


def flush_filter_rules(namespace: str | None = None) -> None:
    """Flush filter-table INPUT/OUTPUT chains inside a network namespace.

    Resets chain policies to ACCEPT so a subsequent
    :func:`setup_network_isolation` can rebuild rules from scratch.
    Does not touch NAT/FORWARD or host-side uplink rules.
    """
    for chain in ("INPUT", "OUTPUT"):
        _run_iptables_both(["-F", chain], namespace=namespace)
        _run_iptables_both(["-P", chain, "ACCEPT"], namespace=namespace)
    logger.info("Flushed filter INPUT/OUTPUT rules in namespace %s", namespace or "current")


def setup_network_isolation(
    policy: NetworkPolicy,
    namespace: str | None = None,
    *,
    bring_up_loopback: bool = True,
    perf_id: str | None = None,
) -> None:
    """Top-level entry point for network isolation setup.

    Called from within the sandbox namespace after bwrap --unshare-net or
    from the host against a pre-created named namespace.
    """
    if policy.mode == NetworkMode.HOST:
        logger.info("Network mode is 'host', skipping isolation")
        return

    label = perf_id or namespace or "-"
    with perf_trace.span("firewall.resolve", label):
        egress_rules = build_network_rules(policy.egress)
        ingress_rules = build_network_rules(policy.ingress)
    with perf_trace.span("firewall.restore", label):
        apply_iptables_rules(
            egress_rules,
            ingress_rules,
            namespace=namespace,
            bring_up_loopback=bring_up_loopback,
        )


def replace_network_isolation(policy: NetworkPolicy, namespace: str | None = None) -> None:
    """Replace egress/ingress iptables rules in an existing network namespace.

    Flushes the current filter INPUT/OUTPUT chains, then reapplies rules
    from ``policy`` the same way :func:`setup_network_isolation` does at
    create time. Host-side NAT/uplink state is left untouched.
    """
    if policy.mode == NetworkMode.HOST:
        logger.info("Network mode is 'host', skipping isolation replace")
        return

    flush_filter_rules(namespace=namespace)
    setup_network_isolation(policy, namespace=namespace)
