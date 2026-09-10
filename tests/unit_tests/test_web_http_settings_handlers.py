# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Settings Web HTTP routes through real WebChannel handlers (no dispatch mock)."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.web import app_web_handlers
from jiuwenswarm.gateway.channel_manager.web.web_http_app import create_web_http_app
from jiuwenswarm.gateway.channel_manager.web.app_web_handlers import (
    WebHandlersBindParams,
    _register_web_handlers,
)
from jiuwenswarm.gateway.channel_manager.web.web_connect import WebChannel, WebChannelConfig


class _FakeCron:
    def __init__(self) -> None:
        self.jobs: dict[str, dict] = {
            "job-1": {
                "id": "job-1",
                "name": "n1",
                "enabled": True,
                "project_id": "default",
            },
        }
        self.run_calls: list[str] = []

    def job_metadata(self) -> dict:
        return {"modes": []}

    async def list_jobs(self, params=None):
        return list(self.jobs.values())

    async def get_job(self, job_id, **kwargs):
        return self.jobs.get(job_id)

    async def update_job(self, job_id, patch, **kwargs):
        if job_id not in self.jobs:
            raise KeyError(job_id)
        self.jobs[job_id] = {**self.jobs[job_id], **dict(patch or {})}
        return self.jobs[job_id]

    async def delete_job(self, job_id, **kwargs):
        return self.jobs.pop(job_id, None) is not None

    async def toggle_job(self, job_id, enabled, **kwargs):
        if job_id not in self.jobs:
            raise KeyError(job_id)
        self.jobs[job_id]["enabled"] = bool(enabled)
        return self.jobs[job_id]

    async def preview_job(self, job_id, count=5, **kwargs):
        if job_id not in self.jobs:
            raise KeyError(job_id)
        n = int(count) if count is not None else 5
        return [{"wake_at": "2026-01-01T00:00:00Z", "push_at": "2026-01-01T00:00:00Z"}] * n

    async def run_now_info(self, job_id):
        if job_id not in self.jobs:
            raise KeyError(job_id)
        return {"run_id": f"run-{job_id}", "session_id": f"sess-{job_id}"}

    async def run_now_with_session(self, job_id, **kwargs):
        if job_id not in self.jobs:
            raise KeyError(job_id)
        self.run_calls.append(job_id)
        return {"run_id": f"run-{job_id}", "session_id": f"sess-{job_id}"}

    async def run_now(self, job_id, **kwargs):
        if job_id not in self.jobs:
            raise KeyError(job_id)
        self.run_calls.append(job_id)


def _client_with_cron(cron: _FakeCron) -> TestClient:
    channel = WebChannel(WebChannelConfig(host="127.0.0.1", port=0), RobotMessageRouter())
    _register_web_handlers(
        WebHandlersBindParams(channel=channel, cron_controller=cron, cron_registry=cron),
    )
    return TestClient(create_web_http_app(channel))


def test_settings_config_models_locale_roundtrip():
    cron = _FakeCron()
    client = _client_with_cron(cron)

    r = client.get("/api/v1/config")
    assert r.status_code == 200
    assert r.headers["x-web-rpc-method"] == "config.get"
    assert r.json()["ok"] is True
    assert "app_version" in r.json()["data"]

    r = client.get("/api/v1/models")
    assert r.status_code == 200
    assert r.headers["x-web-rpc-method"] == "models.list"
    body = r.json()
    assert body["ok"] is True
    assert "models" in body["data"]
    assert "active_model" in body["data"]

    r = client.get("/api/v1/locale")
    assert r.status_code == 200
    assert r.headers["x-web-rpc-method"] == "locale.get_conf"
    lang = r.json()["data"]["preferred_language"]
    assert lang in {"zh", "en"}

    flip = "en" if lang == "zh" else "zh"
    r = client.put("/api/v1/locale", json={"preferred_language": flip})
    assert r.status_code == 200
    assert r.headers["x-web-rpc-method"] == "locale.set_conf"
    assert r.json()["data"]["preferred_language"] == flip

    r = client.get("/api/v1/locale")
    assert r.json()["data"]["preferred_language"] == flip

    # restore
    client.put("/api/v1/locale", json={"preferred_language": lang})

    r = client.put("/api/v1/locale", json={"preferred_language": "fr"})
    assert r.status_code == 400
    assert r.json()["ok"] is False
    assert r.json()["error"]["code"] == "BAD_REQUEST"


def test_enterprise_models_list_uses_bot_header_and_hides_api_key(monkeypatch):
    captured: dict[str, object] = {}

    async def fake_load(request, slots):
        captured["metadata"] = request.metadata
        captured["slots"] = slots
        return SimpleNamespace(
            models={
                "default_model": [
                    {
                        "template_id": "model-template-1",
                        "template_name": "main model",
                        "model_id": "GLM-5.2",
                        "model_provider": "OpenAI",
                        "api_base": "http://model.example/v1",
                        "api_key": "server-only-secret",
                        "parameters": {
                            "temperature": 0.6,
                            "reasoning_level": "high",
                        },
                        "timeout": 90,
                    },
                ],
            },
        )

    monkeypatch.setattr(app_web_handlers, "is_enterprise", lambda: True)
    monkeypatch.setattr(
        app_web_handlers,
        "load_effective_enterprise_config",
        fake_load,
    )

    client = _client_with_cron(_FakeCron())
    response = client.get(
        "/api/v1/models",
        headers={
            "X-User-Id": "user-1",
            "X-Group-Id": "group-1",
            "X-Bot-Id": "bot-1",
        },
    )

    assert response.status_code == 200
    assert captured["metadata"] == {
        "user_id": "user-1",
        "routing": {"group_id": "group-1", "bot_id": "bot-1"},
    }
    assert captured["slots"] == {app_web_handlers.TemplateRefSlot.DEFAULT_MODEL}
    data = response.json()["data"]
    assert len(data["models"]) == 1
    model = data["models"][0]
    assert isinstance(model.pop("context_window_tokens"), int)
    assert model == {
        "model_name": "GLM-5.2",
        "api_base": "http://model.example/v1",
        "api_key": "",
        "model_provider": "OpenAI",
        "timeout": 90,
        "temperature": 0.6,
        "reasoning_level": "high",
        "is_default": True,
        "alias": "",
    }
    assert data == {
        "models": [model],
        "active_model": "GLM-5.2",
        "model_source": "enterprise",
    }


