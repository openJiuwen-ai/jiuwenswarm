# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""企业版 preferred_language：AS 每请求 memo + Gateway DB 点查。

个人版不走本模块的 DB 路径，仍读 ``get_config()["preferred_language"]``。
"""

from __future__ import annotations

import contextvars
import logging
from contextlib import asynccontextmanager, contextmanager
from typing import Any, AsyncIterator, Iterator

logger = logging.getLogger(__name__)

DEFAULT_USER_ID = "__default__"

_USER_ID_CV: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "preferred_language_user_id", default=None
)
_MEMO_CV: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "preferred_language_memo", default=None
)


def _normalize(raw: object) -> str | None:
    from jiuwenswarm.common.i18n import try_normalize_preferred_language

    return try_normalize_preferred_language(raw)


def _yaml_or_en() -> str:
    try:
        from jiuwenswarm.common.config import get_config

        lang = _normalize((get_config() or {}).get("preferred_language"))
        if lang:
            return lang
    except Exception:  # noqa: BLE001
        logger.debug("read yaml preferred_language failed", exc_info=True)
    return "en"


async def _db_get_language(user_id: str) -> str | None:
    from jiuwenswarm.enterprise.config.effective import db_queries

    rows = await db_queries.list_records(
        "user_preference",
        filters={"user_id": user_id},
    )
    if not rows:
        return None
    return _normalize(rows[0].get("preferred_language"))


async def resolve_preferred_language_async(user_id: str | None) -> str:
    """用户行 → ``__default__`` → yaml → ``en``（直查 GDB，可写当前请求 memo）。

    若调用方尚未 ``bind_preferred_language``，本函数会创建一次性本地 memo，
    并在返回前 ``reset``，避免常驻 worker / 复用 Task 上 ContextVar 脏缓存。
    已在 bind 作用域内时，继续写入外层 memo，由 bind 的 ``finally`` 清理。
    """
    from jiuwenswarm.enterprise.edition import is_enterprise

    if not is_enterprise():
        return _yaml_or_en()

    memo = _MEMO_CV.get()
    owned_token = None
    if memo is None:
        memo = {}
        owned_token = _MEMO_CV.set(memo)

    try:
        uid = str(user_id or "").strip()
        if uid and uid != DEFAULT_USER_ID:
            if uid in memo:
                return memo[uid]
            try:
                lang = await _db_get_language(uid)
            except Exception:  # noqa: BLE001
                logger.exception("preferred_language db get failed user_id=%s", uid)
                lang = None
            if lang:
                memo[uid] = lang
                return lang

        if DEFAULT_USER_ID in memo:
            resolved = memo[DEFAULT_USER_ID]
            if uid:
                memo[uid] = resolved
            return resolved
        try:
            default_lang = await _db_get_language(DEFAULT_USER_ID)
        except Exception:  # noqa: BLE001
            logger.exception(
                "preferred_language db get failed user_id=%s", DEFAULT_USER_ID
            )
            default_lang = None
        if default_lang:
            memo[DEFAULT_USER_ID] = default_lang
            if uid:
                memo[uid] = default_lang
            return default_lang

        resolved = _yaml_or_en()
        if uid:
            memo[uid] = resolved
        return resolved
    finally:
        if owned_token is not None:
            _MEMO_CV.reset(owned_token)


def resolve_preferred_language() -> str:
    """同步读取：优先本请求 memo；未绑定企业 memo 时回退 yaml → ``en``。

    企业态应在请求入口 ``bind_preferred_language`` 预取后再调用本函数，
    避免同轮多次 ``_resolve_*_language`` 重复打库。
    """
    from jiuwenswarm.enterprise.edition import is_enterprise

    if not is_enterprise():
        return _yaml_or_en()

    memo = _MEMO_CV.get()
    uid = str(_USER_ID_CV.get() or "").strip()
    if memo is not None:
        if uid and uid in memo:
            return memo[uid]
        if DEFAULT_USER_ID in memo:
            return memo[DEFAULT_USER_ID]
    return _yaml_or_en()


def current_preferred_language_user_id() -> str | None:
    raw = _USER_ID_CV.get()
    text = str(raw or "").strip()
    return text or None


def framework_language_from_params(
    params: dict[str, Any] | None, preferred_lang: str
) -> str:
    """Resolve framework-copy language: ``ui_language`` overrides memo/yaml."""
    from jiuwenswarm.common.i18n import try_normalize_preferred_language

    raw_params = params if isinstance(params, dict) else {}
    ui_language = try_normalize_preferred_language(raw_params.get("ui_language"))
    return ui_language if ui_language is not None else preferred_lang


@asynccontextmanager
async def bind_preferred_language(request: Any) -> AsyncIterator[str]:
    """绑定本请求 user_id，并预取语言写入 memo。"""
    from jiuwenswarm.enterprise.edition import is_enterprise

    if not is_enterprise():
        yield _yaml_or_en()
        return

    from jiuwenswarm.enterprise.tenant.tenant_identity import extract_triple

    _group_id, _bot_id, user_id = extract_triple(request)
    memo: dict[str, str] = {}
    token_user = _USER_ID_CV.set(user_id)
    token_memo = _MEMO_CV.set(memo)
    try:
        lang = await resolve_preferred_language_async(user_id)
        yield lang
    finally:
        _USER_ID_CV.reset(token_user)
        _MEMO_CV.reset(token_memo)


@contextmanager
def bind_preferred_language_memo(
    *,
    user_id: str | None,
    language: str,
) -> Iterator[None]:
    """测试/同步路径：直接注入已解析语言。"""
    uid = str(user_id or "").strip()
    lang = _normalize(language) or "en"
    memo = {DEFAULT_USER_ID: lang}
    if uid:
        memo[uid] = lang
    token_user = _USER_ID_CV.set(uid or None)
    token_memo = _MEMO_CV.set(memo)
    try:
        yield
    finally:
        _USER_ID_CV.reset(token_user)
        _MEMO_CV.reset(token_memo)


__all__ = (
    "DEFAULT_USER_ID",
    "bind_preferred_language",
    "bind_preferred_language_memo",
    "current_preferred_language_user_id",
    "framework_language_from_params",
    "resolve_preferred_language",
    "resolve_preferred_language_async",
)
