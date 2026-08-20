# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

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
from jiuwenswarm.common.local_env_config import (
    SPAWN_ENV_KEYS,
    get_bound_agent_env_ns,
    get_local_config,
    get_task_env_overlay,
)
from jiuwenswarm.common.utils import (
    get_config_dir,
    get_config_file,
    load_yaml_dict,
    merge_template_with_override,
    resolve_shipped_template_config_path,
)

logger = logging.getLogger(__name__)

_CONFIG_MODULE_DIR = Path(__file__).parent
CONFIG_YAML_PATH = get_config_file()
SWARMFLOW_ENABLED_CONFIG_PATH = ("modes", "team", "jiuwen_team", "enable_swarmflow")
DEFAULT_SWARMFLOW_ENABLED = False
# Check if user workspace exists and use it if configured via env
_user_config = os.getenv("JIUWENSWARM_CONFIG_DIR")
if _user_config:
    _CONFIG_MODULE_DIR = Path(_user_config)
elif get_config_dir().exists():
    _CONFIG_MODULE_DIR = get_config_dir()

# Ensure config directory is in sys.path
if str(_CONFIG_MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(_CONFIG_MODULE_DIR))


def _current_config_yaml_path() -> Path:
    """返回用户 override config.yaml 路径（运行时可能被 JIUWENSWARM_CONFIG_DIR 重定位）。"""
    return get_config_file()


def get_merged_config_dict() -> dict[str, Any]:
    """模板与用户 override 合并后的字典（不解析环境变量）。"""
    template = load_yaml_dict(resolve_shipped_template_config_path())
    override = load_yaml_dict(_current_config_yaml_path())
    return merge_template_with_override(template, override)


def resolve_env_vars(value: Any) -> Any:
    """递归解析配置中的环境变量替换语法.

    Supports shell-style ``${VAR:-default}`` (default when unset or empty)
    and ``${VAR-default}`` (default only when unset).

    Args:
        value: 配置值，可能是字符串、字典或列表

    Returns:
        解析后的值
    """
    if isinstance(value, str):
        pattern = r'\$\{([A-Za-z_][A-Za-z0-9_]*)(?:(:-|-)([^}]*))?\}'

        def replace_env(match):
            var_name = match.group(1)
            operator = match.group(2)
            default = match.group(3)
            if var_name in SPAWN_ENV_KEYS:
                current = os.environ.get(var_name)
            else:
                current = get_local_config(var_name)
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
            if operator == ":-":
                if current is None or current == "":
                    return default
                return current
            if operator == "-":
                return default if current is None else current
            return current if current is not None else ""

        return re.sub(pattern, replace_env, value)
    elif isinstance(value, dict):
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


_ConfigNsKey = tuple[str, str] | None
# Cache entries: (monotonic_ts, resolved_config, config_version).
# Keys are either ``ns`` (no overlay) or ``(ns, overlay_content_hash)``.
_resolved_config_by_ns: dict[Any, tuple[float, dict[str, Any], int]] = {}
_CONFIG_CACHE_TTL_SECONDS: float = 20.0
_config_lock = threading.Lock()
_config_version: int = 0


def _overlay_cache_key(ns: _ConfigNsKey, overlay: dict[str, Any] | None) -> Any:
    """Build get_config cache key; overlay content hash avoids id() reuse."""
    if overlay is None:
        return ns
    return (
        ns,
        hash(tuple(sorted((str(k), str(v)) for k, v in overlay.items()))),
    )


def _collect_expired_config_cache_keys(
    entries: dict[Any, tuple[float, dict[str, Any], int]],
    *,
    cleanup_now: float,
    config_version: int,
) -> list[Any]:
    """Return cache keys that are TTL-expired or tied to an old config version."""
    expired_keys: list[Any] = []
    for key, (ts, _, ver) in entries.items():
        if (cleanup_now - ts) >= _CONFIG_CACHE_TTL_SECONDS:
            expired_keys.append(key)
            continue
        if ver != config_version:
            expired_keys.append(key)
    return expired_keys


def get_config():
    """Return merged, env-var-resolved config with per-ns TTL cache.

    Results are cached per bound ``(service_id, agent_id)`` from
    ``bind_agent_env_ns`` with a TTL of ``_CONFIG_CACHE_TTL_SECONDS``.
    Unbound callers share a separate ``None`` slot.

    When a task env overlay is bound, the cache key includes a content hash of
    the overlay so different overlays get different entries while the same
    overlay content reuses the cached result (avoids ``id(overlay)`` reuse
    after GC). Invalidated by ``clear_config_cache()`` via ``_config_version``.

    **WARNING**: The returned dict is a shared reference. Callers MUST NOT
    mutate it (including nested dicts/lists) within the TTL window; use
    ``copy.deepcopy()`` if modification is required.
    """
    global _config_version
    ns: _ConfigNsKey = get_bound_agent_env_ns()
    overlay = get_task_env_overlay()
    cache_key = _overlay_cache_key(ns, overlay)
    now = time.monotonic()

    with _config_lock:
        entry = _resolved_config_by_ns.get(cache_key)
        if entry is not None:
            entry_time, entry_config, entry_version = entry
            if (
                (now - entry_time) < _CONFIG_CACHE_TTL_SECONDS
                and entry_version == _config_version
            ):
                return entry_config
            # Drop stale / version-mismatched entry to avoid unbounded growth.
            del _resolved_config_by_ns[cache_key]
        read_version = _config_version

    # Slow path: lock released during YAML merge / env resolve / normalize.
    config_base = get_merged_config_dict()
    config_base = resolve_env_vars(config_base)
    _normalize_config(config_base)

    with _config_lock:
        if _config_version == read_version:
            _resolved_config_by_ns[cache_key] = (
                time.monotonic(),
                config_base,
                _config_version,
            )
            if len(_resolved_config_by_ns) > 50:
                cleanup_now = time.monotonic()
                expired_keys = _collect_expired_config_cache_keys(
                    _resolved_config_by_ns,
                    cleanup_now=cleanup_now,
                    config_version=_config_version,
                )
                for key in expired_keys:
                    del _resolved_config_by_ns[key]

    return config_base


