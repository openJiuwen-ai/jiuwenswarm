# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import pytest

from jiuwenswarm.gateway.cron.policy import (
    DEFAULT_MAX_JOBS_PER_USER,
    coerce_max_jobs_per_user,
    max_jobs_per_user_from_config,
    resolve_max_jobs_per_user,
)


def test_coerce_max_jobs_per_user_accepts_zero_and_positive() -> None:
    assert coerce_max_jobs_per_user(0) == 0
    assert coerce_max_jobs_per_user(5) == 5
    assert coerce_max_jobs_per_user("12") == 12


@pytest.mark.parametrize("raw", [None, True, False, -1, "nope"])
def test_coerce_max_jobs_per_user_rejects_invalid(raw: object) -> None:
    assert coerce_max_jobs_per_user(raw) == DEFAULT_MAX_JOBS_PER_USER


def test_max_jobs_per_user_from_config_reads_yaml_section(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        lambda: {"cron": {"max_jobs_per_user": 8}},
    )
    assert max_jobs_per_user_from_config() == 8


def test_max_jobs_per_user_from_config_defaults_when_section_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("jiuwenswarm.common.config.get_config", lambda: {})
    assert max_jobs_per_user_from_config() == DEFAULT_MAX_JOBS_PER_USER


@pytest.mark.asyncio
async def test_resolve_max_jobs_per_user_prefers_enterprise_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("jiuwenswarm.edition.is_enterprise", lambda: True)
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        lambda: {"cron": {"max_jobs_per_user": 5}},
    )

    class _Repo:
        async def get(self) -> dict[str, int]:
            return {"max_jobs_per_user": 3}

    monkeypatch.setattr(
        "jiuwenswarm.gateway.config.enterprise.access.get_enterprise_record_repository",
        lambda _name: _Repo(),
    )
    assert await resolve_max_jobs_per_user() == 3


@pytest.mark.asyncio
async def test_resolve_max_jobs_per_user_falls_back_without_enterprise_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("jiuwenswarm.edition.is_enterprise", lambda: False)
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        lambda: {"cron": {"max_jobs_per_user": 9}},
    )
    assert await resolve_max_jobs_per_user() == 9
