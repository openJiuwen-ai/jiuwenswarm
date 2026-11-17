# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Effective context-window denominator for Deep main chat.

Contract: repo-root ``方案_模型思考强度与上下文窗口.md`` §3.2 / §3.4;
server spec ``docs/specs/S_06_context-window-effective-denominator.md``.

Window keys must never enter ``model_config_obj`` (vendor request pollution).
Writes the session private slot ``_context_window_tokens`` (agent-core has no
public API this phase); seeds ``context_engine._config.context_window_tokens``
when the session context is not created yet.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

# External field names (must match the scheme / ChatSendParams / catalog entry).
CATALOG_CONTEXT_WINDOW_KEY = "context_window_tokens"
SELECTED_CONTEXT_WINDOW_KEY = "selected_context_window_tokens"


def parse_positive_window_tokens(value: Any) -> int | None:
    """Return a positive int token budget, or None if illegal (bool excluded)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value <= 0:
        return None
    return value


def resolve_effective_context_window(
    *,
    selected: Any,
    catalog_max: Any,
    ignore_selected: bool = False,
) -> tuple[int | None, str]:
    """Compose the effective window.

    Returns:
        ``(effective_or_none, source)`` where source is
        ``selected`` | ``catalog_max`` | ``none``.
    """
    if not ignore_selected:
        selected_tokens = parse_positive_window_tokens(selected)
        if selected_tokens is not None:
            return selected_tokens, "selected"
    catalog_tokens = parse_positive_window_tokens(catalog_max)
    if catalog_tokens is not None:
        return catalog_tokens, "catalog_max"
    return None, "none"


def catalog_window_from_entry(entry: Any) -> int | None:
    """Read sibling ``context_window_tokens`` only; ignore ``model_config_obj``."""
    if not isinstance(entry, dict):
        return None
    return parse_positive_window_tokens(entry.get(CATALOG_CONTEXT_WINDOW_KEY))


def resolve_catalog_window_tokens(
    *,
    params: Mapping[str, Any] | None,
    entry_by_ref: Mapping[str, Any],
    identity_to_keys: Mapping[str, list[str]] | None = None,
    normalize_ref: Callable[[str], str] | None = None,
) -> int | None:
    """Resolve catalog max from request identity — never via ``Model`` object identity.

    Lookup order:
    1. ``params.model_ref`` (normalized when ``normalize_ref`` is provided)
    2. unique ``params.model_name`` hit in ``identity_to_keys`` values
    3. unique ``is_default`` entry in ``entry_by_ref``
    """
    params = params if isinstance(params, Mapping) else {}
    model_ref = str(params.get("model_ref") or "").strip()
    if model_ref:
        candidates: list[str] = []
        if normalize_ref is not None:
            try:
                candidates.append(normalize_ref(model_ref))
            except Exception:
                # Invalid ref format: still try the raw string against the map.
                pass
        if model_ref not in candidates:
            candidates.append(model_ref)
        for ref in candidates:
            if ref in entry_by_ref:
                return catalog_window_from_entry(entry_by_ref.get(ref))

    model_name = str(params.get("model_name") or "").strip()
    if model_name and identity_to_keys:
        matches = [
            ref
            for ref, keys in identity_to_keys.items()
            if model_name in keys
        ]
        if len(matches) == 1:
            return catalog_window_from_entry(entry_by_ref.get(matches[0]))

    defaults = [
        entry
        for entry in entry_by_ref.values()
        if isinstance(entry, dict) and entry.get("is_default") is True
    ]
    if len(defaults) == 1 and not model_name and not model_ref:
        return catalog_window_from_entry(defaults[0])
    return None


def apply_context_window_to_session(
    *,
    context: Any | None,
    context_engine: Any | None,
    effective: int | None,
    snapshot: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Write or clear this session's compression-denominator override.

    Args:
        context: SessionModelContext, or None if not created yet.
        context_engine: Session ContextEngine; when ``context`` is missing, seed
            ``_config.context_window_tokens`` so a later ``create_context`` inherits.
        effective: Composed window; None clears this feature's override.
        snapshot: Prior override snapshot for this session; None if never overridden.

    Returns:
        Updated snapshot, or None when the session has no override left.
    """
    if effective is not None:
        if snapshot is None:
            snapshot = {
                "context_prev": (
                    getattr(context, "_context_window_tokens", None)
                    if context is not None
                    else None
                ),
                "engine_prev": _engine_config_window(context_engine),
                "seeded_engine": False,
            }
        if context is not None:
            current = getattr(context, "_context_window_tokens", None)
            if current != effective:
                # Private slot: agent-core has no public setter this phase.
                context._context_window_tokens = effective
        elif context_engine is not None:
            if _set_engine_config_window(context_engine, effective):
                snapshot["seeded_engine"] = True
        else:
            # Nowhere to write yet; keep intent for the next turn.
            snapshot["pending"] = effective
            return snapshot
        snapshot.pop("pending", None)
        # Keep engine in sync so a later recreate does not drop the override.
        if context is not None and context_engine is not None:
            if _set_engine_config_window(context_engine, effective):
                snapshot["seeded_engine"] = True
        return snapshot

    # effective is None → clear override (no sticky selected/catalog value).
    if snapshot is None:
        return None
    if context is not None:
        context._context_window_tokens = snapshot.get("context_prev")
    if snapshot.get("seeded_engine") and context_engine is not None:
        _set_engine_config_window(context_engine, snapshot.get("engine_prev"))
    return None


