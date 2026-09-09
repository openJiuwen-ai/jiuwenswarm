# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Model vendor preset registry.

A pure-data table mapping ``(vendor_key, plan)`` combinations to the
configuration jiuwenswarm should auto-fill when a user picks that vendor under
that plan in the model selection UI (Token Plan / Coding Plan / 自定义API).

Data is sourced from ``模型厂商汇总表.xlsx`` and every endpoint URL was
curl-verified on 2026-08-05 (401/403/400 = endpoint exists & needs auth, 200 =
public, 404 = absent). The xlsx has six URL columns — 通用Token & Token Plan &
Coding Plan, each in OpenAI and Anthropic format — because, contrary to an
earlier assumption, the same vendor exposes *separate* base_urls per plan
(e.g. 阿里云 Token Plan uses ``token-plan.cn-beijing.maas.aliyuncs.com``, Coding
Plan uses ``coding.dashscope.aliyuncs.com``). This registry mirrors that
one-(vendor,plan) ⇒ one-base_url mapping.

``client_provider`` values align strictly with the ``ProviderType`` enum in
``openjiuwen.core.foundation.llm.schema.config`` (OpenAI / DashScope /
DeepSeek / OpenRouter / Anthropic / OpenAIAccount / SiliconFlow /
AscendAffinity / IntelliRouter). Chinese vendors without a
native enum entry borrow ``OpenAI`` (their endpoints are OpenAI-compatible) and
are distinguished by ``api_base``; ``icon_key`` matches
``ModelProviderIcon.PROVIDER_SPECS`` keys for correct frontend icon display.

Notes on verified endpoints:
  - 阿里云 Coding Plan OpenAI base in the xlsx (``coding.dashscope.aliyun.com``)
    is a typo — DNS does not resolve. Verified correct host is
    ``coding.dashscope.aliyuncs.com`` (401). Registered with the verified host.
  - Maas: the xlsx leaves col8 (models endpoint) empty, but
    ``api.modelarts-maas.com/openai/v1/models`` was verified to exist (400,
    needs auth header). Registered accordingly.
  - Mimo: official platform domain ``api.xiaomimimo.com`` (custom_api) and
    ``token-plan-cn.xiaomimimo.com`` (Token Plan) both verified reachable (401,
    needs api_key). OpenAI + Anthropic format + models endpoints all exist.
  - 百度: OpenAI base moved from /v1 to /v2 (verified 401). Token Plan has
    personal/team variants; the personal variant is used as default.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

from openjiuwen.core.foundation.llm import (
    get_provider_reasoning_rules,
    get_reasoning_capability,
    get_reasoning_capability_catalog,
)

logger = logging.getLogger(__name__)


class PlanKind(str, Enum):
    """Model selection plan categories."""

    TOKEN_PLAN = "token_plan"  # 预付费套餐/资源包/订阅
    CODING_PLAN = "coding_plan"  # 编码场景
    CUSTOM_API = "custom_api"  # 通用Token(按量计费)


