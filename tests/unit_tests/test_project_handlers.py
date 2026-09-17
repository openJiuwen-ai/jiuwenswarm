# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""项目接口 handler 单元测试 — project.list / get_sessions / create /
remove / restore / pinned_sessions + session.pin + 兼容性(session.create / rename)。

复用 test_session_metadata.py 的 _FakeWebChannel 桩模式,自包含 fixtures。
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
class _FakeWebChannel:
    channel_id = "web"

    def __init__(self):
        self.methods: dict[str, object] = {}
        self.responses: list[dict] = []

    def register_method(self, name, handler):
        self.methods[name] = handler

    def on_connect(self, handler):
        pass

    async def send_response(self, ws, req_id, *, ok, payload=None, error=None, code=None):
        self.responses.append(
            {"id": req_id, "ok": ok, "payload": payload, "error": error, "code": code}
        )


class _FakeSessionCreateAgentClient:
    """AgentServer boundary fake backed by the production binding helpers."""

    def __init__(self) -> None:
        self.requests: list[object] = []
        self._sequence = 0

    async def send_request(self, request):
        self.requests.append(request)
        params = dict(request.params or {})

        from jiuwenswarm.common.work_mode import DEFAULT_WEB_WORK_MODE, is_default_project_id
        from jiuwenswarm.server.runtime.session import project_store
        from jiuwenswarm.server.runtime.session.session_metadata import init_session_metadata
        from jiuwenswarm.server.runtime.session.work_mode import resolve_session_work_mode_params

        binding = resolve_session_work_mode_params(params, channel_id="web")
        if binding.error:
            return SimpleNamespace(
                ok=False,
                payload={"error": binding.error, "code": binding.code},
            )
        project_id, project_dir, error, code = project_store.resolve_session_project_binding(
            binding.project_id, binding.project_dir
        )
        if error:
            return SimpleNamespace(ok=False, payload={"error": error, "code": code})

        work_mode = binding.work_mode
        if not is_default_project_id(project_id):
            project = project_store.get_project_by_id(project_id, cache_bust=True)
            if project is None:
                return SimpleNamespace(
                    ok=False,
                    payload={
                        "error": f"project not found: {project_id}",
                        "code": "NOT_FOUND",
                    },
                )
            work_mode = project.work_mode or DEFAULT_WEB_WORK_MODE

        self._sequence += 1
        session_id = f"web_test_{self._sequence:03d}"
        init_session_metadata(
            session_id=session_id,
            channel_id="web",
            user_id=str(params.get("user_id") or ""),
            title=str(params.get("title") or ""),
            mode=str(params.get("mode") or "agent"),
            project_dir=project_dir,
            project_id=project_id,
            work_mode=work_mode,
        )
        return SimpleNamespace(
            ok=True,
            payload={
                "sessionId": session_id,
                "session_id": session_id,
                "projectId": project_id,
                "projectDir": project_dir,
                "workMode": work_mode,
                "prewarm_hit": True,
                "prewarm_status": "ready",
            },
        )


@pytest.fixture()
def sessions_dir(tmp_path, monkeypatch):
    d = tmp_path / "sessions"
    d.mkdir()
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_agent_sessions_dir",
        lambda: d,
    )
    # _session_create 等 handler 直接引用 app_web_handlers 模块内导入的副本,需一并 patch
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.get_agent_sessions_dir",
        lambda: d,
    )
    from jiuwenswarm.server.runtime.session.session_metadata import _METADATA_CACHE
    _METADATA_CACHE.clear()
    return d


@pytest.fixture()
def project_store_dir(tmp_path, monkeypatch):
    root = tmp_path / "agent"
    root.mkdir()
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.project_store.get_agent_root_dir",
        lambda: root,
    )
    from jiuwenswarm.server.runtime.session import project_store
    project_store.invalidate_cache()
    return root


@pytest.fixture()
def registered_channel(sessions_dir, project_store_dir):
    from jiuwenswarm.gateway.channel_manager.web.app_web_handlers import (
        WebHandlersBindParams,
        _register_web_handlers,
    )
    channel = _FakeWebChannel()
    channel.agent_client = _FakeSessionCreateAgentClient()
    _register_web_handlers(
        WebHandlersBindParams(channel=channel, agent_client=channel.agent_client)
    )
    return channel


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def _call(channel, method, params, sid="sess-caller"):
    handler = channel.methods[method]
    await handler(object(), "req-1", params, sid)
    return channel.responses[-1]


def _drain():
    from jiuwenswarm.server.runtime.session.session_metadata import _METADATA_QUEUE
    _METADATA_QUEUE.join()


def _make_session(sid, *, project_dir="", project_id="", pinned=False, pin_order=0, last_user_message_at=None, model="", cron_id="", channel_id="web"):
    """创建一个会话并写入指定元数据,flush 队列确保落盘。"""
    from jiuwenswarm.server.runtime.session.session_metadata import (
        init_session_metadata, update_session_metadata,
    )
    init_session_metadata(session_id=sid, project_dir=project_dir, project_id=project_id, model=model, cron_id=cron_id, channel_id=channel_id)
    if pinned or pin_order:
        update_session_metadata(session_id=sid, pinned=pinned, pin_order=pin_order)
    if last_user_message_at is not None:
        update_session_metadata(session_id=sid, last_user_message_at=last_user_message_at)
    _drain()