def clear_config_cache(
    service_id: str | None = None,
    agent_id: str | None = None,
) -> None:
    """Invalidate resolved-config cache (all slots or one ns).

    With no args: clear all slots (yaml write / full reload).
    With ``service_id`` / ``agent_id``: clear that ns and its overlay-keyed
    entries. Clearing ``default``/``default`` also clears the unbound
    (``None``) slot and its overlay keys.
    """
    global _config_version
    with _config_lock:
        _config_version += 1
        if service_id is None and agent_id is None:
            _resolved_config_by_ns.clear()
            return
        from jiuwenswarm.common.local_env_config import normalize_env_ns_id

        sid = normalize_env_ns_id(service_id, default="default")
        aid = normalize_env_ns_id(agent_id, default="default")
        ns_key: _ConfigNsKey = (sid, aid)
        _resolved_config_by_ns.pop(ns_key, None)
        keys_to_remove = [
            key
            for key in _resolved_config_by_ns
            if isinstance(key, tuple) and len(key) == 2 and key[0] == ns_key
        ]
        for key in keys_to_remove:
            _resolved_config_by_ns.pop(key, None)
        if sid == "default" and aid == "default":
            _resolved_config_by_ns.pop(None, None)
            unbound_overlay_keys = [
                key
                for key in _resolved_config_by_ns
                if isinstance(key, tuple) and len(key) == 2 and key[0] is None
            ]
            for key in unbound_overlay_keys:
                _resolved_config_by_ns.pop(key, None)


def get_config_raw():
    """合并包内模板与用户 override 后的快照（不解析环境变量）。

    磁盘上的用户文件可能仅为稀疏 override；本函数返回有效配置树。
    局部写回 override 请使用 ``_load_yaml_round_trip(get_config_file())``。
    """
    return get_merged_config_dict()


def get_default_model_provider(config: dict[str, Any] | None) -> str:
    return resolve_default_model_provider(config)


def validate_persisted_kv_cache_affinity() -> tuple[bool, list[str]]:
    """Validate the persisted KVC invariant after a config write."""
    raw = get_config_raw()
    return validate_affinity_invariant(resolve_env_vars(raw))


def set_config(config):
    with open(_current_config_yaml_path(), "w", encoding="utf-8") as f:
        yaml.safe_dump(config, f, allow_unicode=True, sort_keys=False)
    clear_config_cache()


def _get_bool_env(value: str | None) -> bool | None:
    if value is None:
        return None
    return value.lower() in ("true", "1", "yes")


def _get_evolution_config(config: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(config, dict):
        return {}
    react_config = config.get("react")
    if isinstance(react_config, dict) and isinstance(react_config.get("evolution"), dict):
        return react_config["evolution"]
    evolution_config = config.get("evolution")
    if isinstance(evolution_config, dict):
        return evolution_config
    return {}


def get_evolution_review_trigger_enabled(
    config: dict[str, Any] | None,
    *,
    fallback: bool = False,
) -> bool:
    """Return whether review follow-ups are enabled.

    Only honors ``review_trigger`` config / ``EVOLUTION_REVIEW_TRIGGER`` env.
    Does not fall back to legacy ``auto_scan``.
    """
    raw = get_local_config("EVOLUTION_REVIEW_TRIGGER")
    env_review_trigger = _get_bool_env(None if raw is None else str(raw))
    if env_review_trigger is not None:
        return env_review_trigger
    review_trigger = _get_evolution_config(config).get("review_trigger")
    if isinstance(review_trigger, bool):
        return review_trigger
    return fallback


def get_evolution_signal_trigger_enabled(
    config: dict[str, Any] | None,
    *,
    fallback: bool = True,
) -> bool:
    """Return whether passive signal-based evolution scans are enabled.

    Defaults to ``True`` so deployments without an explicit switch still run
    conversation-end signal scans (``run_evolution``). Honors
    ``signal_trigger`` config / ``EVOLUTION_SIGNAL_TRIGGER`` env, with legacy
    fallback to ``auto_scan`` for older deployments.
    """
    for env_key in ("EVOLUTION_SIGNAL_TRIGGER", "EVOLUTION_AUTO_SCAN"):
        raw = get_local_config(env_key)
        env_signal_trigger = _get_bool_env(None if raw is None else str(raw))
        if env_signal_trigger is not None:
            return env_signal_trigger
    evolution = _get_evolution_config(config)
    signal_trigger = evolution.get("signal_trigger")
    if isinstance(signal_trigger, bool):
        return signal_trigger
    auto_scan = evolution.get("auto_scan")
    if isinstance(auto_scan, bool):
        return auto_scan
    return fallback


def get_passive_skill_evolution_triggers(
    config: dict[str, Any] | None,
) -> dict[str, bool]:
    """Return trigger flags for single-agent and teammate passive evolution.

    Product contract: only cluster teammates and single agents run passive
    signal scans (``run_evolution``). Cluster leaders use ``review_trigger``
    follow-ups via :class:`TeamSkillEvolutionRail` instead.
    """
    return {
        "signal_trigger": get_evolution_signal_trigger_enabled(config),
        "review_trigger": False,
    }


def get_skill_evolution_enabled(config: dict[str, Any] | None) -> bool:
    """Return the canonical ``react.evolution.skill_evolution`` switch.

    Falls back to legacy ``enabled`` for deployments that have not migrated yet.
    """
    evolution = _get_evolution_config(config)
    skill_evolution = evolution.get("skill_evolution")
    if isinstance(skill_evolution, bool):
        return skill_evolution
    return bool(evolution.get("enabled"))


def get_evolution_enabled(config: dict[str, Any] | None) -> bool:
    """Return whether skill self-evolution is enabled.

    Prefer :func:`get_skill_evolution_enabled`; kept for backward compatibility.
    """
    return get_skill_evolution_enabled(config)


def _get_ttse_config(config: dict[str, Any] | None) -> dict[str, Any]:
    """Return the TTSE config block from a full yaml or a react-section cache."""
    if not isinstance(config, dict):
        return {}
    react_config = config.get("react")
    if isinstance(react_config, dict) and isinstance(react_config.get("ttse"), dict):
        return react_config["ttse"]
    ttse_config = config.get("ttse")
    if isinstance(ttse_config, dict):
        return ttse_config
    return {}


def get_ttse_enabled(config: dict[str, Any] | None) -> bool:
    """Return whether TTSE (FACT/TIP) rail should be mounted.

    Reads ``react.ttse.enabled`` first, then top-level ``ttse.enabled``.
    """
    return bool(_get_ttse_config(config).get("enabled"))


def get_skill_create_enabled(config: dict[str, Any] | None) -> bool:
    raw = get_local_config("SKILL_CREATE")
    env_skill_create = _get_bool_env(None if raw is None else str(raw))
    if env_skill_create is not None:
        return env_skill_create
    return _get_evolution_config(config).get("skill_create", False)


def get_evolution_auto_save_enabled(config: dict[str, Any] | None = None) -> bool:
    """Return whether evolution approvals may auto-save without user action."""
    try:
        if config is None:
            config = get_config()
        if not isinstance(config, dict):
            return False
        return _get_evolution_config(config).get("auto_save") is True
    except Exception:
        return False


def set_auto_memory_enabled(enabled: bool) -> None:
    """Set auto-memory enabled status in config.

    Args:
        enabled: True to enable, False to disable.
    """
    data = load_yaml_round_trip(_current_config_yaml_path())
    data["auto_memory_enabled"] = enabled
    dump_yaml_round_trip(_current_config_yaml_path(), data)


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
    clear_config_cache()


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
            str(_config_lock_path(_current_config_yaml_path())),
            timeout=lock_timeout,
        ):
            data = load_yaml_round_trip(_current_config_yaml_path())
            if data is None:
                data = {}
            new_data = mutator(data)
            if new_data is None:
                return data
            dump_yaml_round_trip(_current_config_yaml_path(), new_data)
            return new_data


