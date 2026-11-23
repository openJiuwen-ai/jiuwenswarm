"""``get_cron_jobs_path`` is pinned to ``agent/home``.

历史上 getter 曾在 legacy 迁移后优先选择 ``gateway/``；现在 cron 固定读写
``agent/home``。遗留的 ``gateway/cron_jobs.json`` 不再读取、不做迁移也不
删除，原样留在磁盘上；旧布局目录（agent/home、agent/skills、agent/memory）
同样既不搬迁也不清理。
"""

from __future__ import annotations

import contextlib
import json
import logging

import pytest

from agent_gateway.common.paths import get_cron_jobs_path
from jiuwenswarm.common.utils import prepare_workspace


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agent_gateway.common.paths.get_user_workspace_dir", lambda: tmp_path
    )
    return tmp_path


_JOB = {
    "id": "job-1",
    "name": "Example job",
    "enabled": True,
    "expired": False,
    "cron_expr": "0 0 8 * * ? *",
    "timezone": "UTC",
    "description": "Example scheduled job.",
    "targets": "web",
    "session_id": "web_session_1",
    "mode": "agent",
}


def _write_job(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "jobs": [_JOB]}), encoding="utf-8")
    return path


@contextlib.contextmanager
def _scheduler_logs(level=logging.INFO):
    """Collect what the scheduler logs, from the logger that emits it.

    ``caplog`` attaches to the root logger, and ``setup_logger`` sets
    ``propagate = False`` on ``jiuwenswarm`` when it is imported, so these
    records only reach the root on pytest 9.1+, which also attaches to
    non-propagating loggers. Attaching here holds on every version.
    """
    logger = logging.getLogger("agent_gateway.cron.scheduler")
    records: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Collect(level)
    original = logger.level
    logger.setLevel(level)
    logger.addHandler(handler)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(original)


def _scheduler(store):
    """Minimal scheduler for the logging paths: reload() touches only the store
    and its own bookkeeping, so the client and handler are never called."""
    from agent_gateway.cron.scheduler import CronSchedulerService

    return CronSchedulerService(store=store, agent_client=None, message_handler=None)


# ---------------------------------------------------------------------------
# 启动 / 重新初始化：home 原样保留，gateway 遗留文件不读取也不清理
# ---------------------------------------------------------------------------


def test_reinit_preserves_home_cron_and_heartbeat(tmp_path):
    """prepare_workspace(overwrite=True) 不再清理 agent/home。"""
    workspace = tmp_path / ".jiuwenswarm"
    home = workspace / "agent" / "home"
    home.mkdir(parents=True)
    (home / "cron_jobs.json").write_text(
        json.dumps({"version": 1, "jobs": []}), encoding="utf-8"
    )
    (home / "heartbeat_jobs.json").write_text(
        json.dumps({"version": 1, "jobs": []}), encoding="utf-8"
    )

    prepare_workspace(overwrite=True, preferred_language="en", workspace_dir=workspace)

    assert (home / "cron_jobs.json").exists()
    assert (home / "heartbeat_jobs.json").exists()


def test_legacy_layout_dirs_are_left_as_is(tmp_path):
    """旧布局目录既不搬迁也不删除，原样保留。"""
    workspace = tmp_path / ".jiuwenswarm"
    for name in ("home", "skills", "memory"):
        (workspace / "agent" / name).mkdir(parents=True)
    legacy_cron = workspace / "agent" / "home" / "cron_jobs.json"
    legacy_cron.write_text(
        json.dumps({"version": 1, "jobs": [{"id": "legacy-1"}]}), encoding="utf-8"
    )

    prepare_workspace(overwrite=False, preferred_language="en", workspace_dir=workspace)

    assert legacy_cron.exists()
    assert (workspace / "agent" / "skills").is_dir()
    assert (workspace / "agent" / "memory").is_dir()


def test_gateway_leftover_file_is_ignored_and_untouched(tmp_path):
    """gateway 遗留的 cron 文件不读取、不合并、不删除，原样保留。"""
    workspace = tmp_path / ".jiuwenswarm"
    gateway_cron = workspace / "gateway" / "cron_jobs.json"
    gateway_cron.parent.mkdir(parents=True)
    gateway_cron.write_text(
        json.dumps({"version": 1, "jobs": [{"id": "gw-1"}]}), encoding="utf-8"
    )

    prepare_workspace(overwrite=False, preferred_language="en", workspace_dir=workspace)

    assert gateway_cron.exists()
    assert gateway_cron.read_text(encoding="utf-8") == json.dumps(
        {"version": 1, "jobs": [{"id": "gw-1"}]}
    )
