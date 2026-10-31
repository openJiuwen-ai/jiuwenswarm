"""从配置构造视觉、音频、视频、图像模型和模型实例，并计算模型重载指纹。"""

from __future__ import annotations
import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any
from openjiuwen.core.foundation.llm import (
    ModelRequestConfig,
    ModelClientConfig,
    Model,
)
from openjiuwen.core.foundation.llm.utils.provider_utils import is_openai_account_provider
from openjiuwen.harness import (
    AudioModelConfig,
    VisionModelConfig,
)
from jiuwenswarm.agents.harness.common.rails import MultimodalImageRail
from jiuwenswarm.common.model_config_validation import is_placeholder_api_base
from jiuwenswarm.common.local_env_config import read_env
from jiuwenswarm.agents.harness.common.tools.multimodal_config import (
    apply_audio_model_config_from_yaml,
    apply_image_gen_model_config_from_yaml,
    apply_video_model_config_from_yaml,
    apply_vision_model_config_from_yaml,
    dedicated_multimodal_model_configured,
    complete_multimodal_model_configured,
)
from jiuwenswarm.common.reasoning_injector import build_reasoning_model_request_kwargs
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.utils import DEFAULT_ENABLE_READ_IMAGE_MULTIMODAL
from jiuwenswarm.server.runtime.agent_adapter.deep.pure_helpers import _normalize_reload_value

logger = logging.getLogger(__name__)


def _mcc_looks_usable(mcc: dict) -> bool:
    """检查 model_client_config 是否包含有效的 API 凭据。"""
    api_base = str(mcc.get("api_base", "") or "").strip()
    if not api_base or is_placeholder_api_base(api_base):
        return False

    provider = mcc.get("client_provider", "")
    provider = getattr(provider, "value", provider)
    if is_openai_account_provider(str(provider or "")):
        return True

    api_key = str(mcc.get("api_key", "") or "").strip()
    return bool(api_key)


def parse_int(value: Any, default: int) -> int:
    """Parse integer-like values safely."""
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _build_vision_model_config(
    config_base: dict[str, Any],
) -> VisionModelConfig | None:
    """Build DeepAgent vision config from service config/env mapping."""
    if not dedicated_multimodal_model_configured(config_base, "vision"):
        logger.info(
            "[JiuWenSwarmDeepAdapter] vision tools skipped: models.vision has no dedicated "
            "api_key in config.yaml"
        )
        return None
    apply_vision_model_config_from_yaml(config_base)
    api_key = str(read_env("VISION_API_KEY", "")).strip()
    base_url = str(read_env("VISION_BASE_URL") or read_env("VISION_API_BASE") or "").strip()
    model_name = str(read_env("VISION_MODEL") or read_env("VISION_MODEL_NAME") or "").strip()
    # Session-mode accounts authenticate via VISION_DEFAULT_HEADERS (e.g. Basic
    # auth injected by the relay). The value lives in the namespaced env tip;
    # parse it here and carry it inside VisionModelConfig — do NOT bridge it
    # into os.environ (pop_track_b_bare_from_environ hygiene actively removes
    # business keys from os.environ, so an os.environ bridge is unreliable).
    vision_default_headers = str(read_env("VISION_DEFAULT_HEADERS") or "").strip()
    vision_headers_dict: dict[str, str] | None = None
    if vision_default_headers:
        try:
            parsed_headers = json.loads(vision_default_headers)
            if isinstance(parsed_headers, dict) and parsed_headers:
                vision_headers_dict = {
                    str(k): str(v) for k, v in parsed_headers.items()
                }
        except (ValueError, TypeError) as exc:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] VISION_DEFAULT_HEADERS is set but malformed "
                "(JSON parse failed: %s) — auth headers dropped; session-mode vision "
                "calls will likely 401",
                exc,
            )
            vision_headers_dict = None
    if not api_key or not base_url or not model_name:
        logger.info("[JiuWenSwarmDeepAdapter] vision tools skipped: incomplete config")
        return None
    # P2 兜底：依赖的 openjiuwen 版本若尚未包含 default_headers 字段（联动合并
    # 顺序失控），退回不带 headers 的旧构造——宁可丢认证头也不能让 TypeError
    # 阻断 agent 创建（_build_vision_model_config 无 try 包裹，异常会冒泡到
    # AgentManager.create_agent 导致所有视觉 agent 创建失败）。
    try:
        return VisionModelConfig(
            api_key=api_key,
            base_url=base_url,
            model=model_name,
            max_retries=parse_int(read_env("VISION_MAX_RETRIES"), 3),
            default_headers=vision_headers_dict,
        )
    except TypeError:
        logger.warning(
            "[JiuWenSwarmDeepAdapter] VisionModelConfig does not accept "
            "default_headers (openjiuwen version mismatch?) — falling back to "
            "no-auth-headers construction"
        )
        return VisionModelConfig(
            api_key=api_key,
            base_url=base_url,
            model=model_name,
            max_retries=parse_int(read_env("VISION_MAX_RETRIES"), 3),
        )


