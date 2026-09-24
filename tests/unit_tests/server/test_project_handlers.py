# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""项目接口 handler 单元测试 — project.list / get_sessions / create /
remove / restore / pinned_sessions + session.pin + 兼容性(session.create / rename)。

复用 test_session_metadata.py 的 _FakeWebChannel 桩模式,自包含 fixtures。
"""
from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

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

    async def send_event(self, ws, event, payload):
        pass


class _FakeSessionCreateAgentClient:
    """AgentServer boundary fake backed by the production binding helpers."""

    server_ready = True

    def __init__(self) -> None:
        self.requests: list[object] = []
        self._sequence = 0
        # project.remove 预检(project.lifecycle + running_sessions)的应答桩;
        # 用例按需改写 has_running_sessions 模拟项目下有无会话在执行。
        self.project_lifecycle: dict = {
            "exists": True, "hidden": False, "has_running_sessions": False,
        }
        # AgentServer 侧 project.remove 权威 busy 扫描用的 runtime 桩;
        # None 时等价于无 runtime(扫描跳过),与旧版行为一致。
        self.project_runtime = None

    async def send_request(self, request):
        self.requests.append(request)
        params = dict(request.params or {})

        if getattr(request, "method", None) == "project.lifecycle":
            return SimpleNamespace(ok=True, payload=dict(self.project_lifecycle))

        # Phase 2: session.pin 走 E2A 转发到 AgentServer(SessionAdapter SESSION_PIN)。
        # fake 用生产 set_session_pinned 落盘,保持 Web handler 集成路径可验证。
        if getattr(request, "method", None) == "session.pin":
            return self._handle_session_pin(params)

        if getattr(request, "method", None) in {
            "project.info", "project.pinned_sessions", "project.pin",
            "project.get_sessions",
            "project.get_cron_sessions",
            "project.list",
            "project.create",
            "project.rename",
            "project.remove",
            "project.restore",
        }:
            from jiuwenswarm.common.schema.agent import AgentRequest
            from jiuwenswarm.common.schema.message import ReqMethod
            from jiuwenswarm.server.runtime.gateway_adapter.project_adapter import (
                ProjectAdapter,
            )

            method = {
                "project.info": ReqMethod.PROJECT_INFO,
                "project.pinned_sessions": ReqMethod.PROJECT_PINNED_SESSIONS,
                "project.get_sessions": ReqMethod.PROJECT_GET_SESSIONS,
                "project.get_cron_sessions": ReqMethod.PROJECT_GET_CRON_SESSIONS,
                "project.list": ReqMethod.PROJECT_LIST,
                "project.create": ReqMethod.PROJECT_CREATE,
                "project.rename": ReqMethod.PROJECT_RENAME,
                "project.pin": ReqMethod.PROJECT_PIN,
                "project.remove": ReqMethod.PROJECT_REMOVE,
                "project.restore": ReqMethod.PROJECT_RESTORE,
            }[request.method]
            response = await ProjectAdapter(
                runtime_probe=lambda: self.project_runtime
            ).handle(
                AgentRequest(
                    request_id=str(request.request_id or ""),
                    channel_id=str(request.channel or "web"),
                    session_id=request.session_id,
                    req_method=method,
                    params=params,
                    user_id=str(request.user_id or ""),
                )
            )
            return SimpleNamespace(ok=response.ok, payload=response.payload)

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

    @staticmethod
    def _handle_session_pin(params: dict):
        from jiuwenswarm.server.runtime.session.session_metadata import set_session_pinned

        sid = str(params.get("session_id") or "").strip()
        raw_pinned = params.get("pinned")
        if not sid or not isinstance(raw_pinned, bool):
            return SimpleNamespace(
                ok=False, payload={"error": "session_id required / pinned must be bool", "code": "BAD_REQUEST"},
            )
        result = set_session_pinned(sid, raw_pinned)
        if result is None:
            return SimpleNamespace(
                ok=False, payload={"error": "session not found", "code": "NOT_FOUND"},
            )
        new_pinned, new_order = result
        return SimpleNamespace(
            ok=True, payload={"pinned": new_pinned, "pin_order": new_order},
        )


class _FakeRemoveRuntime:
    """project.remove busy 扫描的 AgentRuntime 桩。

    ``running``/``parked`` 分别控制 ``is_session_running`` 与
    ``has_parked_team_streams`` 的应答,模拟执行中会话与 parked 的
    Team 常驻流。
    """

    def __init__(self, running=(), parked=()):
        self._running = set(running)
        self._parked = set(parked)

    def is_session_running(self, session_id):
        return session_id in self._running

    def has_parked_team_streams(self, session_id):
        return session_id in self._parked


@pytest.fixture()
def sessions_dir(tmp_path, monkeypatch):
    d = tmp_path / "sessions"
    d.mkdir()
    monkeypatch.setattr("jiuwenswarm.common.utils.get_agent_sessions_dir", lambda: d)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_agent_sessions_dir",
        lambda: d,
    )
    from jiuwenswarm.server.runtime.session.session_metadata import (
        _METADATA_CACHE,
        _METADATA_QUEUE,
    )
    _METADATA_CACHE.clear()
    yield d
    # 惰性 metadata 迁移会异步写盘；在 monkeypatch 撤销前排空队列，
    # 避免慢环境中把当前用例的写入落到下一个用例的 sessions_dir。
    _METADATA_QUEUE.join()


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
    # 挂到 channel 上供用例断言(如 project.remove 前先停 cron 的调用参数)。
    async def hide_jobs(project_id, *, commit=None):
        if commit is not None:
            await commit()
        return {"stopped_cron_jobs": 0}

    channel.cron_controller = SimpleNamespace(
        store=SimpleNamespace(list_jobs=AsyncMock(return_value=[])),
        scheduler=SimpleNamespace(
            _lifecycle_owners=set(),
            remember_lifecycle_owner=lambda owner: None,
        ),
        hide_project_jobs=AsyncMock(side_effect=hide_jobs),
    )
    _register_web_handlers(
        WebHandlersBindParams(channel=channel, agent_client=channel.agent_client,
                              cron_controller=channel.cron_controller)
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
    save_project(proj)
    if hidden:
        from jiuwenswarm.server.runtime.session import project_store
        def legacy_record(projects):
            for record in projects:
                if record["project_id"] == proj.project_id:
                    record["hidden"] = True
        project_store._mutate(legacy_record)
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
    async def test_real_and_migrated_project_info(registered_channel, tmp_path):
        """隐藏项目仅通过 include_hidden 查询。"""
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

        # 隐藏项目默认不可见。
        ph = _abspath(tmp_path, "hidden")
        hidden_proj = _make_project("隐藏", ph, hidden=True)
        resp_h = await _call(registered_channel, "project.info", {"project_id": hidden_proj.project_id})
        assert resp_h["ok"] is False
        resp_h = await _call(registered_channel, "project.info", {"project_id": hidden_proj.project_id, "include_hidden": True})
        assert resp_h["payload"]["project"]["hidden"] is True

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
    async def test_pagination_and_project_membership(registered_channel, tmp_path):
        """分页 limit/offset 截断，其他项目会话不落入默认项目。"""
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

        # 旧会话按 project_dir 推断项目归属，不落入默认项目。
        ph = _abspath(tmp_path, "hidden")
        proj_h = _make_project("归档项目", ph)
        _make_session("s_hidden", project_dir=ph, last_user_message_at=100.0)
        _make_session("s_default", project_dir="", last_user_message_at=200.0)
        resp_d = await _call(
            registered_channel, "project.get_sessions", {"project_id": "default"}
        )
        ids = [s["session_id"] for s in resp_d["payload"]["sessions"]]
        assert "s_hidden" not in ids  # 已推断为其他项目的会话
        assert "s_default" in ids


# ===========================================================================
# project.get_cron_sessions
# ===========================================================================
class TestProjectGetCronSessions:
    @staticmethod
    @pytest.mark.asyncio
    async def test_returns_only_matching_cron_sessions(registered_channel, tmp_path):
        project_dir = _abspath(tmp_path, "cron-app")
        project = _make_project("Cron", project_dir)
        _make_session(
            "cron-new", project_id=project.project_id, project_dir=project_dir,
            cron_id="job-1", last_user_message_at=300.0,
        )
        _make_session(
            "cron-old", project_id=project.project_id, project_dir=project_dir,
            cron_id="job-2", last_user_message_at=100.0,
        )
        _make_session(
            "ordinary", project_id=project.project_id, project_dir=project_dir,
            last_user_message_at=400.0,
        )
        _make_session(
            "pinned-cron", project_id=project.project_id, project_dir=project_dir,
            cron_id="job-1", pinned=True, pin_order=1, last_user_message_at=500.0,
        )

        response = await _call(
            registered_channel,
            "project.get_cron_sessions",
            {"project_id": project.project_id, "cron_id": "job-1"},
        )

        assert response["ok"] is True
        assert response["payload"]["total"] == 1
        assert [item["session_id"] for item in response["payload"]["sessions"]] == [
            "cron-new"
        ]


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
# project.pin
# ===========================================================================
class TestProjectPin:
    @staticmethod
    @pytest.mark.asyncio
    async def test_pin_reindexes_and_unpin(registered_channel, tmp_path):
        first = _make_project("First", _abspath(tmp_path, "first"))
        second = _make_project("Second", _abspath(tmp_path, "second"))

        response_first = await _call(
            registered_channel, "project.pin", {"project_id": first.project_id, "pinned": True}
        )
        response_second = await _call(
            registered_channel, "project.pin", {"project_id": second.project_id, "pinned": True}
        )
        response_unpin = await _call(
            registered_channel, "project.pin", {"project_id": first.project_id, "pinned": False}
        )

        assert response_first["payload"] == {"pinned": True, "pin_order": 1}
        assert response_second["payload"] == {"pinned": True, "pin_order": 1}
        assert response_unpin["payload"] == {"pinned": False, "pin_order": 0}
        from jiuwenswarm.server.runtime.session.project_store import get_project_by_id
        assert get_project_by_id(second.project_id, cache_bust=True).pin_order == 1

    @staticmethod
    @pytest.mark.asyncio
    async def test_pin_rejects_default_and_bad_params(registered_channel, tmp_path):
        project = _make_project("Project", _abspath(tmp_path, "app"))
        default = await _call(
            registered_channel, "project.pin", {"project_id": "default", "pinned": True}
        )
        invalid = await _call(
            registered_channel, "project.pin", {"project_id": project.project_id, "pinned": "yes"}
        )
        assert default["code"] == "FORBIDDEN"
        assert invalid["code"] == "BAD_REQUEST"


# ===========================================================================
# Project deletion and batch lifecycle coverage lives in test_archive_lifecycle.py.
# ===========================================================================


# ===========================================================================
# session.pin + project.pinned_sessions
# ===========================================================================
class TestSessionPin:
    @staticmethod
    @pytest.mark.asyncio
    async def test_pinned_cron_execution_session_appears_in_pinned_list(
        registered_channel, sessions_dir,
    ):
        _make_session("cron-run", cron_id="job-1", channel_id="__cron__")
        _make_session("other-channel", channel_id="tui", pinned=True, pin_order=2)

        pin = await _call(
            registered_channel, "session.pin", {"session_id": "cron-run", "pinned": True}
        )
        assert pin["ok"] is True

        pinned = await _call(registered_channel, "project.pinned_sessions", {})
        assert [session["session_id"] for session in pinned["payload"]["sessions"]] == [
            "cron-run"
        ]
        assert pinned["payload"]["sessions"][0]["cron_id"] == "job-1"

        cron = await _call(
            registered_channel,
            "project.get_cron_sessions",
            {"project_id": "default", "cron_id": "job-1"},
        )
        assert cron["payload"]["sessions"] == []

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
        pytest.param("deleted", id="deleted_project_id"),
    ])
    async def test_create_with_invalid_project_id(registered_channel, tmp_path, sessions_dir, scenario):
        """传无效 project_id(不存在 / 已删除)→ NOT_FOUND,不创建会话。"""
        if scenario == "nonexistent":
            target_id = "proj_nonexistent"
        else:  # deleted
            pa = _abspath(tmp_path, "app")
            proj = _make_project("P", pa, hidden=True)
            target_id = proj.project_id
            from jiuwenswarm.server.runtime.session.project_store import hide_project
            hide_project(target_id)

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


class TestProjectRemoveRestore:
    @staticmethod
    @pytest.mark.asyncio
    async def test_remove_happy_and_idempotent(registered_channel, tmp_path):
        """remove: 返回被隐藏的对话数(含置顶,不含 cron);已隐藏项目再 remove 幂等。"""
        pa = _abspath(tmp_path, "app")
        proj = _make_project("P", pa)
        _make_session("s1", project_id=proj.project_id, project_dir=pa, last_user_message_at=100.0)
        _make_session("s2", project_id=proj.project_id, project_dir=pa, last_user_message_at=200.0)
        _make_session("s_pin", project_id=proj.project_id, project_dir=pa, pinned=True, pin_order=1, last_user_message_at=300.0)
        _make_session("s_cron", project_id=proj.project_id, project_dir=pa, cron_id="cron_1")

        # 第一次 remove: affected=3(置顶对话同样被隐藏;cron 执行会话不计)
        resp = await _call(registered_channel, "project.remove", {"project_id": proj.project_id})
        assert resp["ok"] is True
        assert resp["payload"]["affected_sessions"] == 3

        # 移除后 get_sessions 返回 NOT_FOUND
        resp2 = await _call(
            registered_channel, "project.get_sessions", {"project_id": proj.project_id}
        )
        assert resp2["code"] == "NOT_FOUND"

        # 未归档会话随项目一起隐藏:不再回落到默认项目
        resp3 = await _call(
            registered_channel, "project.get_sessions", {"project_id": "default"}
        )
        ids = [s["session_id"] for s in resp3["payload"]["sessions"]]
        assert ids == []

        # 置顶会话一并隐藏,但置顶状态保留在会话元数据里
        pinned_resp = await _call(registered_channel, "project.pinned_sessions", {})
        assert pinned_resp["payload"]["sessions"] == []

        # 默认项目统计同步排除被隐藏会话
        resp4 = await _call(registered_channel, "project.list", {"filter": "all"})
        default_info = next(
            p for p in resp4["payload"]["projects"] if p["project_id"] == "default"
        )
        assert default_info["session_count"] == 0

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
    async def test_remove_stops_cron_jobs(registered_channel, tmp_path):
        """remove: 隐藏项目前先停止其下定时任务(停用 + 取消在途执行)。"""
        pa = _abspath(tmp_path, "app")
        proj = _make_project("P", pa)
        cc = registered_channel.cron_controller

        resp = await _call(
            registered_channel, "project.remove", {"project_id": proj.project_id}
        )
        assert resp["ok"] is True
        cc.hide_project_jobs.assert_awaited_once()
        assert cc.hide_project_jobs.await_args.args == (proj.project_id,)
        assert callable(cc.hide_project_jobs.await_args.kwargs["commit"])

        # 任务总数随响应带回:0 表示项目下没有定时任务,前端据此只提示
        # "项目已移除",不再附带"其定时任务已停止"。
        assert resp["payload"]["stopped_cron_jobs"] == 0

        # 项目下有任务时数量原样透传(含移除前已停用的),前端保持原文案
        pa2 = _abspath(tmp_path, "app2")
        proj2 = _make_project("P2", pa2)

        async def hide_jobs_three(project_id, *, commit=None):
            if commit is not None:
                await commit()
            return {"stopped_cron_jobs": 3}

        cc.hide_project_jobs = AsyncMock(side_effect=hide_jobs_three)
        resp2 = await _call(
            registered_channel, "project.remove", {"project_id": proj2.project_id}
        )
        assert resp2["ok"] is True
        assert resp2["payload"]["stopped_cron_jobs"] == 3

    @staticmethod
    @pytest.mark.asyncio
    async def test_remove_aborts_when_cron_stop_fails(registered_channel, tmp_path):
        """cron 停止失败时终止移除:项目保持可见,不产生半隐藏状态。"""
        pa = _abspath(tmp_path, "app")
        proj = _make_project("P", pa)
        cc = registered_channel.cron_controller
        cc.hide_project_jobs = AsyncMock(
            side_effect=RuntimeError("cron runs are still stopping")
        )

        resp = await _call(
            registered_channel, "project.remove", {"project_id": proj.project_id}
        )
        assert resp["ok"] is False
        assert resp["code"] == "CRON_STOP_FAILED"

        # 项目未被隐藏:列表里仍然可见
        listing = await _call(registered_channel, "project.list", {"filter": "all"})
        assert any(
            p["project_id"] == proj.project_id
            for p in listing["payload"]["projects"]
        )

    @staticmethod
    @pytest.mark.asyncio
    async def test_remove_rejected_by_precheck_when_session_running(registered_channel, tmp_path):
        """预检发现项目下有会话在执行:SESSION_BUSY 拒绝,不触碰 cron 停用。"""
        pa = _abspath(tmp_path, "app")
        proj = _make_project("P", pa)
        _make_session("s1", project_id=proj.project_id, project_dir=pa)
        cc = registered_channel.cron_controller
        registered_channel.agent_client.project_lifecycle["has_running_sessions"] = True

        resp = await _call(
            registered_channel, "project.remove", {"project_id": proj.project_id}
        )
        assert resp["ok"] is False
        assert resp["code"] == "SESSION_BUSY"

        # 定时任务停用流程完全未启动:移除被拒后任务不能已被停用
        cc.hide_project_jobs.assert_not_awaited()
        listing = await _call(registered_channel, "project.list", {"filter": "all"})
        assert any(
            p["project_id"] == proj.project_id
            for p in listing["payload"]["projects"]
        )

    @staticmethod
    @pytest.mark.asyncio
    async def test_remove_requires_precheck_support(registered_channel, tmp_path):
        proj = _make_project("P", _abspath(tmp_path, "app"))
        del registered_channel.agent_client.project_lifecycle["has_running_sessions"]

        resp = await _call(
            registered_channel, "project.remove", {"project_id": proj.project_id}
        )
        assert resp["ok"] is False
        assert resp["code"] == "SERVICE_UNAVAILABLE"
        registered_channel.cron_controller.hide_project_jobs.assert_not_awaited()

    @staticmethod
    @pytest.mark.asyncio
    async def test_remove_authoritative_busy_scan_blocks(registered_channel, tmp_path):
        """commit 侧权威扫描兜底:预检漏报(竞态)时仍拒绝隐藏项目。"""
        pa = _abspath(tmp_path, "app")
        proj = _make_project("P", pa)
        _make_session("s_run", project_id=proj.project_id, project_dir=pa)
        # 预检应答保持默认 has_running_sessions=False,只让 AgentServer 侧
        # runtime 桩报告 s_run 在执行,模拟预检与提交之间的竞态窗口。
        registered_channel.agent_client.project_runtime = _FakeRemoveRuntime(
            running={"s_run"}
        )

        resp = await _call(
            registered_channel, "project.remove", {"project_id": proj.project_id}
        )
        assert resp["ok"] is False
        assert resp["code"] == "SESSION_BUSY"

        from jiuwenswarm.server.runtime.session.project_store import get_project_by_id
        assert get_project_by_id(proj.project_id, cache_bust=True).hidden is False

    @staticmethod
    @pytest.mark.asyncio
    async def test_remove_busy_scan_exemptions(registered_channel, tmp_path, monkeypatch):
        """扫描豁免:parked Team 常驻流、cron 执行会话、其他项目的会话不阻塞。"""
        from jiuwenswarm.common.schema.agent import AgentRequest
        from jiuwenswarm.common.schema.message import ReqMethod
        from jiuwenswarm.server.runtime.gateway_adapter.project_adapter import (
            ProjectAdapter,
        )

        pa = _abspath(tmp_path, "app")
        pb = _abspath(tmp_path, "other")
        proj = _make_project("P", pa)
        other = _make_project("Other", pb)
        _make_session("s_idle", project_id=proj.project_id, project_dir=pa)
        _make_session("s_park", project_id=proj.project_id, project_dir=pa)
        _make_session("s_cron", project_id=proj.project_id, project_dir=pa, cron_id="cron_1")
        _make_session("s_other", project_id=other.project_id, project_dir=pb)

        runtime = _FakeRemoveRuntime(
            running={"s_park", "s_cron", "s_other"},
            parked={"s_park"},
        )
        from jiuwenswarm.server.runtime.gateway_adapter import project_adapter

        collect = project_adapter.collect_all_sessions_metadata
        scans = 0

        def counted_collect():
            nonlocal scans
            scans += 1
            return collect()

        monkeypatch.setattr(project_adapter, "collect_all_sessions_metadata", counted_collect)
        response = await ProjectAdapter(runtime_probe=lambda: runtime).handle(
            AgentRequest(
                request_id="req-busy-scan",
                channel_id="web",
                session_id="",
                req_method=ReqMethod.PROJECT_REMOVE,
                params={"project_id": proj.project_id},
                user_id="",
            )
        )
        assert response.ok is True
        assert scans == 1
        # s_idle + s_park 计入 affected(cron 执行会话与外部项目会话不计)
        assert response.payload["affected_sessions"] == 2

    @staticmethod
    @pytest.mark.asyncio
    async def test_restore_returns_pinned_sessions_to_pinned_area(registered_channel, tmp_path):
        """恢复项目时置顶会话回到置顶区,并保留原 pin_order。"""
        pa = _abspath(tmp_path, "app")
        proj = _make_project("P", pa)
        _make_session("s_pin", project_id=proj.project_id, project_dir=pa, pinned=True, pin_order=3)

        await _call(registered_channel, "project.remove", {"project_id": proj.project_id})
        assert (await _call(registered_channel, "project.pinned_sessions", {}))["payload"]["sessions"] == []

        restored = await _call(registered_channel, "project.restore", {"project_id": proj.project_id})
        assert restored["ok"] is True
        assert restored["payload"]["affected_sessions"] == 1

        pinned = (await _call(registered_channel, "project.pinned_sessions", {}))["payload"]["sessions"]
        assert [s["session_id"] for s in pinned] == ["s_pin"]
        assert pinned[0]["pin_order"] == 3

    @staticmethod
    @pytest.mark.asyncio
    async def test_hidden_project_sessions_excluded_from_cron_and_info(registered_channel, tmp_path):
        """隐藏项目的 cron 执行会话同样不进默认项目;无关会话的统计不受影响。"""
        pa = _abspath(tmp_path, "app")
        proj = _make_project("P", pa)
        _make_session("s_cron", project_id=proj.project_id, project_dir=pa, cron_id="cron_1")
        # 空 project_dir 的会话不属于任何项目,始终留在默认项目
        _make_session("s_ok", project_dir="", last_user_message_at=100.0)
        await _call(registered_channel, "project.remove", {"project_id": proj.project_id})

        resp = await _call(
            registered_channel, "project.get_cron_sessions", {"project_id": "default"}
        )
        assert resp["payload"]["sessions"] == []

        info = await _call(registered_channel, "project.info", {"project_id": "default"})
        assert info["payload"]["project"]["session_count"] == 1  # 仅 s_ok

    @staticmethod
    @pytest.mark.asyncio
    async def test_unknown_project_id_still_falls_back_to_default(registered_channel, tmp_path):
        """项目记录不存在(存量数据)的会话仍归入默认项目,不因移除逻辑被误隐藏。"""
        pa = _abspath(tmp_path, "app")
        _make_session(
            "s_orphan", project_id="proj_gone", project_dir=pa, last_user_message_at=100.0
        )
        resp = await _call(registered_channel, "project.list", {"filter": "all"})
        default_info = next(
            p for p in resp["payload"]["projects"] if p["project_id"] == "default"
        )
        assert default_info["session_count"] == 1
        sessions = await _call(
            registered_channel, "project.get_sessions", {"project_id": "default"}
        )
        assert [s["session_id"] for s in sessions["payload"]["sessions"]] == ["s_orphan"]

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

        # 冲突 2:name 被其他可见项目占用 → PROJECT_NAME_CONFLICT
        # (与撤销归档连带恢复同码,前端共用同一套"先重命名占用方"文案)
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
        assert resp_c["code"] == "PROJECT_NAME_CONFLICT"
        # 仍处于隐藏状态(未恢复)
        from jiuwenswarm.server.runtime.session.project_store import get_project_by_id
        assert get_project_by_id(proj_c.project_id, cache_bust=True).hidden is True


# ===========================================================================
# session.pin + project.pinned_sessions
# ===========================================================================


@pytest.mark.asyncio
async def test_project_soft_delete_contract(registered_channel, tmp_path):
    from jiuwenswarm.server.runtime.session import project_store
    assert "project.delete" not in registered_channel.methods
    for method in ("project.remove", "project.restore"):
        result = await _call(registered_channel, method, {"project_id": "default"})
        assert result["code"] == "FORBIDDEN"
    directory = _abspath(tmp_path, "restore-by-directory")
    project = _make_project("RestoreMe", directory, pinned=True, pin_order=1)
    await _call(registered_channel, "project.remove", {"project_id": project.project_id})
    hidden = project_store.get_project_by_id(project.project_id, cache_bust=True)
    assert hidden.hidden and not hidden.pinned
    assert project.project_id not in {p.project_id for p in project_store.list_projects()}
    restored, was_restored = project_store.create_project_checked("RestoreMe", directory)
    assert was_restored and restored.project_id == project.project_id
    assert not restored.hidden
