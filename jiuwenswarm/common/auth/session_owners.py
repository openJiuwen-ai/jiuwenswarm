# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""对话会话 → 在这个会话里选了登录送的免费模型的华为账号。

免费模型的凭据跟着登录会话走：网页端的请求带着登录会话，Gateway 据此挂上凭据。飞书等
IM 通道、以及它们桥接进团队的回复没有登录会话，会话里用的免费模型就挂不上凭据。

这些对话的主人，是在网页上登录、给这个会话选了免费模型的人——和自配模型一样，别人在 IM
上跟你的机器人 / 团队对话，用的是你的模型额度。所以主人带着登录会话用免费模型时记下账号
句柄，之后同一会话里没有登录会话的请求按它找回凭据（见 ``MessageHandler``）。

只记 Gateway 自己挂上凭据的请求，不收调用方传来的值。句柄按账号算、不可逆，落盘无妨；
同一账号重新登录后不变。会话之后换了别的模型，这条记录不会被用上（使用方会核对会话当前
的模型），所以不需要跟着换模型去删。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_FILE_NAME = "session_owners.json"
_TTL_S = 30 * 24 * 60 * 60
_TOUCH_INTERVAL_S = 24 * 60 * 60
_MAX_ENTRIES = 2000

_lock = threading.Lock()
_entries: dict[str, dict] | None = None


@dataclass(frozen=True)
class SessionOwner:
    credential_ref: str
    model_name: str


def _path() -> Path:
    from jiuwenswarm.common.auth.session_store import auth_dir

    return auth_dir() / _FILE_NAME


def _load() -> dict[str, dict]:
    global _entries
    if _entries is None:
        try:
            data = json.loads(_path().read_text(encoding="utf-8"))
            _entries = data if isinstance(data, dict) else {}
        except FileNotFoundError:
            _entries = {}
        except (OSError, ValueError):
            logger.warning("[Auth] 会话主人记录读取失败，按空处理", exc_info=True)
            _entries = {}
    return _entries


def _flush(entries: dict[str, dict]) -> None:
    path = _path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _written_at(item: dict) -> float:
    return float(item.get("at") or 0)


def _is_up_to_date(item: object, credential_ref: str, model_name: str, now: float) -> bool:
    if not isinstance(item, dict):
        return False
    unchanged = (item.get("ref"), item.get("model")) == (credential_ref, model_name)
    return unchanged and now - _written_at(item) < _TOUCH_INTERVAL_S


def _prune(entries: dict[str, dict], now: float) -> None:
    for sid in [sid for sid, item in entries.items() if now - _written_at(item) > _TTL_S]:
        del entries[sid]
    overflow = len(entries) - _MAX_ENTRIES
    if overflow > 0:
        for sid in sorted(entries, key=lambda key: _written_at(entries[key]))[:overflow]:
            del entries[sid]


def remember(session_id: str, credential_ref: str, model_name: str) -> None:
    from jiuwenswarm.common.auth.login_credentials import bare_model_name, is_credential_ref

    session_id = str(session_id or "").strip()
    model_name = bare_model_name(model_name)
    if not session_id or not model_name or not is_credential_ref(credential_ref):
        return
    now = time.time()
    with _lock:
        entries = _load()
        if _is_up_to_date(entries.get(session_id), credential_ref, model_name, now):
            return
        entries[session_id] = {"ref": credential_ref, "model": model_name, "at": now}
        _prune(entries, now)
        try:
            _flush(entries)
        except OSError:
            logger.warning("[Auth] 会话主人记录写入失败 session=%s", session_id, exc_info=True)


def lookup(session_id: str) -> SessionOwner | None:
    session_id = str(session_id or "").strip()
    if not session_id:
        return None
    with _lock:
        item = _load().get(session_id)
    if not isinstance(item, dict) or time.time() - _written_at(item) > _TTL_S:
        return None
    ref = str(item.get("ref") or "")
    model = str(item.get("model") or "")
    return SessionOwner(credential_ref=ref, model_name=model) if ref and model else None


def login_auth_for_owner(owner: SessionOwner) -> dict | None:
    from jiuwenswarm.common.auth.apig import resolve_apig_config
    from jiuwenswarm.common.auth.model_catalog import get_models
    from jiuwenswarm.common.auth.passthrough import refreshed_credential_for_ref

    if owner.model_name not in {model.model_name for model in get_models(allow_refresh=False)}:
        return None
    apig_config = resolve_apig_config(allow_refresh=False)
    if apig_config is None:
        return None
    fresh = refreshed_credential_for_ref(owner.credential_ref)
    if not fresh or fresh.get("revoked") or not fresh.get("api_key"):
        return None
    return {
        "api_base": apig_config.invoke_base_url,
        "api_key": fresh["api_key"],
        "credential_ref": owner.credential_ref,
    }


def reset_for_test() -> None:
    global _entries
    with _lock:
        _entries = None