def _build_audio_model_config(
    config_base: dict[str, Any],
) -> AudioModelConfig | None:
    """Build DeepAgent audio config from service config/env mapping."""
    if not complete_multimodal_model_configured(config_base, "audio"):
        logger.info(
            "[JiuWenSwarmDeepAdapter] audio tools skipped: models.audio requires "
            "api_key, api_base, and model_name in config.yaml"
        )
        return None
    apply_audio_model_config_from_yaml(config_base)
    api_key = str(read_env("AUDIO_API_KEY", "")).strip()
    base_url = str(read_env("AUDIO_BASE_URL") or read_env("AUDIO_API_BASE") or "").strip()
    if not api_key or not base_url:
        logger.info("[JiuWenSwarmDeepAdapter] audio tools skipped: incomplete config")
        return None
    transcription_model = str(
        read_env("AUDIO_TRANSCRIPTION_MODEL") or read_env("AUDIO_MODEL_NAME") or ""
    ).strip()
    question_answering_model = str(
        read_env("AUDIO_QUESTION_ANSWERING_MODEL") or read_env("AUDIO_MODEL_NAME") or ""
    ).strip()
    config_kwargs: dict[str, Any] = {
        "api_key": api_key,
        "base_url": base_url,
        "max_retries": parse_int(read_env("AUDIO_MAX_RETRIES"), 3),
        "http_timeout": parse_int(read_env("AUDIO_HTTP_TIMEOUT"), 20),
        "max_audio_bytes": parse_int(
            read_env("AUDIO_MAX_AUDIO_BYTES"),
            25 * 1024 * 1024,
        ),
    }
    acr_access_key = str(read_env("ACR_ACCESS_KEY", "")).strip()
    acr_access_secret = str(read_env("ACR_ACCESS_SECRET", "")).strip()
    acr_base_url = str(read_env("ACR_BASE_URL", "")).strip()
    if acr_access_key:
        config_kwargs["acr_access_key"] = acr_access_key
    if acr_access_secret:
        config_kwargs["acr_access_secret"] = acr_access_secret
    if acr_base_url:
        config_kwargs["acr_base_url"] = acr_base_url
    if transcription_model:
        config_kwargs["transcription_model"] = transcription_model
    if question_answering_model:
        config_kwargs["question_answering_model"] = question_answering_model
    return AudioModelConfig(**config_kwargs)


def _build_video_model_config(
    config_base: dict[str, Any],
) -> bool:
    """Build DeepAgent video config from service config/env mapping."""
    if not complete_multimodal_model_configured(config_base, "video"):
        logger.info(
            "[JiuWenSwarmDeepAdapter] skip video_understanding: models.video requires "
            "api_key, api_base, and model_name in config.yaml"
        )
        return False
    apply_video_model_config_from_yaml(config_base)
    video_api_key = str(read_env("VIDEO_API_KEY", "")).strip()
    video_api_base = str(read_env("VIDEO_API_BASE", "")).strip()
    video_model_name = str(read_env("VIDEO_MODEL_NAME", "")).strip()
    if not video_api_key or not video_api_base or not video_model_name:
        logger.info("[JiuWenSwarmDeepAdapter] video tools skipped: incomplete config")
        return False
    return True


def _build_image_gen_model_config(
    config_base: dict[str, Any],
) -> bool:
    """Build DeepAgent image generation config from service config/env mapping."""
    apply_image_gen_model_config_from_yaml(config_base)
    if not read_env("IMAGE_GEN_API_KEY"):
        logger.info("[JiuWenSwarmDeepAdapter] image_gen tool skipped: incomplete config")
        return False
    return True


