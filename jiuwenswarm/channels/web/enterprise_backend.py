# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Enterprise user-plane authentication, admission and Gateway proxy."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from http.cookiejar import Cookie, CookieJar, DefaultCookiePolicy
from string import punctuation
from urllib.parse import quote, quote_from_bytes, unquote, urlsplit
from urllib.request import Request as CookieRequest

import httpx
from fastapi import APIRouter, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy import URL, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.datastructures import Headers
from starlette.responses import StreamingResponse
from websockets.exceptions import WebSocketException
from websockets.legacy.client import connect as connect_ws

from jiuwenswarm.channels.web.enterprise_catalog import Catalog, User

_CLUSTER_COOKIE = "jiuwenclaw_id"
_CONTEXT = "/api/v1/user-console"
_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
}
_SCOPE_FIELDS = {"user_id": "x-user-id", "group_id": "x-group-id", "bot_id": "x-bot-id"}


@dataclass(frozen=True)
class Settings:
    database: URL
    schema: str | None
    identity: str
    manager: str
    default_http: str
    default_ws: str

    @classmethod
    def from_env(cls) -> Settings:
        driver = {
            "postgresql": "postgresql+asyncpg", "mysql": "mysql+aiomysql",
            "mariadb": "mysql+aiomysql",
        }[os.environ["USER_BACKEND_DB_TYPE"]]
        user = os.environ["USER_BACKEND_DB_USER"]
        password = os.environ["USER_BACKEND_DB_PASSWORD"]
        if user.lower() in {"root", "postgres"} or not user.strip() or not password:
            raise ValueError("A dedicated user-plane database account is required")
        schema = os.getenv("USER_BACKEND_PG_SCHEMA") or None
        return cls(
            URL.create(
                driver, username=user, password=password,
                host=os.environ["USER_BACKEND_DB_HOST"],
                port=int(os.environ["USER_BACKEND_DB_PORT"]),
                database=os.environ["USER_BACKEND_DB_NAME"],
            ),
            schema if driver == "postgresql+asyncpg" else None,
            os.environ["USER_BACKEND_IDENTITY_URL"].rstrip("/"),
            os.getenv("USER_BACKEND_MANAGER_URL", "").rstrip("/"),
            os.getenv("USER_BACKEND_DEFAULT_GATEWAY_HTTP", ""),
            os.getenv("USER_BACKEND_DEFAULT_GATEWAY_WS", ""),
        )


class ActiveCluster(BaseModel):
    jiuwenclaw_id: str = Field(min_length=1, max_length=64)


class _ProxyCookiePolicy(DefaultCookiePolicy):
    """Keep upstream cookies scoped to the browser that received them."""

    def set_ok(self, cookie: Cookie, request: CookieRequest) -> bool:
        return False


def _headers(headers: httpx.Headers | Headers, exclude: set[str]) -> list[tuple[str, str]]:
    connection_fields = {
        part.strip().lower() for part in headers.get("connection", "").split(",")
    }
    raw = headers.multi_items() if isinstance(headers, httpx.Headers) else headers.items()
    return [
        (name, value) for name, value in raw
        if name.lower() not in _HOP_HEADERS | exclude | connection_fields
    ]


def _target(origin: str, path: str, query: bytes) -> httpx.URL:
    if any(part in {".", ".."} for part in path.split("/")) or "\\" in path:
        raise HTTPException(400, "invalid upstream path")
    url = httpx.URL(origin.rstrip("/") + "/" + quote(path.lstrip("/"), safe="/"))
    if not query:
        return url
    # Preserve query delimiters and existing percent escapes while encoding raw bytes.
    encoded_query = quote_from_bytes(query, safe=punctuation)
    return url.copy_with(query=encoded_query.encode("ascii"))


def _scope(request: Request | WebSocket, name: str, body: dict | None = None) -> str:
    values = set(filter(None, [
        *request.headers.getlist(_SCOPE_FIELDS[name]),
        *request.query_params.getlist(name),
        (body or {}).get(name) if isinstance((body or {}).get(name), str) else None,
    ]))
    if len(values) > 1:
        raise HTTPException(403, "conflicting request identity or Agent scope")
    return next(iter(values), "")


def _gateway_origin(raw: str) -> str:
    origin = raw.strip()
    if origin.lower().startswith(("ws://", "wss://")):
        origin = "http" + origin[2:]
    if origin and "://" not in origin:
        origin = "http://" + origin
    parts = urlsplit(origin)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise HTTPException(502, "gateway address unavailable")
    if parts.username is not None or parts.password is not None:
        raise HTTPException(502, "gateway address unavailable")
    if parts.query or parts.fragment or parts.path not in {"", "/"}:
        raise HTTPException(502, "gateway address unavailable")
    return origin.rstrip("/")