@dataclass(frozen=True)
class VendorPreset:
    """A single ``(vendor, plan)`` selection entry.

    Auto-filled on selection; user only fills ``api_key`` (and optionally picks
    a different model from ``model_options`` / pulls latest via
    ``models_endpoint``).

    ``api_base`` / ``anthropic_base`` are plan-specific base_urls taken from
    the xlsx (verified); the same vendor under a different plan may have a
    different base_url.
    """

    vendor_key: str  # e.g. "alibaba", "baidu"
    display_name: str  # e.g. "阿里云百炼"
    plan: PlanKind  # which bucket this entry belongs to
    # 落库用 (jiuwenswarm config.yaml models.defaults[].model_client_config)
    client_provider: str  # must equal a ProviderType enum value
    api_base: str  # OpenAI-format base_url for this plan
    # 前端展示 + 模型下拉
    default_model: str  # pre-selected model for this (vendor, plan) entry
    model_options: tuple[str, ...]  # dropdown candidates (verified common models)
    icon_key: str  # matches frontend ModelProviderIcon PROVIDER_SPECS.key
    # 可选拉取 (vendors.fetch_models RPC)
    models_endpoint: str | None = None  # GET /models endpoint; None = not supported
    models_needs_key: bool = True  # whether fetch requires api_key
    # Anthropic-format base for this plan (custom_api allows switching OpenAI<->Anthropic)
    anthropic_base: str | None = None  # None = vendor has no Anthropic endpoint
    # OpenAI 协议端点方言(deepseek/openrouter/siliconflow/dashscope/openai_compatible/...);
    # None=不写、走默认 openai;Anthropic 协议(client_provider=Anthropic)时此字段被 core 忽略
    endpoint_profile: str | None = None
    # Text-to-video models. ``/v1/models`` is the chat catalog (e.g. MiniMax-M*)
    # and does not list video-generation IDs such as MiniMax-H3.
    video_gen_default_model: str | None = None
    video_gen_model_options: tuple[str, ...] | None = None
    # Dedicated text-to-video API base when it differs from the chat api_base
    # (e.g. DashScope wan uses /api/v1 rather than compatible-mode).
    video_gen_api_base: str | None = None
    # Text-to-image models. Chat ``/v1/models`` does not list wanx / qwen-image IDs,
    # and DashScope image generation uses /api/v1 rather than compatible-mode.
    image_gen_default_model: str | None = None
    image_gen_model_options: tuple[str, ...] | None = None
    image_gen_api_base: str | None = None


# core 的 ProviderType.Anthropic 枚举值。当用户在前端选 "Anthropic 格式" 时,
# 落库的 client_provider 用这个值(core 会实例化 AnthropicModelClient 走 /v1/messages)。
# Anthropic 格式可用的充要条件:该预设的 anthropic_base 非空。
ANTHROPIC_CLIENT_PROVIDER = "Anthropic"

# DashScope video generation.
DASHSCOPE_VIDEO_GEN_API_BASE = "https://dashscope.aliyuncs.com/api/v1"
DASHSCOPE_VIDEO_GEN_MODELS: tuple[str, ...] = (
    "wan3.0-video",
    "wan3.0-video-prime",
    "happyhorse-1.1-t2v",
    "happyhorse-1.1-i2v",
    "happyhorse-1.1-r2v",
    "wan2.7-t2v",
    "wan2.7-i2v",
    "wan2.7-r2v",
    "wan2.6-t2v",
    "wan2.6-i2v",
    "wan2.6-i2v-flash",
    "wan2.6-r2v",
    "wan2.6-r2v-flash",
)

# DashScope image generation.
DASHSCOPE_IMAGE_GEN_API_BASE = "https://dashscope.aliyuncs.com/api/v1"
DASHSCOPE_IMAGE_GEN_MODELS: tuple[str, ...] = (
    "qwen-image-3.0",
    "qwen-image-3.0-pro",
    "wan2.7-image",
    "wan2.7-image-pro",
    "z-image-turbo",
)


# ---------------------------------------------------------------------------
# Registry: (vendor, plan) combinations, grouped by plan for readability.
# Each entry's api_base / anthropic_base comes from the corresponding xlsx
# column for that (vendor, plan) pair (verified by curl on 2026-08-05).
# xlsx column map: 通用OpenAI=col2, 通用Anthropic=col3, TokenPlanOpenAI=col4,
#   TokenPlanAnthropic=col5, CodingPlanOpenAI=col6, CodingPlanAnthropic=col7.
# ---------------------------------------------------------------------------

