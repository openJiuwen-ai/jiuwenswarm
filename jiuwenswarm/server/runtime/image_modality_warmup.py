# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Warm the process-wide image-modality probe cache.

``DeepAgent._ensure_initialized`` warms native image support when
``enable_read_image_multimodal`` is left on auto. Native-image call sites read
the cache dynamically; when no verdict exists yet,
``schedule_image_support_probe`` fires a background probe (one LLM round-trip
carrying a tiny PNG) and image input stays disabled until a verdict is cached.

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
from dataclasses import dataclass
from typing import Any

from openjiuwen.core.foundation.llm import Model
from openjiuwen.harness.image_modality_probe import (
    probe_cache_key,
    probe_image_support,
    reset_image_support_cache,
    set_cached_image_support,
)

from jiuwenswarm.common.config import (
    get_config,
    get_configured_read_image_multimodal,
    get_default_models,
)

logger = logging.getLogger(__name__)

# Upper bound for one warm-up round. Each probe already carries its own 5s
# timeout (plus one retry without the vendor reasoning switches), and probes
# run concurrently, so this only guards against a model client that ignores
# its own deadline -- server start-up must never hang on a probe.
_WARMUP_TOTAL_TIMEOUT_SECONDS = 30.0


def _format_probe_key(key: tuple[str, str]) -> str:
    """Render a probe cache key for a log line."""
    api_base, model_name = key
    return f"api_base={api_base!r} model_name={model_name!r}"


def _entry_label(origin: str, index: int, entry: dict[str, Any]) -> str:
    """Name one configuration entry by its position in the file.

    The label identifies the entry as written. The key it resolves to is
    logged next to the label, because the two can differ.
    """
    label = f"{origin}[{index}]"
    alias = entry.get("alias")
    if alias:
        label = f"{label} alias={str(alias)!r}"
    return label


@dataclass
class _ProbeTarget:
    """One probe cache key and every configuration entry that resolved to it."""

    key: tuple[str, str]
    model: Model
    model_client_config: dict[str, Any]
    labels: list[str]
    declarations: list[bool]


def _declared_image_support(target: _ProbeTarget) -> bool | None:
    """Return the ``supports_vision`` verdict the operator declared, if any.

    Args:
        target: The probe target and the entries that resolved to it.

    Returns:
        The declared verdict, or ``None`` when no entry declared one or when
        entries sharing the key contradict each other.
    """
    declared = set(target.declarations)
    if not declared:
        return None
    if len(declared) == 1:
        return declared.pop()
    logger.warning(
        "[ImageModalityWarmup] %s: entries sharing this target declare conflicting "
        "supports_vision values (%s); probing instead of trusting either",
        _format_probe_key(target.key),
        ", ".join(target.labels),
    )
    return None


def _enumerate_probe_targets(config_base: dict[str, Any]) -> dict[tuple[str, str], _ProbeTarget]:
    """Group every configured model entry by the probe cache key it resolves to.

    Each entry is logged with the key it produced. The start-up log therefore
    shows which entries were read, and which of them landed on a key that an
    earlier entry had already claimed.

    Args:
        config_base: The resolved ``config.yaml`` mapping.

    Returns:
        The probe targets, keyed by cache key, in configuration order.
    """
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        build_model_from_entry,
    )

    entries = [entry for entry in get_default_models(config_base) if isinstance(entry, dict)]
    logger.info("[ImageModalityWarmup] enumerating %d configured model entry(ies)", len(entries))

    targets: dict[tuple[str, str], _ProbeTarget] = {}
    counters = {"models.defaults": 0, "models.agentos": 0}
    for entry in entries:
        model_client_config = entry.get("model_client_config") or {}
        model_config_obj = entry.get("model_config_obj") or {}
        origin = (
            "models.agentos"
            if isinstance(model_config_obj, dict) and model_config_obj.get("_source") == "agentos"
            else "models.defaults"
        )
        # ``origin`` is always one of the two keys ``counters`` was built with, so
        # the default is unreachable. A bare subscript would leave that implicit.
        index = counters.get(origin, 0)
        counters[origin] = index + 1

        label = _entry_label(origin, index, entry)
        if not model_client_config.get("model_name"):
            logger.info("[ImageModalityWarmup] %s: no model_name, not probed", label)
            continue
        try:
            model = build_model_from_entry(model_client_config, model_config_obj)
        except Exception as exc:  # noqa: BLE001 - a bad entry must not stop the rest
            logger.warning("[ImageModalityWarmup] %s: unusable entry, not probed: %s", label, exc)
            continue
        key = probe_cache_key(model)
        if key is None:
            logger.warning("[ImageModalityWarmup] %s: no probe cache key, not probed", label)
            continue

        declared = model_client_config.get("supports_vision")
        existing = targets.get(key)
        if existing is None:
            targets[key] = _ProbeTarget(
                key=key,
                model=model,
                model_client_config=dict(model_client_config),
                labels=[label],
                declarations=[declared] if isinstance(declared, bool) else [],
            )
            logger.info("[ImageModalityWarmup] %s -> %s", label, _format_probe_key(key))
            continue

        existing.labels.append(label)
        if isinstance(declared, bool):
            existing.declarations.append(declared)
        logger.info(
            "[ImageModalityWarmup] %s -> %s (already claimed by %s)",
            label,
            _format_probe_key(key),
            existing.labels[0],
        )

    return targets


