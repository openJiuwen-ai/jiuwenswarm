# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""登录后自动获得的模型目录。

**模型列表不是配置出来的，是发现出来的**——拿登录用户的 id_token 请求 APIG 的
模型列表接口，把这个账号当前能用的模型抓回来，缓存到本地，再翻译成 jiuwenswarm
``models.defaults`` 的条目形状。

与用户自配模型的关系：**并存，不互斥**。用户在配置页填的模型仍然从 config.yaml 读；
登录送的模型是运行时叠加上去的一层，永远不写进 config.yaml——它们的凭据会过期，
而且账号变了模型也该跟着变。写入侧由 :func:`is_login_model` 挡住。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jiuwenswarm.common.auth.remote_config import get_config as get_remote_config
from jiuwenswarm.common.auth.service import (
    ModelAuthRequired,
    get_auth_service,
    resolve_id_token,
)

logger = logging.getLogger(__name__)

#: 登录模型条目上的来源标记。写配置时据此过滤，前端据此置灰编辑。
LOGIN_MODEL_SOURCE = "huawei-maas-login"

_CACHE_FILE_NAME = "model_catalog.json"
_CACHE_TTL_S = 30 * 60.0

_lock = threading.RLock()
_memo: dict[str, Any] | None = None
#: 上一次向APIG发现模型失败的时间。
_last_discovery_failure_at: float = 0.0
#: 发现失败后的冷却期。APIG 不通时，不冷却的话每次 models.list 都要再撞一次
_DISCOVERY_RETRY_AFTER_FAILURE_S = 30.0


def model_whitelist(allow_refresh: bool = True) -> set[str] | None:
    config = get_remote_config(allow_refresh)
    if config is None or not config.models.whitelist:
        return None
    return set(config.models.whitelist)


def _blocked_models(allow_refresh: bool = True) -> set[str]:
    config = get_remote_config(allow_refresh)
    return set(config.models.blocklist) if config is not None else set()


@dataclass
class LoginModel:

    model_name: str
    display_name: str
    description: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_name": self.model_name,
            "display_name": self.display_name,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LoginModel":
        return cls(
            model_name=str(data.get("model_name") or ""),
            display_name=str(data.get("display_name") or data.get("model_name") or ""),
            description=str(data.get("description") or ""),
        )


def _cache_path() -> Path:
    from jiuwenswarm.common.auth.session_store import auth_dir

    return auth_dir() / _CACHE_FILE_NAME


def _read_cache() -> dict[str, Any]:
    path = _cache_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        logger.warning("[Auth] 模型缓存不可用: %s", exc)
        return {}
    return data if isinstance(data, dict) else {}


def _write_cache(models: list[LoginModel], user_id: str) -> None:
    payload = {
        "v": 1,
        "user_id": user_id,
        "fetched_at": time.time(),
        "models": [model.to_dict() for model in models],
    }
    try:
        path = _cache_path()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        logger.warning("[Auth] 模型缓存写入失败（仅内存生效）: %s", exc)


def clear_cache() -> None:
    global _memo, _last_discovery_failure_at
    with _lock:
        _memo = None
        _last_discovery_failure_at = 0.0
        path = _cache_path()
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:  # pragma: no cover — 平台差异
            logger.debug("[Auth] 删除 %s 失败: %s", path.name, exc)


def discover_models(session_id: str | None = None) -> list[LoginModel]:
    """拿登录用户的 id_token 请求 APIG 模型列表"""
    from jiuwenswarm.common.auth.apig import ApigError, fetch_models, resolve_apig_config

    config = resolve_apig_config()
    if config is None:
        return []
    try:
        items = fetch_models(resolve_id_token(session_id), config)
    except ApigError as exc:
        logger.warning("[Auth] APIG 模型列表获取失败 code=%s: %s", exc.code, exc)
        return []
    logger.info("[Auth] 从 APIG 发现 %d 个模型", len(items))
    return _to_login_models(items)


def _to_login_models(items: list[dict[str, Any]]) -> list[LoginModel]:
    # 这里已经在发现流程里（配置刚被读过），直接用缓存那份，别再触发一次拉取
    whitelist = model_whitelist(allow_refresh=False)
    blocked = _blocked_models(allow_refresh=False)
    models: list[LoginModel] = []
    seen: set[str] = set()
    for item in items:
        model_name = str(item.get("id") or item.get("name") or "").strip()
        if not model_name or model_name in seen:
            continue
        lowered = model_name.lower()
        if lowered in blocked:
            continue
        display_name = str(item.get("name") or model_name).strip() or model_name
        if whitelist is not None and lowered not in whitelist and display_name.lower() not in whitelist:
            continue
        seen.add(model_name)
        models.append(
            LoginModel(
                model_name=model_name,
                display_name=display_name,
                description=str(item.get("description") or item.get("desc") or ""),
                raw=item,
            )
        )
    return models


def refresh(session_id: str | None = None) -> list[LoginModel]:
    global _memo, _last_discovery_failure_at
    try:
        models = discover_models(session_id)
    except ModelAuthRequired as error:
        # 没登录不算 APIG 失败：登录之后应该立刻能发现，不能被冷却挡住
        logger.info("[Auth] 跳过模型发现：%s（%s）", error, error.reason)
        return []
    if not models:
        with _lock:
            _last_discovery_failure_at = time.time()
        return []
    session = get_auth_service().resolve_session(session_id)
    with _lock:
        _last_discovery_failure_at = 0.0
        _write_cache(models, session.user_id if session else "")
        try:
            mtime = _cache_path().stat().st_mtime_ns
        except OSError:
            mtime = None
        _memo = {"models": models, "fetched_at": time.time(), "mtime": mtime}
    return models