def _make_project(name, project_dir, *, pinned=False, pin_order=0, hidden=False):
    from jiuwenswarm.server.runtime.session.project_store import (
        create_project, save_project,
    )
    proj = create_project(name, project_dir)
    if pinned or pin_order:
        proj.pinned = pinned
        proj.pin_order = pin_order
    if hidden:
        proj.hidden = True
    save_project(proj)
    return proj


def _abspath(tmp_path, name):
    """平台无关的已存在目录绝对路径,用于 project.create 的 isabs / isdir 校验。"""
    path = tmp_path / name
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


# ===========================================================================
# project.list
# ===========================================================================
class TestProjectList:
    @staticmethod
    @pytest.mark.asyncio
    async def test_filter_all_sorting_default_last(registered_channel, tmp_path):
        """all: 置顶在前 → 非置顶按 last_user_message_at 倒序 → 默认末位。"""
        pa = _abspath(tmp_path, "app")
        pb = _abspath(tmp_path, "backend")
        p_pinned = _make_project("置顶项目", pa, pinned=True, pin_order=1)
        p_normal = _make_project("普通项目", pb)
        # 普通项目下 1 个会话;默认项目下 1 个会话
        _make_session("s1", project_id=p_normal.project_id, project_dir=pb, last_user_message_at=1000.0)
        _make_session("s2", project_dir="", last_user_message_at=2000.0)

        resp = await _call(registered_channel, "project.list", {"filter": "all"})
        assert resp["ok"] is True
        projects = resp["payload"]["projects"]
        ids = [p["project_id"] for p in projects]

        assert ids[0] == p_pinned.project_id  # 置顶在前
        assert ids[1] == p_normal.project_id  # 非置顶
        # work_mode 改造后默认项目按 work 拆分为两个虚拟条目:
        # work 模式 default 在前, code 模式 default_code 在后(均位于列表末尾)
        assert ids[-2] == "default"  # work 默认项目
        assert ids[-1] == "default_code"  # code 默认项目
        # 统计:普通项目 1 个非置顶会话,默认 1 个
        normal_info = next(p for p in projects if p["project_id"] == p_normal.project_id)
        assert normal_info["session_count"] == 1
        default_info = next(p for p in projects if p["project_id"] == "default")
        assert default_info["session_count"] == 1
        assert default_info["is_default"] is True
        default_code_info = next(p for p in projects if p["project_id"] == "default_code")
        assert default_code_info["is_default"] is True
        assert default_code_info["work_mode"] == "code"
        assert default_code_info["git"]["enabled"] is False
        assert "git" in normal_info

    @staticmethod
    @pytest.mark.asyncio
    async def test_pinned_sessions_not_counted(registered_channel, tmp_path):
        """置顶会话不计入任何项目 session_count。"""
        pa = _abspath(tmp_path, "app")
        proj = _make_project("P", pa)
        _make_session("s_normal", project_id=proj.project_id, project_dir=pa, last_user_message_at=100.0)
        _make_session("s_pinned", project_id=proj.project_id, project_dir=pa, pinned=True, pin_order=1, last_user_message_at=200.0)
        resp = await _call(registered_channel, "project.list", {"filter": "all"})
        p_info = next(p for p in resp["payload"]["projects"] if p["project_dir"] == pa)
        assert p_info["session_count"] == 1  # 仅非置顶


