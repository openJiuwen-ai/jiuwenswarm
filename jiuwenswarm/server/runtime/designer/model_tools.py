# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Settings-backed model tools for Designer node agents."""

from __future__ import annotations

import base64
import ipaddress
import logging
import mimetypes
import os
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator
from urllib.parse import unquote, urlparse

from jiuwenswarm.common.config import get_config, get_default_models
from jiuwenswarm.common.reasoning_config import (
    SUPPORTED_REASONING_LEVELS,
    effective_endpoint_profile,
    reasoning_config_for_level,
    reasoning_level_options,
    resolve_endpoint_profile_override,
    resolve_sampling_override,
)

if TYPE_CHECKING:
    from openjiuwen.core.foundation.llm import AssistantMessage, Model

logger = logging.getLogger(__name__)

# DeepSeek/Ark allow very large completions; keep a high practical ceiling so
# storyboard/plan JSON is not truncated mid-object (truncation → parse retry → slow).
# Thinking mode shares this budget with final content — calls run at the
# model's lowest reasoning setting (see ``_minimal_reasoning``).
_DESIGNER_MAX_TOKENS_CAP = 65536
_DESIGNER_DEFAULT_MAX_TOKENS = 16384
# Large plans need more wall time than a chat turn.
_DESIGNER_REQUEST_TIMEOUT_SEC = 1200.0
# Profiles that name no dialect; core treats them as "infer from api_base".
_GENERIC_ENDPOINT_PROFILES = frozenset({"", "openai", "openai-compatible", "openai_compatible"})
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


def preferred_designer_model_ref(graph: dict[str, Any] | None) -> str:
    """Return the graph's chosen model, preferring a stable id over a shared name."""
    metadata = graph.get("metadata") if isinstance(graph, dict) else None
    if not isinstance(metadata, dict):
        return ""
    return str(metadata.get("model_id") or metadata.get("model_name") or "").strip()


def _clamp_max_tokens(value: int | None) -> int:
    try:
        n = int(value or 0)
    except (TypeError, ValueError):
        n = 0
    if n < 1:
        n = _DESIGNER_DEFAULT_MAX_TOKENS
    return max(256, min(_DESIGNER_MAX_TOKENS_CAP, n))


def _self_hosted_endpoint_profile(client_config: dict[str, Any]) -> str | None:
    """``vllm`` for an OpenAI-protocol endpoint addressed by IP, localhost or a dotless host.

    Core infers a dialect only from official vendor hosts; self-hosted vLLM /
    SGLang gateways need ``chat_template_kwargs`` to switch thinking off and
    the Settings UI has no field to declare them. An explicit endpoint_profile
    or an ``endpoint_profile_overrides`` entry always wins.
    """
    if str(client_config.get("client_provider") or "").strip().lower() == "anthropic":
        return None
    explicit = str(client_config.get("endpoint_profile") or "").strip().lower()
    if explicit not in _GENERIC_ENDPOINT_PROFILES:
        return None
    api_base = str(client_config.get("api_base") or "").strip()
    if not api_base or resolve_endpoint_profile_override(api_base):
        return None
    host = (urlparse(api_base if "://" in api_base else f"http://{api_base}").hostname or "").lower()
    if not host:
        return None
    if host == "localhost" or "." not in host:
        return "vllm"
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return None
    return "vllm"


def _minimal_reasoning(client_config: dict[str, Any], model_name: str) -> dict[str, str] | None:
    """Lowest reasoning setting the model accepts, as core's provider-neutral config.

    Reasoning shares ``max_tokens`` with the visible reply, so Designer calls
    turn it off where the model allows and otherwise pick the lowest level
    core lists for it. Core owns the capability table and the per-endpoint
    wire format; ``None`` means the model exposes no control at all.
    """
    from openjiuwen.core.foundation.llm import get_reasoning_capability

    provider = str(client_config.get("client_provider") or "OpenAI")
    api_base = str(client_config.get("api_base") or "")
    capability = get_reasoning_capability(
        provider=provider,
        model=model_name,
        protocol="anthropic" if provider.strip().lower() == "anthropic" else "openai",
        api_base=api_base,
        endpoint_profile=effective_endpoint_profile(api_base, client_config.get("endpoint_profile")),
    )
    options = set(reasoning_level_options(capability))
    for level in SUPPORTED_REASONING_LEVELS:
        # A bare "on" is a toggle without a lower setting to choose.
        if level != "on" and level in options:
            return reasoning_config_for_level(level)
    return None


