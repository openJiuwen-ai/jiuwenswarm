# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Settings-backed model tools for Designer node agents."""

from __future__ import annotations

import base64
import logging
import mimetypes
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator

from jiuwenswarm.common.config import get_config, get_model_names, resolve_env_vars

logger = logging.getLogger(__name__)

# DeepSeek/Ark allow very large completions; keep a high practical ceiling so
# storyboard/plan JSON is not truncated mid-object (truncation → parse retry → slow).
# Thinking mode shares this budget with final content — we disable thinking below.
_DESIGNER_MAX_TOKENS_CAP = 65536
_DESIGNER_DEFAULT_MAX_TOKENS = 16384
_preferred_designer_model: ContextVar[str | None] = ContextVar(
    "preferred_designer_model",
    default=None,
)


@contextmanager
def use_preferred_designer_model(model_name: str | None) -> Iterator[None]:
    """Apply one UI-selected model to all Designer planning calls in this context."""
    normalized = str(model_name or "").strip() or None
    token = _preferred_designer_model.set(normalized)
    try:
        yield
    finally:
        _preferred_designer_model.reset(token)


def _clamp_max_tokens(value: int | None) -> int:
    try:
        n = int(value or 0)
    except (TypeError, ValueError):
        n = 0
    if n < 1:
        n = _DESIGNER_DEFAULT_MAX_TOKENS
    return max(256, min(_DESIGNER_MAX_TOKENS_CAP, n))


def _thinking_disabled_extra_body() -> dict[str, Any]:
    """Provider-neutral knobs so reasoning does not eat the output budget."""
    return {
        "thinking": {"type": "disabled"},
        "reasoning": {"enabled": False},
        "enable_thinking": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }


def _message_text(msg: Any) -> str:
    """Prefer visible content; fall back to reasoning_content if content is empty."""
    if msg is None:
        return ""
    text = str(getattr(msg, "content", None) or "").strip()
    if text:
        return text
    text = str(getattr(msg, "refusal", None) or "").strip()
    if text:
        return text
    # DeepSeek thinking models may put usable text only in reasoning_content
    # when the visible budget was exhausted — better than a total soft-fail.
    text = str(getattr(msg, "reasoning_content", None) or "").strip()
    if text:
        return text
    extra = getattr(msg, "model_extra", None)
    if isinstance(extra, dict):
        text = str(extra.get("reasoning_content") or "").strip()
        if text:
            return text
    return ""


# Process-wide latch: a 402 / insufficient-balance reply means later model
# calls in the *same* user action (Play / chat turn) fail closed without
# hammering the provider. Cleared at the next user-visible entry
# (``require_llm``) and after a successful model call so a recharged account
# recovers without restarting the server — same recovery model as Work/Code.
_chat_billing_block: str = ""
_chat_confirmed: bool = False

# Stable error codes for Designer LLM failures. Adapter/RPC layers map these
# onto the existing bootstrap_error / chat_error / runError UI surfaces —
# callers should raise DesignerLlmError rather than inventing local reports.
LLM_NOT_CONFIGURED = "LLM_NOT_CONFIGURED"
LLM_BILLING = "LLM_BILLING"
LLM_API_ERROR = "LLM_API_ERROR"
LLM_REQUIRED = "LLM_REQUIRED"
MEDIA_NOT_CONFIGURED = "MEDIA_NOT_CONFIGURED"


