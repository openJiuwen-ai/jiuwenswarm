"""全双工派任务：所选模型和登录凭据在每条发往 Core 的路径上都带到。"""

from types import SimpleNamespace

import pytest

from openjiuwen_protocol.e2a.constants import E2A_MODEL_AUTH_PARAM_KEY

from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.extensions.video_duplex.backend import task_adapter
from jiuwenswarm.extensions.video_duplex.backend.task_adapter import AgentTaskExecutor
from jiuwenswarm.extensions.video_duplex.backend.video_search import execute_core_agent
from jiuwenswarm.runtime.interaction import InteractionAnswerInput

MODEL = {"model_name": "GLM-5.1", "model_selection": {"kind": "model", "name": "GLM-5.1"}}
AUTH = {"api_base": "https://apig.example", "api_key": "id-token", "credential_ref": "ref"}


class RecordingClient:
    def __init__(self):
        self.envs = []

    async def send_request(self, env):
        self.envs.append(env)
        return SimpleNamespace(ok=True, payload={"content": "完成", "success": True})


def answer_input(source):
    return InteractionAnswerInput(
        request_id="req-1",
        channel_id="video_tool",
        session_id="core-1",
        interaction_id="ask-1",
        answers=({"question": "允许吗？", "custom_input": "", "selected_options": ["允许"]},),
        source=source,
        mode="agent",
        work_mode="work",
    )


@pytest.mark.asyncio
async def test_resume_after_approval_keeps_selected_model_and_credentials():
    client = RecordingClient()

    await execute_core_agent(
        client,
        question="整理桌面",
        query="整理桌面",
        visual_context="",
        search_session_id="s1",
        core_session_id="core-1",
        interaction_answer=answer_input("permission_interrupt"),
        model=MODEL,
        model_auth=AUTH,
    )

    params = client.envs[0].params
    assert params["answers"][0]["selected_options"] == ["允许"]
    assert params["model_name"] == "GLM-5.1"
    assert params["model_selection"] == MODEL["model_selection"]
    assert params[E2A_MODEL_AUTH_PARAM_KEY] == AUTH


@pytest.mark.asyncio
async def test_first_run_carries_selected_model_and_credentials():
    client = RecordingClient()

    await execute_core_agent(
        client,
        question="整理桌面",
        query="整理桌面",
        visual_context="",
        search_session_id="s1",
        model=MODEL,
        model_auth=AUTH,
    )

    params = client.envs[0].params
    assert params["model_name"] == "GLM-5.1"
    assert params[E2A_MODEL_AUTH_PARAM_KEY] == AUTH


def executor(client):
    ex = AgentTaskExecutor.__new__(AgentTaskExecutor)
    ex.client = client
    ex.auth_sessions = {("u1", "s1"): "login-1"}
    return ex


def task(source):
    return {
        "owner": "u1",
        "session": "s1",
        "request_id": "req-1",
        "core_session_id": "core-1",
        "request": {"model": MODEL},
        "interaction": {
            "operation_id": "op-1",
            "request_id": "ask-1",
            "source": source,
            "answers": [{"question": "允许吗？", "answer": "", "selected_options": ["允许"]}],
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "method", "has_model"),
    [("permission_interrupt", ReqMethod.CHAT_SEND, True), ("video_task", ReqMethod.CHAT_ANSWER, False)],
)
async def test_answer_carries_model_like_normal_chat(monkeypatch, source, method, has_model):
    seen = []

    async def fake_auth(model_name, auth_session):
        seen.append((model_name, auth_session))
        return AUTH

    monkeypatch.setattr(task_adapter, "_login_model_auth", fake_auth)
    client = RecordingClient()

    await executor(client).answer(task(source))

    env = client.envs[0]
    assert env.method == method.value
    assert seen == [("GLM-5.1", "login-1")]
    assert env.params[E2A_MODEL_AUTH_PARAM_KEY] == AUTH
    assert ("model_name" in env.params) is has_model


def record_warnings(monkeypatch):
    warnings = []

    def warning(msg, *args, **_kwargs):
        warnings.append(msg % args)

    monkeypatch.setattr(task_adapter.logger, "warning", warning)
    return warnings


@pytest.mark.asyncio
async def test_login_model_auth_attaches_credentials(monkeypatch):
    import gateway.auth.passthrough as passthrough

    monkeypatch.setattr(passthrough, "expired_login_session", lambda params, sid: None)

    def fake_normalize(params, sid):
        assert sid == "login-1"
        params[E2A_MODEL_AUTH_PARAM_KEY] = AUTH

    monkeypatch.setattr(passthrough, "normalize_model_auth", fake_normalize)

    assert await task_adapter._login_model_auth("GLM-5.1", "login-1") == AUTH


@pytest.mark.asyncio
async def test_login_model_auth_skips_without_model_or_login(monkeypatch):
    import gateway.auth.passthrough as passthrough

    def boom(*_args):
        raise AssertionError("不该碰凭据链路")

    monkeypatch.setattr(passthrough, "expired_login_session", boom)
    monkeypatch.setattr(passthrough, "normalize_model_auth", boom)

    assert await task_adapter._login_model_auth("", "login-1") is None
    assert await task_adapter._login_model_auth("GLM-5.1", "") is None


@pytest.mark.asyncio
async def test_refresh_failure_still_attaches_existing_credentials(monkeypatch):
    import gateway.auth.passthrough as passthrough

    def expired(params, sid):
        raise RuntimeError("auth service down")

    def fake_normalize(params, sid):
        params[E2A_MODEL_AUTH_PARAM_KEY] = AUTH

    monkeypatch.setattr(passthrough, "expired_login_session", expired)
    monkeypatch.setattr(passthrough, "normalize_model_auth", fake_normalize)

    warnings = record_warnings(monkeypatch)
    assert await task_adapter._login_model_auth("GLM-5.1", "login-1") == AUTH
    assert any("续期失败" in w and "auth service down" in w for w in warnings)


@pytest.mark.asyncio
async def test_credential_failure_does_not_fail_task_and_logs_reason(monkeypatch):
    import gateway.auth.passthrough as passthrough

    def broken(params, sid):
        raise RuntimeError("token parse error")

    monkeypatch.setattr(passthrough, "expired_login_session", lambda params, sid: None)
    monkeypatch.setattr(passthrough, "normalize_model_auth", broken)

    warnings = record_warnings(monkeypatch)
    assert await task_adapter._login_model_auth("GLM-5.1", "login-1") is None
    assert any("挂登录凭据失败" in w and "token parse error" in w for w in warnings)