# Backward-compat aliases — downstream modules import the underscore-prefixed names
_CONFIG_YAML_PATH = CONFIG_YAML_PATH
_load_yaml_round_trip = load_yaml_round_trip
_dump_yaml_round_trip = dump_yaml_round_trip


def update_heartbeat_in_config(payload: dict[str, Any]) -> None:
    """只更新 heartbeat 段并写回。"""
    data = load_yaml_round_trip(_current_config_yaml_path())
    if "heartbeat" not in data:
        data["heartbeat"] = {}
    hb = data["heartbeat"]
    if "every" in payload:
        hb["every"] = payload["every"]
    if "target" in payload:
        hb["target"] = payload["target"]
    if "active_hours" in payload:
        hb["active_hours"] = payload["active_hours"]
    dump_yaml_round_trip(_current_config_yaml_path(), data)


def update_channel_in_config(channel_id: str, conf: dict[str, Any]) -> None:
    """只更新 channels[channel_id] 并写回。"""
    data = load_yaml_round_trip(_current_config_yaml_path())
    if "channels" not in data:
        data["channels"] = {}
    channels = data["channels"]
    if channel_id not in channels:
        channels[channel_id] = {}
    section = channels[channel_id]
    for k, v in conf.items():
        section[k] = v
    dump_yaml_round_trip(_current_config_yaml_path(), data)


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
    data = load_yaml_round_trip(_current_config_yaml_path())
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

    dump_yaml_round_trip(_current_config_yaml_path(), data)


def update_channel_subsection_in_config(
    channel_id: str,
    subsection_id: str,
    conf: dict[str, Any] | list[Any] | Any,
) -> None:
    """更新 channels[channel_id][subsection_id] 并写回。

    若 ``conf`` 为 dict，则合并（partial update）到现有 subsection 中；
    若为 list 或其他非 dict 类型，则整体替换 subsection。
    """
    data = load_yaml_round_trip(_current_config_yaml_path())
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
    dump_yaml_round_trip(_current_config_yaml_path(), data)


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
    data = load_yaml_round_trip(_current_config_yaml_path())
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

    dump_yaml_round_trip(_current_config_yaml_path(), data)


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
    data = load_yaml_round_trip(_current_config_yaml_path())
    apps = data.get("channels", {}).get(channel_id, {}).get("apps", [])
    if not isinstance(apps, list):
        return False
    for app in apps:
        if isinstance(app, dict) and app.get(app_id_key) == app_identifier:
            app.update(field_values)
            dump_yaml_round_trip(_current_config_yaml_path(), data)
            return True
    return False


def update_preferred_language_in_config(lang: str) -> None:
    """只更新顶层 preferred_language 并写回。非法值回退为 zh，与 set_preferred_language_in_config_file 一致。"""
    normalized = str(lang or "zh").strip().lower()
    if normalized not in ("zh", "en"):
        normalized = "zh"
    data = load_yaml_round_trip(_current_config_yaml_path())
    data["preferred_language"] = normalized
    dump_yaml_round_trip(_current_config_yaml_path(), data)


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
    data = load_yaml_round_trip(_current_config_yaml_path())
    if "browser" not in data:
        data["browser"] = {}
    section = data["browser"]
    for k, v in updates.items():
        section[k] = v
    dump_yaml_round_trip(_current_config_yaml_path(), data)


def update_evolution_enabled_in_config(value: bool) -> None:
    """更新 react.evolution.enabled（Skills 自演进总开关）并写回用户 override。"""
    data = load_yaml_round_trip(_current_config_yaml_path())
    react = data.get("react")
    if not isinstance(react, dict):
        react = {}
        data["react"] = react
    evolution = react.get("evolution")
    if not isinstance(evolution, dict):
        evolution = {}
        react["evolution"] = evolution
    evolution["enabled"] = value
    dump_yaml_round_trip(_current_config_yaml_path(), data)


def update_context_engine_enabled_in_config(value: bool) -> None:
    """更新 react.context_engine_config.enabled（上下文压缩开关）并写回。"""
    data = load_yaml_round_trip(_current_config_yaml_path())
    if "react" not in data:
        data["react"] = {}
    react = data["react"]
    if "context_engine_config" not in react:
        react["context_engine_config"] = {}
    react["context_engine_config"]["enabled"] = value
    dump_yaml_round_trip(_current_config_yaml_path(), data)


def update_kv_cache_affinity_enabled_in_config(value: bool) -> None:
    """更新 react.kv_cache_affinity_config.enable_kv_cache_affinity 并写回。"""
    data = load_yaml_round_trip(_current_config_yaml_path())
    if "react" not in data:
        data["react"] = {}
    react = data["react"]
    if "kv_cache_affinity_config" not in react:
        react["kv_cache_affinity_config"] = {}
    react["kv_cache_affinity_config"]["enable_kv_cache_affinity"] = value
    if value:
        react["kv_cache_affinity_config"]["enable_kv_cache_release"] = False
    dump_yaml_round_trip(_current_config_yaml_path(), data)


def update_kv_cache_release_enabled_in_config(value: bool) -> None:
    """更新 react.kv_cache_affinity_config.enable_kv_cache_release 并写回。"""
    data = load_yaml_round_trip(_current_config_yaml_path())
    if "react" not in data:
        data["react"] = {}
    react = data["react"]
    if "kv_cache_affinity_config" not in react:
        react["kv_cache_affinity_config"] = {}
    react["kv_cache_affinity_config"]["enable_kv_cache_release"] = value
    if value:
        react["kv_cache_affinity_config"]["enable_kv_cache_affinity"] = False
    dump_yaml_round_trip(_current_config_yaml_path(), data)


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
    data = load_yaml_round_trip(_current_config_yaml_path())
    if "symphony" not in data or data["symphony"] is None:
        data["symphony"] = {}
    symphony = data["symphony"]
    _merge_config_dict(symphony, updates)
    dump_yaml_round_trip(_current_config_yaml_path(), data)


def update_skill_retrieval_in_config(updates: dict[str, Any]) -> None:
    """更新 symphony.skill_retrieval 配置段并写回。"""
    data = load_yaml_round_trip(_current_config_yaml_path())
    if "symphony" not in data or data["symphony"] is None:
        data["symphony"] = {}
    symphony = data["symphony"]
    if "skill_retrieval" not in symphony or symphony["skill_retrieval"] is None:
        symphony["skill_retrieval"] = {}
    section = symphony["skill_retrieval"]
    _merge_config_dict(section, updates)
    dump_yaml_round_trip(_current_config_yaml_path(), data)


