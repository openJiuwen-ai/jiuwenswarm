# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
from __future__ import annotations

import json

import pytest

from jiuwenswarm.common.work_mode import (
    DEFAULT_PROJECT_ID_WORK,
    DEFAULT_WEB_WORK_MODE,
    is_default_project_id,
    normalize_work_mode,
    resolve_default_project_id,
)
from jiuwenswarm.gateway.cron.models import CronJob
from jiuwenswarm.server.runtime.session.project_store import Project
from jiuwenswarm.server.runtime.session.work_mode import (
    DEFAULT_TUI_WORK_MODE,
    default_work_mode_for_channel,
    infer_legacy_project_work_mode,
    resolve_request_work_mode,
    resolve_session_work_mode_params,
)


class TestPureHelpers:
    @pytest.mark.parametrize("fn, arg, expected", [
        (normalize_work_mode, "WORK", "work"),
        (normalize_work_mode, "cod", DEFAULT_WEB_WORK_MODE),
        (is_default_project_id, "default", True),
        (is_default_project_id, "proj_abc", False),
        (resolve_default_project_id, "work", DEFAULT_PROJECT_ID_WORK),
        (default_work_mode_for_channel, "tui", DEFAULT_TUI_WORK_MODE),
    ])
    def test_pure_helpers(self, fn, arg, expected):
        assert fn(arg) == expected


class TestRequestResolution:
    @pytest.mark.parametrize("params, channel, expected_value, expected_error", [
        ({}, "web", "work", None),
        ({"work_mode": "code"}, "web", "code", None),
        ({"work_mode": "cod"}, "web", None, "BAD_REQUEST"),
    ])
    def test_resolve_request_work_mode(self, params, channel, expected_value, expected_error):
        value, error = resolve_request_work_mode(params, channel)
        assert value == expected_value
        assert error == expected_error

    @pytest.mark.parametrize("data, expected", [
        ({"work_mode": "code"}, "code"),
        ({"name": "demo"}, "work"),
    ])
    def test_infer_legacy_project_work_mode(self, data, expected):
        assert infer_legacy_project_work_mode(data) == expected

    @pytest.mark.parametrize("params, channel_id, expected_pid, expected_mode", [
        ({}, "web", "default", "work"),
        ({"project_id": "default_code"}, "tui", "default_code", "code"),
    ])
    def test_default_project_mapping(self, params, channel_id, expected_pid, expected_mode):
        result = resolve_session_work_mode_params(params, channel_id=channel_id)
        assert result.project_id == expected_pid
        assert result.work_mode == expected_mode
        assert result.error is None

    def test_invalid_work_mode_returns_bad_request(self):
        result = resolve_session_work_mode_params({"work_mode": "invalid"}, channel_id="web")
        assert result.code == "BAD_REQUEST"
        assert result.project_id == ""
        assert result.work_mode == ""

    @pytest.mark.parametrize("project_id, expected_pid, expected_mode", [
        (None, "default", "work"),
        ("default", "default", "work"),
        ("default_code", "default_code", "code"),
    ])
    def test_default_project_preserves_project_dir(self, project_id, expected_pid, expected_mode):
        # 纯参数归一化,不直接拒绝;project_dir 透传给 resolve_session_project_binding
        params = {"project_dir": "/tmp/demo"}
        if project_id is not None:
            params["project_id"] = project_id
        result = resolve_session_work_mode_params(params, channel_id="web")
        assert result.project_id == expected_pid
        assert result.project_dir == "/tmp/demo"
        assert result.work_mode == expected_mode
        assert result.error is None


