from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import OperationalError


@pytest.fixture(autouse=True)
def _reset_team_db_lock_patch():
    from jiuwenswarm.openjiuwen_team_db_lock_patch import reset_team_db_lock_retry_patch

    reset_team_db_lock_retry_patch()
    yield
    reset_team_db_lock_retry_patch()


@pytest.mark.asyncio
async def test_execution_status_write_retries_sqlite_lock(monkeypatch):
    from openjiuwen.agent_teams.tools.database.member_dao import MemberDao

    from jiuwenswarm.openjiuwen_team_db_lock_patch import install_team_db_lock_retry_patch

    result = object()
    locked = OperationalError(
        "UPDATE team_member SET execution_status=?",
        {},
        Exception("database is locked"),
    )
    original = AsyncMock(side_effect=[locked, result])
    monkeypatch.setattr(MemberDao, "update_member_execution_status", original)

    assert install_team_db_lock_retry_patch()
    actual = object.__new__(MemberDao)
    assert await actual.update_member_execution_status("worker-1", "team-1", "idle") is result
    assert original.await_count == 2
