# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway Web HTTP 的登录路由（``/api/v1/auth/*``），华为账号 Account Kit。

====================================  ==========================================
``POST /api/v1/auth/authorize``       生成授权地址（后端做 state / PKCE）
``GET  /api/v1/auth/callback``        华为回调落点：换 token + 建会话 + 下 cookie
``POST /api/v1/auth/claim``           发起方凭 ``{state, claimToken}`` 取回会话
``POST /api/v1/auth/cancel``          发起方凭 ``{state, claimToken}`` 放弃这次登录
``GET  /api/v1/auth/status``          当前登录状态（不含任何凭据）
``POST /api/v1/auth/logout``          清会话
``GET  /api/v1/auth/models``          登录后可用的模型
``GET  /api/v1/auth/quota``           免费模型额度（走 APIG）
====================================  ==========================================

``/callback`` 只在"回调落本机"的形态下用到（AGC 登记的 ``redirect_uri``）：浏览器直接访问，
返回我们自己的落地页而不是 JSON。落地页经 ``BroadcastChannel`` 通知同源的应用标签页并自行
关闭；通知不到的（不同源、桌面端开在系统浏览器里），由应用在窗口重新获得焦点时去 ``/claim``。
生产形态下回调落 ECS，这个路由用不到。

发起登录 / 认领 / 取消 / 登出这几个 POST 必须带 ``X-Jiuwen-Auth: 1``（见 :data:`AUTH_REQUEST_HEADER`）。

