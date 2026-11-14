# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Builder for the jiuwen builtin-memory variant — config-driven.

`memory.builtin.provider: jiuwen` swaps the builtin MemoryRail for a
`JiuwenMemoryRail` + `AgentMemoryMemoryProvider` pair. Both classes live
in site-packages (openjiuwen / jiuwen_memory_adapter) and are imported
lazily — a missing package disables the variant with a warning instead of
breaking startup. Any failure returns None — the main flow is never blocked
(same contract as external_memory_builder).

Two mount modes, picked by `memory.builtin.jiuwen.mode` (env
`JIUWEN_MEMORY_MODE`, default `http`):

- ``http``: provider is a network client to a remote agent-memory HTTP
  service at ``base_url`` (``POST /v1/<verb>``). ``api_key`` /
  ``identity_org`` / ``identity_user`` are http-only.
- ``sdk``: the agent-memory kernel is assembled **in-process**. Kernel config
  priority: (1) the two-level namespace dict inlined under
    ``memory.builtin.jiuwen.sdk`` in config.yaml (credential fields + markdown
    root are injected here, then passed as ``config_dict``); (2) if absent, the
  provider's **built-in agent-scenario default** (F09: document 真源 + shadow
  index + agent engine + agent_llm extractor — the serve-doc config hardcoded
  in the adapter, 2026-10-17). Either way the provider deep-merges the dict
    onto its built-in default via ``_merge_config_over_default()``. An eligible
    Web turn supplies its media settings and workspace binding as SDK globals.

Config shape (config.yaml)::

    memory:
      engine: builtin                  # 内置路径放行（external 路径不再挂）
      builtin:
        provider: jiuwen               # '' / 缺省 = 原生 MemoryRail
        jiuwen:
          mode: http                   # http | sdk（缺省 http；env JIUWEN_MEMORY_MODE）
          base_url: http://127.0.0.1:8137   # http 模式：AgentMemory HTTP 服务
          api_key: ""                       # http 模式可选 Bearer
          user_id: "__default__"            # 数据隔离标识
          scope_id: "__default__"
          session_id: ""                    # 空 = 跨会话共享（AgentMemory 契约）；显式传值按会话隔离
          agent_id: "jiuwenswarm"           # AgentMemory scope 的 agent 轴
          space: ""                         # 只认显式传入
          identity_org: "local"             # http 模式认证身份 org
          identity_user: "developer"        # http 模式认证身份 user
          session_end_extract: false        # uninit 时是否触发批量 EXTRACT（infer 模式下冗余）

`modes.agent.memory.enabled: true` 仍是总开关（is_memory_enabled），
`memory.mode` 必须为 `local`（_handle_memory_rail_by_config 的门禁）。
"""

import hashlib
import json
import logging
import os
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Optional

from jiuwenswarm.agents.harness.common.tools.multimodal_config import (
    get_enabled_multimodal_model_config,
)

from .config import _load_config, get_embed_config

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "http://127.0.0.1:8137"
_DEFAULT_USER = "__default__"
_DEFAULT_SCOPE = "__default__"


def get_builtin_memory_provider(config: Optional[Dict[str, Any]] = None) -> str:
    """Return `memory.builtin.provider` ('' when unset — native MemoryRail)."""
    cfg = config if config is not None else _load_config()
    mem = (cfg or {}).get("memory", {}) if isinstance(cfg, dict) else {}
    builtin = mem.get("builtin", {}) if isinstance(mem, dict) else {}
    provider = builtin.get("provider") if isinstance(builtin, dict) else None
    return str(provider or "").strip()


def is_jiuwen_builtin_memory(config: Optional[Dict[str, Any]] = None) -> bool:
    """True iff the builtin memory path should mount the jiuwen variant."""
    return get_builtin_memory_provider(config) == "jiuwen"


def get_jiuwen_builtin_config(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return the `memory.builtin.jiuwen` section ({} when absent)."""
    cfg = config if config is not None else _load_config()
    mem = (cfg or {}).get("memory", {}) if isinstance(cfg, dict) else {}
    builtin = mem.get("builtin", {}) if isinstance(mem, dict) else {}
    jiuwen = builtin.get("jiuwen", {}) if isinstance(builtin, dict) else {}
    return jiuwen if isinstance(jiuwen, dict) else {}


