"""Issue #2503：TUI session.create 落盘路径与 /resume current-dir 过滤。"""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.gateway.channel_manager.tui.tui_connect import (
    CliHandlersBindParams,
    build_tui_session_create_channel_metadata,
    register_cli_handlers,
    resolve_tui_session_project_path,
    tui_session_matches_project_dir,
)


class _TuiChannel:
    def __init__(self) -> None:
        self.local_handlers: dict[str, dict[str, object]] = {}
        self.responses: list[dict] = []

    def register_local_handler(self, path, method, handler) -> None:
        self.local_handlers.setdefault(path, {})[method] = handler

    async def send_response(self, _ws, req_id, *, ok, payload=None, error=None, code=None):
        self.responses.append(
            {"id": req_id, "ok": ok, "payload": payload or {}, "error": error, "code": code}
        )


class TestResolveTuiSessionProjectPath:
    def test_prefers_channel_metadata_project_dir(self):
        session = {
            "project_dir": "E:/fallback",
            "channel_metadata": {"project_dir": "E:/from-meta", "cwd": "E:/cwd"},
        }
        assert resolve_tui_session_project_path(session) == "E:/from-meta"

    def test_falls_back_to_channel_metadata_cwd(self):
        session = {
            "project_dir": "E:/fallback",
            "channel_metadata": {"cwd": "E:/cwd-only"},
        }
        assert resolve_tui_session_project_path(session) == "E:/cwd-only"

    def test_falls_back_to_top_level_project_dir(self):
        """复现截图场景：create 只写顶层、无 channel_metadata。"""
        session = {"project_dir": r"E:\jiuwenswarm", "channel_metadata": {}}
        assert resolve_tui_session_project_path(session) == r"E:\jiuwenswarm"

    def test_empty_when_no_path(self):
        assert resolve_tui_session_project_path({"channel_id": "tui"}) == ""
        assert resolve_tui_session_project_path(None) == ""


class TestTuiSessionMatchesProjectDir:
    def test_all_projects_always_matches(self):
        assert tui_session_matches_project_dir(
            {"project_dir": ""}, r"E:\jiuwenswarm", show_all_projects=True
        )

    def test_matches_top_level_without_channel_metadata(self, tmp_path: Path):
        project = tmp_path / "proj"
        project.mkdir()
        session = {"project_dir": str(project), "channel_id": "tui"}
        assert tui_session_matches_project_dir(session, str(project)) is True

    def test_excludes_when_path_missing(self, tmp_path: Path):
        project = tmp_path / "proj"
        project.mkdir()
        assert tui_session_matches_project_dir({"channel_id": "tui"}, str(project)) is False

    def test_matches_nested_session_under_project(self, tmp_path: Path):
        project = tmp_path / "proj"
        nested = project / "pkg"
        nested.mkdir(parents=True)
        session = {
            "channel_metadata": {"cwd": str(nested)},
        }
        assert tui_session_matches_project_dir(session, str(project)) is True


class TestBuildTuiSessionCreateChannelMetadata:
    def test_uses_resolved_project_dir(self, monkeypatch):
        monkeypatch.setattr(
            "jiuwenswarm.common.utils.resolve_git_branch",
            lambda _path: "main",
        )
        meta = build_tui_session_create_channel_metadata(
            {"cwd": "E:/ignored"},
            resolved_project_dir="E:/resolved",
        )
        assert meta == {
            "project_dir": "E:/resolved",
            "cwd": "E:/resolved",
            "git_branch": "main",
        }

    def test_falls_back_to_params_cwd(self, monkeypatch):
        monkeypatch.setattr(
            "jiuwenswarm.common.utils.resolve_git_branch",
            lambda _path: "HEAD",
        )
        meta = build_tui_session_create_channel_metadata(
            {"cwd": r"E:\jiuwenswarm"},
            resolved_project_dir="",
        )
        assert meta["project_dir"] == r"E:\jiuwenswarm"
        assert meta["cwd"] == r"E:\jiuwenswarm"

    def test_returns_none_without_path(self):
        assert build_tui_session_create_channel_metadata({}, resolved_project_dir="") is None