class DesignerLlmError(RuntimeError):
    """User-visible Designer chat-model failure.

    Nested helpers call ``call_model_tool`` bluntly and raise via
    ``model_text_or_raise``. RPC entry points catch this and map onto the
    existing bootstrap_error / chat_error / runError surfaces — no per-call
    try/catch/report in business logic.
    """

    def __init__(self, message: str, *, code: str = LLM_API_ERROR) -> None:
        super().__init__(message)
        self.code = str(code or LLM_API_ERROR)
        self.user_message = str(message or "Chat model request failed")[:500]

    @classmethod
    def from_call_result(cls, result: dict[str, Any] | None) -> "DesignerLlmError":
        payload = result if isinstance(result, dict) else {}
        detail = str(payload.get("error") or "Chat model request failed").strip()
        code = str(payload.get("code") or "").strip()
        if not code:
            if payload.get("unavailable") or is_chat_payment_block(detail):
                code = LLM_BILLING
            elif "not configured" in detail.lower() or "no models configured" in detail.lower():
                code = LLM_NOT_CONFIGURED
            else:
                code = LLM_API_ERROR
        if code == LLM_BILLING:
            message = (
                f"Chat model unavailable (billing / insufficient credit): {detail}"
            )
        elif code == LLM_NOT_CONFIGURED:
            message = (
                detail
                if detail
                else "Chat model is not configured. Configure a model in Settings before using Design."
            )
        else:
            message = f"Chat model request failed: {detail}"
        return cls(message, code=code)


def is_chat_payment_block(detail: object) -> bool:
    """True for HTTP 402 / insufficient balance on the chat model (not image or video)."""
    code = getattr(detail, "status_code", None)
    if code is None:
        resp = getattr(detail, "response", None)
        code = getattr(resp, "status_code", None) if resp is not None else None
    try:
        if int(code) == 402:
            return True
    except (TypeError, ValueError):
        pass
    low = str(detail or "").lower()
    return (
        "insufficient balance" in low
        or "insufficient_quota" in low
        or "payment required" in low
        or "error code: 402" in low
    )


def classify_llm_failure(detail: object) -> tuple[str, str]:
    """Return ``(code, user_message)`` for a chat-model failure detail."""
    text = str(detail or "").strip() or "Chat model request failed"
    if is_chat_payment_block(detail) or is_chat_payment_block(text):
        return LLM_BILLING, f"Chat model unavailable (billing / insufficient credit): {text}"[:500]
    low = text.lower()
    if (
        "not configured" in low
        or "no models configured" in low
        or "no chat model credentials" in low
    ):
        return LLM_NOT_CONFIGURED, text[:500]
    return LLM_API_ERROR, f"Chat model request failed: {text}"[:500]


def chat_model_billing_block() -> str:
    """Non-empty when the chat account is known to be unpaid / 402."""
    return _chat_billing_block


def clear_chat_model_billing_block() -> None:
    """Drop a prior 402 latch so the next model call can probe the provider again."""
    global _chat_billing_block
    _chat_billing_block = ""


def note_chat_model_unavailable(detail: object) -> bool:
    """Record a payment block. Returns True only for 402-style failures."""
    global _chat_billing_block
    if not is_chat_payment_block(detail):
        return False
    _chat_billing_block = str(detail or "402")[:300]
    return True


def llm_available() -> bool:
    """True when Settings has a usable chat model with credentials for Designer agents.

    A recorded 402 / insufficient balance makes this False so in-flight model
    calls fail closed; ``require_llm`` clears that latch on the next user entry.
    """
    if _chat_billing_block:
        return False
    try:
        from jiuwenswarm.common.utils import get_env_file
        from jiuwenswarm.dotenv_early import load_dotenv_runtime

        load_dotenv_runtime(dotenv_path=get_env_file(), override=False)
    except Exception:  # noqa: BLE001
        pass
    try:
        models = list_configured_models()
    except Exception:  # noqa: BLE001
        return False
    import os

    env_key = (os.environ.get("API_KEY") or os.environ.get("OPENAI_API_KEY") or "").strip()
    env_base = (os.environ.get("API_BASE") or os.environ.get("OPENAI_API_BASE") or "").strip()
    for m in models:
        if not isinstance(m, dict):
            continue
        if not str(m.get("id") or m.get("model_name") or "").strip():
            continue
        base = resolve_env_vars(str(m.get("api_base") or env_base or "")).strip()
        # Key may live only in env; list_configured_models does not always expose it.
        key = env_key
        if key and base and not base.startswith("https://example.com"):
            return True
        if base and not base.startswith("https://example.com") and env_key:
            return True
    return bool(env_key and env_base)


