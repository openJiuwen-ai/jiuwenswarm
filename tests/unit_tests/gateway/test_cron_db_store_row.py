"""Gateway DB cron_job row mapping — SQLAlchemy records use to_dict(), not dataclasses."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from jiuwenswarm.gateway.cron.db_store import _record_to_mapping, _row_to_job


class _SqlAlchemyStyleCronJobRecord:
    """Mimics openjiuwen_runtime Cron_jobRecord: to_dict + empty annotations."""

    __annotations__ = {}

    def __init__(self) -> None:
        self.job_id = "abc123"
        self.name = "喝水提醒"
        self.enabled = True
        self.expired = False
        # Quartz 7-field: second minute hour day month dow year
        self.cron_expr = "47 3 7 20 7 ? 2026"
        self.timezone = "Asia/Shanghai"
        self.wake_offset_seconds = 59
        self.description = "该喝水了！"
        self.targets = "web"
        self.session_id = "sess_x"
        self.chat_type = None
        self.mode = "agent"
        self.delete_after_run = True
        self.group_id = "1"
        self.bot_id = "122"
        self.user_id = "1"
        self.created_at = datetime(2026, 7, 20, 7, 3, 47)
        self.updated_at = datetime(2026, 7, 20, 7, 3, 47)
        self.data = None

    def to_dict(self) -> dict:
        return {
            "id": 3,
            "job_id": self.job_id,
            "name": self.name,
            "enabled": self.enabled,
            "expired": self.expired,
            "cron_expr": self.cron_expr,
            "timezone": self.timezone,
            "wake_offset_seconds": self.wake_offset_seconds,
            "description": self.description,
            "targets": self.targets,
            "session_id": self.session_id,
            "chat_type": self.chat_type,
            "mode": self.mode,
            "delete_after_run": self.delete_after_run,
            "group_id": self.group_id,
            "bot_id": self.bot_id,
            "user_id": self.user_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "data": self.data,
        }


def test_row_to_job_uses_to_dict_when_annotations_empty() -> None:
    job = _row_to_job(_SqlAlchemyStyleCronJobRecord())
    assert job is not None
    assert job.id == "abc123"
    assert job.name == "喝水提醒"
    assert job.enabled is True
    assert job.wake_offset_seconds == 59
    assert job.targets == "web"


def test_row_to_job_accepts_plain_dict() -> None:
    job = _row_to_job(
        {
            "job_id": "jid1",
            "name": "n",
            "enabled": True,
            "expired": False,
            "cron_expr": "0 * * * *",
            "timezone": "UTC",
            "wake_offset_seconds": 60,
            "description": "d",
            "targets": "web",
        }
    )
    assert job is not None
    assert job.id == "jid1"


def test_record_to_mapping_without_dict_protocol_returns_empty() -> None:
    """Plain objects without to_dict / model_dump / keys map to {} (no attr scrape)."""
    row = SimpleNamespace(
        job_id="x",
        name="n",
        enabled=True,
        expired=False,
        cron_expr="0 * * * *",
        timezone="UTC",
        wake_offset_seconds=10,
        description="d",
        targets="web",
    )
    mapping = _record_to_mapping(row)
    assert mapping == {}
    assert _row_to_job(row) is None