def get_models(session_id: str | None = None, allow_refresh: bool = True) -> list[LoginModel]:
    """当前可用的登录模型。缓存优先，过期后台式刷新（失败继续用旧的）。

    未登录时返回空列表——这个函数在很多热路径上被调用，不该抛异常。

    **活动没在跑就一个都不给**，哪怕本地还留着上次的目录缓存：配置拉不到（或
    ``is_effective=false``）时登录接口已经 404、界面也不显示入口，模型下拉里却还列着
    一批选了就 401 的模型，是自相矛盾的。这一条同时管住了所有下游——models.list、
    模型缓存、请求级凭据透传都从这里取。
    """
    global _memo

    config = get_remote_config(allow_refresh)
    if config is None or not config.is_effective:
        return []
    # 多进程：Gateway 登录后写缓存，AgentServer 另一个进程要读到。所以 memo 除了
    # 看 TTL，还要看缓存文件 mtime 变没变——文件更新了就重新读，不然登录前启动的
    # 进程会一直用空列表。
    try:
        disk_mtime = _cache_path().stat().st_mtime_ns
    except OSError:
        disk_mtime = None
    with _lock:
        memo = _memo
    if (
        memo
        and time.time() - memo["fetched_at"] < _CACHE_TTL_S
        and memo.get("mtime") == disk_mtime
    ):
        return list(memo["models"])

    cached = _read_cache()
    cached_models = [LoginModel.from_dict(item) for item in cached.get("models") or []]
    cached_models = [model for model in cached_models if model.model_name]
    fetched_at = float(cached.get("fetched_at") or 0.0)
    fresh = cached_models and time.time() - fetched_at < _CACHE_TTL_S

    if fresh:
        with _lock:
            _memo = {"models": cached_models, "fetched_at": fetched_at, "mtime": disk_mtime}
        return list(cached_models)

    if not allow_refresh:
        return list(cached_models)
    with _lock:
        cooling = time.time() - _last_discovery_failure_at < _DISCOVERY_RETRY_AFTER_FAILURE_S
    if cooling:
        return list(cached_models)

    refreshed = refresh(session_id)
    if refreshed:
        return refreshed
    return list(cached_models)


def build_model_entry(
    *,
    model_name: str,
    api_base: str,
    api_key: str,
    display_name: str = "",
    description: str = "",
) -> dict[str, Any]:
    """按给定的接入点和 api_key 造一个登录模型条目。

    两个调用方：:func:`list_login_model_entries`（Gateway 列表展示，``api_key`` 为空）和
    AgentServer 侧的请求级登录模型（``api_key`` 是凭据句柄占位值，SDK 拼成
    ``Authorization: Bearer <占位值>``，发请求时由钩子换成真 token，见 ``login_credentials``）。
    两处都不放真 id_token。条目形状必须一致，否则同一个模型"列表里能看到、真跑起来配置不一样"。

    ``source`` 标记让写入侧能把这些条目过滤掉，不落进 config.yaml。
    """
    return {
        "model_client_config": {
            "model_name": model_name,
            "api_base": api_base,
            "api_key": api_key,
            "client_provider": "OpenAI",
        },
        "model_config_obj": {"temperature": 0.95},
        "alias": display_name if display_name and display_name != model_name else "",
        # 必须是 True。这个字段不是"我是那个默认模型"的意思，而是"进不进聊天窗口的
        # 模型下拉"——前端按 `is_default !== false` 过滤（sessionStore.ts）。写 False
        # 会让登录模型只在配置页看得到、聊天里选不了。
        # 它同时让 interface_deep 用纯 model_name 注册一个缓存 key，按名字可寻址。
        # 不会抢走默认模型：配置的模型排在前面，默认取的是第一个。
        "is_default": True,
        "source": LOGIN_MODEL_SOURCE,
        "read_only": True,
        "description": description,
        # 前端据此把条目归到「免费模型」分组，字段沿用已有的：is_free
        "is_free": True,
    }


def list_login_model_entries(
    session_id: str | None = None, allow_refresh: bool = True
) -> list[dict[str, Any]]:
    """登录模型的 ``models.defaults`` 条目列表；未登录 / 未接 APIG 返回空。

    ``session_id`` 决定用**谁的**凭据，必须一路传到底。不传就是没登录，返回空——
    不会退回「本机唯一登录会话」，否则没带会话的请求会用上别人的 id_token。

    ``allow_refresh=False`` 给**请求处理路径**用：既不为过期的目录、也不为快过期的
    id_token 去发同步 HTTP（token 改在后台续），模型解析是逐请求同步执行的
    """
    from jiuwenswarm.common.auth.apig import resolve_apig_config

    config = resolve_apig_config()
    if config is None:
        return []
    try:
        # 只用来确认这个会话登录着、凭据还能用；token 本身不放进条目
        resolve_id_token(session_id, allow_refresh)
    except ModelAuthRequired as exc:
        logger.debug("[Auth] 登录模型凭据不可用: %s", exc)
        return []
    return [
        build_model_entry(
            model_name=model.model_name,
            # 走APIG的推理路径
            api_base=config.invoke_base_url,
            # **不放真 token。** 这些条目只用于列表展示（models.list），推理时 AgentServer
            # 用 Gateway 随请求带下来的凭据现造模型
            api_key="",
            display_name=model.display_name,
            description=model.description,
        )
        for model in get_models(session_id, allow_refresh)
    ]


def is_login_model(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    if entry.get("source") == LOGIN_MODEL_SOURCE:
        return True
    return str(entry.get("model_source") or "") == LOGIN_MODEL_SOURCE
