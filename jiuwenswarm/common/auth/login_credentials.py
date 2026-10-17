# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""登录模型的凭据：模型配置里只放句柄，真正发请求时才换成 id_token。

**为什么不直接把 id_token 放进模型配置。** id_token 一小时就换一次，而模型配置一旦
交出去就收不回来：

- 集群模式的团队运行时跨请求常驻，成员的模型客户端在创建时就定了 api_key。追问走
  ``interact`` 直接投递给已经在跑的团队，不会重建成员——token 过期后它们只会 401。
  SDK 的 ``update_model_pool`` 也只影响之后新建的成员。
- SDK 按 ``(api_key, api_base, …)`` 缓存 HTTP 客户端，token 每换一次就多一个客户端。

所以模型配置里的 api_key 是一个**稳定的占位值** ``jiuwen-login:<ref>``，``ref`` 是
Gateway 按登录账号算出的句柄（见 :func:`credential_ref_for_user`，重新登录前后不变）。
每个带凭据的请求到达 AgentServer 时，把最新的 id_token 登记到本进程的表里；SDK 的
httpx 请求钩子在请求发出前按句柄查表，换成真 token。在跑的成员下一次调模型就自动
用上新 token，配置本身从头到尾不变。

顺带的好处是 token 不进任何配置对象（团队 spec、会话记住的上次模型、日志里的配置都只有句柄）。
已知限制：同一个对话里换账号不行，成员拿的还是上一个账号的句柄，新开对话即可。

**钩子的安全规则。** 登记表按句柄隔离用户；钩子只在目标地址落在登记时的接入点之下才替换，
否则删掉 Authorization 头再放行——占位值不发出去，APIG 回 401，上层据此提示重新登录。

**没有新请求时的续期。** 集群可能很久没有新请求，而 AgentServer 只在收到请求时拿到新 token。
所以它定时扫登记表：最近真的在用、且快过期的句柄，经 server_push 请 Gateway 续期，Gateway 用
``auth.credentials.update`` 推回新 token（会话已注销则推回撤销）。由 AgentServer 发起是因为
只有它知道谁还在调模型——用户走了就不再续；续不了只是退回"不续"。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable, Mapping
from urllib.parse import urlsplit

from jiuwenswarm.common.e2a.constants import E2A_MODEL_AUTH_PARAM_KEY

logger = logging.getLogger(__name__)

#: 模型配置里 api_key 占位值的前缀。SDK 会原样拼成 ``Authorization: Bearer <占位值>``。
PLACEHOLDER_PREFIX = "jiuwen-login:"

#: 登记超过这么久没被刷新就清掉。id_token一小时有效，留两倍余量
REGISTRATION_TTL_S = 2 * 60 * 60

#: 离过期不到这么久就请 Gateway 续期。留足余量：一次续期失败后还有好几轮重试的机会。
REFRESH_AHEAD_S = 15 * 60
#: 这么久之内用过（钩子替换过/有请求带来凭据）才算"还在跑"。要盖过一次长工具调用，期间模型空闲的时间，否则成员刚好在跑长命令时 token 过期，回来就 401。
ACTIVE_WINDOW_S = 20 * 60
#: 同一个句柄两次续期请求的最小间隔：Gateway 那边续期失败时，按这个节奏重试。
REFRESH_RETRY_S = 2 * 60
#: 扫描间隔。
SWEEP_INTERVAL_S = 60

#: AgentServer → Gateway 的续期请求（server_push 的 ``payload.event_type``）。
CREDENTIAL_REFRESH_EVENT = "auth.credential.refresh"

_REF_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_BEARER = "bearer "


@dataclass(frozen=True)
class LoginAuth:
    """一次请求带下来的登录凭据，已经登记过。

    ``api_key`` 是占位值，拿去建模型；真 token 只在登记表里。
    """

    api_base: str
    api_key: str
    ref: str


@dataclass(frozen=True)
class _Registration:
    api_base: str
    token: str
    updated_at: float
    expires_at: float | None
    last_used_at: float
    refresh_requested_at: float = 0.0


_registry: dict[str, _Registration] = {}
_lock = threading.Lock()


#: 最近一次请求用的是免费模型的对话会话 → 记下的时间。错误分类据此判断一句
#: ``Error code: 429`` 是免费额度问题还是用户自己服务商的问题。
_login_model_sessions: dict[str, float] = {}
#: 集群可以在最后一次请求之后自己跑很久，标记要留得比凭据登记更久。
LOGIN_MODEL_SESSION_TTL_S = 24 * 60 * 60


