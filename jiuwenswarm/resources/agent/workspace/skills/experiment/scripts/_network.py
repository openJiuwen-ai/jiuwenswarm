# -*- coding: utf-8 -*-
"""网络端点 / 超时 / 重试的集中配置——数据下载多源与鲁棒性优化的唯一开关处。

背景（2026-09 实测事故）：论文管线在实验阶段连续 4 轮停在数据准备。直接原因是
huggingface.co 直连被重置（WinError 10054），而全仓库的域名、超时都是硬编码单点，
没有任何镜像或重试可退——一次网络抖动 = 一整轮实验白烧。

本模块把「去哪下、等多久、重试几次」从各脚本里抽出来集中管理：

- :func:`hf_bases` / :func:`hf_parquet_bases`：Hugging Face 端点族，**主源在前**，
  逐个尝试直到成功。默认 ``huggingface.co`` → ``hf-mirror.com``（国内可达镜像）。
- :func:`fetch_json_with_retry`：带指数退避的 JSON 拉取，**保持 ``(url, timeout)``
  签名**，可直接替换 ``dataset_resolver._fetch_json``，不破坏
  ``resolve_dataset_source(fetch_json=...)`` 的测试注入缝。
- :func:`download_variants`：把一个文件 URL 展开成「同一路径的多源候选」（换 host，
  主源在前），供下载层逐个回退。

环境变量（全部可选，非法值回退默认并告警）

===========================================  ==========================================
``JIUWENSWARM_HF_BASES``                     逗号分隔的 HF 站点，默认
                                             ``https://huggingface.co,https://hf-mirror.com``。
                                             ``hf-mirror.com`` 在列表里即启用镜像回退；
                                             置空或只留一项即退回单源行为。
``JIUWENSWARM_HF_PARQUET_BASES``             datasets-server（parquet 转换）端点，默认
                                             ``https://datasets-server.huggingface.co``。
``JIUWENSWARM_SEARCH_TIMEOUT_S``             检索/元数据请求超时秒数，默认 30。
``JIUWENSWARM_DOWNLOAD_TIMEOUT_S``           单个数据文件下载超时秒数，默认 60。
``JIUWENSWARM_DOWNLOAD_RETRIES``             单个源的重试次数（不含首次），默认 2。
===========================================  ==========================================

安全边界：本模块只改「连哪个公网端点」，不放松任何校验。下载 URL 仍要过
``prepare_data._validate_download_url`` 的公网 IP 检查，镜像域名是公网域名天然通过，
内网地址依旧被拒。
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any, Callable
from urllib.parse import urlparse, urlunparse

log = logging.getLogger("experiment.network")

# Hugging Face 官方站点 + 国内镜像。镜像只作**回退**：主源能用时一次都不碰它，
# 避免把审计产物里的 URL 换成镜像域、也避免给镜像送无谓流量。
DEFAULT_HF_BASES = ("https://huggingface.co", "https://hf-mirror.com")
DEFAULT_HF_PARQUET_BASES = ("https://datasets-server.huggingface.co",)

DEFAULT_SEARCH_TIMEOUT_SECONDS = 30
DEFAULT_DOWNLOAD_TIMEOUT_SECONDS = 60
DEFAULT_DOWNLOAD_RETRIES = 2

#: 自动下载的**预估**总量上限。超过则不进下载，改为把候选文件清单交给模块二挑一个。
#: 与 ``prepare_data.DEFAULT_MAX_DOWNLOAD_BYTES``（20 GiB **硬上限**，下到一半才触发）
#: 是两个不同的闸门：这个在下载**之前**用 HEAD 预估拦，避免白跑一场超大下载
#: （2026-09 实测：一次 prepare_data 调用因 2.7 GB 下载超过宿主 300s 工具超时被硬杀）。
DEFAULT_MAX_AUTO_DOWNLOAD_BYTES = 10 * 1024**3

#: 体积探测的单次请求超时。只读响应头，应当很快；慢就放弃探测而不是拖慢主流程。
DEFAULT_SIZE_PROBE_TIMEOUT_SECONDS = 5

# 单次响应上限（与 dataset_resolver.MAX_METADATA_BYTES 同量级，防镜像返回超大页）
MAX_METADATA_BYTES = 4 * 1024 * 1024

_BACKOFF_BASE_SECONDS = 1.0


# ════════════════════════════════════════════════════════════════
# 环境变量解析（按 (变量名, 原始值) 记忆化——env 变了立刻生效，测试可直接 patch）
# ════════════════════════════════════════════════════════════════

_env_cache: dict[tuple[str, str | None], Any] = {}


def _cached(name: str, raw: str | None, parse: Callable[[str | None], Any]) -> Any:
    key = (name, raw)
    if key not in _env_cache:
        _env_cache[key] = parse(raw)
    return _env_cache[key]


def reset_env_cache() -> None:
    """清空解析缓存（测试用；生产路径不需要调）。"""
    _env_cache.clear()


def _parse_base_list(raw: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if raw is None or not raw.strip():
        return default
    bases: list[str] = []
    for item in raw.split(","):
        candidate = item.strip().rstrip("/")
        if not candidate:
            continue
        parsed = urlparse(candidate)
        if parsed.scheme != "https" or not parsed.hostname:
            log.warning("忽略非 https 的端点配置项: %r", item.strip())
            continue
        if candidate not in bases:
            bases.append(candidate)
    if not bases:
        log.warning("端点配置无有效项，回退默认: %s", ",".join(default))
        return default
    return tuple(bases)


def _parse_positive_int(raw: str | None, default: int) -> int:
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        log.warning("超时/重试配置不是整数（%r），回退默认 %d", raw, default)
        return default
    if value < 0:
        log.warning("超时/重试配置为负（%r），回退默认 %d", raw, default)
        return default
    return value


def _env(name: str) -> str | None:
    return os.environ.get(name)


def hf_bases() -> tuple[str, ...]:
    """Hugging Face 站点列表，主源在前（默认官方 → hf-mirror）。"""
    return _cached("hf_bases", _env("JIUWENSWARM_HF_BASES"), lambda raw: _parse_base_list(raw, DEFAULT_HF_BASES))


def hf_parquet_bases() -> tuple[str, ...]:
    """datasets-server（parquet 自动转换）端点列表，主源在前。"""
    return _cached(
        "hf_parquet_bases",
        _env("JIUWENSWARM_HF_PARQUET_BASES"),
        lambda raw: _parse_base_list(raw, DEFAULT_HF_PARQUET_BASES),
    )


def search_timeout_seconds() -> int:
    return _cached(
        "search_timeout", _env("JIUWENSWARM_SEARCH_TIMEOUT_S"),
        lambda raw: _parse_positive_int(raw, DEFAULT_SEARCH_TIMEOUT_SECONDS),
    )


def download_timeout_seconds() -> int:
    return _cached(
        "download_timeout", _env("JIUWENSWARM_DOWNLOAD_TIMEOUT_S"),
        lambda raw: _parse_positive_int(raw, DEFAULT_DOWNLOAD_TIMEOUT_SECONDS),
    )


def download_retries() -> int:
    """单个源的额外重试次数（不含首次尝试）。"""
    return _cached(
        "download_retries", _env("JIUWENSWARM_DOWNLOAD_RETRIES"),
        lambda raw: _parse_positive_int(raw, DEFAULT_DOWNLOAD_RETRIES),
    )


def max_auto_download_bytes() -> int:
    """自动下载的预估总量上限。``JIUWENSWARM_MAX_AUTO_DOWNLOAD_GB=0`` 关闭闸门。"""
    return _cached(
        "max_auto_download",
        _env("JIUWENSWARM_MAX_AUTO_DOWNLOAD_GB"),
        lambda raw: _parse_positive_int(
            raw, DEFAULT_MAX_AUTO_DOWNLOAD_BYTES // 1024**3
        ) * 1024**3,
    )


def size_probe_enabled() -> bool:
    """是否做下载前体积探测（``JIUWENSWARM_SIZE_PROBE=0`` 关闭）。"""
    return _cached(
        "size_probe",
        _env("JIUWENSWARM_SIZE_PROBE"),
        lambda raw: (raw or "1").strip().lower() not in {"0", "false", "no", "off"},
    )


def size_probe_timeout_seconds() -> int:
    return _cached(
        "size_probe_timeout",
        _env("JIUWENSWARM_SIZE_PROBE_TIMEOUT_S"),
        lambda raw: _parse_positive_int(raw, DEFAULT_SIZE_PROBE_TIMEOUT_SECONDS),
    )


# ════════════════════════════════════════════════════════════════
# 主机判定
# ════════════════════════════════════════════════════════════════


def _host_of(url_or_host: str) -> str:
    text = (url_or_host or "").strip()
    if "://" in text:
        return (urlparse(text).hostname or "").casefold()
    return text.casefold()


def is_configured_hf_host(url_or_host: str) -> bool:
    """host 是否命中配置的 HF 站点（含 ``www.`` 变体）。"""
    host = _host_of(url_or_host)
    if not host:
        return False
    for base in hf_bases():
        base_host = _host_of(base)
        if host == base_host or host == "www." + base_host:
            return True
    return False


def is_approved_hf_url(value: Any) -> bool:
    """审计放行判定：配置的 HF 站点 ∪ ``*.huggingface.co``（含 CDN / datasets-server）。

    镜像返回的 parquet URL 之前会被 ``_approved_provider_url`` 静默丢弃，
    这里显式放行配置内的镜像域。
    """
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    host = parsed.hostname.casefold()
    if host == "huggingface.co" or host.endswith(".huggingface.co"):
        return True
    return is_configured_hf_host(host)


# ════════════════════════════════════════════════════════════════
# 多源候选展开
# ════════════════════════════════════════════════════════════════


def _swap_netloc(url: str, base: str) -> str:
    parsed = urlparse(url)
    target = urlparse(base)
    return urlunparse(parsed._replace(netloc=target.netloc))


def _variants_in_family(url: str, family: tuple[str, ...]) -> list[str] | None:
    """URL 的 host 属于该端点族时，返回「换 host、主源在前」的同路径候选。"""
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    if not host:
        return None
    for base in family:
        base_parsed = urlparse(base)
        if host == (base_parsed.hostname or "").casefold():
            variants: list[str] = []
            for candidate_base in family:
                swapped = _swap_netloc(url, candidate_base)
                if swapped not in variants:
                    variants.append(swapped)
            return variants
    return None


def download_variants(url: str) -> list[str]:
    """把一个数据文件 URL 展开成多源候选列表（主源在前）。

    ``https://huggingface.co/datasets/x/resolve/main/a.json``
      → ``["https://huggingface.co/...", "https://hf-mirror.com/..."]``

    不属于任何配置端点族的 URL（Zenodo、GitHub raw、模块二给的直链……）原样返回
    单元素列表——不为它们凭空造镜像，避免把请求引到不可信的第三方。
    """
    if not isinstance(url, str) or not url:
        return []
    for family in (hf_bases(), hf_parquet_bases()):
        if len(family) < 2:
            continue
        variants = _variants_in_family(url, family)
        if variants:
            return variants
    return [url]


# ════════════════════════════════════════════════════════════════
# 带重试的 JSON 拉取
# ════════════════════════════════════════════════════════════════


def fetch_json_with_retry(
    url: str,
    timeout_seconds: int,
    *,
    retries: int | None = None,
) -> Any:
    """拉 JSON，失败按指数退避重试（1s、2s…）。

    签名与 ``dataset_resolver._fetch_json`` 完全一致，可直接互换。

    只对**瞬时故障**重试：URLError（连接重置 / DNS / 超时）、HTTP 5xx、429。
    HTTP 4xx（除 429）是确定性拒绝——重试只是浪费时间并把错误信息变模糊，
    立即抛出。
    """
    attempts = download_retries() if retries is None else max(0, retries)
    last_exc: Exception | None = None
    for attempt in range(attempts + 1):
        try:
            return _fetch_json_once(url, timeout_seconds)
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code not in {429, 500, 502, 503, 504}:
                raise
        except Exception as exc:  # URLError / timeout / 解析失败之外的一切瞬时错误
            last_exc = exc
        if attempt < attempts:
            delay = _BACKOFF_BASE_SECONDS * (2 ** attempt)
            log.warning(
                "请求失败（%s），%.1fs 后重试 %d/%d: %s",
                type(last_exc).__name__, delay, attempt + 1, attempts, url,
            )
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc


def probe_size(url: str, timeout_seconds: int | None = None) -> int | None:
    """探测单个下载 URL 的字节数；探不到返回 ``None``（**绝不抛异常**）。

    先 ``HEAD`` 读 ``Content-Length``；服务器不支持 HEAD（405/501）时退化为
    ``GET`` + ``Range: bytes=0-0`` 读 ``Content-Range``。

    「探不到」与「超限」必须区分：未知不等于超限，所以这里一律返回 None 让调用方
    放行——体积闸门只在**确知**超阈值时才拦。

    这是下载前的预估闸门（见 :func:`max_auto_download_bytes`），用来避免白跑一场
    注定超时的超大下载。它**不替代**下载层的实际字节上限。
    """
    if not size_probe_enabled():
        return None
    timeout = timeout_seconds or size_probe_timeout_seconds()
    for method, headers in (
        ("HEAD", {}),
        # 有些对象存储不支持 HEAD；Range 只取第一个字节，代价极小
        ("GET", {"Range": "bytes=0-0"}),
    ):
        request = urllib.request.Request(
            url, method=method,
            headers={"User-Agent": "jiuwenswarm-experiment/1", **headers},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                if method == "HEAD":
                    length = response.headers.get("Content-Length")
                    if length and str(length).strip().isdigit():
                        return int(str(length).strip())
                    continue
                content_range = response.headers.get("Content-Range") or ""
                _, _, total = content_range.partition("/")
                if total.strip().isdigit():
                    return int(total.strip())
                length = response.headers.get("Content-Length")
                # 服务器忽略了 Range、返回了 200 全量 → Content-Length 就是全量大小
                if response.status == 200 and length and str(length).strip().isdigit():
                    return int(str(length).strip())
                return None
        except Exception as exc:  # 网络/权限/不支持 —— 都只是"探不到"
            log.debug("体积探测失败（%s）: %s", type(exc).__name__, url)
            continue
    return None


def _fetch_json_once(url: str, timeout_seconds: int) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "jiuwenswarm-experiment/1",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
        raw = response.read(MAX_METADATA_BYTES + 1)
    if len(raw) > MAX_METADATA_BYTES:
        raise ValueError("dataset registry response exceeded metadata size limit")
    return json.loads(raw.decode("utf-8"))


def fetch_json_over_bases(
    path_and_query: str,
    timeout_seconds: int,
    *,
    bases: tuple[str, ...] | None = None,
    fetch_json: Callable[[str, int], Any] | None = None,
) -> Any:
    """按端点列表逐个尝试同一路径，返回第一个成功的结果。

    ``path_and_query`` 形如 ``/api/datasets?search=foo``。全部端点都失败时抛最后
    一个异常——调用方的 provider_errors 才能记下真实原因，而不是一句「没找到」。
    """
    candidates = bases if bases is not None else hf_bases()
    fetch = fetch_json or fetch_json_with_retry
    last_exc: Exception | None = None
    for base in candidates:
        url = base.rstrip("/") + path_and_query
        try:
            return fetch(url, timeout_seconds)
        except Exception as exc:
            last_exc = exc
            log.warning("端点失败，尝试下一个: %s（%s）", url, type(exc).__name__)
    assert last_exc is not None
    raise last_exc
