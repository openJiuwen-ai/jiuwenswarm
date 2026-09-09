# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Gateway 用户业务适配器层（``server/runtime/gateway_adapter``）单元测试。

覆盖 Phase 1-3 引入的全部适配器契约，按适配器域组织：
- ``SessionAdapter``：session.list 通道分流投影 / get_metadata / pin / color_set / preview；
- ``WorkspaceFileAdapter``：media/document 落盘、path 选择、url 导入、IM 附件落盘；
- ``MemoryAdapter`` / ``ProjectAdapter`` / ``HarmonyOSAdapter``：注入目录执行与错误映射；
- ``AdapterRegistry`` / ``GatewayAdapter`` 协议底座：注册、覆盖、错误编码、参数解析。
"""
from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.common.work_mode import DEFAULT_WEB_WORK_MODE
from jiuwenswarm.server.runtime.gateway_adapter import (
    AdapterRegistry,
    ConfigAdapter,
    HarmonyOSAdapter,
    MemoryAdapter,
    ProjectAdapter,
    SessionAdapter,
    WorkspaceFileAdapter,
)
from jiuwenswarm.server.runtime.gateway_adapter.base import (
    build_error_response,
    parse_int_param,
)
from jiuwenswarm.server.runtime.harmonyos.harmonyos_project import (
    HarmonyOSProjectError,
)


def _request(
    method: ReqMethod,
    params: dict | None = None,
    *,
    channel_id: str = "web",
    user_id: str | None = None,
) -> AgentRequest:
    return AgentRequest(
        request_id="req-1",
        channel_id=channel_id,
        session_id="current-session",
        req_method=method,
        params=params or {},
        user_id=user_id,
    )


_TEAM_SESSION = {
    "session_id": "sess-team-1",
    "mode": "team",
    "team_name": "dev-team-swarm_sess-team-1",
    "agent_group_name": "technical-proposal-review",
    "title": "team task",
    "work_mode": "code",
    "delivery_context": {"channel_id": "internal"},
    "channel_id": "web",
    "channel_metadata": {"git_branch": "main"},
}


@pytest.mark.asyncio
async def test_workspace_upload_chunks_stay_in_injected_user_dir(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Large AgentOS uploads are routed as bounded E2A chunks, not Gateway files."""
    from jiuwenswarm.server.runtime.gateway_adapter import workspace_file_adapter as module

    monkeypatch.setattr(module, "get_user_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(module, "is_path_within_user_dirs", lambda path: Path(path).is_relative_to(tmp_path))
    adapter = WorkspaceFileAdapter()
    first = await adapter.handle(
        _request(
            ReqMethod.FILE_UPLOAD_CHUNK,
            {
                "upload_id": "u1",
                "target_rel_path": "agent/sessions/s1/uploads/report.bin",
                "data": base64.b64encode(b"first-").decode("ascii"),
                "final": False,
            },
            user_id="ignored-for-directory-selection",
        )
    )
    assert first.ok is True
    actual_path = first.payload["path"]
    second = await adapter.handle(
        _request(
            ReqMethod.FILE_UPLOAD_CHUNK,
            {
                "upload_id": "u1",
                "target_rel_path": "agent/sessions/s1/uploads/report.bin",
                "resolved_path": actual_path,
                "data": base64.b64encode(b"second").decode("ascii"),
                "final": True,
            },
        )
    )
    assert second.ok is True
    assert Path(actual_path).read_bytes() == b"first-second"


@pytest.fixture
def patched_sessions(monkeypatch: pytest.MonkeyPatch):
    """替换 SessionAdapter 依赖的 get_all_sessions_metadata。"""

    def _patch(sessions, total):
        monkeypatch.setattr(
            "jiuwenswarm.server.runtime.gateway_adapter.session_adapter.get_all_sessions_metadata",
            lambda limit=20, offset=0: (sessions, total),
        )
        return sessions, total

    return _patch


# ── SessionAdapter ───────────────────────────────────────────────────────────


class TestSessionAdapter:
    async def test_history_list_turns_is_handled_as_session_rpc(self, monkeypatch) -> None:
        from jiuwenswarm.agents.harness.common import session_ops_service

        monkeypatch.setattr(
            session_ops_service,
            "list_session_turns",
            lambda *, session_id: {"session_id": session_id, "turns": [{"index": 1}]},
        )

        response = await SessionAdapter().handle(
            _request(ReqMethod.HISTORY_LIST_TURNS, {"session_id": "session-1"}, channel_id="tui")
        )

        assert response.ok is True
        assert response.payload == {"session_id": "session-1", "turns": [{"index": 1}]}

    async def test_restore_files_is_handled_as_session_rpc(self, monkeypatch) -> None:
        from jiuwenswarm.agents.harness.common import session_ops_service

        captured: dict[str, object] = {}

        def _restore(*, session_id: str, turn_index: int) -> dict[str, object]:
            captured.update(session_id=session_id, turn_index=turn_index)
            return {"restored_files": ["report.txt"]}

        monkeypatch.setattr(session_ops_service, "restore_session_files", _restore)

        response = await SessionAdapter().handle(
            _request(
                ReqMethod.SESSION_RESTORE_FILES,
                {"session_id": "session-1", "turn_index": "2"},
                channel_id="tui",
            )
        )

        assert response.ok is True
        assert response.payload == {"restored_files": ["report.txt"]}
        assert captured == {"session_id": "session-1", "turn_index": 2}

    async def test_web_channel_projects_session_info(self, patched_sessions) -> None:
        patched_sessions([_TEAM_SESSION], 1)
        adapter = SessionAdapter()
        resp = await adapter.handle(_request(ReqMethod.SESSION_LIST, {"limit": 5, "offset": 1}))

        assert resp.ok is True
        payload = resp.payload
        assert payload["limit"] == 5
        assert payload["offset"] == 1
        assert payload["total"] == 1

        info = payload["sessions"][0]
        assert info["session_id"] == "sess-team-1"
        assert info["mode"] == "team"
        assert info["team_name"] == "dev-team-swarm_sess-team-1"
        assert info["agent_group_name"] == "technical-proposal-review"
        assert info["work_mode"] == "code"
        # 投影排除内部字段
        assert "delivery_context" not in info
        assert "channel_metadata" not in info

        # work_mode 缺失时兜底为 Web 默认模式（DEFAULT_WEB_WORK_MODE）
        session_no_wm = dict(_TEAM_SESSION)
        session_no_wm.pop("work_mode")
        patched_sessions([session_no_wm], 1)
        resp = await adapter.handle(_request(ReqMethod.SESSION_LIST))
        assert resp.ok is True
        assert resp.payload["sessions"][0]["work_mode"] == DEFAULT_WEB_WORK_MODE

    async def test_tui_channel_keeps_raw_metadata(self, patched_sessions) -> None:
        patched_sessions([_TEAM_SESSION], 1)
        resp = await SessionAdapter().handle(
            _request(ReqMethod.SESSION_LIST, channel_id="tui")
        )

        assert resp.ok is True
        # TUI 消费原始 metadata（channel_id / channel_metadata 等内部字段保留）
        raw = resp.payload["sessions"][0]
        assert raw["channel_id"] == "web"
        assert raw["channel_metadata"] == {"git_branch": "main"}

    async def test_failure_degrades_to_empty_list_tui_only(self, monkeypatch) -> None:
        """会话元数据读失败时按通道保持迁移前契约：

        - TUI：迁移前由 AgentServer handler 处理，降级为空列表；
        - Web：迁移前由 Gateway 本地 handler 处理，异常上抛返回 ok=False。
        """
        def _boom(limit=20, offset=0):
            raise RuntimeError("boom")

        monkeypatch.setattr(
            "jiuwenswarm.server.runtime.gateway_adapter.session_adapter.get_all_sessions_metadata",
            _boom,
        )
        resp = await SessionAdapter().handle(
            _request(
                ReqMethod.SESSION_LIST,
                channel_id="tui",
                params={"limit": 50, "offset": 100},
            )
        )
        assert resp.ok is True
        assert resp.payload == {
            "sessions": [], "total": 0, "limit": 50, "offset": 100,
        }

        resp = await SessionAdapter().handle(_request(ReqMethod.SESSION_LIST))
        assert resp.ok is False
        assert resp.payload["code"] == "INTERNAL_ERROR"
        assert "boom" in resp.payload["error"]

    # -- Phase 2 扩展：metadata/pin/color/preview --

    async def test_get_metadata(self, monkeypatch) -> None:
        """session.get_metadata：OK 返回 metadata；不存在返回 NOT_FOUND（与 Web fallback 语义一致）。"""
        monkeypatch.setattr(
            "jiuwenswarm.server.runtime.gateway_adapter.session_adapter.get_session_metadata",
            lambda sid, cache_bust=False: {"session_id": sid, "mode": "agent", "model": "m1"},
        )
        resp = await SessionAdapter().handle(
            _request(ReqMethod.SESSION_GET_METADATA, {"session_id": "sess-1"})
        )
        assert resp.ok is True
        assert resp.payload["session_id"] == "sess-1"
        assert resp.payload["model"] == "m1"

        monkeypatch.setattr(
            "jiuwenswarm.server.runtime.gateway_adapter.session_adapter.get_session_metadata",
            lambda sid, cache_bust=False: {},
        )
        resp = await SessionAdapter().handle(
            _request(ReqMethod.SESSION_GET_METADATA, {"session_id": "missing"})
        )
        assert resp.ok is False
        assert resp.payload["code"] == "NOT_FOUND"

    async def test_pin(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "jiuwenswarm.server.runtime.gateway_adapter.session_adapter.set_session_pinned",
            lambda sid, pinned: (True, 3),
        )
        resp = await SessionAdapter().handle(
            _request(ReqMethod.SESSION_PIN, {"session_id": "sess-1", "pinned": True})
        )
        assert resp.ok is True
        assert resp.payload == {"pinned": True, "pin_order": 3}

    async def test_color_set(self, monkeypatch) -> None:
        """查询模式返回当前色值；非法色值拒绝（白名单与 TUI 一致）。"""
        monkeypatch.setattr(
            "jiuwenswarm.server.runtime.gateway_adapter.session_adapter.get_session_metadata",
            lambda sid, cache_bust=False: {"accent_color": "blue"},
        )
        resp = await SessionAdapter().handle(
            _request(ReqMethod.SESSION_COLOR_SET, {"session_id": "sess-1"})
        )
        assert resp.ok is True
        assert resp.payload["accent_color"] == "blue"

        resp = await SessionAdapter().handle(
            _request(ReqMethod.SESSION_COLOR_SET, {"session_id": "sess-1", "color": "neon"})
        )
        assert resp.ok is False
        assert resp.payload["code"] == "BAD_REQUEST"
        assert "invalid color" in resp.payload["error"]

    async def test_preview_whitelist(self, monkeypatch) -> None:
        history = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "event_type": "chat.final", "content": "hi there"},
            {"role": "assistant", "event_type": "chat.delta", "content": "partial"},
            {"role": "assistant", "event_type": "chat.reasoning", "content": "think"},
            {"role": "member", "event_type": "team.message", "content": "team msg"},
        ]
        monkeypatch.setattr(
            "jiuwenswarm.server.runtime.gateway_adapter.session_adapter.load_history_records",
            lambda sid: history,
        )
        resp = await SessionAdapter().handle(
            _request(ReqMethod.SESSION_PREVIEW, {"session_id": "sess-1", "count": 10})
        )
        assert resp.ok is True
        contents = [m["content"] for m in resp.payload["preview_messages"]]
        assert "hello" in contents
        assert "hi there" in contents
        assert "team msg" in contents
        assert "partial" not in contents
        assert "think" not in contents

    async def test_session_delete_team_returns_agent_unavailable(self, monkeypatch) -> None:
        """SessionAdapter.session.delete：team 会话的本地删除不可用（AGENT_UNAVAILABLE）。

        与原 Web/TUI handler fallback 语义一致：team 会话需由 AgentServer 处理，
        本地共享目录 fallback 拒绝并返回 AGENT_UNAVAILABLE，不触发 evict，也不删目录。
        """
        evict_calls: list[dict] = []
        monkeypatch.setattr(
            "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
            lambda sid, cache_bust=False: {"mode": "team"},
        )
        monkeypatch.setattr(
            "openjiuwen.core.session.agent.create_agent_session",
            lambda **kwargs: evict_calls.append(kwargs),
        )
        resp = await SessionAdapter().handle(
            _request(ReqMethod.SESSION_DELETE, {"session_id": "sess-team-1"})
        )
        assert resp.ok is False
        assert resp.payload["code"] == "AGENT_UNAVAILABLE"
        assert evict_calls == []

    async def test_session_delete_missing_returns_not_found_without_evict(
        self, monkeypatch, tmp_path,
    ) -> None:
        """SessionAdapter.session.delete：目标会话目录不存在时返回 NOT_FOUND 且不 evict。"""
        evict_calls: list[dict] = []
        missing_dir = tmp_path / "sessions" / "missing"
        monkeypatch.setattr(
            "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
            lambda sid, cache_bust=False: {"mode": "agent"},
        )
        monkeypatch.setattr(
            "jiuwenswarm.server.runtime.session.session_history.resolve_session_dir",
            lambda sid, sessions_root=None: (missing_dir, None),
        )
        monkeypatch.setattr(
            "openjiuwen.core.session.agent.create_agent_session",
            lambda **kwargs: evict_calls.append(kwargs),
        )
        resp = await SessionAdapter().handle(
            _request(ReqMethod.SESSION_DELETE, {"session_id": "missing"})
        )
        assert resp.ok is False
        assert resp.payload["code"] == "NOT_FOUND"
        assert evict_calls == []

    async def test_session_delete_offline_fallback_evicts_and_removes_dir(
        self, monkeypatch, tmp_path,
    ) -> None:
        """SessionAdapter.session.delete：普通会话的本地 fallback 触发 root evict 并删除目录。"""
        evict_calls: list[dict] = []
        session_root = tmp_path / "sessions" / "sess-del"
        session_root.mkdir(parents=True)
        monkeypatch.setattr(
            "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
            lambda sid, cache_bust=False: {"mode": "agent"},
        )
        monkeypatch.setattr(
            "jiuwenswarm.server.runtime.session.session_history.resolve_session_dir",
            lambda sid, sessions_root=None: (session_root, None),
        )

        class _Session:
            async def release_kvc(self):
                evict_calls.append(
                    {
                        "session_id": "sess-del",
                        "parent_session_id": "sess-del",
                    }
                )
                return True

        monkeypatch.setattr(
            "openjiuwen.core.session.agent.create_agent_session",
            lambda **_kwargs: _Session(),
        )
        resp = await SessionAdapter().handle(
            _request(ReqMethod.SESSION_DELETE, {"session_id": "sess-del"})
        )
        assert resp.ok is True
        assert resp.payload == {"session_id": "sess-del"}
        assert evict_calls == [
            {"session_id": "sess-del", "parent_session_id": "sess-del"}
        ]
        assert not session_root.exists()