# ===========================================================================
# project.info
# ===========================================================================
class TestProjectInfo:
    @staticmethod
    @pytest.mark.asyncio
    @pytest.mark.parametrize("project_id, work_mode", [
        pytest.param("default", "work", id="virtual_default_work"),
        pytest.param("default_code", "code", id="virtual_default_code"),
    ])
    async def test_virtual_default(registered_channel, tmp_path, project_id, work_mode):
        """project_id=default/default_code → 返回 work/code 模式虚拟默认项目。"""
        _make_session("s1", project_dir="", last_user_message_at=100.0)
        resp = await _call(registered_channel, "project.info", {"project_id": project_id})
        assert resp["ok"] is True
        p = resp["payload"]
        assert p["project_id"] == project_id
        assert p["is_default"] is True
        assert p["work_mode"] == work_mode
        assert p["project"]["project_id"] == project_id
        assert p["git"]["enabled"] is False
        assert p["project_dir"] == ""
        # work 默认项目附带 session_count/created_at 校验
        if project_id == "default":
            assert p["session_count"] == 1
            assert p["created_at"] == 0

    @staticmethod
    @pytest.mark.asyncio
    async def test_real_project_and_hidden_not_found(registered_channel, tmp_path):
        """真实 project_id → 返回详情;隐藏项目 → NOT_FOUND。"""
        pa = _abspath(tmp_path, "app")
        proj = _make_project("应用", pa)
        _make_session("s1", project_id=proj.project_id, project_dir=pa, last_user_message_at=100.0)
        _make_session("s_cron", project_id=proj.project_id, project_dir=pa, cron_id="cron_1")
        _make_session("s_pinned", project_id=proj.project_id, project_dir=pa, pinned=True, pin_order=1, last_user_message_at=200.0)

        # 真实项目详情
        resp = await _call(registered_channel, "project.info", {"project_id": proj.project_id})
        assert resp["ok"] is True
        p = resp["payload"]
        assert p["project_id"] == proj.project_id
        assert p["name"] == "应用"
        assert p["project_dir"] == pa
        assert p["is_default"] is False
        assert p["work_mode"] == "work"
        assert p["project"]["project_id"] == proj.project_id
        assert p["project"]["git"] == p["git"]
        assert p["git"]["enabled"] is False
        # 统计口径同 project.list:仅非置顶普通会话(cron_id 为空)
        assert p["session_count"] == 1
        assert p["last_user_message_at"] == 100.0

        # 隐藏项目 → NOT_FOUND
        ph = _abspath(tmp_path, "hidden")
        hidden_proj = _make_project("隐藏", ph, hidden=True)
        resp_h = await _call(registered_channel, "project.info", {"project_id": hidden_proj.project_id})
        assert resp_h["ok"] is False
        assert resp_h["code"] == "NOT_FOUND"

    @staticmethod
    @pytest.mark.asyncio
    async def test_real_project_git_payload_backfills_new_error_fields(
        registered_channel, tmp_path,
    ):
        """旧 Project.git 快照缺少 error_code/hint 时,响应层补齐默认值。"""
        from jiuwenswarm.server.runtime.session.project_store import save_project

        pa = _abspath(tmp_path, "legacy")
        proj = _make_project("旧项目", pa)
        proj.git = {
            "enabled": True,
            "repo_root": pa,
            "initialized_by_jiuwenswarm": False,
            "detected_at": 1,
            "status": "error",
            "branch": "",
            "error": "legacy error",
            "is_dirty": False,
        }
        save_project(proj)

        resp = await _call(registered_channel, "project.info", {"project_id": proj.project_id})

        assert resp["ok"] is True
        assert resp["payload"]["git"]["error"] == "legacy error"
        assert resp["payload"]["git"]["error_code"] == ""
        assert resp["payload"]["git"]["hint"] == ""


# ===========================================================================
# project.get_sessions
# ===========================================================================
class TestProjectGetSessions:
    @staticmethod
    @pytest.mark.asyncio
    async def test_returns_non_pinned_sorted_desc(registered_channel, tmp_path):
        pa = _abspath(tmp_path, "app")
        proj = _make_project("P", pa)
        _make_session("s1", project_id=proj.project_id, project_dir=pa, last_user_message_at=100.0)
        _make_session("s2", project_id=proj.project_id, project_dir=pa, last_user_message_at=300.0)
        _make_session("s3", project_id=proj.project_id, project_dir=pa, last_user_message_at=200.0)
        _make_session("s_pinned", project_id=proj.project_id, project_dir=pa, pinned=True, pin_order=1, last_user_message_at=999.0)

        resp = await _call(
            registered_channel, "project.get_sessions", {"project_id": proj.project_id}
        )
        assert resp["ok"] is True
        sessions = resp["payload"]["sessions"]
        ids = [s["session_id"] for s in sessions]
        # 倒序: s2(300) > s3(200) > s1(100); 置顶 s_pinned 不出现
        assert ids == ["s2", "s3", "s1"]
        assert "s_pinned" not in ids
        assert resp["payload"]["total"] == 3

    @staticmethod
    @pytest.mark.asyncio
    async def test_pagination_and_hidden_default(registered_channel, tmp_path):
        """分页 limit/offset 截断 + default 含隐藏项目会话(临时归属默认)。"""
        pa = _abspath(tmp_path, "app")
        proj = _make_project("P", pa)
        for i in range(5):
            _make_session(f"s{i}", project_id=proj.project_id, project_dir=pa, last_user_message_at=float(i))

        # 分页:offset=1 跳过最新(4),取 3,2
        resp = await _call(
            registered_channel, "project.get_sessions",
            {"project_id": proj.project_id, "limit": 2, "offset": 1},
        )
        sessions = resp["payload"]["sessions"]
        assert len(sessions) == 2
        assert resp["payload"]["total"] == 5  # 截断前全量
        assert sessions[0]["session_id"] == "s3"
        assert sessions[1]["session_id"] == "s2"

        # default: 含隐藏项目的非置顶会话(临时归属默认)
        ph = _abspath(tmp_path, "hidden")
        _make_project("隐藏", ph, hidden=True)
        _make_session("s_hidden", project_dir=ph, last_user_message_at=100.0)
        _make_session("s_default", project_dir="", last_user_message_at=200.0)
        resp_d = await _call(
            registered_channel, "project.get_sessions", {"project_id": "default"}
        )
        ids = [s["session_id"] for s in resp_d["payload"]["sessions"]]
        assert "s_hidden" in ids  # 隐藏项目会话归默认
        assert "s_default" in ids