class TestInitSessionMetadataChannelMetadata:
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

    def test_persists_channel_metadata_on_init(self, sessions_dir: Path):
        from jiuwenswarm.server.runtime.session.session_metadata import init_session_metadata

        init_session_metadata(
            session_id="tui_new",
            channel_id="tui",
            project_dir=r"E:\jiuwenswarm",
            channel_metadata={
                "project_dir": r"E:\jiuwenswarm",
                "cwd": r"E:\jiuwenswarm",
            },
        )
        data = json.loads(
            (sessions_dir / "tui_new" / "metadata.json").read_text(encoding="utf-8")
        )
        assert data["project_dir"] == r"E:\jiuwenswarm"
        assert data["channel_metadata"]["cwd"] == r"E:\jiuwenswarm"


@pytest.mark.asyncio
async def test_session_create_uses_server_allocated_project_path_for_resume(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """AgentServer-owned create keeps a top-level project path usable by /resume."""
    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()
    project = tmp_path / "workspace"
    project.mkdir()
    project_dir = str(project)

    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_agent_sessions_dir", lambda: sessions_root
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_agent_sessions_dir",
        lambda: sessions_root,
    )
    from jiuwenswarm.server.runtime.session.session_metadata import _METADATA_CACHE

    _METADATA_CACHE.clear()

    monkeypatch.setattr(
        "jiuwenswarm.common.utils.resolve_git_branch",
        lambda _path: "HEAD",
    )

    class _CreateAgentClient:
        def __init__(self) -> None:
            self.requests = []

        async def send_request(self, env):
            self.requests.append(env)
            from jiuwenswarm.server.runtime.session.session_metadata import init_session_metadata

            init_session_metadata(
                session_id="tui_6a601aa3_f4ed87",
                channel_id="tui",
                project_dir=env.params["project_dir"],
                project_id="proj_code_1",
                work_mode="code",
                mode=env.params["mode"],
            )
            return SimpleNamespace(
                ok=True,
                payload={
                    "session_id": "tui_6a601aa3_f4ed87",
                    "projectId": "proj_code_1",
                    "projectDir": env.params["project_dir"],
                    "workMode": "code",
                    "prewarm_hit": True,
                    "prewarm_status": "ready",
                },
            )

    channel = _TuiChannel()
    agent_client = _CreateAgentClient()
    register_cli_handlers(
        CliHandlersBindParams(channel=channel, agent_client=agent_client, path="/tui")
    )

    await channel.local_handlers["/tui"]["session.create"](
        object(),
        "req-create",
        {
            "cwd": project_dir,
            "project_dir": project_dir,
            "mode": "code.normal",
            "create_token": "tui-project-path-create",
        },
        "previous",
    )

    assert channel.responses[-1]["ok"] is True
    meta_path = sessions_root / "tui_6a601aa3_f4ed87" / "metadata.json"
    data = json.loads(meta_path.read_text(encoding="utf-8"))
    assert data["project_dir"] == project_dir
    assert agent_client.requests[0].method == "session.create"
    assert agent_client.requests[0].params["cwd"] == project_dir
    assert "session_id" not in agent_client.requests[0].params

    # 模拟 /resume current-dir：刚创建会话应命中（排除当前 sid 的逻辑由 list handler 负责）
    assert tui_session_matches_project_dir(data, project_dir) is True


@pytest.mark.asyncio
async def test_session_list_current_dir_includes_top_level_only_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """存量会话仅有顶层 project_dir 时，current-dir 列表仍应包含。"""
    project = tmp_path / "proj"
    project.mkdir()
    project_dir = str(project)

    class _ListAgentClient:
        async def send_request(self, _env):
            return SimpleNamespace(
                ok=True,
                payload={
                    "sessions": [
                        {
                            "session_id": "tui_old_no_channel_meta",
                            "channel_id": "tui",
                            "project_dir": project_dir,
                            "last_message_at": 100.0,
                        },
                        {
                            "session_id": "tui_other_project",
                            "channel_id": "tui",
                            "project_dir": str(tmp_path / "other"),
                            "last_message_at": 90.0,
                        },
                    ]
                },
            )

    channel = _TuiChannel()
    register_cli_handlers(
        CliHandlersBindParams(
            channel=channel,
            agent_client=_ListAgentClient(),
            path="/tui",
        )
    )

    await channel.local_handlers["/tui"]["session.list"](
        object(),
        "req-list",
        {"project_dir": project_dir, "limit": 20},
        "current-sid",
    )

    resp = channel.responses[-1]
    assert resp["ok"] is True
    sessions = resp["payload"]["sessions"]
    assert [s["session_id"] for s in sessions] == ["tui_old_no_channel_meta"]
    assert sessions[0]["project_dir"] == os.path.realpath(project_dir)