def note_session_model(session_id: str | None, *, uses_login_model: bool) -> None:
    """AgentServer 每个请求都记一次：这个对话会话现在用的是不是免费模型。"""
    if not session_id:
        return
    now = time.time()
    with _lock:
        if uses_login_model:
            _login_model_sessions[session_id] = now
        else:
            _login_model_sessions.pop(session_id, None)
        stale = [key for key, at in _login_model_sessions.items() if now - at > LOGIN_MODEL_SESSION_TTL_S]
        for key in stale:
            del _login_model_sessions[key]


def session_uses_login_model(session_id: str | None) -> bool:
    if not session_id:
        return False
    with _lock:
        noted_at = _login_model_sessions.get(session_id)
    return noted_at is not None and time.time() - noted_at <= LOGIN_MODEL_SESSION_TTL_S


def credential_ref_for_user(user_id: str) -> str:
    """Gateway 侧：登录账号（openid）→ 句柄。

    **按账号而不是按登录会话。** 集群成员的模型在建团队时就定了句柄；重新登录会换一个
    新的会话 id，句柄若跟着会话走，在跑的团队就永远等不到新 token——重新登录都救不回来。
    会话存储里同一账号只保留一个会话（重复登录会顶掉旧的），所以按账号取句柄，登录
    前后是同一个，新 token 登记进去，在跑的成员下一次调用就恢复
    """
    digest = hmac.new(_ref_key(), f"jiuwen-login-credential:{user_id}".encode("utf-8"), hashlib.sha256)
    return digest.hexdigest()[:32]


_ref_key_cache: bytes | None = None


def _ref_key() -> bytes:
    """句柄用的 HMAC 密钥：会话存储的主密钥，进程内只取一次"""
    global _ref_key_cache
    if _ref_key_cache is None:
        from jiuwenswarm.common.auth.session_store import derive_local_secret

        # 派生一把专用的，不直接拿加密密钥当 HMAC 密钥
        _ref_key_cache = derive_local_secret(b"jiuwen-login-credential-ref:")
    return _ref_key_cache


def is_credential_ref(value: str) -> bool:
    return bool(_REF_PATTERN.match(value or ""))


def placeholder_api_key(ref: str) -> str:
    return f"{PLACEHOLDER_PREFIX}{ref}"


def register(ref: str, api_base: str, token: str) -> None:
    """登记/刷新句柄对应的 token。顺手清掉太久没刷新的登记。

    有请求带着凭据来，本身就说明这个句柄在用，所以同时记一次使用时间。
    """
    now = time.time()
    with _lock:
        _registry[ref] = _Registration(
            api_base=api_base,
            token=token,
            updated_at=now,
            expires_at=_token_expiry(token),
            last_used_at=now,
        )
        stale = [key for key, value in _registry.items() if now - value.updated_at > REGISTRATION_TTL_S]
        for key in stale:
            del _registry[key]


def update_token(ref: str, token: str) -> bool:
    """Gateway 续期后推回来的新 token。只更新**已登记**的句柄，接入点不变。

    不接受推送新建登记：接入点只能来自请求带下来的那份，推送通道改不了token发往哪里。
    """
    now = time.time()
    with _lock:
        current = _registry.get(ref)
        if current is None:
            return False
        _registry[ref] = replace(
            current,
            token=token,
            updated_at=now,
            expires_at=_token_expiry(token),
            refresh_requested_at=0.0,
        )
    return True


def unregister(ref: str) -> None:
    """登录会话已注销：立刻停用这个句柄，不必等 token 自然过期。"""
    with _lock:
        _registry.pop(ref, None)


def apply_credential_update(params: Mapping[str, Any] | None) -> bool:
    if not isinstance(params, Mapping):
        return False
    ref = str(params.get("credential_ref") or "").strip()
    if not _REF_PATTERN.match(ref):
        return False
    if params.get("revoked") is True:
        unregister(ref)
        logger.info("[LoginCredential] 登录会话已注销，停用凭据 ref=...%s", ref[-8:])
        return True
    token = str(params.get("api_key") or "").strip()
    if not token:
        return False
    updated = update_token(ref, token)
    if updated:
        logger.info("[LoginCredential] 凭据已续期 ref=...%s", ref[-8:])
    return updated


