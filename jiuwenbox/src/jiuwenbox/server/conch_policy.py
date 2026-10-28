# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
"""Helpers for Conch policy resolution and SDK payload mapping."""

from __future__ import annotations

import os
from typing import Any

from jiuwenbox.models.policy import (
    CONCH_INGRESS_DENY_ALL_CIDR,
    ConchNetworkPolicy,
    ConchPolicy,
    SecurityPolicy,
)

JIUWENBOX_CONCH_TEMPLATE_NAME_ENV = "JIUWENBOX_CONCH_TEMPLATE_NAME"

# Linux uid_t/gid_t are 32-bit; (uid_t)-1 / (gid_t)-1 is reserved as invalid.
_CONCH_POSIX_ID_MAX = 2**32 - 2


class ConchRunAsError(ValueError):
    """Raised when conch.run_as_user / conch.run_as_group cannot be resolved on the host."""


def resolve_conch_template_name(policy: SecurityPolicy) -> str | None:
    """Resolve Conch template name: policy field, then env, else None for conchd default."""
    template_name = (policy.conch.template_name or "").strip()
    if template_name:
        return template_name
    env_template_name = (os.environ.get(JIUWENBOX_CONCH_TEMPLATE_NAME_ENV) or "").strip()
    if env_template_name:
        return env_template_name
    return None


def _resolve_posix_id(value: str, *, kind: str) -> int:
    """Resolve a user/group name or digit string to a numeric id on the host.

    ASCII digit-only strings are treated as raw ids (chown-style), not names.
    """
    if value.isdigit():
        number = int(value)
        if number > _CONCH_POSIX_ID_MAX:
            raise ConchRunAsError(
                f"conch.run_as_{kind} id {number} is out of range "
                f"(max {_CONCH_POSIX_ID_MAX})"
            )
        return number
    try:
        if kind == "user":
            import pwd

            return int(pwd.getpwnam(value).pw_uid)
        import grp

        return int(grp.getgrnam(value).gr_gid)
    except KeyError as exc:
        raise ConchRunAsError(
            f"conch.run_as_{kind} {value!r} could not be resolved on the host"
        ) from exc
    except ImportError as exc:
        raise ConchRunAsError(
            "POSIX user/group database is unavailable; "
            "cannot resolve conch.run_as_user / conch.run_as_group"
        ) from exc


def resolve_conch_run_as(policy: SecurityPolicy | ConchPolicy) -> tuple[int, int] | None:
    """Resolve optional ``run_as_user`` / ``run_as_group`` to ``(uid, gid)``.

    Returns ``None`` when both fields are unset. Pairing is enforced by
    ``ConchPolicy``; this helper only performs host name/id lookup.
    """
    conch = policy.conch if isinstance(policy, SecurityPolicy) else policy
    user = conch.run_as_user
    group = conch.run_as_group
    if user is None and group is None:
        return None
    if user is None or group is None:
        # Defensive: model validator should already reject half-set pairs.
        raise ConchRunAsError(
            "conch.run_as_user and conch.run_as_group must both be set or both omitted"
        )
    uid = _resolve_posix_id(user, kind="user")
    gid = _resolve_posix_id(group, kind="group")
    return uid, gid


def build_conch_resource_kwargs(conch: ConchPolicy) -> dict[str, Any]:
    """Return SDK create kwargs for optional VM resource fields (omit unset)."""
    kwargs: dict[str, Any] = {}
    if conch.vcpu_num is not None:
        kwargs["vcpu_num"] = conch.vcpu_num
    if conch.vcpu_max is not None:
        kwargs["vcpu_max"] = conch.vcpu_max
    if conch.ram_mb is not None:
        kwargs["ram_mb"] = conch.ram_mb
    return kwargs


def merge_conch_create_env(
    conch_env: dict[str, str] | None,
    api_env: dict[str, str] | None,
) -> dict[str, str]:
    """Merge ``policy.conch.env`` with create-request env (API overrides).

    Does **not** read top-level ``SecurityPolicy.environment`` (bwrap-only).
    """
    merged = dict(conch_env or {})
    if api_env:
        merged.update(api_env)
    return merged


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def map_conch_network_policy(
    network: ConchNetworkPolicy,
    *,
    omit_empty: bool = True,
) -> dict[str, Any] | None:
    """Map jiuwenbox ConchNetworkPolicy to Conch SandboxNetworkConfig dict.

    When ``omit_empty`` is True (create path), returns ``None`` for an empty
    allow-all policy so Conch skips installing iptables hooks. When False
    (hot-update path), always returns the five Conch fields so full replace
    can clear previous rules.
    """
    allow_out = _dedupe_preserve_order(sorted(network.egress.allowed_ips))
    deny_out = _dedupe_preserve_order(sorted(network.egress.blocked_ips))
    allow_in = _dedupe_preserve_order(sorted(network.ingress.allowed_ips))
    deny_in = _dedupe_preserve_order(sorted(network.ingress.blocked_ips))

    if network.ingress.default == "deny" and not allow_in:
        if CONCH_INGRESS_DENY_ALL_CIDR not in deny_in:
            deny_in.append(CONCH_INGRESS_DENY_ALL_CIDR)

    allow_internet_access = network.egress.default == "allow"
    payload: dict[str, Any] = {
        "allowOut": allow_out,
        "denyOut": deny_out,
        "allowIn": allow_in,
        "denyIn": deny_in,
        "allow_internet_access": allow_internet_access,
    }

    is_empty = (
        not allow_out
        and not deny_out
        and not allow_in
        and not deny_in
        and allow_internet_access
    )
    if omit_empty and is_empty:
        return None
    return payload


def map_conch_volume_mounts(policy: SecurityPolicy) -> list[dict[str, Any]]:
    """Map conch.filesystem_policy.bind_mounts to Conch volume_mounts."""
    mounts: list[dict[str, Any]] = []
    for mount in policy.conch.filesystem_policy.bind_mounts:
        mounts.append(
            {
                "source": mount.host_path,
                "path": mount.sandbox_path,
                "readonly": mount.mode == "ro",
            }
        )
    return mounts
