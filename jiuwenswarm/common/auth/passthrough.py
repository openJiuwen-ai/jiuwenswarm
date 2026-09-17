# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""把登录模型的凭据随请求带给 AgentServer。

积分按用户（openid）结算，AgentServer 必须知道"这次请求是谁"，磁盘上那份会话回答不了
（它只在"整台机器一个登录用户"时才成立）。所以由 Gateway 在转发前挂上该用户的 id_token；
参数名以 ``_`` 开头，是两个进程之间的内部约定。

硬规则：:func:`normalize_model_auth` **无条件先删掉客户端传来的这个键**，再决定要不要由
服务端填——否则前端自己塞一个就能指定"用哪个地址、带哪个头"。
"""

from __future__ import annotations

import logging
from typing import Any

from jiuwenswarm.common.e2a.constants import E2A_MODEL_AUTH_PARAM_KEY

logger = logging.getLogger(__name__)

#: 请求参数里承载模型鉴权的键。Gateway 写、AgentServer 读，前端不可见也不可写。
#: 定义在 e2a.constants 里——它是两个进程之间的线上约定，不属于 Gateway 私有。
MODEL_AUTH_PARAM_KEY = E2A_MODEL_AUTH_PARAM_KEY


def normalize_model_auth(params: dict[str, Any], session_id: str | None = None) -> None:
    """就地处理 ``params``：先清客户端传来的凭据键，再按需由服务端填上。

    只在请求用的是登录送的模型时才填：用户自配模型、没登录时，凭据本来就在 config.yaml
    或进程内存里，AgentServer 自己有。

    Args:
        params: 入站请求参数，**会被就地修改**。
        session_id: 调用方的登录会话 id。为空表示这个请求没有登录身份，
            此时不填任何凭据（而不是回落到"当前唯一登录用户"——那等于取消
            会话校验，见 ``AuthService.resolve_session`` 的说明）。
    """
    # 无条件先删。客户端传什么都不作数。
    if params.pop(MODEL_AUTH_PARAM_KEY, None) is not None:
        logger.warning(
            "[ModelAuth] 客户端请求里带了 %s，已丢弃（该键只允许服务端写）",
            MODEL_AUTH_PARAM_KEY,
        )

    model_name = str(params.get("model_name") or "").strip()
    if not model_name:
        return

    auth = _build_model_auth(model_name, session_id)
    if auth is not None:
        params[MODEL_AUTH_PARAM_KEY] = auth


def _build_model_auth(model_name: str, session_id: str | None) -> dict[str, Any] | None:
    """登录模型 → ``{api_base, api_key, credential_ref}``（api_key 就是 id_token）；
    不是登录模型或拿不到凭据返回 ``None``。

    ``credential_ref`` 是这个登录账号的句柄：AgentServer 把 id_token 按它登记，模型配置里
    只放句柄，发请求时再换成真 token（见 ``login_credentials``）。
    """
    from jiuwenswarm.common.auth.apig import resolve_apig_config
    from jiuwenswarm.common.auth.login_credentials import bare_model_name, credential_ref_for_user
    from jiuwenswarm.common.auth.model_catalog import get_models
    from jiuwenswarm.common.auth.service import ModelAuthRequired, live_session

    if not session_id:
        return None
    apig_config = resolve_apig_config(allow_refresh=False)
    if apig_config is None:
        return None

    try:
        # 请求的模型是不是登录来源？名字带 #index 后缀时取前半段（通道侧的全局序号）。
        bare_name = bare_model_name(model_name)
        # allow_refresh=False：这里在 Gateway 的转发热路径上，不能为了刷模型目录
        # 去发一次同步 HTTP。缓存里没有就当作不是登录模型。
        known = {model.model_name for model in get_models(session_id, allow_refresh=False)}
        if bare_name not in known and model_name not in known:
            return None
    except Exception:  # noqa: BLE001 — 目录读不到就当作不是登录模型
        logger.debug("[ModelAuth] 读取登录模型目录失败", exc_info=True)
        return None

    try:
        # 不在这里同步续期：续期是同步 HTTP，会卡住事件循环。快过期的 token会在后台续好，这一次先用旧的。
        session = live_session(session_id, allow_refresh=False)
    except ModelAuthRequired as exc:
        # 拿不到凭据不在这里报错：AgentServer 侧解析模型时会走到同样的判断并给出
        # 面向用户的提示。这里硬失败只会把"登录过期"变成一个语焉不详的转发错误。
        logger.info("[ModelAuth] 暂时取不到 %s 的登录凭据: %s", model_name, exc)
        return None
    return {
        "api_base": apig_config.invoke_base_url,
        "api_key": session.credential.id_token,
        "credential_ref": credential_ref_for_user(session.user_id),
    }


def refreshed_credential_for_ref(credential_ref: str) -> dict[str, Any] | None:
    """AgentServer 请求续期时调用：按句柄找到登录会话，续期后返回要推回去的参数。

    - 续好了（或还没到需要续的时候）→ ``{credential_ref, api_key}``
    - 会话已注销 / 凭据过期且续不了 → ``{credential_ref, revoked: True}``，让 AgentServer
      立刻停用，而不是拿着一个没用的 token 等到自然过期
    - 续期暂时失败但旧 token 还能用 → 仍返回旧 token；AgentServer 按自己的重试间隔再来要

    这是同步 HTTP（续期），调用方要放到线程里跑。句柄格式不对返回 ``None``。
    """
    from jiuwenswarm.common.auth.login_credentials import (
        REFRESH_AHEAD_S,
        credential_ref_for_user,
        is_credential_ref,
    )
    from jiuwenswarm.common.auth.service import get_auth_service
    from jiuwenswarm.common.auth.session_store import get_session_store

    if not is_credential_ref(credential_ref):
        return None
    revoked = {"credential_ref": credential_ref, "revoked": True}
    # 句柄是账号的单向 HMAC，只能逐个算出来比对。本机会话数量很少，代价可以忽略。
    # 同一账号重新登录过也能找到：句柄按账号算，找到的是它当前那个会话。
    session = next(
        (s for s in get_session_store().list_sessions() if credential_ref_for_user(s.user_id) == credential_ref),
        None,
    )
    if session is None:
        return revoked
    fresh = get_auth_service().try_refresh(session, ahead_s=REFRESH_AHEAD_S)
    if fresh is None or fresh.credential.is_expired():
        return revoked
    return {"credential_ref": credential_ref, "api_key": fresh.credential.id_token}
