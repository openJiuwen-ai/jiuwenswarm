# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Overlay-aware HTTP proxy resolution for outbound requests.

Reads ``HTTP_PROXY`` / ``HTTPS_PROXY`` / ``NO_PROXY`` via tip/overlay
(:func:`read_env_if_set`) first so namespaced tenant env and task overlays
apply without process-env fallthrough, then falls back to bare ``os.environ``
for spawn values. Callers pass explicit ``proxies`` / ``proxy`` and disable
``trust_env`` so requests does not scrape the whole process environment.
"""

from __future__ import annotations

import ipaddress
import os
import threading
from typing import Any
from urllib.parse import urlparse

import requests

from jiuwenswarm.common.local_env_config import read_env, read_env_if_set

_PROXY_ENV_KEYS: tuple[str, ...] = (
    "http_proxy",
    "https_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
)
_NO_PROXY_ENV_KEYS: tuple[str, ...] = ("NO_PROXY", "no_proxy")


def _read_env_then_environ(keys: tuple[str, ...]) -> str:
    """Tip/overlay first (no process fallthrough), then process env.

    Tip-only first pass matters on Windows where ``os.environ`` is case-
    insensitive: ``read_env('http_proxy')`` would otherwise hit ``HTTP_PROXY``
    in the process environment before the tip value for ``HTTP_PROXY``.
    """
    for key in keys:
        raw = read_env_if_set(key)
        if raw is not None and str(raw).strip():
            return str(raw).strip()
    for key in keys:
        raw = (os.environ.get(key) or "").strip()
        if raw:
            return raw
    return ""


def read_proxy_url() -> str:
    """Return the configured proxy URL for the current env context."""
    return _read_env_then_environ(_PROXY_ENV_KEYS)


def read_no_proxy_list() -> list[str]:
    """Parse NO_PROXY for the current env context (deduped, lowercased)."""
    result: list[str] = []
    seen: set[str] = set()

    def _consume(raw: str) -> None:
        normalized = raw.replace(" ", ",").replace(";", ",")
        for item in normalized.split(","):
            entry = item.strip().lower()
            if entry and entry not in seen:
                seen.add(entry)
                result.append(entry)

    tip_hit = False
    for key in _NO_PROXY_ENV_KEYS:
        raw = read_env_if_set(key)
        if raw is not None and str(raw).strip():
            tip_hit = True
            _consume(str(raw))
    if tip_hit:
        return result
    for key in _NO_PROXY_ENV_KEYS:
        raw = (os.environ.get(key) or "").strip()
        if raw:
            _consume(raw)
    return result


def _hostname_matches_no_proxy(hostname: str, no_proxy_list: list[str]) -> bool:
    hostname_lower = hostname.lower()
    for entry in no_proxy_list:
        if entry == "*":
            return True
        if entry == hostname_lower:
            return True
        if entry.startswith(".") and hostname_lower.endswith(entry):
            return True
        if _is_ip_match(hostname_lower, entry):
            return True
    return False


def _is_ip_match(hostname: str, entry: str) -> bool:
    try:
        ip_addr = ipaddress.ip_address(hostname)
        if "/" in entry:
            network = ipaddress.ip_network(entry, strict=False)
            return ip_addr in network
        return ip_addr == ipaddress.ip_address(entry)
    except ValueError:
        return False


def should_bypass_proxy(url: str) -> bool:
    """Return True when *url* should not use the configured HTTP proxy."""
    parsed = urlparse(url or "")
    hostname = parsed.hostname
    if not hostname:
        return False
    no_proxy_list = read_no_proxy_list()
    if not no_proxy_list:
        return False
    return _hostname_matches_no_proxy(hostname, no_proxy_list)


def resolve_requests_proxies(url: str) -> dict[str, str] | None:
    """Return a ``requests`` proxies mapping, or ``None`` for a direct connection."""
    if url and should_bypass_proxy(url):
        return None
    proxy_url = read_proxy_url()
    if not proxy_url:
        return None
    return {"http": proxy_url, "https": proxy_url}


def resolve_httpx_proxy(url: str) -> str | None:
    """Return an ``httpx`` proxy URL, or ``None`` for a direct connection."""
    if url and should_bypass_proxy(url):
        return None
    proxy_url = read_proxy_url()
    return proxy_url or None


def prepare_requests_kwargs(url: str, kwargs: dict[str, Any] | None = None) -> dict[str, Any]:
    """Merge overlay-aware proxy settings into ``requests`` keyword arguments."""
    out = dict(kwargs or {})
    proxies = resolve_requests_proxies(url)
    # Respect caller-supplied proxies (e.g. FREE_SEARCH_PROXY_URL).
    if proxies and "proxies" not in out:
        out["proxies"] = proxies
    return out


def _ssl_verify_raw() -> str:
    for key in ("JIUWENSWARM_SSL_VERIFY", "JIUWENCLAW_SSL_VERIFY"):
        raw = read_env(key, "").strip()
        if raw:
            return raw
        raw = (os.environ.get(key) or "").strip()
        if raw:
            return raw
    return ""


def ssl_verify_enabled(default: bool = True) -> bool:
    """Whether TLS verification is enabled (tip, then process environ)."""
    text = _ssl_verify_raw().strip().lower()
    if text in ("0", "false", "no", "off"):
        return False
    if text in ("1", "true", "yes", "on"):
        return True
    return default


def resolve_requests_verify() -> bool | str:
    """Return requests ``verify`` kwarg: False | CA path | True.

    Priority:
    1. ``JIUWENCLAW_SSL_VERIFY`` falsy → ``False``
    2. ``REQUESTS_CA_BUNDLE`` exists → CA path
    3. otherwise → ``True``

    Explicit CA injection is required because ``trust_env=False`` prevents
    requests from reading ``REQUESTS_CA_BUNDLE`` from the process environment.
    """
    if not ssl_verify_enabled():
        return False
    ca_bundle = (os.environ.get("REQUESTS_CA_BUNDLE") or "").strip()
    if ca_bundle and os.path.exists(ca_bundle):
        return ca_bundle
    return True


def _requests_verify() -> bool | str:
    return resolve_requests_verify()


# ---------------------------------------------------------------------------
# 进程级复用 Session
#
# 旧实现每次调用 ``with requests.Session()`` 新建并销毁连接池：
# 1. 每个请求一次完整 TCP+TLS 握手，无 keep-alive 复用；
# 2. 异常路径（raise_for_status 抛错、响应体未读完）遗留的连接/池对象
#    只能等 GC 回收 —— urllib3 通过 weakref 终结器在**触发 GC 的线程**
#    上同步执行 ``_close_pool_connections → ssl._real_close``；当 GC 恰好
#    落在 asyncio 事件循环线程上时（ISSUE 4868 实测），~10s 的同步 SSL
#    关闭会冻结整个循环，网关 pong 无应答 → 连接被主动关闭。
# 复用 Session 后：连接池常驻、TLS 连接按连接粒度复用，进程生命周期内
# 不再产生"待 GC 回收的临时连接池"，从源头阻断该阻塞路径。
# ---------------------------------------------------------------------------
_session: requests.Session | None = None
_session_lock = threading.Lock()


def _get_session() -> requests.Session:
    """返回进程级复用的 ``requests.Session``（惰性创建，线程安全）。

    ``urllib3`` 连接池按连接粒度加锁，``Session.request`` 可安全地被
    ``asyncio.to_thread`` 工作线程并发调用；本工具链不做 cookie 语义，
    无 cookie jar 竞态顾虑。
    """
    global _session
    session = _session
    if session is not None:
        return session
    with _session_lock:
        if _session is None:
            session = requests.Session()
            session.trust_env = False
            _session = session
        return _session


def reset_shared_session() -> None:
    """清空进程级复用 Session 的缓存（仅供测试隔离使用）。

    单元测试通过 monkeypatch 替换 ``requests.Session`` 构造来捕获调用
    参数；缓存的 Session 会跨用例存活，导致后继用例的替身失效。
    生产代码无需调用——复用正是本模块的设计目标。仅释放引用、
    不主动 close，避免影响可能仍在使用的连接。
    """
    global _session
    with _session_lock:
        _session = None


def requests_request(method: str, url: str, **kwargs: Any) -> requests.Response:
    """Issue a ``requests`` call using overlay-aware proxy settings only."""
    method_up = method.upper()
    kwargs = prepare_requests_kwargs(url, kwargs)
    kwargs.setdefault("verify", _requests_verify())
    session = _get_session()
    try:
        return session.request(method_up, url, **kwargs)
    except requests.exceptions.ProxyError:
        fallback = dict(kwargs)
        fallback.pop("proxies", None)
        return session.request(method_up, url, **fallback)


def requests_get(url: str, **kwargs: Any) -> requests.Response:
    return requests_request("GET", url, **kwargs)


def requests_post(url: str, **kwargs: Any) -> requests.Response:
    return requests_request("POST", url, **kwargs)