def _engine_config_window(context_engine: Any | None) -> int | None:
    if context_engine is None:
        return None
    config = getattr(context_engine, "_config", None)
    if config is None:
        return None
    return getattr(config, "context_window_tokens", None)


def _set_engine_config_window(context_engine: Any, value: int | None) -> bool:
    """Set engine config window; return False if config missing or assignment fails."""
    config = getattr(context_engine, "_config", None)
    if config is None:
        return False
    try:
        config.context_window_tokens = value
        return True
    except Exception:
        # Read-only config etc.: compression still relies on the context slot.
        return False


def run_request_context_window_apply(
    *,
    session_id: str,
    params: dict[str, Any] | None,
    catalog_max: int | None,
    context: Any | None,
    context_engine: Any | None,
    snapshots: dict[str, dict[str, Any]],
    model_name: str = "",
    log: Any | None = None,
) -> None:
    """One request: compose → write/clear slot → update ``snapshots``."""
    sid = str(session_id or "").strip()
    if not sid:
        return
    params = params if isinstance(params, dict) else {}
    ignore_selected = params.get("enable_routing") is True
    selected_raw = params.get(SELECTED_CONTEXT_WINDOW_KEY)
    effective, source = resolve_effective_context_window(
        selected=selected_raw,
        catalog_max=catalog_max,
        ignore_selected=ignore_selected,
    )
    _log = log
    if ignore_selected and selected_raw is not None and _log is not None:
        _log.info(
            "[ModelWindow] skipped selected window under enable_routing "
            "model=%s selected=%r",
            model_name,
            selected_raw,
        )

    prev_snap = snapshots.get(sid)
    new_snap = apply_context_window_to_session(
        context=context,
        context_engine=context_engine,
        effective=effective,
        snapshot=prev_snap,
    )
    if new_snap is None:
        snapshots.pop(sid, None)
    else:
        snapshots[sid] = new_snap

    if _log is None:
        return
    if effective is not None:
        pending = bool(new_snap and new_snap.get("pending") is not None)
        _log.info(
            "[ModelWindow] applied model=%s effective=%s source=%s pending=%s",
            model_name,
            effective,
            source,
            pending,
        )
    elif prev_snap is not None:
        _log.info(
            "[ModelWindow] reverted model=%s reason=no_effective_window",
            model_name,
        )