_PRESETS: list[VendorPreset] = [
    # ── Token Plan ──────────────────────────────────────────────────────────
    VendorPreset(
        vendor_key="alibaba",
        display_name="阿里云百炼",
        plan=PlanKind.TOKEN_PLAN,
        client_provider="OpenAI",
        api_base="https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        endpoint_profile="dashscope",
        default_model="qwen3.7-max",
        model_options=(
            "qwen3.8-max",
            "qwen3.7-max",
            "qwen3.7-plus",
            "qwen3.7-flash",
            "qwen3.6-max-preview",
        ),
        icon_key="qwen",
        # token-plan key 只在 token-plan 域名有效,通用 dashscope 域名 401 invalid_api_key,
        # 所以拉列表也必须走 token-plan maas 域名(实测 200,12 个模型)。
        models_endpoint="https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/models",
        models_needs_key=True,
        anthropic_base="https://token-plan.cn-beijing.maas.aliyuncs.com/apps/anthropic",
        video_gen_default_model="wan2.6-t2v",
        video_gen_model_options=DASHSCOPE_VIDEO_GEN_MODELS,
        video_gen_api_base=DASHSCOPE_VIDEO_GEN_API_BASE,
        image_gen_default_model="wanx-v1",
        image_gen_model_options=DASHSCOPE_IMAGE_GEN_MODELS,
        image_gen_api_base=DASHSCOPE_IMAGE_GEN_API_BASE,
    ),
    VendorPreset(
        vendor_key="minimax",
        display_name="MiniMax",
        plan=PlanKind.TOKEN_PLAN,
        client_provider="OpenAI",
        api_base="https://api.minimaxi.com/v1",
        default_model="MiniMax-M2",
        model_options=(
            "MiniMax-M3",
            "MiniMax-M2.7",
            "MiniMax-M2.5",
            "MiniMax-M2",
            "MiniMax-M1",
        ),
        icon_key="minimax",
        models_endpoint="https://api.minimaxi.com/v1/models",
        models_needs_key=True,
        anthropic_base="https://api.minimaxi.com/anthropic",
    ),
    VendorPreset(
        vendor_key="maas",
        display_name="Maas盘古",
        plan=PlanKind.TOKEN_PLAN,
        client_provider="ModelArts",
        api_base="https://api.modelarts-maas.com/plan/v2",
        endpoint_profile="modelarts",
        default_model="glm-5.1",
        model_options=("glm-5.1", "kimi-k2.6", "deepseek-v4-flash"),
        icon_key="pangu",
        models_endpoint="https://api.modelarts-maas.com/v2/models",
        models_needs_key=True,
        anthropic_base="https://api.modelarts-maas.com/plan/anthropic",
    ),
    VendorPreset(
        vendor_key="baidu",
        display_name="百度智能云",
        plan=PlanKind.TOKEN_PLAN,
        client_provider="OpenAI",
        api_base="https://qianfan.baidubce.com/v2/tokenplan/personal",  # personal; team variant: /v2/tokenplan/team
        default_model="ernie-5.1",
        model_options=(
            "ernie-5.1",
            "deepseek-v4-pro",
            "deepseek-v4-flash",
            "glm-5.2",
            "kimi-k2.6",
            "mimo-v2.5",
        ),
        icon_key="baidu",
        # token plan key 走 tokenplan 端点;/v2/models 是通用端点。/v2/tokenplan/models 404,
        # personal 变体 401(端点存在,对齐 api_base)。
        models_endpoint="https://qianfan.baidubce.com/v2/tokenplan/personal/models",
        models_needs_key=True,  # 实测无 key 返回 401;带 Bearer 才 200
        anthropic_base="https://qianfan.baidubce.com/anthropic/tokenplan/personal",
    ),
    VendorPreset(
        vendor_key="mimo",
        display_name="小米Mimo",
        plan=PlanKind.TOKEN_PLAN,
        client_provider="MiMo",
        api_base="https://token-plan-cn.xiaomimimo.com/v1",  # 套餐调用走套餐域名(需套餐 key)
        endpoint_profile="mimo",
        default_model="mimo-v2.5-pro",
        model_options=("mimo-v2.5-pro", "mimo-v2.5"),
        icon_key="mimo",
        # 拉列表也必须走套餐域名:通用 api.xiaomimimo.com 对套餐 key 返回 401 Invalid API Key,
        # 套餐域名 token-plan-cn.xiaomimimo.com/v1/models 才 200。
        models_endpoint="https://token-plan-cn.xiaomimimo.com/v1/models",
        models_needs_key=True,
        anthropic_base="https://token-plan-cn.xiaomimimo.com/anthropic",
    ),
    # ── Coding Plan ─────────────────────────────────────────────────────────
    VendorPreset(
        vendor_key="alibaba",
        display_name="阿里云百炼",
        plan=PlanKind.CODING_PLAN,
        client_provider="OpenAI",
        api_base="https://coding.dashscope.aliyuncs.com/v1",  # xlsx 写 aliyun.com 是笔误,实测 aliuyuncs.com 才存在(401)
        endpoint_profile="dashscope",
        default_model="qwen3-coder-next",
        model_options=("qwen3-coder-next", "qwen3.7-max", "qwen3.6-max-preview"),
        icon_key="qwen",
        # coding key 只在 coding 域名有效,通用 dashscope 域名 401。coding 域名 /v1/models 实测 200(公开)。
        models_endpoint="https://coding.dashscope.aliyuncs.com/v1/models",
        models_needs_key=True,
        anthropic_base="https://coding.dashscope.aliyuncs.com/apps/anthropic",
        video_gen_default_model="wan2.6-t2v",
        video_gen_model_options=DASHSCOPE_VIDEO_GEN_MODELS,
        video_gen_api_base=DASHSCOPE_VIDEO_GEN_API_BASE,
        image_gen_default_model="wanx-v1",
        image_gen_model_options=DASHSCOPE_IMAGE_GEN_MODELS,
        image_gen_api_base=DASHSCOPE_IMAGE_GEN_API_BASE,
    ),
    VendorPreset(
        vendor_key="kimi",
        display_name="Kimi(月之暗面)",
        plan=PlanKind.CODING_PLAN,
        client_provider="OpenAI",
        api_base="https://api.kimi.com/coding/v1",
        default_model="kimi-k2.7-code",
        model_options=("kimi-k3", "kimi-k2.7-code", "kimi-k2.6", "kimi-k2-thinking"),
        icon_key="kimi",
        # coding key 只在 api.kimi.com 有效,moonshot.cn 是通用域。coding 域 /coding/v1/models 实测 401(端点存在,非 404)。
        models_endpoint="https://api.kimi.com/coding/v1/models",
        models_needs_key=True,
        anthropic_base="https://api.kimi.com/coding",
    ),
    VendorPreset(
        vendor_key="zhipu",
        display_name="智谱GLM",
        plan=PlanKind.CODING_PLAN,
        client_provider="OpenAI",
        api_base="https://open.bigmodel.cn/api/coding/paas/v4",
        default_model="glm-4.7",
        model_options=(
            "glm-5.2",
            "glm-5.1",
            "glm-5",
            "glm-4.7",
            "glm-4.7-flash",
            "codegeex-4",
        ),
        icon_key="zhipu",
        # coding key 走 coding 路径;/api/paas/v4/models 是通用端点。/api/coding/paas/v4/models 实测 401(端点存在)。
        models_endpoint="https://open.bigmodel.cn/api/coding/paas/v4/models",
        models_needs_key=True,
        anthropic_base="https://open.bigmodel.cn/api/anthropic",
    ),
    VendorPreset(
        vendor_key="volcengine",
        display_name="火山引擎",
        plan=PlanKind.CODING_PLAN,
        client_provider="OpenAI",
        api_base="https://ark.cn-beijing.volces.com/api/coding/v3",
        default_model="seed-2.0-mini",
        model_options=("seed-2.0-mini", "seed-2.0-lite", "seed-1.6", "seed-1.6-flash"),
        icon_key="doubao",
        # coding key 走 coding 端点;/api/v3/models 是通用端点。/api/coding/v3/models 实测 401(端点存在)。
        models_endpoint="https://ark.cn-beijing.volces.com/api/coding/v3/models",
        models_needs_key=True,
        anthropic_base="https://ark.cn-beijing.volces.com/api/coding",
    ),
    VendorPreset(
        vendor_key="baidu",
        display_name="百度智能云",
        plan=PlanKind.CODING_PLAN,
        client_provider="OpenAI",
        api_base="https://qianfan.baidubce.com/v2/coding",
        default_model="deepseek-v4-pro",
        model_options=("deepseek-v4-pro", "deepseek-v4-flash", "glm-5.1", "kimi-k2.5"),
        icon_key="baidu",
        # coding key 走 /v2/coding 端点;/v2/models 是通用端点。/v2/coding/models 实测 401(端点存在)。
        models_endpoint="https://qianfan.baidubce.com/v2/coding/models",
        models_needs_key=True,  # 实测无 key 返回 401;带 Bearer 才 200
        anthropic_base="https://qianfan.baidubce.com/anthropic/coding",
    ),
    # ── 自定义API (通用Token / 按量计费) ────────────────────────────────────
    VendorPreset(
        vendor_key="alibaba",
        display_name="阿里云百炼",
        plan=PlanKind.CUSTOM_API,
        client_provider="OpenAI",
        api_base="https://dashscope.aliyuncs.com/compatible-mode/v1",
        endpoint_profile="dashscope",
        default_model="qwen3.8-max",
        model_options=(
            # 通义旗舰/通用
            "qwen3.8-max",
            "qwen3.8-max-0902",
            "qwen3.8-flash",
            "qwen3.7-plus",
            # 代码
            "qwen3-coder-next",
            "qwen3-coder-plus",
            "qwen3-coder-flash",
            "kimi-k2.7-code",
            # 第三方模型（百炼直供裸 ID，探活报告中全部实测 200）
            "MiniMax-M2.1",
            "MiniMax-M2.5",
            "deepseek-r1",
            "deepseek-r1-distill-llama-8b",
            "deepseek-r1-distill-qwen-1.5b",
            "deepseek-r1-distill-qwen-7b",
            "deepseek-r1-distill-qwen-14b",
            "deepseek-r1-distill-qwen-32b",
            "deepseek-v3",
            "deepseek-v3.1",
            "deepseek-v3.2",
            "deepseek-v4-pro",
            "deepseek-v4-pro-0813",
            "deepseek-v4-flash",
            "deepseek-v4-flash-0731",
            "kimi-k2-thinking",
            "kimi-k2.5",
            "kimi-k2.6",
            "kimi-k3",
            "glm-4.7",
            "glm-5",
            "glm-5.1",
            "glm-5.2",
            "glm-5.2-fast-preview",
            # 视觉/OCR
            "qwen3-vl-plus",
            "qwen3-vl-flash",
            "qwen-vl-ocr-latest",
            "qwen3.5-ocr",
            # 全模态
            "qwen3.5-omni-plus",
            "qwen3.5-omni-flash",
            # 通义开源
            "qwen3.8-2.4t-a95b",
            "qwen3.8-27b",
            "qwen3-next-80b-a3b-instruct",
            "qwen3-next-80b-a3b-thinking",
            # 数学/翻译/长文本
            "qwen-math-plus-latest",
            "qwen-mt-plus",
            "qwen-long",
            # 研究/搜索
            "qwen-deep-research-2025-12-15",
            "qwen-deep-search-planning",
        ),
        icon_key="qwen",
        models_endpoint="https://dashscope.aliyuncs.com/compatible-mode/v1/models",
        models_needs_key=True,
        anthropic_base="https://dashscope.aliyuncs.com/apps/anthropic",
        video_gen_default_model="wan2.6-t2v",
        video_gen_model_options=DASHSCOPE_VIDEO_GEN_MODELS,
        video_gen_api_base=DASHSCOPE_VIDEO_GEN_API_BASE,
        image_gen_default_model="wanx-v1",
        image_gen_model_options=DASHSCOPE_IMAGE_GEN_MODELS,
        image_gen_api_base=DASHSCOPE_IMAGE_GEN_API_BASE,
    ),
    VendorPreset(
        vendor_key="deepseek",
        display_name="DeepSeek",
        plan=PlanKind.CUSTOM_API,
        client_provider="OpenAI",
        api_base="https://api.deepseek.com",
        endpoint_profile="deepseek",
        default_model="deepseek-v4-pro",
        model_options=(
            "deepseek-v4-pro",
            "deepseek-v4-flash",
            "deepseek-v4-flash-latest",
        ),
        icon_key="deepseek",
        models_endpoint="https://api.deepseek.com/models",
        models_needs_key=True,
        anthropic_base="https://api.deepseek.com/anthropic",
    ),
    VendorPreset(
        vendor_key="kimi",
        display_name="Kimi(月之暗面)",
        plan=PlanKind.CUSTOM_API,
        client_provider="OpenAI",
        api_base="https://api.moonshot.cn/v1",
        default_model="kimi-k3",
        model_options=("kimi-k3", "kimi-k2.7-code", "kimi-k2.6", "kimi-k2-thinking"),
        icon_key="kimi",
        models_endpoint="https://api.moonshot.cn/v1/models",
        models_needs_key=True,
        anthropic_base="https://api.moonshot.cn/anthropic",
    ),
    VendorPreset(
        vendor_key="openrouter",
        display_name="OpenRouter",
        plan=PlanKind.CUSTOM_API,
        client_provider="OpenAI",
        api_base="https://openrouter.ai/api/v1",
        endpoint_profile="openrouter",  # 归因头由 core 按 profile 注入
        default_model="anthropic/claude-sonnet-4.5",
        model_options=(
            "anthropic/claude-sonnet-4.5",
            "qwen/qwen3.8-max",
            "z-ai/glm-5.2",
            "deepseek/deepseek-v4-pro",
            "openai/gpt-5.6-terra",
            "google/gemini-2.5-flash",
        ),
        icon_key="openai",
        models_endpoint="https://openrouter.ai/api/v1/models",
        models_needs_key=False,  # 公开列出(实测200,338模型)
        anthropic_base="https://openrouter.ai/api/v1",  # xlsx 标 Anthropic 格式与 OpenAI 同 url(实测 /v1/messages 也 401 存在)
    ),
    VendorPreset(
        vendor_key="zhipu",
        display_name="智谱GLM",
        plan=PlanKind.CUSTOM_API,
        client_provider="OpenAI",
        api_base="https://open.bigmodel.cn/api/paas/v4",
        default_model="glm-5.2",
        model_options=(
            "glm-5.2",
            "glm-5.1",
            "glm-5",
            "glm-4.7",
            "glm-4.7-flash",
            "glm-5-turbo",
            "glm-5v-turbo",
            "codegeex-4",
        ),
        icon_key="zhipu",
        models_endpoint="https://open.bigmodel.cn/api/paas/v4/models",
        models_needs_key=True,
        anthropic_base="https://open.bigmodel.cn/api/anthropic",
    ),
    VendorPreset(
        vendor_key="minimax",
        display_name="MiniMax",
        plan=PlanKind.CUSTOM_API,
        client_provider="OpenAI",
        api_base="https://api.minimaxi.com/v1",
        default_model="MiniMax-M3",
        model_options=(
            "MiniMax-M3",
            "MiniMax-M2.7",
            "MiniMax-M2.5",
            "MiniMax-M2",
            "MiniMax-M2-HER",
            "MiniMax-M1",
        ),
        icon_key="minimax",
        models_endpoint="https://api.minimaxi.com/v1/models",
        models_needs_key=True,
        anthropic_base="https://api.minimaxi.com/anthropic",
    ),
    VendorPreset(
        vendor_key="maas",
        display_name="Maas盘古",
        plan=PlanKind.CUSTOM_API,
        client_provider="ModelArts",
        api_base="https://api.modelarts-maas.com/v2",
        endpoint_profile="modelarts",
        default_model="openpangu-2.0-pro",
        model_options=(
            "openpangu-2.0-pro",
            "openpangu-2.0-flash",
            "glm-5.2",
            "glm-5.1",
            "kimi-k2.6",
            "deepseek-v4-pro",
            "deepseek-v4-flash",
        ),
        icon_key="pangu",
        models_endpoint="https://api.modelarts-maas.com/v2/models",
        models_needs_key=True,
        anthropic_base="https://api.modelarts-maas.com/anthropic",
    ),
    VendorPreset(
        vendor_key="volcengine",
        display_name="火山引擎",
        plan=PlanKind.CUSTOM_API,
        client_provider="OpenAI",
        api_base="https://ark.cn-beijing.volces.com/api/v3",
        default_model="seed-2.0-mini",
        model_options=(
            "seed-2.0-mini",
            "seed-2.0-lite",
            "seed-1.6",
            "seed-1.6-flash",
            "doubao-1.5-pro-32k",
        ),
        icon_key="doubao",
        models_endpoint="https://ark.cn-beijing.volces.com/api/v3/models",
        models_needs_key=True,
        anthropic_base="https://ark.cn-beijing.volces.com/api/compatible",
    ),
    VendorPreset(
        vendor_key="baidu",
        display_name="百度智能云",
        plan=PlanKind.CUSTOM_API,
        client_provider="OpenAI",
        api_base="https://qianfan.baidubce.com/v2",
        default_model="ernie-5.1",
        model_options=(
            "ernie-5.1",
            "ernie-5.0",
            "deepseek-v4-pro",
            "deepseek-v4-flash",
            "glm-5.2",
            "kimi-k2.6",
            "mimo-v2.5",
            "qianfan-ocr-fast",
        ),
        icon_key="baidu",
        models_endpoint="https://qianfan.baidubce.com/v2/models",
        models_needs_key=True,  # 实测无 key 返回 403;带 Bearer 才 200
        anthropic_base="https://qianfan.baidubce.com/anthropic",
    ),
    VendorPreset(
        vendor_key="mimo",
        display_name="小米Mimo",
        plan=PlanKind.CUSTOM_API,
        client_provider="MiMo",
        api_base="https://api.xiaomimimo.com/v1",
        endpoint_profile="mimo",
        default_model="mimo-v2.5-pro",
        model_options=("mimo-v2.5-pro", "mimo-v2.5"),
        icon_key="mimo",
        models_endpoint="https://api.xiaomimimo.com/v1/models",
        models_needs_key=True,
        anthropic_base="https://api.xiaomimimo.com/anthropic",
    ),
]