def _message_text(msg: AssistantMessage) -> str:
    """Prefer visible content; fall back to reasoning_content if content is empty."""
    content = msg.content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("text"):
                parts.append(str(block["text"]))
        content = "".join(parts)
    text = str(content or "").strip()
    if text:
        return text
    # Thinking models that cannot be switched off may put usable text only in
    # reasoning_content when the visible budget was exhausted — better than a
    # total soft-fail.
    return str(msg.reasoning_content or "").strip()


def _exception_chain(exc: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _http_status(detail: object) -> int | None:
    """HTTP status of a provider error, looking through core's wrapping exceptions."""
    candidates = _exception_chain(detail) if isinstance(detail, BaseException) else iter([detail])
    for item in candidates:
        code = getattr(item, "status_code", None)
        if code is None:
            resp = getattr(item, "response", None)
            code = getattr(resp, "status_code", None) if resp is not None else None
        try:
            return int(code)
        except (TypeError, ValueError):
            continue
    return None


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
    if _http_status(detail) == 402:
        return True
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
    _load_runtime_env()
    try:
        models = list_configured_models()
    except Exception:  # noqa: BLE001
        return False
    for m in models:
        if not str(m.get("id") or m.get("model_name") or "").strip():
            continue
        if _has_credentials(_client_config_for(m)):
            return True
    return bool(_env_api_key() and _env_api_base())


def _load_runtime_env() -> None:
    """Ensure ~/.jiuwenswarm/config/.env is loaded (API_KEY / API_BASE)."""
    try:
        from jiuwenswarm.common.utils import get_env_file
        from jiuwenswarm.dotenv_early import load_dotenv_runtime

        load_dotenv_runtime(dotenv_path=get_env_file(), override=False)
    except Exception:  # noqa: BLE001
        pass


def _env_api_key() -> str:
    return (os.environ.get("API_KEY") or os.environ.get("OPENAI_API_KEY") or "").strip()


def _env_api_base() -> str:
    return (os.environ.get("API_BASE") or os.environ.get("OPENAI_API_BASE") or "").strip()


def _model_entries() -> list[dict[str, Any]]:
    """Settings chat-model entries with decrypted credentials (``get_default_models``)."""
    return [entry for entry in get_default_models(get_config() or {}) if isinstance(entry, dict)]


def _entry_configs(chosen: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(model_client_config, model_config_obj)`` of a listed model.

    The key / base fall back to API_KEY / API_BASE so env-only setups keep working.
    """
    entries = _model_entries()
    idx = int(chosen.get("index") or 0)
    entry = entries[idx] if 0 <= idx < len(entries) else {}
    mcc = entry.get("model_client_config")
    mco = entry.get("model_config_obj")
    client = dict(mcc) if isinstance(mcc, dict) else {}
    if not str(client.get("api_key") or "").strip():
        client["api_key"] = _env_api_key()
    if not str(client.get("api_base") or "").strip():
        client["api_base"] = str(chosen.get("api_base") or "").strip() or _env_api_base()
    return client, dict(mco) if isinstance(mco, dict) else {}


def _client_config_for(chosen: dict[str, Any]) -> dict[str, Any]:
    return _entry_configs(chosen)[0]


def _has_credentials(client_config: dict[str, Any]) -> bool:
    api_base = str(client_config.get("api_base") or "").strip()
    if not api_base or api_base.startswith("https://example.com"):
        return False
    return bool(str(client_config.get("api_key") or "").strip() or client_config.get("custom_headers"))


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

    Callers decide which modalities are needed. Checks Design's selected
    multimodal model (or the legacy environment slot when no catalogue is
    stored): URL / key / model and a supported backend. The agent tool switch
    is not consulted once a catalogue exists. Credit and endpoint failures
    stay on the generation call.
    """
    from jiuwenswarm.server.runtime.designer.media_generation import generation_problem

    problems: list[str] = []
    for kind, wanted in (("image", image), ("video", video)):
        problem = generation_problem(kind) if wanted else None
        if problem:
            problems.append(problem)
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


def _configured_model_matches(model: dict[str, Any], preferred_model: str) -> bool:
    """Match a catalog row without stacking boolean expressions in the caller."""
    stable_id = model.get("model_id")
    if stable_id and stable_id == preferred_model:
        return True
    if model.get("id") == preferred_model:
        return True
    return model.get("model_name") == preferred_model


def list_configured_models() -> list[dict[str, Any]]:
    """Return models from Settings / config.yaml for agent tool use.

    Entries come from ``get_default_models`` (``models.defaults``, the legacy
    single default or the env fallback, plus AgentOS backups); ``index``
    addresses that list. Credentials are never exposed here.
    """
    out: list[dict[str, Any]] = []
    for idx, entry in enumerate(_model_entries()):
        mcc = entry.get("model_client_config")
        if not isinstance(mcc, dict):
            continue
        name = str(mcc.get("model_name") or "").strip()
        alias = str(entry.get("alias") or "").strip()
        if not name and not alias:
            continue
        out.append(
            {
                "id": alias or name,
                "model_id": str(entry.get("model_id") or "").strip(),
                "model_name": name,
                "alias": alias,
                "api_base": str(mcc.get("api_base") or ""),
                "client_provider": str(mcc.get("client_provider") or "OpenAI"),
                "is_default": bool(entry.get("is_default")),
                "index": idx,
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
    """Call the Settings chat model through openjiuwen core as an agent tool.

    Core owns the wire protocol (OpenAI / Anthropic) and every endpoint's
    reasoning dialect. This layer keeps the Designer call policy: lowest
    reasoning setting, clamped ``max_tokens``, one retry on an empty reply,
    the 402 latch, and fail-closed result dicts (see ``model_text_or_raise``).
    """
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
    _load_runtime_env()

    max_tokens = _clamp_max_tokens(max_tokens)
    preferred_model = preferred_model or _preferred_designer_model.get()

    models = list_configured_models()
    chosen: dict[str, Any] | None = None
    if preferred_model:
        for m in models:
            # Stable id first: several custom rows can share model_name, and
            # ``id`` is only the alias or that shared name.
            if _configured_model_matches(m, preferred_model):
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

    client_config, request_config = _entry_configs(chosen)
    model_name = str(
        chosen.get("model_name") or chosen.get("id") or client_config.get("model_name") or ""
    )
    client_config["model_name"] = model_name
    self_hosted_profile = _self_hosted_endpoint_profile(client_config)
    if self_hosted_profile:
        client_config["endpoint_profile"] = self_hosted_profile
    if not _has_credentials(client_config):
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

    messages: list[dict[str, Any]] = []
    if str(system or "").strip():
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": vision_user_content(prompt, images)})
    # Endpoints with mandated sampling (Kimi) reject any other temperature;
    # the model's request config already carries the required values.
    fixed_sampling = resolve_sampling_override(client_config.get("api_base"), model_name) is not None

    try:
        model = _build_chat_model(client_config, request_config)
        reasoning = _minimal_reasoning(client_config, model_name)

        async def _once(*, temperature: float, attempt: int) -> dict[str, Any]:
            nonlocal reasoning
            request: dict[str, Any] = {
                "messages": messages,
                "max_tokens": max_tokens,
                "timeout": _DESIGNER_REQUEST_TIMEOUT_SEC,
            }
            if not fixed_sampling:
                request["temperature"] = temperature
            if reasoning is not None:
                request["reasoning"] = reasoning
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
                    "input": {"model": model_name, **request},
                },
            ) as span_payload:
                try:
                    msg = await model.invoke(**request)
                except Exception as exc:
                    if reasoning is None or _http_status(exc) != 400:
                        raise
                    # Unknown self-hosted gateways need an explicit
                    # endpoint_profile for core to pick their dialect.
                    logger.warning(
                        "call_model_tool: endpoint rejected reasoning=%s for model=%s (%s); "
                        "retrying without it. Set endpoint_profile on self-hosted gateways.",
                        reasoning,
                        model_name,
                        exc,
                    )
                    reasoning = None
                    request.pop("reasoning", None)
                    span_payload["reasoning_dropped"] = True
                    msg = await model.invoke(**request)
                text = _message_text(msg)
                finish = "" if msg.finish_reason in (None, "", "null") else str(msg.finish_reason)
                span_payload["output"] = text
                span_payload["finish_reason"] = finish or None
                span_payload["response_model"] = msg.response_model
                if msg.usage_metadata is not None:
                    span_payload["usage"] = {
                        "input_tokens": msg.usage_metadata.input_tokens,
                        "output_tokens": msg.usage_metadata.output_tokens,
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


def _build_chat_model(client_config: dict[str, Any], request_config: dict[str, Any]) -> Model:
    """Build the chat model with the same rule as every other Settings model path."""
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import build_model_from_entry

    return build_model_from_entry(client_config, request_config)
