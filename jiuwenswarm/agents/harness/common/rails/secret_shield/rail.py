# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Secret-shield rail: tokenize observed secrets at the tool-result gate.

Gates:

- ``after_tool_call``: observe candidates, tokenize plaintext for the model view.
- ``before_tool_call``: resolve token references to plaintext before execution,
  or reject the call when a token can no longer be resolved.
- ``after_model_call``: mask user-visible text; never touch tool arguments.

Every gate is fail-open: a protection error is logged and the turn continues.
"""

from __future__ import annotations

import json
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from openjiuwen.core.foundation.llm import ToolMessage
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.agents.harness.common.rails.secret_shield.config import (
    SecretShieldConfig,
    load_secret_shield_config,
)
from jiuwenswarm.agents.harness.common.rails.secret_shield.detect import (
    MAX_CANDIDATES,
    collect_candidates,
)
from jiuwenswarm.agents.harness.common.rails.secret_shield.tokenize import (
    TOKEN_PATTERN,
    TOKEN_UNRESOLVED_NOTICE,
    mask_text,
    substitute_tokens,
    token_hints,
    tokenize_text,
)
from jiuwenswarm.agents.harness.common.rails.secret_shield.vault import (
    SecretEntry,
    SecretVault,
)
from jiuwenswarm.common.config import get_config
from jiuwenswarm.common.utils import logger

MAX_SCAN_CHARS = 256 * 1024
MAX_SESSIONS = 256


def _load_config() -> SecretShieldConfig:
    try:
        section = (get_config() or {}).get("secret_shield") or {}
    except Exception:  # noqa: BLE001 - config is optional in embedded runs
        section = {}
    return load_secret_shield_config(section)


def _tool_result_text(result: Any) -> str:
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(result)


def _args_text(args: Any) -> str:
    if args is None:
        return ""
    if isinstance(args, str):
        return args
    try:
        return json.dumps(args, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(args)


def _set_tool_result(ctx: AgentCallbackContext, text: str) -> None:
    inputs = ctx.inputs
    if isinstance(inputs, dict):
        inputs["tool_result"] = text
        message = inputs.get("tool_msg")
    else:
        inputs.tool_result = text
        message = getattr(inputs, "tool_msg", None)
    if message is not None and hasattr(message, "content"):
        message.content = text


def _response_content(response: Any) -> Any:
    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        return response.get("content")
    if response is None:
        return None
    return getattr(response, "content", None)


def _mask_content_value(content: Any, vault: SecretVault) -> tuple[Any, list[str]]:
    """Mask text inside a response content value without flattening it.

    ``content`` may be a string or a block list; non-text blocks are preserved
    so tool calls, images and provider-native blocks survive the rewrite.
    """
    if isinstance(content, str):
        return mask_text(content, vault)
    if not isinstance(content, list):
        return content, []
    hits: list[str] = []
    changed = False
    items: list[Any] = []
    for item in content:
        if isinstance(item, str):
            masked, item_hits = mask_text(item, vault)
            changed = changed or masked != item
            hits.extend(item_hits)
            items.append(masked)
            continue
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            masked, item_hits = mask_text(item["text"], vault)
            if masked != item["text"]:
                item = {**item, "text": masked}
                changed = True
            hits.extend(item_hits)
            items.append(item)
            continue
        items.append(item)
    return (items if changed else content), hits


def _set_response_content(inputs: Any, response: Any, content: Any) -> None:
    if isinstance(response, dict):
        response["content"] = content
        return
    if response is not None and hasattr(response, "content"):
        response.content = content
        return
    if isinstance(inputs, dict):
        inputs["response"] = content
    else:
        inputs.response = content


def _session_id(ctx: AgentCallbackContext) -> str:
    session = getattr(ctx, "session", None)
    if session is not None:
        getter = getattr(session, "get_session_id", None)
        if callable(getter):
            try:
                sid = getter() or ""
            except Exception:  # noqa: BLE001 - session id is best effort
                sid = ""
            if sid:
                return str(sid)
        for attr in ("conversation_id", "id"):
            value = getattr(session, attr, "")
            if value:
                return str(value)
    inputs = ctx.inputs
    value = (
        inputs.get("conversation_id", "")
        if isinstance(inputs, dict)
        else getattr(inputs, "conversation_id", "")
    )
    return str(value or "")


def _substitute_value(value: Any, vault: SecretVault, resolved: list[str]) -> Any:
    if isinstance(value, str):
        replaced, fps = substitute_tokens(value, vault)
        resolved.extend(fps)
        return replaced
    if isinstance(value, dict):
        return {key: _substitute_value(item, vault, resolved) for key, item in value.items()}
    if isinstance(value, list):
        return [_substitute_value(item, vault, resolved) for item in value]
    return value


class SecretShieldRail(DeepAgentRail):
    """Tokenize observed secrets; mask tokens from user-visible text."""

    priority: int = 110

    def __init__(
        self,
        cfg: SecretShieldConfig | None = None,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        super().__init__()
        self.cfg = cfg or _load_config()
        self._clock = clock
        self._vaults: OrderedDict[str, SecretVault] = OrderedDict()

    # ------------------------------------------------------------------ #
    # Session-scoped vaults
    # ------------------------------------------------------------------ #
    def _vault_for(self, session_id: str) -> SecretVault:
        vault = self._vaults.get(session_id)
        if vault is None:
            # The floor keeps one full result batch addressable, otherwise the
            # first candidates would be evicted before the result is rewritten.
            capacity = max(self.cfg.vault_max_entries, MAX_CANDIDATES)
            if self._clock is None:
                vault = SecretVault(max_entries=capacity)
            else:
                vault = SecretVault(max_entries=capacity, clock=self._clock)
            self._vaults[session_id] = vault
            while len(self._vaults) > MAX_SESSIONS:
                self._vaults.popitem(last=False)
        else:
            self._vaults.move_to_end(session_id)
        return vault

    # ------------------------------------------------------------------ #
    # G1: tool-result gate
    # ------------------------------------------------------------------ #
    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        if not self.cfg.enabled:
            return
        try:
            self._handle_tool_result(ctx)
        except Exception as exc:  # noqa: BLE001 - fail open, never break the turn
            logger.warning("[SecretShield] tool-result gate failed: %s", exc)
            self._audit("internal_error", {"gate": "tool-result", "error": type(exc).__name__})

    def _handle_tool_result(self, ctx: AgentCallbackContext) -> None:
        inputs = ctx.inputs
        tool_name = str(getattr(inputs, "tool_name", "") or "").lower()
        text = _tool_result_text(getattr(inputs, "tool_result", None))
        if not text:
            return
        vault = self._vault_for(_session_id(ctx))
        candidates = collect_candidates(text[:MAX_SCAN_CHARS])
        new_entries: list[SecretEntry] = []
        for value in candidates:
            entry = vault.remember(value, defer_evict=True)
            if entry is not None:
                new_entries.append(entry)
        if new_entries:
            self._audit(
                "secret_detected",
                {
                    "tool": tool_name,
                    "count": len(new_entries),
                    "fps": [entry.fp for entry in new_entries],
                },
            )
        if self.cfg.tool_result_mode != "enforce":
            if candidates:
                self._audit("secret_observed", {"tool": tool_name, "count": len(candidates)})
            self._audit_evictions(vault)
            return
        tokenized, hits = tokenize_text(text, vault)
        evicted = self._audit_evictions(vault)
        if not hits:
            return
        if evicted:
            # Tokens issued for evicted entries stay resolvable for this result
            # only if their entries survived; report the loss explicitly.
            lost = [fp for fp in hits if vault.entry(fp) is None]
            if lost:
                self._audit("token_issued_then_evicted", {"fps": lost})
        tokenized += token_hints(hits)
        _set_tool_result(ctx, tokenized)
        self._audit(
            "tool_result_tokenized",
            {"tool": tool_name, "count": len(hits), "fps": hits},
        )

    # ------------------------------------------------------------------ #
    # G2: execution gate (reference detection; substitution is opt-in)
    # ------------------------------------------------------------------ #
    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        if not self.cfg.enabled or self.cfg.exec_gate_mode == "off":
            return
        try:
            self._handle_token_references(ctx)
        except Exception as exc:  # noqa: BLE001 - fail open, never break the turn
            logger.warning("[SecretShield] execution gate failed: %s", exc)
            self._audit("internal_error", {"gate": "execution", "error": type(exc).__name__})

    def _handle_token_references(self, ctx: AgentCallbackContext) -> None:
        inputs = ctx.inputs
        tool_name = str(getattr(inputs, "tool_name", "") or "").lower()
        args = getattr(inputs, "tool_args", None)
        fps = sorted(set(TOKEN_PATTERN.findall(_args_text(args))))
        if not fps:
            return
        self._audit("token_reference", {"tool": tool_name, "count": len(fps), "fps": fps})
        vault = self._vault_for(_session_id(ctx))
        unresolved = [fp for fp in fps if vault.entry(fp) is None]
        if unresolved:
            self._audit(
                "token_unresolvable",
                {
                    "tool": tool_name,
                    "count": len(unresolved),
                    "fps": unresolved,
                    "evicted": [fp for fp in unresolved if vault.was_evicted(fp)],
                },
            )
        if self.cfg.exec_gate_mode != "enforce":
            return
        if unresolved:
            # A token that no longer resolves would otherwise reach the tool as
            # a literal placeholder, so the call is refused with an explicit
            # protection notice instead of failing silently downstream.
            self._reject_tool(ctx, TOKEN_UNRESOLVED_NOTICE)
            self._audit("tool_rejected", {"tool": tool_name, "fps": unresolved})
            return
        resolved: list[str] = []
        replaced = _substitute_value(args, vault, resolved)
        if not resolved:
            return
        if isinstance(inputs, dict):
            inputs["tool_args"] = replaced
        else:
            inputs.tool_args = replaced
        self._audit(
            "token_substituted",
            {
                "tool": tool_name,
                "count": len(set(resolved)),
                "fps": sorted(set(resolved)),
            },
        )

    def _audit_evictions(self, vault: SecretVault) -> list[SecretEntry]:
        evicted = vault.evict_to_limit()
        if evicted:
            self._audit(
                "vault_evicted",
                {"count": len(evicted), "fps": [entry.fp for entry in evicted]},
            )
        return evicted

    @staticmethod
    def _reject_tool(ctx: AgentCallbackContext, message: str) -> None:
        inputs = ctx.inputs
        tool_call = (
            inputs.get("tool_call")
            if isinstance(inputs, dict)
            else getattr(inputs, "tool_call", None)
        )
        tool_call_id = getattr(tool_call, "id", "") if tool_call else ""
        ctx.extra["_skip_tool"] = True
        if isinstance(inputs, dict):
            inputs["tool_result"] = message
            inputs["tool_msg"] = ToolMessage(content=message, tool_call_id=tool_call_id)
        else:
            inputs.tool_result = message
            inputs.tool_msg = ToolMessage(content=message, tool_call_id=tool_call_id)

    # ------------------------------------------------------------------ #
    # G3: model-output gate (user-visible text only)
    # ------------------------------------------------------------------ #
    async def after_model_call(self, ctx: AgentCallbackContext) -> None:
        if not self.cfg.enabled or self.cfg.output_mode == "off":
            return
        try:
            self._handle_model_output(ctx)
        except Exception as exc:  # noqa: BLE001 - fail open, never break the turn
            logger.warning("[SecretShield] output gate failed: %s", exc)
            self._audit("internal_error", {"gate": "output", "error": type(exc).__name__})

    def _handle_model_output(self, ctx: AgentCallbackContext) -> None:
        inputs = ctx.inputs
        response = getattr(inputs, "response", None)
        content = _response_content(response)
        if content is None or content == "":
            return
        vault = self._vault_for(_session_id(ctx))
        masked, hits = _mask_content_value(content, vault)
        if not hits:
            return
        if self.cfg.output_mode != "enforce":
            self._audit("model_output_observed", {"count": len(hits), "fps": hits})
            return
        if masked == content:
            return
        _set_response_content(inputs, response, masked)
        self._audit("model_output_masked", {"count": len(hits), "fps": hits})

    # ------------------------------------------------------------------ #
    # Audit: fingerprints and counts only, never plaintext.
    # ------------------------------------------------------------------ #
    @staticmethod
    def _audit(event: str, fields: dict[str, Any]) -> None:
        payload = {"event": event, **fields}
        logger.info("[secret_shield] %s", json.dumps(payload, ensure_ascii=False))


__all__ = ["MAX_SCAN_CHARS", "SecretShieldRail"]
