from __future__ import annotations

import pytest

from jiuwenswarm.gateway.cron.models import (
    CRON_DEFAULT_TIMEOUT_SECONDS,
    CRON_JOB_DEFAULT_MODE,
    CRON_JOB_MODES,
    CRON_MAX_TIMEOUT_SECONDS,
    CRON_TEAM_DEFAULT_TIMEOUT_SECONDS,
    CronJob,
    coerce_cron_job_mode,
    cron_job_metadata,
    is_team_cron_mode,
    normalize_cron_job_mode,
    normalize_cron_job_timeout_seconds,
    resolve_cron_job_timeout_seconds,
)


@pytest.mark.parametrize("mode", sorted(CRON_JOB_MODES))
def test_normalize_cron_job_mode_accepts_supported_values(mode: str) -> None:
    expected = "agent" if mode in {"plan", "agent.plan", "agent.fast"} else mode
    assert normalize_cron_job_mode(mode) == expected
    assert normalize_cron_job_mode(mode.upper()) == expected


def test_normalize_cron_job_mode_defaults_to_agent() -> None:
    assert normalize_cron_job_mode(None) == CRON_JOB_DEFAULT_MODE
    assert normalize_cron_job_mode("") == CRON_JOB_DEFAULT_MODE
    assert CRON_JOB_DEFAULT_MODE == "agent"


def test_normalize_cron_job_mode_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="Invalid cron job mode"):
        normalize_cron_job_mode("unknown-mode")


@pytest.mark.parametrize(
    "mode",
    ["team", "team.plan", "code.team", "TEAM"],
)
def test_is_team_cron_mode_true(mode: str) -> None:
    assert is_team_cron_mode(mode) is True


@pytest.mark.parametrize(
    "mode",
    ["agent", "plan", "agent.plan", "", None],
)
def test_is_team_cron_mode_false(mode: str | None) -> None:
    assert is_team_cron_mode(mode) is False


def test_coerce_cron_job_mode_passthrough_unknown() -> None:
    assert coerce_cron_job_mode("future.mode") == "future.mode"
    assert coerce_cron_job_mode("Future.Mode") == "future.mode"


def test_coerce_cron_job_mode_known_values() -> None:
    assert coerce_cron_job_mode("team") == "team"
    assert coerce_cron_job_mode(None, default=CRON_JOB_DEFAULT_MODE) == CRON_JOB_DEFAULT_MODE


def test_cron_job_default_mode_matches_normalize_default() -> None:
    assert normalize_cron_job_mode(None) == CRON_JOB_DEFAULT_MODE


def test_cron_job_metadata_matches_modes_and_default() -> None:
    meta = cron_job_metadata()
    assert set(meta["modes"]) == CRON_JOB_MODES
    assert meta["default_mode"] == CRON_JOB_DEFAULT_MODE
    assert meta["default_timeout_seconds"] == CRON_DEFAULT_TIMEOUT_SECONDS
    assert meta["default_team_timeout_seconds"] == CRON_TEAM_DEFAULT_TIMEOUT_SECONDS
    assert meta["max_timeout_seconds"] == CRON_MAX_TIMEOUT_SECONDS
    assert meta["modes"] == sorted(meta["modes"])


def test_resolve_cron_job_timeout_seconds_defaults_by_mode() -> None:
    normal_job = CronJob(
        id="j1",
        name="normal",
        enabled=True,
        cron_expr="0 9 * * *",
        timezone="Asia/Shanghai",
        description="task",
        targets="tui",
        mode="agent.fast",
    )
    team_job = CronJob(
        id="j2",
        name="team",
        enabled=True,
        cron_expr="0 9 * * *",
        timezone="Asia/Shanghai",
        description="task",
        targets="tui",
        mode="team",
    )
    assert resolve_cron_job_timeout_seconds(normal_job) == CRON_DEFAULT_TIMEOUT_SECONDS
    assert resolve_cron_job_timeout_seconds(team_job) == CRON_TEAM_DEFAULT_TIMEOUT_SECONDS


def test_resolve_cron_job_timeout_seconds_uses_user_override() -> None:
    job = CronJob(
        id="j3",
        name="custom",
        enabled=True,
        cron_expr="0 9 * * *",
        timezone="Asia/Shanghai",
        description="task",
        targets="tui",
        mode="team",
        timeout_seconds=1800,
    )
    assert resolve_cron_job_timeout_seconds(job) == 1800


def test_normalize_cron_job_timeout_seconds_rejects_invalid_values() -> None:
    assert normalize_cron_job_timeout_seconds(None) is None
    with pytest.raises(ValueError, match="at least 60"):
        normalize_cron_job_timeout_seconds(30)
    assert normalize_cron_job_timeout_seconds(CRON_MAX_TIMEOUT_SECONDS) == CRON_MAX_TIMEOUT_SECONDS
    with pytest.raises(ValueError, match="at most"):
        normalize_cron_job_timeout_seconds(CRON_MAX_TIMEOUT_SECONDS + 1)