def refs_due_for_refresh(now: float | None = None) -> list[str]:
    """该请 Gateway 续期的句柄：最近在用、快过期、且距上次请求已超过重试间隔。

    选中的同时记下请求时间，下一轮扫描不会重复要。
    """
    now = time.time() if now is None else now
    due: list[str] = []
    with _lock:
        for ref, registration in _registry.items():
            if registration.expires_at is None:
                continue
            if registration.expires_at - now > REFRESH_AHEAD_S:
                continue
            if now - registration.last_used_at > ACTIVE_WINDOW_S:
                continue
            if now - registration.refresh_requested_at < REFRESH_RETRY_S:
                continue
            _registry[ref] = replace(registration, refresh_requested_at=now)
            due.append(ref)
    return due


async def run_refresh_requests(
    send_push: Callable[[dict[str, Any]], Awaitable[bool]],
    *,
    interval_s: float = SWEEP_INTERVAL_S,
) -> None:
    """AgentServer 后台任务：定时扫描，给快过期的在用句柄发续期请求。任务被取消才退出。"""
    while True:
        await asyncio.sleep(interval_s)
        try:
            due = refs_due_for_refresh()
        except Exception:  # noqa: BLE001 — 这一轮扫描出错不能让任务退出，否则之后再也不续期
            logger.warning("[LoginCredential] 扫描凭据表失败，下一轮再试", exc_info=True)
            continue
        for ref in due:
            try:
                delivered = await send_push(build_refresh_request(ref))
            except Exception:  # noqa: BLE001 — 发不出去就等下一轮
                logger.warning("[LoginCredential] 续期请求发送失败 ref=...%s", ref[-8:], exc_info=True)
                continue
            if not delivered:
                logger.warning("[LoginCredential] 续期请求未送达 Gateway ref=...%s", ref[-8:])


def build_refresh_request(ref: str) -> dict[str, Any]:
    """续期请求的 server_push 消息。只带句柄：Gateway 自己按句柄找会话。"""
    return {
        "request_id": f"auth-credential-refresh-{ref[-8:]}-{int(time.time())}",
        "channel_id": "",
        "payload": {"event_type": CREDENTIAL_REFRESH_EVENT, "credential_ref": ref},
    }


def _token_expiry(token: str) -> float | None:
    """读JWT的 ``exp``，只用来决定什么时候续期，**不验签**——验签是APIG认证器做的"""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        segment = parts[1]
        payload = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
        exp = payload.get("exp") if isinstance(payload, dict) else None
        return float(exp) if isinstance(exp, (int, float)) else None
    except (ValueError, TypeError):
        return None


def _lookup(ref: str) -> _Registration | None:
    with _lock:
        registration = _registry.get(ref)
    if registration is None or time.time() - registration.updated_at > REGISTRATION_TTL_S:
        return None
    return registration


def login_auth_from_params(params: Mapping[str, Any] | None) -> LoginAuth | None:
    """AgentServer 侧：读 Gateway 随请求带下来的凭据，登记后返回占位形态；没带返回 ``None``"""
    if not isinstance(params, Mapping):
        return None
    auth = params.get(E2A_MODEL_AUTH_PARAM_KEY)
    if not isinstance(auth, Mapping):
        return None
    api_base = str(auth.get("api_base") or "").strip()
    token = str(auth.get("api_key") or "").strip()
    ref = str(auth.get("credential_ref") or "").strip()
    if not api_base or not token or not _REF_PATTERN.match(ref):
        return None
    register(ref, api_base, token)
    return LoginAuth(api_base=api_base, api_key=placeholder_api_key(ref), ref=ref)


def build_login_model_entry(params: Mapping[str, Any] | None, model_name: str) -> dict[str, Any] | None:
    """请求带了登录凭据时，按请求的模型名造一个占位凭据的模型条目；否则 ``None``。

    单 agent 和集群装配共用这一个入口，保证两边拿到的条目形状一致。
    """
    bare_name = bare_model_name(model_name)
    if not bare_name:
        return None
    login_auth = login_auth_from_params(params)
    if login_auth is None:
        return None
    from jiuwenswarm.common.auth.model_catalog import build_model_entry

    return build_model_entry(model_name=bare_name, api_base=login_auth.api_base, api_key=login_auth.api_key)


def bare_model_name(model_name: str | None) -> str:
    name = str(model_name or "").strip()
    return name.rsplit("#", 1)[0] if "#" in name else name