def update_permissions_enabled_in_config(value: bool) -> None:
    """更新 permissions.enabled（工具安全护栏开关）并写回。"""
    def mutate(perms: dict[str, Any]) -> None:
        perms["enabled"] = value

    _persist_permissions(mutate)


def _effective_permissions() -> dict[str, Any]:
    from jiuwenswarm.agents.harness.common.rails.permissions.config_loader import (
        get_effective_permissions_config,
    )

    return get_effective_permissions_config()


def _persist_permissions(mutate_fn) -> dict[str, Any]:
    from jiuwenswarm.agents.harness.common.rails.permissions.config_loader import (
        persist_permissions_mutate,
    )

    return persist_permissions_mutate(mutate_fn, persist_scope="base")


def get_permissions_file_guard_workspace_rw_enabled() -> bool:
    """读取 ``permissions.file_guard.workspace.rw_enabled``（缺省为 True）。"""
    fg = _effective_permissions().get("file_guard")
    if not isinstance(fg, dict):
        return True
    ws = fg.get("workspace")
    if not isinstance(ws, dict):
        return True
    return bool(ws.get("rw_enabled", True))


def update_permissions_file_guard_workspace_rw_enabled_in_config(value: bool) -> None:
    """更新 ``permissions.file_guard.workspace.rw_enabled`` 并写回。"""
    def mutate(perms: dict[str, Any]) -> None:
        fg = perms.get("file_guard")
        if not isinstance(fg, dict):
            fg = {}
            perms["file_guard"] = fg
        ws = fg.get("workspace")
        if not isinstance(ws, dict):
            ws = {}
            fg["workspace"] = ws
        ws["rw_enabled"] = bool(value)

    _persist_permissions(mutate)


_PERMISSIONS_WORKSPACE_ACCESS_AXES: tuple[str, ...] = ("read", "write", "exec")
_PERMISSIONS_WORKSPACE_ACCESS_LEVELS: frozenset[str] = frozenset({"allow", "ask", "deny"})


def get_permissions_file_guard_workspace_access() -> dict[str, str]:
    """读取 ``permissions.file_guard.workspace`` 的 read/write/exec 三轴。

    缺省轴按 openjiuwen 引擎语义返回 ``ask``（与 ``default_level=ASK`` 一致）。
    非法值（非字符串、或不在 allow/ask/deny 范围内）会被清洗为 ``ask``，
    确保 GET 端点始终返回合法值，与 SET 路径的校验对称。
    """
    fg = _effective_permissions().get("file_guard")
    if not isinstance(fg, dict):
        return {axis: "ask" for axis in _PERMISSIONS_WORKSPACE_ACCESS_AXES}
    ws = fg.get("workspace")
    if not isinstance(ws, dict):
        return {axis: "ask" for axis in _PERMISSIONS_WORKSPACE_ACCESS_AXES}

    result: dict[str, str] = {}
    for axis in _PERMISSIONS_WORKSPACE_ACCESS_AXES:
        raw = ws.get(axis, "ask")
        if not isinstance(raw, str) or raw not in _PERMISSIONS_WORKSPACE_ACCESS_LEVELS:
            logger.warning(
                "[config] permissions.file_guard.workspace.%s has invalid value %r, "
                "coerced to 'ask'",
                axis,
                raw,
            )
            result[axis] = "ask"
        else:
            result[axis] = raw
    return result


def update_permissions_file_guard_workspace_access_in_config(axis: dict[str, Any]) -> dict[str, str]:
    """更新 ``permissions.file_guard.workspace`` 的 read/write/exec 三轴并写回。

    支持部分更新：仅写入 ``axis`` 中出现的键（read/write/exec），取值必须为
    ``allow`` / ``ask`` / ``deny`` 之一。返回更新后的完整三轴快照。
    """
    normalized: dict[str, str] = {}
    for axis_name in _PERMISSIONS_WORKSPACE_ACCESS_AXES:
        if axis_name not in axis:
            continue
        val = axis[axis_name]
        if val not in _PERMISSIONS_WORKSPACE_ACCESS_LEVELS:
            raise ValueError(
                f"workspace.{axis_name} must be one of "
                f"{sorted(_PERMISSIONS_WORKSPACE_ACCESS_LEVELS)}, got {val!r}"
            )
        normalized[axis_name] = val

    def mutate(perms: dict[str, Any]) -> None:
        fg = perms.get("file_guard")
        if not isinstance(fg, dict):
            fg = {}
            perms["file_guard"] = fg
        ws = fg.get("workspace")
        if not isinstance(ws, dict):
            ws = {}
            fg["workspace"] = ws
        for axis_name, val in normalized.items():
            ws[axis_name] = val

    _persist_permissions(mutate)
    return get_permissions_file_guard_workspace_access()


def update_auto_recap_enabled_in_config(value: bool) -> None:
    """更新 auto_recap.enabled（自动回顾开关）并写回。"""
    data = load_yaml_round_trip(_current_config_yaml_path())
    if "auto_recap" not in data:
        data["auto_recap"] = {}
    data["auto_recap"]["enabled"] = value
    dump_yaml_round_trip(_current_config_yaml_path(), data)


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


def update_proactive_recommendation_in_config(updates: dict[str, Any]) -> None:
    """更新 proactive_recommendation 配置段并写回。"""
    data = load_yaml_round_trip(_current_config_yaml_path())
    if "proactive_recommendation" not in data or data["proactive_recommendation"] is None:
        data["proactive_recommendation"] = {}
    section = data["proactive_recommendation"]
    _merge_config_dict(section, updates)
    dump_yaml_round_trip(_current_config_yaml_path(), data)


def update_updater_in_config(updates: dict[str, Any]) -> None:
    """只更新 updater 段并写回。"""
    data = load_yaml_round_trip(_current_config_yaml_path())
    if "updater" not in data:
        data["updater"] = {}
    section = data["updater"]
    for key, value in updates.items():
        section[key] = value
    dump_yaml_round_trip(_current_config_yaml_path(), data)


def update_memory_enabled_in_config(mode: str, value: bool) -> None:
    """更新 memory.enabled（记忆系统开关）并写回。"""
    _update_memory_in_modes_config(mode, "enabled", value)


def update_proactive_memory_in_config(mode: str, value: bool) -> None:
    """更新 memory.proactive_memory（主动记忆开关）并写回。"""
    _update_memory_in_modes_config(mode, "is_proactive", value)


def _update_memory_in_modes_config(mode: str, item: str, value: bool) -> None:
    data = load_yaml_round_trip(_current_config_yaml_path())
    if "modes" not in data:
        data["modes"] = {}
    if "claw" not in data["modes"]:
        data["modes"]["claw"] = {}
    if mode not in data["modes"]["claw"]:
        data["modes"]["claw"][mode] = {}
    if "memory" not in data["modes"]["claw"][mode]:
        data["modes"]["claw"][mode]["memory"] = {}
    data["modes"]["claw"][mode]["memory"][item] = value
    dump_yaml_round_trip(_current_config_yaml_path(), data)


