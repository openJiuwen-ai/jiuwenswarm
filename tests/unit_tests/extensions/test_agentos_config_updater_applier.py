# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unit tests for the in-memory AgentOS config updater applier."""

from __future__ import annotations

import copy
from dataclasses import dataclass

from jiuwenswarm.extensions.agentos.config_updater.service import ConfigUpdaterApplier


@dataclass
class FakeFetched:
    section: dict
    metadata: dict
    mod_revision: int


class Harness:
    def __init__(self, config: dict | None = None) -> None:
        self.config = config if config is not None else {}
        self.refreshes: list[dict] = []

    async def refresh(self, merged: dict) -> None:
        self.refreshes.append(merged)

    def applier(self) -> ConfigUpdaterApplier:
        return ConfigUpdaterApplier(self.config, refresh_handler=self.refresh)


async def test_apply_merges_full_snapshot_and_refreshes():
    harness = Harness(
        {
            "gateway": {"cron": {"store_backend": "etcd"}},
            "sandbox": {"type": "yuanrong", "cpu": 1000},
        }
    )

    result = await harness.applier().apply(
        FakeFetched(
            {
                "agent_sandbox": {"idle_timeout": 120},
                "sandbox": {"cpu": 2000, "memory": 4096},
            },
            {},
            5,
        )
    )

    assert result.applied is True
    # ``sandbox.cpu/memory`` are unmanaged: the remote values are ignored and
    # the local tool-sandbox values are preserved.
    assert harness.refreshes == [
        {
            "gateway": {"cron": {"store_backend": "etcd"}},
            "sandbox": {
                "type": "yuanrong",
                "cpu": 1000,
            },
            "agent_sandbox": {"idle_timeout": 120},
        }
    ]


async def test_apply_does_not_mutate_initial_config():
    initial = {"agent_sandbox": {"idle_timeout": 600}}
    applier = ConfigUpdaterApplier(initial)

    await applier.apply(
        FakeFetched({"agent_sandbox": {"idle_timeout": 120}}, {}, 5)
    )

    assert initial == {"agent_sandbox": {"idle_timeout": 600}}


async def test_apply_ignores_unmanaged_fields():
    harness = Harness(
        {
            "gateway": {
                "agentos": {"workspace_root": "/safe"},
                "cron": {"store_backend": "etcd"},
            },
            "sandbox": {"type": "yuanrong", "image": "trusted", "cpu": 1000},
        }
    )

    await harness.applier().apply(
        FakeFetched(
            {
                "agent_sandbox": {"idle_timeout": 120},
                "gateway": {
                    "agentos": {"workspace_root": "/bad"},
                    "cron": {"store_backend": "file"},
                },
                "sandbox": {"type": "other", "image": "bad", "cpu": 2000},
            },
            {},
            5,
        )
    )

    merged = harness.refreshes[0]
    assert merged["agent_sandbox"] == {"idle_timeout": 120}
    # Unmanaged remote fields (gateway.* and sandbox.*) must not be applied.
    assert merged["gateway"]["agentos"] == {"workspace_root": "/safe"}
    assert merged["gateway"]["cron"] == {"store_backend": "etcd"}
    assert merged["sandbox"] == {
        "type": "yuanrong",
        "image": "trusted",
        "cpu": 1000,
    }


async def test_only_unmanaged_fields_are_skipped():
    harness = Harness({"sandbox": {"type": "yuanrong"}})
    applier = harness.applier()

    result = await applier.apply(
        FakeFetched({"sandbox": {"type": "other"}}, {}, 5)
    )

    assert result.skipped_reason == "no-managed-fields"
    assert harness.refreshes == []
    assert applier.last_applied_mod_revision == 5


async def test_invalid_managed_field_is_rejected():
    harness = Harness({"agent_sandbox": {"idle_timeout": 600}})
    applier = harness.applier()

    result = await applier.apply(
        FakeFetched({"agent_sandbox": {"idle_timeout": None}}, {}, 5)
    )

    assert result.skipped_reason == "validation-failed"
    assert result.errors
    assert harness.refreshes == []
    assert applier.last_applied_mod_revision == 5

    repeated = await applier.apply(
        FakeFetched({"agent_sandbox": {"idle_timeout": None}}, {}, 5)
    )
    assert repeated.skipped_reason == "revision-unchanged"


