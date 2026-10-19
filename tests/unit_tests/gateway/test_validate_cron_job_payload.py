from __future__ import annotations

import pytest

from jiuwenswarm.gateway.cron.models import (
    CRON_JOB_DESCRIPTION_MAX_LENGTH,
    CRON_JOB_NAME_MAX_LENGTH,
    CronJob,
    validate_cron_job_payload,
)


def _valid_payload(**overrides):
    base = {
        "id": "job-1",
        "name": "drink",
        "cron_expr": "0 0 9 * * ? *",
        "timezone": "Asia/Shanghai",
        "description": "remind to drink water",
        "targets": "web",
        "enabled": True,
        "mode": "agent",
    }
    base.update(overrides)
    return base


def test_validate_accepts_valid_payload() -> None:
    validate_cron_job_payload(_valid_payload(), strict_mode=True)


def test_validate_rejects_overlong_description() -> None:
    with pytest.raises(ValueError, match="description must be at most"):
        validate_cron_job_payload(
            _valid_payload(description="x" * (CRON_JOB_DESCRIPTION_MAX_LENGTH + 1)),
            strict_mode=True,
        )


def test_validate_rejects_overlong_name() -> None:
    with pytest.raises(ValueError, match="name must be at most"):
        validate_cron_job_payload(
            _valid_payload(name="n" * (CRON_JOB_NAME_MAX_LENGTH + 1)),
            strict_mode=True,
        )


def test_validate_strict_mode_rejects_invalid_mode() -> None:
    with pytest.raises(ValueError, match="Invalid cron job mode"):
        validate_cron_job_payload(_valid_payload(mode="not-a-mode"), strict_mode=True)


def test_validate_lenient_mode_allows_unknown_mode_for_from_dict() -> None:
    # Deserialize path must still load legacy unknown modes via coerce.
    validate_cron_job_payload(_valid_payload(mode="legacy.custom"), strict_mode=False)
    job = CronJob.from_dict(_valid_payload(mode="legacy.custom"))
    assert job.mode == "legacy.custom"


def test_from_dict_still_rejects_overlong_description() -> None:
    with pytest.raises(ValueError, match="description must be at most"):
        CronJob.from_dict(
            _valid_payload(description="y" * (CRON_JOB_DESCRIPTION_MAX_LENGTH + 1))
        )


def test_validate_treats_null_wake_offset_as_zero() -> None:
    validate_cron_job_payload(
        _valid_payload(wake_offset_seconds=None),
        strict_mode=True,
    )
    job = CronJob.from_dict(_valid_payload(wake_offset_seconds=None))
    assert job.wake_offset_seconds == 0
