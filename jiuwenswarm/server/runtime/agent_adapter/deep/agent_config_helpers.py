"""DeepAgent 装配用的配置解析与轻量副作用辅助。

职责：
- model_routing / instance config 补缺 / context_engine / kv_cache_affinity
- agent_ras / enable_subagent_runtime 可选 kwargs
- session adapter idle TTL 与 skill lazy refresh 环境开关
- prompt cache_control 标记、permission shim、evolution trigger 赋值

只做配置到结构的转换，不持有 adapter 实例。
"""

from __future__ import annotations

import copy
import logging
import os
from typing import Any

from openjiuwen.core.context_engine.schema.config import CompressionRecallConfig, ContextEngineConfig
from openjiuwen.core.foundation.kv_cache import KVCacheAffinityConfig
from openjiuwen.core.foundation.llm import Model
from openjiuwen.core.single_agent import ReActAgentConfig
from openjiuwen.harness import DeepAgentConfig

from jiuwenswarm.common.config import get_config, resolve_config_env_vars
from jiuwenswarm.common.openjiuwen_rail_compat import filter_unsupported_kwargs
from jiuwenswarm.common.utils import (
    fill_template_defaults,
    load_yaml_dict,
    resolve_shipped_template_config_path,
)
from jiuwenswarm.server.runtime.agent_adapter.deep.model_factory import parse_int

logger = logging.getLogger(__name__)


def _set_skill_evolution_triggers(
    rail: Any,
    *,
    review_trigger: bool,
    signal_trigger: bool = True,
) -> None:
    rail.review_trigger = review_trigger
    rail.signal_trigger = signal_trigger


def init_permission_engine(*_args: Any, **_kwargs: Any) -> None:
    """Legacy shim for tests/older call sites.

    The project now relies on openjiuwen's PermissionInterruptRail and does not
    require a standalone permission engine initialization step.
    """
    return None


def _model_routing_enabled(config: dict[str, Any] | None) -> bool:
    """model_routing 总开关：config.yaml ``model_routing.enabled``（默认 false）+ 进程级 env 覆盖。

    relay 侧可经 spawn env 注入 ``JIUWENSWARM_MODEL_ROUTING_ENABLED``（进程级开关，
    仿 JIUWENSWARM_CODE_COAUTHOR_HEADER_ENABLED）；env 显式设置时优先于 config.yaml。

    判定委托给 rail 侧 ``model_routing_enabled``：swarm 成员 provider（``swarm.model_routing``）
    读的是同一个函数，两处不能各读一遍开关。
    """
    from jiuwenswarm.agents.harness.common.rails.model_routing.model_routing_rail import (
        model_routing_enabled,
    )

    return model_routing_enabled(config)


def _resolve_instance_config_base(config_base: dict[str, Any] | None) -> dict[str, Any]:
    if config_base is None:
        return copy.deepcopy(get_config())
    if not isinstance(config_base, dict):
        raise TypeError("config_base must be a dict when provided")
    # 外部传入的 config_base（如企业同步的稀疏 override）与 shipped 模板做补缺型
    # 合并：模板补全缺失键（如 react.ttse / react.subagents），外部显式键与独有
    # 键全部保留。create_instance 与 reload 共用，避免热更丢掉模板默认值。
    template = load_yaml_dict(resolve_shipped_template_config_path())
    return resolve_config_env_vars(fill_template_defaults(config_base, template))


def _deep_agent_context_engine_config(react_cfg: dict[str, Any] | None) -> ContextEngineConfig:
    """供 ``create_deep_agent(..., context_engine_config=...)`` 使用（与 agent-core 集成测试方法二一致）。

    仅承接 ContextEngine 自身配置；KV cache affinity 由独立
    ``react.kv_cache_affinity_config`` 管理。
    """
    react_cfg = react_cfg or {}
    cec = react_cfg.get("context_engine_config")
    cec = cec if isinstance(cec, dict) else {}
    recall = cec.get("compression_recall_config")
    recall = recall if isinstance(recall, dict) else {}
    return ReActAgentConfig().context_engine_config.model_copy(
        update={
            "enable_reload": bool(cec.get("enable_reload", False)),
            "enable_openrouter_model_context_window_tokens": bool(
                cec.get("enable_openrouter_model_context_window_tokens", False)
            ),
            # 显式设置的上下文窗口上限；非法值回退 None（由 agent-core 按模型解析）
            "context_window_tokens": parse_int(cec.get("context_window_tokens"), None),
            # 上下文压缩 debug 落盘开关；目录缺省时由 agent-core 按
            # OPENJIUWEN_CONTEXT_DEBUG_DIR / workspace 默认路径解析
            "enable_context_debug": bool(cec.get("enable_context_debug", False)),
            "context_debug_dir": cec.get("context_debug_dir") or None,
            # 压缩召回：压缩时归档原始消息，供模型按需召回
            "compression_recall_config": CompressionRecallConfig(
                enabled=bool(recall.get("enabled", False)),
                chunk_size_tokens=parse_int(recall.get("chunk_size_tokens"), 3000),
                chunk_overlap_tokens=parse_int(recall.get("chunk_overlap_tokens"), 300),
            ),
        }
    )


def _model_provider(model: Any) -> str:
    for owner in (model, getattr(model, "_client", None)):
        model_client_config = getattr(owner, "model_client_config", None)
        provider = getattr(model_client_config, "client_provider", None)
        if provider is not None:
            return str(getattr(provider, "value", provider) or "").strip()
    return ""


