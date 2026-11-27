# coding: utf-8
"""Tests for the MCP Apps sandbox proxy routes on the WebChannel port."""
from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from jiuwenswarm.gateway.channel_manager.web.mcp_app_sandbox_http import (
    SANDBOX_INFO_PATH,
    SANDBOX_PATH,
    build_mcp_app_csp,
    register_mcp_app_sandbox_routes,
)


def _client() -> TestClient:
    app = FastAPI()
    register_mcp_app_sandbox_routes(app)
    return TestClient(app)


def test_sandbox_page_sends_csp_header_from_query():
    csp = {"connectDomains": ["https://api.test"], "resourceDomains": ["https://cdn.test"]}
    response = _client().get(SANDBOX_PATH, params={"csp": json.dumps(csp)})
    assert response.status_code == 200
    header = response.headers["content-security-policy"]
    assert "connect-src https://api.test" in header
    assert "worker-src blob: https://cdn.test" in header
    # 代理页防嵌入：仅宿主 UI（loopback）可嵌入，其余来源拒绝
    assert "frame-ancestors 'self' http://localhost:* http://127.0.0.1:*" in header
    assert "sandbox-proxy-ready" in response.text
    assert "doc.write(params.html)" in response.text
    # OpenStreetMap-style usage policies reject requests without a Referer.
    assert response.headers["referrer-policy"] == "origin"


def test_sandbox_csp_never_allows_self_or_injected_keywords():
    header = build_mcp_app_csp(
        {
            "connectDomains": [
                "'self'",
                "https://ok.test; script-src *",
                "*",
                "http://127.0.0.1:19000",
                "http://:19000",
                "http://.0.0.1:19000",
            ],
            # CR-10997-04：own-origin 剔除必须按浏览器匹配语义——空宿主+
            # 端口、点后缀、通配形式均可匹配自身 origin，一律剔除。
            "frameDomains": [
                "http://:19000",
                "http://.0.0.1:19000",
                "http://127.0.0.1:19000",
                "https://frames.test",
            ],
            "resourceDomains": [
                "http://.0.0.1:19000/api",
                "http://127.0.0.1:80",
                "https://.example.com",
                "https://cdn.test",
            ],
            "baseUriDomains": ["http://.0.0.1:19000"],
        },
        own_origin="http://127.0.0.1:19000",
    )
    connect = next(d for d in header.split("; ") if d.startswith("connect-src"))
    # 自身 origin 被拒（connect/frame/resource/baseUri 四域统一剔除；
    # 空宿主/点后缀/通配三形态 + 精确）
    assert connect == "connect-src 'none'"
    frame = next(d for d in header.split("; ") if d.startswith("frame-src"))
    assert frame == "frame-src https://frames.test"
    # 端口/scheme 不同 → 保留（评审 keep-case）
    assert "http://127.0.0.1:80" in header
    assert "https://.example.com" in header
    assert "https://cdn.test" in header
    assert "'self'" not in header
    assert header.count("script-src") == 1


def test_sandbox_page_tolerates_bad_csp_query():
    response = _client().get(SANDBOX_PATH, params={"csp": "{not json"})
    assert response.status_code == 200
    assert "connect-src 'none'" in response.headers["content-security-policy"]


def test_sandbox_info_reports_bound_origin_not_host_header():
    response = _client().get(SANDBOX_INFO_PATH, headers={"Host": "127.0.0.1:5173"})
    url = response.json()["url"]
    assert url.endswith(SANDBOX_PATH)
    assert ":5173" not in url  # the proxied Host header must not leak into the origin