def get_jiuwen_builtin_mode(config: Optional[Dict[str, Any]] = None) -> str:
    """Return `memory.builtin.jiuwen.mode` — 'http' (default) or 'sdk'.

    `JIUWEN_MEMORY_MODE` env var overrides an unset config value (same
    variable name as the external path's mode switch — same semantics).
    Unknown values degrade to 'http' with a warning.
    """
    j = get_jiuwen_builtin_config(config)
    mode = str(j.get("mode") or os.environ.get("JIUWEN_MEMORY_MODE", "") or "").strip().lower()
    if mode in ("http", "sdk"):
        return mode
    if mode:
        logger.warning("[JiuwenRailBuilder] unknown jiuwen mode %r, falling back to http", mode)
    return "http"


def _custom_sdk_config_dict(j: Dict[str, Any]) -> Dict[str, Any]:
    """Return the full two-level namespace dict inlined in config.yaml ({} when unset).

    ``memory.builtin.jiuwen.sdk`` holds the complete agent-memory assembly config
    (globals + per-namespace instance maps, same shape as config.serve-doc.yml).
    Returns a shallow copy without the legacy ``config_path`` key.
    """
    sdk = j.get("sdk") if isinstance(j.get("sdk"), dict) else {}
    return {k: v for k, v in sdk.items() if k != "config_path"}


def _memory_scope(config: dict[str, Any], agent_name: str | None) -> dict[str, str]:
    session_id = config.get("session_id")
    return {
        "org": str(config.get("scope_id") or _DEFAULT_SCOPE),
        "space": str(config.get("space") or ""),
        "user": str(config.get("user_id") or _DEFAULT_USER),
        "agent": str(agent_name or config.get("agent_id") or "jiuwenswarm"),
        "session": str(session_id) if session_id is not None else "",
    }


def get_jiuwen_sdk_media_globals(
    config: dict[str, Any],
    runtime_config: dict[str, Any],
    *,
    workspace: str | None,
    agent_name: str | None,
) -> dict[str, Any]:
    """Forward Web models and bind a workspace when the host grants access."""
    video = get_enabled_multimodal_model_config(runtime_config, "video")
    audio = get_enabled_multimodal_model_config(runtime_config, "audio")
    if not video and not audio:
        return {}

    jiuwen = get_jiuwen_builtin_config(config)
    sdk_globals = _custom_sdk_config_dict(jiuwen).get("globals")
    configured_roots = sdk_globals.get("local_media_roots") if isinstance(sdk_globals, dict) else None
    globals_config: dict[str, Any] = {}
    if workspace and not configured_roots and not os.getenv("JIUWEN_MEMORY_VIDEO_ROOTS", "").strip():
        try:
            root = Path(workspace).expanduser()
            if root.is_absolute():
                root = root.resolve(strict=True)
                if root != root.parent and not str(root).startswith(("//", "\\\\")) and root.is_dir():
                    globals_config["local_media_roots"] = [
                        {"scope": _memory_scope(jiuwen, agent_name), "root": str(root)},
                    ]
        except (OSError, ValueError, RuntimeError):
            logger.debug("[JiuwenRailBuilder] workspace unavailable for media import")
    for prefix, model in (("video", video), ("asr", audio)):
        if not model:
            continue
        globals_config.update({
            f"{prefix}_base_url": str(model.get("api_base") or model.get("base_url")).strip(),
            f"{prefix}_model": str(model.get("model_name") or model.get("model")).strip(),
            f"{prefix}_api_key": str(model.get("api_key") or "").strip(),
        })
    return globals_config