# ---------- 数字分身相关配置 ----------

def get_permissions_owner_scopes() -> dict[str, Any]:
    """读取 permissions.owner_scopes 及 deny_guidance_message."""
    perm = _effective_permissions()
    return {
        "owner_scopes": perm.get("owner_scopes", {}),
        "deny_guidance_message": perm.get("deny_guidance_message", ""),
    }


def update_permissions_owner_scopes_in_config(
    owner_scopes: dict[str, Any],
    deny_guidance_message: str | None = None,
) -> None:
    """更新 permissions.owner_scopes（及可选 deny_guidance_message）并写回。"""
    def mutate(perms: dict[str, Any]) -> None:
        perms["owner_scopes"] = owner_scopes
        if deny_guidance_message is not None:
            perms["deny_guidance_message"] = deny_guidance_message

    _persist_permissions(mutate)


def get_permissions_deny_guidance() -> str:
    """读取 permissions.deny_guidance_message."""
    return str(_effective_permissions().get("deny_guidance_message", ""))


def update_permissions_deny_guidance_in_config(msg: str) -> None:
    """更新 permissions.deny_guidance_message 并写回。"""
    def mutate(perms: dict[str, Any]) -> None:
        perms["deny_guidance_message"] = msg

    _persist_permissions(mutate)


# ---------- Web UI：permissions.tools / rules / approval_overrides ----------

_VALID_PERM_LEVEL = frozenset({"allow", "ask", "deny"})
_VALID_RULE_SEVERITY = frozenset({"LOW", "MEDIUM", "HIGH", "CRITICAL"})
_RULE_MUTABLE_KEYS = frozenset({"tools", "pattern", "severity", "action", "description", "match_type"})


def normalize_permissions_tool_level(raw: Any) -> str | None:
    if isinstance(raw, str):
        level = raw.strip().lower()
        if level in ("guard", "ask"):
            return "ask"
        if level in _VALID_PERM_LEVEL:
            return level
        return None
    if isinstance(raw, dict):
        star = raw.get("*")
        if star is not None:
            return normalize_permissions_tool_level(star)
    return None


def get_permissions_defaults_level() -> str:
    cfg = get_config() or {}
    raw = (cfg.get("permissions") or {}).get("defaults", "guard")
    return normalize_permissions_tool_level(raw) or "ask"


