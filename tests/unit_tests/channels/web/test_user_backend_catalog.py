# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression tests for the enterprise user-plane read boundary."""

import httpx
import pytest
from sqlalchemy import URL, insert, update
from sqlalchemy.ext.asyncio import create_async_engine

from jiuwenswarm.channels.web.enterprise_backend import Settings, _target, create_app
from jiuwenswarm.channels.web.enterprise_catalog import Catalog, User, _matches_agent_rule


def test_app_web_exports_enterprise_backend(monkeypatch):
    from jiuwenswarm.channels.web.app_web import create_enterprise_app

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
    # FastAPI >=0.165 keeps include_router results as _IncludedRouter (no .path);
    # walk nested original_router.routes so both old and new layouts work.
    paths: set[str] = set()
    stack = list(app.routes)
    while stack:
        route = stack.pop()
        path = getattr(route, "path", None)
        if isinstance(path, str):
            paths.add(path)
        nested = getattr(route, "original_router", None)
        if nested is not None:
            stack.extend(nested.routes)
    assert {"/ready", "/api/v1/user-console/agent-contexts", "/{path:path}"} <= paths


@pytest.mark.parametrize("edition", [None, "", "personal"])
def test_direct_enterprise_backend_requires_enterprise_edition(monkeypatch, edition):
    if edition is None:
        monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)
    else:
        monkeypatch.setenv("JIUWENSWARM_EDITION", edition)

    with pytest.raises(RuntimeError, match="JIUWENSWARM_EDITION=enterprise"):
        create_app()


@pytest.mark.asyncio
async def test_identity_proxy_keeps_browser_binding_cookies_separate(monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
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
    user = User("Alice", {"A": "Organization A", "B": "Organization B"})
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
                "jiuwenclaw_id": "X", "resource_id": "1", "ref_template_id": "cfg",
                "match_expr": "group_id in('A',)", "enabled": True,
            }, {
                "jiuwenclaw_id": "Y", "resource_id": "1", "ref_template_id": "cfg",
                "match_expr": "group_id == 'B'", "enabled": True,
            }])
            await db.execute(insert(catalog.service_resources), [{
                "jiuwenclaw_id": "X", "resource_id": "scope-x",
                "ref_template_id": "rt", "priority": 0, "enabled": True,
            }, {
                "jiuwenclaw_id": "Y", "resource_id": "scope-y",
                "ref_template_id": "rt", "priority": 0, "enabled": True,
            }])
            await db.execute(insert(catalog.agent_definitions), [{
                "agent_config_template_id": "cfg", "runtime_template_id": "rt",
                "agent_name": "Agent One",
            }])

        async with engine.connect() as db:
            contexts, matched = await catalog.contexts(db, user)
            assert [row["bot_id"] for row in contexts] == ["1"]
            assert matched == 1
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
            assert await catalog.contexts(db, user) == ([], 0)
            assert await catalog.resolve(db, user, "X", "1", "A") is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_agent_catalog_requires_configured_agent_template():
    engine = create_async_engine("sqlite+aiosqlite://")
    catalog = Catalog(None)
    user = User("Alice", {"A": "Organization A"})
    try:
        async with engine.begin() as db:
            await db.run_sync(catalog.instances.metadata.create_all)
            await db.execute(insert(catalog.instances), [{
                "jiuwenclaw_id": "X", "jiuwenclaw_name": "Cluster X",
            }])
            await db.execute(insert(catalog.grants), [{
                "jiuwenclaw_id": "X", "subject_type": "org", "subject_id": "A",
                "login_policy": "allow", "enabled": True,
            }])
            await db.execute(insert(catalog.agents), [{
                "jiuwenclaw_id": "X", "resource_id": "bot", "ref_template_id": "cfg",
                "enabled": True,
            }])
            await db.execute(insert(catalog.service_resources), [{
                "jiuwenclaw_id": "X", "resource_id": "scope-a",
                "ref_template_id": "rt-a", "priority": 0, "enabled": True,
            }, {
                "jiuwenclaw_id": "X", "resource_id": "scope-b",
                "ref_template_id": "rt-b", "priority": 1, "enabled": True,
            }])

        async with engine.connect() as db:
            # No configured Agent template: hidden, but still counted as matched.
            assert await catalog.contexts(db, user) == ([], 1)

        async with engine.begin() as db:
            await db.execute(insert(catalog.agent_definitions), [{
                "agent_config_template_id": "cfg", "runtime_template_id": "rt-b",
                "agent_name": "Wrong runtime",
            }])

        async with engine.connect() as db:
            # First-fit resolves the highest-priority scope (rt-a), not rt-b.
            assert await catalog.contexts(db, user) == ([], 1)

        async with engine.begin() as db:
            await db.execute(insert(catalog.agent_definitions), [{
                "agent_config_template_id": "cfg", "runtime_template_id": "rt-a",
                "agent_name": "Configured",
            }])

        async with engine.connect() as db:
            contexts, matched = await catalog.contexts(db, user)
            assert matched == 1
            assert contexts == [{
                "bot_id": "bot", "group_id": "A", "user_id": "Alice",
                "jiuwenclaw_id": "X", "runtime_template_id": "rt-a",
                "agent_template_id": "cfg", "agent_name": "Configured",
                "allow_local_skill_upload": False,
                "group_name": "Organization A", "jiuwenclaw_name": "Cluster X",
            }]
    finally:
        await engine.dispose()


@pytest.mark.parametrize("rule", [None, "", [], "[]"])
def test_agent_rule_preserves_unrestricted_values(rule):
    assert _matches_agent_rule(rule, "Alice", "A")


@pytest.mark.parametrize("rule", ["False", "group_id = 'A'", {"group_id": "A"}])
def test_agent_rule_rejects_invalid_or_nonmatching_values(rule):
    assert not _matches_agent_rule(rule, "Alice", "A")


@pytest.mark.parametrize("path", ["api/v1/search", "ws"])
def test_proxy_preserves_query_parameters_and_escapes_raw_utf8(path):
    parameters = b"&blank=&multiple=a+b&multiple=%2F&array[]=1"
    encoded = b"q=%e4%b8%ad" + parameters
    raw = "q=中".encode("utf-8") + parameters
    assert _target("https://gateway.example", path, encoded).query == encoded
    assert _target("https://gateway.example", path, raw).query == b"q=%E4%B8%AD" + parameters


@pytest.mark.parametrize("username", ["root", "postgres", ""])
def test_user_backend_rejects_builtin_or_missing_database_user(monkeypatch, username):
    monkeypatch.setenv("USER_BACKEND_DB_TYPE", "mariadb")
    monkeypatch.setenv("USER_BACKEND_DB_USER", username)
    monkeypatch.setenv("USER_BACKEND_DB_PASSWORD", "fixture-value")
    with pytest.raises(ValueError, match="dedicated user-plane database account"):
        Settings.from_env()
