# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression tests for the enterprise user-plane read boundary."""

import httpx
import pytest
from sqlalchemy import URL, insert, update
from sqlalchemy.ext.asyncio import create_async_engine

from jiuwenswarm.channels.web.app_web import create_enterprise_app
from jiuwenswarm.channels.web.enterprise_backend import Settings, create_app
from jiuwenswarm.channels.web.enterprise_catalog import Catalog, User


def test_app_web_exports_enterprise_backend(monkeypatch):
    for name, value in {
        "JIUWENSWARM_EDITION": "enterprise",
        "USER_BACKEND_DB_USER": "fixture_reader",
        "USER_BACKEND_DB_PASSWORD": "fixture-value",
        "USER_BACKEND_DB_TYPE": "mariadb",
        "USER_BACKEND_DB_HOST": "manager-db.example",
        "USER_BACKEND_DB_PORT": "3306",
        "USER_BACKEND_DB_NAME": "manager",
        "USER_BACKEND_IDENTITY_URL": "http://identity.example",
    }.items():
        monkeypatch.setenv(name, value)

    app = create_enterprise_app()
    paths = {route.path for route in app.routes}
    assert {"/ready", "/api/v1/user-console/agent-contexts", "/{path:path}"} <= paths


@pytest.mark.asyncio
async def test_identity_proxy_keeps_browser_binding_cookies_separate(monkeypatch):
    cookie_name = "__Host-identity_federation_browser"
    upstream_cookies = []

    def identity(request):
        cookie = request.headers.get("cookie")
        upstream_cookies.append(cookie)
        binding = cookie or f"{cookie_name}=binding-{len(upstream_cookies)}"
        return httpx.Response(
            303,
            headers={
                "Location": "https://keycloak.example/authorize",
                "Set-Cookie": f"{binding}; Secure; HttpOnly; Path=/",
            },
            stream=httpx.ByteStream(b""),
        )

    async_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kwargs: async_client(transport=httpx.MockTransport(identity), **kwargs),
    )
    app = create_app(Settings(
        database=URL.create("postgresql+asyncpg", username="reader", database="manager"),
        schema=None, identity="https://identity.example", manager="",
        default_http="", default_ws="",
    ))
    login = "/idp/v1/auth/federation/keycloak/login"
    client_options = {"transport": httpx.ASGITransport(app=app), "base_url": "https://web.example"}
    async with (
        app.router.lifespan_context(app),
        async_client(**client_options) as browser_a,
        async_client(**client_options) as browser_b,
    ):
        assert (await browser_a.get(login)).status_code == 303
        assert (await browser_b.get(login)).status_code == 303
        assert (await browser_a.get(login)).status_code == 303
        assert browser_a.cookies[cookie_name] == "binding-1"
        assert browser_b.cookies[cookie_name] == "binding-2"
        assert upstream_cookies == [None, None, f"{cookie_name}=binding-1"]


@pytest.mark.asyncio
async def test_agent_catalog_checks_live_admission_and_resource_scope():
    engine = create_async_engine("sqlite+aiosqlite://")
    catalog = Catalog(None)
    user = User("Alice", {"A": "Organization A"})
    try:
        async with engine.begin() as db:
            await db.run_sync(catalog.instances.metadata.create_all)
            await db.execute(insert(catalog.instances), [{
                "jiuwenclaw_id": "X", "jiuwenclaw_name": "Cluster X",
                "gateway_http": "http://gateway:19000",
            }, {
                "jiuwenclaw_id": "Y", "jiuwenclaw_name": "Cluster Y",
                "gateway_http": "http://gateway-y:19000",
            }])
            await db.execute(insert(catalog.grants), [{
                "jiuwenclaw_id": "X", "subject_type": "org", "subject_id": "A",
                "login_policy": "allow", "enabled": True,
            }, {
                "jiuwenclaw_id": "Y", "subject_type": "user", "subject_id": "Bob",
                "login_policy": "allow", "enabled": True,
            }])
            await db.execute(insert(catalog.agents), [{
                "jiuwenclaw_id": "X", "resource_id": "1",
                "match_expr": "group_id == 'A'", "enabled": True,
            }, {
                "jiuwenclaw_id": "Y", "resource_id": "1",
                "match_expr": "group_id == 'B'", "enabled": True,
            }])

        async with engine.connect() as db:
            assert [row["bot_id"] for row in await catalog.contexts(db, user)] == ["1"]
            assert await catalog.resolve(db, user, "X", "1", "A") is not None
            assert await catalog.resolve(db, user, "X", "1", "B") is None
            assert await catalog.resolve(db, user, "Y", "1", "A") is None
            assert await catalog.resolve(db, User("Alice", {}), "X", "1", "__none__") is None
            bob = User("Bob", {"B": "Organization B"})
            target = await catalog.resolve(db, bob, "Y", "1", "B")
            assert target is not None and target.gateway_http == "http://gateway-y:19000"

        async with engine.begin() as db:
            await db.execute(update(catalog.agents).values(enabled=False))

        async with engine.connect() as db:
            assert not await catalog.contexts(db, user)
            assert await catalog.resolve(db, user, "X", "1", "A") is None
    finally:
        await engine.dispose()


@pytest.mark.parametrize("username", ["root", "postgres", ""])
def test_user_backend_rejects_builtin_or_missing_database_user(monkeypatch, username):
    monkeypatch.setenv("USER_BACKEND_DB_TYPE", "mariadb")
    monkeypatch.setenv("USER_BACKEND_DB_USER", username)
    monkeypatch.setenv("USER_BACKEND_DB_PASSWORD", "fixture-value")
    with pytest.raises(ValueError, match="dedicated user-plane database account"):
        Settings.from_env()
