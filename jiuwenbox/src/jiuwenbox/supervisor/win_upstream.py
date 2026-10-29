# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""选择 win_proxy 出站时的上一跳代理.

沙箱进程只能连本机 win_proxy。这里决定 win_proxy 自己怎么连目标:

- 没有上游代理时返回 None, 调用方直接 TCP 连接, 行为和原来一样。
- 有上游时返回该代理。办公区绿区的 Xgate 就是这种代理, 可能在环境变量里,
  也可能只写在当前用户的系统代理或 PAC 里。

``JIUWENBOX_UPSTREAM_PROXY=direct`` 强制直连。设成 ``http://host:port``
则固定走这一跳。指向本机 win_proxy 端口的地址会被丢掉, 避免环回。
"""

from __future__ import annotations

import base64
import ctypes
import ipaddress
import logging
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from jiuwenbox.supervisor import win_constants as const

logger = logging.getLogger(__name__)

ENV_UPSTREAM = "JIUWENBOX_UPSTREAM_PROXY"
_DIRECT_VALUES = frozenset({"direct", "off", "none", "0"})
_ENV_KEYS_HTTPS = (
    "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy",
    "HTTP_PROXY", "http_proxy", "CLAW_HTTP_PROXY",
)
_ENV_KEYS_OTHER = (
    "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy",
    "HTTPS_PROXY", "https_proxy", "CLAW_HTTP_PROXY",
)
_LOOPBACK_NAMES = frozenset({"localhost", "localhost.", "::1"})
_CACHE_TTL_SEC = 60.0
_cache_lock = threading.Lock()
_system_cache: dict[tuple[str, int], tuple[float, "UpstreamProxy | None"]] = {}
_PAC_FAILED = object()


@dataclass(frozen=True)
class UpstreamProxy:
    """一跳 HTTP 代理. 鉴权头不进入日志."""

    host: str
    port: int
    proxy_authorization: str = ""

    def __repr__(self) -> str:
        return f"UpstreamProxy(host={self.host!r}, port={self.port})"


def resolve_upstream(
    host: str,
    port: int,
    environ: dict[str, str] | None = None,
    system_lookup=None,
) -> UpstreamProxy | None:
    """返回目标应使用的上游代理. None 表示直连.

    ``system_lookup`` 缺省时, Windows 上读当前用户的系统代理 (含 PAC).
    测试传入替身, 避免碰到这台机器上真实的代理。
    """
    env = os.environ if environ is None else environ
    target = (host or "").strip()
    if not target or _is_loopback(target):
        return None

    explicit = (env.get(ENV_UPSTREAM) or "").strip()
    if explicit.lower() in _DIRECT_VALUES:
        return None
    if explicit:
        return _usable(parse_proxy_endpoint(explicit))

    if _bypassed(target, env.get("NO_PROXY") or env.get("no_proxy") or ""):
        return None

    keys = _ENV_KEYS_HTTPS if port == 443 else _ENV_KEYS_OTHER
    seen: set[str] = set()
    for key in keys:
        raw = (env.get(key) or "").strip()
        if not raw or raw in seen:
            continue
        seen.add(raw)
        chosen = _usable(parse_proxy_endpoint(raw))
        if chosen is not None:
            return chosen

    lookup = _winhttp_proxy_for if system_lookup is None else system_lookup
    if lookup is None:
        return None
    try:
        found = lookup(target, port)
    except Exception:  # noqa: BLE001 - 系统代理解析失败时退回直连
        logger.warning("读取系统代理失败, 出站改为直连", exc_info=True)
        return None
    return _usable(found)


def parse_proxy_endpoint(value: str) -> UpstreamProxy | None:
    """解析 ``host:port`` 或 ``http://host:port``. SOCKS 不在这里转发."""
    raw = (value or "").strip()
    if not raw or raw.upper() == "DIRECT":
        return None
    if raw.upper().startswith("PROXY "):
        raw = raw[6:].strip()
    if "://" not in raw:
        raw = "http://" + raw
    try:
        parsed = urlsplit(raw)
        scheme = (parsed.scheme or "").lower()
        if scheme.startswith("socks") or scheme not in {"http", "https"}:
            return None
        hostname = parsed.hostname
        if not hostname:
            return None
        port = parsed.port or (443 if scheme == "https" else 80)
        authorization = ""
        if parsed.username is not None:
            user = unquote(parsed.username)
            password = unquote(parsed.password or "")
            token = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
            authorization = f"Basic {token}"
    except ValueError as exc:
        logger.warning("上游代理地址无法解析, 已跳过: %s", exc)
        return None
    return UpstreamProxy(hostname, port, authorization)