# SDK请求钩子
_HOOK_MARKER = "_jiuwen_login_credential_hook"


def apply_login_credential_patch() -> bool:
    """给 SDK 的 OpenAI 客户端挂上凭据替换钩子。幂等；SDK 结构对不上时返回 ``False``。

    SDK 建 httpx 客户端时本来就带 ``event_hooks``（它自己用来在 ``auth_mode=none``
    时剥掉 Authorization）。这里包一层 ``_build_async_openai_client``，往请求钩子列表
    末尾追加一步，没有占位值的请求不受影响。
    """
    try:
        from openjiuwen.core.foundation.llm.model_clients.openai_model_client import (
            OpenAIModelClient,
        )
    except Exception:  # noqa: BLE001 — SDK 结构变了就不打补丁，别影响启动
        logger.warning("[LoginCredential] 找不到 OpenAIModelClient，凭据钩子未安装", exc_info=True)
        return False

    build_client_attr = "_build_async_openai_client"
    original = getattr(OpenAIModelClient, build_client_attr, None)
    if original is None:
        logger.warning("[LoginCredential] SDK 没有 %s，凭据钩子未安装", build_client_attr)
        return False
    if getattr(original, _HOOK_MARKER, False):
        return True

    def _build_with_credential_hook(self: Any, *args: Any, **kwargs: Any) -> Any:
        client = original(self, *args, **kwargs)
        try:
            hooks = getattr(getattr(client, "_client", None), "event_hooks", None)
            if isinstance(hooks, dict):
                # 追加而不是替换：SDK 自己的钩子要保留
                hooks.setdefault("request", []).append(inject_login_credential)
        except Exception:  # noqa: BLE001 — 挂不上时占位值会被 APIG 拒掉，401 能暴露问题
            logger.warning("[LoginCredential] 凭据钩子挂载失败", exc_info=True)
        return client

    setattr(_build_with_credential_hook, _HOOK_MARKER, True)
    setattr(OpenAIModelClient, build_client_attr, _build_with_credential_hook)
    logger.info("[LoginCredential] 凭据钩子已安装")
    return True


async def inject_login_credential(request: Any) -> None:
    """httpx 请求钩子：把占位值换成登记表里的 id_token。

    这个钩子装在所有 OpenAI 客户端上，没有占位值的请求比如：用户自配模型等，必须原样放行。
    """
    authorization = request.headers.get("Authorization") or ""
    if not authorization.lower().startswith(_BEARER):
        return
    value = authorization[len(_BEARER):].strip()
    if not value.startswith(PLACEHOLDER_PREFIX):
        return
    ref = value[len(PLACEHOLDER_PREFIX):]

    registration = _lookup(ref)
    if registration is None:
        # 无论如何占位值都不发出去
        del request.headers["Authorization"]
        logger.warning("[LoginCredential] 登录凭据已失效（长时间没有新请求刷新），请求将以未认证发出")
        return
    # 只发给登记时的接入点。AgentServer 的 WS 默认不鉴权，本机进程能自己塞一份 _model_auth、
    # 让模型指向任意主机——不查地址的话，别人的句柄就能把真 token 引到那台主机上。
    if not _targets(str(request.url), registration.api_base):
        del request.headers["Authorization"]
        logger.warning(
            "[LoginCredential] 请求地址不在登录接入点下，拒绝附带凭据: %s", urlsplit(str(request.url)).netloc
        )
        return
    request.headers["Authorization"] = f"Bearer {registration.token}"
    _mark_used(ref)


def _mark_used(ref: str) -> None:
    """钩子真正替换过才算在用：地址对不上、句柄无效的请求不能让句柄一直被续期。"""
    now = time.time()
    with _lock:
        current = _registry.get(ref)
        if current is not None:
            _registry[ref] = replace(current, last_used_at=now)


def _targets(url: str, api_base: str) -> bool:
    """``url`` 是否落在 ``api_base`` 下：协议、主机（含端口）一致，路径在其之下。"""
    target, base = urlsplit(url), urlsplit(api_base)
    if (target.scheme.lower(), target.netloc.lower()) != (base.scheme.lower(), base.netloc.lower()):
        return False
    prefix = base.path.rstrip("/")
    return not prefix or target.path == prefix or target.path.startswith(prefix + "/")


def reset_for_test() -> None:
    with _lock:
        _registry.clear()
        _login_model_sessions.clear()