class TestModelRoundtrip:
    @pytest.mark.parametrize("raw, expected_work_mode, expected_git", [
        ({"project_id": "proj_1", "name": "demo", "project_dir": "/tmp/demo"}, "work", {}),
        ({"project_id": "proj_2", "name": "c", "project_dir": "/tmp/c", "work_mode": "code"}, "code", {}),
        ({"project_id": "proj_3", "name": "c", "project_dir": "/tmp/c", "git": {"enabled": True, "branch": "main"}},
         "work", {"enabled": True, "branch": "main"}),
    ])
    def test_project_from_dict(self, raw, expected_work_mode, expected_git):
        p = Project.from_dict(raw)
        assert p.work_mode == expected_work_mode
        assert p.git == expected_git

    def test_project_and_cronjob_roundtrip(self):
        p = Project(project_id="proj_6", name="demo", project_dir="/tmp/demo",
                    work_mode="code", git={"enabled": True, "branch": "dev"})
        d = p.to_dict()
        assert d["work_mode"] == "code"
        assert d["git"] == {"enabled": True, "branch": "dev"}
        assert Project.from_dict(d).git == {"enabled": True, "branch": "dev"}

        base_job = {"id": "j1", "name": "n", "enabled": True, "cron_expr": "0 0 * * *",
                    "timezone": "UTC", "description": "d", "targets": "web"}
        assert CronJob.from_dict(dict(base_job)).to_dict()["work_mode"] == "work"
        raw_code = dict(base_job, work_mode="code")
        assert CronJob.from_dict(raw_code).to_dict()["work_mode"] == "code"

    @pytest.mark.parametrize("raw_overrides, expected_work_mode", [
        ({}, "work"),
        ({"work_mode": "invalid"}, "work"),
        ({"work_mode": "code"}, "code"),
    ])
    def test_cronjob_from_dict(self, raw_overrides, expected_work_mode):
        base = {"id": "j1", "name": "n", "enabled": True, "cron_expr": "0 0 * * *",
                "timezone": "UTC", "description": "d", "targets": "web"}
        cj = CronJob.from_dict({**base, **raw_overrides})
        assert cj.work_mode == expected_work_mode


class TestSessionMetadataWorkMode:
    @pytest.fixture()
    def sessions_dir(self, tmp_path, monkeypatch):
        d = tmp_path / "sessions"
        d.mkdir()
        monkeypatch.setattr(
            "jiuwenswarm.server.runtime.session.session_metadata.get_agent_sessions_dir",
            lambda: d,
        )
        from jiuwenswarm.server.runtime.session.session_metadata import _METADATA_CACHE
        _METADATA_CACHE.clear()
        return d

    def _drain_queue(self):
        from jiuwenswarm.server.runtime.session.session_metadata import _METADATA_QUEUE
        _METADATA_QUEUE.join()

    @pytest.mark.parametrize("channel_id, work_mode, expected", [
        ("web", "code", "code"),
        ("web", None, "work"),
    ])
    def test_init_work_mode(self, sessions_dir, channel_id, work_mode, expected):
        from jiuwenswarm.server.runtime.session.session_metadata import (
            get_session_metadata, init_session_metadata,
        )
        init_session_metadata(session_id=f"sess_{channel_id}", channel_id=channel_id, work_mode=work_mode)
        assert get_session_metadata(f"sess_{channel_id}")["work_mode"] == expected

    def test_update_first_lock_and_repair(self, sessions_dir):
        from jiuwenswarm.server.runtime.session.session_metadata import (
            get_session_metadata, init_session_metadata, update_session_metadata,
        )
        init_session_metadata(session_id="sess_lock", channel_id="web", work_mode="code")
        update_session_metadata(session_id="sess_lock", work_mode="work")
        self._drain_queue()
        # 首次 init 后 update 不可覆盖 work_mode(锁定语义)
        assert get_session_metadata("sess_lock")["work_mode"] == "code"

        # 损坏数据(cod 非法值)由 update 修复
        sid = "sess_repair"
        meta_path = sessions_dir / sid / "metadata.json"
        meta_path.parent.mkdir()
        meta_path.write_text(json.dumps({
            "session_id": sid, "channel_id": "web",
            "project_dir": "", "project_id": "", "work_mode": "cod",
        }), encoding="utf-8")
        update_session_metadata(session_id=sid, channel_id="web", work_mode="code")
        self._drain_queue()
        assert get_session_metadata(sid, cache_bust=True)["work_mode"] == "code"