# ===========================================================================
# project.create
# ===========================================================================
class TestProjectCreate:
    @staticmethod
    @pytest.mark.asyncio
    async def test_create_new(registered_channel, tmp_path):
        pa = _abspath(tmp_path, "myapp")
        resp = await _call(
            registered_channel, "project.create", {"name": "我的应用", "project_dir": pa}
        )
        assert resp["ok"] is True
        assert resp["payload"]["project_id"].startswith("proj_")
        assert resp["payload"]["restored"] is False

    @staticmethod
    @pytest.mark.asyncio
    async def test_auto_restore_on_hidden(registered_channel, tmp_path):
        pa = _abspath(tmp_path, "restored")
        existing = _make_project("旧名", pa, hidden=True)
        resp = await _call(
            registered_channel, "project.create", {"name": "新名", "project_dir": pa}
        )
        assert resp["ok"] is True
        assert resp["payload"]["restored"] is True
        assert resp["payload"]["project_id"] == existing.project_id
        # 恢复后可见 + 名字更新
        from jiuwenswarm.server.runtime.session.project_store import get_project_by_id
        proj = get_project_by_id(existing.project_id, cache_bust=True)
        assert proj.hidden is False
        assert proj.name == "新名"

    @staticmethod
    @pytest.mark.asyncio
    @pytest.mark.parametrize("scenario", [
        pytest.param("visible_dup", id="conflict_on_visible_duplicate"),
        pytest.param("dup_name", id="conflict_on_duplicate_name"),
        pytest.param("hidden_name", id="conflict_on_hidden_project_name"),
        pytest.param("non_absolute", id="bad_request_non_absolute_path"),
        pytest.param("empty_name", id="bad_request_empty_name"),
    ])
    async def test_conflict_and_bad_request(registered_channel, tmp_path, scenario):
        """各类 CONFLICT 与 BAD_REQUEST 场景。"""
        if scenario == "visible_dup":
            # 同路径已有可见项目 → CONFLICT
            _make_project("P1", _abspath(tmp_path, "dup"))
            resp = await _call(
                registered_channel, "project.create",
                {"name": "P2", "project_dir": _abspath(tmp_path, "dup")},
            )
            assert resp["ok"] is False
            assert resp["code"] == "CONFLICT"
        elif scenario == "dup_name":
            # 不同路径、同名 → CONFLICT
            _make_project("P1", _abspath(tmp_path, "a"))
            resp = await _call(
                registered_channel, "project.create",
                {"name": "P1", "project_dir": _abspath(tmp_path, "b")},
            )
            assert resp["ok"] is False
            assert resp["code"] == "CONFLICT"
        elif scenario == "hidden_name":
            # 新项目复用隐藏项目名称 → CONFLICT(隐藏项目名称保留)
            _make_project("P", _abspath(tmp_path, "a"), hidden=True)
            resp = await _call(
                registered_channel, "project.create",
                {"name": "P", "project_dir": _abspath(tmp_path, "b")},
            )
            assert resp["ok"] is False
            assert resp["code"] == "CONFLICT"
        elif scenario == "non_absolute":
            resp = await _call(
                registered_channel, "project.create", {"name": "P", "project_dir": "relative/path"}
            )
            assert resp["code"] == "BAD_REQUEST"
        elif scenario == "empty_name":
            resp = await _call(
                registered_channel, "project.create",
                {"name": "", "project_dir": _abspath(tmp_path, "x")},
            )
            assert resp["code"] == "BAD_REQUEST"

    @staticmethod
    @pytest.mark.asyncio
    async def test_rejects_missing_existing_project_dir(registered_channel, tmp_path):
        """传 project_dir 表示选择现有项目,目录不存在时拒绝创建项目记录。"""
        missing = str(tmp_path / "missing")
        resp = await _call(
            registered_channel, "project.create", {"name": "P", "project_dir": missing}
        )
        assert resp["ok"] is False
        assert resp["code"] == "PROJECT_DIR_MISSING"
        assert resp["error"] == "project directory does not exist"