# Pre-built indices for O(1) lookup.
_BY_PLAN: dict[PlanKind, list[VendorPreset]] = {p: [] for p in PlanKind}
_BY_VENDOR_PLAN: dict[tuple[str, PlanKind], VendorPreset] = {}
_BY_VENDOR: dict[
    str, VendorPreset
] = {}  # first occurrence per vendor (for icon lookup)

for _p in _PRESETS:
    _BY_PLAN[_p.plan].append(_p)
    _BY_VENDOR_PLAN[(_p.vendor_key, _p.plan)] = _p
    _BY_VENDOR.setdefault(_p.vendor_key, _p)


def get_presets_by_plan(plan: PlanKind | str) -> list[VendorPreset]:
    """All vendor entries under a given plan bucket (for UI tab rendering)."""
    if isinstance(plan, str):
        plan = PlanKind(plan)
    return list(_BY_PLAN.get(plan, []))


def get_preset(vendor_key: str, plan: PlanKind | str) -> VendorPreset | None:
    """The specific (vendor, plan) preset; None if the vendor isn't under that plan."""
    if isinstance(plan, str):
        plan = PlanKind(plan)
    return _BY_VENDOR_PLAN.get((vendor_key, plan))


def get_preset_by_vendor_key(vendor_key: str) -> VendorPreset | None:
    """First preset for a vendor (used for icon display / partial lookup)."""
    return _BY_VENDOR.get(vendor_key)