def build_permissions_tools_list_view(
    catalog_by_name: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    from jiuwenswarm.server.runtime.tool_catalog import ui_list_short_description

    catalog = dict(catalog_by_name or {})
    explicit = get_permissions_tools().get("tools")
    if not isinstance(explicit, dict):
        explicit = {}
    default_level = get_permissions_defaults_level()

    seen: set[str] = set()
    tools_out: list[dict[str, Any]] = []

    for name in sorted(catalog.keys()):
        seen.add(name)
        entry = catalog.get(name, {})
        raw_explicit = explicit.get(name)
        if raw_explicit is not None:
            level = normalize_permissions_tool_level(raw_explicit) or default_level
            configured = True
        else:
            level = default_level
            configured = False

        short = ui_list_short_description(
            name,
            description=str(entry.get("description", "") or ""),
            short_description=str(entry.get("short_description", "") or ""),
        )
        tools_out.append({
            "name": name,
            "short_description": short,
            "level": level,
            "configured": configured,
            "registered": True,
        })

    for name in sorted(explicit.keys()):
        if name in seen:
            continue
        raw_explicit = explicit.get(name)
        level = normalize_permissions_tool_level(raw_explicit) or default_level
        tools_out.append({
            "name": name,
            "short_description": "",
            "level": level,
            "configured": True,
            "registered": False,
        })

    return {"default_level": default_level, "tools": tools_out}


def get_permissions_tools() -> dict[str, Any]:
    """返回 ``permissions.tools``（原始结构，可能含 legacy dict）。"""
    tools = _effective_permissions().get("tools")
    if not isinstance(tools, dict):
        return {"tools": {}}
    return {"tools": dict(tools)}


def normalize_permissions_tool_level(raw: Any) -> str | None:
    """Normalize a configured tool level for UI display."""
    if isinstance(raw, str):
        level = raw.strip().lower()
        if level in {"guard", "ask"}:
            return "ask"
        if level in _VALID_PERM_LEVEL:
            return level
        return None
    if isinstance(raw, dict) and "*" in raw:
        return normalize_permissions_tool_level(raw.get("*"))
    return None


def get_permissions_defaults_level() -> str:
    """Return the effective default tool level as ``allow|ask|deny``."""
    raw = _effective_permissions().get("defaults", "guard")
    return normalize_permissions_tool_level(raw) or "ask"


def build_permissions_tools_list_view(
    catalog_by_name: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Build the permissions list from runtime and explicitly configured tools."""
    from jiuwenswarm.server.runtime.tool_catalog import (
        get_stable_tools_catalog,
        ui_list_short_description,
    )

    runtime_catalog = dict(catalog_by_name or {})
    configured_tools = get_permissions_tools().get("tools")
    if not isinstance(configured_tools, dict):
        configured_tools = {}
    default_level = get_permissions_defaults_level()
    preferred_language = str(
        (get_config() or {}).get("preferred_language", "")
    ).lower()
    stable_catalog = get_stable_tools_catalog(
        "en" if preferred_language.startswith("en") else "cn"
    )
    visible_names = set(runtime_catalog) | {
        str(name).strip() for name in configured_tools if str(name).strip()
    }

    tools_out: list[dict[str, Any]] = []
    for name in sorted(visible_names):
        runtime_entry = runtime_catalog.get(name, {})
        stable_entry = stable_catalog.get(name, {})
        short = ui_list_short_description(
            name,
            description=str(runtime_entry.get("description", "") or ""),
            short_description=str(
                runtime_entry.get("short_description", "") or ""
            ),
        )
        if not short:
            short = ui_list_short_description(
                name,
                description=str(stable_entry.get("description", "") or ""),
                short_description=str(
                    stable_entry.get("short_description", "") or ""
                ),
            )
        configured = name in configured_tools
        level = (
            normalize_permissions_tool_level(configured_tools.get(name))
            if configured
            else None
        ) or default_level
        tools_out.append(
            {
                "name": name,
                "short_description": short,
                "level": level,
                "configured": configured,
                "registered": name in runtime_catalog,
            }
        )
    return {"default_level": default_level, "tools": tools_out}


def replace_permissions_tools_in_config(tools: Any) -> None:
    """整表替换 ``permissions.tools``；值仅允许 ``allow|ask|deny``（或 legacy ``{\"*\": level}``）。"""
    normalized = _validate_tools_map(tools)

    def mutate(perms: dict[str, Any]) -> None:
        perms["tools"] = normalized

    _persist_permissions(mutate)


def update_permissions_tool_in_config(tool_name: str, level: Any) -> dict[str, Any]:
    """合并单条工具级别到 ``permissions.tools`` 并写回。

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
    result: dict[str, str] = {}

    def mutate(perms: dict[str, Any]) -> None:
        existing = perms.get("tools")
        if not isinstance(existing, dict):
            existing = {}
        merged = {str(k): v for k, v in existing.items()}
        merged[name] = piece[name]
        perms["tools"] = merged
        result.update(merged)

    _persist_permissions(mutate)
    return {"tools": dict(result)}


def delete_permissions_tool_in_config(tool_name: str) -> bool:
    """从 ``permissions.tools`` 中删除一个键；不存在则返回 False。"""
    name = str(tool_name).strip()
    if not name:
        raise ValueError("tool name must be non-empty")
    found = {"value": False}

    def mutate(perms: dict[str, Any]) -> None:
        tools = perms.get("tools")
        if not isinstance(tools, dict):
            return
        key_to_drop = None
        for k in tools:
            if str(k).strip() == name:
                key_to_drop = k
                break
        if key_to_drop is None:
            return
        perms["tools"] = {k: v for k, v in tools.items() if k != key_to_drop}
        found["value"] = True

    _persist_permissions(mutate)
    return bool(found["value"])


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
    rules = _effective_permissions().get("rules")
    if not isinstance(rules, list):
        return {"rules": []}
    return {"rules": [r for r in rules if isinstance(r, dict)]}


def get_permissions_approval_overrides() -> dict[str, Any]:
    """返回 ``permissions.approval_overrides`` 列表（仅 dict 项）。"""
    raw = _effective_permissions().get("approval_overrides")
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

    def mutate(perms: dict[str, Any]) -> None:
        rules = perms.get("rules")
        if not isinstance(rules, list):
            rules = []
        if any(isinstance(r, dict) and str(r.get("id") or "").strip() == rid for r in rules):
            raise ValueError(f"rule id already exists: {rid}")
        rules.append(stored)
        perms["rules"] = rules

    _persist_permissions(mutate)
    return stored


def update_permissions_rule_in_config(rule_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    """按 ``id`` 合并更新一条 rule。"""
    rid = str(rule_id or "").strip()
    if not rid:
        raise ValueError("id is required")
    if not isinstance(patch, dict):
        raise ValueError("patch must be an object")

    merged_result: dict[str, Any] = {}

    def mutate(perms: dict[str, Any]) -> None:
        rules = perms.get("rules")
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
        perms["rules"] = rules
        merged_result.update(merged)

    _persist_permissions(mutate)
    return merged_result


def delete_permissions_rule_in_config(rule_id: str) -> bool:
    """删除 ``permissions.rules`` 中指定 ``id``；若未找到返回 False。"""
    rid = str(rule_id or "").strip()
    if not rid:
        raise ValueError("id is required")
    found = {"value": False}

    def mutate(perms: dict[str, Any]) -> None:
        rules = perms.get("rules")
        if not isinstance(rules, list):
            return
        new_rules = [
            r for r in rules
            if not (isinstance(r, dict) and str(r.get("id") or "").strip() == rid)
        ]
        if len(new_rules) == len(rules):
            return
        perms["rules"] = new_rules
        found["value"] = True

    _persist_permissions(mutate)
    return bool(found["value"])


def delete_permissions_approval_override_in_config(override_id: str) -> bool:
    """按 ``id`` 删除 ``approval_overrides`` 中一项；若未找到返回 False。"""
    oid = str(override_id or "").strip()
    if not oid:
        raise ValueError("id is required")
    found = {"value": False}

    def mutate(perms: dict[str, Any]) -> None:
        ov = perms.get("approval_overrides")
        if not isinstance(ov, list):
            return
        new_ov = [
            x for x in ov
            if not (isinstance(x, dict) and str(x.get("id") or "").strip() == oid)
        ]
        if len(new_ov) == len(ov):
            return
        perms["approval_overrides"] = new_ov
        found["value"] = True

    _persist_permissions(mutate)
    return bool(found["value"])


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
        return _decrypt_model_entries(models["defaults"])

    # 旧格式：单个 default 对象 → 包装为列表
    if "default" in models and isinstance(models["default"], dict):
        return _decrypt_model_entries([models["default"]])

    # 回退：从环境变量构造（env var 已在 resolve_env_vars 中解密）
    alias = str(get_local_config("MODEL_ALIAS", "") or "")
    entry: dict[str, Any] = {
        "model_client_config": {
            "api_base": str(get_local_config("API_BASE", "") or ""),
            "api_key": str(get_local_config("API_KEY", "") or ""),
            "model_name": str(get_local_config("MODEL_NAME", "") or ""),
            "client_provider": str(get_local_config("MODEL_PROVIDER", "") or ""),
            "custom_headers": _parse_custom_headers(get_local_config("CUSTOM_HEADERS", None)),
            "timeout": 1800,
            "verify_ssl": False,
        },
        "model_config_obj": {"temperature": 0.95},
    }
    if alias:
        entry["alias"] = alias
    return [entry]


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

    data = load_yaml_round_trip(_current_config_yaml_path())
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
            dump_yaml_round_trip(_current_config_yaml_path(), data)
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
        dump_yaml_round_trip(_current_config_yaml_path(), data)
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


def resolve_legacy_team_model_ref(
    model_raw: dict[str, Any],
    config_data: dict[str, Any] | None,
) -> str | None:
    legacy_model_name = str(resolve_env_vars(model_raw.get("model") or "")).strip()
    if not legacy_model_name:
        return None
    models = config_data.get("models") if isinstance(config_data, dict) else None
    defaults = models.get("defaults") if isinstance(models, dict) else None
    if not isinstance(defaults, list):
        return None

    source_provider = str(
        resolve_env_vars(model_raw.get("provider") or "")
    ).strip().lower()
    source_api_base = str(
        resolve_env_vars(model_raw.get("api_base") or "")
    ).strip().rstrip("/")
    matches: list[int] = []
    for index, entry in enumerate(defaults):
        if not isinstance(entry, dict):
            continue
        client_config = entry.get("model_client_config")
        if not isinstance(client_config, dict):
            continue
        candidate_name = str(resolve_env_vars(client_config.get("model_name") or "")).strip()
        if candidate_name != legacy_model_name:
            continue
        candidate_provider = str(
            resolve_env_vars(client_config.get("client_provider") or "")
        ).strip().lower()
        if source_provider and candidate_provider != source_provider:
            continue
        candidate_api_base = str(
            resolve_env_vars(client_config.get("api_base") or "")
        ).strip().rstrip("/")
        if source_api_base and candidate_api_base != source_api_base:
            continue
        matches.append(index)
    if len(matches) != 1:
        return None
    return f"{legacy_model_name}#{matches[0]}"


def _transform_front_team_model_config(
    model_raw: dict[str, Any],
    config_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    model_ref = str(model_raw.get("ref") or "").strip()
    if not model_ref:
        legacy_value = str(model_raw.get("model") or "").strip()
        if "#" in legacy_value:
            model_ref = legacy_value
    if not model_ref:
        if not str(model_raw.get("model") or "").strip():
            return {}
        model_ref = resolve_legacy_team_model_ref(model_raw, config_data)
        if model_ref is None:
            legacy_model_name = str(model_raw.get("model") or "").strip()
            raise ValueError(
                f"legacy team agent model {legacy_model_name!r} does not resolve to a unique models.defaults entry"
            )

    model_name, separator, index_text = model_ref.rpartition("#")
    if not separator or not model_name.strip():
        raise ValueError("team agent model.ref must use model_name#index")
    try:
        model_index = int(index_text)
    except ValueError as exc:
        raise ValueError("team agent model.ref index must be an integer") from exc
    if model_index < 0:
        raise ValueError("team agent model.ref index must be non-negative")
    return {"ref": f"{model_name.strip()}#{model_index}"}


def _transform_front_team_agent_spec(
    agent_key: str,
    agent_raw: Any,
    config_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    agent_config = _require_dict(agent_raw, f"agents.{agent_key}")
    transformed: dict[str, Any] = {}

    if "model" in agent_config:
        model_raw = _require_dict(agent_config.get("model"), f"agents.{agent_key}.model")
        transformed_model = _transform_front_team_model_config(model_raw, config_data)
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
    config_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resolved_key = _require_non_empty_string(agent_key, field_name)
    if resolved_key not in agents_raw:
        raise ValueError(f"{field_name} references unknown agent_key: {resolved_key}")
    return _transform_front_team_agent_spec(resolved_key, agents_raw[resolved_key], config_data)


def _build_modes_team_mapping(
    front_payload: dict[str, Any],
    config_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
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
            if key in {"leader", "teammate", "predefined_members"}:
                continue
            transformed_team[key] = value
        transformed_team["team_name"] = team_name

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
            config_data=config_data,
        )

        teammate_raw = team_raw.get("teammate")
        teammate_agent_spec: dict[str, Any] | None = None
        if teammate_raw is not None:
            teammate_raw = _require_dict(teammate_raw, f"team[{team_index}].teammate")
            teammate_agent_spec = _resolve_front_team_agent_spec(
                agents_raw,
                teammate_raw.get("agent_key"),
                field_name=f"team[{team_index}].teammate.agent_key",
                config_data=config_data,
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
                config_data=config_data,
            )
            transformed_agents[member_name] = member_agent_spec

        if transformed_members:
            transformed_team["predefined_members"] = transformed_members

        if teammate_agent_spec is not None:
            transformed_agents["teammate"] = deepcopy(teammate_agent_spec)

        transformed_team["agents"] = transformed_agents
        team_mapping[team_name] = transformed_team

    return team_mapping


def _build_front_agent_registry(
    front_payload: dict[str, Any],
    config_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    agents_raw = _require_dict(front_payload.get("agents"), "agents")
    registry: dict[str, Any] = {}
    for agent_key, agent_raw in agents_raw.items():
        resolved_key = _require_non_empty_string(agent_key, f"agents.{agent_key}")
        registry[resolved_key] = _transform_front_team_agent_spec(resolved_key, agent_raw, config_data)
    return registry


def replace_teams_in_config(front_payload: dict[str, Any]) -> None:
    """Replace ``modes.team`` using the frontend team-editor payload.

    Keep legacy top-level ``team`` config intact for backward compatibility.

    If team array is empty, delete ``modes.team`` from config.
    """
    if not isinstance(front_payload, dict):
        raise ValueError("payload must be an object")

    teams_raw = front_payload.get("team")
    data = _load_yaml_round_trip(_current_config_yaml_path())

    panel_cfg_modified = False
    agent_registry = None
    if "agents" in front_payload:
        agent_registry = _build_front_agent_registry(front_payload, data)
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
        _dump_yaml_round_trip(_current_config_yaml_path(), data)
        return

    # 非空数组：正常构建并保存
    team_mapping = _build_modes_team_mapping(front_payload, data)

    data = load_yaml_round_trip(_current_config_yaml_path())
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
    dump_yaml_round_trip(_current_config_yaml_path(), data)


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
    data = load_yaml_round_trip(_current_config_yaml_path())
    current = data
    path_so_far: list[str] = []
    for segment in SWARMFLOW_ENABLED_CONFIG_PATH[:-1]:
        path_so_far.append(segment)
        current = _ensure_config_object(current, segment, ".".join(path_so_far))
    current[SWARMFLOW_ENABLED_CONFIG_PATH[-1]] = bool(enabled)
    dump_yaml_round_trip(_current_config_yaml_path(), data)


def get_mcp_servers() -> list[dict[str, Any]]:
    """读取 config.yaml 中的 mcp.servers（原始结构，不解析环境变量）。"""
    data = get_config_raw()
    mcp_cfg = data.get("mcp", {})
    if not isinstance(mcp_cfg, dict):
        return []
    servers = mcp_cfg.get("servers", [])
    if not isinstance(servers, list):
        return []
    return [item for item in servers if isinstance(item, dict)]


def upsert_mcp_server_in_config(server: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """新增或更新 mcp.servers 条目，返回（条目, 是否创建）。"""
    name = str(server.get("name", "")).strip()
    if not name:
        raise ValueError("MCP server name is required")
    data = load_yaml_round_trip(_current_config_yaml_path())
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
    dump_yaml_round_trip(_current_config_yaml_path(), data)
    return server, created


def set_mcp_server_enabled_in_config(name: str, enabled: bool) -> dict[str, Any]:
    """切换 mcp.servers 指定 name 的 enabled 状态并返回更新后的条目。"""
    target = str(name or "").strip()
    if not target:
        raise ValueError("MCP server name is required")
    data = load_yaml_round_trip(_current_config_yaml_path())
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
        dump_yaml_round_trip(_current_config_yaml_path(), data)
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
    data = load_yaml_round_trip(_current_config_yaml_path())
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
        dump_yaml_round_trip(_current_config_yaml_path(), data)
        return removed
    raise KeyError(f"MCP server '{target}' not found")


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
    data = load_yaml_round_trip(_current_config_yaml_path())
    if "react" not in data or not isinstance(data["react"], dict):
        data["react"] = {}
    react = data["react"]
    if "subagents" not in react or not isinstance(react["subagents"], dict):
        react["subagents"] = {}
    subagents = react["subagents"]
    if target not in subagents or not isinstance(subagents[target], dict):
        subagents[target] = {}
    subagents[target]["enabled"] = bool(enabled)
    dump_yaml_round_trip(_current_config_yaml_path(), data)


def remove_subagent_from_config(name: str) -> bool:
    """从 react.subagents.<name> 中删除 agent 条目。

    Returns:
        True: 找到并删除
        False: 条目不存在
    """
    target = str(name or "").strip()
    if not target:
        raise ValueError("subagent name is required")
    data = load_yaml_round_trip(_current_config_yaml_path())
    react = data.get("react")
    if not isinstance(react, dict):
        return False
    subagents = react.get("subagents")
    if not isinstance(subagents, dict):
        return False
    if target not in subagents:
        return False
    del subagents[target]
    dump_yaml_round_trip(_current_config_yaml_path(), data)
    return True


def update_memory_forbidden_enabled_in_config(value: bool) -> None:
    """更新 memory.forbidden_memory_definition.enabled（记忆系统敏感信息过滤开关）并写回。"""
    data = load_yaml_round_trip(_current_config_yaml_path())
    if "memory" not in data:
        data["memory"] = {}
    if "forbidden_memory_definition" not in data["memory"]:
        data["memory"]["forbidden_memory_definition"] = {}
    data["memory"]["forbidden_memory_definition"]["enabled"] = value
    dump_yaml_round_trip(_current_config_yaml_path(), data)


def update_memory_forbidden_description_in_config(description: dict[str, str]) -> None:
    """更新 memory.forbidden_memory_definition.description（禁止记忆内容描述）并写回。"""
    data = load_yaml_round_trip(_current_config_yaml_path())
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
    dump_yaml_round_trip(_current_config_yaml_path(), data)


def update_memory_forbidden_in_config(updates: dict[str, Any]) -> None:
    """更新 memory.forbidden_memory_definition 并写回。"""
    data = load_yaml_round_trip(_current_config_yaml_path())
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
    dump_yaml_round_trip(_current_config_yaml_path(), data)


def update_a2ui_in_config(updates: dict[str, Any]) -> None:
    """更新 a2ui 配置段并写回 config.yaml。"""
    data = _load_yaml_round_trip(_current_config_yaml_path())
    if "a2ui" not in data:
        data["a2ui"] = {}
    section = data["a2ui"]
    for key, value in updates.items():
        section[key] = value
    _dump_yaml_round_trip(_current_config_yaml_path(), data)


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


def _prune_override_keys(template: dict[str, Any], override: dict[str, Any], depth: int = 0) -> dict[str, Any]:
    """递归清理 override 中模板不存在的字段（Remove 规则）。

    与 _deep_merge 的 Remove 规则一致，但只做清理不合并模板值。
    """
    if depth >= 4:
        return override
    result: dict[str, Any] = {}
    for key, over_val in override.items():
        if key not in template:
            # 模板没有此字段 → 丢弃
            continue
        tmpl_val = template[key]
        if isinstance(tmpl_val, dict) and isinstance(over_val, dict):
            result[key] = _prune_override_keys(tmpl_val, over_val, depth + 1)
        else:
            result[key] = over_val
    return result


def cleanup_override_against_template(
    template_path: Path,
    user_config_path: Path,
) -> bool:
    """清理用户 override 中模板已删除的废弃字段。

    稀疏 override 模式：override 只应包含用户主动修改的字段和 version。
    当模板删除了某个字段时，override 中对应的残留字段也应被清理。
    """
    if not user_config_path.exists():
        return False
    if not template_path.exists():
        return False

    template_data = load_yaml_dict(template_path)
    override_data = load_yaml_dict(user_config_path)

    if not isinstance(template_data, dict):
        return False
    if not isinstance(override_data, dict):
        return False

    pruned = _prune_override_keys(template_data, override_data)

    # version 字段始终保留（即使模板里也有，它是结构性元数据）
    if "version" in override_data and "version" not in pruned:
        pruned["version"] = override_data["version"]

    if pruned != override_data:
        dump_yaml_round_trip(user_config_path, pruned)
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
    data = load_yaml_round_trip(_current_config_yaml_path())
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
    dump_yaml_round_trip(_current_config_yaml_path(), data)


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
    data = _load_yaml_round_trip(_current_config_yaml_path())
    if "sandbox" not in data or not isinstance(data.get("sandbox"), dict):
        data["sandbox"] = {}
    data["sandbox"]["startup_mode"] = normalized
    _dump_yaml_round_trip(_current_config_yaml_path(), data)
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
    data = _load_yaml_round_trip(_current_config_yaml_path())
    if "sandbox" not in data or not isinstance(data.get("sandbox"), dict):
        data["sandbox"] = {}
    data["sandbox"]["policy_file"] = text
    _dump_yaml_round_trip(_current_config_yaml_path(), data)
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

    data = _load_yaml_round_trip(_current_config_yaml_path())
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

    _dump_yaml_round_trip(_current_config_yaml_path(), data)
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
    data = _load_yaml_round_trip(_current_config_yaml_path())
    if "sandbox" not in data or not isinstance(data.get("sandbox"), dict):
        data["sandbox"] = {}
    data["sandbox"]["preserve_file_sharing_mode"] = normalized
    _dump_yaml_round_trip(_current_config_yaml_path(), data)
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

    data = _load_yaml_round_trip(_current_config_yaml_path())
    if "sandbox" not in data or not isinstance(data.get("sandbox"), dict):
        data["sandbox"] = {}
    sandbox_block = data["sandbox"]
    # 写入扁平 runtime 字段, 每次 update 都把全集刷一遍, 保证 yaml 形状稳定。
    for key in _SANDBOX_RUNTIME_KEYS:
        sandbox_block[key] = merged[key]
    _dump_yaml_round_trip(_current_config_yaml_path(), data)
    return merged


def _parse_comma_separated_string(raw: str) -> list[str]:
    """Split a comma/semicolon-separated string into stripped non-empty items."""
    if not raw:
        return []
    return [item.strip() for item in raw.replace(";", ",").split(",") if item.strip()]


def resolve_string_or_list_config(value: Any) -> list[str]:
    """Resolve a config field that can be either a YAML list or an env string.

    Handles three cases:
      - list   → copy as ``list[str]``; non-string items are rejected
      - str    → parse comma/semicolon-separated into ``list[str]``
      - None   → empty list

    Other values are rejected instead of being treated as an empty list. This
    prevents malformed security configuration from silently enabling tools.

    Used for ``react.disabled_tools`` which accepts both
    ``["search_skill", "install_skill"]`` (YAML list) and
    ``${DISABLED_TOOLS-search_skill,install_skill}`` (env string).
    """
    if isinstance(value, list):
        if any(not isinstance(item, str) for item in value):
            raise TypeError("config list items must be strings")
        return [item.strip() for item in value if item.strip()]
    if isinstance(value, str):
        return _parse_comma_separated_string(value)
    if value is None:
        return []
    raise TypeError(
        f"config value must be a string, list of strings, or None; "
        f"got {type(value).__name__}"
    )
