# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""登录与免费模型的**远端配置**：官网接口下发。

地址默认指向正式环境（:data:`DEFAULT_CONFIG_URL`），所以这块功能**默认开着**；
联调、私有部署用 ``JIUWENSWARM_CONFIG_URL`` 指到别处。把它显式设成空串（或 off）
就是彻底关掉：一个请求都不发，登录接口 404、界面不显示入口。
拉不到配置、或配置里 ``is_effective`` 为 false，同样是功能关闭

响应形状::

    {
      "v1.0": {                        // 版本层，客户端自动剥掉
        "is_effective": true,          // 免费模型活动是否有效；false = 整块功能关闭
        "ttl_seconds": 600,            // 收下但不用：配置只在进程启动时拉一次（见 get_config）
        "huaweiaccount_login": {       // 一种登录形式；原样交给对应实现
          "client_id": "...", "redirect_uri": "...", "scope": "...", "exchange_url": "..."
        },
        "gateway": {"base_url": "...", "models_path": "...", "quota_path": "...", "invoke_path": "..."},
        "models": {"whitelist": ["GLM-5"], "blocklist": []}   // 可选，不给 = 不过滤
      }
    }

**登录段按名字分开。** 凡是以 ``_login`` 结尾的段都收下，实现各取各的段名
（``account_kit.LOGIN_SECTION`` = ``huaweiaccount_login``）。这样一份配置能同时带多种
登录形式，将来加一种不用动这个模块，两条分支也不会在这里打架。段内的键**一律不在
这里解释**，由实现自己认。
"""

from __future__ import annotations

import logging
import os
import random
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from jiuwenswarm.common.auth.net import requests_request

logger = logging.getLogger(__name__)

CONFIG_URL_ENV = "JIUWENSWARM_CONFIG_URL"
#: 正式环境的配置接口。功能默认开启，是否真的开着由接口返回的 is_effective 决定。
DEFAULT_CONFIG_URL = "https://aigw.openjiuwen.com/v1/config"
_DISABLED_VALUES = ("", "off", "none", "false", "0", "disabled")
REQUEST_TIMEOUT_S = 8.0


@dataclass(frozen=True)
class GatewaySettings:

    base_url: str = ""
    models_path: str = "/v1/models"
    quota_path: str = "/v1/usage"
    invoke_path: str = "/v1"


@dataclass(frozen=True)
class ModelFilters:

    whitelist: frozenset[str] | None = None
    blocklist: frozenset[str] = frozenset()


@dataclass(frozen=True)
class LoginSection:

    values: Mapping[str, Any] = field(default_factory=dict)

    def value(self, key: str, default: str = "") -> str:
        raw = self.values.get(key)
        return str(raw).strip() if isinstance(raw, (str, int)) and str(raw).strip() else default


@dataclass(frozen=True)
class RemoteConfig:
    #: 活动是否有效。false 时登录接口一律 404、前端不显示任何入口。
    is_effective: bool = False
    #: 各种登录形式，按段名索引（``huaweiaccount_login`` 等）。
    logins: Mapping[str, LoginSection] = field(default_factory=dict)
    gateway: GatewaySettings = field(default_factory=GatewaySettings)
    models: ModelFilters = field(default_factory=ModelFilters)
    fetched_at: float = 0.0

    def login(self, section: str) -> LoginSection:
        return self.logins.get(section, LoginSection())


def _as_str_set(value: Any) -> frozenset[str]:
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, (list, tuple)):
        items = [str(item) for item in value]
    else:
        return frozenset()
    return frozenset(item.strip().lower() for item in items if str(item).strip())


#: 登录段的后缀。一份配置可以同时带多种登录形式（``huaweiaccount_login``）
LOGIN_SECTION_SUFFIX = "_login"


def _login_sections(payload: dict) -> dict:
    return {
        str(key): LoginSection({str(k): v for k, v in value.items()})
        for key, value in payload.items()
        if str(key).endswith(LOGIN_SECTION_SUFFIX) and isinstance(value, dict)
    }


_VERSION_KEY = re.compile(r"^v\d+(\.\d+)*$", re.IGNORECASE)


def _unwrap_version(payload: dict) -> dict:
    if not payload or not all(_VERSION_KEY.match(str(key)) for key in payload):
        return payload
    versions = sorted(
        payload,
        key=lambda key: tuple(int(part) for part in str(key).lstrip("vV").split(".")),
    )
    inner = payload[versions[-1]]
    return inner if isinstance(inner, dict) else payload


def parse_config(payload: Any) -> RemoteConfig:
    if not isinstance(payload, dict):
        return RemoteConfig()
    payload = _unwrap_version(payload)
    gateway = payload.get("gateway") if isinstance(payload.get("gateway"), dict) else {}
    models = payload.get("models") if isinstance(payload.get("models"), dict) else {}

    whitelist = _as_str_set(models.get("whitelist"))

    return RemoteConfig(
        is_effective=bool(payload.get("is_effective")),
        logins=_login_sections(payload),
        gateway=GatewaySettings(
            base_url=_normalize_base_url(str(gateway.get("base_url") or "")),
            models_path=str(gateway.get("models_path") or "/v1/models"),
            quota_path=str(gateway.get("quota_path") or "/v1/usage"),
            invoke_path=str(gateway.get("invoke_path") or "/v1"),
        ),
        models=ModelFilters(
            whitelist=whitelist or None,
            blocklist=_as_str_set(models.get("blocklist")),
        ),
        fetched_at=time.time(),
    )


def _normalize_base_url(raw: str) -> str:
    value = raw.strip().rstrip("/")
    if value and not value.startswith(("http://", "https://")):
        value = f"https://{value}"
    return value


def config_url() -> str:
    raw = os.environ.get(CONFIG_URL_ENV)
    if raw is None:
        return DEFAULT_CONFIG_URL
    value = raw.strip()
    return "" if value.lower() in _DISABLED_VALUES else value


_lock = threading.Lock()
#: 同一时刻只让一个线程去拉：TTL 一到，并发进来的请求共用这一次，不各发一遍。
_fetch_lock = threading.Lock()
_cached: RemoteConfig | None = None
_last_failure_at: float = 0.0
_background_refreshing = False
#: 第一次拉失败后的冷却期，避免每个请求都去撞一次不通的接口。
_RETRY_AFTER_FAILURE_S = 30.0
#: 连续失败时冷却期翻倍，封顶这么久。官网接口挂着时所有还没拉到配置的客户端都在重试，
#: 固定 30 秒会把压力钉在"在线数 ÷ 30 秒"，恢复瞬间还会一起涌上来；封顶 10 分钟既把持续
#: 故障的压力降一个量级，也保证恢复后最迟 10 分钟入口自己回来。
_RETRY_BACKOFF_MAX_S = 600.0
#: 冷却期上下浮动这么多，错开各客户端的重试时刻。
_RETRY_JITTER = 0.2
#: 这次失败要等多久才允许再试（含抖动）；0 = 没在冷却
_retry_after_s: float = 0.0
#: 下一次失败的冷却基准，成功后复位
_retry_base_s: float = _RETRY_AFTER_FAILURE_S


def get_config(allow_refresh: bool = True) -> RemoteConfig | None:
    """当前配置；从没拿到过返回 ``None``（= 功能关闭）。

    **一个进程只拉一次**（启动时预热，见 :func:`warm_up_in_background`）：每个客户端都是一个
    进程，按 TTL 定期刷新会给官网压上"在线客户端数 ÷ 刷新周期"的常态 QPS。代价是改配置
    （含活动下线）要客户端重启才生效。只有从来没拉到过才会重试，冷却从 ``_RETRY_AFTER_FAILURE_S``
    起、连续失败翻倍（见 :func:`_record_failure`）。

    ``allow_refresh=False`` 给不能阻塞的调用方（Gateway 事件循环、逐请求的模型解析）：不在调用
    线程上发 HTTP，只在后台线程补拉。AgentServer 读配置的地方全是这类热路径。
    """
    url = config_url()
    if not url:
        return None

    cached, cooling = _snapshot()
    if cached is not None or cooling:
        return cached
    if not allow_refresh:
        _refresh_in_background()
        return cached
    return _fetch_once(url)


def warm_up_in_background() -> None:
    if config_url():
        _refresh_in_background()


def _snapshot() -> tuple[RemoteConfig | None, bool]:
    with _lock:
        return _cached, time.time() - _last_failure_at < _retry_after_s


def _refresh_in_background() -> None:
    global _background_refreshing
    with _lock:
        if _background_refreshing:
            return
        _background_refreshing = True

    def _run() -> None:
        global _background_refreshing
        try:
            url = config_url()
            if url:
                _fetch_once(url)
        except Exception:  # noqa: BLE001 — 后台刷新失败只记日志，拿不到就继续用上一份
            logger.warning("[Auth] 后台刷新远端配置异常", exc_info=True)
        finally:
            with _lock:
                _background_refreshing = False

    threading.Thread(target=_run, name="remote-config-refresh", daemon=True).start()


def _fetch_once(url: str) -> RemoteConfig | None:
    with _fetch_lock:
        cached, cooling = _snapshot()
        if cached is not None or cooling:
            return cached
        return _fetch(url, cached)


def _backoff(base: float) -> tuple[float, float]:
    return base * random.uniform(1 - _RETRY_JITTER, 1 + _RETRY_JITTER), min(base * 2, _RETRY_BACKOFF_MAX_S)


def _record_failure() -> None:
    global _last_failure_at, _retry_after_s, _retry_base_s
    _last_failure_at = time.time()
    _retry_after_s, _retry_base_s = _backoff(_retry_base_s)


def _reset_backoff() -> None:
    global _last_failure_at, _retry_after_s, _retry_base_s
    _last_failure_at = 0.0
    _retry_after_s = 0.0
    _retry_base_s = _RETRY_AFTER_FAILURE_S


def _fetch(url: str, cached: RemoteConfig | None) -> RemoteConfig | None:
    global _cached

    try:
        response = requests_request("GET", url, headers={"Accept": "application/json"}, timeout=REQUEST_TIMEOUT_S)
        if response.status_code != 200:
            raise ValueError(f"HTTP {response.status_code}")
        config = parse_config(response.json())
    except Exception as exc:  # noqa: BLE001 — 拿不到就用上一份，别让官网抖动把人踢下线
        with _lock:
            _record_failure()
        logger.warning(
            "[Auth] 配置接口不可用（%s）：%s",
            "继续用上一次的配置" if cached is not None else "登录功能暂不可用",
            exc,
        )
        return cached

    with _lock:
        _cached = config
        _reset_backoff()
    logger.info(
        "[Auth] 已拉取远端配置 is_effective=%s gateway=%s 模型白名单=%s",
        config.is_effective,
        bool(config.gateway.base_url),
        "有" if config.models.whitelist else "无",
    )
    return config


def set_config_for_test(config: RemoteConfig | None) -> None:
    global _cached, _background_refreshing
    with _lock:
        _cached = config
        _reset_backoff()
        _background_refreshing = False
