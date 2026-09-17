# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Memory configuration for JiuWenSwarm.

Configuration is loaded from config/config.yaml.
Embedding API settings are in the 'embed' section.
"""

import logging
import os
import re
from typing import Any, Optional, Dict, List
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from jiuwenswarm.common.utils import get_config_file, get_agent_workspace_dir

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = str(get_config_file())
DEFAULT_WORKSPACE_DIR = str(get_agent_workspace_dir())

_config_cache: Optional[Dict[str, Any]] = None


def _resolve_env_vars(value: Any) -> Any:
    """Recursively resolve environment variables in config values."""
    if isinstance(value, str):
        pattern = r'\$\{([^:}]+)(?::-([^}]*))?\}'

        def replace_env(match):
            var_name = match.group(1)
            default = match.group(2) if match.group(2) is not None else ""
            return os.getenv(var_name, default)
        return re.sub(pattern, replace_env, value)
    elif isinstance(value, dict):
        return {k: _resolve_env_vars(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [_resolve_env_vars(item) for item in value]
    else:
        return value


def clear_config_cache() -> None:
    """清除配置缓存，使下次 _load_config() 重新从 config.yaml 读取并解析环境变量."""
    global _config_cache
    _config_cache = None


def _load_config() -> Dict[str, Any]:
    """Load configuration from YAML file."""
    global _config_cache

    if _config_cache is not None:
        return _config_cache
    
    config_path = Path(DEFAULT_CONFIG_PATH)
    
    if not config_path.exists():
        logger.warning(f"Config file not found: {config_path}")
        _config_cache = {}
        return _config_cache
    
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    
    config = _resolve_env_vars(config)
    _config_cache = config
    return config


def get_embed_config() -> Dict[str, str]:
    """Get embedding configuration from config file.
    
    Returns embedding API configuration from config.yaml embed section.
    """
    config = _load_config()
    embed_config = config.get("embed", {})
    
    return {
        "api_key": embed_config.get("embed_api_key"),
        "base_url": embed_config.get("embed_base_url"),
        "model": embed_config.get("embed_model"),
    }


EMBED_API_KEY = property(lambda self: get_embed_config()["api_key"])
EMBED_BASE_URL = property(lambda self: get_embed_config()["base_url"])
EMBED_MODEL = property(lambda self: get_embed_config()["model"])


@dataclass
class MemorySettings:
    """Memory configuration settings."""
    provider: str = "openai_compatible"
    model: str = "text-embedding-v3"
    fallback: str = "mock"
    sources: List[str] = field(default_factory=lambda: ["memory", "sessions"])
    extraPaths: List[str] = field(default_factory=list)
    
    chunking: Dict[str, int] = field(default_factory=lambda: {"tokens": 256, "overlap": 32})
    
    query: Dict[str, Any] = field(default_factory=lambda: {
        "maxResults": 10,
        "minScore": 0.3,
        "hybrid": {
            "enabled": True,
            "vectorWeight": 0.7,
            "textWeight": 0.3,
            "candidateMultiplier": 2.0
        }
    })
    
    store: Dict[str, Any] = field(default_factory=lambda: {
        # 相对于 workspace_dir/memory/ 目录
        "path": "memory.db",
        "vector": {"enabled": True},
        "fts": {"enabled": True}
    })
    
    sync: Dict[str, Any] = field(default_factory=lambda: {
        "watch": True,
        "watchDebounceMs": 2000,
        "onSearch": True,
        "onSessionStart": True,
        "intervalMinutes": 0
    })
    
    cache: Dict[str, Any] = field(default_factory=lambda: {
        "enabled": True,
        "maxEntries": 10000
    })


def create_memory_settings(
    workspace_dir: str = DEFAULT_WORKSPACE_DIR,
    **overrides
) -> MemorySettings:
    """Create MemorySettings instance.
    
    Args:
        workspace_dir: Workspace directory
        **overrides: Override default settings
    
    Returns:
        MemorySettings instance
    """
    config = _load_config()
    embed_config = get_embed_config()
    memory_config = config.get("memory", {})
    
    settings = MemorySettings()
    
    settings.model = embed_config.get("model", settings.model)
    
    if memory_config:
        if "provider" in memory_config:
            settings.provider = memory_config["provider"]
        if "fallback" in memory_config:
            settings.fallback = memory_config["fallback"]
        if "sources" in memory_config:
            settings.sources = memory_config["sources"]
        if "extraPaths" in memory_config:
            settings.extraPaths = memory_config["extraPaths"]
        if "chunking" in memory_config:
            settings.chunking = memory_config["chunking"]
        if "query" in memory_config:
            settings.query = memory_config["query"]
        if "sync" in memory_config:
            settings.sync = memory_config["sync"]
        if "cache" in memory_config:
            settings.cache = memory_config["cache"]
    
    if "store" not in overrides:
        store_config = memory_config.get("store", {})
        # 向量数据库索引文件存放在与 MEMORY.md 同目录 (workspace_dir/memory/memory.db)
        # 只使用文件名，让 manager.py 的 _resolve_db_path 处理完整路径
        overrides["store"] = {
            "path": store_config.get("path", "memory.db"),
            "vector": store_config.get("vector", {"enabled": True}),
            "fts": store_config.get("fts", {"enabled": True}),
        }
    
    for key, value in overrides.items():
        if hasattr(settings, key):
            setattr(settings, key, value)
    
    return settings


def is_agent_mode(mode: str) -> bool:
    normalized_mode = (mode or "").strip()
    return normalized_mode in ("agent", "agent.plan", "agent.fast", "plan", "fast")


def _resolve_mode_memory(mode: str, config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Locate the `memory:` block under modes for a given mode token.

    Accepts several mode formats used across the codebase:
      - "agent"                      -> modes.agent (merged single mode)
      - "agent.plan" / "agent.fast"  -> modes.agent (legacy tokens,归一)
      - "plan" / "fast"              -> modes.agent (legacy sub-tokens, 归一)
      - "code" / "code.normal"       -> modes.code

    plan / fast 已合并为单一 ``agent`` 模式，记忆配置统一读取
    ``modes.agent.memory``；历史 ``agent.plan`` / ``agent.fast`` /
    ``plan`` / ``fast`` 均归一到该节点。
    Returns {} when no block is found (callers treat missing as disabled).
    """
    modes_cfg = (config or {}).get("modes", {}) if isinstance(config, dict) else {}
    if not isinstance(modes_cfg, dict):
        return {}

    normalized_mode = (mode or "").strip()
    if is_agent_mode(normalized_mode):
        # "agent" 或历史 "agent.plan" / "agent.fast" / 单独出现的 "plan" / "fast"
        node = modes_cfg.get("agent", {})
    elif normalized_mode == "code" or normalized_mode.startswith("code."):
        # "code" 及其子模式（code.normal / code.plan / code.team...）统一读取 modes.code。
        node = modes_cfg.get("code", {})
    else:
        # 其它未识别的 mode（如 "team"、"team.plan"、"auto_harness"）没有对应的
        # 记忆配置节点，不应落到 modes.agent / modes.code 兜底（否则会误读/误写）。
        return {}

    if not isinstance(node, dict):
        return {}
    mem = node.get("memory", {})
    return mem if isinstance(mem, dict) else {}