def require_llm() -> None:
    """Local credential-shape gate (Design analogue of Work/Code ``_has_valid_model_config``).

    Use only at user-visible RPC entry points (bootstrap / chat / Play) so a
    missing model fails before expensive graph work. Nested helpers should
    call ``call_model_tool`` bluntly and raise via ``model_text_or_raise`` —
    including billing/402, which Work/Code also surfaces on the model call
    rather than with a preflight network probe.

    Clears any prior in-process billing latch so a recharged account is
    re-probed on this user action without requiring a server restart.
    """
    clear_chat_model_billing_block()
    if not llm_available():
        raise DesignerLlmError(
            "Chat model is not configured. Configure a model in Settings before using Design.",
            code=LLM_NOT_CONFIGURED,
        )


def require_media_models(*, image: bool = False, video: bool = False) -> None:
    """Block Play when a requested media modality has no usable endpoint.

    Callers decide which modalities are needed. Checks the Settings > Agent
    generation slot (switch, URL / key / model, supported backend); credit and
    endpoint failures stay on the generation call.
    """
    from jiuwenswarm.server.runtime.designer.media_generation import generation_problem

    problems = [
        problem
        for kind, wanted in (("image", image), ("video", video))
        if wanted and (problem := generation_problem(kind))
    ]
    if not problems:
        return
    raise DesignerLlmError(
        "Media model config is incomplete: " + " ".join(problems),
        code=MEDIA_NOT_CONFIGURED,
    )


def model_text_or_raise(result: dict[str, Any] | None) -> str:
    """Return model text from ``call_model_tool``, or raise ``DesignerLlmError``."""
    if not isinstance(result, dict):
        raise DesignerLlmError("Chat model returned an empty response", code=LLM_API_ERROR)
    if result.get("unavailable") or result.get("ok") is False or result.get("fallback"):
        raise DesignerLlmError.from_call_result(result)
    text = str(result.get("text") or "").strip()
    if not text:
        raise DesignerLlmError("Chat model returned an empty response", code=LLM_API_ERROR)
    if text.startswith("[local-tool-fallback]"):
        raise DesignerLlmError(
            "Chat model credentials are missing; local fallback is disabled.",
            code=LLM_NOT_CONFIGURED,
        )
    return text


def list_configured_models() -> list[dict[str, Any]]:
    """Return models from Settings / config.yaml for agent tool use."""
    cfg = get_config() or {}
    models = cfg.get("models") or {}
    defaults = models.get("defaults")
    out: list[dict[str, Any]] = []
    if isinstance(defaults, list):
        for idx, entry in enumerate(defaults):
            if not isinstance(entry, dict):
                continue
            mcc = entry.get("model_client_config") or {}
            if not isinstance(mcc, dict):
                continue
            name = resolve_env_vars(str(mcc.get("model_name") or "")).strip()
            alias = resolve_env_vars(str(entry.get("alias") or "")).strip()
            if not name and not alias:
                continue
            out.append(
                {
                    "id": alias or name,
                    "model_name": name,
                    "alias": alias,
                    "api_base": resolve_env_vars(str(mcc.get("api_base") or "")),
                    "client_provider": str(mcc.get("client_provider") or "OpenAI"),
                    "is_default": bool(entry.get("is_default")),
                    "index": idx,
                }
            )
    if not out:
        # Fallback to env-backed default entry names.
        for name in get_model_names():
            out.append(
                {
                    "id": name,
                    "model_name": name,
                    "alias": "",
                    "api_base": "",
                    "client_provider": "OpenAI",
                    "is_default": False,
                    "index": 0,
                }
            )
    return out


def pick_default_model() -> dict[str, Any] | None:
    """Pick a chat model from Settings (prefer pro/reason when present)."""
    models = list_configured_models()
    if not models:
        return None
    defaults = [m for m in models if m.get("is_default")]
    pool = defaults or models
    for m in pool:
        nid = str(m.get("id") or "").lower()
        if "pro" in nid or "reason" in nid:
            return m
    return pool[0]