# ===========================================================================
# CronJobStore 惰性迁移:list_jobs 读取时为缺 work_mode 的老 job 推断并写回磁盘
# 替代原 migrate_legacy_jobs_at_startup 启动迁移
# ===========================================================================
import json
from pathlib import Path
from unittest.mock import patch

from jiuwenswarm.gateway.cron.store import CronJobStore


def _write_cron_jobs(path: Path, jobs: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "jobs": jobs}, ensure_ascii=False), encoding="utf-8")


def _read_cron_jobs(path: Path) -> list[dict]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8") or "{}")
    return data.get("jobs") if isinstance(data, dict) else []


def _make_legacy_job(job_id: str, **overrides) -> dict:
    """构造缺 work_mode 的老 cron job raw dict。"""
    base = {
        "id": job_id,
        "name": "legacy",
        "enabled": True,
        "cron_expr": "0 9 * * *",
        "timezone": "Asia/Shanghai",
        "description": "legacy job",
        "targets": [],
        "mode": "agent",
        "project_id": "",
        "wake_offset_seconds": 300,
    }
    base.update(overrides)
    return base


class TestCronJobLazyMigration:
    """list_jobs 读取老 job 时按需推断 work_mode 并写回磁盘。"""

    @pytest.mark.asyncio
    async def test_legacy_job_without_work_mode_inferred_from_tui_targets(self, tmp_path):
        """缺 work_mode 的老 job,按 targets.channel_id=tui 推断为 code 并写盘。"""
        store_path = tmp_path / "cron_jobs.json"
        _write_cron_jobs(store_path, [
            _make_legacy_job("j1", targets=[{"channel_id": "tui"}]),
        ])

        store = CronJobStore(path=store_path)
        jobs = await store.list_jobs()

        assert jobs[0].work_mode == "code"
        # 写盘后磁盘也有 work_mode
        disk_jobs = _read_cron_jobs(store_path)
        assert disk_jobs[0]["work_mode"] == "code"

    @pytest.mark.asyncio
    async def test_legacy_job_without_work_mode_inferred_from_web_targets(self, tmp_path):
        """缺 work_mode 的老 job,targets 含 web 通道时推断为 work。"""
        store_path = tmp_path / "cron_jobs.json"
        _write_cron_jobs(store_path, [
            _make_legacy_job("j1", targets=[{"channel_id": "web"}]),
        ])

        store = CronJobStore(path=store_path)
        jobs = await store.list_jobs()

        assert jobs[0].work_mode == "work"
        assert _read_cron_jobs(store_path)[0]["work_mode"] == "work"

    @pytest.mark.asyncio
    async def test_legacy_job_targets_string_format_tui(self, tmp_path):
        """targets 为新格式 string "tui" 时,推断为 code。"""
        store_path = tmp_path / "cron_jobs.json"
        _write_cron_jobs(store_path, [
            _make_legacy_job("j1", targets="tui"),
        ])

        store = CronJobStore(path=store_path)
        jobs = await store.list_jobs()

        assert jobs[0].work_mode == "code"
        assert _read_cron_jobs(store_path)[0]["work_mode"] == "code"

    @pytest.mark.asyncio
    async def test_legacy_job_targets_string_format_web(self, tmp_path):
        """targets 为新格式 string "web" 时,推断为 work。"""
        store_path = tmp_path / "cron_jobs.json"
        _write_cron_jobs(store_path, [
            _make_legacy_job("j1", targets="web"),
        ])

        store = CronJobStore(path=store_path)
        jobs = await store.list_jobs()

        assert jobs[0].work_mode == "work"

    @pytest.mark.asyncio
    async def test_legacy_job_work_mode_inferred_from_project_id(self, tmp_path, monkeypatch):
        """缺 work_mode 但有 project_id 的老 job,按 Project 反查继承 work_mode。"""
        # 准备 project_store: 创建一个 code 模式项目
        root = tmp_path / "agent"
        root.mkdir()
        monkeypatch.setattr(
            "jiuwenswarm.server.runtime.session.project_store.get_agent_root_dir",
            lambda: root,
        )
        from jiuwenswarm.server.runtime.session import project_store
        project_store.invalidate_cache()
        proj = project_store.create_project("P", str(tmp_path / "app"), work_mode="code")

        store_path = tmp_path / "cron_jobs.json"
        _write_cron_jobs(store_path, [
            _make_legacy_job("j1", project_id=proj.project_id, targets=[{"channel_id": "web"}]),
        ])

        store = CronJobStore(path=store_path)
        jobs = await store.list_jobs()

        # project_id 命中 code 项目,继承 work_mode="code"(而非按 web 通道推断为 work)
        assert jobs[0].work_mode == "code"
        assert _read_cron_jobs(store_path)[0]["work_mode"] == "code"

    @pytest.mark.asyncio
    async def test_legacy_job_project_id_hits_hidden_project(self, tmp_path, monkeypatch):
        """project_id 命中已隐藏项目时,仍继承该项目的 work_mode(最准确的归属)。"""
        root = tmp_path / "agent"
        root.mkdir()
        monkeypatch.setattr(
            "jiuwenswarm.server.runtime.session.project_store.get_agent_root_dir",
            lambda: root,
        )
        from jiuwenswarm.server.runtime.session import project_store
        project_store.invalidate_cache()
        proj = project_store.create_project("P", str(tmp_path / "app"), work_mode="code")
        project_store.hide_project(proj.project_id)  # 软删除(hidden=True)

        store_path = tmp_path / "cron_jobs.json"
        _write_cron_jobs(store_path, [
            _make_legacy_job("j1", project_id=proj.project_id, targets=[{"channel_id": "web"}]),
        ])

        store = CronJobStore(path=store_path)
        jobs = await store.list_jobs()

        # 隐藏项目仍命中,继承 work_mode="code"
        assert jobs[0].work_mode == "code"

    @pytest.mark.asyncio
    async def test_valid_work_mode_not_overwritten(self, tmp_path):
        """已有合法 work_mode 的 job 不被迁移覆盖。"""
        store_path = tmp_path / "cron_jobs.json"
        _write_cron_jobs(store_path, [
            _make_legacy_job("j1", work_mode="work", targets=[{"channel_id": "tui"}]),
        ])

        store = CronJobStore(path=store_path)
        jobs = await store.list_jobs()

        # 已有合法 work_mode="work",不按 tui 通道覆盖为 code
        assert jobs[0].work_mode == "work"

    @pytest.mark.asyncio
    async def test_invalid_work_mode_replaced(self, tmp_path):
        """非法 work_mode 值被推断替换。"""
        store_path = tmp_path / "cron_jobs.json"
        _write_cron_jobs(store_path, [
            _make_legacy_job("j1", work_mode="invalid", targets=[{"channel_id": "tui"}]),
        ])

        store = CronJobStore(path=store_path)
        jobs = await store.list_jobs()

        assert jobs[0].work_mode == "code"
        assert _read_cron_jobs(store_path)[0]["work_mode"] == "code"

    @pytest.mark.asyncio
    async def test_mixed_legacy_and_valid_jobs(self, tmp_path):
        """混合场景:老 job 迁移、新 job 不动,只写回有变更的部分。"""
        store_path = tmp_path / "cron_jobs.json"
        _write_cron_jobs(store_path, [
            _make_legacy_job("j1", targets=[{"channel_id": "tui"}]),  # 缺 work_mode
            _make_legacy_job("j2", work_mode="work", targets=[{"channel_id": "web"}]),  # 已有合法
            _make_legacy_job("j3", targets=[{"channel_id": "web"}]),  # 缺 work_mode
        ])

        store = CronJobStore(path=store_path)
        jobs = await store.list_jobs()
        by_id = {j.id: j for j in jobs}

        assert by_id["j1"].work_mode == "code"
        assert by_id["j2"].work_mode == "work"
        assert by_id["j3"].work_mode == "work"

        # 磁盘写回
        disk = {j["id"]: j for j in _read_cron_jobs(store_path)}
        assert disk["j1"]["work_mode"] == "code"
        assert disk["j2"]["work_mode"] == "work"
        assert disk["j3"]["work_mode"] == "work"

    @pytest.mark.asyncio
    async def test_no_migration_when_all_jobs_valid(self, tmp_path):
        """所有 job 都有合法 work_mode 时,不触发 lookup 与 writeback(零开销)。"""
        store_path = tmp_path / "cron_jobs.json"
        original_jobs = [
            _make_legacy_job("j1", work_mode="work", targets=[{"channel_id": "web"}]),
            _make_legacy_job("j2", work_mode="code", targets=[{"channel_id": "tui"}]),
        ]
        _write_cron_jobs(store_path, original_jobs)
        original_mtime = store_path.stat().st_mtime

        store = CronJobStore(path=store_path)

        # mock _build_cron_project_lookup 验证不被调用
        with patch(
            "jiuwenswarm.gateway.cron.store._build_cron_project_lookup"
        ) as mock_lookup:
            jobs = await store.list_jobs()
            mock_lookup.assert_not_called()

        # 文件未被改写
        assert store_path.stat().st_mtime == original_mtime