async def test_empty_section_revision_is_acknowledged():
    applier = ConfigUpdaterApplier({})

    result = await applier.apply(FakeFetched({}, {}, 5))
    repeated = await applier.apply(FakeFetched({}, {}, 5))

    assert result.skipped_reason == "empty-section"
    assert applier.last_applied_mod_revision == 5
    assert repeated.skipped_reason == "revision-unchanged"


async def test_same_and_older_revisions_are_ignored():
    harness = Harness({"agent_sandbox": {"idle_timeout": 600}})
    applier = harness.applier()

    await applier.apply(FakeFetched({"agent_sandbox": {"idle_timeout": 120}}, {}, 6))
    same = await applier.apply(FakeFetched({"agent_sandbox": {"idle_timeout": 60}}, {}, 6))
    older = await applier.apply(FakeFetched({"agent_sandbox": {"idle_timeout": 60}}, {}, 5))

    assert same.skipped_reason == "revision-unchanged"
    assert older.skipped_reason == "revision-unchanged"
    assert len(harness.refreshes) == 1


async def test_unchanged_managed_values_skip_refresh():
    harness = Harness({"agent_sandbox": {"idle_timeout": 120}})
    applier = harness.applier()

    result = await applier.apply(
        FakeFetched({"agent_sandbox": {"idle_timeout": 120}}, {}, 5)
    )

    assert result.skipped_reason == "no-change"
    assert harness.refreshes == []
    assert applier.last_applied_mod_revision == 5


async def test_refresh_failure_retries_same_snapshot():
    attempts: list[dict] = []

    async def flaky_refresh(merged):
        attempts.append(merged)
        if len(attempts) == 1:
            raise RuntimeError("reload failed")

    applier = ConfigUpdaterApplier({}, refresh_handler=flaky_refresh)
    fetched = FakeFetched({"agent_sandbox": {"idle_timeout": 120}}, {}, 5)

    failed = await applier.apply(fetched)
    retried = await applier.apply(fetched)

    assert failed.skipped_reason == "refresh-failed"
    assert failed.retryable is True
    assert retried.applied is True
    assert attempts == [
        {"agent_sandbox": {"idle_timeout": 120}},
        {"agent_sandbox": {"idle_timeout": 120}},
    ]
    assert applier.last_applied_mod_revision == 5


async def test_refresh_receives_copy_of_effective_config():
    seen: list[dict] = []

    async def mutating_refresh(merged):
        seen.append(copy.deepcopy(merged))
        merged["agent_sandbox"]["idle_timeout"] = 9999

    applier = ConfigUpdaterApplier(
        {"agent_sandbox": {"idle_timeout": 600}},
        refresh_handler=mutating_refresh,
    )
    await applier.apply(FakeFetched({"agent_sandbox": {"idle_timeout": 120}}, {}, 5))
    await applier.apply(FakeFetched({"agent_sandbox": {"idle_timeout": 60}}, {}, 6))

    assert seen[1]["agent_sandbox"] == {"idle_timeout": 60}


async def test_new_revision_uses_latest_local_base_config():
    current = {
        "models": {"marker": "initial"},
        "gateway": {"agentos": {"sandbox_idle_timeout_seconds": 600}},
    }
    seen: list[dict] = []

    async def refresh(merged):
        seen.append(merged)

    applier = ConfigUpdaterApplier(
        current,
        config_provider=lambda: current,
        refresh_handler=refresh,
    )
    await applier.apply(FakeFetched({"agent_sandbox": {"idle_timeout": 120}}, {}, 5))

    current["models"]["marker"] = "updated-locally"
    await applier.apply(FakeFetched({"agent_sandbox": {"idle_timeout": 60}}, {}, 6))

    assert seen[1]["models"]["marker"] == "updated-locally"
    # The local config field is untouched; the managed override is separate.
    assert seen[1]["gateway"]["agentos"] == {"sandbox_idle_timeout_seconds": 600}
    assert seen[1]["agent_sandbox"] == {"idle_timeout": 60}