会话 id 同时接受 cookie（``jiuwenswarm_auth``）和 ``X-Auth-Session`` 头——
浏览器用 cookie，桌面 WebView / TUI 这类拿不到那份 cookie 的前端用请求头。
"""

from __future__ import annotations

import html
import logging
from typing import Annotated

from fastapi import Body, FastAPI, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from jiuwenswarm.common.auth.account_kit import OAuthError
from jiuwenswarm.common.auth.model_catalog import (
    get_models,
    model_whitelist,
)
from jiuwenswarm.common.auth.service import ModelAuthRequired, get_auth_service, resolve_id_token
from jiuwenswarm.common.auth.session_store import AuthSession

logger = logging.getLogger(__name__)

_OPENAPI_TAG = "auth"
AUTH_SESSION_COOKIE = "jiuwenswarm_auth"
AUTH_SESSION_HEADER = "x-auth-session"
#: 回调落地页广播给应用标签页的频道名和消息类型，前端按它们识别（authClient.ts）。
AUTH_CALLBACK_CHANNEL = "jiuwenswarm:auth"
AUTH_CALLBACK_MESSAGE = "jiuwenswarm:auth-callback"
#: 发起登录 / 认领 / 登出必须带的请求头（值为 ``1``）。
#:
#: Gateway 监听在本机端口上，用户浏览的任何网页都能对它发"简单请求"——不带自定义头的
#: POST 不走 CORS 预检，浏览器照发不误，只是读不到响应。``/authorize`` 不需要 cookie，
#: SameSite 挡不住：不设防的话，一个网页就能不停地往内存里塞待登录记录。要求自定义头之后，
#: 跨站请求必须先过预检，而 Gateway 不开 CORS，预检不会通过。
AUTH_REQUEST_HEADER = "x-jiuwen-auth"
# cookie 存活时间：比 id_token（1h）长得多，续期失败时由 status 接口判定为未登录。
_COOKIE_MAX_AGE_S = 30 * 24 * 60 * 60


class AuthClaimBody(BaseModel):
    state: str = Field(..., description="发起授权时返回的 state")
    claimToken: str = Field(..., description="发起授权时返回的 claimToken")  # noqa: N815 — 前端字段名


class AuthCallbackQuery(BaseModel):
    """华为账号授权回调的 query 参数。"""

    state: str = ""
    code: str = ""
    error: str = ""
    error_description: str = ""
    sub_error: str = ""


def resolve_session_id(request: Request) -> str | None:
    """``X-Auth-Session`` 头优先，其次 cookie。"""
    header = (request.headers.get(AUTH_SESSION_HEADER) or "").strip()
    if header:
        return header
    cookie = (request.cookies.get(AUTH_SESSION_COOKIE) or "").strip()
    return cookie or None


def _error(message: str, code: str, status: int) -> JSONResponse:
    return JSONResponse({"ok": False, "error": {"code": code, "message": message}}, status_code=status)


def _reject_without_auth_header(request: Request) -> JSONResponse | None:
    if request.headers.get(AUTH_REQUEST_HEADER) == "1":
        return None
    return _error("缺少请求头 X-Jiuwen-Auth", "auth_header_required", 403)


def _attach_session(response: Response, session: AuthSession, request: Request) -> None:
    response.set_cookie(
        AUTH_SESSION_COOKIE,
        session.session_id,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
        path="/",
        max_age=_COOKIE_MAX_AGE_S,
    )
    # 拿不到这份cookie的前端（桌面 WebView、TUI）从响应头里取
    response.headers["X-Auth-Session"] = session.session_id


def _landing_page(ok: bool, title: str, detail: str, status: int = 200) -> HTMLResponse:
    """回调落地页，用户在第三方授权页之后看到的唯一页面"""
    auto_close = "setTimeout(function(){window.close();},1200);" if ok else ""
    body = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>
body{{margin:0;font:15px/1.7 system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
background:#f5f7f9;color:#141a21;display:flex;min-height:100vh;align-items:center;justify-content:center}}
main{{max-width:28rem;padding:2.5rem 2rem;text-align:center}}
h1{{font-size:1.25rem;margin:0 0 .5rem}}p{{margin:0;color:#58646f}}
@media (prefers-color-scheme:dark){{body{{background:#101419;color:#e4e9ef}}p{{color:#a0abb7}}}}
</style></head>
<body><main><h1>{html.escape(title)}</h1><p>{html.escape(detail)}</p></main>
<script>
try{{var c=new BroadcastChannel("{AUTH_CALLBACK_CHANNEL}");c.postMessage({{type:"{AUTH_CALLBACK_MESSAGE}",ok:{str(ok).lower()}}});c.close();}}catch(e){{}}
{auto_close}
</script></body></html>"""
    return HTMLResponse(
        body,
        status_code=status,
        # URL里带着授权码，不能进缓存
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


async def _revoke_agent_server_credential(user_id: str) -> None:
    try:
        from jiuwenswarm.common.auth.login_credentials import credential_ref_for_user
        from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler

        handler = MessageHandler.try_get_instance()
        if handler is None:
            return
        await handler.push_login_credential_update(
            {"credential_ref": credential_ref_for_user(user_id), "revoked": True}
        )
    except Exception:  # noqa: BLE001
        logger.warning("[Auth] 登出后撤销 AgentServer 凭据失败", exc_info=True)


def register_auth_routes(app: FastAPI) -> None:
    from jiuwenswarm.common.auth.remote_config import warm_up_in_background

    # 启动时先在后台把官网配置拉下来：否则第一个请求要么等网络、要么拿到空配置
    warm_up_in_background()

    @app.post(
        "/api/v1/auth/authorize",
        tags=[_OPENAPI_TAG],
        summary="发起登录：返回浏览器要打开的华为账号授权地址",
    )
    async def auth_authorize(request: Request) -> Response:
        rejected = _reject_without_auth_header(request)
        if rejected is not None:
            return rejected
        service = get_auth_service()

        def _authorize() -> Response:
            if not service.enabled:
                return _error("登录未开启（拉不到官网配置、活动已结束，或本地显式关闭了）", "login_disabled", 404)
            try:
                return JSONResponse(service.create_authorization_request())
            except OAuthError as error:
                logger.warning("[Auth] 无法发起登录: %s (%s)", error, error.code)
                return _error(str(error), error.code, 503)

        return await run_in_threadpool(_authorize)

    @app.get(
        "/api/v1/auth/callback",
        tags=[_OPENAPI_TAG],
        summary="华为账号授权回调（浏览器访问，返回落地页）",
        response_class=HTMLResponse,
    )
    async def auth_callback(
        request: Request,
        query: Annotated[AuthCallbackQuery, Query()],
    ) -> Response:
        service = get_auth_service()
        if not await run_in_threadpool(lambda: service.enabled):
            return _landing_page(False, "登录未开启", "请回到应用。", 404)
        if query.error:
            # 华为的错误码（如 1201）本身不说明原因，要靠这两个字段才查得下去
            logger.warning(
                "[Auth] 华为回调带错误 error=%s sub_error=%s error_description=%s",
                query.error, query.sub_error or "-", query.error_description or "-",
            )
        try:
            session = await run_in_threadpool(
                service.complete_callback,
                query.state.strip(),
                query.code.strip(),
                query.error.strip(),
                query.error_description.strip(),
            )
        except OAuthError as err:
            logger.warning("[Auth] 回调处理失败: %s (%s)", err, err.code)
            if err.code == "oauth_state_invalid":
                # 不回显细节：这不是任何人发起的登录（或已过期），统一一个中性页面
                return _landing_page(False, "登录链接已失效", "请回到应用重新发起登录。", 400)
            return _landing_page(False, "登录未完成", f"{err}", 400)

        response = _landing_page(True, "华为账号登录成功", "请回到应用继续使用，这个页面可以关闭。")
        _attach_session(response, session, request)
        return response

    @app.post(
        "/api/v1/auth/claim",
        tags=[_OPENAPI_TAG],
        summary="取回登录结果（202=授权还没完成，200=登录成功并下发会话）",
    )
    async def auth_claim(request: Request, body: AuthClaimBody = Body(...)) -> Response:
        rejected = _reject_without_auth_header(request)
        if rejected is not None:
            return rejected
        service = get_auth_service()
        if not await run_in_threadpool(lambda: service.enabled):
            return _error("登录未开启", "login_disabled", 404)
        try:
            # 回调落鉴权服务时会当场去鉴权服务取一次，是网络调用
            session = await run_in_threadpool(service.claim, body.state.strip(), body.claimToken.strip())
        except OAuthError as err:
            return _error(str(err), err.code, 400)
        if session is None:
            return JSONResponse({"ok": True, "pending": True}, status_code=202)
        response = JSONResponse({"ok": True, "islogin": True, **session.public_view()})
        _attach_session(response, session, request)
        return response

    @app.post(
        "/api/v1/auth/cancel",
        tags=[_OPENAPI_TAG],
        summary="放弃这次登录（停止向鉴权服务认领）",
    )
    async def auth_cancel(request: Request, body: AuthClaimBody = Body(...)) -> Response:
        rejected = _reject_without_auth_header(request)
        if rejected is not None:
            return rejected
        # 不区分state存不存在、对不对得上：不给探测留信号
        get_auth_service().cancel(body.state.strip(), body.claimToken.strip())
        return JSONResponse({"ok": True})

    @app.get(
        "/api/v1/auth/status",
        tags=[_OPENAPI_TAG],
        summary="当前登录状态（不含任何凭据）",
    )
    async def auth_status(request: Request) -> Response:
        service = get_auth_service()
        return JSONResponse(await run_in_threadpool(service.status, resolve_session_id(request)))

    @app.post("/api/v1/auth/logout", tags=[_OPENAPI_TAG], summary="退出登录")
    async def auth_logout(request: Request) -> Response:
        rejected = _reject_without_auth_header(request)
        if rejected is not None:
            return rejected
        service = get_auth_service()
        session_id = resolve_session_id(request)
        # 先取账号再登出：句柄按账号算，会话删了就查不到是谁了
        session = service.resolve_session(session_id)
        service.logout(session_id)
        if session is not None:
            await _revoke_agent_server_credential(session.user_id)
        response = JSONResponse({"ok": True, "islogin": False})
        response.delete_cookie(AUTH_SESSION_COOKIE, path="/")
        return response

    @app.get(
        "/api/v1/auth/models",
        tags=[_OPENAPI_TAG],
        summary="登录后自动获得的模型列表",
        description=(
            "模型不是配置出来的，是拿登录凭据向 APIG 发现出来的，本地带缓存。"
            "这些模型同时会并进 `models.list`，和用户自配的模型一起展示。"
        ),
    )
    async def auth_models(request: Request) -> Response:
        session_id = resolve_session_id(request)
        service = get_auth_service()

        def _load() -> tuple[list[dict[str, str]] | None, bool]:
            whitelist_applied = model_whitelist() is not None
            session = service.resolve_session(session_id)
            # 没登录 / 已过期时不能拿缓存充数：这些模型此刻用不了，
            # 报出去会和 models.list（那边已经正确剔掉了）自相矛盾。
            if session is None or service.ensure_fresh(session) is None:
                return None, whitelist_applied
            return [model.to_dict() for model in get_models(session_id)], whitelist_applied

        models, whitelist_applied = await run_in_threadpool(_load)
        return JSONResponse(
            {
                "ok": True,
                "islogin": models is not None,
                "whitelistApplied": whitelist_applied,
                "models": models or [],
            }
        )

    @app.get(
        "/api/v1/auth/quota",
        tags=[_OPENAPI_TAG],
        summary="免费模型的积分使用情况",
        description=(
            "积分就是 LiteLLM 这把 key 的 `max_budget` / `spend`，1:1 透传，不做换算。"
            "积分用完时仍然返回 200 且 `exhausted=true`——用户正是这时最需要看到自己的积分。"
            "未接 APIG 时返回 `available=false`。"
        ),
    )
    async def auth_quota(request: Request) -> Response:
        from jiuwenswarm.common.auth.apig import ApigError, fetch_quota, resolve_apig_config

        config = await run_in_threadpool(resolve_apig_config)
        if config is None:
            # 没接 APIG 不是错误，是"这套部署没有免费额度这回事"。
            # 前端据此整块不展示，而不是显示一个报错。
            return JSONResponse({"ok": True, "available": False, "quota": None})

        # 严格按调用方的会话取：没带会话就是没登录，不能退回「本机唯一登录用户」
        session_id = resolve_session_id(request)
        if get_auth_service().resolve_session(session_id) is None:
            return _error("请先登录", "not_logged_in", 401)
        try:
            quota = await run_in_threadpool(
                lambda: fetch_quota(resolve_id_token(session_id), config)
            )
        except ModelAuthRequired as error:
            return _error(str(error), error.reason, 401)
        except ApigError as error:
            logger.warning("[Auth] 额度查询失败 code=%s: %s", error.code, error)
            status = 401 if error.code == "session_expired" else 502
            return _error(str(error), error.code, status)
        return JSONResponse({"ok": True, "available": True, "quota": quota.public_view()})

    logger.info("[Auth] 已注册 /api/v1/auth/* 登录路由")
