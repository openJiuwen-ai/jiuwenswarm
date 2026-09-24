# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unit tests for the in-memory AgentOS config updater applier."""

from __future__ import annotations

from dataclasses import dataclass

from jiuwenswarm.extensions.agentos.config_updater.service import ConfigUpdaterApplier


@dataclass
class FakeFetched:
    section: dict
    metadata: dict
    mod_revision: int


class Harness:
    def __init__(self) -> None:
        self.refreshes: list[dict] = []

    async def refresh(self, overrides: dict) -> None:
        self.refreshes.append(overrides)

    def applier(self) -> ConfigUpdaterApplier:
        return ConfigUpdaterApplier(refresh_handler=self.refresh)


async def test_apply_forwards_only_managed_overrides():
    harness = Harness()

    result = await harness.applier().apply(
        FakeFetched(
            {
                "sandbox": {
                    "sandbox_idle_timeout_seconds": 120,
                    "jiuwen_sandbox": {"cpu": 2000, "memory": 4096},
                }
            },
            {},
            5,
        )
    )

    assert result.applied is True
    assert harness.refreshes == [
        {
            "sandbox": {
                "sandbox_idle_timeout_seconds": 120,
                "jiuwen_sandbox": {"cpu": 2000, "memory": 4096},
            }
        }
    ]


async def test_overrides_accumulate_across_revisions():
    harness = Harness()
    applier = harness.applier()

    await applier.apply(
        FakeFetched({"sandbox": {"sandbox_idle_timeout_seconds": 120}}, {}, 5)
    )
    await applier.apply(
        FakeFetched({"sandbox": {"jiuwen_sandbox": {"cpu": 4000}}}, {}, 6)
    )

    assert harness.refreshes[-1] == {
        "sandbox": {
            "sandbox_idle_timeout_seconds": 120,
            "jiuwen_sandbox": {"cpu": 4000},
        }
    }


async def test_same_values_on_new_revision_are_no_change():
    harness = Harness()
    applier = harness.applier()

    await applier.apply(
        FakeFetched({"sandbox": {"sandbox_idle_timeout_seconds": 120}}, {}, 5)
    )
    result = await applier.apply(
        FakeFetched({"sandbox": {"sandbox_idle_timeout_seconds": 120}}, {}, 6)
    )

    assert result.skipped_reason == "no-change"
    assert len(harness.refreshes) == 1


async def test_unmanaged_fields_are_ignored():
    harness = Harness()

    result = await harness.applier().apply(
        FakeFetched({"sandbox": {"type": "docker", "cpu": 2000}}, {}, 5)
    )

    assert result.skipped_reason == "no-managed-fields"
    assert harness.refreshes == []


async def test_invalid_timeout_is_rejected():
    harness = Harness()
    applier = harness.applier()

    result = await applier.apply(
        FakeFetched({"sandbox": {"sandbox_idle_timeout_seconds": None}}, {}, 5)
    )

    assert result.skipped_reason == "validation-failed"
    assert result.errors
    assert harness.refreshes == []
    assert applier.last_applied_mod_revision == 5


async def test_same_and_older_revisions_are_ignored():
    harness = Harness()
    applier = harness.applier()

    await applier.apply(
        FakeFetched({"sandbox": {"sandbox_idle_timeout_seconds": 120}}, {}, 6)
    )
    same = await applier.apply(
        FakeFetched({"sandbox": {"sandbox_idle_timeout_seconds": 60}}, {}, 6)
    )
    older = await applier.apply(
        FakeFetched({"sandbox": {"sandbox_idle_timeout_seconds": 60}}, {}, 5)
    )

    assert same.skipped_reason == "revision-unchanged"
    assert older.skipped_reason == "revision-unchanged"
    assert len(harness.refreshes) == 1


async def test_refresh_failure_retries_same_overrides():
    attempts: list[dict] = []

    async def flaky_refresh(overrides):
        attempts.append(overrides)
        if len(attempts) == 1:
            raise RuntimeError("apply failed")

    applier = ConfigUpdaterApplier(refresh_handler=flaky_refresh)
    fetched = FakeFetched(
        {"sandbox": {"sandbox_idle_timeout_seconds": 120}}, {}, 5
    )

    failed = await applier.apply(fetched)
    retried = await applier.apply(fetched)

    assert failed.skipped_reason == "refresh-failed"
    assert failed.retryable is True
    assert retried.applied is True
    assert attempts == [
        {"sandbox": {"sandbox_idle_timeout_seconds": 120}},
        {"sandbox": {"sandbox_idle_timeout_seconds": 120}},
    ]
