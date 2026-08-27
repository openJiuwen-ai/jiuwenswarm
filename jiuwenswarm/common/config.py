# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

import json
import logging
import os
import re
import sys
import threading
import time
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any, Optional

from ruamel.yaml import YAML
from ruamel.yaml.scalarstring import DoubleQuotedScalarString, PlainScalarString
import yaml
import portalocker

from jiuwenswarm.common.kv_cache_affinity_config import (
    ASCEND_AFFINITY_PROVIDER,
    get_default_model_provider as resolve_default_model_provider,
    set_default_model_provider_in_entries,
    validate_affinity_invariant,
)
from jiuwenswarm.common.utils import (
    get_config_dir,
    get_config_file,
)

logger = logging.getLogger(__name__)

_CONFIG_MODULE_DIR = Path(__file__).parent
CONFIG_YAML_PATH = get_config_file()
SWARMFLOW_ENABLED_CONFIG_PATH = ("modes", "team", "jiuwen_team", "enable_swarmflow")
SWARMFLOW_BUDGET_CONFIG_PATH = ("modes", "team", "jiuwen_team", "swarmflow_budget")
DEFAULT_SWARMFLOW_ENABLED = False
EXTERNAL_CLI_PUBLISH_URL_FRONT_KEY = "external_cli_publish_url"
EXTERNAL_CLI_AGENTS_CONFIG_PATH = ("modes", "team", "jiuwen_team", "external_cli_agents")
EXTERNAL_TRANSPORT_CONFIG_PATH = ("modes", "team", "jiuwen_team", "external_transport")
_ALLOWED_EXTERNAL_CLI_AGENTS = {"claude", "codex"}
# Keep progressive tool search enabled by default for existing user workspaces
# whose config.yaml predates this switch.
DEFAULT_PROGRESSIVE_TOOL_ENABLED = False
# Check if user workspace exists and use it if configured via env
_user_config = os.getenv("JIUWENSWARM_CONFIG_DIR")
if _user_config:
    _CONFIG_MODULE_DIR = Path(_user_config)
elif get_config_dir().exists():
    _CONFIG_MODULE_DIR = get_config_dir()

# Ensure config directory is in sys.path
if str(_CONFIG_MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(_CONFIG_MODULE_DIR))