# ── WorkspaceFileAdapter ─────────────────────────────────────────────────────


class TestWorkspaceFileAdapter:
    def _adapter(self) -> WorkspaceFileAdapter:
        return WorkspaceFileAdapter()

    async def test_document_formats(self) -> None:
        resp = await self._adapter().handle(_request(ReqMethod.DOCUMENT_FORMATS, {}))
        assert resp.ok is True
        assert ".exe" in resp.payload["forbidden_formats"]

    async def test_media_persist_payload_subset(self, monkeypatch) -> None:
        def _fake_normalize(normalized, session_id):
            normalized["media_items"] = [{"type": "image", "path": "/tmp/a.png"}]
            normalized["content"] = "x"

        monkeypatch.setattr(
            "jiuwenswarm.server.runtime.gateway_adapter.workspace_file_adapter.normalize_chat_media_attachments",
            _fake_normalize,
        )
        resp = await self._adapter().handle(
            _request(ReqMethod.MEDIA_PERSIST, {"content": "x", "media_items": []})
        )
        assert resp.ok is True
        assert resp.payload["media_items"][0]["path"] == "/tmp/a.png"
        assert resp.payload["content"] == "x"

    async def test_document_persist(self, monkeypatch) -> None:
        def _fake_persist(normalized):
            normalized["documents"] = [{"path": "/tmp/d.md"}]
            normalized["forbidden_formats"] = [".exe"]

        monkeypatch.setattr(
            "jiuwenswarm.server.runtime.gateway_adapter.workspace_file_adapter.persist_and_parse_documents",
            _fake_persist,
        )
        resp = await self._adapter().handle(
            _request(ReqMethod.DOCUMENT_PERSIST, {"documents": []})
        )
        assert resp.ok is True
        assert resp.payload["documents"][0]["path"] == "/tmp/d.md"

    async def test_select_directory(self, monkeypatch, tmp_path) -> None:
        """D3 边界：initial_dir 越界拒绝；无 initial_dir 返回 UNSUPPORTED 引导手动输入。

        AgentOS/远程容器内无法弹系统目录选择器，空 initial_dir 不再把 workspace
        根当作"已选中"返回（避免前端默认选中 jiuwen workspace 直接建项目），
        而是返回 UNSUPPORTED 让前端回退到手动输入表单。
        """
        monkeypatch.setattr(
            "jiuwenswarm.server.runtime.gateway_adapter.workspace_file_adapter.get_agent_workspace_dir",
            lambda: tmp_path,
        )
        outside = tmp_path.parent / "outside"
        resp = await self._adapter().handle(
            _request(ReqMethod.PATH_SELECT_DIRECTORY, {"initial_dir": str(outside)})
        )
        assert resp.ok is False
        assert resp.payload["code"] == "BAD_REQUEST"

        resp = await self._adapter().handle(_request(ReqMethod.PATH_SELECT_DIRECTORY, {}))
        assert resp.ok is False
        assert resp.payload["code"] == "UNSUPPORTED"

        (tmp_path / "sub").mkdir(parents=True)
        resp = await self._adapter().handle(
            _request(ReqMethod.PATH_SELECT_DIRECTORY, {"initial_dir": "sub"})
        )
        assert resp.ok is True
        assert resp.payload["cancelled"] is False
        assert str(tmp_path / "sub") in resp.payload["path"]

    async def test_select_files(self, monkeypatch, tmp_path) -> None:
        """文件枚举：隐藏文件排除；图片 base64 保持在 E2A 安全帧大小内。"""
        import base64 as _b64

        (tmp_path / "a.txt").write_text("a", encoding="utf-8")
        (tmp_path / ".hidden.txt").write_text("h", encoding="utf-8")
        (tmp_path / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\nfake-png")
        (tmp_path / "big.png").write_bytes(b"x" * (4 * 1024 * 1024 + 1))
        (tmp_path / "doc.md").write_text("# t", encoding="utf-8")
        monkeypatch.setattr(
            "jiuwenswarm.server.runtime.gateway_adapter.workspace_file_adapter.get_agent_workspace_dir",
            lambda: tmp_path,
        )
        resp = await self._adapter().handle(_request(ReqMethod.PATH_SELECT_FILES, {}))
        assert resp.ok is True
        by_name = {f["filename"]: f for f in resp.payload["files"]}
        assert "a.txt" in by_name
        assert ".hidden.txt" not in by_name
        assert by_name["pic.png"]["kind"] == "image"
        assert by_name["pic.png"]["base64"] == _b64.b64encode(
            b"\x89PNG\r\n\x1a\nfake-png"
        ).decode("ascii")
        assert "base64" not in by_name["big.png"]  # 超过 4MB 不返回 base64
        assert by_name["doc.md"]["kind"] == "document"
        assert "base64" not in by_name["doc.md"]

    async def test_import_url_ok(self, monkeypatch, tmp_path) -> None:
        imported = {
            "url": "http://x/y.txt",
            "path": str(tmp_path / "y.txt"),
            "filename": "y.txt",
            "size": 3,
        }
        monkeypatch.setattr(
            "jiuwenswarm.server.runtime.gateway_adapter.workspace_file_adapter._import_url_file",
            lambda item: {**item, **imported},
        )
        resp = await self._adapter().handle(
            _request(ReqMethod.FILE_IMPORT_URL, {"files": [{"url": "http://x/y.txt", "name": "y.txt"}]})
        )
        assert resp.ok is True
        assert resp.payload["files"][0]["path"] == str(tmp_path / "y.txt")

    async def test_im_file_persist(self, monkeypatch, tmp_path) -> None:
        """IM 附件落盘注入目录；非法 base64 拒绝（决策 D6）。"""
        import base64 as _b64

        monkeypatch.setattr(
            "jiuwenswarm.server.runtime.gateway_adapter.workspace_file_adapter.get_agent_workspace_dir",
            lambda: tmp_path,
        )
        data = b"\x89PNG\r\n\x1a\nfake"
        resp = await self._adapter().handle(
            _request(
                ReqMethod.IM_FILE_PERSIST,
                {
                    "platform": "feishu",
                    "category": "images",
                    "filename": "img.png",
                    "data": _b64.b64encode(data).decode("ascii"),
                },
            )
        )
        assert resp.ok is True
        assert resp.payload["file_category"] == "images"
        target = tmp_path / "feishu_files" / "downloads" / "images" / "img.png"
        assert target.read_bytes() == data

        resp = await self._adapter().handle(
            _request(
                ReqMethod.IM_FILE_PERSIST,
                {"platform": "feishu", "category": "files", "filename": "a.txt", "data": "!!!"},
            )
        )
        assert resp.ok is False
        assert resp.payload["code"] == "BAD_REQUEST"

    async def test_media_persist_passes_through_http_persisted_item(
        self, tmp_path
    ) -> None:
        """大图已由 Gateway 经 HTTP bridge 落盘（``_persisted``）：适配器侧直接
        透传落盘记录，不重复解码/写盘（Phase 2 传输取舍）。"""
        from jiuwenswarm.server.runtime.attachments.media_attachments import (
            normalize_chat_media_attachments,
        )

        uploaded = tmp_path / "uploads"
        uploaded.mkdir()
        existing = uploaded / "big.png"
        existing.write_bytes(b"\x89PNG\r\n\x1a\n" + b"data")
        params = {
            "content": "x",
            "media_items": [
                {
                    "type": "image",
                    "mimeType": "image/png",
                    "filename": "big.png",
                    "path": str(existing),
                    "size_bytes": 9,
                    "_persisted": True,
                }
            ],
        }
        normalize_chat_media_attachments(params, session_id="sess-1")
        items = params.get("media_items")
        assert isinstance(items, list) and len(items) == 1
        assert items[0]["path"] == str(existing)
        assert items[0]["size_bytes"] == 12
        assert items[0]["filename"] == "big.png"
        assert "base64Data" not in items[0]

    async def test_media_persist_drops_missing_persisted_item(self, tmp_path) -> None:
        """``_persisted`` 项指向的文件不存在（跨进程路径失效）时视为无效项丢弃。"""
        from jiuwenswarm.server.runtime.attachments.media_attachments import (
            normalize_chat_media_attachments,
        )

        params = {
            "media_items": [
                {
                    "type": "image",
                    "mimeType": "image/png",
                    "path": str(tmp_path / "missing.png"),
                    "_persisted": True,
                }
            ]
        }
        normalize_chat_media_attachments(params, session_id="sess-1")
        assert "media_items" not in params


# ── MemoryAdapter ────────────────────────────────────────────────────────────


class TestMemoryAdapter:
    async def test_list_uses_agentserver_workspace(self, monkeypatch, tmp_path) -> None:
        captured: dict[str, object] = {}

        async def fake_list(workspace, mode, params):
            captured.update(workspace=workspace, mode=mode, params=params)
            return {"files": [], "mode": mode}

        monkeypatch.setattr(
            "jiuwenswarm.server.runtime.gateway_adapter.memory_adapter.get_agent_workspace_dir",
            lambda: tmp_path,
        )
        monkeypatch.setattr(
            "jiuwenswarm.server.runtime.gateway_adapter.memory_adapter.handle_memory_list",
            fake_list,
        )

        response = await MemoryAdapter().handle(
            _request(ReqMethod.MEMORY_LIST, {"mode": "code"}, channel_id="tui", user_id="authenticated-user")
        )

        assert response.ok is True
        assert captured["workspace"] == str(tmp_path)
        assert captured["mode"] == "code"
        # 路由身份不供给用户态实现，避免其据此选择目录。
        assert "user_id" not in captured["params"]

    async def test_edit_injects_project_dir_from_trusted_dirs(self, monkeypatch) -> None:
        """TUI 前端只传 trusted_dirs/cwd，adapter 必须补全 project_dir 才不误拒项目记忆。

        回归：Phase 3 迁移前 tui_connect 会从 trusted_dirs[0]/cwd 解析 project_dir
        注入 params；MemoryAdapter 若不补全，code 模式项目目录内记忆文件会被
        memory_rpc._validate_edit_path 以 project_dir=None 误判为越界。
        """
        captured: dict[str, object] = {}

        async def fake_edit(workspace, params):
            captured.update(workspace=workspace, params=params)
            return {"path": "p", "editable": True}

        monkeypatch.setattr(
            "jiuwenswarm.server.runtime.gateway_adapter.memory_adapter.handle_memory_edit",
            fake_edit,
        )
        response = await MemoryAdapter().handle(
            _request(
                ReqMethod.MEMORY_EDIT,
                {
                    "path": "/home/user/projects/foo/MEMORY.md",
                    "trusted_dirs": ["/home/user/projects/foo"],
                    "cwd": "/home/user/projects/foo",
                },
                channel_id="tui",
            )
        )

        assert response.ok is True
        assert captured["params"]["project_dir"] == "/home/user/projects/foo"

    async def test_toggle_keeps_legacy_no_project_dir_injection(self, monkeypatch) -> None:
        """memory.toggle 迁移前不注入 project_dir，保持原行为（注入范围只覆盖 4 个入口）。"""
        captured: dict[str, object] = {}

        async def fake_toggle(workspace, mode, params):
            captured.update(workspace=workspace, params=params)
            return {"enabled": True}

        monkeypatch.setattr(
            "jiuwenswarm.server.runtime.gateway_adapter.memory_adapter.handle_memory_toggle",
            fake_toggle,
        )
        response = await MemoryAdapter().handle(
            _request(
                ReqMethod.MEMORY_TOGGLE,
                {"trusted_dirs": ["/home/user/projects/foo"]},
                channel_id="tui",
            )
        )

        assert response.ok is True
        assert "project_dir" not in captured["params"]


# ── ProjectAdapter ───────────────────────────────────────────────────────────


class TestProjectAdapterGit:
    @staticmethod
    def _status() -> SimpleNamespace:
        return SimpleNamespace(
            is_git=True,
            repo_root="/agent-data/workspace/repo",
            branch="main",
            head="abcdef",
            detached=False,
            transient=False,
            upstream="origin/main",
            is_dirty=False,
            staged=0,
            unstaged=0,
            untracked=0,
            conflicted=0,
            local_branches=["main", "feature"],
            remote_branches=["origin/main"],
            error=None,
        )

    @staticmethod
    def _project() -> SimpleNamespace:
        return SimpleNamespace(
            project_id="proj_1",
            name="repo",
            project_dir="/agent-data/workspace/repo",
            work_mode="code",
        )

    async def test_switch_branch_runs_in_agentserver_service(self, monkeypatch) -> None:
        project = self._project()
        captured: dict[str, object] = {}

        class Service:
            def switch_branch(self, received_project, branch, *, require_clean):
                captured.update(project=received_project, branch=branch, require_clean=require_clean)
                return SimpleNamespace(
                    success=True,
                    previous_branch="main",
                    repo_status=TestProjectAdapterGit._status(),
                )

        monkeypatch.setattr(
            "jiuwenswarm.server.runtime.session.project_git.resolve_git_project",
            lambda project_id, *, cache_bust: (project, None, None),
        )
        monkeypatch.setattr(
            "jiuwenswarm.server.runtime.session.project_git.get_project_git_service",
            lambda: Service(),
        )

        response = await ProjectAdapter().handle(
            _request(
                ReqMethod.PROJECT_GIT_SWITCH_BRANCH,
                {"project_id": "proj_1", "branch": "feature", "require_clean": True},
            )
        )

        assert response.ok is True
        assert captured == {"project": project, "branch": "feature", "require_clean": True}
        assert response.payload["current_branch"] == "main"
        assert response.payload["status"]["project_dir"] == project.project_dir

    async def test_create_branch_preserves_structured_git_error(self, monkeypatch) -> None:
        project = self._project()
        git_error = SimpleNamespace(
            code="BRANCH_EXISTS",
            message="branch already exists",
            to_dict=lambda: {"code": "BRANCH_EXISTS", "message": "branch already exists"},
        )

        class Service:
            def create_branch(self, *_args, **_kwargs):
                return SimpleNamespace(success=False, error=git_error)

        monkeypatch.setattr(
            "jiuwenswarm.server.runtime.session.project_git.resolve_git_project",
            lambda project_id, *, cache_bust: (project, None, None),
        )
        monkeypatch.setattr(
            "jiuwenswarm.server.runtime.session.project_git.get_project_git_service",
            lambda: Service(),
        )

        response = await ProjectAdapter().handle(
            _request(ReqMethod.PROJECT_GIT_CREATE_BRANCH, {"project_id": "proj_1", "branch": "feature"})
        )

        assert response.ok is False
        assert response.payload == {
            "error": "branch already exists",
            "code": "BRANCH_EXISTS",
            "detail": {"code": "BRANCH_EXISTS", "message": "branch already exists"},
        }


class TestProjectAdapterSessions:
    async def test_historical_single_user_sessions_ignore_request_user_id(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Directory isolation is authoritative; old metadata owners are not."""
        module = "jiuwenswarm.server.runtime.gateway_adapter.project_adapter"
        monkeypatch.setattr(f"{module}.project_store.list_projects", lambda **_kwargs: [])
        monkeypatch.setattr(
            f"{module}.collect_all_sessions_metadata",
            lambda: [{
                "session_id": "legacy-web-session",
                "channel_id": "web",
                "user_id": "legacy-owner",
                "project_id": "",
                "work_mode": "work",
                "last_user_message_at": 1,
                "last_message_at": 1,
            }],
        )

        response = await ProjectAdapter().handle(
            _request(
                ReqMethod.PROJECT_GET_SESSIONS,
                {"project_id": "default"},
                user_id="current-connection-owner",
            )
        )

        assert response.ok is True
        assert response.payload["total"] == 1
        assert response.payload["sessions"][0]["session_id"] == "legacy-web-session"


# ── HarmonyOSAdapter ─────────────────────────────────────────────────────────


class TestHarmonyOSAdapter:
    async def test_project_init_delegates_to_agentserver(self, monkeypatch) -> None:
        captured: dict[str, object] = {}

        async def fake_project_init(params):
            captured["params"] = params
            return {"ok": True, "context": {"kind": "harmonyos-project"}}

        monkeypatch.setattr(
            "jiuwenswarm.server.runtime.gateway_adapter.harmonyos_adapter.run_harmonyos_project_init",
            fake_project_init,
        )

        response = await HarmonyOSAdapter().handle(
            _request(ReqMethod.HARMONYOS_PROJECT_INIT, {"path": "/tmp/proj"}, channel_id="tui")
        )

        assert response.ok is True
        assert captured["params"] == {"path": "/tmp/proj"}
        assert response.payload["ok"] is True

    async def test_project_init_error_maps_to_bad_request(self, monkeypatch) -> None:
        async def raising_project_init(params):
            raise HarmonyOSProjectError("not a HarmonyOS project")

        monkeypatch.setattr(
            "jiuwenswarm.server.runtime.gateway_adapter.harmonyos_adapter.run_harmonyos_project_init",
            raising_project_init,
        )

        response = await HarmonyOSAdapter().handle(
            _request(ReqMethod.HARMONYOS_PROJECT_INIT, {"path": "/tmp/plain"})
        )

        assert response.ok is False
        assert response.payload["code"] == "BAD_REQUEST"
        assert response.payload["error"] == "not a HarmonyOS project"


# ── 协议底座：AdapterRegistry / build_error_response / parse_int_param ───────


class TestAdapterRegistry:
    def test_register_get_contains(self) -> None:
        registry = AdapterRegistry()
        assert not registry.contains(ReqMethod.SESSION_LIST.value)

        registry.register(SessionAdapter())

        assert registry.contains(ReqMethod.SESSION_LIST.value)
        assert isinstance(registry.get(ReqMethod.SESSION_LIST.value), SessionAdapter)
        # SESSION_DELETE / SESSION_RENAME 注册在 SessionAdapter：供 e2a_proxy
        # 单用户离线 fallback 使用（在线 dispatch 由 AgentWebSocketServer 显式
        # 跳过适配器、走既有 handler）。
        assert isinstance(registry.get(ReqMethod.SESSION_DELETE.value), SessionAdapter)
        assert isinstance(registry.get(ReqMethod.SESSION_RENAME.value), SessionAdapter)
        assert registry.get("session.unknown") is None
        assert registry.get(None) is None
        assert ReqMethod.SESSION_LIST.value in registry.methods()

    def test_register_replaces_for_same_method(self) -> None:
        registry = AdapterRegistry()
        registry.register(SessionAdapter())

        class _Other(SessionAdapter):
            pass

        registry.register(_Other())
        assert isinstance(registry.get(ReqMethod.SESSION_LIST.value), _Other)

    def test_each_adapter_registers_all_its_methods(self) -> None:
        """每个适配器声明的方法都能被注册表检索（methods 声明与分发一致）。"""
        adapters = [
            SessionAdapter(),
            WorkspaceFileAdapter(),
            MemoryAdapter(),
            ProjectAdapter(),
            HarmonyOSAdapter(),
            ConfigAdapter(),
        ]
        registry = AdapterRegistry()
        for adapter in adapters:
            registry.register(adapter)
        for adapter in adapters:
            for method in adapter.methods:
                assert registry.contains(method)


@pytest.mark.asyncio
async def test_config_adapter_executes_panel_get_in_agentserver_context() -> None:
    response = await ConfigAdapter().handle(_request(ReqMethod.CONFIG_GET))

    assert response.ok is True
    assert "app_version" in response.payload


@pytest.mark.asyncio
async def test_config_adapter_command_model_forces_local_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The AgentServer-side command must not proxy to Gateway a second time."""
    from jiuwenswarm.gateway.channel_manager.tui import tui_connect

    def fake_register(bind) -> None:
        assert bind.force_local_config is True

        async def handler(ws, req_id, params, session_id):
            _ = (params, session_id)
            await bind.channel.send_response(
                ws, req_id, ok=True, payload={"type": "switched", "current": "m1"}
            )

        bind.channel.register_local_handler("/tui", ReqMethod.COMMAND_MODEL.value, handler)

    monkeypatch.setattr(tui_connect, "register_cli_handlers", fake_register)

    response = await ConfigAdapter().handle(
        _request(ReqMethod.COMMAND_MODEL, {"model": "m1"}, channel_id="tui")
    )
    assert response.ok is True
    assert response.payload["current"] == "m1"
    assert response.metadata["config_changed"] is True


@pytest.mark.asyncio
async def test_config_adapter_keeps_browser_config_in_agentserver_directory(monkeypatch) -> None:
    """path.* must use the current AgentServer config, never Gateway config."""
    from jiuwenswarm.common import config as config_module

    current = {"browser": {"chrome_path": "/agent/chrome", "browser_type": "msedge", "headless": False}}
    updates: list[dict] = []
    monkeypatch.setattr(config_module, "get_config", lambda: current)
    monkeypatch.setattr(config_module, "update_browser_in_config", lambda payload: updates.append(payload))
    adapter = ConfigAdapter()

    got = await adapter.handle(_request(ReqMethod.PATH_GET))
    changed = await adapter.handle(
        _request(ReqMethod.PATH_SET, {"chrome_path": "/agent/new-chrome", "browser_type": "chrome", "headless": True})
    )

    assert got.payload == {"chrome_path": "/agent/chrome", "browser_type": "msedge", "headless": False}
    assert updates == [{"chrome_path": "/agent/new-chrome", "browser_type": "chrome", "headless": True}]
    assert changed.metadata["config_changed"] is True
    assert changed.metadata["browser_runtime_restart"] is True


@pytest.mark.asyncio
async def test_config_adapter_resolves_platform_browser_path_for_runtime_restart(monkeypatch) -> None:
    """PATH_GET must return the concrete binary used to identify an active runtime."""
    from jiuwenswarm.common import config as config_module

    current = {
        "browser": {
            "chrome_path": {"default": "  /agent/chrome  "},
            "browser_type": "chrome",
            "headless": True,
        }
    }
    monkeypatch.setattr(config_module, "get_config", lambda: current)

    response = await ConfigAdapter().handle(_request(ReqMethod.PATH_GET))

    assert response.payload == {
        "chrome_path": "/agent/chrome",
        "browser_type": "chrome",
        "headless": True,
    }


@pytest.mark.asyncio
async def test_config_adapter_resolves_env_browser_path_for_runtime_restart(monkeypatch) -> None:
    """PATH_GET must match the env-expanded binary used by the runtime."""
    from jiuwenswarm.common import config as config_module

    monkeypatch.setenv("JIUWEN_TEST_CHROME", "/agent/chrome")
    monkeypatch.setattr(
        config_module,
        "get_config",
        lambda: {"browser": {"chrome_path": "${JIUWEN_TEST_CHROME}"}},
    )

    response = await ConfigAdapter().handle(_request(ReqMethod.PATH_GET))

    assert response.payload["chrome_path"] == "/agent/chrome"


class TestBuildErrorResponse:
    def test_build_error_response(self) -> None:
        request = _request(ReqMethod.SESSION_LIST)
        resp = build_error_response(request, "oops", code="BAD_REQUEST")
        assert resp.ok is False
        assert resp.payload == {"error": "oops", "code": "BAD_REQUEST"}
        assert resp.request_id == "req-1"


class TestParseIntParam:
    def test_parse_int_param(self) -> None:
        assert parse_int_param({"limit": 5}, "limit", 20, minimum=1, maximum=200) == 5
        assert parse_int_param({"limit": 5.0}, "limit", 20, minimum=1, maximum=200) == 5
        assert parse_int_param({"limit": "7"}, "limit", 20, minimum=1, maximum=200) == 7
        assert parse_int_param({"limit": 999}, "limit", 20, minimum=1, maximum=200) == 200
        assert parse_int_param({"limit": 0}, "limit", 20, minimum=1, maximum=200) == 1
        assert parse_int_param({"limit": "abc"}, "limit", 20, minimum=1, maximum=200) == 20
        assert parse_int_param({"limit": True}, "limit", 20, minimum=1, maximum=200) == 20
        assert parse_int_param({}, "limit", 20, minimum=1, maximum=200) == 20
