# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Warm the process-wide image-modality probe cache.

``DeepAgent._ensure_initialized`` resolves ``enable_read_image_multimodal``
when it is left on auto: a cached verdict is applied straight away, otherwise
``schedule_image_support_probe`` fires a background probe (one LLM round-trip
carrying a tiny PNG) and the current run degrades to metadata-only.

Two properties of that mechanism make the verdict unreliable in the agent
server unless it is warmed up here:

- The probe is an ``asyncio`` task, so it dies with the loop that scheduled
  it. The code adapter initializes the main agent inside a throwaway loop
  (``asyncio.run`` on a worker thread, see
  ``JiuwenSwarmCodeAdapter.create_instance``), so the probe scheduled there is
  cancelled before it can cache anything.
- ``create_subagent`` does not forward ``enable_read_image_multimodal``, so
  every sub-agent starts on auto and re-schedules the probe whenever the cache
  is still empty.

The net effect is a stray "what color is this image" request appearing at
sub-agent start-up. Probing once at server start (and again whenever the model
configuration changes) makes the cache authoritative, so agents read the
verdict instead of re-probing.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from openjiuwen.core.foundation.llm import Model
from openjiuwen.harness.image_modality_probe import (
    probe_cache_key,
    probe_image_support,
    reset_image_support_cache,
)

from jiuwenswarm.common.config import get_config, get_default_models

logger = logging.getLogger(__name__)

# Upper bound for one warm-up round. Each probe already carries its own 5s
# timeout (plus one retry without the vendor reasoning switches), and probes
# run concurrently, so this only guards against a model client that ignores
# its own deadline -- server start-up must never hang on a probe.
_WARMUP_TOTAL_TIMEOUT_SECONDS = 30.0


def _read_image_multimodal_is_explicit(config_base: dict[str, Any]) -> bool:
    """Return whether ``enable_read_image_multimodal`` is pinned in config.

    A pinned boolean short-circuits ``_resolve_read_image_multimodal`` in every
    agent, so probing would spend LLM calls on a verdict nobody reads.

    Args:
        config_base: The resolved ``config.yaml`` mapping.

    Returns:
        True when the switch carries an explicit boolean.
    """
    react_cfg = config_base.get("react") if isinstance(config_base, dict) else None
    if not isinstance(react_cfg, dict):
        return False
    return isinstance(react_cfg.get("enable_read_image_multimodal"), bool)


def _build_probe_models(config_base: dict[str, Any]) -> list[Model]:
    """Build one Model per distinct probe cache key in ``models.defaults``.

    The probe verdict is cached by ``(api_base, model_name)``, so entries that
    collapse onto the same key (an alias of an already-listed model, a repeated
    entry) are probed once.

    Args:
        config_base: The resolved ``config.yaml`` mapping.

    Returns:
        The models to probe, in configuration order.
    """
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        build_model_from_entry,
    )

    models: list[Model] = []
    seen_keys: set[tuple[str, str]] = set()
    for entry in get_default_models(config_base):
        if not isinstance(entry, dict):
            continue
        model_client_config = entry.get("model_client_config") or {}
        if not model_client_config.get("model_name"):
            continue
        try:
            model = build_model_from_entry(
                model_client_config,
                entry.get("model_config_obj") or {},
            )
        except Exception as exc:  # noqa: BLE001 - a bad entry must not stop the rest
            logger.warning(
                "[ImageModalityWarmup] skipping unusable model entry %s: %s",
                model_client_config.get("model_name"),
                exc,
            )
            continue
        key = probe_cache_key(model)
        if key is None or key in seen_keys:
            continue
        seen_keys.add(key)
        models.append(model)
    return models


async def warm_image_modality_cache(
    config_base: dict[str, Any] | None = None,
    *,
    reason: str,
) -> None:
    """Probe every configured model once and cache the verdicts.

    Never raises and never leaves the caller hanging: probe failures are
    swallowed by ``probe_image_support`` itself, and the whole round is bounded
    by :data:`_WARMUP_TOTAL_TIMEOUT_SECONDS`.

    Args:
        config_base: The resolved ``config.yaml`` mapping. Read from
            ``get_config()`` when omitted.
        reason: Short tag for the log line ("startup" / "model config change").
    """
    effective_config = config_base if isinstance(config_base, dict) else get_config()

    if _read_image_multimodal_is_explicit(effective_config):
        logger.info(
            "[ImageModalityWarmup] skipped (%s): "
            "react.enable_read_image_multimodal is set explicitly",
            reason,
        )
        return

    models = _build_probe_models(effective_config)
    if not models:
        logger.info("[ImageModalityWarmup] skipped (%s): no probeable model configured", reason)
        return

    logger.info("[ImageModalityWarmup] probing %d model(s) (%s)", len(models), reason)
    try:
        verdicts = await asyncio.wait_for(
            asyncio.gather(
                *(probe_image_support(model) for model in models),
                return_exceptions=True,
            ),
            timeout=_WARMUP_TOTAL_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "[ImageModalityWarmup] probe round timed out after %.0fs (%s); "
            "agents fall back to probing on demand",
            _WARMUP_TOTAL_TIMEOUT_SECONDS,
            reason,
        )
        return

    for model, verdict in zip(models, verdicts):
        key = probe_cache_key(model)
        if isinstance(verdict, BaseException):
            logger.warning(
                "[ImageModalityWarmup] probe failed for %s: %s",
                key,
                verdict,
            )
            continue
        logger.info(
            "[ImageModalityWarmup] %s image_input=%s",
            key,
            "unknown (not cached)" if verdict is None else verdict,
        )


async def refresh_image_modality_cache(
    config_base: dict[str, Any] | None = None,
    *,
    reason: str,
) -> None:
    """Drop cached verdicts and re-probe the currently configured models.

    Used after a model configuration change: an entry may now point at a
    different endpoint, key or backend behind the same ``(api_base,
    model_name)``, so a stale verdict must not survive.

    Args:
        config_base: The resolved ``config.yaml`` mapping. Read from
            ``get_config()`` when omitted.
        reason: Short tag for the log line.
    """
    reset_image_support_cache()
    await warm_image_modality_cache(config_base, reason=reason)


__all__ = [
    "warm_image_modality_cache",
    "refresh_image_modality_cache",
]