# ===========================================================================
# project.rename + 名称唯一性
# ===========================================================================
class TestProjectRename:
    @staticmethod
    @pytest.mark.asyncio
    async def test_rename_to_unique_and_self_ok(registered_channel, tmp_path):
        """重命名为新名 / 自身当前名 → 成功。"""
        pa = _abspath(tmp_path, "a")
        proj = _make_project("P1", pa)
        # 重命名为新名
        resp = await _call(
            registered_channel, "project.rename",
            {"project_id": proj.project_id, "name": "新名"},
        )
        assert resp["ok"] is True
        from jiuwenswarm.server.runtime.session.project_store import get_project_by_id
        assert get_project_by_id(proj.project_id, cache_bust=True).name == "新名"
        # 重命名为自身当前名不冲突
        resp2 = await _call(
            registered_channel, "project.rename",
            {"project_id": proj.project_id, "name": "新名"},
        )
        assert resp2["ok"] is True

    @staticmethod
    @pytest.mark.asyncio
    @pytest.mark.parametrize("scenario", [
        pytest.param("duplicate", id="conflict_on_duplicate_name"),
        pytest.param("hidden", id="conflict_with_hidden_project"),
    ])
    async def test_rename_conflict(registered_channel, tmp_path, scenario):
        """重命名为已占用名称(可见项目 / 隐藏项目)→ CONFLICT。"""
        from jiuwenswarm.server.runtime.session.project_store import get_project_by_id
        if scenario == "duplicate":
            _make_project("P1", _abspath(tmp_path, "a"))
            p2 = _make_project("P2", _abspath(tmp_path, "b"))
            resp = await _call(
                registered_channel, "project.rename",
                {"project_id": p2.project_id, "name": "P1"},
            )
            assert resp["ok"] is False
            assert resp["code"] == "CONFLICT"
            # 原名不变
            assert get_project_by_id(p2.project_id, cache_bust=True).name == "P2"
        elif scenario == "hidden":
            _make_project("P", _abspath(tmp_path, "a"), hidden=True)
            p2 = _make_project("P2", _abspath(tmp_path, "b"))
            resp = await _call(
                registered_channel, "project.rename",
                {"project_id": p2.project_id, "name": "P"},
            )
            assert resp["ok"] is False
            assert resp["code"] == "CONFLICT"

    @staticmethod
    @pytest.mark.asyncio
    @pytest.mark.parametrize("scenario", [
        pytest.param("default", id="forbidden_default"),
        pytest.param("not_found", id="not_found"),
        pytest.param("empty", id="bad_request_empty_name"),
        pytest.param("illegal", id="bad_request_illegal_name"),
        pytest.param("reserved", id="bad_request_reserved_name"),
    ])
    async def test_rename_errors(registered_channel, tmp_path, scenario):
        """重命名各类错误场景:FORBIDDEN / NOT_FOUND / BAD_REQUEST。"""
        if scenario == "default":
            resp = await _call(
                registered_channel, "project.rename",
                {"project_id": "default", "name": "X"},
            )
            assert resp["code"] == "FORBIDDEN"
        elif scenario == "not_found":
            resp = await _call(
                registered_channel, "project.rename",
                {"project_id": "proj_nope", "name": "X"},
            )
            assert resp["code"] == "NOT_FOUND"
        elif scenario == "empty":
            pa = _abspath(tmp_path, "a")
            proj = _make_project("P", pa)
            resp = await _call(
                registered_channel, "project.rename",
                {"project_id": proj.project_id, "name": ""},
            )
            assert resp["code"] == "BAD_REQUEST"
        elif scenario == "illegal":
            pa = _abspath(tmp_path, "a")
            proj = _make_project("P", pa)
            for bad_name in ["新/A", "新\\A", "新:A", "新*A", '新"A', "新<A", "新>A", "新|A", "新?A"]:
                resp = await _call(
                    registered_channel, "project.rename",
                    {"project_id": proj.project_id, "name": bad_name},
                )
                assert resp["ok"] is False, f"expected BAD_REQUEST for name={bad_name!r}"
                assert resp["code"] == "BAD_REQUEST", f"expected BAD_REQUEST for name={bad_name!r}"
            # 原名未被修改
            from jiuwenswarm.server.runtime.session.project_store import get_project_by_id
            assert get_project_by_id(proj.project_id, cache_bust=True).name == "P"
        elif scenario == "reserved":
            pa = _abspath(tmp_path, "a")
            proj = _make_project("P", pa)
            for reserved in ["CON", "PRN", "AUX", "NUL", "COM1", "LPT1"]:
                resp = await _call(
                    registered_channel, "project.rename",
                    {"project_id": proj.project_id, "name": reserved},
                )
                assert resp["ok"] is False, f"expected BAD_REQUEST for name={reserved!r}"
                assert resp["code"] == "BAD_REQUEST", f"expected BAD_REQUEST for name={reserved!r}"