_MAX_VISION_IMAGE_BYTES = 6 * 1024 * 1024


def vision_user_content(
    prompt: str,
    images: list[str] | None = None,
) -> str | list[dict[str, Any]]:
    """OpenAI-compatible user content: text plus original reference images.

    Original files stay the visual authority. Callers should describe slots in
    ``prompt``; this helper does not caption or summarize the pictures.
    """
    blocks: list[dict[str, Any]] = [{"type": "text", "text": str(prompt or "")}]
    for raw in (images or [])[:3]:
        url = _image_data_uri(raw)
        if not url:
            continue
        blocks.append({"type": "image_url", "image_url": {"url": url}})
    if len(blocks) == 1:
        return str(prompt or "")
    return blocks


def _image_data_uri(raw: str) -> str | None:
    value = str(raw or "").strip()
    if not value:
        return None
    if value.startswith(("http://", "https://", "data:")):
        return value
    path = Path(value)
    if value.startswith("file:"):
        from urllib.parse import unquote, urlparse

        parsed = urlparse(value)
        pathname = unquote(parsed.path)
        if len(pathname) >= 3 and pathname[0] == "/" and pathname[2] == ":":
            pathname = pathname[1:]
        path = Path(pathname)
    try:
        if not path.is_file():
            return None
        size = path.stat().st_size
        if size <= 0 or size > _MAX_VISION_IMAGE_BYTES:
            return None
        mime, _ = mimetypes.guess_type(str(path))
        if not mime or not mime.startswith("image/"):
            mime = "image/png"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError:
        return None
    return f"data:{mime};base64,{encoded}"