def _deep_agent_kv_cache_affinity_config(
        react_cfg: dict[str, Any] | None,
        model: Model | None = None,
) -> KVCacheAffinityConfig:
    """Build the ReActAgent KV cache affinity config from jiuwenswarm config."""
    react_cfg = react_cfg or {}
    kv_cfg = react_cfg.get("kv_cache_affinity_config")
    kv_cfg = kv_cfg if isinstance(kv_cfg, dict) else {}
    affinity_enabled = bool(kv_cfg.get("enable_kv_cache_affinity", False))
    if affinity_enabled and model is not None:
        provider = _model_provider(model)
        if provider != "AscendAffinity":
            logger.warning(
                "[JiuWenSwarmDeepAdapter] KV cache affinity failed closed: "
                "model provider=%s requires=AscendAffinity",
                provider or "<empty>",
            )
            affinity_enabled = False
    return KVCacheAffinityConfig(
        enable_kv_cache_release=bool(kv_cfg.get("enable_kv_cache_release", False)),
        enable_kv_cache_affinity=affinity_enabled,
    )


def _try_add_cache_control(msg: Any) -> None:
    """Add cache_control to the last content block of a message.

    Only modifies dict-based content blocks (safe for openjiuwen message types
    where content is ``Union[str, List[Union[str, dict]]]``). If the last block
    is a dict, we add ``cache_control: {"type": "ephemeral"}`` to it.

    Mark the last pre-prompt message for prompt caching,
    while the btw/recap prompt itself carries no marker
    (skipCacheWrite — the side response doesn't create a new cache entry).

    String content is left untouched — converting it to a list would change
    the wire format and break the byte-identical prefix needed for cache hits.
    """
    content = getattr(msg, "content", None)
    if content is None:
        return
    if isinstance(content, list) and len(content) > 0:
        last_block = content[-1]
        if isinstance(last_block, dict):
            last_block["cache_control"] = {"type": "ephemeral"}


def _optional_enable_subagent_runtime(enabled: bool) -> dict[str, bool]:
    """Pass ``enable_subagent_runtime`` only when ``DeepAgentConfig`` accepts it.

    Official ``dev-stable`` openjiuwen 0.1.16 is a dataclass without this
    field; constructing it with the keyword raises TypeError. Do not filter
    against ``create_deep_agent``: that factory still has ``**config_kwargs``,
    so signature filtering would keep the keyword and only warn, or the
    hot-reload path would still TypeError because it builds ``DeepAgentConfig``
    directly. Fork overlay and a future official 3A merge keep the field.
    """
    return filter_unsupported_kwargs(
        DeepAgentConfig.__init__,
        {"enable_subagent_runtime": enabled},
    )


def _agent_ras_kwargs_from_config(config_base: dict[str, Any] | None) -> dict[str, Any]:
    """Thin YAML gate for create_deep_agent ``agent_ras``.

    Schema validation stays in agent-core. Missing / non-dict section → do
    not pass (core defaults to Agent RAS enabled). Explicit ``enabled: false``
    passes ``False`` to disable. Otherwise pass a deep-copied raw dict.
    """
    raw = (config_base or {}).get("agent_ras")
    if not isinstance(raw, dict):
        if raw is not None:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] ignoring invalid agent_ras section: "
                "expected dict, got %s",
                type(raw).__name__,
            )
        return {}
    if raw.get("enabled", True) is not True:
        return {"agent_ras": False}
    return {"agent_ras": copy.deepcopy(raw)}

_SESSION_ADAPTER_IDLE_TTL_ENV = "JIUWENSWARM_SESSION_ADAPTER_IDLE_TTL_SEC"
# Kill switch for lazy per-session SkillUseRail whitelist refresh after install.
# Default on; set to 0/false/off/no to restore pre-fix behavior.
_SKILL_SESSION_LAZY_REFRESH_ENV = "JIUWEN_SKILL_SESSION_LAZY_REFRESH"


def _skill_session_lazy_refresh_enabled() -> bool:
    """Whether install/uninstall should lazily refresh session SkillUseRail allow-lists."""
    raw = os.environ.get(_SKILL_SESSION_LAZY_REFRESH_ENV)
    if raw is None or not str(raw).strip():
        return True
    return str(raw).strip().lower() not in {"0", "false", "off", "no"}


def _session_adapter_idle_ttl_default() -> int:
    """Session-scoped adapter idle TTL in seconds (default 2h).

    Override via ``JIUWENSWARM_SESSION_ADAPTER_IDLE_TTL_SEC`` at process
    start; non-numeric or non-positive values warn and fall back to the
    default so a bad deploy cannot disable eviction entirely.
    """
    default = 2 * 60 * 60
    raw = os.environ.get(_SESSION_ADAPTER_IDLE_TTL_ENV)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        logger.warning(
            "[JiuWenSwarmDeepAdapter] ignoring non-numeric %s=%s",
            _SESSION_ADAPTER_IDLE_TTL_ENV,
            raw,
        )
        return default
    if value <= 0:
        logger.warning(
            "[JiuWenSwarmDeepAdapter] ignoring non-positive %s=%s",
            _SESSION_ADAPTER_IDLE_TTL_ENV,
            raw,
        )
        return default
    return value