def get_all_presets() -> list[VendorPreset]:
    """All presets, flat list."""
    return list(_PRESETS)


def _reasoning_capabilities(preset: VendorPreset) -> dict[str, dict[str, Any]]:
    capabilities: dict[str, dict[str, Any]] = {}
    for model in preset.model_options:
        # 逐模型隔离：能力查询是静态表 lookup，正常不会抛，但 vendors.list 是
        # 前端配置页入口 RPC，不能因单个模型异常导致整个厂商列表挂掉。失败
        # 时跳过该模型，前端会回落到 reasoning_rules / model_fallbacks。
        try:
            protocols = {
                "openai": get_reasoning_capability(
                    provider=preset.client_provider,
                    model=model,
                    protocol="openai",
                    api_base=preset.api_base,
                    endpoint_profile=preset.endpoint_profile,
                ).to_dict(),
            }
            if preset.anthropic_base:
                protocols["anthropic"] = get_reasoning_capability(
                    provider=ANTHROPIC_CLIENT_PROVIDER,
                    model=model,
                    protocol="anthropic",
                    api_base=preset.anthropic_base,
                ).to_dict()
        except Exception:
            logger.warning(
                "skip reasoning capability for vendor=%s model=%s",
                preset.vendor_key,
                model,
                exc_info=True,
            )
            continue
        capabilities[model] = protocols
    return capabilities


