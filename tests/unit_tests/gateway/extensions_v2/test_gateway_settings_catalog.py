# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway packages in the shared settings catalog (S_10 section 2, AgentServer S_18)."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.gateway.channel_manager.web import webui_handlers
from jiuwenswarm.gateway.extensions_v2 import assembly
from jiuwenswarm.gateway.routing import e2a_proxy
from jiuwenswarm.gateway.extensions_v2.webui.settings_contributions import merge_catalog, parse_settings

CASES = json.loads((Path(__file__).parents[4] / "tests/fixtures/settings_contribution_cases.json").read_text("utf-8"))
AGENT = {"revision": "r-agent", "items": [{"package_id": "agent.weather", "id": "show", "kind": "item",
                                           "location": {"menu": "general", "section": "display"},
                                           "scope": "application", "instance_key": "show", "presentation": "row",
                                           "title": {"zh": "显示", "en": "Show"}}],
         "errors": {"agent.broken::x": "PARENT_MISSING"}}
THEME = [{"package_id": "test.theme", "id": "menu", "kind": "menu", "location": {"after": "extensions"},
          "title": {"zh": "主题", "en": "Theme"}, "icon": "image"}]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_gateway_declarations_follow_the_shared_cases(case):
    if case["gateway"]:
        parse_settings(case["declaration"])
    else:
        with pytest.raises(ValueError):
            parse_settings(case["declaration"])


def test_merging_keeps_the_agentserver_catalog_and_covers_both_in_the_revision():
    assert merge_catalog(AGENT, []) == AGENT, "without Gateway items the AgentServer answer is unchanged"
    merged = merge_catalog(AGENT, THEME)
    assert merged["items"] == [*AGENT["items"], *THEME]
    assert merged["errors"] == AGENT["errors"]
    payload = {"items": merged["items"], "errors": merged["errors"]}
    expected = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    assert merged["revision"] == expected != AGENT["revision"]


class Channel:
    channel_id = "web"

    def __init__(self) -> None:
        self.methods: dict = {}
        self.responses: list = []

    def register_method(self, name, handler):
        self.methods[name] = handler

    async def send_response(self, ws, req_id, *, ok, payload=None, error=None, code=None):
        self.responses.append({"ok": ok, "payload": payload, "error": error, "code": code})


@pytest.mark.parametrize("agent_ok,agent_payload,expected", [
    (True, AGENT, {"ok": True, "payload": merge_catalog(AGENT, THEME), "error": None, "code": None}),
    (False, {"error": "catalog unavailable", "code": "SERVICE_UNAVAILABLE"},
     {"ok": False, "payload": None, "error": "catalog unavailable", "code": "SERVICE_UNAVAILABLE"}),
])
async def test_the_catalog_method_adds_gateway_packages(monkeypatch, agent_ok, agent_payload, expected):
    sent = []

    async def send(agent_client, env, **kwargs):
        sent.append((agent_client, env))
        return SimpleNamespace(ok=agent_ok, payload=agent_payload)

    runtime = SimpleNamespace(browser=SimpleNamespace(settings_contributions=lambda: THEME))
    monkeypatch.setattr(e2a_proxy, "send_agent_request_with_timeout", send)
    monkeypatch.setattr(assembly, "get_gateway_runtime", lambda: runtime)
    channel = Channel()
    webui_handlers.register_webui_handlers(channel, lambda: SimpleNamespace(server_ready=True))
    await channel.methods["webui.settings.catalog"](SimpleNamespace(_jiuwen_ws_id="browser-1"), "req-1", {}, None)
    assert channel.responses == [expected], "failures keep the plain proxy answer"
    assert sent[0][1].channel_context == {"ws_id": "browser-1"} and sent[0][1].method == "webui.settings.catalog"


async def test_without_the_extension_runtime_the_catalog_is_passed_through(monkeypatch):
    async def send(agent_client, env, **kwargs):
        return SimpleNamespace(ok=True, payload=AGENT)

    monkeypatch.setattr(e2a_proxy, "send_agent_request_with_timeout", send)
    monkeypatch.setattr(assembly, "get_gateway_runtime", lambda: None)
    channel = Channel()
    webui_handlers.register_webui_handlers(channel, lambda: SimpleNamespace(server_ready=True))
    await channel.methods["webui.settings.catalog"](SimpleNamespace(), "req-1", {}, None)
    assert channel.responses == [{"ok": True, "payload": AGENT, "error": None, "code": None}]
