# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Validate the narrow Windows network update against the last loaded policy."""

import hashlib

import yaml

from jiuwenbox.server.policy_engine import read_policy_text


def _static_policy(policy):
    result = policy.model_dump(mode="json")
    network = result["windows"]["network"]
    network.pop("disable_all")
    # default=deny projections also clear inherited IP/port allow rules.
    # All egress fields are enforced by EgressFilter, not per-instance WFP.
    network.pop("egress")
    return result


def read_network_candidate(reader, previous, expected_fingerprint):
    """Reject file/port/runtime changes before mutating any live policy."""
    path = reader.policy_path
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected_fingerprint:
        raise ValueError("runtime policy fingerprint changed")
    # Startup loading tolerates malformed copies by falling back to the base.
    # A hot-update receipt must never acknowledge that fallback as this file.
    try:
        data = yaml.safe_load(read_policy_text(path))
    except (yaml.YAMLError, UnicodeError) as exc:
        raise ValueError("invalid runtime policy YAML") from exc
    if not isinstance(data, dict):
        raise ValueError("runtime policy must be an object")
    # load_policy -> merge_policy validates the merged SecurityPolicy. Its
    # schema ValidationError is a ValueError and propagates to the 409 response.
    candidate = reader.load_policy()
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected_fingerprint:
        raise ValueError("runtime policy changed during validation")
    if candidate.windows.network.mode != "wfp_loopback_proxy":
        raise ValueError("Windows proxy enforcement is disabled")
    if _static_policy(candidate) != _static_policy(previous):
        raise ValueError("non-network policy changed; restart required")
    return candidate