def to_frontend_payload() -> dict[str, Any]:
    """Grouped payload for the ``vendors.list`` RPC (plan -> [vendor cards])."""
    out: dict[str, Any] = {
        "reasoning": get_reasoning_capability_catalog(),
    }
    for plan in PlanKind:
        out[plan.value] = [
            {
                "vendor_key": p.vendor_key,
                "display_name": p.display_name,
                "plan": p.plan.value,
                # OpenAI 格式(默认): client_provider + api_base + endpoint_profile
                "client_provider": p.client_provider,
                "api_base": p.api_base,
                "endpoint_profile": p.endpoint_profile,
                "default_model": p.default_model,
                "model_options": list(p.model_options),
                "icon_key": p.icon_key,
                "models_endpoint": p.models_endpoint,
                "models_needs_key": p.models_needs_key,
                "reasoning_capabilities": _reasoning_capabilities(p),
                # Provider-scoped pattern 规则：给 models_endpoint 拉取到的、
                # 不在 model_options 精确表里的新模型用，避免前端退到跨厂商
                # model_fallbacks 时与后端 provider 级校验产生漂移。
                "reasoning_rules": get_provider_reasoning_rules(
                    provider=p.client_provider,
                    api_base=p.api_base,
                    endpoint_profile=p.endpoint_profile,
                ),
                # Anthropic 格式(可选切换): 仅当 anthropic_base 非空时可用,
                # 切换后落库 client_provider=ANTHROPIC_CLIENT_PROVIDER、
                # api_base=anthropic_base(core 用 AnthropicModelClient 走 /v1/messages)。
                "supports_anthropic": bool(p.anthropic_base),
                "anthropic_base": p.anthropic_base,
                "anthropic_client_provider": ANTHROPIC_CLIENT_PROVIDER
                if p.anthropic_base
                else None,
                "video_gen_default_model": p.video_gen_default_model,
                "video_gen_model_options": list(p.video_gen_model_options or ()),
                "video_gen_api_base": p.video_gen_api_base,
                "image_gen_default_model": p.image_gen_default_model,
                "image_gen_model_options": list(p.image_gen_model_options or ()),
                "image_gen_api_base": p.image_gen_api_base,
            }
            for p in _BY_PLAN[plan]
        ]
    return out
