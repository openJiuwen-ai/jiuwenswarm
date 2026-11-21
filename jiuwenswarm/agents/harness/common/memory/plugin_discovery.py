# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Generic loader for user-installed memory plugins.

Discovers providers under ``~/.jiuwenswarm/plugins/memory/<name>/``; each
plugin directory must expose a single-file ``plugin.py`` (sibling imports need
the plugin dir on sys.path, injected by the entry itself) with a
``build_provider(config) -> provider | None`` factory; failures -> warning + None.
"""

import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

PLUGIN_ROOT = Path.home() / ".jiuwenswarm" / "plugins" / "memory"
ENTRY_MODULE, ENTRY_FACTORY = "plugin", "build_provider"


def load_memory_plugin(name: str, config: Optional[Dict[str, Any]] = None,
                       allowed_plugins: Optional[List[str]] = None,
                       plugin_root: Optional[Path] = None):
    """Load plugin ``name`` and build its provider via the entry factory.

    ``allowed_plugins`` (when given) is a strict whitelist; ``None`` means no
    whitelist configured. ``plugin_root`` overrides the discovery directory.
    String values are normalised by comma-splitting (YAML scalar form); a
    sequence is element-wise stringified; any other type fails closed.
    """
    if not name or not name.isascii() or not all(ch.isalnum() or ch in "-_" for ch in name):
        logger.warning("[PluginDiscovery] plugin '%s' rejected: unsafe name", name)
        return None
    if allowed_plugins is not None:
        # P2（atomgit 2026-11-18 #201711173）：类型归一——YAML 写成标量
        # （`allowed_plugins: hindsight-memory`）时值为 str，`name in allowed_plugins`
        # 退化为子串匹配：`hindsight` 可穿过只允许 `hindsight-memory` 的白名单，
        # 严格白名单门被放宽。标量按逗号切分为列表、序列逐元素 str 化；
        # 非法类型 fail-closed 直接拒绝，不得放宽为“视为无白名单”。
        if isinstance(allowed_plugins, str):
            allowed_plugins = [p.strip() for p in allowed_plugins.split(",") if p.strip()]
        elif isinstance(allowed_plugins, (list, tuple, set)):
            allowed_plugins = [str(p).strip() for p in allowed_plugins]
        else:
            logger.warning(
                "[PluginDiscovery] allowed_plugins invalid type %s, plugin rejected (fail-closed)",
                type(allowed_plugins).__name__,
            )
            return None
    if allowed_plugins is not None and name not in allowed_plugins:
        logger.warning("[PluginDiscovery] plugin '%s' rejected by allowed_plugins", name)
        return None
    root = Path(plugin_root) if plugin_root is not None else PLUGIN_ROOT
    entry_file = root / name / f"{ENTRY_MODULE}.py"
    if not entry_file.is_file():
        logger.warning("[PluginDiscovery] plugin '%s' entry not found: %s", name, entry_file)
        return None
    try:
        # P3（atomgit 2026-11-18）：name 字符集已在前置校验限定为
        # ASCII alnum/-/_（点号被拒），原 replace(".", "_") 为不可达死代码。
        spec_name = f"jiuwenswarm_plugins_memory_{name}"
        spec = importlib.util.spec_from_file_location(spec_name, entry_file)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot build import spec for {entry_file}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec_name] = module  # register before exec (dataclass/reflection safety)
        spec.loader.exec_module(module)
        factory = getattr(module, ENTRY_FACTORY, None)
        if not callable(factory):
            logger.warning("[PluginDiscovery] plugin '%s' has no callable %s", name, ENTRY_FACTORY)
            return None
        return factory(config or {})
    except Exception as exc:
        logger.warning("[PluginDiscovery] plugin '%s' load failed: %s", name, exc)
        return None