def _inject_sdk_creds_and_root(
    cfg: Dict[str, Any],
    full_config: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Inject empty credential fields + markdown root into an inlined sdk dict.

    config.yaml 里 ``sdk.globals`` 的 ``llm_*`` / ``embedder_*`` 凭证与
    ``markdown_store.default.params.root`` 留空表示"用 jiuwenswarm 顶层真源"。
    空值若直接透传，会被 provider 的 ``_merge_config_over_default`` 深合并
    覆盖掉内置默认（``llm_base_url`` 变空、``root`` 变空 → md 落 cwd），
    所以这里把空字段就地填上真值：

    - LLM 凭证来自 ``models.defaults[is_default=true].model_client_config``
    - embedder 凭证来自顶层 ``embed.*``（strip ``/embeddings`` 后缀，避免 404）
    - markdown root 缺省 ``{user_workspace}/agent``
    """
    from .external_memory_builder import _jiuwenswarm_llm_creds, _strip_embeddings_suffix

    g = cfg.get("globals")
    if not isinstance(g, dict):
        g = {}
        cfg["globals"] = g

    llm = _jiuwenswarm_llm_creds(full_config) or {}
    embed = get_embed_config() or {}
    cred_defaults = {
        "llm_base_url": llm.get("base_url", ""),
        "llm_api_key": llm.get("api_key", ""),
        "llm_model": llm.get("model", ""),
        "embedder_base_url": _strip_embeddings_suffix(embed.get("base_url", "")),
        "embedder_api_key": embed.get("api_key", ""),
        "embedder_model": embed.get("model", ""),
    }
    for key, val in cred_defaults.items():
        if not g.get(key):
            g[key] = val

    md_ns = cfg.get("markdown_store")
    if isinstance(md_ns, dict):
        md_default = md_ns.get("default")
        if isinstance(md_default, dict):
            params = md_default.get("params")
            if isinstance(params, dict) and not params.get("root"):
                from jiuwenswarm.common.utils import get_user_workspace_dir

                params["root"] = str(Path(get_user_workspace_dir()) / "agent" / "workspace")
    return cfg


def _build_security_provider(org: str, user: str) -> Any:
    """进程内直连（sdk）模式的可信身份来源（mem2.0 P1-2 角色感知授权）。

    mem2.0 起身份不由业务 scope 自述——provider 只声明「要操作哪个 scope」，
    不能声明「我是谁」。进程内直连没有网络认证层，身份由本装配层（composition
    root）显式产出：actor 用 user 单主体（org=scope_id、user=user_id），
    ``role=USER`` 保留 org 硬边界；agent/session 维留空，在默认 ``USER_AGENT``
    主体路径下覆盖该 user 名下任意 agent（provider 的 ``bound_scope`` 把 target
    映射成 ``Scope(org=scope_id, user=user_id, agent=agent_id, session=session_id)``，
    与 actor 的 org/user 精确对齐）。

    安全类型走函数内 import——与文件头部约定的 lazy import 语义一致：缺包
    只影响 sdk 装配，不影响模块加载。
    """
    from datetime import UTC, datetime

    from jiuwen_memory.api import Scope, Surface
    from jiuwen_memory.common.security import new_request_context
    from jiuwen_memory.common.security.types import AuthContext, Role

    context = new_request_context(
        AuthContext(
            actor=Scope(org=org, user=user),
            role=Role.USER,
            credential_type="internal",
            auth_method="internal",
            authenticated_at=datetime.now(UTC),
        ),
        surface=Surface.INTERNAL,
    )
    return lambda: context


def build_jiuwen_memory_rail(
    config: Optional[Dict[str, Any]] = None,
    agent_name: Optional[str] = None,
    *,
    sdk_media_globals: dict[str, Any] | None = None,
) -> Optional[Any]:
    """Build a JiuwenMemoryRail + AgentMemoryMemoryProvider from config.

    ``agent_name``（当前运行 agent 的唯一 name）优先作为 scope 的 agent 轴——
    使记忆按 agent 隔离，而非所有 agent 共享 config 里固定的 ``agent_id``。
    缺省回落 config 的 ``agent_id``，再缺省固定 ``"jiuwenswarm"``。

    Returns None on any failure (missing packages / provider unavailable /
    explicit config file missing) — callers treat None as "memory disabled",
    never as an error.
    """
    try:
        from openjiuwen.harness.rails import JiuwenMemoryRail
    except Exception as exc:
        logger.warning("[JiuwenRailBuilder] JiuwenMemoryRail import failed: %s", exc)
        return None
    try:
        from jiuwen_memory_adapter.jiuwenswarm.agent_memory_provider import (
            AgentMemoryMemoryProvider,
        )
    except Exception as exc:
        logger.warning("[JiuwenRailBuilder] AgentMemoryMemoryProvider import failed: %s", exc)
        return None

    j = get_jiuwen_builtin_config(config)
    mode = get_jiuwen_builtin_mode(config)
    scope = _memory_scope(j, agent_name)
    user_id, scope_id, agent_id, session_id = (
        scope["user"], scope["org"], scope["agent"], scope["session"]
    )

    try:
        if mode == "sdk":
            # 进程内装配。内核配置优先级：
            #   1. config.yaml 内联的 sdk 两层命名空间 dict（凭证注入 + root 默认后直传）
            #   2. provider 内置 agent 场景默认装配（_agent_default_config()，serve-doc 的代码化副本）
            # http-only 字段（base_url/api_key/identity_*）忽略。
            # mem2.0 进程内直连需注入可信身份（fail-closed）：缺 security_provider
            # 时 provider.is_available() 为 False，rail 静默降级（memory 停用）。
            # 身份由本装配层产出（composition root），不从业务 user_id/scope_id 自述。
            security_provider = _build_security_provider(scope_id, user_id)
            inline = deepcopy(_custom_sdk_config_dict(j))
            if inline:
                _inject_sdk_creds_and_root(inline, config)
            if sdk_media_globals:
                inline.setdefault("globals", {}).update(deepcopy(sdk_media_globals))
            provider = AgentMemoryMemoryProvider(
                config_dict=inline or None,
                user_id=user_id,
                agent_id=agent_id,
                space=scope["space"],
                security_provider=security_provider,
            )
            config_desc = "merged-config-dict" if inline else "provider-builtin-agent-default"
        else:
            base_url = str(j.get("base_url") or os.environ.get("JIUWEN_MEMORY_BASE_URL", "") or "").strip()
            if not base_url:
                base_url = _DEFAULT_BASE_URL
            api_key = str(j.get("api_key") or os.environ.get("JIUWEN_MEMORY_API_KEY", "") or "")
            provider = AgentMemoryMemoryProvider(
                base_url=base_url,
                api_key=api_key,
                user_id=user_id,
                agent_id=agent_id,
                space=str(j.get("space") or ""),
                identity_org=str(j.get("identity_org") or "local"),
                identity_user=str(j.get("identity_user") or "developer"),
            )
        if not provider.is_available():
            logger.warning(
                "[JiuwenRailBuilder] AgentMemory provider unavailable (no base_url)"
            )
            return None
        rail = JiuwenMemoryRail(
            provider,
            user_id=user_id,
            scope_id=scope_id,
            agent_id=agent_id,
            session_id=session_id,
            session_end_extract=bool(j.get("session_end_extract", False)),
        )
    except Exception as exc:
        logger.warning("[JiuwenRailBuilder] build JiuwenMemoryRail failed: %s", exc)
        return None

    if mode == "sdk":
        logger.info(
            "[JiuwenRailBuilder] JiuwenMemoryRail built (mode=sdk, config=%s, user_id=%s, session_id=%r)",
            config_desc, user_id, session_id,
        )
    else:
        logger.info(
            "[JiuwenRailBuilder] JiuwenMemoryRail built (base_url=%s, user_id=%s, session_id=%r)",
            base_url, user_id, session_id,
        )
    return rail


def jiuwen_builtin_config_fingerprint(
    config: Optional[Dict[str, Any]] = None,
    *,
    sdk_media_globals: dict[str, Any] | None = None,
) -> str:
    """Stable fingerprint of the jiuwen builtin config for rail rebuild checks.

    Mirrors _embedding_config_fingerprint's contract: equal configs must
    compare equal; any semantic change must differ. api_key is hashed, never
    compared in clear.

    模式相关字段只进各自模式的指纹：sdk 指纹 = 内联 sdk dict 的 sha256（无内联
    时固定 ``builtin-default`` 标记）以及会话多模态模型与导入目录的哈希；
    http 指纹 = base_url/api_key hash/identity 轴。两模式
    共享的 scope 轴（user/scope/session/agent/space）与 session_end_extract 恒参与。
    """
    j = get_jiuwen_builtin_config(config)
    mode = get_jiuwen_builtin_mode(config)
    parts = [
        "jiuwen",
        mode,
        str(j.get("user_id") or _DEFAULT_USER),
        str(j.get("scope_id") or _DEFAULT_SCOPE),
        str(j.get("session_id") if j.get("session_id") is not None else ""),
        str(j.get("agent_id") or "jiuwenswarm"),
        str(j.get("space") or ""),
        str(bool(j.get("session_end_extract", False))),
    ]
    if mode == "sdk":
        sdk_dict = _custom_sdk_config_dict(j)
        if sdk_dict:
            try:
                payload = json.dumps(sdk_dict, sort_keys=True, ensure_ascii=False, default=str)
                parts += [hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]]
            except Exception:
                parts += ["inline-unhashable"]
        else:
            parts += ["builtin-default"]
        if sdk_media_globals:
            payload = json.dumps(sdk_media_globals, sort_keys=True, ensure_ascii=False)
            parts.append(hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16])
    else:
        base_url = str(j.get("base_url") or os.environ.get("JIUWEN_MEMORY_BASE_URL", "") or "").strip()
        if not base_url:
            base_url = _DEFAULT_BASE_URL
        api_key = str(j.get("api_key") or os.environ.get("JIUWEN_MEMORY_API_KEY", "") or "")
        parts += [
            base_url.rstrip("/"),
            hashlib.sha256(api_key.encode()).hexdigest()[:16],
            str(j.get("identity_org") or "local"),
            str(j.get("identity_user") or "developer"),
        ]
    return "|".join(parts)