async def call_model_tool(
    *,
    prompt: str,
    system: str,
    preferred_model: str | None = None,
    max_tokens: int = _DESIGNER_DEFAULT_MAX_TOKENS,
    images: list[str] | None = None,
) -> dict[str, Any]:
    """Call a configured chat model (OpenAI-compatible) as an agent tool."""
    global _chat_confirmed
    if _chat_billing_block:
        return {
            "ok": False,
            "unavailable": True,
            "code": LLM_BILLING,
            "error": _chat_billing_block,
            "model": None,
            "text": "",
        }
    # Ensure ~/.jiuwenswarm/config/.env is loaded (API_KEY / API_BASE).
    try:
        from jiuwenswarm.common.utils import get_env_file
        from jiuwenswarm.dotenv_early import load_dotenv_runtime

        load_dotenv_runtime(dotenv_path=get_env_file(), override=False)
    except Exception:  # noqa: BLE001
        pass

    max_tokens = _clamp_max_tokens(max_tokens)
    preferred_model = preferred_model or _preferred_designer_model.get()

    models = list_configured_models()
    chosen: dict[str, Any] | None = None
    if preferred_model:
        for m in models:
            if m.get("id") == preferred_model or m.get("model_name") == preferred_model:
                chosen = m
                break
    if chosen is None:
        chosen = pick_default_model()
    if chosen is None:
        return {
            "ok": False,
            "code": LLM_NOT_CONFIGURED,
            "error": "No models configured in Settings",
            "model": None,
            "text": "",
        }

    # Resolve credentials from defaults entry / env
    cfg = get_config() or {}
    defaults = (cfg.get("models") or {}).get("defaults") or []
    api_key = ""
    api_base = str(chosen.get("api_base") or "")
    model_name = str(chosen.get("model_name") or chosen.get("id") or "")
    if isinstance(defaults, list) and defaults:
        idx = int(chosen.get("index") or 0)
        if 0 <= idx < len(defaults) and isinstance(defaults[idx], dict):
            mcc = defaults[idx].get("model_client_config") or {}
            api_key = resolve_env_vars(str(mcc.get("api_key") or ""))
            if not api_base:
                api_base = resolve_env_vars(str(mcc.get("api_base") or ""))
            if not model_name:
                model_name = resolve_env_vars(str(mcc.get("model_name") or ""))
    if not api_key:
        import os

        api_key = (os.environ.get("API_KEY") or os.environ.get("OPENAI_API_KEY") or "").strip()
    if not api_base:
        import os

        api_base = (os.environ.get("API_BASE") or os.environ.get("OPENAI_API_BASE") or "").strip()
    api_base = resolve_env_vars(api_base)
    api_key = resolve_env_vars(api_key)
    model_name = resolve_env_vars(model_name)

    if not api_key or not api_base or api_base.startswith("https://example.com"):
        return {
            "ok": False,
            "fallback": False,
            "code": LLM_NOT_CONFIGURED,
            "error": (
                "No chat model credentials configured in Settings "
                "(API_KEY / API_BASE required)."
            ),
            "model": chosen.get("id"),
            "model_name": model_name,
            "text": "",
        }

    try:
        from openai import AsyncOpenAI

        async def _once(*, temperature: float, attempt: int) -> dict[str, Any]:
            # Large plans need more wall time than the old 90s default.
            client = AsyncOpenAI(api_key=api_key, base_url=api_base, timeout=1200.0)
            try:
                user_content = vision_user_content(prompt, images)
                request_input = {
                    "model": model_name,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user_content},
                    ],
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                    "extra_body": _thinking_disabled_extra_body(),
                }
                from jiuwenswarm.server.runtime.designer.trajectory import (
                    current_trajectory_span,
                )

                with current_trajectory_span(
                    action="agent_call",
                    phase="inference",
                    detail={
                        "agent_type": "chat_model",
                        "attempt": attempt,
                        "prompt": prompt,
                        "system_prompt": system,
                        "input": request_input,
                    },
                ) as span_payload:
                    resp = await client.chat.completions.create(**request_input)
                    choice = resp.choices[0] if resp.choices else None
                    msg = choice.message if choice is not None else None
                    text = _message_text(msg)
                    finish = str(getattr(choice, "finish_reason", None) or "")
                    span_payload["output"] = text
                    span_payload["finish_reason"] = finish or None
                    span_payload["response_model"] = resp.model
                    if resp.usage is not None:
                        span_payload["usage"] = {
                            "input_tokens": resp.usage.prompt_tokens,
                            "output_tokens": resp.usage.completion_tokens,
                        }
                if not text:
                    return {
                        "ok": False,
                        "fallback": False,
                        "error": "empty_model_response",
                        "model": chosen.get("id"),
                        "model_name": model_name,
                        "text": "",
                        "finish_reason": finish or None,
                        "max_tokens": max_tokens,
                    }
                return {
                    "ok": True,
                    "fallback": False,
                    "model": chosen.get("id"),
                    "model_name": model_name,
                    "text": text,
                    "finish_reason": finish or None,
                    "max_tokens": max_tokens,
                }
            finally:
                try:
                    await client.close()
                except Exception:  # noqa: BLE001
                    pass

        first = await _once(
            temperature=0.4,
            attempt=1,
        )
        if first.get("ok"):
            _chat_confirmed = True
            clear_chat_model_billing_block()
            return first
        if first.get("error") == "empty_model_response":
            logger.warning(
                "call_model_tool empty response model=%s finish=%s max_tokens=%s; retrying once",
                model_name,
                first.get("finish_reason"),
                max_tokens,
            )
            second = await _once(temperature=0.2, attempt=2)
            if second.get("ok"):
                _chat_confirmed = True
                clear_chat_model_billing_block()
            return second
        return first
    except Exception as exc:  # noqa: BLE001
        blocked = note_chat_model_unavailable(exc)
        code, _ = classify_llm_failure(exc)
        logger.warning("call_model_tool failed: %s", exc)
        return {
            "ok": False,
            "unavailable": blocked,
            "code": LLM_BILLING if blocked else code,
            "error": str(exc),
            "model": chosen.get("id"),
            "model_name": model_name,
            "text": "",
        }
