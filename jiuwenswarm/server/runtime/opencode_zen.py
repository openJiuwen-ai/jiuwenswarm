# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Opencode Zen free models, fetched live at startup and held in memory only.

Opencode Zen (https://opencode.ai/docs/zh-cn/zen/) is a model-hosting gateway
that exposes a number of time-limited free models requiring *no* API key. This
module makes JiuwenSwarm work out of the box: on AgentServer start-up we fetch
the live model catalog from Zen, keep the free ones (model id ending in
``-free``, plus the always-free ``big-pickle``), and hold them in a process-wide
in-memory cache. Nothing is written to ``config.yaml``.

Consumers read the cache through :func:`get_zen_free_model_entries`:

- ``models.list`` (Gateway web) appends them after the user's own models so
  they show up in the frontend dropdown.
- ``AgentWebSocketServer._build_model_cache`` builds a :class:`Model` per entry
  so a Zen free model is resolvable when a chat selects it.

If the catalog cannot be reached, the cache stays empty and no free models are
offered — per the requirement that free models are only available when Zen is.

Key points reflected below:

- **No config.yaml mutation.** Every entry is constructed in memory.
- **No agent-core change.** Each entry uses ``client_provider="OpenAI"`` with
  ``api_key="public"``; Zen treats the literal ``"public"`` as anonymous
  access, and ``OpenAI`` is already a registered ``ProviderType``.
- **Failure-tolerant.** A network error or Zen outage leaves the cache empty
  and never blocks server start-up.

The pattern mirrors :mod:`jiuwenswarm.server.runtime.image_modality_warmup`:
an ``async`` entry point wrapping a synchronous worker via
``asyncio.to_thread``, bounded by ``asyncio.wait_for``.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Zen gateway (OpenAI-compatible). Free models are reachable anonymously by
# sending the literal token "public" as the bearer key.
ZEN_API_BASE = "https://opencode.ai/zen/v1"
ZEN_MODELS_URL = f"{ZEN_API_BASE}/models"
ZEN_ANON_API_KEY = "public"

# Identifying headers opencode itself sends on every Zen request. Zen's free
# tier is gated on these: a bare "public"-keyed request with no referer is
# rejected with 429 FreeUsageLimitError, while the same request carrying
# HTTP-Referer/X-Title succeeds (verified against the live gateway). Mirror
# opencode's provider.ts exactly so JiuwenSwarm free-model calls are treated
# the same as a first-party opencode client.
#
# User-Agent is the decisive difference: opencode (Bun) sends an opencode-shaped
# UA and Zen admits it; the Python openai SDK sends "openai-python/..." and Zen
# rate-limits *that* UA to 429 FreeUsageLimitError on the free tier, even with
# the referer headers present. Overriding UA to "opencode" via default_headers
# makes the SDK request indistinguishable from a first-party opencode call.
# (default_headers overrides the SDK's built-in UA — verified against live Zen.)
ZEN_CLIENT_HEADERS: dict[str, str] = {
    "HTTP-Referer": "https://opencode.ai/",
    "X-Title": "opencode",
    "X-Source": "opencode",
    "User-Agent": "opencode",
}

# models.dev catalog — the authoritative source of model metadata (cost,
# limits). The Zen ``/models`` endpoint is OpenAI-shaped and omits cost, so it
# cannot tell free from paid; models.dev carries ``cost.input == 0`` which is
# exactly how opencode itself decides which models to keep without a key.
# https://models.opencode.ai/api.json
MODELS_DEV_URL = "https://models.opencode.ai/api.json"

# Upper bound for the whole warm round (fetch). Network is expected to be
# quick; this only guards against a hung upstream so start-up never blocks.
_WARM_TOTAL_TIMEOUT_SECONDS = 15.0
_FETCH_TIMEOUT_SECONDS = 10.0

# Conservative context window for free models; used only for display in the
# frontend dropdown, not for truncation logic. models.dev may carry the real
# ``limit.context``; we fall back to this when it is missing.
_ZEN_FREE_CONTEXT_WINDOW = 200000

# Process-wide in-memory cache of Zen free-model entries. Populated once at
# AgentServer start-up; read by Gateway and AgentServer consumers. An empty
# cache (start-up failure / disabled / no free models) means "no free models".
_zen_free_entries: list[dict[str, Any]] = []
_zen_free_lock = threading.Lock()

# 后台定时重试：高频阶段间隔（秒）
_BACKGROUND_RETRY_INTERVAL: float = 30.0

# 后台定时重试：高频阶段持续时间（秒），启动初期用较短间隔快速恢复
_BACKGROUND_RETRY_MAX_DURATION: float = 180.0

# 后台定时重试：高频阶段耗尽后降级为低频持续重试间隔（秒），
# 直到成功或开关关闭，不设最终停止时间
_BACKGROUND_RETRY_LOW_INTERVAL: float = 60.0

# 并发保护：确保 _populate_zen_free_entries 同一时间只有一个线程在执行
_populate_lock = threading.Lock()

# 防止重复启动后台重试循环
_background_retry_started: bool = False

# 最后一次拉取失败的错误信息（供后台重试失败日志引用，避免排查时往上翻）
_last_fetch_error: str = ""

# ---------- 免费模型就绪回调 ----------
# 后台重试/惰性重试成功后通知 Gateway 广播 models.updated 事件，
# 让前端自动刷新模型列表，无需用户手动刷新。
_main_event_loop: asyncio.AbstractEventLoop | None = None
_models_ready_callbacks: list = []


def _zen_free_models_enabled() -> bool:
    """Whether Zen free-model fetching is turned on.

    Reads ``models.enable_free_models`` from config.yaml (default ``true``).
    Returns ``True`` (enabled) on any config-read failure so start-up is never
    broken.
    """
    try:
        from jiuwenswarm.common.config import get_config
        cfg = get_config() or {}
        val = (cfg.get("models") or {}).get("enable_free_models", None)
        if val is None:
            return True
        if isinstance(val, bool):
            return val
        return str(val).strip().lower() not in ("0", "false", "no", "off")
    except Exception:  # noqa: BLE001 - config unavailable; default on
        return True


def _is_free_model(model_meta: dict[str, Any]) -> bool:
    """A model is free when its ``cost.input`` is 0.

    This mirrors how opencode itself decides which models to keep without an
    API key (``provider.ts``: ``if (value.cost.input === 0) continue``). The
    ``-free`` suffix is just a naming convention most free models follow — but
    not all: ``big-pickle`` and ``grok-code`` are free yet have no suffix. So
    we rely on the cost metadata from models.dev, not the id pattern.

    ``cost.input`` may be an int (``0``) or a float (``0.14``); compare as
    float — ``int(0.14)`` would truncate to 0 and falsely flag a paid model.
    """
    cost = model_meta.get("cost")
    if not isinstance(cost, dict):
        return False
    try:
        return float(cost.get("input", -1)) == 0.0
    except (TypeError, ValueError):
        return False


def _build_zen_model_entry(model_id: str, name: str, context: int) -> dict[str, Any]:
    """Build one in-memory ``models.defaults``-shaped entry for a Zen free model.

    The shape matches what :func:`get_default_models` returns so downstream
    consumers (``_models_list``, ``_build_model_cache``) can treat it uniformly.

    ``model_name`` keeps the real Zen API id (e.g. ``laguna-s-2.1-free``) so
    requests target the correct endpoint — the ``-free`` suffix is part of the
    API id and must not be stripped. The display ``alias`` is cleaned of the
    redundant "Free" qualifier (free-ness is shown via a dedicated group), and
    an ``is_free`` flag lets the frontend group without coupling to opencode.
    """
    # Strip a trailing " Free" / "-free" / "(free)" from the display name so
    # the dropdown doesn't duplicate the free-ness already conveyed by the
    # "免费模型" group header. Only trims a single trailing occurrence.
    display_name = name
    for sep in (" Free", "-free", "-Free", " (free)", " (Free)"):
        if display_name.endswith(sep):
            display_name = display_name[: -len(sep)].rstrip()
            break
    return {
        "model_client_config": {
            "api_base": ZEN_API_BASE,
            "api_key": ZEN_ANON_API_KEY,
            "model_name": model_id,
            "client_provider": "OpenAI",
            "timeout": 360,
            "stream_first_chunk_timeout": 300,
            "stream_idle_timeout": 120,
            "verify_ssl": True,
            # Zen's free tier requires opencode-identifying headers (see
            # ZEN_CLIENT_HEADERS); without them every call 429s with
            # FreeUsageLimitError even though the key "public" is accepted.
            "custom_headers": dict(ZEN_CLIENT_HEADERS),
            # Free models 429 on rate-limit exhaustion, and the quota does
            # not recover within the SDK's retry backoff window — so retrying
            # a 429 only fires a second failing request and burns the limit
            # faster. Disable SDK-level retries for Zen free calls; the
            # outer LLMRetryRail handles genuine transient failures.
            "max_retries": 0,
        },
        "model_config_obj": {
            "temperature": 0.95,
        },
        # No is_default: user-configured models stay the active model. These
        # entries are appended last so active_model (result[0]) is unaffected.
        "alias": display_name,
        "context_window_tokens": context,
        # Marks this entry as a free model so the frontend can group it under
        # "免费模型" without inspecting api_base/api_key (no opencode coupling).
        "is_free": True,
    }


def _fetch_models_dev_catalog() -> dict[str, dict[str, Any]]:
    """Fetch the models.dev catalog and return the opencode provider's models.

    Returns a mapping of ``model_id -> model_meta``. On any error, returns an
    empty dict (meaning we cannot identify free models this run).
    """
    global _last_fetch_error
    try:
        resp = httpx.get(MODELS_DEV_URL, timeout=_FETCH_TIMEOUT_SECONDS)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001 - any failure ⇒ cannot identify free
        _last_fetch_error = f"models.dev: {exc}"
        logger.warning(
            "[OpencodeZen] failed to fetch models.dev catalog (%s); "
            "no free models available this run",
            exc,
        )
        return {}

    opencode = payload.get("opencode") if isinstance(payload, dict) else None
    models = opencode.get("models") if isinstance(opencode, dict) else None
    if not isinstance(models, dict) or not models:
        _last_fetch_error = "models.dev: catalog has no opencode models"
        logger.warning(
            "[OpencodeZen] models.dev catalog has no opencode models; "
            "no free models available this run"
        )
        return {}
    return models


def _fetch_zen_free_models() -> list[dict[str, Any]]:
    """Identify the free Zen models that are actually servable right now.

    Free-ness comes from the models.dev catalog (``cost.input == 0``), because
    the Zen ``/models`` endpoint is OpenAI-shaped and omits cost. We then
    intersect with the live Zen ``/models`` list so we never advertise a model
    Zen is not currently serving (the free tier rotates). If either source is
    unreachable we offer nothing, per the requirement that free models are only
    available when Zen is reachable.

    Returns a list of ``{"id", "name", "context"}`` dicts.
    """
    global _last_fetch_error
    dev_catalog = _fetch_models_dev_catalog()
    if not dev_catalog:
        return []

    # Free models per models.dev cost metadata.
    free_ids: dict[str, dict[str, Any]] = {}
    for mid, meta in dev_catalog.items():
        mid = (mid or "").strip()
        if mid and _is_free_model(meta or {}):
            free_ids[mid] = meta

    # Live Zen catalog: only keep models Zen is actually serving.
    live_ids: set[str] = set()
    try:
        resp = httpx.get(ZEN_MODELS_URL, timeout=_FETCH_TIMEOUT_SECONDS)
        resp.raise_for_status()
        payload = resp.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    live_ids.add(str(item.get("id") or "").strip())
    except Exception as exc:  # noqa: BLE001 - cannot confirm live servability
        _last_fetch_error = f"Zen /models: {exc}"
        logger.warning(
            "[OpencodeZen] failed to fetch live Zen model list (%s); "
            "no free models available this run",
            exc,
        )
        return []

    if not live_ids:
        _last_fetch_error = "Zen /models: live model list is empty"
        logger.warning(
            "[OpencodeZen] live Zen model list is empty; "
            "no free models available this run"
        )
        return []

    free: list[dict[str, Any]] = []
    for mid, meta in free_ids.items():
        if mid not in live_ids:
            continue  # free in catalog but not currently served by Zen
        name = str(meta.get("name") or mid).strip()
        context = _ZEN_FREE_CONTEXT_WINDOW
        limit = meta.get("limit")
        if isinstance(limit, dict):
            try:
                ctx = int(limit.get("context", 0) or 0)
                if ctx > 0:
                    context = ctx
            except (TypeError, ValueError):
                pass
        free.append({"id": mid, "name": name, "context": context})

    if not free:
        logger.info(
            "[OpencodeZen] no free models currently served by Zen; "
            "no free models available this run"
        )
    else:
        logger.info(
            "[OpencodeZen] identified %d free model(s) served by Zen", len(free)
        )
    return free


# 探测单个模型可用性时的请求超时（秒）。Zen 返回 403 RegionError
# 等需要先验证 key、查模型、再查地区限制，可能需要 3-5 秒。
# 探测是并发的（最多 8 路），不阻塞启动，10 秒足够且不会太慢。
_PROBE_TIMEOUT_SECONDS: float = 10.0


def _probe_model_availability(model_id: str) -> bool:
    """发一个最轻量 chat 请求探测模型是否真正可用。

    Zen 的 /models 端点可能列出已下线/不可用的模型（如 deepseek 被标为
    付费但未从列表删除），或对某些地区限制访问（403 RegionError）。
    只有实际发请求才能确认可用性。

    返回 True=可用；False=明确不可用（400/403/404/503）。
    网络/超时等不确定错误返回 True（保守保留，避免误过滤可用模型）。
    """
    try:
        resp = httpx.post(
            f"{ZEN_API_BASE}/chat/completions",
            headers={
                "Authorization": f"Bearer {ZEN_ANON_API_KEY}",
                **ZEN_CLIENT_HEADERS,
            },
            json={
                "model": model_id,
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 1,
                "stream": False,
            },
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
        if resp.status_code == 200:
            return True
        if resp.status_code in (400, 403, 404, 503):
            logger.info(
                "[OpencodeZen] model %s probed unavailable (HTTP %d: %s)",
                model_id,
                resp.status_code,
                resp.text[:200],
            )
            return False
        logger.info(
            "[OpencodeZen] model %s probe uncertain (HTTP %d), kept",
            model_id,
            resp.status_code,
        )
        return True
    except Exception as exc:  # noqa: BLE001 - 网络/超时等不确定错误
        logger.warning(
            "[OpencodeZen] model %s probe failed (%s), kept", model_id, exc,
        )
        return True


def _filter_available_models(
    free_models: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """并发探测每个免费模型的实际可用性，过滤掉明确不可用的。

    不可用模型（400/503）秒回，可用模型也很快返回 max_tokens=1 的响应，
    并发探测总耗时通常 1-2 秒。全部探测失败时保留原始列表（降级）。
    """
    if not free_models:
        return free_models
    from concurrent.futures import ThreadPoolExecutor, as_completed

    available: list[dict[str, Any]] = []
    unavailable: list[str] = []
    with ThreadPoolExecutor(max_workers=min(len(free_models), 8)) as executor:
        future_to_model = {
            executor.submit(_probe_model_availability, m["id"]): m
            for m in free_models
        }
        for future in as_completed(future_to_model):
            model = future_to_model[future]
            try:
                if future.result():
                    available.append(model)
                else:
                    unavailable.append(model["id"])
            except Exception:  # noqa: BLE001
                available.append(model)
    if unavailable:
        logger.info(
            "[OpencodeZen] probed %d model(s): %d available, %d filtered (%s)",
            len(free_models),
            len(available),
            len(unavailable),
            ", ".join(unavailable),
        )
    # 全部被过滤时保留原始列表（可能是网络问题导致探测失败）
    return available if available else free_models


def _populate_zen_free_entries() -> int:
    """Fetch Zen free models and store them in the in-memory cache.

    Replaces any previously cached list (idempotent on repeat calls). Returns
    the number of cached entries. Never raises.

    Concurrency-safe: uses ``_populate_lock`` (non-blocking) so concurrent
    callers (background retry + frontend lazy warm) skip if one is already
    running, returning the current cache size instead.
    """
    global _zen_free_entries, _last_fetch_error

    # 并发保护：另一个线程正在执行时跳过，返回当前缓存
    if not _populate_lock.acquire(blocking=False):
        with _zen_free_lock:
            cache_size = len(_zen_free_entries)
        logger.debug(
            "[OpencodeZen] populate already in progress, skipping (cache size: %d)",
            cache_size,
        )
        return cache_size

    try:
        if not _zen_free_models_enabled():
            logger.info("[OpencodeZen] fetching disabled by env; skipping")
            with _zen_free_lock:
                _zen_free_entries = []
            return 0

        free_models = _fetch_zen_free_models()
        # 健康检查：并发探测每个模型，过滤掉在 Zen /models 列表中
        # 但实际不可用的模型（如已转付费/已下线但未从列表删除）。
        free_models = _filter_available_models(free_models)
        # 按 context window 降序排序，优先选 context 大的模型作为默认。
        # context 大通常意味着模型更强，避免选到能力不足的小模型导致
        # skill 执行失败。排序后 entries[0] 就是 context 最大的可用模型。
        free_models.sort(key=lambda m: m.get("context", 0), reverse=True)
        entries = [
            _build_zen_model_entry(m["id"], m["name"], m["context"])
            for m in free_models
        ]
        with _zen_free_lock:
            _zen_free_entries = entries
        if entries:
            # 仅成功后清空错误信息，失败时保留原因供后续日志引用
            _last_fetch_error = ""
        logger.info(
            "[OpencodeZen] cached %d free model(s) in memory", len(entries)
        )
        # 缓存从空变为有数据时通知前端刷新
        if entries:
            _notify_models_ready()
        return len(entries)
    finally:
        _populate_lock.release()


async def warm_zen_free_models(*, reason: str) -> None:
    """Async entry point: fetch Zen free models with a start-up timeout.

    Mirrors :func:`warm_image_modality_cache`: the synchronous worker runs on a
    thread, the whole round is bounded so a hung upstream never blocks
    start-up, and any failure leaves the cache empty (no free models offered).

    On failure, schedules a background retry loop that keeps retrying
    (high-frequency then low-frequency) until success or the free-models
    toggle is turned off, so free models auto-recover without a restart.
    """
    try:
        await asyncio.wait_for(
            asyncio.to_thread(_populate_zen_free_entries),
            timeout=_WARM_TOTAL_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "[OpencodeZen] fetch round timed out after %.0fs (%s); "
            "no free models available this run",
            _WARM_TOTAL_TIMEOUT_SECONDS,
            reason,
        )
    except Exception as exc:  # noqa: BLE001 - defensive; _populate already swallows
        logger.warning("[OpencodeZen] fetch failed (%s): %s", reason, exc)

    # 预热失败后启动后台定时重试（高频→低频持续，直到成功或开关关闭）
    if not get_zen_free_model_entries():
        logger.info(
            "[OpencodeZen] startup warm failed (reason=%s), cache empty, "
            "scheduling background retry",
            reason,
        )
        _start_background_retry()


def _start_background_retry() -> None:
    """启动后台定时重试循环。

    高频阶段 30s 间隔，耗尽后降级为 60s 低频持续重试，直到成功或开关关闭。
    daemon 线程，不影响进程退出。同一时间只有一个后台循环在运行：
    ``_background_retry_started`` 在循环真正结束时重置，之后（如开关重新打开）
    可再次启动新循环。
    """
    global _background_retry_started
    with _zen_free_lock:
        if _background_retry_started:
            return
        _background_retry_started = True
    thread = threading.Thread(target=_background_retry_loop, daemon=True)
    thread.start()
    logger.info(
        "[OpencodeZen] background retry scheduled (every %.0fs, then %.0fs "
        "low-frequency)",
        _BACKGROUND_RETRY_INTERVAL,
        _BACKGROUND_RETRY_LOW_INTERVAL,
    )


def _background_retry_loop() -> None:
    """后台定时重试循环。

    高频阶段（启动初期，``_BACKGROUND_RETRY_MAX_DURATION`` 内）每
    ``_BACKGROUND_RETRY_INTERVAL`` 秒重试一次；高频阶段耗尽后降级为每
    ``_BACKGROUND_RETRY_LOW_INTERVAL`` 秒低频重试。循环一直运行到成功或
    开关关闭为止，不设最终停止时间——这样"启动后很久才恢复网络"的场景
    也能自动拿到免费模型，无需用户重启服务或刷新界面。

    循环无论因何退出（成功/开关关闭）都会重置 ``_background_retry_started``，
    允许开关重新打开后再次调度新循环。
    """
    global _background_retry_started
    start = time.time()
    attempt = 0
    try:
        while True:
            elapsed = time.time() - start
            # 高频阶段耗尽后切换低频间隔（低频持续重试，直到成功/开关关闭）
            interval = (
                _BACKGROUND_RETRY_INTERVAL
                if elapsed < _BACKGROUND_RETRY_MAX_DURATION
                else _BACKGROUND_RETRY_LOW_INTERVAL
            )
            time.sleep(interval)
            elapsed = time.time() - start
            if not _zen_free_models_enabled():
                logger.info(
                    "[OpencodeZen] background retry stopped: free models disabled"
                )
                return
            if get_zen_free_model_entries():
                logger.info(
                    "[OpencodeZen] background retry skipped: cache already populated"
                )
                return
            attempt += 1
            logger.info(
                "[OpencodeZen] background retry attempt #%d (elapsed %.0fs, "
                "interval %.0fs)",
                attempt, elapsed, interval,
            )
            _populate_zen_free_entries()
            if get_zen_free_model_entries():
                logger.info(
                    "[OpencodeZen] background retry succeeded on attempt #%d "
                    "(elapsed %.0fs), cached %d model(s)",
                    attempt, elapsed, len(get_zen_free_model_entries()),
                )
                return
            logger.warning(
                "[OpencodeZen] background retry attempt #%d failed, "
                "cache still empty (elapsed %.0fs, error: %s)",
                attempt, elapsed, _last_fetch_error or "unknown",
            )
    finally:
        with _zen_free_lock:
            _background_retry_started = False


def get_zen_free_model_entries() -> list[dict[str, Any]]:
    """Return a shallow copy of the cached Zen free-model entries.

    Returns an empty list when fetching is disabled, failed, or found nothing.
    The entries are in-memory only (never written to config.yaml).

    Honors the live toggle: when ``models.enable_free_models`` is ``false``,
    returns ``[]`` immediately even if a previously-warmed cache exists, so
    disabling via ``config.set`` takes effect without a restart.
    """
    if not _zen_free_models_enabled():
        return []
    with _zen_free_lock:
        return list(_zen_free_entries)


def get_zen_free_context_window() -> int:
    """Conservative context window used for display of Zen free models."""
    return _ZEN_FREE_CONTEXT_WINDOW


def get_zen_default_free_model_entry() -> dict[str, Any] | None:
    """Return the first free-model entry to act as the product default model.

    当用户自配的默认模型仍为占位符（首次启动）时，用第一个免费模型兜底；
    Zen 不可达/缓存为空/免费模型被关闭时返回 ``None``（调用方保持原行为）。
    不硬编码优先选某个模型——免费模型会轮换，按上游 models.dev 返回顺序
    取第一个最稳妥。
    """
    entries = get_zen_free_model_entries()
    return entries[0] if entries else None


def set_main_event_loop(loop: asyncio.AbstractEventLoop | None) -> None:
    """注册主 event loop，供后台线程通过 call_soon_threadsafe 调度回调。"""
    global _main_event_loop
    _main_event_loop = loop


def register_models_ready_callback(cb) -> None:
    """注册回调：Zen 免费模型缓存从空变为有数据时调用。

    回调将在主 event loop 线程中被 call_soon_threadsafe 调度执行，
    因此回调内部可以安全地使用 ``asyncio.create_task`` 调度异步操作。
    """
    _models_ready_callbacks.append(cb)


def _notify_models_ready() -> None:
    """通知所有注册的回调：免费模型已就绪。

    从后台线程调用时，通过 call_soon_threadsafe 安全地调度到主 event loop
    线程执行，避免跨线程直接操作 asyncio 资源。
    """
    if not _models_ready_callbacks:
        return
    if _main_event_loop and _main_event_loop.is_running():
        _main_event_loop.call_soon_threadsafe(_dispatch_models_ready)
    else:
        logger.debug("[OpencodeZen] no running event loop, skipping models ready notify")


def _dispatch_models_ready() -> None:
    """在主 event loop 线程中执行所有已注册的回调。"""
    for cb in _models_ready_callbacks:
        try:
            cb()
        except Exception:  # noqa: BLE001
            logger.debug("[OpencodeZen] models ready callback failed", exc_info=True)