def is_memory_enabled(mode: str, config: Optional[Dict[str, Any]] = None) -> bool:
    """Check if built-in memory is enabled for the given mode.

    Reads `modes.agent.memory.enabled` (or `modes.code.memory.enabled`).

    Args:
        config: Optional config dict. If provided, reads from it directly
                (avoids stale cache). Otherwise reads from config.yaml.

    Note:
        For 'code' mode, default is True (CodingMemoryRail was always mounted before).
        For the merged 'agent' mode, default is False.
    """
    try:
        mem_cfg = _resolve_mode_memory(mode, config)
        # code 模式默认开启（之前 CodingMemoryRail 是固定挂载的）
        # agent 模式默认关闭
        # 判断需与 _resolve_mode_memory 的 code 归一逻辑一致：code 及其子模式
        # （code.normal / code.plan / code.team ...）都应默认开启，否则用户配置
        # 缺 enabled 字段时子模式记忆会被错误默认关闭。
        normalized_mode = (mode or "").strip()
        is_code = normalized_mode == "code" or normalized_mode.startswith("code.")
        default_value = True if is_code else False
        return bool(mem_cfg.get("enabled", default_value))
    except Exception as e:
        logger.warning(f"Invalid memory config, disable memory, error: {e}")
        return False


def is_proactive_memory(mode: str, config: Optional[Dict[str, Any]] = None) -> bool:
    """Check if proactive memory is enabled for the given mode.

    plan / fast 合并后，agent 模式**只保留被动记忆**：始终返回 ``False``
    （注入被动模式记忆提示词，仅在用户明确要求时读写记忆）。``is_proactive``
    配置开关已下线。code 等其他模式仍读取各自 ``memory.is_proactive``。
    """
    try:
        # agent 合并模式（含历史 agent.plan / agent.fast / 单独出现的 plan|fast，
        if is_agent_mode(mode):
            return False
        return bool(_resolve_mode_memory(mode, config).get("is_proactive", False))
    except Exception as e:
        logger.warning(f"Invalid proactive memory config, disable proactive memory, error: {e}")
        return False


def is_auto_memory_enabled(mode: str, config: Optional[Dict[str, Any]] = None) -> bool:
    """Check if auto-memory (post-conversation extraction) is enabled for the given mode.

    Mode-aware (mirrors is_memory_enabled / is_proactive_memory):

    - code mode: reads ``modes.code.memory.auto_coding_memory`` (default False).
      This controls the sub-agent fallback extraction specific to code mode.
    - agent mode: reads the global ``auto_memory_enabled`` flag. Path, default
      and exception behaviour are preserved verbatim from the legacy
      ``common.config.is_auto_memory_enabled()`` so agent logic is unchanged.

    Args:
        config: Optional config dict. If provided, reads from it directly
                (avoids stale cache). Otherwise reads from config.yaml.
    """
    token = (mode or "").strip()
    if token.startswith("code"):
        # code mode: 读 modes.code.memory.auto_coding_memory，默认 False
        try:
            return bool(_resolve_mode_memory(mode, config).get("auto_coding_memory", False))
        except Exception as e:
            logger.warning(f"Invalid auto_coding_memory config, disabled. error: {e}")
            return False
    # agent mode: 读全局 auto_memory_enabled，路径/默认/行为全不变
    try:
        if config is None:
            from jiuwenswarm.common.config import get_config
            config = get_config()
        return bool(config.get("auto_memory_enabled", False))
    except Exception:
        # 与旧实现保持一致：config 读取失败时默认 True
        return True


def get_memory_mode(config: Optional[Dict[str, Any]] = None) -> str:
    """读取 ``memory.mode``：``cloud`` 或 ``local``（默认）。"""
    memory_cfg = (config or {}).get("memory", {})
    mode = str(memory_cfg.get("mode") or "local").strip().lower()
    return "cloud" if mode == "cloud" else "local"