# ===========================================================================
# project.remove / project.restore
# ===========================================================================
class TestProjectRemoveRestore:
    @staticmethod
    @pytest.mark.asyncio
    async def test_remove_happy_and_idempotent(registered_channel, tmp_path):
        """remove: 软删除后返回 affected(仅非置顶);已隐藏项目再 remove 幂等(affected=0)。"""
        pa = _abspath(tmp_path, "app")
        proj = _make_project("P", pa)
        _make_session("s1", project_id=proj.project_id, project_dir=pa, last_user_message_at=100.0)
        _make_session("s2", project_id=proj.project_id, project_dir=pa, last_user_message_at=200.0)
        _make_session("s_pin", project_id=proj.project_id, project_dir=pa, pinned=True, pin_order=1, last_user_message_at=300.0)

        # 第一次 remove: affected=2(仅非置顶)
        resp = await _call(registered_channel, "project.remove", {"project_id": proj.project_id})
        assert resp["ok"] is True
        assert resp["payload"]["affected_sessions"] == 2

        # 软删除后 get_sessions 返回 NOT_FOUND
        resp2 = await _call(
            registered_channel, "project.get_sessions", {"project_id": proj.project_id}
        )
        assert resp2["code"] == "NOT_FOUND"

        # 非置顶会话临时归入默认
        resp3 = await _call(
            registered_channel, "project.get_sessions", {"project_id": "default"}
        )
        ids = [s["session_id"] for s in resp3["payload"]["sessions"]]
        assert "s1" in ids and "s2" in ids

        # 已隐藏项目再 remove → affected=0(幂等)
        pa2 = _abspath(tmp_path, "app2")
        proj2 = _make_project("P2", pa2, hidden=True)
        resp_idem = await _call(
            registered_channel, "project.remove", {"project_id": proj2.project_id}
        )
        assert resp_idem["ok"] is True
        assert resp_idem["payload"]["affected_sessions"] == 0

    @staticmethod
    @pytest.mark.asyncio
    async def test_restore_happy_and_conflict(registered_channel, tmp_path):
        """restore: 重新归属会话;冲突(可见项目 / 同名占用)时不恢复。"""
        pa = _abspath(tmp_path, "app")
        proj = _make_project("P", pa)
        _make_session("s1", project_id=proj.project_id, project_dir=pa, last_user_message_at=100.0)
        _make_session("s2", project_id=proj.project_id, project_dir=pa, last_user_message_at=200.0)
        # 先移除
        await _call(registered_channel, "project.remove", {"project_id": proj.project_id})
        # 恢复:会话回归
        resp = await _call(
            registered_channel, "project.restore", {"project_id": proj.project_id}
        )
        assert resp["ok"] is True
        assert resp["payload"]["affected_sessions"] == 2
        resp2 = await _call(
            registered_channel, "project.get_sessions", {"project_id": proj.project_id}
        )
        ids = [s["session_id"] for s in resp2["payload"]["sessions"]]
        assert sorted(ids) == ["s1", "s2"]

        # 冲突 1:可见项目 restore → CONFLICT
        pa_v = _abspath(tmp_path, "visible")
        proj_v = _make_project("Vis", pa_v)  # 可见
        resp_v = await _call(
            registered_channel, "project.restore", {"project_id": proj_v.project_id}
        )
        assert resp_v["code"] == "CONFLICT"

        # 冲突 2:name 被其他可见项目占用 → CONFLICT
        pa_c = _abspath(tmp_path, "a3")
        pb_c = _abspath(tmp_path, "b3")
        proj_c = _make_project("P3", pa_c)
        await _call(registered_channel, "project.remove", {"project_id": proj_c.project_id})
        # 隐藏期间,另一个可见项目占用同名 "P3"
        _make_project("P3", pb_c)
        resp_c = await _call(
            registered_channel, "project.restore", {"project_id": proj_c.project_id}
        )
        assert resp_c["ok"] is False
        assert resp_c["code"] == "CONFLICT"
        # 仍处于隐藏状态(未恢复)
        from jiuwenswarm.server.runtime.session.project_store import get_project_by_id
        assert get_project_by_id(proj_c.project_id, cache_bust=True).hidden is True


# ===========================================================================
# session.pin + project.pinned_sessions
# ===========================================================================
class TestSessionPin:
    @staticmethod
    @pytest.mark.asyncio
    async def test_pin_and_unpin_idempotent(registered_channel, sessions_dir):
        _make_session("s1", last_user_message_at=100.0)
        # 置顶
        resp = await _call(registered_channel, "session.pin", {"session_id": "s1", "pinned": True})
        _drain()
        assert resp["ok"] is True
        assert resp["payload"]["pinned"] is True
        assert resp["payload"]["pin_order"] == 1
        # 再次置顶(幂等)
        resp2 = await _call(registered_channel, "session.pin", {"session_id": "s1", "pinned": True})
        _drain()
        assert resp2["payload"]["pin_order"] == 1
        # 取消
        resp3 = await _call(registered_channel, "session.pin", {"session_id": "s1", "pinned": False})
        _drain()
        assert resp3["payload"]["pinned"] is False
        assert resp3["payload"]["pin_order"] == 0

    @staticmethod
    @pytest.mark.asyncio
    async def test_pin_reindex_compact(registered_channel, sessions_dir):
        _make_session("s1", last_user_message_at=100.0)
        _make_session("s2", last_user_message_at=200.0)
        _make_session("s3", last_user_message_at=300.0)
        await _call(registered_channel, "session.pin", {"session_id": "s1", "pinned": True})
        _drain()
        await _call(registered_channel, "session.pin", {"session_id": "s2", "pinned": True})
        _drain()
        await _call(registered_channel, "session.pin", {"session_id": "s3", "pinned": True})
        _drain()
        # 取消 s2 → s3,s1 重编号为 1,2(新置顶在最前: s3 最先 pin_order 最小)
        await _call(registered_channel, "session.pin", {"session_id": "s2", "pinned": False})
        _drain()
        resp = await _call(registered_channel, "project.pinned_sessions", {})
        sessions = resp["payload"]["sessions"]
        assert [s["session_id"] for s in sessions] == ["s3", "s1"]
        assert [s["pin_order"] for s in sessions] == [1, 2]


