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

# 用户自配的默认模型仍为 .env 占位符（首次启动）时，回退使用的免费模型。
# 已确认 deepseek-v4-flash-free 长期匿名免费服务（models.dev cost.input==0），
# 前端展示名为 "DeepSeek V4 Flash"。
DEFAULT_FREE_MODEL_ID = "deepseek-v4-flash-free"

# Process-wide in-memory cache of Zen free-model entries. Populated once at
# AgentServer start-up; read by Gateway and AgentServer consumers. An empty
# cache (start-up failure / disabled / no free models) means "no free models".
_zen_free_entries: list[dict[str, Any]] = []
_zen_free_lock = threading.Lock()


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
    try:
        resp = httpx.get(MODELS_DEV_URL, timeout=_FETCH_TIMEOUT_SECONDS)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001 - any failure ⇒ cannot identify free
        logger.warning(
            "[OpencodeZen] failed to fetch models.dev catalog (%s); "
            "no free models available this run",
            exc,
        )
        return {}

    opencode = payload.get("opencode") if isinstance(payload, dict) else None
    models = opencode.get("models") if isinstance(opencode, dict) else None
    if not isinstance(models, dict) or not models:
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
        logger.warning(
            "[OpencodeZen] failed to fetch live Zen model list (%s); "
            "no free models available this run",
            exc,
        )
        return []

    if not live_ids:
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


def _populate_zen_free_entries() -> int:
    """Fetch Zen free models and store them in the in-memory cache.

    Replaces any previously cached list (idempotent on repeat calls). Returns
    the number of cached entries. Never raises.
    """
    global _zen_free_entries
    if not _zen_free_models_enabled():
        logger.info("[OpencodeZen] fetching disabled by env; skipping")
        with _zen_free_lock:
            _zen_free_entries = []
        return 0

    free_models = _fetch_zen_free_models()
    entries = [
        _build_zen_model_entry(m["id"], m["name"], m["context"])
        for m in free_models
    ]
    with _zen_free_lock:
        _zen_free_entries = entries
    logger.info(
        "[OpencodeZen] cached %d free model(s) in memory", len(entries)
    )
    return len(entries)


async def warm_zen_free_models(*, reason: str) -> None:
    """Async entry point: fetch Zen free models with a start-up timeout.

    Mirrors :func:`warm_image_modality_cache`: the synchronous worker runs on a
    thread, the whole round is bounded so a hung upstream never blocks
    start-up, and any failure leaves the cache empty (no free models offered).
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
    """Return the free-model entry that may act as the product default model.

    当用户自配的默认模型仍为占位符（首次启动）时，用该免费模型兜底：优先
    ``DEFAULT_FREE_MODEL_ID``（DeepSeek V4 Flash），其次取第一个免费模型；
    Zen 不可达/缓存为空/免费模型被关闭时返回 ``None``（调用方保持原行为）。
    """
    entries = get_zen_free_model_entries()
    for entry in entries:
        if (entry.get("model_client_config") or {}).get("model_name") == DEFAULT_FREE_MODEL_ID:
            return entry
    return entries[0] if entries else None