def _mask_model_secret(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "(empty)"
    if len(text) <= 8:
        return "***"
    return f"{text[:4]}***{text[-2:]}"


def _inject_model_selection_into_inputs(
    request: AgentRequest, inputs: dict[str, Any]
) -> None:
    """把请求的模型选择值注入 ``inputs["run"]["context"]["extra"]["model_selection"]``，供 ModelRoutingRail 每请求读取。

    relay 前端下拉框与具体模型同级：四档（fast/balanced/extreme/auto）与具体
    模型统一经 frame ``params.model_name`` → ``request.params`` → 这里写入
    ``run_context.extra``（DeepAgent ``_normalize_inputs`` 会把它带进
    ``InvokeInputs.run_context.extra``）。取值可为具体模型名，或
    fast/balanced/extreme/auto（写死路由）；缺失不写入，rail 侧走
    "具体模型/默认"分支（跳过路由）。
    """
    params = request.params if isinstance(request.params, dict) else {}
    raw = str(params.get("model_name") or "").strip()
    if not raw:
        return
    run_extra = (
        inputs.setdefault("run", {})
        .setdefault("context", {})
        .setdefault("extra", {})
    )
    if isinstance(run_extra, dict):
        run_extra["model_selection"] = raw
        logger.info(
            "[JiuWenSwarmDeepAdapter] model_selection injected: value=%s", raw
        )


def _inject_model_routing_into_inputs(
    request: AgentRequest, inputs: dict[str, Any]
) -> None:
    """把 ``params.enable_routing`` 注入 ``inputs["run"]["context"]["extra"]["enable_routing"]``。

    relay 的 auto 档在 ``params.model_name`` 上仍下发**保底具体模型名**，另置
    ``enable_routing=True`` 表达「本帧模型名可被路由覆盖」。开关经这里写进
    ``run_context.extra``，供 ``ModelRoutingRail.before_invoke`` 每请求读取。

    严格判 ``is True``：缺省 / ``False`` / 非布尔（含字符串 "true"）一律**不写**，
    避免任何异常输入把 rail 从「跳过」翻成「真路由」。params 是白名单消费
    （``_build_inputs`` 逐键 ``params.get``），故 relay 侧必须显式列出该键。
    """
    params = request.params if isinstance(request.params, dict) else {}
    if params.get("enable_routing") is not True:
        return
    run_extra = (
        inputs.setdefault("run", {})
        .setdefault("context", {})
        .setdefault("extra", {})
    )
    if isinstance(run_extra, dict):
        run_extra["enable_routing"] = True
        logger.info(
            "[JiuWenSwarmDeepAdapter] enable_routing injected: value=True"
        )


def _stable_reload_fingerprint(value: Any) -> str:
    normalized = _normalize_reload_value(value)
    payload = json.dumps(normalized, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _model_reload_fingerprint(deep_cfg: Any | None) -> str | None:
    if deep_cfg is None:
        return None
    model = getattr(deep_cfg, "model", None)
    if model is None:
        return None
    return _stable_reload_fingerprint(
        {
            "model": {
                "model_client_config": getattr(model, "model_client_config", None),
                "model_config": getattr(model, "model_config", None),
            },
            "enable_task_loop": getattr(deep_cfg, "enable_task_loop", False),
            "max_iterations": getattr(deep_cfg, "max_iterations", None),
            "context_engine_config": getattr(deep_cfg, "context_engine_config", None),
        }
    )


def _system_prompt_reload_fingerprint(deep_cfg: Any | None) -> str | None:
    if deep_cfg is None:
        return None
    system_prompt = getattr(deep_cfg, "system_prompt", None)
    if system_prompt is None:
        return None
    return _stable_reload_fingerprint(
        {
            "system_prompt": system_prompt,
            "language": getattr(deep_cfg, "language", None),
            "prompt_mode": getattr(deep_cfg, "prompt_mode", None),
        }
    )


def _build_model_from_entry(mcc: dict, mco: dict) -> Model:
    """根据单个模型条目的 model_client_config / model_config_obj 构建 Model 实例。"""
    name = mcc.get("model_name", "")
    # models.json 在 relay spawn 前落盘；未登录时 maas 条目 api_base/api_key 为空，
    # Model 构建会失败（整批跳过，四档路由只能 keep current）。登录后 invoke 同步
    # 把官方通道凭证 env（API_BASE/API_KEY/default_headers）注入 tip，这里在 api_base
    # 缺失时原位回填：仅当候选 base 命中官方通道端点标记才填，避免把自定义 OpenAI
    # 网关误配给官方通道模型。原位修改 mcc 使 _register_model_cache_entry 的
    # model_ref 哈希与注入后的一致。
    if not str(mcc.get("api_base") or "").strip():
        try:
            from jiuwenswarm.llm_sse_patch import _is_huawei_maas_api_base

            for _base_key in (
                "OFFICE_CLAW_HUAWEI_MAAS_BASE_URL",
                "API_BASE",
                "OPENAI_BASE_URL",
                "OPENAI_API_BASE",
            ):
                _base_val = read_env(_base_key, "").strip()
                if _base_val and _is_huawei_maas_api_base(_base_val):
                    mcc["api_base"] = _base_val
                    # 仅「候选 base 命中 maas 端点标记」时才回填 api_key，与注释承诺一致；
                    # 避免把 OPENAI_API_KEY/API_KEY/huawei-maas-session 假凭证写进
                    # 任意 api_base 为空的非 maas 条目（本方法被全部模型缓存构建复用）。
                    if not str(mcc.get("api_key") or "").strip():
                        mcc["api_key"] = (
                            read_env("OPENAI_API_KEY", "").strip()
                            or read_env("API_KEY", "").strip()
                            or "huawei-maas-session"
                        )
                    break
        except Exception as exc:
            logger.debug(
                "[JiuWenSwarmDeepAdapter] MaaS credential backfill skipped: %s", exc
            )
    mcc_fields = {k: v for k, v in mcc.items() if k != "model_name"}
    if not mcc_fields.get("client_provider"):
        mcc_fields["client_provider"] = "OpenAI"
    # 真实鉴权在 tip ``default_headers`` 里同步过来
    # (Authorization: Basic ...). api_key is only the placeholder
    # ``huawei-maas-session``. Merge into custom_headers; llm_sse_patch keeps
    # Authorization through openjiuwen sanitize_headers (otherwise APIG.0303).
    tip_headers = None
    try:
        from jiuwenswarm.common.local_env_config import read_default_headers

        tip_headers = read_default_headers()
    except Exception:
        logger.warning(
            "[JiuWenSwarmDeepAdapter] failed to read tip default_headers",
            exc_info=True,
        )
    if tip_headers:
        existing = mcc_fields.get("custom_headers")
        merged = dict(existing) if isinstance(existing, dict) else {}
        for header_name, header_value in tip_headers.items():
            key = str(header_name)
            # Tip credentials must win for Authorization (MaaS Basic auth).
            if key.lower() == "authorization":
                merged[key] = str(header_value)
            else:
                merged.setdefault(key, str(header_value))
        mcc_fields["custom_headers"] = merged
        has_authorization = any(
            str(k).lower() == "authorization" for k in merged
        )
        logger.info(
            "[JiuWenSwarmDeepAdapter] merged tip default_headers into "
            "custom_headers: keys=%s has_authorization=%s",
            sorted(str(k) for k in merged),
            has_authorization,
        )
    else:
        api_key = str(mcc_fields.get("api_key", "") or "").strip()
        if api_key == "huawei-maas-session":
            logger.warning(
                "[JiuWenSwarmDeepAdapter] tip default_headers empty; "
                "Huawei MaaS calls may fail with APIG.0303"
            )
        else:
            logger.debug(
                "[JiuWenSwarmDeepAdapter] tip default_headers empty "
                "(api_key_present=%s)",
                bool(api_key),
            )
    m_config = ModelRequestConfig(
        **build_reasoning_model_request_kwargs(
            model_client_config=mcc_fields,
            model_config_obj=mco,
            model_name=name,
        )
    )
    return Model(model_client_config=ModelClientConfig(**mcc_fields), model_config=m_config)


def _models_config_fingerprint(config: dict) -> str:
    """计算模型配置段的指纹，用于判断是否需要重建模型缓存。"""
    models = config.get("models", {})
    react = config.get("react") or {}
    # Tip credentials are not in config.yaml; include fingerprints so MaaS
    # default_headers / API_KEY rotation rebuilds the OpenAI client.
    tip_api_key = ""
    tip_api_base = ""
    tip_headers_raw = ""
    try:
        from jiuwenswarm.common.local_env_config import read_default_headers_raw

        tip_api_key = str(read_env("API_KEY", "") or "")
        tip_api_base = str(read_env("API_BASE", "") or "")
        tip_headers_raw = str(read_default_headers_raw() or "")
    except Exception:
        logger.debug(
            "[JiuWenSwarmDeepAdapter] tip credential fingerprint unavailable",
            exc_info=True,
        )

    def _fp(value: str) -> str:
        if not value:
            return ""
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]

    payload = json.dumps(
        {
            "models": models,
            "react": {
                "model_client_config": react.get("model_client_config"),
                "model_name": react.get("model_name"),
                "model_config_obj": react.get("model_config_obj"),
            },
            "tip": {
                "api_key_fp": _fp(tip_api_key),
                "api_base": tip_api_base,
                "headers_fp": _fp(tip_headers_raw),
            },
        },
        sort_keys=True, default=str, ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _prepare_multimodal_image_inputs(
    request: AgentRequest,
    inputs: dict[str, Any],
) -> dict[str, Any]:
    from jiuwenswarm.agents.harness.common.prompt.user_prompt_builder import (
        extract_multimodal_image_files,
    )

    image_files = extract_multimodal_image_files(request.params)
    if not image_files:
        return inputs

    updated = dict(inputs)
    updated["_multimodal_image_files"] = image_files
    logger.info(
        "[JiuWenSwarmDeepAdapter] Prepared %d image attachment(s) "
        "for Core multimodal context-window injection",
        len(image_files),
    )
    return updated


def _prepare_react_image_tool_prompt(
    request: AgentRequest,
    inputs: dict[str, Any],
    *,
    enable_read_image_multimodal: bool,
) -> dict[str, Any]:
    """Add image file paths to the ReAct prompt when native image input is off."""
    if enable_read_image_multimodal:
        return inputs

    query = inputs.get("query")
    if not isinstance(query, str) or "jiuwenswarm_image_tool_context" in query:
        return inputs

    from jiuwenswarm.agents.harness.common.prompt.user_prompt_builder import (
        extract_multimodal_image_files,
    )

    image_files = inputs.get("_multimodal_image_files")
    if not isinstance(image_files, list) or not image_files:
        image_files = extract_multimodal_image_files(request.params)
    if not image_files:
        return inputs

    params = request.params if isinstance(request.params, dict) else {}
    raw_question = params.get("query")
    if not isinstance(raw_question, str) or not raw_question.strip():
        raw_question = params.get("content")
    question = raw_question.strip() if isinstance(raw_question, str) else ""
    first_path = str(image_files[0].get("path") or "").strip()
    if not first_path:
        return inputs

    media_items = []
    for image_file in image_files:
        path = str(image_file.get("path") or "").strip()
        if not path:
            continue
        media_items.append(
            {
                "type": "image",
                "filename": image_file.get("filename") or Path(path).name,
                "mediaPath": path,
                "mimeType": image_file.get("mime_type") or image_file.get("mimeType"),
            }
        )
    if not media_items:
        return inputs

    tool_context = {
        "marker": "jiuwenswarm_image_tool_context",
        "mediaPath": first_path,
        "mediaItems": media_items,
        "question": question,
        "toolHint": (
            "当前主模型未启用原生图片输入。如果需要理解图片内容，请调用图片理解工具；"
            "优先使用 image_reading(local_url=mediaPath, prompt=question)，"
            "或使用 visual_question_answering(image_path_or_url=mediaPath, question=question)。"
        ),
    }

    updated = dict(inputs)
    updated.pop("_multimodal_image_files", None)
    updated["query"] = (
        query
        + "\n\n图片附件上下文（供 ReAct 选择图片理解工具使用）：\n"
        + json.dumps(tool_context, ensure_ascii=False)
    )
    return updated


def _build_image_tool_fallback_notice(
    request: AgentRequest,
    *,
    enable_read_image_multimodal: bool,
    model: Any | None,
) -> dict[str, Any] | None:
    if enable_read_image_multimodal:
        return None

    from jiuwenswarm.agents.harness.common.prompt.user_prompt_builder import (
        extract_multimodal_image_files,
    )

    image_files = extract_multimodal_image_files(request.params)
    if not image_files:
        return None

    model_config = getattr(model, "model_config", None)
    model_name = str(getattr(model_config, "model_name", "") or "").strip()
    model_label = f"（{model_name}）" if model_name else ""
    content = f"当前模型{model_label}不支持原生图片理解，已切换为图片理解工具处理。"
    notice = {
        "event_type": "chat.notice",
        "notice_type": "image_tool_fallback",
        "level": "info",
        "content": content,
        "request_id": request.request_id,
        "session_id": request.session_id,
        "image_count": len(image_files),
    }
    if model_name:
        notice["model_name"] = model_name
    return notice


def _resolve_enable_read_image_multimodal(config: dict[str, Any]) -> bool:
    configured = config.get("enable_read_image_multimodal")
    if isinstance(configured, bool):
        return configured
    return DEFAULT_ENABLE_READ_IMAGE_MULTIMODAL


def _build_multimodal_image_rail(
    enable_image_multimodal: bool | None = None,
) -> MultimodalImageRail | None:
    """Build MultimodalImageRail."""
    try:
        multimodal_image_rail = MultimodalImageRail(
            enable_image_multimodal=enable_image_multimodal,
        )
        logger.info("[JiuWenSwarmDeepAdapter] MultimodalImageRail create success")
    except Exception as exc:
        logger.warning("[JiuWenSwarmDeepAdapter] MultimodalImageRail create failed: %s", exc)
        multimodal_image_rail = None
    return multimodal_image_rail


def _embedding_config_fingerprint(config: dict | None) -> str:
    """计算 config.yaml embed 段的配置指纹，用于检测是否变化。

    归一化逻辑与 openjiuwen OpenAICompatibleEmbeddingProvider.normalize_base_url
    保持一致（去尾斜杠 + 去尾 /embeddings），使等价 endpoint 不会被误判为变化。
    api_key 取 sha256 截断，不明文比较。
    """
    embed = config.get("embed") if isinstance(config, dict) else None
    if not isinstance(embed, dict):
        return ""
    api_key = str(embed.get("embed_api_key") or "")
    base_url = str(embed.get("embed_base_url") or "").strip()
    # 与 provider 侧归一化一致：去尾 /embeddings 再去尾斜杠
    if base_url.endswith("/embeddings"):
        base_url = base_url.rsplit("/embeddings", 1)[0]
    while base_url.endswith("/"):
        base_url = base_url[:-1]
    model = str(embed.get("embed_model") or "")
    api_key_hash = hashlib.sha256(api_key.encode()).hexdigest()[:16]
    return f"{model}:{base_url}:{api_key_hash}"


def _model_looks_usable(model: Model | None) -> bool:
    if model is None:
        return False
    mcc_obj = getattr(model, "model_client_config", None)
    if not isinstance(mcc_obj, ModelClientConfig):
        return False
    return _mcc_looks_usable({
        "api_key": mcc_obj.api_key,
        "api_base": getattr(mcc_obj, "api_base", None),
        "client_provider": getattr(mcc_obj, "client_provider", None),
    })


def _build_messages_for_model(records: list[dict[str, Any]]) -> list[Any]:
    from openjiuwen.core.foundation.llm.schema.message import UserMessage, AssistantMessage

    messages: list[Any] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        role = rec.get("role")
        content = rec.get("content")
        if not isinstance(content, str) or not content.strip():
            continue

        event_type = rec.get("event_type")
        # skip tool call/result records — they contain JSON blobs, not useful for summary
        if role == "user":
            # strip file-content blocks to save tokens
            cleaned = re.sub(r"<file-content[^>]*>.*?</file-content>", "", content, flags=re.DOTALL).strip()
            if cleaned:
                messages.append(UserMessage(content=cleaned))
        elif role == "assistant":
            if rec.get("is_goal_completed_message"):
                continue
            if event_type in ("chat.final", "context.compact_summary", "context.rewind_summary") or not event_type:
                if event_type in ("context.compact_boundary",):
                    continue
                messages.append(AssistantMessage(content=content))

    return messages
