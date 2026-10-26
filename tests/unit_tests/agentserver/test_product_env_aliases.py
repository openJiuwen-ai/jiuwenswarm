# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""JIUWENCLAW_* → JIUWENSWARM_* product env alias mapping."""

from __future__ import annotations

import os

import pytest

from jiuwenswarm.common.local_env_config import (
    ENV_CONFIG_DICT,
    bind_task_env_overlay,
    canonical_product_env_key,
    normalize_product_env_aliases,
    read_env,
    reset_task_env_overlay,
)
from jiuwenswarm.server.runtime.sync_agents_configs import (
    SYNC_ENV_SCHEMA,
    materialize_sync_env,
)


@pytest.fixture(autouse=True)
def _reset_env():
    saved = dict(os.environ)
    ENV_CONFIG_DICT.clear()
    yield
    os.environ.clear()
    os.environ.update(saved)
    ENV_CONFIG_DICT.clear()


def test_canonical_product_env_key_maps_legacy_prefix():
    assert (
        canonical_product_env_key("JIUWENCLAW_SHARED_SKILLS_DIRS")
        == "JIUWENSWARM_SHARED_SKILLS_DIRS"
    )
    assert (
        canonical_product_env_key("JIUWENSWARM_SHARED_SKILLS_DIRS")
        == "JIUWENSWARM_SHARED_SKILLS_DIRS"
    )
    assert canonical_product_env_key("ENABLED_SKILLS") == "ENABLED_SKILLS"


def test_normalize_prefers_canonical_on_clash():
    out = normalize_product_env_aliases(
        {
            "JIUWENCLAW_SHARED_SKILLS_DIRS": "/legacy",
            "JIUWENSWARM_SHARED_SKILLS_DIRS": "/canon",
        }
    )
    assert out == {"JIUWENSWARM_SHARED_SKILLS_DIRS": "/canon"}


def test_materialize_sync_env_remaps_legacy():
    out = materialize_sync_env({"JIUWENCLAW_DISABLED_SKILLS": "a,b"})
    assert out == {"JIUWENSWARM_DISABLED_SKILLS": "a,b"}


def test_sync_schema_keeps_relay_jiuwenclaw_wire_keys():
    """Schema documents what relay sends; tip remaps to JIUWENSWARM_*."""
    assert "JIUWENCLAW_SHARED_SKILLS_DIRS" in SYNC_ENV_SCHEMA
    assert "JIUWENCLAW_DISABLED_SKILLS" in SYNC_ENV_SCHEMA
    assert "JIUWENSWARM_SHARED_SKILLS_DIRS" in SYNC_ENV_SCHEMA
    assert "JIUWENSWARM_DISABLED_SKILLS" in SYNC_ENV_SCHEMA


def test_validate_accepts_relay_claw_keys_without_swarm_twins():
    """Relay-only ``JIUWENCLAW_*`` satisfies the pair; tip stores ``JIUWENSWARM_*``."""
    from jiuwenswarm.server.runtime.sync_agents_configs import validate_sync_payload

    env = {key: "" for key in SYNC_ENV_SCHEMA}
    env.pop("JIUWENSWARM_SHARED_SKILLS_DIRS", None)
    env.pop("JIUWENSWARM_DISABLED_SKILLS", None)
    env["JIUWENCLAW_SHARED_SKILLS_DIRS"] = "/office-claw-skills"
    env["JIUWENCLAW_DISABLED_SKILLS"] = ""
    params = validate_sync_payload(
        {
            "revision": "r1",
            "service_id": "default",
            "agents": [
                {
                    "agent_id": "office",
                    "config": {},
                    "env": env,
                    "runtime": {},
                }
            ],
        }
    )
    tip_env = params["agents"][0]["env"]
    assert tip_env["JIUWENSWARM_SHARED_SKILLS_DIRS"] == "/office-claw-skills"
    assert "JIUWENCLAW_SHARED_SKILLS_DIRS" not in tip_env
    assert "JIUWENSWARM_DISABLED_SKILLS" in tip_env
    assert "JIUWENCLAW_DISABLED_SKILLS" not in tip_env


def test_read_env_resolves_legacy_tip_key():
    ENV_CONFIG_DICT["JIUWENCLAW_SHARED_SKILLS_DIRS"] = "/office-claw-skills"
    assert read_env("JIUWENSWARM_SHARED_SKILLS_DIRS") == "/office-claw-skills"
    # Writes are stored under the canonical key.
    assert "JIUWENSWARM_SHARED_SKILLS_DIRS" in dict(ENV_CONFIG_DICT)


def test_overlay_legacy_key_readable_via_canonical():
    token = bind_task_env_overlay(
        {"JIUWENCLAW_SSL_VERIFY": "false"}
    )
    try:
        assert read_env("JIUWENSWARM_SSL_VERIFY") == "false"
    finally:
        reset_task_env_overlay(token)