def resolve_env_vars(value: Any) -> Any:
    """递归解析配置中的环境变量替换语法 ${VAR:-default}.

    Args:
        value: 配置值，可能是字符串、字典或列表

    Returns:
        解析后的值
    """
    if isinstance(value, str):
        # 匹配 ${VAR:-default} 格式
        pattern = r'\$\{([^:}]+)(?::-([^}]*))?\}'

        def replace_env(match):
            var_name = match.group(1)
            default = match.group(2)
            current = os.getenv(var_name)
            is_need_decrypt = ("api_key" in var_name.lower() or "token" in var_name.lower()) and current
            reg_mod = sys.modules.get("jiuwenswarm.extensions.registry")
            if reg_mod is not None and hasattr(reg_mod, "ExtensionRegistry"):
                try:
                    reg = reg_mod.ExtensionRegistry.get_instance()
                    crypto = reg.get_crypto_provider()
                    if is_need_decrypt and crypto:
                        current = crypto.decrypt(current)
                except Exception:
                    logger.debug(
                        "Crypto provider unavailable while resolving env var %s; using raw value",
                        var_name,
                        exc_info=True,
                    )
            # Bash: ${VAR:-default} uses default when VAR is unset OR empty.
            # ${VAR} (no :-) keeps getenv behavior; unset -> "".
            if default is not None:
                if current is None or current == "":
                    return default
                return current
            return current if current is not None else ""

        return re.sub(pattern, replace_env, value)
    elif isinstance(value, dict):
        # mcp.servers entries hold ${VAR} placeholders in headers/env that
        # are meant for the CredentialStore (resolved at McpServerConfig build
        # time), NOT for the process env. If resolve_env_vars touches them
        # here, an unset env var collapses ${GITHUB_TOKEN} to "" and the
        # adapter receives ``Authorization: "Bearer "`` — an empty token that
        # httpx rejects as ``Illegal header value b'Bearer '``. Preserve these
        # credential subtrees verbatim on mcp server entries (identified by
        # the transport + name + url/command shape, or the server_id_scope
        # stamp the mcp registry adds).
        mcp_credential_keys = ("headers", "env", "staticHeaders", "static_headers")
        is_mcp_server_entry = (
            "transport" in value
            and "name" in value
            and ("url" in value or "command" in value)
        ) or "server_id_scope" in value
        if is_mcp_server_entry:
            return {
                k: (v if k in mcp_credential_keys else resolve_env_vars(v))
                for k, v in value.items()
            }
        return {k: resolve_env_vars(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [resolve_env_vars(item) for item in value]
    else:
        return value


def _normalize_config(config: dict[str, Any] | None) -> None:
    """后处理配置，将需要结构化的字符串字段解析为原生类型。

    例如 custom_headers 在 YAML 中通过环境变量传入时是 JSON 字符串，
    需要统一解析为 dict。
    """
    if config is None:
        return
    models = config.get("models", {})
    if isinstance(models, dict):
        for entry in models.values():
            if isinstance(entry, dict):
                mcc = entry.get("model_client_config")
                if isinstance(mcc, dict) and "custom_headers" in mcc:
                    mcc["custom_headers"] = _parse_custom_headers(mcc["custom_headers"])

    react = config.get("react", {})
    if isinstance(react, dict):
        mcc = react.get("model_client_config")
        if isinstance(mcc, dict) and "custom_headers" in mcc:
            mcc["custom_headers"] = _parse_custom_headers(mcc["custom_headers"])
        kv_cfg = react.get("kv_cache_affinity_config")
        if isinstance(kv_cfg, dict) and kv_cfg.get("enable_kv_cache_affinity", False):
            provider = get_default_model_provider(config)
            if provider != ASCEND_AFFINITY_PROVIDER:
                logger.warning(
                    "KV cache affinity configuration failed closed: default provider=%s requires=%s",
                    provider or "<empty>",
                    ASCEND_AFFINITY_PROVIDER,
                )
                # Runtime-only normalization: preserve the user's file for
                # diagnosis, but never activate an inconsistent configuration.
                kv_cfg["enable_kv_cache_affinity"] = False
    # send_file 工具默认开关：web/feishu/xiaoyi 顶层缺 send_file_allowed 时兜底 True。
    channels = config.get("channels", {})
    for _ch in ("web", "feishu", "xiaoyi"):
        _ch_conf = channels.get(_ch)
        if not isinstance(_ch_conf, dict):
            _ch_conf = {}
            channels[_ch] = _ch_conf
        if _ch_conf.get("send_file_allowed") is None:
            _ch_conf["send_file_allowed"] = True


# Parsed YAML per file path, keyed on the file's identity so an edit — by this
# process or any other — invalidates it. Only the parse is cached: reading and
# parsing config.yaml costs ~11 ms while everything downstream of it (env
# resolution, normalization) costs ~0.3 ms, and ``get_config`` sits on every
# request path. Deliberately not caching past the parse keeps env var changes
# (``load_dotenv_runtime`` runs with override=True before some reads) taking
# effect immediately, so this needs no explicit invalidation hook.
_YAML_PARSE_CACHE: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}
_YAML_PARSE_CACHE_LOCK = threading.Lock()


def _yaml_file_stamp(filepath: Path) -> tuple[int, int] | None:
    """Return the identity a cached parse of this file is keyed on.

    Args:
        filepath: YAML file to stamp.

    Returns:
        ``(mtime_ns, size)``, or None when the file cannot be stat'd — in which
        case the caller must not use or populate the cache.
    """
    try:
        stat_result = filepath.stat()
    except OSError:
        return None
    return stat_result.st_mtime_ns, stat_result.st_size


def _read_with_retry(filepath: Path, max_attempts: int = 3) -> dict[str, Any]:
    """读取 YAML，遇解析错误重试（应对跨进程写竞态）。

    解析结果按文件身份（mtime + size）缓存；调用方会就地修改返回值
    （``_normalize_config`` 即是），故命中与写入缓存时都做深拷贝，
    保证各调用方拿到互不影响的副本。

    Args:
        filepath: 待读取的 YAML 文件路径。
        max_attempts: 解析失败时的最大尝试次数。

    Returns:
        解析后的字典；文件为空时返回空字典。
    """
    stamp = _yaml_file_stamp(filepath)
    cache_key = str(filepath)
    if stamp is not None:
        with _YAML_PARSE_CACHE_LOCK:
            cached = _YAML_PARSE_CACHE.get(cache_key)
        if cached is not None and cached[0] == stamp:
            return deepcopy(cached[1])

    for attempt in range(max_attempts):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                parsed = yaml.safe_load(f) or {}
            if stamp is not None:
                with _YAML_PARSE_CACHE_LOCK:
                    _YAML_PARSE_CACHE[cache_key] = (stamp, deepcopy(parsed))
            return parsed
        except yaml.YAMLError:
            if attempt < max_attempts - 1:
                time.sleep(0.05 * (attempt + 1))
                continue
            raise
    return {}


def get_config():
    """读取并解析 config.yaml（锁保护 + 跨进程降级重试）。"""
    config_base = _read_with_retry(get_config_file())
    # resolve_env_vars 和 _normalize_config 只操作内存数据，在锁外执行以缩短锁持有时间
    config_base = resolve_env_vars(config_base)
    _normalize_config(config_base)

    return config_base


def get_configured_read_image_multimodal(
    config: dict[str, Any] | None = None,
) -> bool | None:
    """Return the explicit native-image policy, or ``None`` for auto mode."""

    resolved = config if isinstance(config, dict) else get_config()
    react = resolved.get("react")
    value = (
        react.get("enable_read_image_multimodal")
        if isinstance(react, dict)
        else None
    )
    return value if isinstance(value, bool) else None


def get_config_raw():
    """读 config.yaml 原始内容（不解析环境变量），供局部更新后写回。"""
    return _read_with_retry(CONFIG_YAML_PATH)


def get_default_model_provider(config: dict[str, Any] | None) -> str:
    return resolve_default_model_provider(config)


def validate_persisted_kv_cache_affinity() -> tuple[bool, list[str]]:
    """Validate the persisted KVC invariant after a config write."""
    raw = get_config_raw()
    return validate_affinity_invariant(resolve_env_vars(raw))


def set_config(config):
    with open(CONFIG_YAML_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(config, f, allow_unicode=True, sort_keys=False)


def _get_evolution_config(config: dict[str, Any] | None) -> dict[str, Any]:
    """Return the canonical ``react.evolution`` mapping.

    Evolution settings deliberately have one source of truth.  In particular,
    the historical top-level ``evolution`` mapping and the old environment
    overrides are not consulted here; this keeps a stale deployment variable
    from changing a running agent's capability set.
    """
    if not isinstance(config, dict):
        return {}
    react_config = config.get("react")
    if not isinstance(react_config, dict):
        return {}
    evolution_config = react_config.get("evolution")
    return evolution_config if isinstance(evolution_config, dict) else {}


def get_skill_evolution_enabled(config: dict[str, Any] | None) -> bool:
    """Return the canonical ``react.evolution.skill_evolution`` switch."""
    return _get_evolution_config(config).get("skill_evolution") is True


def is_subagent_runtime_enabled(config: dict[str, Any] | None = None) -> bool:
    """Return ``react.subagent_runtime.enabled`` for persistent subagent tools."""
    cfg = config or get_config()
    react = cfg.get("react") if isinstance(cfg, dict) else None
    runtime_cfg = react.get("subagent_runtime") if isinstance(react, dict) else None
    return bool(runtime_cfg.get("enabled")) if isinstance(runtime_cfg, dict) else False


def get_progressive_tool_enabled(config: dict[str, Any] | None = None) -> bool:
    """Return whether the ProgressiveToolRail is enabled for an agent.

    The switch is the top-level ``progressive_tool_enabled`` key in
    ``config.yaml``.  A missing key keeps the historical enabled-by-default
    behavior for workspaces initialized before this setting was introduced.
    """
    if not isinstance(config, dict):
        return DEFAULT_PROGRESSIVE_TOOL_ENABLED

    value = config.get("progressive_tool_enabled", DEFAULT_PROGRESSIVE_TOOL_ENABLED)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    return bool(value)


def get_evolution_review_feedback_min_confidence(config: dict[str, Any] | None) -> float:
    """Return the minimum confidence required for reviewer-driven evolution."""

    raw = _get_evolution_config(config).get("review_feedback_min_confidence", 0.7)
    try:
        return max(0.0, min(1.0, float(raw)))
    except (TypeError, ValueError):
        return 0.7


def get_evolution_auto_save_enabled(config: dict[str, Any] | None = None) -> bool:
    """Return canonical ``react.evolution.auto_save`` without disk/env reads."""
    return _get_evolution_config(config).get("auto_save") is True


def update_skill_evolution_enabled_in_config(enabled: bool) -> None:
    """Atomically update the canonical evolution capability switch."""

    def mutator(data: dict[str, Any]) -> dict[str, Any]:
        react = data.get("react")
        if not isinstance(react, dict):
            react = {}
            data["react"] = react
        evolution = react.get("evolution")
        if not isinstance(evolution, dict):
            evolution = {}
            react["evolution"] = evolution
        evolution["skill_evolution"] = bool(enabled)
        return data

    update_config(mutator)


def set_auto_memory_enabled(enabled: bool) -> None:
    """Set auto-memory enabled status in config.

    Args:
        enabled: True to enable, False to disable.
    """
    data = load_yaml_round_trip(CONFIG_YAML_PATH)
    data["auto_memory_enabled"] = enabled
    dump_yaml_round_trip(CONFIG_YAML_PATH, data)


def load_yaml_round_trip(config_path: Path):
    """ruamel 加载 config，保留注释与格式。"""
    rt = YAML()
    rt.preserve_quotes = True
    with open(config_path, "r", encoding="utf-8") as f:
        return rt.load(f)


def dump_yaml_round_trip(config_path: Path, data: Any) -> None:
    """ruamel 写回 config，保留注释与格式（原子写入：临时文件 + os.replace）。"""
    rt = YAML()
    rt.preserve_quotes = True
    rt.default_flow_style = False
    rt.indent(mapping=2, sequence=4, offset=2)
    rt.width = 4096
    tmp = config_path.with_name(f"{config_path.stem}.{uuid.uuid4().hex}.yaml.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            rt.dump(data, f)
        _atomic_replace(tmp, config_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _atomic_replace(src: Path, dst: Path, max_attempts: int = 10) -> None:
    """os.replace 重试：应对 Windows 下目标文件被并发占用导致的 PermissionError。

    max_attempts 为总尝试次数（含首次），非重试次数；至少尝试 1 次。
    """
    for attempt in range(max(1, max_attempts)):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == max(1, max_attempts) - 1:
                raise
            time.sleep(0.002 * (attempt + 1))


# 进程内不可重入锁。portalocker 文件锁同样不可同进程重入，
# 因此 update_config 的 mutator 内禁止再调用任何走 update_config 的函数
# （如 ensure_defaults_list_in_config / update_default_models_in_config），
# 否则同进程二次获取锁会死锁。展示用数据请在事务外、另起独立事务读取。
_CONFIG_WRITE_LOCK = threading.Lock()


def _config_lock_path(config_path: Path) -> Path:
    """锁文件路径跟随当前 CONFIG_YAML_PATH，避免模块加载时静态绑定。"""
    return config_path.with_name(f"{config_path.stem}.lock")


def update_config(mutator, *, lock_timeout: float = 10.0) -> Any:
    """跨进程互斥地读-改-写 config.yaml。

    portalocker 文件锁防跨进程并发（AgentServer+Gateway 两个 PID 同时写），
    threading.Lock 防同进程多线程。整个 load→mutate→dump 在锁内为原子临界区。

    警告：双层锁均不可重入。mutator 内不得调用任何会再次进入 update_config
    的函数（ensure_defaults_list_in_config、update_default_models_in_config 等），
    否则同进程二次获取锁将死锁。如需在写盘后读取展示数据，请在 update_config
    返回后另起一次独立调用（此时锁已释放，安全）。
    """
    with _CONFIG_WRITE_LOCK:
        with portalocker.Lock(
            str(_config_lock_path(CONFIG_YAML_PATH)),
            timeout=lock_timeout,
        ):
            data = load_yaml_round_trip(CONFIG_YAML_PATH)
            if data is None:
                data = {}
            new_data = mutator(data)
            if new_data is None:
                return data
            dump_yaml_round_trip(CONFIG_YAML_PATH, new_data)
            return new_data


# Backward-compat aliases — downstream modules import the underscore-prefixed names
_CONFIG_YAML_PATH = CONFIG_YAML_PATH
_load_yaml_round_trip = load_yaml_round_trip
_dump_yaml_round_trip = dump_yaml_round_trip


def update_health_check_in_config(payload: dict[str, Any]) -> None:
    """只更新 health_check 段并写回。"""
    def _mutate(data: dict[str, Any]) -> dict[str, Any]:
        current = data.get("health_check")
        if not isinstance(current, dict):
            current = {}
            data["health_check"] = current
        for key in ("every", "target", "active_hours"):
            if key in payload:
                current[key] = payload[key]
        return data

    update_config(_mutate)


def migrate_legacy_heartbeat_probe_config() -> bool:
    """Move legacy probe keys to health_check without touching heartbeat.jobs."""
    changed = False
    probe_keys = ("every", "target", "active_hours")

    def _mutate(data: dict[str, Any]) -> dict[str, Any] | None:
        nonlocal changed
        legacy = data.get("heartbeat")
        if not isinstance(legacy, dict):
            return None
        present = [key for key in probe_keys if key in legacy]
        if not present:
            return None
        current = data.get("health_check")
        if not isinstance(current, dict):
            current = {}
            data["health_check"] = current
        for key in present:
            current.setdefault(key, legacy[key])
            legacy.pop(key, None)
        if not legacy:
            data.pop("heartbeat", None)
        changed = True
        return data

    update_config(_mutate)
    return changed


def update_channel_in_config(channel_id: str, conf: dict[str, Any]) -> None:
    """只更新 channels[channel_id] 并写回。"""
    data = load_yaml_round_trip(CONFIG_YAML_PATH)
    if "channels" not in data:
        data["channels"] = {}
    channels = data["channels"]
    if channel_id not in channels:
        channels[channel_id] = {}
    section = channels[channel_id]
    for k, v in conf.items():
        section[k] = v
    dump_yaml_round_trip(CONFIG_YAML_PATH, data)


def _as_plain_yaml_str(value: Any) -> Any:
    """字符串写成无引号 plain scalar，避免顶层/apps 因旧值风格（如 ``''``）不一致。"""
    if isinstance(value, str):
        return PlainScalarString(value)
    return value


def update_xiaoyi_runtime_in_config(
    conf: dict[str, Any],
    *,
    api_id: str = "",
    agent_id: str = "",
) -> None:
    """更新 ``channels.xiaoyi`` 运行时身份，并在存在 ``push_id`` 时同步写入 ``apps[]``。

    顶层字段（``last_session_id`` / ``last_task_id`` / ``push_id`` 等）供 cron 等读；
    ``apps[].push_id`` 供频道重启后 ``XiaoyiChannelConfig`` 加载。一次 IO 写完，避免不一致。

    ``apps`` 匹配优先级：``api_id`` → ``agent_id`` → ``is_default`` → 唯一 app。
    """
    data = load_yaml_round_trip(CONFIG_YAML_PATH)
    if "channels" not in data:
        data["channels"] = {}
    channels = data["channels"]
    if "xiaoyi" not in channels or not isinstance(channels["xiaoyi"], dict):
        channels["xiaoyi"] = {}
    section = channels["xiaoyi"]
    for k, v in conf.items():
        section[k] = _as_plain_yaml_str(v)

    push_id = conf.get("push_id")
    if push_id:
        plain_push_id = _as_plain_yaml_str(str(push_id))
        apps = section.get("apps")
        if isinstance(apps, list) and apps:
            target: dict[str, Any] | None = None
            api_id = str(api_id or "").strip()
            agent_id = str(agent_id or "").strip()
            if api_id:
                for app in apps:
                    if isinstance(app, dict) and str(app.get("api_id") or "").strip() == api_id:
                        target = app
                        break
            if target is None and agent_id:
                for app in apps:
                    if isinstance(app, dict) and str(app.get("agent_id") or "").strip() == agent_id:
                        target = app
                        break
            if target is None:
                for app in apps:
                    if isinstance(app, dict) and app.get("is_default", False):
                        target = app
                        break
            if target is None and len(apps) == 1 and isinstance(apps[0], dict):
                target = apps[0]
            if target is not None:
                target["push_id"] = plain_push_id

    dump_yaml_round_trip(CONFIG_YAML_PATH, data)


def update_channel_subsection_in_config(
    channel_id: str,
    subsection_id: str,
    conf: dict[str, Any] | list[Any] | Any,
) -> None:
    """更新 channels[channel_id][subsection_id] 并写回。

    若 ``conf`` 为 dict，则合并（partial update）到现有 subsection 中；
    若为 list 或其他非 dict 类型，则整体替换 subsection。
    """
    data = load_yaml_round_trip(CONFIG_YAML_PATH)
    if "channels" not in data:
        data["channels"] = {}
    channels = data["channels"]
    if channel_id not in channels:
        channels[channel_id] = {}
    section = channels[channel_id]

    if isinstance(conf, dict):
        # 合并（partial update）—— 用于 feishu_enterprise 按 bot 维度保存状态等场景
        if subsection_id not in section:
            section[subsection_id] = {}
        for k, v in conf.items():
            section[subsection_id][k] = v
    else:
        # 整体替换 —— 用于 apps 列表等非 dict 类型
        section[subsection_id] = conf
    dump_yaml_round_trip(CONFIG_YAML_PATH, data)


def replace_channel_subsection_with_cleanup(
    channel_id: str,
    subsection_id: str,
    conf: dict[str, Any] | list[Any] | Any,
    keep_keys: set[str],
) -> None:
    """整体替换 channels[channel_id][subsection_id] 并清理旧字段，一次 IO 完成。

    写入 subsection 后，删除 channels[channel_id] 下不在 ``keep_keys`` 中的字段。
    用于多应用模式下替换 apps 并清理旧平铺字段，避免两套数据源不一致。
    """
    data = load_yaml_round_trip(CONFIG_YAML_PATH)
    if "channels" not in data:
        data["channels"] = {}
    channels = data["channels"]
    if channel_id not in channels:
        channels[channel_id] = {}
    section = channels[channel_id]

    section[subsection_id] = conf

    for k in list(section.keys()):
        if k not in keep_keys:
            del section[k]

    dump_yaml_round_trip(CONFIG_YAML_PATH, data)


def update_channel_app_field(
    channel_id: str,
    app_identifier: str,
    field_values: dict[str, Any],
    *,
    app_id_key: str = "app_id",
) -> bool:
    """更新 ``channels[channel_id].apps`` 列表中某个 app 条目的字段。

    用于多应用模式下运行时状态（如 ``last_chat_id``、``bot_open_id``）写回
    对应 app 条目，避免所有 app 争抢同一个平铺字段。

    Args:
        channel_id: 通道 ID，如 ``"feishu"`` / ``"xiaoyi"``。
        app_identifier: app 标识值，如 ``"cli_a"``。
        field_values: 要更新的字段 dict，如 ``{"last_chat_id": "oc_xxx"}``。
        app_id_key: 匹配 app 条目的键名，默认 ``"app_id"``。

    Returns:
        ``True`` 表示更新成功，``False`` 表示未找到匹配 app。
    """
    data = load_yaml_round_trip(CONFIG_YAML_PATH)
    apps = data.get("channels", {}).get(channel_id, {}).get("apps", [])
    if not isinstance(apps, list):
        return False
    for app in apps:
        if isinstance(app, dict) and app.get(app_id_key) == app_identifier:
            app.update(field_values)
            dump_yaml_round_trip(CONFIG_YAML_PATH, data)
            return True
    return False


def update_preferred_language_in_config(lang: str) -> None:
    """只更新顶层 preferred_language 并写回。非法值回退为 zh，与 set_preferred_language_in_config_file 一致。"""
    normalized = str(lang or "zh").strip().lower()
    if normalized not in ("zh", "en"):
        normalized = "zh"
    data = load_yaml_round_trip(CONFIG_YAML_PATH)
    data["preferred_language"] = normalized
    dump_yaml_round_trip(CONFIG_YAML_PATH, data)


def set_preferred_language_in_config_file(config_path: Path, lang: str) -> None:
    """将 preferred_language 写入指定 config.yaml（用于 init 等尚未绑定全局路径的场景）。"""
    lang = str(lang or "zh").strip().lower()
    if lang not in ("zh", "en"):
        lang = "zh"
    if not config_path.exists():
        return
    data = load_yaml_round_trip(config_path)
    data["preferred_language"] = lang
    dump_yaml_round_trip(config_path, data)


def update_browser_in_config(updates: dict[str, Any]) -> None:
    """只更新 browser 段（如 chrome_path）并写回。"""
    data = load_yaml_round_trip(CONFIG_YAML_PATH)
    if "browser" not in data:
        data["browser"] = {}
    section = data["browser"]
    for k, v in updates.items():
        section[k] = v
    dump_yaml_round_trip(CONFIG_YAML_PATH, data)


def update_context_engine_enabled_in_config(value: bool) -> None:
    """更新 react.context_engine_config.enabled（上下文压缩开关）并写回。"""
    data = load_yaml_round_trip(CONFIG_YAML_PATH)
    if "react" not in data:
        data["react"] = {}
    react = data["react"]
    if "context_engine_config" not in react:
        react["context_engine_config"] = {}
    react["context_engine_config"]["enabled"] = value
    dump_yaml_round_trip(CONFIG_YAML_PATH, data)


def update_kv_cache_affinity_enabled_in_config(value: bool) -> None:
    """更新 react.kv_cache_affinity_config.enable_kv_cache_affinity 并写回。"""
    data = load_yaml_round_trip(CONFIG_YAML_PATH)
    if "react" not in data:
        data["react"] = {}
    react = data["react"]
    if "kv_cache_affinity_config" not in react:
        react["kv_cache_affinity_config"] = {}
    react["kv_cache_affinity_config"]["enable_kv_cache_affinity"] = value
    if value:
        react["kv_cache_affinity_config"]["enable_kv_cache_release"] = False
    dump_yaml_round_trip(CONFIG_YAML_PATH, data)


def update_kv_cache_release_enabled_in_config(value: bool) -> None:
    """更新 react.kv_cache_affinity_config.enable_kv_cache_release 并写回。"""
    data = load_yaml_round_trip(CONFIG_YAML_PATH)
    if "react" not in data:
        data["react"] = {}
    react = data["react"]
    if "kv_cache_affinity_config" not in react:
        react["kv_cache_affinity_config"] = {}
    react["kv_cache_affinity_config"]["enable_kv_cache_release"] = value
    if value:
        react["kv_cache_affinity_config"]["enable_kv_cache_affinity"] = False
    dump_yaml_round_trip(CONFIG_YAML_PATH, data)


def _merge_config_dict(target: dict[str, Any], patch: dict[str, Any]) -> None:
    for key, value in patch.items():
        if isinstance(value, dict):
            child = target.get(key)
            if not isinstance(child, dict):
                child = {}
                target[key] = child
            _merge_config_dict(child, value)
        else:
            target[key] = value


def update_symphony_in_config(updates: dict[str, Any]) -> None:
    """更新 symphony 配置段并写回。"""
    data = load_yaml_round_trip(CONFIG_YAML_PATH)
    if "symphony" not in data or data["symphony"] is None:
        data["symphony"] = {}
    symphony = data["symphony"]
    _merge_config_dict(symphony, updates)
    dump_yaml_round_trip(CONFIG_YAML_PATH, data)


def update_skill_retrieval_in_config(updates: dict[str, Any]) -> None:
    """更新 symphony.skill_retrieval 配置段并写回。"""
    data = load_yaml_round_trip(CONFIG_YAML_PATH)
    if "symphony" not in data or data["symphony"] is None:
        data["symphony"] = {}
    symphony = data["symphony"]
    if "skill_retrieval" not in symphony or symphony["skill_retrieval"] is None:
        symphony["skill_retrieval"] = {}
    section = symphony["skill_retrieval"]
    _merge_config_dict(section, updates)
    dump_yaml_round_trip(CONFIG_YAML_PATH, data)


def update_permissions_enabled_in_config(value: bool) -> None:
    """更新 permissions.enabled（工具安全护栏开关）并写回。"""
    data = load_yaml_round_trip(CONFIG_YAML_PATH)
    if "permissions" not in data:
        data["permissions"] = {}
    data["permissions"]["enabled"] = value
    dump_yaml_round_trip(CONFIG_YAML_PATH, data)


def update_auto_recap_enabled_in_config(value: bool) -> None:
    """更新 auto_recap.enabled（自动回顾开关）并写回。"""
    data = load_yaml_round_trip(CONFIG_YAML_PATH)
    if "auto_recap" not in data:
        data["auto_recap"] = {}
    data["auto_recap"]["enabled"] = value
    dump_yaml_round_trip(CONFIG_YAML_PATH, data)


def update_setup_guide_enabled_in_config(value: bool) -> None:
    """原子更新 setup_guide.enabled（Web 首次配置引导开关）。"""
    def mutator(data: dict[str, Any]) -> dict[str, Any]:
        section = data.get("setup_guide")
        if not isinstance(section, dict):
            section = {}
            data["setup_guide"] = section
        section["enabled"] = value
        return data

    update_config(mutator)


def update_enable_free_models_in_config(value: bool) -> None:
    """原子更新 models.enable_free_models（Opencode Zen 免费模型开关）。"""
    def mutator(data: dict[str, Any]) -> dict[str, Any]:
        section = data.get("models")
        if not isinstance(section, dict):
            section = {}
            data["models"] = section
        section["enable_free_models"] = value
        return data

    update_config(mutator)


def update_proactive_recommendation_in_config(updates: dict[str, Any]) -> None:
    """更新 proactive_recommendation 配置段并写回。"""
    data = load_yaml_round_trip(CONFIG_YAML_PATH)
    if "proactive_recommendation" not in data or data["proactive_recommendation"] is None:
        data["proactive_recommendation"] = {}
    section = data["proactive_recommendation"]
    _merge_config_dict(section, updates)
    dump_yaml_round_trip(CONFIG_YAML_PATH, data)


def update_updater_in_config(updates: dict[str, Any]) -> None:
    """只更新 updater 段并写回。"""
    data = load_yaml_round_trip(CONFIG_YAML_PATH)
    if "updater" not in data:
        data["updater"] = {}
    section = data["updater"]
    for key, value in updates.items():
        section[key] = value
    dump_yaml_round_trip(CONFIG_YAML_PATH, data)


def update_memory_enabled_in_config(mode: str, value: bool) -> None:
    """更新 memory.enabled（记忆系统开关）并写回。"""
    _update_memory_in_modes_config(mode, "enabled", value)


def update_proactive_memory_in_config(mode: str, value: bool) -> None:
    """更新 memory.proactive_memory（主动记忆开关）并写回。"""
    _update_memory_in_modes_config(mode, "is_proactive", value)


def _update_memory_in_modes_config(mode: str, item: str, value: bool) -> None:
    data = load_yaml_round_trip(CONFIG_YAML_PATH)
    if "modes" not in data:
        data["modes"] = {}
    if "claw" not in data["modes"]:
        data["modes"]["claw"] = {}
    if mode not in data["modes"]["claw"]:
        data["modes"]["claw"][mode] = {}
    if "memory" not in data["modes"]["claw"][mode]:
        data["modes"]["claw"][mode]["memory"] = {}
    data["modes"]["claw"][mode]["memory"][item] = value
    dump_yaml_round_trip(CONFIG_YAML_PATH, data)


# ---------- 数字分身相关配置 ----------

def get_permissions_owner_scopes() -> dict[str, Any]:
    """读取 permissions.owner_scopes 及 deny_guidance_message."""
    cfg = get_config() or {}
    perm = cfg.get("permissions", {})
    return {
        "owner_scopes": perm.get("owner_scopes", {}),
        "deny_guidance_message": perm.get("deny_guidance_message", ""),
    }


def update_permissions_owner_scopes_in_config(
    owner_scopes: dict[str, Any],
    deny_guidance_message: str | None = None,
) -> None:
    """更新 permissions.owner_scopes（及可选 deny_guidance_message）并写回。"""
    data = load_yaml_round_trip(CONFIG_YAML_PATH)
    if "permissions" not in data:
        data["permissions"] = {}
    data["permissions"]["owner_scopes"] = owner_scopes
    if deny_guidance_message is not None:
        data["permissions"]["deny_guidance_message"] = deny_guidance_message
    dump_yaml_round_trip(CONFIG_YAML_PATH, data)


def get_permissions_deny_guidance() -> str:
    """读取 permissions.deny_guidance_message."""
    cfg = get_config() or {}
    return cfg.get("permissions", {}).get("deny_guidance_message", "")


def update_permissions_deny_guidance_in_config(msg: str) -> None:
    """更新 permissions.deny_guidance_message 并写回。"""
    data = load_yaml_round_trip(CONFIG_YAML_PATH)
    if "permissions" not in data:
        data["permissions"] = {}
    data["permissions"]["deny_guidance_message"] = msg
    dump_yaml_round_trip(CONFIG_YAML_PATH, data)


# ---------- Web UI：permissions.tools / rules / approval_overrides ----------

_VALID_PERM_LEVEL = frozenset({"allow", "ask", "deny"})
_VALID_RULE_SEVERITY = frozenset({"LOW", "MEDIUM", "HIGH", "CRITICAL"})
_RULE_MUTABLE_KEYS = frozenset({"tools", "pattern", "severity", "action", "description", "match_type"})


def get_permissions_tools() -> dict[str, Any]:
    """返回 ``permissions.tools``（原始结构，可能含 legacy dict）。"""
    cfg = get_config() or {}
    tools = (cfg.get("permissions") or {}).get("tools")
    if not isinstance(tools, dict):
        return {"tools": {}}
    return {"tools": dict(tools)}


def replace_permissions_tools_in_config(tools: Any) -> None:
    """整表替换 ``permissions.tools``；值仅允许 ``allow|ask|deny``（或 legacy ``{\"*\": level}``）。"""
    normalized = _validate_tools_map(tools)
    data = load_yaml_round_trip(CONFIG_YAML_PATH)
    if "permissions" not in data:
        data["permissions"] = {}
    data["permissions"]["tools"] = normalized
    dump_yaml_round_trip(CONFIG_YAML_PATH, data)


def update_permissions_tool_in_config(tool_name: str, level: Any) -> dict[str, Any]:
    """合并单条工具级别到 ``permissions.tools`` 并写回 YAML。

    Args:
        tool_name: 工具名（如 ``mcp_exec_command``），与 ``permissions.tools`` 键一致。
        level: ``allow`` / ``ask`` / ``deny`` 字符串，或 legacy ``{\"*\": level}``。

    Returns:
        ``{\"tools\": {...}}`` 更新后的完整 tools 映射（便于前端刷新）。
    """
    name = str(tool_name).strip()
    if not name:
        raise ValueError("tool name must be non-empty")
    piece = _validate_tools_map({name: level})
    data = load_yaml_round_trip(CONFIG_YAML_PATH)
    if "permissions" not in data:
        data["permissions"] = {}
    existing = data["permissions"].get("tools")
    if not isinstance(existing, dict):
        existing = {}
    merged = {str(k): v for k, v in existing.items()}
    merged[name] = piece[name]
    data["permissions"]["tools"] = merged
    dump_yaml_round_trip(CONFIG_YAML_PATH, data)
    return {"tools": dict(merged)}


def delete_permissions_tool_in_config(tool_name: str) -> bool:
    """从 ``permissions.tools`` 中删除一个键；不存在则返回 False。"""
    name = str(tool_name).strip()
    if not name:
        raise ValueError("tool name must be non-empty")
    data = load_yaml_round_trip(CONFIG_YAML_PATH)
    if "permissions" not in data:
        return False
    tools = data["permissions"].get("tools")
    if not isinstance(tools, dict):
        return False
    key_to_drop = None
    for k in tools:
        if str(k).strip() == name:
            key_to_drop = k
            break
    if key_to_drop is None:
        return False
    new_tools = {k: v for k, v in tools.items() if k != key_to_drop}
    data["permissions"]["tools"] = new_tools
    dump_yaml_round_trip(CONFIG_YAML_PATH, data)
    return True


def _validate_tools_map(tools: Any) -> dict[str, str]:
    if not isinstance(tools, dict):
        raise ValueError("tools must be an object")
    out: dict[str, str] = {}
    for k, v in tools.items():
        name = str(k).strip()
        if not name:
            raise ValueError("tool name must be non-empty")
        if isinstance(v, dict) and isinstance(v.get("*"), str):
            level = str(v["*"]).strip().lower()
        elif isinstance(v, str):
            level = v.strip().lower()
        else:
            raise ValueError(f"tools[{name!r}]: value must be allow|ask|deny or object {{'*': level}}")
        if level not in _VALID_PERM_LEVEL:
            raise ValueError(f"tools[{name!r}]: invalid level {level!r}")
        out[name] = level
    return out


def get_permissions_rules() -> dict[str, Any]:
    """返回 ``permissions.rules`` 列表（仅 dict 项）。"""
    cfg = get_config() or {}
    rules = (cfg.get("permissions") or {}).get("rules")
    if not isinstance(rules, list):
        return {"rules": []}
    return {"rules": [r for r in rules if isinstance(r, dict)]}


def get_permissions_approval_overrides() -> dict[str, Any]:
    """返回 ``permissions.approval_overrides`` 列表（仅 dict 项）。"""
    cfg = get_config() or {}
    raw = (cfg.get("permissions") or {}).get("approval_overrides")
    if not isinstance(raw, list):
        return {"approval_overrides": []}
    return {"approval_overrides": [x for x in raw if isinstance(x, dict)]}


def create_permissions_rule_in_config(rule: dict[str, Any]) -> dict[str, Any]:
    """追加一条 ``permissions.rules`` 项，返回落盘后的规则（含 ``id``）。"""
    if not isinstance(rule, dict):
        raise ValueError("rule must be an object")
    rid = str(rule.get("id") or "").strip() or f"ui_rule_{uuid.uuid4().hex[:12]}"
    stored: dict[str, Any] = {"id": rid}
    for key in _RULE_MUTABLE_KEYS:
        if key in rule and rule[key] is not None:
            stored[key] = rule[key]
    if "tools" not in stored or "pattern" not in stored:
        raise ValueError("tools and pattern are required")
    stored["tools"] = _normalize_rule_tools(stored["tools"])
    stored["pattern"] = str(stored["pattern"]).strip()
    if not stored["tools"]:
        raise ValueError("tools must be a non-empty list")
    if not stored["pattern"]:
        raise ValueError("pattern must be non-empty")
    _normalize_rule_severity_action(stored)

    data = load_yaml_round_trip(CONFIG_YAML_PATH)
    if "permissions" not in data:
        data["permissions"] = {}
    rules = data["permissions"].get("rules")
    if not isinstance(rules, list):
        rules = []
    if any(isinstance(r, dict) and str(r.get("id") or "").strip() == rid for r in rules):
        raise ValueError(f"rule id already exists: {rid}")
    rules.append(stored)
    data["permissions"]["rules"] = rules
    dump_yaml_round_trip(CONFIG_YAML_PATH, data)
    return stored


def update_permissions_rule_in_config(rule_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    """按 ``id`` 合并更新一条 rule。"""
    rid = str(rule_id or "").strip()
    if not rid:
        raise ValueError("id is required")
    if not isinstance(patch, dict):
        raise ValueError("patch must be an object")

    data = load_yaml_round_trip(CONFIG_YAML_PATH)
    if "permissions" not in data:
        data["permissions"] = {}
    rules = data["permissions"].get("rules")
    if not isinstance(rules, list):
        rules = []
    idx: int | None = None
    for i, r in enumerate(rules):
        if isinstance(r, dict) and str(r.get("id") or "").strip() == rid:
            idx = i
            break
    if idx is None:
        raise ValueError(f"rule not found: {rid}")

    merged: dict[str, Any] = dict(rules[idx])
    for k, v in patch.items():
        if k == "id":
            continue
        if k not in _RULE_MUTABLE_KEYS:
            continue
        if v is None:
            merged.pop(k, None)
        else:
            merged[k] = v
    merged["id"] = rid
    if "tools" in merged:
        merged["tools"] = _normalize_rule_tools(merged["tools"])
    if "pattern" in merged:
        merged["pattern"] = str(merged["pattern"]).strip()
    if not merged.get("tools"):
        raise ValueError("tools must be a non-empty list")
    if not merged.get("pattern"):
        raise ValueError("pattern must be non-empty")
    _normalize_rule_severity_action(merged)
    rules[idx] = merged
    data["permissions"]["rules"] = rules
    dump_yaml_round_trip(CONFIG_YAML_PATH, data)
    return merged


def delete_permissions_rule_in_config(rule_id: str) -> bool:
    """删除 ``permissions.rules`` 中指定 ``id``；若未找到返回 False。"""
    rid = str(rule_id or "").strip()
    if not rid:
        raise ValueError("id is required")
    data = load_yaml_round_trip(CONFIG_YAML_PATH)
    if "permissions" not in data:
        return False
    rules = data["permissions"].get("rules")
    if not isinstance(rules, list):
        return False
    new_rules = [r for r in rules if not (isinstance(r, dict) and str(r.get("id") or "").strip() == rid)]
    if len(new_rules) == len(rules):
        return False
    data["permissions"]["rules"] = new_rules
    dump_yaml_round_trip(CONFIG_YAML_PATH, data)
    return True


def delete_permissions_approval_override_in_config(override_id: str) -> bool:
    """按 ``id`` 删除 ``approval_overrides`` 中一项；若未找到返回 False。"""
    oid = str(override_id or "").strip()
    if not oid:
        raise ValueError("id is required")
    data = load_yaml_round_trip(CONFIG_YAML_PATH)
    if "permissions" not in data:
        return False
    ov = data["permissions"].get("approval_overrides")
    if not isinstance(ov, list):
        return False
    new_ov = [x for x in ov if not (isinstance(x, dict) and str(x.get("id") or "").strip() == oid)]
    if len(new_ov) == len(ov):
        return False
    data["permissions"]["approval_overrides"] = new_ov
    dump_yaml_round_trip(CONFIG_YAML_PATH, data)
    return True


def _normalize_rule_tools(raw: Any) -> list[str]:
    if isinstance(raw, str):
        s = raw.strip()
        return [s] if s else []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if isinstance(x, str) and str(x).strip()]
    raise ValueError("tools must be a string or array of strings")


def _normalize_rule_severity_action(rule: dict[str, Any]) -> None:
    if "severity" in rule:
        sev = str(rule["severity"]).strip().upper()
        if sev not in _VALID_RULE_SEVERITY:
            raise ValueError(f"invalid severity {sev!r}")
        rule["severity"] = sev
    if "action" in rule:
        act = str(rule["action"]).strip().lower()
        if act not in _VALID_PERM_LEVEL:
            raise ValueError(f"invalid action {act!r}")
        rule["action"] = act


def _parse_custom_headers(value: str | dict | None) -> dict[str, Any] | None:
    """解析 custom_headers 配置，支持 JSON 字符串格式或已解析的字典。

    Args:
        value: 环境变量值，可以是 None、空字符串、JSON 字符串或已解析的字典

    Returns:
        解析后的字典，如果输入为空或解析失败则返回 None
    """
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if not value or value.strip() == "":
        return None
    try:
        result = json.loads(value)
        if isinstance(result, dict):
            return result
        logger.warning(f"custom_headers must be a JSON object, got: {type(result).__name__}")
        return None
    except json.JSONDecodeError as e:
        logger.warning(f"custom_headers JSON parse failed: {e}")
        return None


def _infer_is_default(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """为模型条目列表推断 is_default 字段。

    规则：
    - 同 model_name 组内仅一个条目 → is_default = True
    - 同 model_name 组内多个条目 → 第一个为 True，其余为 False
    - 已有 is_default 字段且为 True 的条目保留，同组内其余置 False
    """
    from collections import OrderedDict
    import copy

    result = copy.deepcopy(entries)

    groups: OrderedDict[str, list[int]] = OrderedDict()
    for i, entry in enumerate(result):
        name = (entry.get("model_client_config") or {}).get("model_name", "")
        if name not in groups:
            groups[name] = []
        groups[name].append(i)

    for name, indices in groups.items():
        if len(indices) == 1:
            result[indices[0]]["is_default"] = True
            continue

        has_explicit = False
        for idx in indices:
            if result[idx].get("is_default") is True:
                has_explicit = True
                break

        if has_explicit:
            first_true = True
            for idx in indices:
                if result[idx].get("is_default") is True and first_true:
                    result[idx]["is_default"] = True
                    first_true = False
                else:
                    result[idx]["is_default"] = False
        else:
            for j, idx in enumerate(indices):
                result[idx]["is_default"] = j == 0

    return result


def _decrypt_model_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """解密模型条目中的 api_key 字段，返回深拷贝不改变原始数据。同时推断 is_default。"""
    import copy

    result = copy.deepcopy(entries)

    reg_mod = sys.modules.get("jiuwenswarm.extensions.registry")
    crypto = None
    if reg_mod is not None and hasattr(reg_mod, "ExtensionRegistry"):
        try:
            crypto = reg_mod.ExtensionRegistry.get_instance().get_crypto_provider()
        except Exception:
            logger.debug(
                "Crypto provider unavailable while decrypting model entries; "
                "api_key fields will be returned as stored",
                exc_info=True,
            )

    for entry in result:
        mcc = entry.get("model_client_config")
        if isinstance(mcc, dict):
            if mcc.get("api_key") and crypto:
                try:
                    mcc["api_key"] = crypto.decrypt(mcc["api_key"])
                except Exception:
                    logger.warning(
                        "Failed to decrypt api_key for model entry; using raw value",
                        exc_info=True,
                    )
            if "custom_headers" in mcc:
                mcc["custom_headers"] = _parse_custom_headers(mcc["custom_headers"])

    result = _infer_is_default(result)
    return result


def get_agentos_models(config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """读取 models.agentos 备份模型列表，返回带标记的条目。

    与 ``get_default_models`` 的 defaults 条目并列、同等可选可切换，但：
    - ``is_default`` 始终为 ``False``：绝不抢启动主对话默认（``_create_model``
      选默认时只取 ``is_default=True`` 的条目作为 ``self._model``）。
    - ``model_config_obj._source = "agentos"``：仅供前端 ``is_agentos`` 置灰只读
      展示用。``context_window``（模型支持的上下文总长度）每个模型条目均可配
      （defaults / agentos / video / audio / vision / image_gen 均可），放进 core 的
      ``ModelRequestConfig`` 供 core 人员取值。是否在 jiuwenswarm 出口 pop 由
      ``reasoning_injector.core_has_context_window_field`` 自动适配 core 字段状态：
      core 未加 context_window 正式字段时 pop 防发厂商，加字段后停止 pop 留给 core。

    仅当 ``models.agentos`` 是 list 且每条 ``model_client_config.model_name`` 非空
    （即用户已手动在 config.yaml 填入凭证）时才追加；非 list 视为未配置返回空。
    config.yaml 模板不预置 agentos 字段，需用户手动添加——此函数即"运行时检测
    config.yaml 是否有 agentos 字段"的落点。
    """
    if config is None:
        config = get_config()
    models = config.get("models", {})
    agentos_raw = models.get("agentos")
    agentos_list = agentos_raw if isinstance(agentos_raw, list) else []
    entries: list[dict[str, Any]] = []
    for agentos_block in agentos_list:
        if not isinstance(agentos_block, dict):
            continue
        mcc = agentos_block.get("model_client_config")
        if not (isinstance(mcc, dict) and mcc.get("model_name")):
            # model_name 为空 = 该条未配置，跳过不入缓存
            continue
        agentos_entry = deepcopy(agentos_block)
        agentos_entry["is_default"] = False
        # _source 注入到 model_config_obj 内部，仅供前端 is_agentos 置灰只读展示
        # （不再参与 context_window 出口判断——所有条目一视同仁）。context_window
        # 每个模型条目均可配，随之进入 kwargs，是否由 reasoning_injector
        # _build_model_request_kwargs 公共出口 pop 取决于 core 是否已把 context_window
        # 加为 ModelRequestConfig 正式字段（core_has_context_window_field 自动适配）：
        # core 未加字段时 pop 防发厂商，加字段后停止 pop，context_window 留在
        # ModelRequestConfig 供 core 读取。
        agentos_mco = agentos_entry.setdefault("model_config_obj", {})
        if isinstance(agentos_mco, dict):
            agentos_mco["_source"] = "agentos"
        entries.append(agentos_entry)
    return entries


def get_default_models(config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """获取默认模型列表，兼容新旧格式。

    优先级：models.defaults（列表） > models.default（单对象） > 环境变量回退
    返回的 api_key 已解密。每个条目可能含顶层 alias 字段。
    """
    if config is None:
        config = get_config()
    models = config.get("models", {})

    # 新格式：已有 defaults 列表
    if "defaults" in models and isinstance(models["defaults"], list) and models["defaults"]:
        entries = _decrypt_model_entries(models["defaults"])
        entries.extend(get_agentos_models(config))
        return entries

    # 旧格式：单个 default 对象 → 包装为列表
    if "default" in models and isinstance(models["default"], dict):
        entries = _decrypt_model_entries([models["default"]])
        entries.extend(get_agentos_models(config))
        return entries

    # 回退：从环境变量构造（env var 已在 resolve_env_vars 中解密）
    alias = os.getenv("MODEL_ALIAS", "")
    entry: dict[str, Any] = {
        "model_client_config": {
            "api_base": os.getenv("API_BASE", ""),
            "api_key": os.getenv("API_KEY", ""),
            "model_name": os.getenv("MODEL_NAME", ""),
            "client_provider": os.getenv("MODEL_PROVIDER", ""),
            # endpoint_profile：仅 OpenAI 协议生效的端点方言；Anthropic 时忽略。
            "endpoint_profile": os.getenv("ENDPOINT_PROFILE", "") or "",
            "custom_headers": _parse_custom_headers(os.getenv("CUSTOM_HEADERS", None)),
            "timeout": 1800,
            "verify_ssl": False,
        },
        "model_config_obj": {"temperature": 0.95},
    }
    if alias:
        entry["alias"] = alias
    entries = [entry]
    entries.extend(get_agentos_models(config))
    return entries


def update_default_models_in_config(models_list: list[dict[str, Any]]) -> None:
    def _mutate(data):
        if "models" not in data:
            data["models"] = {}
        for entry in models_list:
            if isinstance(entry, dict) and isinstance(entry.get("alias"), str):
                entry["alias"] = DoubleQuotedScalarString(entry["alias"])
        data["models"]["defaults"] = models_list
        if "default" in data["models"]:
            del data["models"]["default"]
        return data
    update_config(_mutate)


def update_default_model_provider_in_config(provider: str) -> bool:
    """Update only the default model provider in config.yaml.

    Returns True when a persisted model entry was changed. The default entry is
    the first entry with ``is_default: true``; if none is marked, the first
    ``models.defaults`` entry is used. This intentionally does not rewrite
    non-default models or agent-specific model bindings.
    """
    normalized_provider = str(provider or "").strip()
    if not normalized_provider:
        return False

    data = load_yaml_round_trip(CONFIG_YAML_PATH)
    models = data.get("models")
    if not isinstance(models, dict):
        return False

    defaults = models.get("defaults")
    if isinstance(defaults, list) and defaults:
        changed = set_default_model_provider_in_entries(
            defaults,
            normalized_provider,
        )
        if changed:
            dump_yaml_round_trip(CONFIG_YAML_PATH, data)
        return changed

    default_entry = models.get("default")
    if isinstance(default_entry, dict):
        mcc = default_entry.setdefault("model_client_config", {})
        if not isinstance(mcc, dict):
            mcc = {}
            default_entry["model_client_config"] = mcc
        if mcc.get("client_provider") == normalized_provider:
            return False
        mcc["client_provider"] = normalized_provider
        dump_yaml_round_trip(CONFIG_YAML_PATH, data)
        return True

    return False


def ensure_defaults_list_in_config() -> list[dict[str, Any]]:
    def _mutate(data):
        models = data.get("models") or {}
        if not isinstance(models, dict):
            models = {}
        defaults = models.get("defaults")
        if isinstance(defaults, list) and defaults:
            return None
        default_entry = models.get("default")
        if isinstance(default_entry, dict):
            if "is_default" not in default_entry:
                default_entry["is_default"] = True
            defaults_list = [default_entry]
        else:
            defaults_list = [{
                "model_client_config": {
                    "api_base": "${API_BASE}",
                    "api_key": "${API_KEY}",
                    "model_name": "${MODEL_NAME}",
                    "client_provider": "${MODEL_PROVIDER}",
                    "endpoint_profile": "${ENDPOINT_PROFILE:-openai}",
                },
                "model_config_obj": {"temperature": 0.95},
                "is_default": True,
            }]
        models["defaults"] = defaults_list
        data["models"] = models
        if "default" in data["models"]:
            del data["models"]["default"]
        return data

    result = update_config(_mutate)
    defs = (result.get("models") or {}).get("defaults")
    return defs if isinstance(defs, list) else []


def _require_dict(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be an object")
    return value


def _require_non_empty_string(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} must be non-empty")
    return text


def _transform_front_team_model_config(model_raw: dict[str, Any]) -> dict[str, Any]:
    model_client_config: dict[str, Any] = {}
    model_request_config: dict[str, Any] = {}

    # 从 models.defaults 列表中按 #index 查找完整配置
    model_value = model_raw.get("model")
    if model_value and isinstance(model_value, str) and "#" in model_value:
        sep = model_value.rfind("#")
        model_name_part = model_value[:sep]
        index_part = model_value[sep + 1:]
        try:
            target_index = int(index_part)
        except ValueError:
            target_index = None
        if target_index is not None:
            defaults_list = get_config_raw().get("models", {}).get("defaults")
            if isinstance(defaults_list, list) and 0 <= target_index < len(defaults_list):
                entry = defaults_list[target_index]
                if isinstance(entry, dict):
                    mcc = entry.get("model_client_config")
                    if isinstance(mcc, dict):
                        model_client_config.update({
                            k: v for k, v in mcc.items()
                            if k not in ("model_name",) and v is not None
                        })
                        model_request_config["model"] = resolve_env_vars(str(mcc.get("model_name", model_name_part)))
                    mco = entry.get("model_config_obj")
                    if isinstance(mco, dict):
                        model_request_config.update(mco)

    # 前端字段覆盖（优先级高于 #index 解析）
    if "provider" in model_raw and model_raw["provider"] is not None:
        model_client_config["client_provider"] = model_raw["provider"]
    if "api_base" in model_raw and model_raw["api_base"] is not None:
        model_client_config["api_base"] = model_raw["api_base"]
    if "api_key" in model_raw and model_raw["api_key"] is not None:
        model_client_config["api_key"] = model_raw["api_key"]
    if "model" in model_raw and model_raw["model"] is not None:
        # 若包含 #index，提取纯 model_name
        raw_model = model_raw["model"]
        if isinstance(raw_model, str) and "#" in raw_model:
            model_request_config["model"] = raw_model[:raw_model.rfind("#")]
        else:
            model_request_config["model"] = raw_model

    transformed: dict[str, Any] = {}
    if model_client_config:
        model_client_config.setdefault("timeout", 1800)
        model_client_config.setdefault("verify_ssl", False)
        model_client_config.setdefault("custom_headers", {})
        transformed["model_client_config"] = model_client_config
    if model_request_config:
        transformed["model_request_config"] = model_request_config
    return transformed


def _transform_front_team_agent_spec(agent_key: str, agent_raw: Any) -> dict[str, Any]:
    agent_config = _require_dict(agent_raw, f"agents.{agent_key}")
    transformed: dict[str, Any] = {}

    if "model" in agent_config:
        model_raw = _require_dict(agent_config.get("model"), f"agents.{agent_key}.model")
        transformed_model = _transform_front_team_model_config(model_raw)
        if transformed_model:
            transformed["model"] = transformed_model

    for field_name in ("skills", "workspace", "max_iterations", "completion_timeout"):
        if field_name in agent_config:
            transformed[field_name] = deepcopy(agent_config[field_name])

    return transformed


def _resolve_front_team_agent_spec(
    agents_raw: dict[str, Any],
    agent_key: Any,
    *,
    field_name: str,
) -> dict[str, Any]:
    resolved_key = _require_non_empty_string(agent_key, field_name)
    if resolved_key not in agents_raw:
        raise ValueError(f"{field_name} references unknown agent_key: {resolved_key}")
    return _transform_front_team_agent_spec(resolved_key, agents_raw[resolved_key])


def _build_modes_team_mapping(front_payload: dict[str, Any]) -> dict[str, Any]:
    agents_raw = _require_dict(front_payload.get("agents"), "agents")
    teams_raw = front_payload.get("team")
    if teams_raw is None:
        return {}
    if not isinstance(teams_raw, list):
        raise ValueError("team must be an array")

    team_mapping: dict[str, Any] = {}
    seen_team_names: set[str] = set()

    for team_index, team_item in enumerate(teams_raw):
        team_raw = _require_dict(team_item, f"team[{team_index}]")
        team_name = _require_non_empty_string(team_raw.get("team_name"), f"team[{team_index}].team_name")
        if team_name in seen_team_names:
            raise ValueError(f"duplicate team_name: {team_name}")
        seen_team_names.add(team_name)

        transformed_team: dict[str, Any] = {}
        for key, value in team_raw.items():
            if key in {"leader", "teammate", "predefined_members", EXTERNAL_CLI_PUBLISH_URL_FRONT_KEY}:
                continue
            transformed_team[key] = value
        transformed_team["team_name"] = team_name
        _normalize_external_cli_team_config(
            transformed_team,
            publish_url=team_raw.get(EXTERNAL_CLI_PUBLISH_URL_FRONT_KEY),
            field_name=f"team[{team_index}].external_cli_agents",
        )

        leader_raw = _require_dict(team_raw.get("leader"), f"team[{team_index}].leader")
        transformed_team["leader"] = {
            key: leader_raw[key]
            for key in ("member_name", "display_name", "persona")
            if key in leader_raw
        }
        transformed_team["leader"]["agent_key"] = leader_raw.get("agent_key", "")
        leader_agent_spec = _resolve_front_team_agent_spec(
            agents_raw,
            leader_raw.get("agent_key"),
            field_name=f"team[{team_index}].leader.agent_key",
        )

        teammate_raw = team_raw.get("teammate")
        teammate_agent_spec: dict[str, Any] | None = None
        if teammate_raw is not None:
            teammate_raw = _require_dict(teammate_raw, f"team[{team_index}].teammate")
            teammate_agent_spec = _resolve_front_team_agent_spec(
                agents_raw,
                teammate_raw.get("agent_key"),
                field_name=f"team[{team_index}].teammate.agent_key",
            )
            transformed_team["teammate"] = {"agent_key": teammate_raw.get("agent_key", "")}

        predefined_members_raw = team_raw.get("predefined_members", [])
        if predefined_members_raw is None:
            predefined_members_raw = []
        if not isinstance(predefined_members_raw, list):
            raise ValueError(f"team[{team_index}].predefined_members must be an array")

        transformed_members: list[dict[str, Any]] = []
        transformed_agents: dict[str, Any] = {"leader": leader_agent_spec}
        seen_member_names: set[str] = set()

        for member_index, member_item in enumerate(predefined_members_raw):
            member = _require_dict(
                member_item,
                f"team[{team_index}].predefined_members[{member_index}]",
            )
            member_name = _require_non_empty_string(
                member.get("member_name"),
                f"team[{team_index}].predefined_members[{member_index}].member_name",
            )
            if member_name in seen_member_names:
                raise ValueError(
                    f"duplicate member_name in team[{team_index}]: {member_name}"
                )
            seen_member_names.add(member_name)
            transformed_member = {
                key: member[key]
                for key in ("member_name", "display_name", "role_type", "persona", "prompt_hint", "agent_key")
                if key in member
            }
            transformed_member["member_name"] = member_name
            transformed_members.append(transformed_member)

            member_agent_spec = _resolve_front_team_agent_spec(
                agents_raw,
                member.get("agent_key"),
                field_name=f"team[{team_index}].predefined_members[{member_index}].agent_key",
            )
            transformed_agents[member_name] = member_agent_spec

        if transformed_members:
            transformed_team["predefined_members"] = transformed_members

        if teammate_agent_spec is not None:
            transformed_agents["teammate"] = deepcopy(teammate_agent_spec)

        transformed_team["agents"] = transformed_agents
        team_mapping[team_name] = transformed_team

    return team_mapping


def _normalize_external_cli_team_config(
    transformed_team: dict[str, Any],
    *,
    publish_url: Any,
    field_name: str,
) -> None:
    external_cli_agents = transformed_team.get("external_cli_agents")
    normalized_cli_agents = _normalize_external_cli_agents(external_cli_agents, field_name)
    if normalized_cli_agents:
        transformed_team["external_cli_agents"] = normalized_cli_agents
    else:
        transformed_team.pop("external_cli_agents", None)

    has_codex = any(item["cli_agent"] == "codex" for item in normalized_cli_agents)
    if not has_codex:
        transformed_team.pop("external_transport", None)
        return

    if isinstance(transformed_team.get("external_transport"), dict):
        external_transport = transformed_team["external_transport"]
    else:
        external_transport = {}

    transport_type = str(external_transport.get("type") or "").strip()
    params = external_transport.get("params")
    params = dict(params) if isinstance(params, dict) else {}
    if transport_type and transport_type != "hybrid":
        raise ValueError(f"{field_name} includes codex but external_transport.type must be hybrid")

    if not params.get("external_publish_url"):
        url = str(publish_url or "").strip()
        if not url:
            raise ValueError(f"{field_name} includes codex but external_cli_publish_url is empty")
        params["external_publish_url"] = url

    transformed_team["external_transport"] = {"type": "hybrid", "params": params}


def _normalize_external_cli_agents(value: Any, field_name: str) -> list[dict[str, str]]:
    if value is None or value == "":
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be an array")

    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if isinstance(item, str):
            cli_agent = item.strip()
        elif isinstance(item, dict):
            cli_agent = str(item.get("cli_agent") or "").strip()
        else:
            raise ValueError(f"{field_name}[{index}] must be an object or string")
        if not cli_agent:
            continue
        if cli_agent not in _ALLOWED_EXTERNAL_CLI_AGENTS:
            allowed = ", ".join(sorted(_ALLOWED_EXTERNAL_CLI_AGENTS))
            raise ValueError(f"{field_name}[{index}].cli_agent must be one of: {allowed}")
        if cli_agent in seen:
            continue
        seen.add(cli_agent)
        normalized_item = {"cli_agent": cli_agent}
        if isinstance(item, dict):
            cli_path = str(item.get("cli_path") or "").strip()
            legacy_codex_bin = str(item.get("codex_bin") or "").strip()
            if cli_path:
                normalized_item["cli_path"] = cli_path
            elif cli_agent == "codex" and legacy_codex_bin:
                normalized_item["cli_path"] = legacy_codex_bin
        normalized.append(normalized_item)
    return normalized


def _build_front_agent_registry(front_payload: dict[str, Any]) -> dict[str, Any]:
    agents_raw = _require_dict(front_payload.get("agents"), "agents")
    registry: dict[str, Any] = {}
    for agent_key, agent_raw in agents_raw.items():
        resolved_key = _require_non_empty_string(agent_key, f"agents.{agent_key}")
        registry[resolved_key] = _transform_front_team_agent_spec(resolved_key, agent_raw)
    return registry


def replace_teams_in_config(front_payload: dict[str, Any]) -> None:
    """Replace ``modes.team`` using the frontend team-editor payload.

    Keep legacy top-level ``team`` config intact for backward compatibility.

    If team array is empty, delete ``modes.team`` from config.
    """
    if not isinstance(front_payload, dict):
        raise ValueError("payload must be an object")

    teams_raw = front_payload.get("team")
    data = _load_yaml_round_trip(_CONFIG_YAML_PATH)

    panel_cfg_modified = False
    agent_registry = None
    if "agents" in front_payload:
        agent_registry = _build_front_agent_registry(front_payload)
        panel_cfg = data.get("web_config_panel")
        if not isinstance(panel_cfg, dict):
            panel_cfg = {}
            data["web_config_panel"] = panel_cfg
        if agent_registry:
            panel_cfg["agent_team_agents"] = agent_registry
        else:
            panel_cfg.pop("agent_team_agents", None)
        panel_cfg_modified = True

    # 空数组：删除 modes.team 配置项
    if isinstance(teams_raw, list) and not teams_raw:
        if "modes" in data and isinstance(data["modes"], dict) and "team" in data["modes"]:
            del data["modes"]["team"]
        _dump_yaml_round_trip(_CONFIG_YAML_PATH, data)
        return

    # 非空数组：正常构建并保存
    team_mapping = _build_modes_team_mapping(front_payload)

    data = load_yaml_round_trip(CONFIG_YAML_PATH)
    # Merge web_config_panel back into reloaded data (it was written earlier in this function)
    if panel_cfg_modified:
        if "web_config_panel" not in data or not isinstance(data.get("web_config_panel"), dict):
            data["web_config_panel"] = {}
        if agent_registry:
            data["web_config_panel"]["agent_team_agents"] = agent_registry
        else:
            data.get("web_config_panel", {}).pop("agent_team_agents", None)
    if "modes" not in data or not isinstance(data["modes"], dict):
        data["modes"] = {}
    data["modes"]["team"] = team_mapping
    dump_yaml_round_trip(CONFIG_YAML_PATH, data)


def _ensure_config_object(parent: dict[str, Any], key: str, path: str) -> dict[str, Any]:
    value = parent.get(key)
    if value is None:
        value = {}
        parent[key] = value
    if not isinstance(value, dict):
        raise ValueError(f"{path} config must be an object")
    return value


def update_swarmflow_enabled_in_config(enabled: bool) -> None:
    """Update ``modes.team.jiuwen_team.enable_swarmflow`` in config.yaml."""
    data = load_yaml_round_trip(CONFIG_YAML_PATH)
    current = data
    path_so_far: list[str] = []
    for segment in SWARMFLOW_ENABLED_CONFIG_PATH[:-1]:
        path_so_far.append(segment)
        current = _ensure_config_object(current, segment, ".".join(path_so_far))
    current[SWARMFLOW_ENABLED_CONFIG_PATH[-1]] = bool(enabled)
    dump_yaml_round_trip(CONFIG_YAML_PATH, data)


def update_external_cli_agents_in_config(agents: list[str | dict[str, Any]], publish_url: str | None = None) -> None:
    """Update the external CLI agent switches for the default team."""
    data = load_yaml_round_trip(CONFIG_YAML_PATH)
    current = data
    path_so_far: list[str] = []
    for segment in EXTERNAL_CLI_AGENTS_CONFIG_PATH[:-1]:
        path_so_far.append(segment)
        current = _ensure_config_object(current, segment, ".".join(path_so_far))

    normalized_agents = _normalize_external_cli_agents(agents, "external_cli_agents")
    if normalized_agents:
        current[EXTERNAL_CLI_AGENTS_CONFIG_PATH[-1]] = normalized_agents
    else:
        current.pop(EXTERNAL_CLI_AGENTS_CONFIG_PATH[-1], None)

    if any(item["cli_agent"] == "codex" for item in normalized_agents):
        external_transport = current.get(EXTERNAL_TRANSPORT_CONFIG_PATH[-1])
        if isinstance(external_transport, dict):
            transport_type = str(external_transport.get("type") or "").strip()
            params = external_transport.get("params")
            params = dict(params) if isinstance(params, dict) else {}
        else:
            transport_type = ""
            params = {}
        if transport_type and transport_type != "hybrid":
            raise ValueError("external_cli_agents includes codex but external_transport.type must be hybrid")
        if not params.get("external_publish_url"):
            url = str(publish_url or "").strip()
            if not url:
                raise ValueError("external_cli_agents includes codex but external_cli_publish_url is empty")
            params["external_publish_url"] = url
        current[EXTERNAL_TRANSPORT_CONFIG_PATH[-1]] = {"type": "hybrid", "params": params}
    else:
        current.pop(EXTERNAL_TRANSPORT_CONFIG_PATH[-1], None)

    dump_yaml_round_trip(CONFIG_YAML_PATH, data)


def update_swarmflow_budget_in_config(budget: str) -> None:
    """Update ``modes.team.jiuwen_team.swarmflow_budget`` in config.yaml.

    Pass an empty string or ``"none"`` to remove the budget ceiling.
    """
    clear_budget = not budget or budget.strip().lower() in ("none", "null")
    if clear_budget:
        data = load_yaml_round_trip(CONFIG_YAML_PATH)
        current = data
        for segment in SWARMFLOW_BUDGET_CONFIG_PATH[:-1]:
            if segment not in current or not isinstance(current, dict):
                return  # path doesn't exist, nothing to clear
            current = current[segment]
        current.pop(SWARMFLOW_BUDGET_CONFIG_PATH[-1], None)
        dump_yaml_round_trip(CONFIG_YAML_PATH, data)
        return

    try:
        value = int(budget)
    except (ValueError, TypeError):
        raise ValueError(f"swarmflow_budget must be a positive integer, got {budget!r}") from None
    if value <= 0:
        raise ValueError(f"swarmflow_budget must be a positive integer, got {value}")
    data = load_yaml_round_trip(CONFIG_YAML_PATH)
    current = data
    for segment in SWARMFLOW_BUDGET_CONFIG_PATH[:-1]:
        current = _ensure_config_object(current, segment, ".".join(SWARMFLOW_BUDGET_CONFIG_PATH))
    current[SWARMFLOW_BUDGET_CONFIG_PATH[-1]] = value
    dump_yaml_round_trip(CONFIG_YAML_PATH, data)


def get_mcp_servers() -> list[dict[str, Any]]:
    """合并 config.yaml mcp.servers（command.mcp/TUI 手填）+ mcp/state.json（连接器）。

    两套事实源永久并存（非迁移期重叠）：
    - config.yaml mcp.servers 是 command.mcp（TUI channel）手动管理 MCP 的
      事实源；add/remove/enable/disable 经 agent_ws_server 的 command.mcp
      handler 直接读写 config.yaml。这条链路不动。
    - mcp/state.json 是MCP（marketplace + custom）的事实源；
      connect/disconnect/enable/disable 经 mcp handler 写 state.json。
    """
    # A. config.yaml — 手填 MCP（raw, no env resolve）.
    data = get_config_raw()
    mcp_cfg = data.get("mcp", {})
    if not isinstance(mcp_cfg, dict):
        servers_yaml: list[dict[str, Any]] = []
    else:
        servers_yaml = [item for item in mcp_cfg.get("servers", [])
                        if isinstance(item, dict)]

    # B. mcp/state.json — 已连接 MCP（state==connected）.
    state_mcps: list[dict[str, Any]] = []
    try:
        from jiuwenswarm.server.runtime.mcp.state_store import (
            list_connected_mcps,
            record_to_mcp_entry,
        )
        for rec in list_connected_mcps():
            name = rec.get("name", "")
            if not name:
                continue
            entry = record_to_mcp_entry(name, rec)
            # skill-only MCPs return None
            if entry is not None:
                state_mcps.append(entry)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[config] failed to merge mcp state.json: %s", exc)

    # Merge: state.json wins on name conflict (dedup by name, state first).
    state_names = {s["name"] for s in state_mcps}
    merged = [s for s in servers_yaml if s.get("name") not in state_names]
    merged.extend(state_mcps)
    return merged


def get_config_yaml_mcp_servers() -> list[dict[str, Any]]:
    """只读 config.yaml 的 mcp.servers（TUI/command.mcp 手配）。"""
    data = get_config_raw()
    mcp_cfg = data.get("mcp", {})
    if not isinstance(mcp_cfg, dict):
        return []
    return [item for item in mcp_cfg.get("servers", []) if isinstance(item, dict)]


def upsert_mcp_server_in_config(server: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """新增或更新 mcp.servers 条目，返回（条目, 是否创建）。"""
    name = str(server.get("name", "")).strip()
    if not name:
        raise ValueError("MCP server name is required")
    data = load_yaml_round_trip(CONFIG_YAML_PATH)
    if "mcp" not in data or not isinstance(data["mcp"], dict):
        data["mcp"] = {}
    mcp_cfg = data["mcp"]
    servers = mcp_cfg.get("servers")
    if not isinstance(servers, list):
        servers = []
        mcp_cfg["servers"] = servers

    created = True
    for idx, item in enumerate(servers):
        if not isinstance(item, dict):
            continue
        if str(item.get("name", "")).strip() != name:
            continue
        servers[idx] = server
        created = False
        break
    else:
        servers.append(server)
    dump_yaml_round_trip(CONFIG_YAML_PATH, data)
    return server, created


def set_mcp_server_enabled_in_config(name: str, enabled: bool) -> dict[str, Any]:
    """切换 mcp.servers 指定 name 的 enabled 状态并返回更新后的条目。"""
    target = str(name or "").strip()
    if not target:
        raise ValueError("MCP server name is required")
    data = load_yaml_round_trip(CONFIG_YAML_PATH)
    if "mcp" not in data or not isinstance(data["mcp"], dict):
        raise KeyError(f"MCP server '{target}' not found")
    servers = data["mcp"].get("servers", [])
    if not isinstance(servers, list):
        raise KeyError(f"MCP server '{target}' not found")
    for item in servers:
        if not isinstance(item, dict):
            continue
        if str(item.get("name", "")).strip() != target:
            continue
        item["enabled"] = bool(enabled)
        dump_yaml_round_trip(CONFIG_YAML_PATH, data)
        return dict(item)
    raise KeyError(f"MCP server '{target}' not found")


def get_mcp_server_config(name: str) -> dict[str, Any] | None:
    """按名称读取单个 mcp server 配置（原始结构）。"""
    target = str(name or "").strip()
    if not target:
        return None
    for item in get_mcp_servers():
        if str(item.get("name", "")).strip() == target:
            return item
    return None


def remove_mcp_server_in_config(name: str) -> dict[str, Any]:
    """删除指定 mcp server 配置并返回被删除的条目。"""
    target = str(name or "").strip()
    if not target:
        raise ValueError("MCP server name is required")
    data = load_yaml_round_trip(CONFIG_YAML_PATH)
    mcp_cfg = data.get("mcp")
    if not isinstance(mcp_cfg, dict):
        raise KeyError(f"MCP server '{target}' not found")
    servers = mcp_cfg.get("servers", [])
    if not isinstance(servers, list):
        raise KeyError(f"MCP server '{target}' not found")
    for idx, item in enumerate(servers):
        if not isinstance(item, dict):
            continue
        if str(item.get("name", "")).strip() != target:
            continue
        removed = dict(item)
        del servers[idx]
        dump_yaml_round_trip(CONFIG_YAML_PATH, data)
        return removed
    raise KeyError(f"MCP server '{target}' not found")


def _mcp_name_in_state(name: str) -> bool:
    """Whether state.json has a record for ``name`` (any state)."""
    try:
        from jiuwenswarm.server.runtime.mcp.state_store import get_mcp_record
        return get_mcp_record(name) is not None
    except Exception:  # noqa: BLE001
        return False


def _mcp_name_in_config_yaml(name: str) -> bool:
    """Whether config.yaml mcp.servers has an entry for ``name``."""
    return any(str(s.get("name", "")).strip() == name
               for s in get_config_yaml_mcp_servers())


def upsert_mcp_server(server: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Upsert a TUI-managed MCP, routing by source: update in place if the
    name already lives in config.yaml (legacy stock) or state.json (TUI-
    created / web-connected); otherwise create new in state.json.

    TUI ``add``/``update`` lands here. New MCPs always go to state.json
    (``enabled`` defaults to the payload's ``enabled``, True for TUI add) —
    config.yaml is no longer a creation target, only legacy stock edited
    in place. Returns ``(entry, created)``. Raises ``ValueError`` if no name.
    """
    name = str(server.get("name", "")).strip()
    if not name:
        raise ValueError("MCP server name is required")
    # Legacy stock in config.yaml: update in place (keeps its source).
    if _mcp_name_in_config_yaml(name):
        return upsert_mcp_server_in_config(server)
    # Else state.json is the creation/update home for TUI MCPs.
    from jiuwenswarm.server.runtime.mcp.state_store import upsert_mcp_record
    prior = _mcp_name_in_state(name)
    enabled = bool(server.get("enabled", True)) if "enabled" in server else None
    rec = upsert_mcp_record(name, server, state="connected",
                            integration_type=_integration_type_for(server),
                            enabled=enabled)
    # Shape a config-like entry for the response (carries enabled through).
    entry = dict(rec)
    entry["name"] = name
    if "enabled" not in entry:
        entry["enabled"] = bool(server.get("enabled", True))
    return entry, (not prior)


def set_mcp_server_enabled(name: str, enabled: bool) -> dict[str, Any]:
    """Flip a TUI-managed MCP's ``enabled`` flag, routing by source: state.json
    first (TUI-created / web-connected), then config.yaml (legacy stock).

    Only the TUI channel reads ``enabled``; web ignores it. Raises
    ``KeyError`` if the name is in neither source.
    """
    target = str(name or "").strip()
    if not target:
        raise ValueError("MCP server name is required")
    if _mcp_name_in_state(target):
        from jiuwenswarm.server.runtime.mcp.state_store import (
            get_mcp_record, set_mcp_enabled,
        )
        set_mcp_enabled(target, enabled=enabled)
        rec = get_mcp_record(target) or {}
        entry = dict(rec)
        entry["name"] = target
        return entry
    # Legacy stock in config.yaml.
    return set_mcp_server_enabled_in_config(target, enabled)


def remove_mcp_server(name: str) -> dict[str, Any]:
    """Remove a TUI-managed MCP, routing by source: state.json first, then
    config.yaml (legacy stock). Returns the removed entry. Raises
    ``KeyError`` if the name is in neither source.
    """
    target = str(name or "").strip()
    if not target:
        raise ValueError("MCP server name is required")
    if _mcp_name_in_state(target):
        from jiuwenswarm.server.runtime.mcp.state_store import remove_mcp_record
        removed = remove_mcp_record(target) or {}
        entry = dict(removed)
        entry["name"] = target
        return entry
    # Legacy stock in config.yaml.
    return remove_mcp_server_in_config(target)


def _integration_type_for(server: dict[str, Any]) -> str:
    """Derive the state.json integration_type from a TUI payload's transport."""
    t = str(server.get("transport", "")).strip().lower()
    if t == "stdio":
        return "stdio-mcp"
    if t in {"sse", "http", "streamable-http", "streamable_http"}:
        return "remote-mcp"
    return "remote-mcp"


# ---------------------------------------------------------------------------
# react.subagents 段操作
# ---------------------------------------------------------------------------


def upsert_subagent_in_config(name: str, enabled: bool = True) -> None:
    """在 react.subagents.<name> 中添加或更新 agent 启用状态。

    自动创建不存在的 react / subagents 段。
    保留已有的其他 subagent 配置键（如 max_iterations 等）。
    """
    target = str(name or "").strip()
    if not target:
        raise ValueError("subagent name is required")
    data = load_yaml_round_trip(CONFIG_YAML_PATH)
    if "react" not in data or not isinstance(data["react"], dict):
        data["react"] = {}
    react = data["react"]
    if "subagents" not in react or not isinstance(react["subagents"], dict):
        react["subagents"] = {}
    subagents = react["subagents"]
    if target not in subagents or not isinstance(subagents[target], dict):
        subagents[target] = {}
    subagents[target]["enabled"] = bool(enabled)
    dump_yaml_round_trip(CONFIG_YAML_PATH, data)


def remove_subagent_from_config(name: str) -> bool:
    """从 react.subagents.<name> 中删除 agent 条目。

    Returns:
        True: 找到并删除
        False: 条目不存在
    """
    target = str(name or "").strip()
    if not target:
        raise ValueError("subagent name is required")
    data = load_yaml_round_trip(CONFIG_YAML_PATH)
    react = data.get("react")
    if not isinstance(react, dict):
        return False
    subagents = react.get("subagents")
    if not isinstance(subagents, dict):
        return False
    if target not in subagents:
        return False
    del subagents[target]
    dump_yaml_round_trip(CONFIG_YAML_PATH, data)
    return True


def update_memory_forbidden_enabled_in_config(value: bool) -> None:
    """更新 memory.forbidden_memory_definition.enabled（记忆系统敏感信息过滤开关）并写回。"""
    data = load_yaml_round_trip(CONFIG_YAML_PATH)
    if "memory" not in data:
        data["memory"] = {}
    if "forbidden_memory_definition" not in data["memory"]:
        data["memory"]["forbidden_memory_definition"] = {}
    data["memory"]["forbidden_memory_definition"]["enabled"] = value
    dump_yaml_round_trip(CONFIG_YAML_PATH, data)


def update_memory_forbidden_description_in_config(description: dict[str, str]) -> None:
    """更新 memory.forbidden_memory_definition.description（禁止记忆内容描述）并写回。"""
    data = load_yaml_round_trip(CONFIG_YAML_PATH)
    if "memory" not in data:
        data["memory"] = {}
    if "forbidden_memory_definition" not in data["memory"]:
        data["memory"]["forbidden_memory_definition"] = {}
    if "description" not in data["memory"]["forbidden_memory_definition"]:
        data["memory"]["forbidden_memory_definition"]["description"] = {}
    # 合并描述，保留其他语言的描述
    current_desc = data["memory"]["forbidden_memory_definition"]["description"] or {}
    if isinstance(current_desc, dict):
        data["memory"]["forbidden_memory_definition"]["description"] = {**current_desc, **description}
    else:
        data["memory"]["forbidden_memory_definition"]["description"] = description
    dump_yaml_round_trip(CONFIG_YAML_PATH, data)


def update_memory_forbidden_in_config(updates: dict[str, Any]) -> None:
    """更新 memory.forbidden_memory_definition 并写回。"""
    data = load_yaml_round_trip(CONFIG_YAML_PATH)
    if "memory" not in data:
        data["memory"] = {}
    if "forbidden_memory_definition" not in data["memory"]:
        data["memory"]["forbidden_memory_definition"] = {}
    section = data["memory"]["forbidden_memory_definition"]
    for k, v in updates.items():
        if k == "description" and isinstance(v, dict) and isinstance(section.get("description"), dict):
            section["description"] = {**section["description"], **v}
        else:
            section[k] = v
    dump_yaml_round_trip(CONFIG_YAML_PATH, data)


def update_a2ui_in_config(updates: dict[str, Any]) -> None:
    """更新 a2ui 配置段并写回 config.yaml。"""
    data = _load_yaml_round_trip(_CONFIG_YAML_PATH)
    if "a2ui" not in data:
        data["a2ui"] = {}
    section = data["a2ui"]
    for key, value in updates.items():
        section[key] = value
    _dump_yaml_round_trip(_CONFIG_YAML_PATH, data)


def _deep_merge(
    template: dict[str, Any],
    user: dict[str, Any],
    depth: int = 0,
) -> dict[str, Any]:
    """Recursively merge template with user config, cleaning deprecated fields.

    Rules:
    - Add: fields only in template (new config options)
    - Keep: user values for fields that exist in template (preserve user settings)
    - Remove: fields only in user (deprecated config, cleanup)
    - Max recursion depth: 4 (covers deep nested config like context_engine_config)

    Args:
        template: Template config dict with default values
        user: User config dict
        depth: Current recursion depth

    Returns:
        Merged dict synced with template structure, preserving user values.
    """
    if depth >= 4:
        return user

    result: dict[str, Any] = {}

    for key, template_value in template.items():
        if key not in user:
            result[key] = template_value
        elif isinstance(template_value, dict) and isinstance(user.get(key), dict):
            result[key] = _deep_merge(template_value, user[key], depth + 1)
        else:
            result[key] = user[key]

    return result



_LEGACY_AGENT_SUBMODE_KEYS: tuple[str, ...] = ("plan", "fast", "agent.plan", "agent.fast")


def _migrate_legacy_agent_submode_memory(user_data: dict[str, Any]) -> None:
    modes = user_data.get("modes")
    if not isinstance(modes, dict):
        return
    agent_node = modes.get("agent")
    if not isinstance(agent_node, dict):
        return

    legacy_enabled_values: list[bool] = []
    for key in _LEGACY_AGENT_SUBMODE_KEYS:
        sub_node = agent_node.get(key)
        if isinstance(sub_node, dict):
            sub_memory = sub_node.get("memory")
            if isinstance(sub_memory, dict) and "enabled" in sub_memory:
                legacy_enabled_values.append(bool(sub_memory["enabled"]))

    if not legacy_enabled_values:
        return

    flat_memory = agent_node.get("memory")
    if not isinstance(flat_memory, dict):
        flat_memory = {}
        agent_node["memory"] = flat_memory

    if "enabled" not in flat_memory:
        flat_memory["enabled"] = all(legacy_enabled_values)


def migrate_config_from_template(
    template_path: Path,
    user_config_path: Path,
) -> bool:
    """Sync user config with template structure, preserving user values.

    Three-way merge:
    - Add: new fields from template (new config options)
    - Keep: user values for fields that exist in template
    - Remove: deprecated fields not in template (cleanup)

    This preserves user settings like:
    - models.*.model_config_obj.temperature
    - react.context_engine_config.enabled
    - react.context_engine_config.message_summary_offloader_config.*

    Args:
        template_path: Path to template config.yaml
        user_config_path: Path to user config.yaml

    Returns:
        True if migration was performed, False otherwise.
    """
    if not user_config_path.exists():
        return False

    if not template_path.exists():
        return False

    template_data = load_yaml_round_trip(template_path)
    user_data = load_yaml_round_trip(user_config_path)

    if not isinstance(template_data, dict):
        return False

    if user_data is None:
        user_data = {}

    # 结构性迁移：plan/fast 子模式 memory 配置 -> 合并后的 modes.agent.memory
    # 必须在 _deep_merge 之前执行，否则旧子节点会被静默丢弃而非迁移。
    _migrate_legacy_agent_submode_memory(user_data)

    # Deep merge: template provides defaults, user values preserved
    merged_data = _deep_merge(template_data, user_data)

    # Guard against empty merged_data overwriting valid user config
    if merged_data is None or not merged_data:
        return False

    # Only write if there are actual changes
    if merged_data != user_data:
        dump_yaml_round_trip(user_config_path, merged_data)
        return True

    return False


# ---------- 模型配置管理 ----------
def get_model_names() -> list[str]:
    """获取可切换的模型名称列表。优先从 models.defaults 列表读取。

    与web端一致：允许同名 model_name 多次出现（不同 api_key/api_base 即为不同配置）。
    """
    data = get_config_raw()
    models = data.get("models", {})
    defaults_list = models.get("defaults")
    if isinstance(defaults_list, list) and defaults_list:
        names: list[str] = []
        name_count: dict[str, int] = {}
        for entry in defaults_list:
            if not isinstance(entry, dict):
                continue
            model_name = (entry.get("model_client_config") or {}).get("model_name", "")
            alias = entry.get("alias", "")
            resolved_alias = resolve_env_vars(str(alias)) if alias else ""
            resolved_name = resolve_env_vars(str(model_name)) if model_name else ""
            display = resolved_alias or resolved_name
            if display:
                count = name_count.get(display, 0) + 1
                name_count[display] = count
                names.append(display)
        return names
    skip = {"default", "defaults"}
    return [k for k, v in models.items() if isinstance(v, dict) and k not in skip]


def add_or_update_model_in_config(name: str, model_config: dict[str, Any]) -> None:
    """新增或更新一个模型配置，写入 config.yaml 的 models.<name> 节点。"""
    data = load_yaml_round_trip(CONFIG_YAML_PATH)
    if "models" not in data:
        data["models"] = {}
    if name not in data["models"]:
        data["models"][name] = model_config
    else:
        existing = data["models"][name]
        for k, v in model_config.items():
            if v is None and k in existing:
                del existing[k]
            else:
                existing[k] = v
    dump_yaml_round_trip(CONFIG_YAML_PATH, data)


def get_model_config(name: str, index: int | None = None) -> dict[str, Any] | None:
    """获取指定模型的原始配置（不解析环境变量）。

    优先从 models.defaults 列表中按 model_name 查找。
    当存在同名模型时，可通过 index 参数指定第几个匹配项（0-based）。
    若 index 为 None，返回 is_default=True 的条目；若无则返回第一个匹配。
    """
    data = get_config_raw()
    models = data.get("models", {})
    defaults_list = models.get("defaults")
    if isinstance(defaults_list, list):
        matches: list[tuple[int, dict]] = []
        for i, entry in enumerate(defaults_list):
            if not isinstance(entry, dict):
                continue
            entry_name = (entry.get("model_client_config") or {}).get("model_name", "")
            if resolve_env_vars(str(entry_name)) == name:
                matches.append((i, entry))
        if not matches:
            # 按 alias 查找（alias 全局唯一，index 无意义）
            for entry in defaults_list:
                if not isinstance(entry, dict):
                    continue
                alias = entry.get("alias", "")
                if alias and resolve_env_vars(str(alias)) == name:
                    return entry
            return models.get(name) if name in models else None
        if index is not None:
            for pos, entry in matches:
                if pos == index:
                    return entry
            return None
        for _, entry in matches:
            if entry.get("is_default") is True:
                return entry
        return matches[0][1]
    return models.get(name) if name in models else None


# =====================================================================
# Sandbox runtime config
#
# 字段挂在 ``sandbox`` 顶层 (与 ``url`` / ``type`` / ``startup_mode`` /
# ``policy_file`` / ``preserve_file_sharing_mode`` 并列):
#
#   sandbox:
#     url: ...
#     enabled: true
#     excluded_commands: [...]
#     files: { allow: [...], deny: [...] }
#     idle_ttl_seconds: 600         # 可选, 默认 None = 不进行 idle 驱逐
#     idle_check_interval: 60       # 可选, 默认 None = 让 jiuwenbox 端用自身默认值
#     fallback_on_failure: false    # jiuwenbox exec 异常时回退本地 (见 agent-core jiuwenbox provider)
#
# ``get_sandbox_runtime`` 把这些 key 读出来填默认值;
# ``update_sandbox_runtime`` 写回时也只动这几个 key, 不动 endpoint 字段。
#
# ``idle_ttl_seconds`` / ``idle_check_interval`` 透传给
# ``create_sandbox_sysop_card`` 作为同名参数, 最终在 jiuwenbox provider 里通过
# ``PUT /api/v1/timeout`` 写到 jiuwenbox server 根 policy 上 (per-sandbox policy
# 的 ``timeout`` 子段不驱动 reaper, 必须改根 policy 才能让空闲淘汰生效)。
# =====================================================================

_SANDBOX_RUNTIME_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "excluded_commands": [],
    "files": {"allow": [], "deny": []},
    "idle_ttl_seconds": None,
    "idle_check_interval": None,
    "fallback_on_failure": False,
}

# 受 ``get_sandbox_runtime`` / ``update_sandbox_runtime`` 管辖的 sandbox 字段。
_SANDBOX_RUNTIME_KEYS: tuple[str, ...] = tuple(_SANDBOX_RUNTIME_DEFAULTS.keys())


def _coerce_optional_positive_int(
    value: Any, *, field: str, allow_zero: bool = False
) -> Optional[int]:
    """把 yaml/json 来的 idle 配置值归一化为 ``Optional[int]``.

    - ``None`` / 缺失 / 空字符串 → ``None``。
    - ``int`` / 数字字符串 → ``int(value)``; ``allow_zero=False`` 时 ``<= 0``
      也归一化为 ``None`` (避免后续负值流到 jiuwenbox 端引发歧义)。
    - 其它 (``bool`` / 不可解析字符串等) → ``ValueError``。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        # bool 是 int 子类, 必须先排掉; ``True`` -> 1 这种隐式转换在配置文件里
        # 几乎肯定是误写, 不要静默放行。
        raise ValueError(f"{field} must be a number, not a boolean")
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            number = int(text)
        except ValueError:
            try:
                number = int(float(text))
            except ValueError as exc:
                raise ValueError(
                    f"{field} must parse as an integer of seconds, got {value!r}"
                ) from exc
    elif isinstance(value, (int, float)):
        number = int(value)
    else:
        raise ValueError(
            f"{field} must be number or string, got {type(value).__name__}"
        )
    if not allow_zero and number <= 0:
        return None
    return number


def _ensure_sandbox_runtime_shape(runtime: Any) -> dict[str, Any]:
    """填充 sandbox runtime 缺省字段，返回归一化后的 dict（不写盘）。"""
    base = {k: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v)
            for k, v in _SANDBOX_RUNTIME_DEFAULTS.items()}
    if not isinstance(runtime, dict):
        return base
    out = dict(base)
    if "enabled" in runtime:
        out["enabled"] = bool(runtime["enabled"])
    if "fallback_on_failure" in runtime:
        out["fallback_on_failure"] = bool(runtime["fallback_on_failure"])
    raw_excluded = runtime.get("excluded_commands")
    if isinstance(raw_excluded, list):
        out["excluded_commands"] = [
            str(p) for p in raw_excluded if str(p).strip()
        ]
    files = runtime.get("files")
    if isinstance(files, dict):
        allow = files.get("allow")
        deny = files.get("deny")
        out["files"] = {
            "allow": list(allow) if isinstance(allow, list) else [],
            "deny": list(deny) if isinstance(deny, list) else [],
        }
    if "idle_ttl_seconds" in runtime:
        # ``<= 0`` 归一化成 ``None`` (= 禁用淘汰), 与 jiuwenbox server 端
        # ``TimeoutPolicy.idle_timeout`` 的语义对齐。
        out["idle_ttl_seconds"] = _coerce_optional_positive_int(
            runtime["idle_ttl_seconds"], field="sandbox.idle_ttl_seconds",
        )
    if "idle_check_interval" in runtime:
        out["idle_check_interval"] = _coerce_optional_positive_int(
            runtime["idle_check_interval"], field="sandbox.idle_check_interval",
        )
    return out


def get_sandbox_runtime() -> dict[str, Any]:
    """返回 sandbox runtime 当前内容 (含缺省字段填充)。

    直接从 ``sandbox.<key>`` 扁平字段读; 字段缺失时用 ``_SANDBOX_RUNTIME_DEFAULTS``。
    """
    cfg = get_config() or {}
    sandbox = cfg.get("sandbox")
    if not isinstance(sandbox, dict):
        return _ensure_sandbox_runtime_shape(None)
    raw = {key: sandbox[key] for key in _SANDBOX_RUNTIME_KEYS if key in sandbox}
    return _ensure_sandbox_runtime_shape(raw)


# ``preserve_file_sharing_mode`` 当前合法取值集合 = ``{"mount"}``; 空值表示
# "未配置, 按默认走"。
_VALID_PRESERVE_FILE_SHARING_MODES = ("mount",)
_DEFAULT_PRESERVE_FILE_SHARING_MODE = "mount"


def _normalize_preserve_file_sharing_mode(value: Any) -> str | None:
    """归一化 ``sandbox.preserve_file_sharing_mode``.

    - ``None`` / 空字符串 → 返回 ``None`` (表示 "未配置", 调用方按默认值处理).
    - ``"mount"`` (大小写不敏感, 前后空格) → 返回 ``"mount"``.
    - 其它任何取值 → 抛 ``ValueError``, 让调用方拒绝该 yaml / API 输入。
    """
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    if text not in _VALID_PRESERVE_FILE_SHARING_MODES:
        raise ValueError(
            f"sandbox.preserve_file_sharing_mode must be one of "
            f"{_VALID_PRESERVE_FILE_SHARING_MODES}, got {value!r}"
        )
    return text


_VALID_SANDBOX_STARTUP_MODES = ("internal", "external")
_DEFAULT_SANDBOX_STARTUP_MODE = "internal"
_DEFAULT_SANDBOX_POLICY_FILE = "code-agent-policy.yaml"

# YuanRong sandbox knobs under flat ``sandbox:`` (see get_sandbox_endpoint).
_VALID_YUANRONG_EXECUTORS = ("default", "docker")
_DEFAULT_YUANRONG_EXECUTOR = "docker"
_DEFAULT_YUANRONG_URL = "http://yuanrong.local"
_YUANRONG_ENDPOINT_OPTIONAL_KEYS: tuple[str, ...] = (
    "image",
    "workdir",
    "mounts",
    "cpu",
    "cpu_limit",
    "memory",
    "mem_limit",
    "rootfs",
)

# Public re-exports for callers that need to fall back to / advertise defaults
# (e.g. agent_ws_server 把缺省值持久化到 config.yaml, 让重启后 get_sandbox_endpoint
# 能直接读到, 而不必每次再走一遍 fallback 逻辑)。
DEFAULT_SANDBOX_STARTUP_MODE = _DEFAULT_SANDBOX_STARTUP_MODE
DEFAULT_SANDBOX_POLICY_FILE = _DEFAULT_SANDBOX_POLICY_FILE
DEFAULT_YUANRONG_SANDBOX_URL = _DEFAULT_YUANRONG_URL
DEFAULT_YUANRONG_EXECUTOR = _DEFAULT_YUANRONG_EXECUTOR


def _normalize_yuanrong_executor(value: Any) -> str:
    """归一化 ``sandbox.executor``; 非法或空值回落到默认 ``docker``."""
    text = str(value or "").strip().lower()
    if text not in _VALID_YUANRONG_EXECUTORS:
        return _DEFAULT_YUANRONG_EXECUTOR
    return text


def _normalize_sandbox_startup_mode(value: Any) -> str:
    """归一化 ``sandbox.startup_mode``; 非法或空值回落到默认 ``internal``."""
    text = str(value or "").strip().lower()
    if text not in _VALID_SANDBOX_STARTUP_MODES:
        return _DEFAULT_SANDBOX_STARTUP_MODE
    return text


def get_sandbox_startup_mode() -> str:
    """返回 ``sandbox.startup_mode``: ``internal`` (agent-server 拉起 jiuwenbox)
    或 ``external`` (用户自己启动 jiuwenbox)。

    未配置时默认 ``internal`` (当前 code-agent 模式预期行为)。
    """
    cfg = get_config() or {}
    sandbox = cfg.get("sandbox") or {}
    return _normalize_sandbox_startup_mode(sandbox.get("startup_mode"))


def get_sandbox_startup_mode_explicit() -> str | None:
    """同 :func:`get_sandbox_startup_mode`, 但仅返回 ``config.yaml`` 里**显式**
    写出的合法值 (``internal`` / ``external``); 未配置 / 空串 / 非法值都返回
    ``None``。

    用途: agent-server boot 时判断要不要自动拉起 jiuwenbox 子进程。
    ``get_sandbox_startup_mode`` 默认归一会把缺失字段变成 ``internal``, 那样
    会让从来没碰过沙箱的用户在升级到带 bootstrap 逻辑的版本后, 突然多出一个
    jiuwenbox 进程。靠这个 ``_explicit`` 变体可以严格区分 "用户主动选了 internal"
    和 "字段不存在, 走的默认"。
    """
    cfg = get_config() or {}
    sandbox = cfg.get("sandbox") or {}
    raw = sandbox.get("startup_mode")
    if not isinstance(raw, str):
        return None
    text = raw.strip().lower()
    if text not in _VALID_SANDBOX_STARTUP_MODES:
        return None
    return text


def update_sandbox_startup_mode(mode: str) -> str:
    """写入 ``sandbox.startup_mode`` 到 config.yaml; 返回归一化后的值。"""
    normalized = _normalize_sandbox_startup_mode(mode)
    if str(mode or "").strip().lower() not in _VALID_SANDBOX_STARTUP_MODES:
        raise ValueError(
            f"startup_mode must be one of {_VALID_SANDBOX_STARTUP_MODES}, got {mode!r}",
        )
    data = _load_yaml_round_trip(_CONFIG_YAML_PATH)
    if "sandbox" not in data or not isinstance(data.get("sandbox"), dict):
        data["sandbox"] = {}
    data["sandbox"]["startup_mode"] = normalized
    _dump_yaml_round_trip(_CONFIG_YAML_PATH, data)
    return normalized


def _looks_like_bare_filename(value: str) -> bool:
    """``True`` 表示参数应该被解释为 ``jiuwenbox/configs/`` 下的文件名。

    判据: 不含任何路径分隔符 (``/`` 或 ``os.sep``); 含分隔符或绝对路径一律按整路径处理。
    """
    if not value:
        return False
    return "/" not in value and "\\" not in value and not Path(value).is_absolute()


def _jiuwenbox_configs_dir() -> Path | None:
    """探测仓库或安装位置上的 ``jiuwenbox/configs/`` 目录。

    优先在仓库目录树里寻找 (开发场景, ``jiuwenbox/src/jiuwenbox/configs``);
    失败再尝试已 ``pip install`` 的 jiuwenbox 包内 ``configs/``。
    """
    here = Path(__file__).resolve()

    for ancestor in here.parents[1:7]:
        for candidate in (
            ancestor / "jiuwenbox" / "src" / "jiuwenbox" / "configs",
            ancestor / "jiuwenbox" / "configs",
        ):
            if candidate.is_dir():
                return candidate
    try:
        import jiuwenbox  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        pkg_dir = Path(jiuwenbox.__file__).resolve().parent
    except Exception:  # noqa: BLE001
        return None
    # wheel 安装版: policy 模板随包打入 ``<site-packages>/jiuwenbox/configs/``。
    direct = pkg_dir / "configs"
    if direct.is_dir():
        return direct

    for steps_up in (2, 3):
        candidate = pkg_dir
        for _ in range(steps_up):
            candidate = candidate.parent
        candidate = candidate / "configs"
        if candidate.is_dir():
            return candidate
    return None


def resolve_sandbox_policy_path(value: str | None) -> Path | None:
    r"""把 ``sandbox.policy_file`` 的取值解析为宿主机绝对路径。

    - ``None`` / 空字符串 → 返回 None, 由调用方决定是否落到 jiuwenbox 自身默认 policy;
    - 仅文件名 (不含路径分隔符) → 在 ``jiuwenbox/configs/`` 下拼接;
      ``configs/`` 探测不到时返回 None (调用方应当报错并提示给绝对路径);
    - 含 ``/`` ``\\`` 或绝对路径 → 展开 ``~`` / ``$VAR`` 后按整路径返回 (不强校验是否存在,
      留给上游 ``_load_policy_file`` / jiuwenbox 自己做存在性检查并给出明确错误)。
    """
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    expanded = os.path.expandvars(os.path.expanduser(text))
    if _looks_like_bare_filename(expanded):
        configs_dir = _jiuwenbox_configs_dir()
        if configs_dir is None:
            return None
        return (configs_dir / expanded).resolve()
    return Path(expanded).resolve()


def get_sandbox_policy_file() -> str:
    """返回 ``sandbox.policy_file`` 原始字符串 (空表示未配置, 由调用方走默认)。"""
    cfg = get_config() or {}
    sandbox = cfg.get("sandbox") or {}
    return str(sandbox.get("policy_file") or "").strip()


def get_sandbox_policy_path() -> Path | None:
    """返回 ``sandbox.policy_file`` 解析后的绝对路径。

    未配置时回落到 ``_DEFAULT_SANDBOX_POLICY_FILE`` (即 ``code-agent-policy.yaml``);
    解析失败 (例如仅给文件名但 ``configs/`` 不可达) 返回 None。
    """
    raw = get_sandbox_policy_file() or _DEFAULT_SANDBOX_POLICY_FILE
    return resolve_sandbox_policy_path(raw)


def update_sandbox_policy_file(value: str) -> str:
    """写入 ``sandbox.policy_file`` (仅文件名或绝对路径) 到 config.yaml; 返回归一化后的字符串。"""
    text = str(value or "").strip()
    if not text:
        raise ValueError("policy_file must be non-empty")
    data = _load_yaml_round_trip(_CONFIG_YAML_PATH)
    if "sandbox" not in data or not isinstance(data.get("sandbox"), dict):
        data["sandbox"] = {}
    data["sandbox"]["policy_file"] = text
    _dump_yaml_round_trip(_CONFIG_YAML_PATH, data)
    return text


def get_sandbox_endpoint() -> dict[str, Any]:
    """返回 ``sandbox.url`` / ``sandbox.type`` / ``sandbox.preserve_file_sharing_mode``
    / ``sandbox.startup_mode`` / ``sandbox.policy_file``, 以及 yuanrong 可选 knobs。

    ``preserve_file_sharing_mode`` 缺省或为空时返回空串, 由调用方决定默认值
    (当前只有 ``"mount"``)。 ``startup_mode`` 未配置时回落到 ``internal``;
    ``policy_file`` 未配置时返回空串 (由调用方决定默认 policy)。

    当 ``type=yuanrong`` 时额外返回:
    - ``executor`` (缺省 ``docker``)
    - 若 yaml 中存在: ``image`` / ``workdir`` / ``mounts`` / ``cpu`` /
      ``cpu_limit`` / ``memory`` / ``mem_limit`` / ``rootfs``
    - ``url`` 为空时回落占位 ``http://yuanrong.local`` (仅作 cache key)

    Raises:
        ValueError: yaml 里 ``preserve_file_sharing_mode`` 写了非法值时, 直接
            把 :func:`_normalize_preserve_file_sharing_mode` 的异常向上抛, 由
            调用方决定要不要兜底 (例如 TUI 命令层会回执给用户看明确错误)。
    """
    cfg = get_config() or {}
    sandbox = cfg.get("sandbox") or {}
    mode = _normalize_preserve_file_sharing_mode(sandbox.get("preserve_file_sharing_mode"))
    sandbox_type = str(sandbox.get("type") or "").strip()
    url = str(sandbox.get("url") or "").strip()
    result: dict[str, Any] = {
        "url": url,
        "type": sandbox_type,
        "preserve_file_sharing_mode": mode or "",
        "startup_mode": _normalize_sandbox_startup_mode(sandbox.get("startup_mode")),
        "policy_file": str(sandbox.get("policy_file") or "").strip(),
    }
    if sandbox_type == "yuanrong":
        result["url"] = url or _DEFAULT_YUANRONG_URL
        result["executor"] = _normalize_yuanrong_executor(sandbox.get("executor"))
        for key in _YUANRONG_ENDPOINT_OPTIONAL_KEYS:
            if key in sandbox:
                result[key] = sandbox[key]
    return result


def update_sandbox_endpoint(
    url: str,
    sandbox_type: str,
    *,
    preserve_file_sharing_mode: str | None = None,
    startup_mode: str | None = None,
    policy_file: str | None = None,
    executor: str | None = None,
    image: str | None = None,
    workdir: str | None = None,
    mounts: list | None = None,
    cpu: int | None = None,
    cpu_limit: int | None = None,
    memory: int | None = None,
    mem_limit: int | None = None,
    rootfs: dict | None = None,
) -> dict[str, Any]:
    """写入 ``sandbox.url`` / ``sandbox.type`` 以及可选的
    ``preserve_file_sharing_mode`` / ``startup_mode`` / ``policy_file``
    / yuanrong knobs 到 config.yaml; 返回实际写入的字段集合
    (没有改动的字段不在返回里)。

    所有 ``None`` 入参表示"本次不修改该字段, 保留 config.yaml 中既有值",
    以方便 ``_handle_sandbox_enable`` 在不同阶段分批落盘。
    """
    url_value = str(url or "").strip()
    type_value = str(sandbox_type or "").strip()
    if not url_value or not type_value:
        raise ValueError("sandbox url and type must be non-empty")
    mode_value = _normalize_preserve_file_sharing_mode(preserve_file_sharing_mode)

    startup_value: str | None = None
    if startup_mode is not None:
        raw_startup = str(startup_mode).strip().lower()
        if raw_startup not in _VALID_SANDBOX_STARTUP_MODES:
            raise ValueError(
                f"startup_mode must be one of {_VALID_SANDBOX_STARTUP_MODES}, "
                f"got {startup_mode!r}",
            )
        startup_value = raw_startup

    policy_value: str | None = None
    if policy_file is not None:
        policy_text = str(policy_file).strip()
        if not policy_text:
            raise ValueError("policy_file must be non-empty when provided")
        policy_value = policy_text

    executor_value: str | None = None
    if executor is not None:
        executor_value = _normalize_yuanrong_executor(executor)

    data = _load_yaml_round_trip(_CONFIG_YAML_PATH)
    if "sandbox" not in data or not isinstance(data.get("sandbox"), dict):
        data["sandbox"] = {}
    data["sandbox"]["url"] = url_value
    data["sandbox"]["type"] = type_value
    if mode_value is not None:
        data["sandbox"]["preserve_file_sharing_mode"] = mode_value
    if startup_value is not None:
        data["sandbox"]["startup_mode"] = startup_value
    if policy_value is not None:
        data["sandbox"]["policy_file"] = policy_value
    if executor_value is not None:
        data["sandbox"]["executor"] = executor_value

    optional_writes: dict[str, Any] = {}
    if image is not None:
        optional_writes["image"] = str(image).strip()
    if workdir is not None:
        optional_writes["workdir"] = str(workdir).strip()
    if mounts is not None:
        if not isinstance(mounts, list):
            raise ValueError("mounts must be a list")
        optional_writes["mounts"] = mounts
    if cpu is not None:
        optional_writes["cpu"] = int(cpu)
    if cpu_limit is not None:
        optional_writes["cpu_limit"] = int(cpu_limit)
    if memory is not None:
        optional_writes["memory"] = int(memory)
    if mem_limit is not None:
        optional_writes["mem_limit"] = int(mem_limit)
    if rootfs is not None:
        if not isinstance(rootfs, dict):
            raise ValueError("rootfs must be a dict")
        optional_writes["rootfs"] = rootfs
    for key, value in optional_writes.items():
        data["sandbox"][key] = value

    _dump_yaml_round_trip(_CONFIG_YAML_PATH, data)
    result: dict[str, Any] = {"url": url_value, "type": type_value}
    if mode_value is not None:
        result["preserve_file_sharing_mode"] = mode_value
    if startup_value is not None:
        result["startup_mode"] = startup_value
    if policy_value is not None:
        result["policy_file"] = policy_value
    if executor_value is not None:
        result["executor"] = executor_value
    result.update(optional_writes)
    return result


def get_sandbox_preserve_file_sharing_mode() -> str | None:
    """返回 ``sandbox.preserve_file_sharing_mode`` (当前仅 ``"mount"``).

    未配置返回 ``None`` (调用方按默认走)。 非法取值不在这里兜底, 直接由
    :func:`_normalize_preserve_file_sharing_mode` 抛 ``ValueError``。
    """
    cfg = get_config() or {}
    sandbox = cfg.get("sandbox") or {}
    return _normalize_preserve_file_sharing_mode(sandbox.get("preserve_file_sharing_mode"))


def resolve_preserve_file_sharing_mode_default() -> str:
    """返回当前可用的 ``preserve_file_sharing_mode``.

    优先取 yaml 已配置的值, 否则用默认 (``"mount"``)。 当前合法值只有
    ``"mount"`` 一个, 函数保留是为了将来若引入新模式时调用点不必再改一遍。
    """
    configured = get_sandbox_preserve_file_sharing_mode()
    return configured or _DEFAULT_PRESERVE_FILE_SHARING_MODE


def update_sandbox_preserve_file_sharing_mode(mode: str) -> str:
    """写入 ``sandbox.preserve_file_sharing_mode``; 返回归一化后的值.

    空值与非法值都会抛 ``ValueError``——写入路径不允许 "保留旧值" 语义, 必须
    给出明确的合法 mode (当前仅 ``"mount"``)。
    """
    normalized = _normalize_preserve_file_sharing_mode(mode)
    if normalized is None:
        raise ValueError(
            f"preserve_file_sharing_mode must be one of {_VALID_PRESERVE_FILE_SHARING_MODES}"
        )
    data = _load_yaml_round_trip(_CONFIG_YAML_PATH)
    if "sandbox" not in data or not isinstance(data.get("sandbox"), dict):
        data["sandbox"] = {}
    data["sandbox"]["preserve_file_sharing_mode"] = normalized
    _dump_yaml_round_trip(_CONFIG_YAML_PATH, data)
    return normalized


def update_sandbox_runtime(patch: dict[str, Any]) -> dict[str, Any]:
    """合并 patch 到 sandbox runtime 字段, 写回 YAML, 返回合并后的完整 runtime.

    持久化位置: ``sandbox`` 顶层 (扁平形式), 与 ``url`` / ``type`` /
    ``startup_mode`` / ``policy_file`` / ``preserve_file_sharing_mode`` 并列。

    Args:
        patch: 部分字段更新；支持顶层键 ``enabled`` / ``excluded_commands``
            / ``files`` / ``idle_ttl_seconds`` / ``idle_check_interval``
            / ``fallback_on_failure``。
            ``files`` 字典若提供则整体替换；其余键按值合并。 ``idle_*`` 字段
            接受整数秒数 (``<= 0`` 归一化为 ``None`` = 禁用淘汰) 或 ``None``。
    """
    if not isinstance(patch, dict):
        raise ValueError("patch must be an object")

    current = get_sandbox_runtime()
    merged = dict(current)

    if "enabled" in patch:
        merged["enabled"] = bool(patch["enabled"])
    if "fallback_on_failure" in patch:
        merged["fallback_on_failure"] = bool(patch["fallback_on_failure"])
    if "excluded_commands" in patch:
        value = patch["excluded_commands"]
        if not isinstance(value, list):
            raise ValueError("excluded_commands must be a list")
        merged["excluded_commands"] = [
            str(p) for p in value if str(p).strip()
        ]
    if "files" in patch:
        files = patch["files"] or {}
        if not isinstance(files, dict):
            raise ValueError("files must be an object")
        allow = files.get("allow")
        deny = files.get("deny")
        merged["files"] = {
            "allow": list(allow) if isinstance(allow, list) else merged["files"]["allow"],
            "deny": list(deny) if isinstance(deny, list) else merged["files"]["deny"],
        }
    if "idle_ttl_seconds" in patch:
        merged["idle_ttl_seconds"] = _coerce_optional_positive_int(
            patch["idle_ttl_seconds"], field="sandbox.idle_ttl_seconds",
        )
    if "idle_check_interval" in patch:
        merged["idle_check_interval"] = _coerce_optional_positive_int(
            patch["idle_check_interval"], field="sandbox.idle_check_interval",
        )

    data = _load_yaml_round_trip(_CONFIG_YAML_PATH)
    if "sandbox" not in data or not isinstance(data.get("sandbox"), dict):
        data["sandbox"] = {}
    sandbox_block = data["sandbox"]
    # 写入扁平 runtime 字段, 每次 update 都把全集刷一遍, 保证 yaml 形状稳定。
    for key in _SANDBOX_RUNTIME_KEYS:
        sandbox_block[key] = merged[key]
    _dump_yaml_round_trip(_CONFIG_YAML_PATH, data)
    return merged