def _allowed_identity_path(path: str) -> bool:
    return path in {
        "/v1/auth/token", "/v1/auth/refresh", "/v1/auth/logout",
        "/v1/auth/me", "/v1/auth/me/orgs", "/v1/auth/public_key",
    } or path.startswith("/v1/auth/federation/")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create the enterprise backend independently of the personal Web server."""
    if os.getenv("JIUWENSWARM_EDITION", "").strip().lower() != "enterprise":
        raise RuntimeError("The enterprise user backend requires JIUWENSWARM_EDITION=enterprise")

    cfg = settings or Settings.from_env()
    catalog = Catalog(cfg.schema)
    db = create_async_engine(
        cfg.database, pool_size=5, max_overflow=5, pool_timeout=5, pool_pre_ping=True,
    )
    ordinary = httpx.Timeout(15, connect=5)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Identity callbacks can carry one-time codes in the URL.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        async with (
            httpx.AsyncClient(timeout=ordinary, trust_env=False) as identity,
            httpx.AsyncClient(
                timeout=httpx.Timeout(3600, connect=5, pool=5),
                trust_env=False,
                cookies=CookieJar(policy=_ProxyCookiePolicy()),
            ) as upstream,
        ):
            app.state.identity = identity
            app.state.upstream = upstream
            try:
                yield
            finally:
                await db.dispose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/ready")
    async def ready():
        try:
            async with asyncio.timeout(5), db.connect() as connection:
                for view in catalog.tables():
                    await connection.execute(select(view).limit(0))
            response = await app.state.identity.get(f"{cfg.identity}/v1/auth/public_key")
            response.raise_for_status()
        except (SQLAlchemyError, httpx.HTTPError, TimeoutError):
            raise HTTPException(503, "user backend dependencies unavailable") from None
        return {"status": "ok"}

    async def current_user(request: Request | WebSocket) -> User:
        authority = request.headers.get("authorization")
        if not authority:
            token = unquote(request.cookies.get("openjiuwen_user_access_token", ""))
            authority = f"Bearer {token}" if token else ""
        if not authority:
            raise HTTPException(401, "not authenticated")
        try:
            response = await app.state.identity.get(
                f"{cfg.identity}/v1/auth/user-session",
                headers={"Authorization": authority, "X-Jiuwen-Web-Surface": "user"},
            )
        except httpx.RequestError:
            raise HTTPException(503, "identity service unavailable") from None
        if response.status_code in (401, 403):
            raise HTTPException(response.status_code, "user session denied")
        try:
            response.raise_for_status()
            body = response.json()
            user_id = body["user_id"]
            orgs = {
                item["group_id"]: item.get("display_name") or item["group_id"]
                for item in body["orgs"] if item["group_id"] != "__none__"
            }
            if not isinstance(user_id, str) or not user_id:
                raise ValueError
        except (ValueError, KeyError, TypeError, httpx.HTTPError):
            raise HTTPException(503, "identity service unavailable") from None
        return User(user_id, orgs)

    async def resolve(request: Request | WebSocket, user: User, bot="", group="", jid=""):
        selected = jid or request.cookies.get(_CLUSTER_COOKIE, "")
        try:
            async with asyncio.timeout(5), db.connect() as connection:
                row = await catalog.resolve(connection, user, selected, bot, group)
        except (SQLAlchemyError, TimeoutError):
            raise HTTPException(503, "user access data unavailable") from None
        if row is None:
            raise HTTPException(403, "Agent or instance not admitted")
        return row

    console = APIRouter(prefix=_CONTEXT)

    @console.get("/agent-contexts")
    async def contexts(request: Request):
        user = await current_user(request)
        try:
            async with asyncio.timeout(5), db.connect() as connection:
                items, matched = await catalog.contexts(connection, user)
        except (SQLAlchemyError, TimeoutError):
            raise HTTPException(503, "user access data unavailable") from None
        empty_reason = None
        if not items:
            empty_reason = "no_agent_template" if matched else "no_grant"
        return {
            "code": 200, "message": "success",
            "data": {"contexts": items, "empty_reason": empty_reason},
        }

    @console.post("/active-cluster")
    async def active_cluster(body: ActiveCluster, request: Request, response: Response):
        user = await current_user(request)
        jid = body.jiuwenclaw_id.strip()
        if not jid:
            raise HTTPException(422, "instance required")
        await resolve(request, user, jid=jid)
        response.set_cookie(_CLUSTER_COOKIE, jid, httponly=True, samesite="lax",
                            max_age=30 * 24 * 60 * 60)
        return {"code": 200, "message": "success", "data": {"jiuwenclaw_id": jid}}

    @console.delete("/active-cluster")
    async def clear_cluster(response: Response):
        response.delete_cookie(_CLUSTER_COOKIE, httponly=True, samesite="lax")
        return {"code": 200, "message": "success", "data": {"cleared": True}}

    async def proxy(request: Request, origin: str, path: str, *, surface=False, user=None,
                    body: bytes | None = None):
        headers = _headers(request.headers, {"host", "x-jiuwen-web-surface"})
        if surface:
            headers.append(("x-jiuwen-web-surface", "user"))
        if user is not None:
            headers = [(name, value) for name, value in headers if name != "x-user-id"]
            headers.append(("x-user-id", user.user_id))
        content = body
        if content is None and (
            request.headers.get("content-length") or request.headers.get("transfer-encoding")
        ):
            content = request.stream()
        message = app.state.upstream.build_request(
            request.method, _target(origin, path, request.scope.get("query_string", b"")),
            headers=headers, content=content,
        )
        try:
            response = await app.state.upstream.send(message, stream=True)
        except httpx.TimeoutException:
            raise HTTPException(504, "upstream timed out") from None
        except httpx.RequestError:
            raise HTTPException(502, "upstream unavailable") from None

        async def stream() -> AsyncIterator[bytes]:
            try:
                async for chunk in response.aiter_raw():
                    yield chunk
            finally:
                await response.aclose()

        result = StreamingResponse(stream(), response.status_code)
        result.raw_headers = [
            (name.encode("latin-1"), value.encode("latin-1"))
            for name, value in _headers(response.headers, set())
        ]
        return result

    @app.api_route("/idp/{path:path}", methods=_METHODS)
    async def identity_proxy(request: Request, path: str):
        path = "/" + path
        if not _allowed_identity_path(path):
            raise HTTPException(404)
        return await proxy(request, cfg.identity, path, surface=True)

    @console.api_route("/{path:path}", methods=_METHODS)
    async def manager_workflow(request: Request, path: str):
        if (
            path.startswith(("approvals/", "skills/upload-requests/"))
            or path in {"workspace/expand-requests", "skills/upload-requests"}
        ) and cfg.manager:
            # Manager keeps the existing approval APIs and authenticates them as before.
            return await proxy(request, cfg.manager, _CONTEXT + "/" + path)
        raise HTTPException(404)

    app.include_router(console)

    @app.api_route("/{path:path}", methods=_METHODS)
    async def gateway_proxy(request: Request, path: str):
        root, _, _ = path.partition("/")
        if root not in {"api", "gateway-api", "file-api", "share-api"}:
            raise HTTPException(404)
        if root == "gateway-api":
            path = "api" + path[len(root):]
        user = await current_user(request)
        payload = None
        content = None
        if request.headers.get("content-type", "").split(";", 1)[0] == "application/json":
            content = await request.body()
            if content:
                try:
                    payload = await request.json()
                except ValueError:
                    raise HTTPException(400, "invalid JSON request") from None
                if not isinstance(payload, dict):
                    payload = None
        if _scope(request, "user_id", payload) not in {"", user.user_id}:
            raise HTTPException(403, "user mismatch")
        bot = _scope(request, "bot_id", payload)
        group = _scope(request, "group_id", payload)
        if (path == "api/v1/sessions" or path.startswith(
            ("api/v1/chat/", "api/v1/sessions/")
        )) and not bot:
            raise HTTPException(403, "Agent scope required")
        row = await resolve(request, user, bot, group)
        return await proxy(
            request, _gateway_origin(row.gateway_http or cfg.default_http), path, user=user,
            body=content,
        )

    @app.websocket("/ws")
    @app.websocket("/ws/{path:path}")
    async def websocket_proxy(websocket: WebSocket, path: str = ""):
        try:
            user = await current_user(websocket)
            if _scope(websocket, "user_id") not in {"", user.user_id}:
                raise HTTPException(403, "user mismatch")
            bot = _scope(websocket, "bot_id")
            group = _scope(websocket, "group_id")
            if path != "git" and not bot:
                raise HTTPException(403, "Agent scope required")
            row = await resolve(websocket, user, bot, group)
            origin = _gateway_origin(row.gateway_ws or cfg.default_ws)
            query = websocket.scope.get("query_string", b"")
            url = _target(origin, "ws/" + path if path else "ws", query)
            url = url.copy_with(scheme="wss" if url.scheme == "https" else "ws")
            headers = []
            for name, value in _headers(websocket.headers, {"host", "x-user-id"}):
                if not name.startswith("sec-websocket-"):
                    headers.append((name, value))
            headers.append(("x-user-id", user.user_id))
            async with connect_ws(str(url), extra_headers=headers, max_size=None) as upstream:
                await websocket.accept()

                async def upload():
                    while True:
                        message = await websocket.receive()
                        if message["type"] == "websocket.disconnect":
                            break
                        try:
                            fresh_user = await current_user(websocket)
                            await resolve(websocket, fresh_user, bot, group)
                        except HTTPException:
                            return
                        data = message.get("text")
                        await upstream.send(data if data is not None else message["bytes"])

                async def download():
                    async for data in upstream:
                        if isinstance(data, str):
                            await websocket.send_text(data)
                        else:
                            await websocket.send_bytes(data)

                tasks = [asyncio.create_task(upload()), asyncio.create_task(download())]
                try:
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    await websocket.close()
        except (HTTPException, WebSocketException, OSError, WebSocketDisconnect):
            if websocket.application_state.name == "CONNECTING":
                await websocket.close(code=1008)

    return app
