import logging
import sys
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.exc import OperationalError


@pytest.fixture(autouse=True)
def _reset_team_db_lock_patch(monkeypatch):
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


@pytest.mark.asyncio
async def test_non_lock_operational_error_is_not_retried(monkeypatch):
    from openjiuwen.agent_teams.tools.database.member_dao import MemberDao

    from jiuwenswarm.openjiuwen_team_db_lock_patch import install_team_db_lock_retry_patch

    failure = OperationalError(
        "UPDATE team_member SET execution_status=?",
        {},
        Exception("disk I/O error"),
    )
    original = AsyncMock(side_effect=failure)
    monkeypatch.setattr(MemberDao, "update_member_execution_status", original)

    assert install_team_db_lock_retry_patch()
    actual = object.__new__(MemberDao)
    with pytest.raises(OperationalError) as raised:
        await actual.update_member_execution_status("worker-1", "team-1", "idle")
    assert raised.value is failure
    assert original.await_count == 1


def test_repeated_install_does_not_wrap_method_again():
    from openjiuwen.agent_teams.tools.database.member_dao import MemberDao

    from jiuwenswarm.openjiuwen_team_db_lock_patch import install_team_db_lock_retry_patch

    assert install_team_db_lock_retry_patch()
    wrapper = MemberDao.update_member_execution_status

    assert install_team_db_lock_retry_patch()
    assert MemberDao.update_member_execution_status is wrapper


def test_reset_does_not_overwrite_replacement_method(monkeypatch):
    from openjiuwen.agent_teams.tools.database.member_dao import MemberDao

    from jiuwenswarm.openjiuwen_team_db_lock_patch import (
        install_team_db_lock_retry_patch,
        reset_team_db_lock_retry_patch,
    )

    assert install_team_db_lock_retry_patch()
    replacement = AsyncMock()
    monkeypatch.setattr(MemberDao, "update_member_execution_status", replacement)

    reset_team_db_lock_retry_patch()

    assert MemberDao.update_member_execution_status is replacement


def test_reset_logs_warning_when_sdk_dao_cannot_be_imported(monkeypatch, caplog):
    from jiuwenswarm.openjiuwen_team_db_lock_patch import (
        install_team_db_lock_retry_patch,
        reset_team_db_lock_retry_patch,
    )

    assert install_team_db_lock_retry_patch()
    monkeypatch.setitem(
        sys.modules,
        "openjiuwen.agent_teams.tools.database.member_dao",
        None,
    )

    with caplog.at_level(logging.WARNING, logger="jiuwenswarm.openjiuwen_team_db_lock_patch"):
        reset_team_db_lock_retry_patch()

    assert "cannot restore MemberDao method" in caplog.text


def test_runtime_entry_continues_when_team_db_patch_raises(monkeypatch, caplog):
    from jiuwenswarm import (
        llm_sse_patch,
        openjiuwen_fs_rwlock_patch,
        openjiuwen_team_db_lock_patch,
    )
    from jiuwenswarm.agents.harness.common.tools import bash_tool_safety
    from jiuwenswarm.common import config, llm_provider_compat_patch, native_tokenizer_cache
    from jiuwenswarm.common import team_workspace_read_guard
    from jiuwenswarm.common.auth import login_credentials
    from jiuwenswarm.server.worker import runtime_backend

    monkeypatch.setattr(runtime_backend.logger, "handlers", [caplog.handler])
    monkeypatch.setattr(runtime_backend.logger, "propagate", False)
    warning = Mock(wraps=runtime_backend.logger.warning)
    monkeypatch.setattr(runtime_backend.logger, "warning", warning)

    calls = []

    def record(stage):
        calls.append(stage)

    def fail_team_db_patch():
        calls.append("team_db")
        raise RuntimeError("SDK patch installation failed")

    monkeypatch.setattr(native_tokenizer_cache, "install_native_tokenizer_cache", lambda: record("native"))
    monkeypatch.setattr(bash_tool_safety, "install_shell_tool_safety_hooks", lambda: record("shell"))
    monkeypatch.setattr(
        openjiuwen_fs_rwlock_patch,
        "install_fs_rwlock_task_affinity_patch",
        lambda: record("fs_rwlock"),
    )
    monkeypatch.setattr(openjiuwen_team_db_lock_patch, "install_team_db_lock_retry_patch", fail_team_db_patch)
    monkeypatch.setattr(llm_provider_compat_patch, "apply_provider_compat_patches", lambda: record("provider"))
    monkeypatch.setattr(team_workspace_read_guard, "install_team_workspace_read_guard", lambda: record("guard"))
    monkeypatch.setattr(
        team_workspace_read_guard,
        "preload_bundled_team_descriptions",
        lambda: record("descriptions"),
    )
    monkeypatch.setattr(login_credentials, "apply_login_credential_patch", lambda: record("login"))
    monkeypatch.setattr(
        config,
        "get_config",
        lambda: {"channels": {"xiaoyi": {"mode": ""}}},
    )
    monkeypatch.setattr(
        llm_sse_patch,
        "apply_openai_sse_invoke_patch",
        lambda: pytest.fail("SSE patch should not be installed"),
    )

    runtime_backend._apply_runtime_entry_patches()

    assert calls == [
        "native",
        "shell",
        "fs_rwlock",
        "team_db",
        "provider",
        "guard",
        "descriptions",
        "login",
    ]
    warning.assert_called_once()
    assert "[TeamDBLockPatch]" in warning.call_args.args[0]
    assert "optional lock-retry installation failed" in warning.call_args.args[0]
    assert warning.call_args.kwargs["exc_info"] is True