# ===========================================================================
# 兼容性: 旧 session.create 不传 project_dir + session.rename
# ===========================================================================
class TestCompat:
    @staticmethod
    @pytest.mark.asyncio
    async def test_session_create_without_project_dir(registered_channel, sessions_dir):
        """不传 project_dir → 归入默认项目,project_dir="" 兜底,行为不变。"""
        resp = await _call(
            registered_channel, "session.create",
            {
                "title": "兼容",
                "mode": "code.normal",
                "channel_id": "web",
                "create_token": "compat-create-1",
            },
        )
        assert resp["ok"] is True
        allocated_id = resp["payload"]["session_id"]
        assert allocated_id.startswith("web_test_")
        # metadata 中 project_dir 为空
        from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata
        meta = get_session_metadata(allocated_id, cache_bust=True)
        assert meta["project_dir"] == ""
        # 该会话出现在默认项目
        resp2 = await _call(
            registered_channel, "project.get_sessions", {"project_id": "default"}
        )
        ids = [s["session_id"] for s in resp2["payload"]["sessions"]]
        assert allocated_id in ids


# ===========================================================================
# 不传 project_dir → 自动生成工作目录 + project_id 归属
# ===========================================================================
class TestEmptyPathProject:
    @staticmethod
    @pytest.mark.asyncio
    async def test_create_without_project_dir(registered_channel):
        """不传 project_dir → 在默认工作区下按项目名自动生成工作目录。"""
        from jiuwenswarm.server.runtime.session.project_store import get_agent_root_dir

        resp = await _call(
            registered_channel, "project.create", {"name": "空项目A"}
        )
        assert resp["ok"] is True
        assert resp["payload"]["project_id"].startswith("proj_")
        # work_mode 改造后默认工作区按 work_mode 分桶:Web 通道默认 work 模式
        # → workspace/work/{name}
        expected_path = str(get_agent_root_dir() / "workspace" / "work" / "空项目A")
        assert resp["payload"]["project_dir"] == expected_path
        assert os.path.isdir(expected_path)
        assert resp["payload"]["restored"] is False
        assert resp["payload"]["git"]["enabled"] is False
        assert resp["payload"]["project"]["project_id"] == resp["payload"]["project_id"]
        assert resp["payload"]["project"]["git"] == resp["payload"]["git"]

    @staticmethod
    @pytest.mark.asyncio
    @pytest.mark.parametrize("scenario", [
        pytest.param("empty_path_illegal", id="empty_path_illegal_name"),
        pytest.param("empty_path_reserved", id="empty_path_reserved_name"),
        pytest.param("with_path_illegal", id="with_path_illegal_name"),
        pytest.param("with_path_reserved", id="with_path_reserved_name"),
    ])
    async def test_create_illegal_reserved_name(registered_channel, tmp_path, scenario):
        """项目名含文件系统非法字符 / Windows 保留设备名 → BAD_REQUEST(store 层统一校验)。

        覆盖四种组合:不传/传 project_dir × illegal/reserved。
        """
        if scenario == "empty_path_illegal":
            bad_names = ["项目/A", "项目\\A", "项目:A", "项目*A", '项目"A', "项目<A", "项目>A", "项目|A", "项目?A"]
            for bad_name in bad_names:
                resp = await _call(registered_channel, "project.create", {"name": bad_name})
                assert resp["ok"] is False, f"expected BAD_REQUEST for name={bad_name!r}"
                assert resp["code"] == "BAD_REQUEST", f"expected BAD_REQUEST for name={bad_name!r}"
        elif scenario == "empty_path_reserved":
            for reserved in ["CON", "PRN", "AUX", "NUL", "COM1", "LPT1"]:
                resp = await _call(registered_channel, "project.create", {"name": reserved})
                assert resp["ok"] is False, f"expected BAD_REQUEST for name={reserved!r}"
                assert resp["code"] == "BAD_REQUEST", f"expected BAD_REQUEST for name={reserved!r}"
        elif scenario == "with_path_illegal":
            pa = _abspath(tmp_path, "workdir")
            bad_names = ["项目/A", "项目\\A", "项目:A", "项目*A", '项目"A', "项目<A", "项目>A", "项目|A", "项目?A"]
            for bad_name in bad_names:
                resp = await _call(
                    registered_channel, "project.create",
                    {"name": bad_name, "project_dir": pa},
                )
                assert resp["ok"] is False, f"expected BAD_REQUEST for name={bad_name!r}"
                assert resp["code"] == "BAD_REQUEST", f"expected BAD_REQUEST for name={bad_name!r}"
        elif scenario == "with_path_reserved":
            pa = _abspath(tmp_path, "workdir")
            for reserved in ["CON", "PRN", "AUX", "NUL", "COM1", "LPT1"]:
                resp = await _call(
                    registered_channel, "project.create",
                    {"name": reserved, "project_dir": pa},
                )
                assert resp["ok"] is False, f"expected BAD_REQUEST for name={reserved!r}"
                assert resp["code"] == "BAD_REQUEST", f"expected BAD_REQUEST for name={reserved!r}"