def parse_proxy_server_list(value: str, port: int) -> UpstreamProxy | None:
    """解析 WinHTTP / 注册表里的代理串.

    支持 ``host:port``、``http=h:p;https=h:p``、``PROXY h:p; DIRECT``。
    """
    text = (value or "").strip()
    if not text or text.upper() == "DIRECT":
        return None
    upper = text.upper()
    if upper.startswith("PROXY ") or "DIRECT" in upper:
        for part in text.split(";"):
            item = part.strip()
            if item.upper().startswith("PROXY "):
                return parse_proxy_endpoint(item)
        return None
    if "=" in text:
        fallback: UpstreamProxy | None = None
        for part in text.split(";"):
            if "=" not in part:
                continue
            scheme, endpoint = part.split("=", 1)
            scheme = scheme.strip().lower()
            parsed = parse_proxy_endpoint(endpoint.strip())
            if parsed is None:
                continue
            if scheme == "https" and port == 443:
                return parsed
            if scheme == "http" and port != 443:
                return parsed
            if fallback is None and scheme in {"http", "https"}:
                fallback = parsed
        return fallback
    return parse_proxy_endpoint(text)


def _usable(proxy: UpstreamProxy | None) -> UpstreamProxy | None:
    if proxy is None or _is_own_proxy(proxy.host, proxy.port):
        return None
    return proxy


def _is_own_proxy(host: str, port: int) -> bool:
    if not _is_loopback(host):
        return False
    start = const.DEFAULT_PROXY_PORT_RANGE_START
    end = const.DEFAULT_PROXY_PORT_RANGE_END
    return start <= port <= end


def _is_loopback(host: str) -> bool:
    name = host.strip().lower().strip("[]")
    if name in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def _bypassed(host: str, patterns: str) -> bool:
    name = host.strip().lower().rstrip(".")
    for raw in re.split(r"[,\s;]+", patterns):
        pat = raw.strip().lower()
        if not pat:
            continue
        if pat == "*":
            return True
        if pat in {"<local>", ".local"}:
            if "." not in name:
                return True
            continue
        if pat.startswith("*."):
            base = pat[2:]
            if name == base or name.endswith("." + base):
                return True
        elif pat.startswith("."):
            if name.endswith(pat) or name == pat[1:]:
                return True
        elif pat == name:
            return True
    return False


def _winhttp_proxy_for(host: str, port: int) -> UpstreamProxy | None:
    """按当前用户的系统代理为这个目标选上游. 非 Windows 返回 None."""
    if sys.platform != "win32":
        return None
    now = time.monotonic()
    key = (host.lower(), port)
    with _cache_lock:
        cached = _system_cache.get(key)
        if cached is not None and now - cached[0] < _CACHE_TTL_SEC:
            return cached[1]
    found = _query_winhttp(host, port)
    with _cache_lock:
        _system_cache[key] = (now, found)
    return found


def _query_winhttp(host: str, port: int) -> UpstreamProxy | None:
    try:
        winhttp = ctypes.WinDLL("winhttp")
    except OSError:
        return None
    _bind_winhttp(winhttp)
    config = _IEProxyConfig()
    try:
        if not winhttp.WinHttpGetIEProxyConfigForCurrentUser(ctypes.byref(config)):
            return None
        auto_url = _wstring(config.lpszAutoConfigUrl)
        if config.fAutoDetect or auto_url:
            scheme = "https" if port == 443 else "http"
            found = _proxy_for_url(
                winhttp, f"{scheme}://{host}:{port}", auto_url, bool(config.fAutoDetect),
            )
            if found is not _PAC_FAILED:
                return found
        proxy = _wstring(config.lpszProxy)
        bypass = _wstring(config.lpszProxyBypass)
        if proxy and not _bypassed(host, bypass):
            return parse_proxy_server_list(proxy, port)
        return None
    except Exception:  # noqa: BLE001
        logger.debug("WinHTTP 代理解析失败", exc_info=True)
        return None
    finally:
        _global_free(config.lpszAutoConfigUrl)
        _global_free(config.lpszProxy)
        _global_free(config.lpszProxyBypass)