def _neutralize_contested_target(target: _ProbeTarget) -> bool:
    """Strip the per-entry request configuration from a contested target.

    Several entries that resolve to one cache key are probed once. At most one
    of their ``model_config_obj`` blocks could go out with that probe. The
    entries were written as different things, so any one block sent this way
    speaks for entries that did not ask for it.

    ``model_config_obj`` shapes the request body: sampling parameters,
    ``extra_body``, and the rest. ``model_client_config`` only states how to
    reach the endpoint, and every entry in this group agrees on the endpoint
    and on the model name. The target therefore keeps the connection settings
    of the first entry and sends no request configuration.

    Args:
        target: The probe target to rebuild in place.

    Returns:
        True when the target is still probeable.
    """
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        build_model_from_entry,
    )

    logger.warning(
        "[ImageModalityWarmup] %d entries resolve to one probe target %s: %s. "
        "Probing it once, with no model_config_obj, so no entry's request "
        "configuration is sent on another entry's behalf.",
        len(target.labels),
        _format_probe_key(target.key),
        ", ".join(target.labels),
    )
    try:
        target.model = build_model_from_entry(target.model_client_config, {})
    except Exception as exc:  # noqa: BLE001 - a bad entry must not stop the rest
        logger.warning(
            "[ImageModalityWarmup] %s: cannot rebuild without request configuration, "
            "not probed: %s",
            _format_probe_key(target.key),
            exc,
        )
        return False
    return True


def _build_probe_models(config_base: dict[str, Any]) -> list[Model]:
    """Build one Model per distinct probe cache key in ``models.defaults``.

    The probe verdict is cached by ``(api_base, model_name)``. That key is
    computed after environment substitution, so entries written as different
    things can still land on one key. Such entries are probed once, and the
    probe then sends none of their request configuration. See
    :func:`_neutralize_contested_target`.

    Entries that declare ``supports_vision`` in ``model_client_config`` are not
    probed at all. The declared verdict is written into the cache instead.

    Args:
        config_base: The resolved ``config.yaml`` mapping.

    Returns:
        The models to probe, in configuration order.
    """
    models: list[Model] = []
    for target in _enumerate_probe_targets(config_base).values():
        # The declaration is read first. A declared target is never probed, so
        # its request configuration is never sent and there is nothing to
        # neutralize.
        declared = _declared_image_support(target)
        if declared is not None:
            set_cached_image_support(target.key, declared)
            logger.info(
                "[ImageModalityWarmup] %s image_input=%s (declared via supports_vision)",
                _format_probe_key(target.key),
                declared,
            )
            continue

        if len(target.labels) > 1 and not _neutralize_contested_target(target):
            continue

        models.append(target.model)
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

    if get_configured_read_image_multimodal(effective_config) is not None:
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
        label = _format_probe_key(key) if key is not None else "unknown target"
        if isinstance(verdict, BaseException):
            logger.warning(
                "[ImageModalityWarmup] probe failed for %s: %s",
                label,
                verdict,
            )
            continue
        logger.info(
            "[ImageModalityWarmup] %s image_input=%s (probed)",
            label,
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