# ===========================================================================
# session.create + project_id 校验
# ===========================================================================
class TestSessionCreateProjectIdValidation:
    """session.create 对 project_id 的存在性/可见性校验。"""

    @staticmethod
    @pytest.mark.asyncio
    async def test_create_with_valid_project_id(registered_channel, tmp_path, sessions_dir):
        """传合法 project_id → 创建成功,会话归属到该项目。"""
        pa = _abspath(tmp_path, "app")
        proj = _make_project("P", pa)
        resp = await _call(
            registered_channel, "session.create",
            {
                "project_id": proj.project_id,
                "channel_id": "web",
                "create_token": "valid-project-create",
            },
        )
        assert resp["ok"] is True
        allocated_id = resp["payload"]["session_id"]
        # 归属到该项目
        r = await _call(
            registered_channel, "project.get_sessions", {"project_id": proj.project_id}
        )
        assert [s["session_id"] for s in r["payload"]["sessions"]] == [allocated_id]

    @staticmethod
    @pytest.mark.asyncio
    @pytest.mark.parametrize("scenario", [
        pytest.param("nonexistent", id="nonexistent_project_id"),
        pytest.param("hidden", id="hidden_project_id"),
    ])
    async def test_create_with_invalid_project_id(registered_channel, tmp_path, sessions_dir, scenario):
        """传无效 project_id(不存在 / 已隐藏)→ NOT_FOUND,不创建会话。"""
        if scenario == "nonexistent":
            target_id = "proj_nonexistent"
        else:  # hidden
            pa = _abspath(tmp_path, "app")
            proj = _make_project("P", pa, hidden=True)
            target_id = proj.project_id

        resp = await _call(
            registered_channel, "session.create",
            {"project_id": target_id, "create_token": f"invalid-{scenario}"},
        )
        assert resp["ok"] is False
        assert resp["code"] == "NOT_FOUND"
        # 会话目录不应被创建(metadata 为空)
        assert not any(sessions_dir.iterdir())


# ===========================================================================
# session.create + project_id / project_dir 一致性校验
# ===========================================================================
class TestSessionCreateProjectDirConsistency:
    """session.create 的 project_id / project_dir 绑定规则:
    仅 project_id 自动补齐、同时传校验一致性、仅 path 拒绝。"""

    @staticmethod
    @pytest.mark.asyncio
    async def test_project_id_auto_fills_dir(registered_channel, tmp_path, sessions_dir):
        """仅传 project_id(无 project_dir)→ 按项目记录自动补齐 project_dir。

        同时传 project_id + 一致 project_dir 也创建成功。
        """
        pa = _abspath(tmp_path, "app")
        proj = _make_project("P", pa)
        # 仅 project_id → 自动补齐
        resp1 = await _call(
            registered_channel, "session.create",
            {"project_id": proj.project_id, "create_token": "autofill-create"},
        )
        assert resp1["ok"] is True
        from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata
        meta = get_session_metadata(resp1["payload"]["session_id"], cache_bust=True)
        assert meta.get("project_id") == proj.project_id
        assert meta.get("project_dir") == pa
        # project_id + 一致 path → 成功
        resp2 = await _call(
            registered_channel, "session.create",
            {
                "project_id": proj.project_id,
                "project_dir": pa,
                "create_token": "matching-create",
            },
        )
        assert resp2["ok"] is True

    @staticmethod
    @pytest.mark.asyncio
    @pytest.mark.parametrize("scenario", [
        pytest.param("mismatched", id="mismatched_path_rejected"),
        pytest.param("path_only", id="path_only_rejected"),
        pytest.param("default_with_path", id="default_project_id_with_path_rejected"),
    ])
    async def test_inconsistent_project_id_path_rejected(registered_channel, tmp_path, sessions_dir, scenario):
        """project_id / project_dir 不一致(错配 / 仅 path / default 带 path)→ BAD_REQUEST。"""
        pa = _abspath(tmp_path, "app")
        other = _abspath(tmp_path, "other")
        if scenario == "mismatched":
            proj = _make_project("P", pa)
            params = {"project_id": proj.project_id, "project_dir": other}
        elif scenario == "path_only":
            params = {"project_dir": pa}
        else:  # default_with_path
            params = {"project_id": "default", "project_dir": pa}

        params["create_token"] = f"inconsistent-{scenario}"

        resp = await _call(registered_channel, "session.create", params)
        assert resp["ok"] is False
        assert resp["code"] == "BAD_REQUEST"
        assert not any(sessions_dir.iterdir())