def test_enterprise_models_list_does_not_fallback_to_local_config(monkeypatch):
    async def fake_load(_request, _slots):
        return None

    def fail_local_config_read():
        raise AssertionError("enterprise models.list must not read local config")

    monkeypatch.setattr(app_web_handlers, "is_enterprise", lambda: True)
    monkeypatch.setattr(
        app_web_handlers,
        "load_effective_enterprise_config",
        fake_load,
    )
    monkeypatch.setattr(app_web_handlers, "get_config", fail_local_config_read)

    client = _client_with_cron(_FakeCron())
    response = client.get(
        "/api/v1/models",
        headers={"X-Bot-Id": "missing-bot"},
    )

    assert response.status_code == 200
    assert response.json()["data"] == {
        "models": [],
        "active_model": "",
        "model_source": "enterprise",
    }


def test_cron_job_full_path():
    cron = _FakeCron()
    client = _client_with_cron(cron)

    r = client.get("/api/v1/cron/jobs", params={"project_id": "default"})
    assert r.status_code == 200
    assert r.headers["x-web-rpc-method"] == "cron.job.list"
    assert any(j["id"] == "job-1" for j in r.json()["data"]["jobs"])

    r = client.get("/api/v1/cron/jobs/job-1")
    assert r.status_code == 200
    assert r.json()["data"]["job"]["name"] == "n1"

    r = client.patch("/api/v1/cron/jobs/job-1", json={"patch": {"name": "renamed"}})
    assert r.status_code == 200
    assert r.headers["x-web-rpc-method"] == "cron.job.update"
    assert r.json()["data"]["job"]["name"] == "renamed"
    assert cron.jobs["job-1"]["name"] == "renamed"

    r = client.post("/api/v1/cron/jobs/job-1/actions/toggle", json={"enabled": False})
    assert r.status_code == 200
    assert r.headers["x-web-rpc-method"] == "cron.job.toggle"
    assert r.json()["data"]["job"]["enabled"] is False

    r = client.post("/api/v1/cron/jobs/job-1/actions/preview", json={"count": 2})
    assert r.status_code == 200
    assert r.headers["x-web-rpc-method"] == "cron.job.preview"
    assert len(r.json()["data"]["next"]) == 2

    r = client.post("/api/v1/cron/jobs/job-1/actions/run-now", json={})
    assert r.status_code == 200
    assert r.headers["x-web-rpc-method"] == "cron.job.run_now"
    assert r.json()["data"]["accepted"] is True
    assert r.json()["data"]["run_id"] == "run-job-1"
    assert cron.run_calls == ["job-1"]

    r = client.delete("/api/v1/cron/jobs/job-1")
    assert r.status_code == 200
    assert r.json()["data"]["deleted"] is True

    r = client.get("/api/v1/cron/jobs/job-1")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"

    r = client.post("/api/v1/cron/jobs/missing/actions/toggle", json={})
    assert r.status_code == 400
    assert r.json()["error"]["message"] == "enabled is required"


class _FakeCronRegistry:
    def __init__(self, cron: _FakeCron) -> None:
        self.cron = cron
        self.toggle_kwargs: list[dict] = []

    async def get_controller(self, service_id: str, agent_id: str):
        return self.cron

    async def web_toggle_job(
        self,
        job_id: str,
        enabled: bool,
        service_id: str,
        agent_id: str,
        *,
        group_id: str | None = None,
        bot_id: str | None = None,
        user_id: str | None = None,
    ) -> dict:
        self.toggle_kwargs.append(
            {
                "job_id": job_id,
                "enabled": enabled,
                "service_id": service_id,
                "agent_id": agent_id,
                "group_id": group_id,
                "bot_id": bot_id,
                "user_id": user_id,
            }
        )
        return await self.cron.toggle_job(
            job_id,
            enabled,
            group_id=group_id,
            bot_id=bot_id,
            user_id=user_id,
        )


def test_cron_job_toggle_passes_routing_triple_to_registry():
    cron = _FakeCron()
    registry = _FakeCronRegistry(cron)
    channel = WebChannel(WebChannelConfig(host="127.0.0.1", port=0), RobotMessageRouter())
    _register_web_handlers(
        WebHandlersBindParams(channel=channel, cron_controller=cron, cron_registry=registry),
    )
    client = TestClient(create_web_http_app(channel))

    r = client.post(
        "/api/v1/cron/jobs/job-1/actions/toggle",
        json={"enabled": True},
        headers={
            "X-Group-Id": "g1",
            "X-Bot-Id": "b1",
            "X-User-Id": "u1",
        },
    )
    assert r.status_code == 200
    assert r.json()["data"]["job"]["enabled"] is True
    assert registry.toggle_kwargs == [
        {
            "job_id": "job-1",
            "enabled": True,
            "service_id": "default",
            "agent_id": "default",
            "group_id": "g1",
            "bot_id": "b1",
            "user_id": "u1",
        }
    ]