class _IEProxyConfig(ctypes.Structure):
    _fields_ = [
        ("fAutoDetect", ctypes.c_int),
        ("lpszAutoConfigUrl", ctypes.c_void_p),
        ("lpszProxy", ctypes.c_void_p),
        ("lpszProxyBypass", ctypes.c_void_p),
    ]


class _AutoProxyOptions(ctypes.Structure):
    _fields_ = [
        ("dwFlags", ctypes.c_uint32),
        ("dwAutoDetectFlags", ctypes.c_uint32),
        ("lpszAutoConfigUrl", ctypes.c_wchar_p),
        ("lpvReserved", ctypes.c_void_p),
        ("dwReserved", ctypes.c_uint32),
        ("fAutoLogonIfChallenged", ctypes.c_int),
    ]


class _ProxyInfo(ctypes.Structure):
    _fields_ = [
        ("dwAccessType", ctypes.c_uint32),
        ("lpszProxy", ctypes.c_void_p),
        ("lpszProxyBypass", ctypes.c_void_p),
    ]


def _bind_winhttp(winhttp) -> None:
    winhttp.WinHttpOpen.argtypes = [
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32,
    ]
    winhttp.WinHttpOpen.restype = ctypes.c_void_p
    winhttp.WinHttpCloseHandle.argtypes = [ctypes.c_void_p]
    winhttp.WinHttpCloseHandle.restype = ctypes.c_int
    winhttp.WinHttpGetIEProxyConfigForCurrentUser.argtypes = [ctypes.c_void_p]
    winhttp.WinHttpGetIEProxyConfigForCurrentUser.restype = ctypes.c_int
    winhttp.WinHttpGetProxyForUrl.argtypes = [
        ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_void_p,
    ]
    winhttp.WinHttpGetProxyForUrl.restype = ctypes.c_int
    winhttp.WinHttpSetOption.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
    ]
    winhttp.WinHttpSetOption.restype = ctypes.c_int


def _proxy_for_url(winhttp, url: str, auto_url: str, auto_detect: bool):
    """PAC 结果. 调用失败返回 ``_PAC_FAILED``, 以便回退静态代理."""
    session = winhttp.WinHttpOpen("jiuwenbox-upstream/1.0", 1, None, None, 0)
    if not session:
        return _PAC_FAILED
    info = _ProxyInfo()
    try:
        millis = ctypes.c_uint32(5000)
        for option in (3, 6):  # CONNECT_TIMEOUT, RECEIVE_TIMEOUT
            winhttp.WinHttpSetOption(
                session, option, ctypes.byref(millis), ctypes.sizeof(millis),
            )
        flags = 0
        detect = 0
        if auto_detect:
            flags |= 0x00000001  # WINHTTP_AUTOPROXY_AUTO_DETECT
            detect = 0x00000001 | 0x00000002
        if auto_url:
            flags |= 0x00000002  # WINHTTP_AUTOPROXY_CONFIG_URL
        options = _AutoProxyOptions(flags, detect, auto_url or None, None, 0, 1)
        if not winhttp.WinHttpGetProxyForUrl(
            session, url, ctypes.byref(options), ctypes.byref(info),
        ):
            return _PAC_FAILED
        if info.dwAccessType == 1:  # WINHTTP_ACCESS_TYPE_NO_PROXY
            return None
        text = _wstring(info.lpszProxy)
        if not text or text.upper() == "DIRECT":
            return None
        return parse_proxy_server_list(text, _port_of(url))
    finally:
        _global_free(info.lpszProxy)
        _global_free(info.lpszProxyBypass)
        winhttp.WinHttpCloseHandle(session)


def _port_of(url: str) -> int:
    parsed = urlsplit(url)
    if parsed.port:
        return parsed.port
    return 443 if parsed.scheme == "https" else 80


def _wstring(ptr) -> str:
    if not ptr:
        return ""
    return ctypes.wstring_at(ptr)


def _global_free(ptr) -> None:
    if not ptr:
        return
    ctypes.windll.kernel32.GlobalFree(ctypes.c_void_p(ptr))
