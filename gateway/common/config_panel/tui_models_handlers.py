# Gateway 侧副本，源文件 jiuwenswarm/common/config_panel/tui_models_handlers.py；改动请同步两侧。
# 本副本仅保留 gateway 实际引用的符号；同步母仓时请勿整体覆盖。
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
"""TUI 侧 config 域 handler 下沉实现（自 gateway tui_connect 迁出）。

gateway 拆分：ConfigAdapter（AgentServer 进程）复用这些成熟实现时不再 import
``gateway.channel_manager.tui.tui_connect``。gateway 侧 ``register_cli_handlers``
通过本模块间接消费同一实现，保持单一实现源。

与 Web 侧（``models_handlers`` / ``config_set_handlers``）契约不一致，禁止合并：
- ``config.set``：env 键集不同（无 endpoint_profile/vendor_key 等）、YAML setter 表
  含 Auto-Harness 专属项、回包无 canonical_config；
- ``config.validate_model``：api_key 对所有 provider 必填、temperature=0、返回
  ``response`` 内容而非 ``ok``；
- ``models.list``：字段集不同（无 origin_index/is_free/is_agentos）；
- ``command.model``：TUI 专属命令（add/update/delete/switch/list）。

E2A 代理分叉（单用户共享目录本地执行 / AgentOS 多用户代理到目标 AgentServer）
由 gateway 侧 ``_register_config_proxy`` / ``_command_model`` 包装器保留；
``force_local_config`` 语义（ConfigAdapter 场景禁止二次代理）同样由 gateway
包装器处理。本模块仅提供本地 handler。

``send_request`` 注入：gateway 进程传入带超时包装的请求发送器
（``_send_tui_agent_request``）；未注入时直接 ``agent_client.send_request(env)``
（ConfigAdapter / AgentServer 上下文）。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import sys
import time
from typing import Any

import yaml
from ruamel.yaml.scalarstring import DoubleQuotedScalarString
from openjiuwen.rsi.harness_rsi.auto_harness.schema import load_auto_harness_config

from gateway.common.config import (
    get_config,
    get_config_raw,
    get_default_models,
    get_model_names,
    resolve_env_vars,
    update_config,
)
from gateway.common.reasoning_config import (
    resolve_endpoint_profile_override,
    validate_reasoning_level_for_model,
)
from gateway.common.paths import get_user_workspace_dir

logger = logging.getLogger(__name__)


class ModelOpError(Exception):
    """模型操作校验失败：在 update_config 事务内抛出，事务外转成 RPC 错误响应。"""


def _resolve_agent_client(agent_client: Any) -> Any:
    if isinstance(agent_client, dict):
        return agent_client.get("value")
    return agent_client


async def _default_send_request(client: Any, env: Any, *, label: str) -> Any:
    del label  # 直发场景（ConfigAdapter）无超时包装
    return await client.send_request(env)


def _get_crypto_provider() -> Any:
    """经 common 钩子取 crypto provider（未注册/未就绪返回 None）。

    gateway 进程由 ``extensions.registry`` 在 create_instance 时安装桥接；
    AgentServer 进程无 crypto 扩展时 api_key 以明文落库（与原实现一致）。
    """
    try:
        from gateway.extensions.base_crypto import get_crypto_provider

        return get_crypto_provider()
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
# Auto-Harness 配置（~/.jiuwenswarm/auto-harness/config.yaml，TUI config 域专属）
# --------------------------------------------------------------------------- #
_DEFAULT_REPO_URL = "https://gitcode.com/openJiuwen/agent-core.git"
_AUTO_HARNESS_CONFIG_DIR = get_user_workspace_dir() / "auto-harness"
_AUTO_HARNESS_CONFIG_FILE = _AUTO_HARNESS_CONFIG_DIR / "config.yaml"
_AUTO_HARNESS_LOCAL_REPO = _AUTO_HARNESS_CONFIG_DIR / "repo" / "openJiuwen--agent-core"

# Default values for ci_gate config
_DEFAULT_CI_GATE_PYTHON_EXECUTABLE = sys.executable
_DEFAULT_CI_GATE_INSTALL_COMMAND = "uv sync --active --group dev --extra cli"


def get_auto_harness_config() -> dict[str, Any]:
    """Load auto-harness config.yaml with auto-fill for ci_gate defaults."""
    config: dict[str, Any] = {}

    if not _AUTO_HARNESS_CONFIG_FILE.exists():
        load_auto_harness_config(str(_AUTO_HARNESS_CONFIG_FILE))

    try:
        config = yaml.safe_load(_AUTO_HARNESS_CONFIG_FILE.read_text(encoding="utf-8")) or {}
    except Exception as e:
        logger.warning("[auto-harness config] Failed to load: %s", e)
        config = {}

    # Auto-fill ci_gate defaults if missing
    ci_gate = config.get("ci_gate") or {}
    git_config = config.get("git") or {}
    needs_save = False

    git_remote = git_config.get("remote")
    if not git_remote:
        git_config["remote"] = "autoharness"

    # Ensure local_repo is a string (not Path object which causes YAML serialization issues)
    local_repo = config.get("local_repo")
    repo_url = config.get("repo_url")
    if not local_repo:
        config["local_repo"] = str(_AUTO_HARNESS_LOCAL_REPO)
        needs_save = True

    if not repo_url:
        config["repo_url"] = str(_DEFAULT_REPO_URL)
        needs_save = True

    elif hasattr(local_repo, "__fspath__"):  # Path-like object
        config["local_repo"] = str(local_repo)
        needs_save = True

    if not ci_gate.get("python_executable"):
        ci_gate["python_executable"] = str(_DEFAULT_CI_GATE_PYTHON_EXECUTABLE)
        needs_save = True

    if not ci_gate.get("install_command"):
        ci_gate["install_command"] = _DEFAULT_CI_GATE_INSTALL_COMMAND
        needs_save = True

    budget = config.get("budget", {})
    max_tasks_per_session = budget.get("max_tasks_per_session", 5)
    if max_tasks_per_session > 5:
        budget["max_tasks_per_session"] = 5
        needs_save = True

    if needs_save:
        config["ci_gate"] = ci_gate
        _save_auto_harness_config(config)
        logger.info("[auto-harness config] Auto-filled ci_gate defaults: python_executable=%s, install_command=%s",
                    ci_gate.get("python_executable"), ci_gate.get("install_command"))

    return config


def _save_auto_harness_config(config: dict[str, Any]) -> None:
    """Save auto-harness config.yaml."""
    _AUTO_HARNESS_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _AUTO_HARNESS_CONFIG_FILE.write_text(
        yaml.dump(config, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8"
    )


def _update_auto_harness_git_user_name(value: str) -> None:
    """Update git.user_name, fork_owner, and gitcode.username in auto-harness config.
    - git.user_name: 用于 git commit
    - git.fork_owner: 用于创建 PR
    - gitcode.username: GitCode 登录用户名
    """
    config = get_auto_harness_config()
    if "git" not in config:
        config["git"] = {}
    config["git"]["user_name"] = value
    config["git"]["fork_owner"] = value  # 合并：用户名同时作为 fork_owner
    if "gitcode" not in config:
        config["gitcode"] = {}
    config["gitcode"]["username"] = value  # 合并：用户名同时作为 gitcode.username
    _save_auto_harness_config(config)


def _update_auto_harness_git_user_email(value: str) -> None:
    """Update git.user_email in auto-harness config."""
    config = get_auto_harness_config()
    if "git" not in config:
        config["git"] = {}
    config["git"]["user_email"] = value
    _save_auto_harness_config(config)


def _update_auto_harness_gitcode_access_token(value: str) -> None:
    """Update gitcode.access_token in auto-harness config."""
    config = get_auto_harness_config()
    if "gitcode" not in config:
        config["gitcode"] = {}
    config["gitcode"]["access_token"] = value
    _save_auto_harness_config(config)


# --------------------------------------------------------------------------- #
# TUI config.set 键映射与工具
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# handler 实现（channel 语义与 tui_connect 原实现一致）
# --------------------------------------------------------------------------- #


async def command_model_handler(
    channel: Any,
    ws: Any,
    req_id: Any,
    params: Any,
    session_id: Any,
    *,
    agent_client: Any = None,
    on_config_saved: Any = None,
    send_request: Any = None,
    user_id: Any = None,
) -> None:
    """TUI ``command.model`` 本地执行（add/update/delete/switch/list）。

    E2A 代理分叉与 ``force_local_config`` 语义由 gateway 侧包装器处理；
    本 handler 假定已在正确的用户目录上下文中执行。
    """
    from openjiuwen_protocol.e2a.gateway_normalize import e2a_from_agent_fields
    from openjiuwen_protocol.message import ReqMethod

    if not isinstance(params, dict):
        params = {}
    action = params.get("action")
    model_name = params.get("model")
    model_index = params.get("index")

    real_client = _resolve_agent_client(agent_client)
    if real_client is None:
        await channel.send_response(
            ws, req_id, ok=False, error="agent client not available"
        )
        return

    async def _reload_model_config_background(config_payload: dict[str, Any], label: str) -> None:
        _reload_env = e2a_from_agent_fields(
            request_id=req_id,
            channel_id="cli",
            session_id=session_id,
            req_method=ReqMethod.AGENT_RELOAD_CONFIG,
            params={"config": config_payload, "env": {}},
            is_stream=False,
            timestamp=time.time(),
            user_id=user_id,
        )
        sender = send_request or _default_send_request
        try:
            await sender(real_client, _reload_env, label=f"command.model.{label}")
        except Exception as _e_reload:
            logger.warning("[cli command.model] %s AGENT_RELOAD_CONFIG failed: %s", label, _e_reload)
        if on_config_saved:
            try:
                _cb = on_config_saved(set(), env_updates={}, config_payload=config_payload)
                if inspect.isawaitable(_cb):
                    await _cb
            except Exception as _e_saved:
                logger.warning("[cli command.model] %s on_config_saved failed: %s", label, _e_saved)

    if action == "add_model":
        target = str(params.get("target", "")).strip()
        configs = params.get("config", {})
        if not target:
            await channel.send_response(
                ws, req_id, ok=False, error="Target model name (target) is required"
            )
            return
        client_cfg = {}
        model_config_obj = configs.get("model_config_obj", {})
        if not isinstance(model_config_obj, dict):
            model_config_obj = {}
        key_map = {
            "model": "model_name",
            "model_name": "model_name",
            "provider": "client_provider",
            "model_provider": "client_provider",
            "client_provider": "client_provider",
            "reasoning_level": "reasoning_level",
            "api_key": "api_key",
            "key": "api_key",
            "api_base": "api_base",
            "url": "api_base",
            "base_url": "api_base",
            "timeout": "timeout",
            "verify_ssl": "verify_ssl",
            "ssl_cert": "ssl_cert",
            "alias": "alias",
        }
        # target 可能是 "model=gpt-5" 形式（前端把第一个 key=value 当作 name 参数解析）
        if "=" in target:
            _eq = target.index("=")
            _k, _v = target[:_eq].strip().lower(), target[_eq + 1:].strip()
            _mapped_target_key = key_map.get(_k, _k)
            if _mapped_target_key == "reasoning_level":
                if _v:
                    model_config_obj["reasoning_level"] = _v
            else:
                client_cfg[_mapped_target_key] = _v
            if _k in ("model", "model_name"):
                target = _v
        for k, v in configs.items():
            mapped_k = key_map.get(str(k).lower(), str(k))
            if mapped_k == "model_config_obj":
                continue
            if mapped_k == "reasoning_level":
                if str(v).strip():
                    model_config_obj["reasoning_level"] = str(v).strip()
                else:
                    model_config_obj.pop("reasoning_level", None)
                continue
            client_cfg[mapped_k] = v
        if "verify_ssl" not in client_cfg:
            client_cfg["verify_ssl"] = False
        if "timeout" not in client_cfg:
            client_cfg["timeout"] = 1800
        # target 作为 model_name 的回退：若未通过 model= 参数指定，则以 target 为准
        if not client_cfg.get("model_name"):
            client_cfg["model_name"] = target
        effective_name = client_cfg["model_name"]

        # 与 web 端 models.replace_all 一致：按 core 能力表校验具体模型
        # 支持的思考档位，并落库规范化后的值。
        try:
            _normalized_reasoning = validate_reasoning_level_for_model(
                raw_level=model_config_obj.get("reasoning_level"),
                model_name=resolve_env_vars(str(effective_name)),
                model_provider=resolve_env_vars(str(client_cfg.get("client_provider", ""))),
                api_base=resolve_env_vars(str(client_cfg.get("api_base", ""))),
                endpoint_profile=client_cfg.get("endpoint_profile"),
            )
        except ValueError as _reasoning_err:
            await channel.send_response(ws, req_id, ok=False, error=str(_reasoning_err))
            return
        if _normalized_reasoning:
            # 必须带引号落库：裸 on/off 会被 YAML 1.1 加载器读成布尔。
            model_config_obj["reasoning_level"] = DoubleQuotedScalarString(_normalized_reasoning)
        else:
            model_config_obj.pop("reasoning_level", None)
        # 与 web 端一致：已知自建网关按 api_base host 推断 endpoint_profile
        # 并落库（如 vLLM 风格端点需走 core 的 "vllm" 方言才能关思考）。
        if not client_cfg.get("endpoint_profile"):
            _inferred_profile = resolve_endpoint_profile_override(
                resolve_env_vars(str(client_cfg.get("api_base", "")))
            )
            if _inferred_profile:
                client_cfg["endpoint_profile"] = _inferred_profile

        # alias 为顶层字段，从 client_cfg 提取；提前算最终值，
        # 确保唯一性校验基于实际存储值
        entry_alias = client_cfg.pop("alias", None)
        effective_alias = str(entry_alias).strip() if entry_alias else ""

        new_entry = {
            "model_client_config": client_cfg,
            "model_config_obj": model_config_obj,
        }
        # alias 带双引号写出，避免 yes/no/on/off 被 YAML 1.1 解析为布尔
        new_entry["alias"] = DoubleQuotedScalarString(effective_alias) if effective_alias else ""
        try:
            # 单事务读-校验-改：避免 ensure+update 两步间的 TOCTOU 窗口
            def _add_mutate(data):
                models = data.get("models")
                if not isinstance(models, dict):
                    models = {}
                    data["models"] = models
                _raw_defs = models.get("defaults")
                if not (isinstance(_raw_defs, list) and _raw_defs):
                    _raw_defs = [{
                        "model_client_config": {
                            "api_base": "${API_BASE}",
                            "api_key": "${API_KEY}",
                            "model_name": "${MODEL_NAME}",
                            "client_provider": "${MODEL_PROVIDER}",
                        },
                        "model_config_obj": {},
                        "is_default": True,
                    }]
                    models["defaults"] = _raw_defs
                    models.pop("default", None)
                _effective_api_base = resolve_env_vars(str(client_cfg.get("api_base", "")))
                _effective_api_key = resolve_env_vars(str(client_cfg.get("api_key", "")))
                for _e in _raw_defs:
                    if not isinstance(_e, dict):
                        continue
                    _emn = resolve_env_vars(str((_e.get("model_client_config") or {}).get("model_name", "")))
                    _eab = resolve_env_vars(str((_e.get("model_client_config") or {}).get("api_base", "")))
                    _eak = resolve_env_vars(str((_e.get("model_client_config") or {}).get("api_key", "")))
                    if _emn == effective_name and _eab == _effective_api_base and _eak == _effective_api_key:
                        raise ModelOpError(
                            f"Model '{effective_name}' with the same api_base and api_key already exists"
                        )
                _missing = []
                for field, display in {
                    "api_key": "api_key",
                    "api_base": "api_base",
                    "model_name": "model_name",
                    "client_provider": "model_provider",
                }.items():
                    if not resolve_env_vars(str(client_cfg.get(field, ""))):
                        _missing.append(display)
                if _missing:
                    raise ModelOpError(
                        f"Failed to add model '{effective_name}'. "
                        f"Required fields missing: {', '.join(_missing)}. "
                        f"Usage: /model add <name> "
                        f"api_base=xxx api_key=xxx "
                        f"model=<name> model_provider=<provider>"
                    )
                if effective_alias:
                    for _e in _raw_defs:
                        if not isinstance(_e, dict):
                            continue
                        _emn = resolve_env_vars(str((_e.get("model_client_config") or {}).get("model_name", "")))
                        _ea = resolve_env_vars(str(_e.get("alias", "")))
                        if _ea == effective_alias:
                            raise ModelOpError(f"Alias '{effective_alias}' is already used by model '{_emn}'")
                        if _emn == effective_alias:
                            raise ModelOpError(f"Alias '{effective_alias}' conflicts with model name '{_emn}'")
                _raw_defs.append(new_entry)
                return data
            update_config(_add_mutate)
            logger.info(
                "[cli command.model] 新增模型: name=%s, "
                "client_cfg=%s, model_config_obj=%s",
                effective_name, client_cfg, model_config_obj,
            )
        except ModelOpError as _op_err:
            await channel.send_response(ws, req_id, ok=False, error=str(_op_err))
            return
        except Exception as e:
            await channel.send_response(ws, req_id, ok=False, error=str(e))
            return
        _config_payload = get_config()
        await _reload_model_config_background(_config_payload, "model.add")
        await channel.send_response(
            ws, req_id, ok=True,
            payload={"type": "model_added", "name": target},
        )
        return

    if action == "update_model":
        configs = params.get("config", {})
        if not isinstance(configs, dict):
            await channel.send_response(ws, req_id, ok=False, error="config must be object")
            return
        try:
            _idx = int(model_index)
        except (ValueError, TypeError):
            await channel.send_response(ws, req_id, ok=False, error="index is required")
            return
        try:
            _update_result: dict = {}

            def _update_mutate(data):
                models = data.get("models")
                if not isinstance(models, dict):
                    models = {}
                    data["models"] = models
                _raw_defs = models.get("defaults")
                if not (isinstance(_raw_defs, list) and _raw_defs):
                    raise ModelOpError("model index not found")
                if _idx < 0 or _idx >= len(_raw_defs) or not isinstance(_raw_defs[_idx], dict):
                    raise ModelOpError("model index not found")
                _entry = _raw_defs[_idx]
                _client_cfg = _entry.get("model_client_config")
                if not isinstance(_client_cfg, dict):
                    _client_cfg = {}
                    _entry["model_client_config"] = _client_cfg
                key_map = {
                    "model": "model_name", "model_name": "model_name",
                    "provider": "client_provider", "model_provider": "client_provider",
                    "client_provider": "client_provider", "reasoning_level": "reasoning_level",
                    "api_key": "api_key", "key": "api_key", "api_base": "api_base",
                    "url": "api_base", "base_url": "api_base", "timeout": "timeout",
                    "verify_ssl": "verify_ssl", "ssl_cert": "ssl_cert", "alias": "alias",
                }
                _model_cfg_obj = _entry.get("model_config_obj")
                if not isinstance(_model_cfg_obj, dict):
                    _model_cfg_obj = {}
                    _entry["model_config_obj"] = _model_cfg_obj
                for k, v in configs.items():
                    mapped_k = key_map.get(str(k).lower(), str(k))
                    if mapped_k == "alias":
                        _alias_val = str(v).strip()
                        _entry["alias"] = (
                            DoubleQuotedScalarString(_alias_val) if _alias_val else ""
                        )
                    elif mapped_k == "reasoning_level":
                        _rl = str(v).strip()
                        if _rl:
                            _model_cfg_obj["reasoning_level"] = _rl
                        else:
                            _model_cfg_obj.pop("reasoning_level", None)
                    elif mapped_k == "model_config_obj":
                        continue
                    else:
                        _client_cfg[mapped_k] = v
                # 与 web 端 models.replace_all 一致：按 core 能力表校验具体模型
                # 支持的思考档位，并落库规范化后的值。
                try:
                    _normalized_reasoning = validate_reasoning_level_for_model(
                        raw_level=_model_cfg_obj.get("reasoning_level"),
                        model_name=resolve_env_vars(str(_client_cfg.get("model_name", ""))),
                        model_provider=resolve_env_vars(str(_client_cfg.get("client_provider", ""))),
                        api_base=resolve_env_vars(str(_client_cfg.get("api_base", ""))),
                        endpoint_profile=_client_cfg.get("endpoint_profile"),
                    )
                except ValueError as _reasoning_err:
                    raise ModelOpError(str(_reasoning_err)) from _reasoning_err
                if _normalized_reasoning:
                    # 必须带引号落库：裸 on/off 会被 YAML 1.1 加载器读成布尔。
                    _model_cfg_obj["reasoning_level"] = DoubleQuotedScalarString(_normalized_reasoning)
                else:
                    _model_cfg_obj.pop("reasoning_level", None)
                # 与 web 端一致：已知自建网关按 api_base host 推断
                # endpoint_profile 并落库。
                if not _client_cfg.get("endpoint_profile"):
                    _inferred_profile = resolve_endpoint_profile_override(
                        resolve_env_vars(str(_client_cfg.get("api_base", "")))
                    )
                    if _inferred_profile:
                        _client_cfg["endpoint_profile"] = _inferred_profile
                if "verify_ssl" not in _client_cfg:
                    _client_cfg["verify_ssl"] = False
                if "timeout" not in _client_cfg:
                    _client_cfg["timeout"] = 1800
                _missing_fields = []
                for _req_field, _display in [
                    ("api_key", "api_key"), ("api_base", "api_base"),
                    ("model_name", "model_name"), ("client_provider", "model_provider"),
                ]:
                    if not resolve_env_vars(str(_client_cfg.get(_req_field, ""))):
                        _missing_fields.append(_display)
                if _missing_fields:
                    raise ModelOpError(f"Model missing required config: {', '.join(_missing_fields)}")
                _effective_alias = resolve_env_vars(str(_entry.get("alias", ""))) if _entry.get("alias") else ""
                if _effective_alias:
                    for _other_idx, _other in enumerate(_raw_defs):
                        if _other_idx == _idx or not isinstance(_other, dict):
                            continue
                        _other_mcc = _other.get("model_client_config") or {}
                        _other_mn = resolve_env_vars(str(_other_mcc.get("model_name", "")))
                        _other_alias = resolve_env_vars(str(_other.get("alias", ""))) if _other.get("alias") else ""
                        if _other_alias == _effective_alias:
                            raise ModelOpError(
                                f"Alias '{_effective_alias}' is already used by model '{_other_mn}'"
                            )
                        if _other_mn == _effective_alias:
                            raise ModelOpError(
                                f"Alias '{_effective_alias}' conflicts with model name '{_other_mn}'"
                            )
                # 展示字段从锁内 data 直接取，避免事务后再开锁读取
                _upd_mcc = _entry.get("model_client_config") or {}
                _update_result["updated_name"] = resolve_env_vars(str(_upd_mcc.get("model_name", "")))
                _cur_mcc = (_raw_defs[0].get("model_client_config") or {}) if _raw_defs else {}
                _update_result["current_name"] = resolve_env_vars(str(_cur_mcc.get("model_name", "")))
                return data
            update_config(_update_mutate)
        except ModelOpError as _op_err:
            await channel.send_response(ws, req_id, ok=False, error=str(_op_err))
            return
        except Exception as e:
            await channel.send_response(ws, req_id, ok=False, error=str(e))
            return
        _updated_name = _update_result.get("updated_name", "")
        _current_name = _update_result.get("current_name", "")
        _config_payload = get_config()
        await _reload_model_config_background(_config_payload, "model.update")
        await channel.send_response(ws, req_id, ok=True, payload={
            "type": "model_updated",
            "name": _updated_name,
            "index": _idx,
            "current": _current_name,
        })
        return

    if action == "delete_model":
        # 前端传 model（model_name/alias）+ index；两者均可选。
        # 优先按 model 稳定标识（model_name 或 alias）匹配定位，index 仅作辅助与兜底。
        # 早期实现只按 index pop，若确认页停留期间 defaults 被切换操作重排，
        # 同一 index 会指向漂移后的另一条目，导致"删错模型"。
        _del_target_name = str(model_name or "").strip()
        try:
            _idx = int(model_index) if model_index is not None else -1
        except (ValueError, TypeError):
            _idx = -1
        _removed_holder: dict = {}
        try:
            def _delete_mutate(data):
                models = data.get("models")
                if not isinstance(models, dict):
                    models = {}
                    data["models"] = models
                _raw_defs = models.get("defaults")
                if not (isinstance(_raw_defs, list) and _raw_defs):
                    raise ModelOpError("model index not found")
                if len(_raw_defs) <= 1:
                    raise ModelOpError("Cannot delete the last model")
                # 1) 按 model_name/alias 稳定匹配（同名时进一步用 provider+api_base 区分）
                _target_idx = None
                if _del_target_name:
                    _candidates: list[tuple[int, dict]] = []
                    for _i, _e in enumerate(_raw_defs):
                        if not isinstance(_e, dict):
                            continue
                        _mcc = _e.get("model_client_config") or {}
                        _mn = resolve_env_vars(str(_mcc.get("model_name", "")))
                        _al = resolve_env_vars(str(_e.get("alias", ""))) if _e.get("alias") else ""
                        if _mn == _del_target_name or _al == _del_target_name:
                            _candidates.append((_i, _e))
                    if len(_candidates) == 1:
                        _target_idx = _candidates[0][0]
                    elif len(_candidates) > 1:
                        # 同名多条：用前端传入的 index 在候选中挑选；不在候选则报漂移
                        if _idx >= 0:
                            for _ci, _ce in _candidates:
                                if _ci == _idx:
                                    _target_idx = _ci
                                    break
                        if _target_idx is None:
                            raise ModelOpError(
                                "Multiple models match '%s'; list may have changed, please refresh and retry"
                                % _del_target_name
                            )
                # 2) 退化为纯 index：仅当前端未传 model 时使用，且仍校验越界
                if _target_idx is None and 0 <= _idx < len(_raw_defs) and isinstance(_raw_defs[_idx], dict):
                    # 若前端传了 model 但与该 index 当前指向的条目不一致，说明列表已漂移，
                    # 拒绝静默删错：要求刷新重试。
                    if _del_target_name:
                        _idx_mcc = _raw_defs[_idx].get("model_client_config") or {}
                        _idx_mn = resolve_env_vars(str(_idx_mcc.get("model_name", "")))
                        _idx_entry = _raw_defs[_idx]
                        _idx_alias_raw = _idx_entry.get("alias", "")
                        _idx_al = (
                            resolve_env_vars(str(_idx_alias_raw))
                            if _idx_alias_raw else ""
                        )
                        if _idx_mn != _del_target_name and _idx_al != _del_target_name:
                            raise ModelOpError(
                                "Model '%s' no longer at index %d; list may have changed, please refresh and retry"
                                % (_del_target_name, _idx)
                            )
                    _target_idx = _idx
                if _target_idx is None:
                    raise ModelOpError("model not found; list may have changed, please refresh and retry")
                _removed_holder["entry"] = _raw_defs.pop(_target_idx)
                # 展示字段从锁内 data 直接取，避免事务后再开锁读取
                _cur_mcc = (_raw_defs[0].get("model_client_config") or {}) if _raw_defs else {}
                _removed_holder["current_name"] = resolve_env_vars(str(_cur_mcc.get("model_name", "")))
                return data
            update_config(_delete_mutate)
        except ModelOpError as _op_err:
            await channel.send_response(ws, req_id, ok=False, error=str(_op_err))
            return
        except Exception as e:
            await channel.send_response(ws, req_id, ok=False, error=str(e))
            return
        _removed = _removed_holder.get("entry") or {}
        _removed_name = resolve_env_vars(str((_removed.get("model_client_config") or {}).get("model_name", "")))
        _current_name = _removed_holder.get("current_name", "")
        _config_payload = get_config()
        await _reload_model_config_background(_config_payload, "model.delete")
        await channel.send_response(ws, req_id, ok=True, payload={
            "type": "model_deleted",
            "name": _removed_name,
            "current": _current_name,
        })
        return

    if not model_name or not str(model_name).strip():
        names = get_model_names()
        logger.info(
            "[cli command.model] 列出模型: names=%s, current=%s",
            names,
            os.getenv("MODEL_NAME", "unknown"),
        )
        # 列出模型全部数据均可从本地 config.yaml 获取，
        # 无需等待 AgentServer 响应（其返回的 current/available 会被本地值覆盖）。
        # 若 await send_request() 阻塞 >30s，会导致 TUI WS 超时且后续请求排队，
        # 故直接以本地数据构建 payload 立即回包。
        payload: dict = {}
        _raw = get_config_raw()
        _raw_models = _raw.get("models") if isinstance(_raw, dict) else {}
        _raw_models = _raw_models if isinstance(_raw_models, dict) else {}
        _raw_defs = _raw_models.get("defaults")
        _defs = _raw_defs if isinstance(_raw_defs, list) else []
        _available_models = list(names)
        _first_default = None
        for entry in _defs:
            if isinstance(entry, dict):
                _first_default = entry
                break

        # _model_meta 必须在 if/else 之前定义，两个分支共用；
        # 否则 models.defaults 为空、仅配 agentos 时无法构造模型列表。
        def _model_meta(i: int, e: dict, *, is_agentos: bool = False) -> dict:
            mcc = e.get("model_client_config") or {}
            mco = e.get("model_config_obj") or {}
            _alias = e.get("alias", "")
            _resolved_alias = resolve_env_vars(str(_alias)) if _alias else ""
            _model_name = resolve_env_vars(str(mcc.get("model_name", "")))
            _api_key = resolve_env_vars(str(mcc.get("api_key", "")))
            # agentos 条目 index 用 "a" 前缀编码，与 defaults 的纯数字 index 区分，
            # 避免切换时按 index 命中错位。is_current 仅 defaults 首位为 true
            # （agentos 永不抢默认，不会是 current）。
            return {
                "index": f"a{i}" if is_agentos else i,
                "name": _resolved_alias or _model_name,
                "alias": _resolved_alias,
                "model_name": _model_name,
                "model_provider": resolve_env_vars(str(mcc.get("client_provider", ""))),
                "api_base": resolve_env_vars(str(mcc.get("api_base", ""))),
                "reasoning_level": resolve_env_vars(str(mco.get("reasoning_level", ""))),
                # 同名模型冲突时用于区分：仅展示末4位，避免泄露过多 key 信息
                "api_key_suffix": _api_key[-4:] if _api_key else "",
                "is_current": (
                    not is_agentos
                    and _first_default is e
                ),
                "is_agentos": is_agentos,
            }

        if _first_default is not None:
            _first_name = resolve_env_vars(
                str((_first_default.get("model_client_config") or {}).get("model_name", ""))
            )
            _first_alias = (
                resolve_env_vars(str(_first_default.get("alias", "")))
                if _first_default.get("alias")
                else ""
            )
            payload["current"] = _first_alias or _first_name or os.getenv("MODEL_NAME", "unknown")
            payload["current_model_name"] = _first_name or os.getenv("MODEL_NAME", "unknown")
        else:
            # models.defaults 不存在/为空：仍需展示 agentos 备份模型（若有），
            # 否则 .env 全空且只有 agentos 时列表为空，用户无法切换。
            payload["current"] = os.getenv("MODEL_NAME", "unknown")
            payload["current_model_name"] = os.getenv("MODEL_NAME", "unknown")
        _models_list = []
        for i, entry in enumerate(_defs):
            if isinstance(entry, dict):
                _models_list.append(_model_meta(i, entry))

        # 追加 agentos 备份模型：与 defaults 并列展示、同等可选可切换，
        # 但 is_current 恒 False、is_agentos True 供前端区分渲染与切换路径。
        _agentos_raw = _raw_models.get("agentos")
        _agentos_list = _agentos_raw if isinstance(_agentos_raw, list) else []
        for _ai, _ab in enumerate(_agentos_list):
            if not isinstance(_ab, dict):
                continue
            _ab_mcc = _ab.get("model_client_config")
            if not (isinstance(_ab_mcc, dict) and _ab_mcc.get("model_name")):
                continue
            _agentos_meta = _model_meta(_ai, _ab, is_agentos=True)
            _models_list.append(_agentos_meta)
            if (
                _agentos_meta["name"]
                and _agentos_meta["name"] not in _available_models
            ):
                _available_models.append(_agentos_meta["name"])
        payload["available_models"] = _available_models
        payload["models"] = _models_list
        await channel.send_response(ws, req_id, ok=True, payload=payload)
        return

    target = str(model_name).strip()
    logger.info(
        "[cli command.model] 切换模型: target=%s, model_index=%s, params=%s",
        target, model_index, params,
    )
    _switch_result: dict = {}
    # ── agentos 备份模型切换：不改 config、不重排 defaults、不 reload ──
    # agentos 走"请求级 model_name 注入"机制（与 Web 的 ModelSelector 一致），
    # 仅全局回显选中名，后续 chat.send 由前端注入 model_name，AgentServer
    # _resolve_model_for_request 命中 agentos 缓存条目。故此处立即回包，
    # 不触发 AGENT_RELOAD_CONFIG。仅当 target 命中 models.agentos 条目时走此路径。
    _raw_cfg = get_config_raw()
    _agentos_blocks = (_raw_cfg.get("models") or {}).get("agentos")
    _agentos_blocks = _agentos_blocks if isinstance(_agentos_blocks, list) else []
    _agentos_matched_name = ""
    _agentos_matched_provider = ""
    _agentos_matched_global_idx: int | None = None
    logger.info(
        "[cli command.model] agentos 匹配: target=%s, blocks=%d, raw_has_agentos=%s",
        target, len(_agentos_blocks), _agentos_blocks is not None and len(_agentos_blocks) > 0,
    )
    # 遍历合并后的 defaults+agentos 列表（与 AgentServer _build_model_cache_from_defaults
    # 同源、同序），按 global_idx 定位命中的 agentos 条目。回包 current 带
    # ``{model_name}#{global_idx}``，供前端注入 chat.send 的 model_name，
    # 后端 _resolve_model_by_name 据此精确命中 agentos 缓存条目——否则同名时
    # 纯 model_name 会被解析到 defaults 首条（is_default=true），误路由。
    for _gi, _e in enumerate(get_default_models(_raw_cfg)):
        if not isinstance(_e, dict):
            continue
        _e_mco = _e.get("model_config_obj") or {}
        if not (isinstance(_e_mco, dict) and _e_mco.get("_source") == "agentos"):
            continue
        _e_mcc = _e.get("model_client_config") or {}
        if not (isinstance(_e_mcc, dict) and _e_mcc.get("model_name")):
            continue
        _ab_name = resolve_env_vars(str(_e_mcc.get("model_name", "")))
        _ab_alias = resolve_env_vars(str(_e.get("alias", ""))) if _e.get("alias") else ""
        logger.info(
            "[cli command.model] agentos 条目: name=%s alias=%s vs target=%s global_idx=%d",
            _ab_name, _ab_alias, target, _gi,
        )
        if _ab_name == target or (_ab_alias and _ab_alias == target):
            _agentos_matched_name = _ab_name
            _agentos_matched_provider = resolve_env_vars(str(_e_mcc.get("client_provider", "")))
            _agentos_matched_global_idx = _gi
            break
    if _agentos_matched_name and _agentos_matched_global_idx is not None:
        # 注入名带 #global_idx，与 Web 的 model_name#origin_index 契约一致；
        # 后端 _resolve_model_by_name 走 _global_index_to_cache_key 换算精确命中
        # 同名 defaults/agentos 中的指定条目。current 仍为纯名供前端展示，
        # model_key 专供 chat.send 的 model_name 注入。
        _agentos_inject_key = f"{_agentos_matched_name}#{_agentos_matched_global_idx}"
        logger.info(
            "[cli command.model] 切换 agentos 备份模型（请求级注入，不 reload）: %s",
            _agentos_inject_key,
        )
        await channel.send_response(ws, req_id, ok=True, payload={
            "current": _agentos_matched_name,
            "model_key": _agentos_inject_key,
            "provider": _agentos_matched_provider,
            "requested": target,
            "type": "switched_agentos",
            "applied": True,
            "is_agentos": True,
        })
        return
    # ── defaults 模型切换：原逻辑（重排 defaults 首位 + is_default + reload） ──
    try:
        def _switch_mutate(data):
            models = data.get("models")
            if not isinstance(models, dict):
                models = {}
                data["models"] = models
            _raw_defaults = models.get("defaults")
            if not (isinstance(_raw_defaults, list) and _raw_defaults):
                _raw_defaults = [{
                    "model_client_config": {
                        "api_base": "${API_BASE}", "api_key": "${API_KEY}",
                        "model_name": "${MODEL_NAME}", "client_provider": "${MODEL_PROVIDER}",
                    },
                    "model_config_obj": {}, "is_default": True,
                }]
                models["defaults"] = _raw_defaults
                models.pop("default", None)
            _valid_names: set[str] = set()
            _avail_parts: list[str] = []
            for _e in _raw_defaults:
                if not isinstance(_e, dict):
                    continue
                _mn = resolve_env_vars(str((_e.get("model_client_config") or {}).get("model_name", "")))
                _al = resolve_env_vars(str(_e.get("alias", ""))) if _e.get("alias") else ""
                if _mn:
                    _valid_names.add(_mn)
                if _al:
                    _valid_names.add(_al)
                if _al and _mn and _al != _mn:
                    _avail_parts.append(f"{_al} ({_mn})")
                elif _mn:
                    _avail_parts.append(_mn)
            _skip_name_check = model_index is not None
            if not _skip_name_check and target not in _valid_names:
                logger.warning(
                    "[cli command.model] 模型不存在: %s, 可用: %s",
                    target, _avail_parts,
                )
                raise ModelOpError(
                    f"Model '{target}' not found. "
                    f"Available: {', '.join(_avail_parts) or ''}"
                )
            _target_entry = None
            _target_idx = None
            if model_index is not None:
                try:
                    _idx = int(model_index)
                    if 0 <= _idx < len(_raw_defaults) and isinstance(_raw_defaults[_idx], dict):
                        _target_entry = _raw_defaults[_idx]
                        _target_idx = _idx
                except (ValueError, TypeError):
                    pass
            if _target_entry is None:
                for _i, _e in enumerate(_raw_defaults):
                    if not isinstance(_e, dict):
                        continue
                    _ename = resolve_env_vars(str((_e.get("model_client_config") or {}).get("model_name", "")))
                    _ealias = resolve_env_vars(str(_e.get("alias", ""))) if _e.get("alias") else ""
                    if _ename == target or _ealias == target:
                        _target_entry = _e
                        _target_idx = _i
                        break
            if _target_entry is None:
                raise ModelOpError(f"Model '{target}' config not found")
            _target_mcc = _target_entry.get("model_client_config") or {}
            _missing_fields = []
            for _req_field, _display in [
                ("api_key", "api_key"), ("api_base", "api_base"),
                ("model_name", "model_name"), ("client_provider", "client_provider"),
            ]:
                if not resolve_env_vars(str(_target_mcc.get(_req_field, ""))):
                    _missing_fields.append(_display)
            if _missing_fields:
                raise ModelOpError(f"Model '{target}' missing required config: {', '.join(_missing_fields)}")
            _target_model_name_resolved = resolve_env_vars(str(_target_mcc.get("model_name", "")))
            _target_entry["is_default"] = True
            for _i, _e in enumerate(_raw_defaults):
                if _i == _target_idx or not isinstance(_e, dict):
                    continue
                _other_mcc = _e.get("model_client_config") or {}
                _other_name = resolve_env_vars(str(_other_mcc.get("model_name", "")))
                if _other_name == _target_model_name_resolved and _e.get("is_default") is True:
                    _e["is_default"] = False
            _others = [_e for _i, _e in enumerate(_raw_defaults) if _i != _target_idx]
            models["defaults"] = [_target_entry] + _others
            _switch_result["name"] = resolve_env_vars(
                str((_target_entry.get("model_client_config") or {}).get("model_name", target)))
            return data
        update_config(_switch_mutate)
    except ModelOpError as _op_err:
        await channel.send_response(ws, req_id, ok=False, error=str(_op_err))
        return
    except Exception as e:
        await channel.send_response(ws, req_id, ok=False, error=str(e))
        return
    logger.info("[cli command.model] 切换，已更新 models.defaults 首位: %s", target)
    _target_model_name = _switch_result.get("name", target)

    # 先回包再执行 Agent 热重载（与 config.set 保持一致），
    # 避免 WebSocket 长时间无响应、CLI 误以为无反馈 / 超时。
    await channel.send_response(ws, req_id, ok=True, payload={
        "current": _target_model_name,
        "requested": target,
        "type": "switched",
        "applied": True,
    })

    # 后台触发 AgentServer reload + on_config_saved（不阻塞 WS 消息循环）
    _config_payload = get_config()

    async def _model_switch_background():
        _reload_env = e2a_from_agent_fields(
            request_id=req_id,
            channel_id="cli",
            session_id=session_id,
            req_method=ReqMethod.AGENT_RELOAD_CONFIG,
            params={
                "config": _config_payload,
                "env": {},
                "target_channel_id": "tui",
                "target_session_id": session_id,
                "reason": "model_switch",
            },
            is_stream=False,
            timestamp=time.time(),
            user_id=user_id,
        )
        sender = send_request or _default_send_request
        try:
            await sender(real_client, _reload_env, label="command.model.switch")
        except Exception as _e_reload:
            logger.warning("[cli model.switch] AGENT_RELOAD_CONFIG failed: %s", _e_reload)
        if on_config_saved:
            try:
                _cb = on_config_saved(set(), env_updates={}, config_payload=_config_payload)
                if inspect.isawaitable(_cb):
                    await _cb
            except Exception as _e2:
                logger.warning("[cli model.switch] on_config_saved failed: %s", _e2)
        logger.info("[cli command.model] 切换完成: current=%s", _target_model_name)

    asyncio.create_task(_model_switch_background())
