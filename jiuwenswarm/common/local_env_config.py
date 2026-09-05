"""Process env tip bags for Track B; Track A stays in real ``os.environ``.

Storage contract (acceptance)::

- **Track A** (``SPAWN_ENV_KEYS``): process/spawn shared — live in ``os.environ``.
- **Track B** (business / sync ``agents[].env``): tip/overlay only — never
  resident as bare keys or ``sid__aid__*`` namespaced keys in ``os.environ``.
- **Process baseline**: ``.env`` / bare Track B ingested into
  ``_process_baseline`` (shared). Readers do **not** fall through to baseline;
  values reach tips only via hydrate (local) or sync gaps.
- **Legal child exits**: ``export_agent_environ`` and skill credential injection.
- **Authority**: ``sync_agents_configs`` replaces per-agent tip then gaps
  baseline keys absent from the raw ``agents[].env`` object; ``shared_env``
  is audit-only and must not mutate process env on the sync path.

Isolation dimension is always ``(service_id, agent_id)`` (request-side).
Tip bags live here; Manager ``_latest_*`` is write-through only.

Tip formula B (effective tip)::
    active[(sid, aid)] ∪ staged[(sid, aid)]   # staged wins on key clash

Task seal: when overlay is bound (including ``{}``), readers only see overlay.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Iterable, Iterator, Mapping, MutableMapping
from contextvars import ContextVar, Token
from typing import Any
from jiuwenswarm.edition import is_enterprise

DEFAULT_HEADERS_ENV_KEY = "default_headers"
_DEFAULT_HEADERS_ALIASES = (
    DEFAULT_HEADERS_ENV_KEY,
    "DEFAULT_HEADERS",
    "OPENAI_DEFAULT_HEADERS",
)
# Huawei MaaS / OfficeClaw: use the protocol-specific header as a fallback when
# ``default_headers`` is missing from the tip seal.
_DEFAULT_HEADERS_FALLBACK_ALIASES = (
    "OFFICE_CLAW_HUAWEI_MAAS_HEADERS_JSON",
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Track A / mirror / P2 key tables (authoritative MVP inventories)
# ---------------------------------------------------------------------------

SPAWN_ENV_KEYS: frozenset[str] = frozenset(
    {
        "HOME",
        "JIUWENSWARM_DATA_DIR",
        "JIUWENSWARM_AGENT_ROOT",
        "PYTHONUNBUFFERED",
        "WEB_HOST",
        # Align with relay-claw launchEnv / sync_agents_configs shared_env (short names).
        "OFFICE_CLAW_MCP_SERVER_PATH",
        "OFFICE_CLAW_MCP_COMMAND",
        "OFFICE_CLAW_MCP_ARGS_JSON",
        "OFFICE_CLAW_MCP_CWD",
        "OFFICE_CLAW_MCP_EXCLUDED_TOOLS",
        "OFFICE_CLAW_MCP_MANIFEST_PATH",
        "JIUWENSWARM_MCP_MANIFEST",
        # Legacy aliases (pre-alignment SPAWN table); accept so old shared_env is not ignored.
        "OFFICE_CLAW_MCP_SERVER_COMMAND",
        "OFFICE_CLAW_MCP_SERVER_ARGS_JSON",
        "OFFICE_CLAW_MCP_SERVER_CWD",
        "OTEL_ENABLED",
        "OTEL_TRACES_EXPORTER",
        "OTEL_METRICS_EXPORTER",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_PROTOCOL",
        "OTEL_SERVICE_NAME",
        "OTEL_LOG_MESSAGES",
        "PATH",
        "JIUWENSWARM_EDITION",
        # Unified tool switch (blacklist); config.yaml references ${DISABLED_TOOLS-...}.
        # Defaults to disabling search_skill / install_skill / uninstall_skill.
        "DISABLED_TOOLS",
        # Code-mode generated-code co-author header switch; process shared.
        "JIUWENSWARM_CODE_COAUTHOR_HEADER_ENABLED",
        # launchEnv / config.yaml ${EXTENSION_DIRS}; process-shared (relay RELAYCLAW_SHARED_ENV_KEYS TBD).
        "EXTENSION_DIRS",
    }
)

BUSINESS_MIRROR_KEYS: frozenset[str] = frozenset(
    {
        # A. sync agents[].env schema
        "API_KEY",
        "API_BASE",
        "MEMORY_ENGINE",
        "EVOLUTION_ENABLED",
        "EMBED_API_KEY",
        "EMBED_API_BASE",
        "EMBED_MODEL",
        "MODEL_NAME",
        "MODEL_PROVIDER",
        "MODEL_CONTEXT_WINDOW",
        "TOOL_CALLING_GUARD_ENABLED",
        "TOOL_CALLING_GUARD_DISABLE",
        "TOOL_CALLING_GUARD_STRIP_REASON",
        "ENABLED_SKILLS",
        "DISABLED_SKILLS",
        # Canonical product keys only; relay ``JIUWENCLAW_*`` is remapped on ingest.
        "JIUWENSWARM_DISABLED_SKILLS",
        "JIUWENSWARM_SHARED_SKILLS_DIRS",
        "BOCHA_API_KEY",
        "JINA_API_KEY",
        "PERPLEXITY_API_KEY",
        "SERPER_API_KEY",
        "PETAL_SEARCH_URL",
        "PETAL_SEARCH_HEADERS",
        "default_headers",
        "DEFAULT_HEADERS",
        "VISION_API_KEY",
        "VISION_API_BASE",
        "VISION_PROVIDER",
        "VISION_MODEL_NAME",
        "VISION_DEFAULT_HEADERS",
        "IMAGE_GEN_API_KEY",
        "IMAGE_GEN_API_BASE",
        "IMAGE_GEN_PROVIDER",
        "IMAGE_GEN_MODEL_NAME",
        "IMAGE_GEN_DEFAULT_HEADERS",
        # B. Gateway/CLI extensions
        "AUDIO_API_KEY",
        "AUDIO_API_BASE",
        "AUDIO_PROVIDER",
        "AUDIO_MODEL_NAME",
        "VIDEO_API_KEY",
        "VIDEO_API_BASE",
        "VIDEO_PROVIDER",
        "VIDEO_MODEL_NAME",
        "EMAIL_ADDRESS",
        "EMAIL_TOKEN",
        "GITHUB_TOKEN",
        "FREE_SEARCH_PROXY_URL",
        # Web config.set free-search flags (distinct from JIUWENSWARM_ENABLE_*)
        "FREE_SEARCH_DDG_ENABLED",
        "FREE_SEARCH_BING_ENABLED",
        # Web config.set DeepSearch / deepresearch
        "LLM_MODEL_NAME",
        "LLM_MODEL_TYPE",
        "LLM_BASE_URL",
        "LLM_API_KEY",
        "WEB_SEARCH_ENGINE_NAME",
        "WEB_SEARCH_API_KEY",
        "WEB_SEARCH_URL",
        "EXECUTION_METHOD",
        "TAVILY_API_KEY",
        # ACRCloud (audio_tools / read_env)
        "ACR_ACCESS_KEY",
        "ACR_ACCESS_SECRET",
        "ACR_BASE_URL",
        # SkillNet / OpenJiuwen market (skill_manager)
        "SKILLNET_DOWNLOAD_TIMEOUT",
        "SKILLNET_MAX_RETRIES",
        "OPENJIUWEN_MARKET_TIMEOUT",
        "OPENJIUWEN_MARKET_BASE_URL",
        "OPENJIUWEN_ALLOWED_DOWNLOAD_HOSTS",
        "IMPORT_LOCAL_REMOTE_TIMEOUT",
        "IMPORT_LOCAL_ALLOWED_DOWNLOAD_HOSTS",
        "BROWSER_DRIVER",
        "BROWSER_PROFILE_NAME",
        "BROWSER_MANAGED_BINARY",
        "BROWSER_TIMEOUT_S",
        "BROWSER_ALLOW_SHORT_TIMEOUT_OVERRIDE",
        "MEMORY_MODE",
        "JIUWENSWARM_ENABLE_DDG_SEARCH",
        "JIUWENSWARM_ENABLE_JINA_SEARCH",
        "JIUWENSWARM_ENABLE_JINA_FETCH",
        "JIUWENSWARM_SSL_VERIFY",
    }
)

PROCESS_UNIQUE_ENV_KEYS: frozenset[str] = frozenset()

# ---------------------------------------------------------------------------
# Product env alias: relay-claw still ships ``JIUWENCLAW_*``; tip/code use
# ``JIUWENSWARM_*``. Remap at the boundary — do not dual-list both everywhere.
# ---------------------------------------------------------------------------

_LEGACY_PRODUCT_ENV_PREFIX = "JIUWENCLAW_"
_CANONICAL_PRODUCT_ENV_PREFIX = "JIUWENSWARM_"


def canonical_product_env_key(name: str) -> str:
    """Map ``JIUWENCLAW_*`` → ``JIUWENSWARM_*``; leave other keys unchanged."""
    key = str(name)
    if key.startswith(_LEGACY_PRODUCT_ENV_PREFIX):
        return _CANONICAL_PRODUCT_ENV_PREFIX + key[len(_LEGACY_PRODUCT_ENV_PREFIX):]
    return key


def legacy_product_env_key(name: str) -> str | None:
    """Return the relay ``JIUWENCLAW_*`` alias for a ``JIUWENSWARM_*`` key."""
    key = str(name)
    if key.startswith(_CANONICAL_PRODUCT_ENV_PREFIX):
        return _LEGACY_PRODUCT_ENV_PREFIX + key[len(_CANONICAL_PRODUCT_ENV_PREFIX):]
    return None


def normalize_product_env_aliases(
    env: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Rewrite product legacy keys to canonical; canonical wins on clash."""
    if not isinstance(env, Mapping):
        return {}
    out: dict[str, Any] = {}
    for raw_key, value in env.items():
        key = str(raw_key)
        canon = canonical_product_env_key(key)
        if canon in out and key.startswith(_LEGACY_PRODUCT_ENV_PREFIX):
            continue
        out[canon] = value
    return out


def env_keys_with_product_aliases(keys: Iterable[str]) -> frozenset[str]:
    """Expand a key set with both canonical and legacy product forms."""
    expanded: set[str] = set()
    for raw in keys:
        key = str(raw)
        expanded.add(key)
        expanded.add(canonical_product_env_key(key))
        legacy = legacy_product_env_key(key)
        if legacy is not None:
            expanded.add(legacy)
        # If caller passed a legacy key, also keep its canonical form (above)
        # and the original legacy form.
        if key.startswith(_LEGACY_PRODUCT_ENV_PREFIX):
            expanded.add(key)
    return frozenset(expanded)


def product_env_lookup_names(name: str) -> tuple[str, ...]:
    """Ordered names to probe for a product env key (canonical + relay alias)."""
    key = str(name)
    canon = canonical_product_env_key(key)
    if key.startswith(_LEGACY_PRODUCT_ENV_PREFIX) and canon != key:
        return (canon, key)
    names: list[str] = [key]
    if canon not in names:
        names.append(canon)
    legacy = legacy_product_env_key(key)
    if legacy is not None and legacy not in names:
        names.append(legacy)
    return tuple(names)


# ---------------------------------------------------------------------------
# Id / bag helpers
# ---------------------------------------------------------------------------

_DEFAULT_SERVICE_ID = "default"
_DEFAULT_AGENT_ID = "default"
EnvNsKey = tuple[str, str]

_active_bags: dict[EnvNsKey, dict[str, Any]] = {}
_staged_bags: dict[EnvNsKey, dict[str, Any]] = {}
# Process-shared Track B from .env / cold start (not a per-agent tip).
_process_baseline: dict[str, str] = {}

# Unbound sentinel: distinguish "not bound" from bound empty dict ``{}``.
_UNBOUND: object = object()

_task_env_overlay: ContextVar[Any] = ContextVar(
    "jiuwenswarm_task_env_overlay", default=_UNBOUND
)
_agent_env_ns: ContextVar[EnvNsKey | None] = ContextVar(
    "jiuwenswarm_agent_env_ns", default=None
)

_mirrored_once = False


class EnvNsIdError(ValueError):
    """Raised when service_id / agent_id contains ``__`` or is otherwise invalid."""


def normalize_env_ns_id(value: str | None, *, default: str = _DEFAULT_AGENT_ID) -> str:
    if value is None:
        text = default
    else:
        text = str(value).strip() or default
    if "__" in text:
        raise EnvNsIdError(f"env ns id must not contain '__': {text!r}")
    if any(token in text for token in ("\x00", "/", "\\")) or text in {".", ".."}:
        raise EnvNsIdError(f"env ns id must not contain path syntax: {text!r}")
    return text


def get_bound_agent_env_ns() -> EnvNsKey | None:
    """Return the currently bound (service_id, agent_id), or None if unbound."""
    return _agent_env_ns.get()


def resolve_env_ns(
    service_id: str | None = None,
    agent_id: str | None = None,
) -> EnvNsKey:
    """Resolve bag key: explicit args > ContextVar > default/default."""
    bound = _agent_env_ns.get()
    if service_id is None and agent_id is None and bound is not None:
        return bound
    sid = normalize_env_ns_id(
        service_id if service_id is not None else (bound[0] if bound else _DEFAULT_SERVICE_ID),
        default=_DEFAULT_SERVICE_ID,
    )
    aid = normalize_env_ns_id(
        agent_id if agent_id is not None else (bound[1] if bound else _DEFAULT_AGENT_ID),
        default=_DEFAULT_AGENT_ID,
    )
    return sid, aid


def make_env_ns_key(service_id: str, agent_id: str, name: str) -> str:
    sid = normalize_env_ns_id(service_id, default=_DEFAULT_SERVICE_ID)
    aid = normalize_env_ns_id(agent_id, default=_DEFAULT_AGENT_ID)
    logical = str(name)
    if "__" in logical:
        raise EnvNsIdError(f"logical env key must not contain '__': {logical!r}")
    return f"{sid}__{aid}__{logical}"


def parse_env_ns_key(full_key: str) -> tuple[str, str, str] | None:
    parts = str(full_key).split("__", 2)
    if len(parts) != 3:
        return None
    sid, aid, logical = parts
    if not sid or not aid or not logical:
        return None
    if "__" in sid or "__" in aid:
        return None
    try:
        normalize_env_ns_id(sid)
        normalize_env_ns_id(aid)
    except EnvNsIdError:
        return None
    return sid, aid, logical


def _bag(store: dict[EnvNsKey, dict[str, Any]], key: EnvNsKey) -> dict[str, Any]:
    bag = store.get(key)
    if bag is None:
        bag = {}
        store[key] = bag
    return bag


def get_active_env(
    service_id: str | None = None,
    agent_id: str | None = None,
) -> dict[str, Any]:
    return dict(_bag(_active_bags, resolve_env_ns(service_id, agent_id)))


def get_staged_env(
    service_id: str | None = None,
    agent_id: str | None = None,
) -> dict[str, Any]:
    """Return a copy of staged env overrides for the resolved ``(sid, aid)``."""
    return dict(_bag(_staged_bags, resolve_env_ns(service_id, agent_id)))


def clear_staged_env(
    service_id: str | None = None,
    agent_id: str | None = None,
) -> None:
    key = resolve_env_ns(service_id, agent_id)
    _staged_bags.pop(key, None)


def effective_tip(
    service_id: str | None = None,
    agent_id: str | None = None,
) -> dict[str, Any]:
    """Formula B: ``active ∪ staged`` (staged wins)."""
    key = resolve_env_ns(service_id, agent_id)
    merged = dict(_bag(_active_bags, key))
    merged.update(_bag(_staged_bags, key))
    return merged


def _invalidate_resolved_config_cache(
    service_id: str | None = None,
    agent_id: str | None = None,
) -> None:
    """Drop get_config() resolved cache for this ns (lazy import avoids cycle)."""
    try:
        from jiuwenswarm.common.config import clear_config_cache
    except ImportError as e:
        logger.debug("clear_config_cache unavailable during import: %s", e)
        return
    clear_config_cache(service_id=service_id, agent_id=agent_id)


# Incremental reload must not seal empty model credentials into tip (OfficeClaw
# often sends API_BASE="" when callbackEnv is not yet resolved). Null still deletes.
_EMPTY_OMIT_ENV_KEYS: frozenset[str] = frozenset(
    {
        "API_BASE",
        "API_KEY",
        "MODEL_PROVIDER",
        "EMBED_API_BASE",
        "EMBED_API_KEY",
    }
)


def stage_env_overrides(
    env_overrides: dict[str, Any] | None,
    *,
    service_id: str | None = None,
    agent_id: str | None = None,
) -> None:
    """Merge env reload payload into staged bag without touching active."""
    if not isinstance(env_overrides, dict):
        return
    bag = _bag(_staged_bags, resolve_env_ns(service_id, agent_id))
    for env_key, env_value in normalize_product_env_aliases(env_overrides).items():
        key = str(env_key)
        if key in SPAWN_ENV_KEYS:
            logger.warning("拒绝 stage 轨道 A 键: %s", key)
            continue
        if env_value is None:
            # 用 None 标记删除意图，让 promote_staged_env 能通过
            # if value is None 分支正确执行 active.pop(name, None)。
            # 不能用 bag.pop(key, None)，否则 staged bag 为空时
            # promote_staged_env 会提前返回，删除意图丢失。
            bag[key] = None
        else:
            text = str(env_value)
            if key in _EMPTY_OMIT_ENV_KEYS and not text.strip():
                continue
            bag[key] = text


def promote_staged_env(
    *,
    service_id: str | None = None,
    agent_id: str | None = None,
) -> None:
    """Promote staged bag into active tip for this pair (tip-only)."""
    key = resolve_env_ns(service_id, agent_id)
    staged = _staged_bags.get(key)
    if not staged:
        return
    active = _bag(_active_bags, key)
    sid, aid = key
    for name, value in list(staged.items()):
        if value is None:
            active.pop(name, None)
            _pop_bare_if_default_default(sid, aid, name)
        else:
            active[name] = _plaintext_tip_value(name, value)
    _staged_bags.pop(key, None)
    _invalidate_resolved_config_cache(service_id=sid, agent_id=aid)


def _plaintext_tip_value(name: str, value: Any) -> str:
    """Store tip values as plaintext (decrypt ciphertext from .env / legacy)."""
    text = str(value)
    if not text:
        return text
    return str(decrypt(name, text))


def _ensure_ciphertext(name: str, value: Any) -> str:
    """Store sensitive tip/baseline values as ciphertext; others unchanged.

    Heuristic: if ``decrypt`` changes the value, treat it as already ciphertext
    and keep as-is (avoids double-encrypt for .env ingest / legacy). Otherwise
    encrypt plaintext. Without a crypto provider this is a no-op.
    """
    text = str(value)
    if not text:
        return text
    if not is_sensitive_env_name(name):
        return text
    plain = decrypt(name, text)
    if plain != text:
        return text
    return encrypt(name, text)


def seal_env_mapping(mapping: Mapping[str, Any] | None) -> dict[str, Any]:
    """Copy a mapping with sensitive values sealed as ciphertext for long-lived stores."""
    if not isinstance(mapping, Mapping):
        return {}
    out: dict[str, Any] = {}
    for key, value in mapping.items():
        name = str(key)
        if value is None:
            out[name] = None
        else:
            out[name] = _ensure_ciphertext(name, value)
    return out


def materialize_env_mapping(mapping: Mapping[str, Any] | None) -> dict[str, Any]:
    """Copy a mapping with sensitive values decrypted for short-lived use."""
    if not isinstance(mapping, Mapping):
        return {}
    out: dict[str, Any] = {}
    for key, value in mapping.items():
        name = str(key)
        if value is None:
            out[name] = None
        elif isinstance(value, str):
            out[name] = decrypt(name, value) if value else value
        else:
            out[name] = value
    return out


def _pop_bare_if_default_default(service_id: str, agent_id: str, name: str) -> None:
    """Pop residual bare Track B key only for the default/default bag."""
    if service_id == _DEFAULT_SERVICE_ID and agent_id == _DEFAULT_AGENT_ID:
        os.environ.pop(name, None)


def pop_track_b_bare_from_environ() -> list[str]:
    """Remove Track B bare keys from ``os.environ`` (H1 hygiene / after load_dotenv).

    Returns the logical key names that were present and removed (values never logged).
    """
    removed: list[str] = []
    for key in BUSINESS_MIRROR_KEYS:
        if key in SPAWN_ENV_KEYS:
            continue
        for name in env_keys_with_product_aliases((key,)):
            if name not in os.environ:
                continue
            os.environ.pop(name, None)
            if name not in removed:
                removed.append(name)
    return removed


def apply_env_overrides_to_active(
    env_overrides: dict[str, Any] | None,
    *,
    service_id: str | None = None,
    agent_id: str | None = None,
) -> None:
    """Write env overrides directly to active tip (cold start / incremental)."""
    if not isinstance(env_overrides, dict):
        return
    key = resolve_env_ns(service_id, agent_id)
    active = _bag(_active_bags, key)
    sid, aid = key
    for env_key, env_value in normalize_product_env_aliases(env_overrides).items():
        name = str(env_key)
        if name in SPAWN_ENV_KEYS:
            logger.warning(
                "拒绝将轨道 A 键写入 active tip: %s (sid=%s aid=%s)", name, sid, aid
            )
            continue
        if env_value is None:
            active.pop(name, None)
            _pop_bare_if_default_default(sid, aid, name)
        else:
            value = str(env_value)
            if name in _EMPTY_OMIT_ENV_KEYS and not value.strip():
                continue
            active[name] = _plaintext_tip_value(name, value)
    _invalidate_resolved_config_cache(service_id=sid, agent_id=aid)


def replace_active_env(
    env_overrides: dict[str, Any] | None,
    *,
    service_id: str | None = None,
    agent_id: str | None = None,
    clear_staged: bool = True,
) -> None:
    """Full-replace active tip for one ``(sid, aid)`` (sync path)."""
    key = resolve_env_ns(service_id, agent_id)
    sid, aid = key
    previous = dict(_bag(_active_bags, key))
    new_map: dict[str, Any] = {}
    if isinstance(env_overrides, dict):
        for env_key, env_value in normalize_product_env_aliases(env_overrides).items():
            name = str(env_key)
            if name in SPAWN_ENV_KEYS:
                continue
            if env_value is None:
                continue
            text = str(env_value)
            if name in _EMPTY_OMIT_ENV_KEYS and not text.strip():
                continue
            new_map[name] = _plaintext_tip_value(name, text)
    _active_bags[key] = new_map
    for name in previous:
        if name not in new_map:
            _pop_bare_if_default_default(sid, aid, name)
    if clear_staged:
        _staged_bags.pop(key, None)
    _invalidate_resolved_config_cache(service_id=sid, agent_id=aid)


def clear_agent_env_ns(service_id: str, agent_id: str) -> None:
    """Wipe staged + active tip for one ``(service_id, agent_id)`` pair."""
    clear_staged_env(service_id=service_id, agent_id=agent_id)
    replace_active_env(
        {},
        service_id=service_id,
        agent_id=agent_id,
        clear_staged=True,
    )
    if (
        normalize_env_ns_id(service_id, default=_DEFAULT_SERVICE_ID) == _DEFAULT_SERVICE_ID
        and normalize_env_ns_id(agent_id, default=_DEFAULT_AGENT_ID) == _DEFAULT_AGENT_ID
    ):
        pop_track_b_bare_from_environ()


def apply_env_removals(
    removals: dict[str, None] | None,
    *,
    service_id: str | None = None,
    agent_id: str | None = None,
) -> None:
    """Remove env keys from active and staged tip for one pair."""
    if not isinstance(removals, dict) or not removals:
        return
    key = resolve_env_ns(service_id, agent_id)
    sid, aid = key
    active = _bag(_active_bags, key)
    staged = _bag(_staged_bags, key)
    for env_key in normalize_product_env_aliases(removals):
        name = str(env_key)
        active.pop(name, None)
        staged.pop(name, None)
        _pop_bare_if_default_default(sid, aid, name)
        legacy = legacy_product_env_key(name)
        if legacy is not None:
            active.pop(legacy, None)
            staged.pop(legacy, None)
            _pop_bare_if_default_default(sid, aid, legacy)
    _invalidate_resolved_config_cache(service_id=sid, agent_id=aid)


def build_effective_env_overlay(
    *extra: dict[str, Any] | None,
    service_id: str | None = None,
    agent_id: str | None = None,
) -> dict[str, Any]:
    """Formula B tip, then merge optional extras (extras win; ``None`` pops)."""
    merged = effective_tip(service_id, agent_id)
    for part in extra:
        if isinstance(part, dict):
            for key, value in normalize_product_env_aliases(part).items():
                k = str(key)
                if value is None:
                    merged.pop(k, None)
                else:
                    text = str(value)
                    if k in _EMPTY_OMIT_ENV_KEYS and not text.strip():
                        continue
                    merged[k] = text
    # Drop empty credential keys already present in tip so seal does not pin "".
    for k in _EMPTY_OMIT_ENV_KEYS:
        if k in merged and not str(merged.get(k) or "").strip():
            merged.pop(k, None)
    return merged


def bind_agent_env_ns(service_id: str, agent_id: str) -> Token:
    """Bind tip env ns ``(service_id, agent_id)`` for this task."""
    key = resolve_env_ns(service_id, agent_id)
    return _agent_env_ns.set(key)


def reset_agent_env_ns(token: Token) -> None:
    _agent_env_ns.reset(token)


def bind_task_env_overlay(overlay: dict[str, Any] | None) -> Token:
    """Bind task-scoped overlay. Always binds a dict (``None`` → ``{}``).

    Callers must not use truthiness of the return/overlay to skip bind.
    Use :func:`reset_task_env_overlay` to unbind.
    """
    if overlay is None:
        bound: dict[str, Any] = {}
    else:
        bound = normalize_product_env_aliases(overlay)
    return _task_env_overlay.set(bound)


def reset_task_env_overlay(token: Token) -> None:
    _task_env_overlay.reset(token)


def get_task_env_overlay() -> dict[str, Any] | None:
    """Return current overlay if bound; ``None`` when unbound."""
    value = _task_env_overlay.get()
    if value is _UNBOUND:
        return None
    return value


def is_task_env_overlay_bound() -> bool:
    return _task_env_overlay.get() is not _UNBOUND


# ---------------------------------------------------------------------------
# Compat view: ENV_CONFIG_DICT → active[default,default] (tests / legacy)
# ---------------------------------------------------------------------------


class _ActiveEnvDict(MutableMapping[str, Any]):
    """MutableMapping proxy over the resolved active bag (default: default/default)."""

    def _target(self) -> dict[str, Any]:
        return _bag(_active_bags, resolve_env_ns())

    def __getitem__(self, key: str) -> Any:
        target = self._target()
        for lookup in product_env_lookup_names(key):
            if lookup in target:
                return target[lookup]
        raise KeyError(key)

    def __setitem__(self, key: str, value: Any) -> None:
        name = canonical_product_env_key(key)
        bag = self._target()
        if value is None:
            bag.pop(name, None)
        else:
            bag[name] = value

    def __delitem__(self, key: str) -> None:
        name = canonical_product_env_key(key)
        del self._target()[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self._target())

    def __len__(self) -> int:
        return len(self._target())

    def clear(self) -> None:
        # Test helper: wipe all bags (active + staged) for isolation.
        _active_bags.clear()
        _staged_bags.clear()

    def update(
        self,
        other: Mapping[Any, Any] | Iterable[tuple[Any, Any]] = (),
        /,
        **kwargs: Any,
    ) -> None:
        bag = self._target()
        if isinstance(other, Mapping):
            items: Iterable[tuple[Any, Any]] = other.items()
        else:
            items = other
        for k, v in normalize_product_env_aliases(dict(items)).items():
            bag[str(k)] = v
        for k, v in normalize_product_env_aliases(kwargs).items():
            bag[str(k)] = v


ENV_CONFIG_DICT: MutableMapping[str, Any] = _ActiveEnvDict()


# ---------------------------------------------------------------------------
# Tip writers / export (Track B never resident in os.environ)
# ---------------------------------------------------------------------------


def set_os_environ(
    name: str,
    value: Any,
    *,
    service_id: str | None = None,
    agent_id: str | None = None,
) -> None:
    """Write Track B active tip only (plaintext). Does not touch ``os.environ``."""
    if name in SPAWN_ENV_KEYS:
        logger.warning("拒绝 set_os_environ 轨道 A 键: %s", name)
        return
    key = resolve_env_ns(service_id, agent_id)
    sid, aid = key
    active = _bag(_active_bags, key)
    if value is None:
        active.pop(str(name), None)
        _pop_bare_if_default_default(sid, aid, str(name))
        return
    active[str(name)] = _plaintext_tip_value(str(name), value)


def get_os_environ(
    name: str,
    default: Any = None,
    *,
    service_id: str | None = None,
    agent_id: str | None = None,
) -> Any:
    """Read Track B from tip only (compat alias; prefer ``get_local_config``)."""
    if name in SPAWN_ENV_KEYS:
        logger.warning("get_os_environ 不服务轨道 A 键: %s —— 请直读 spawn 环境", name)
        return default
    tip = effective_tip(service_id, agent_id)
    if name not in tip:
        return default
    return _read_from_mapping(name, tip, default)


def export_agent_environ(
    service_id: str,
    agent_id: str,
) -> dict[str, str]:
    """Tip (Track B, plaintext) ∪ Track A spawn keys ∪ Windows platform vars.

    Explicit child ``env=`` only — never a reason to leave Track B in the parent
    process environ.
    """
    out: dict[str, str] = {}
    tip = effective_tip(service_id, agent_id)
    for k, v in tip.items():
        if v is None:
            continue
        out[str(k)] = str(v)
    for k in SPAWN_ENV_KEYS:
        if k in os.environ:
            out[k] = os.environ[k]
    for k in PROCESS_UNIQUE_ENV_KEYS:
        if k in os.environ:
            out[k] = os.environ[k]
    _ensure_windows_platform_env(out)
    return out


def export_spawn_environ() -> dict[str, str]:
    """Return only process-shared keys that are safe for a child process.

    Values come directly from the real process environment. Tenant Track-B
    tips are intentionally excluded; callers that need those credentials must
    use an explicit, narrower export boundary.
    """
    out: dict[str, str] = {}
    for key in SPAWN_ENV_KEYS | PROCESS_UNIQUE_ENV_KEYS:
        value = os.environ.get(key)
        if value is not None:
            out[key] = value
    _ensure_windows_platform_env(out)
    return out


def _ensure_windows_platform_env(out: dict[str, str]) -> None:
    """Pass through OS-level vars a Windows child process needs to function."""
    if os.name != "nt":
        return
    for k in (
        "SYSTEMROOT",
        "SystemDrive",
        "windir",
        "TEMP",
        "TMP",
        "COMSPEC",
        "PATHEXT",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "HOMEDRIVE",
        "HOMEPATH",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
    ):
        v = os.environ.get(k)
        if v and k not in out:
            out[k] = v


def get_process_baseline() -> dict[str, str]:
    """Return a copy of the process-shared Track B baseline (from ``.env``)."""
    return dict(_process_baseline)


def update_process_baseline(updates: Mapping[str, Any] | None) -> None:
    """Merge plaintext Track B keys into process baseline (Web/CLI persist)."""
    if not isinstance(updates, Mapping):
        return
    for env_key, env_value in updates.items():
        name = str(env_key)
        if name in SPAWN_ENV_KEYS:
            continue
        if env_value is None:
            _process_baseline.pop(name, None)
            continue
        text = str(env_value)
        if name in _EMPTY_OMIT_ENV_KEYS and not text.strip():
            continue
        _process_baseline[name] = _plaintext_tip_value(name, text)


def apply_process_baseline_gaps(
    service_id: str | None,
    agent_id: str | None,
    *,
    reserved_keys: Iterable[str] | None = None,
) -> None:
    """Copy baseline keys not in ``reserved_keys`` into the agent tip."""
    key = resolve_env_ns(service_id, agent_id)
    active = _bag(_active_bags, key)
    reserved = {str(k) for k in (reserved_keys or ())}
    for name, value in _process_baseline.items():
        if name in reserved:
            continue
        if name in active:
            continue
        active[name] = value
    sid, aid = key
    _invalidate_resolved_config_cache(service_id=sid, agent_id=aid)


def hydrate_default_tip_from_baseline() -> None:
    """Local cold-start: copy entire baseline into ``default/default`` tip."""
    apply_process_baseline_gaps(
        _DEFAULT_SERVICE_ID,
        _DEFAULT_AGENT_ID,
        reserved_keys=(),
    )


def should_hydrate_default_tip() -> bool:
    """True for local processes; False when enterprise edition."""
    return not is_enterprise()


_LEGACY_OFFICE_CLAW_DISABLE_TOOL_CALLING = "OFFICE_CLAW_DISABLE_TOOL_CALLING"
_LEGACY_OFFICE_CLAW_DISABLE_TRUTHY = frozenset({"1", "true", "yes", "on"})
_LEGACY_TOOL_CALLING_GUARD_STRIP_REASON = "legacy_office_claw_disable_tool_calling"


def _map_legacy_office_claw_disable_tool_calling() -> bool:
    """Map deprecated ``OFFICE_CLAW_DISABLE_TOOL_CALLING`` into Guard tip keys."""
    if _LEGACY_OFFICE_CLAW_DISABLE_TOOL_CALLING not in os.environ:
        return False
    raw = os.environ.pop(_LEGACY_OFFICE_CLAW_DISABLE_TOOL_CALLING, None)
    if raw is None:
        return False
    if str(raw).strip().lower() not in _LEGACY_OFFICE_CLAW_DISABLE_TRUTHY:
        return False
    logger.warning(
        "%s is deprecated; mapped to TOOL_CALLING_GUARD_ENABLED=true, "
        "TOOL_CALLING_GUARD_DISABLE=true, TOOL_CALLING_GUARD_STRIP_REASON=%s. "
        "Prefer the TOOL_CALLING_GUARD_* variables.",
        _LEGACY_OFFICE_CLAW_DISABLE_TOOL_CALLING,
        _LEGACY_TOOL_CALLING_GUARD_STRIP_REASON,
    )
    os.environ.setdefault("TOOL_CALLING_GUARD_ENABLED", "true")
    os.environ.setdefault("TOOL_CALLING_GUARD_DISABLE", "true")
    os.environ.setdefault(
        "TOOL_CALLING_GUARD_STRIP_REASON",
        _LEGACY_TOOL_CALLING_GUARD_STRIP_REASON,
    )
    return True


def _ingest_legacy_guard_keys_into_baseline() -> None:
    """Copy mapped Guard keys from ``os.environ`` into baseline (secondary ingest)."""
    for key in (
        "TOOL_CALLING_GUARD_ENABLED",
        "TOOL_CALLING_GUARD_DISABLE",
        "TOOL_CALLING_GUARD_STRIP_REASON",
    ):
        if key not in os.environ:
            continue
        _process_baseline.setdefault(
            key, _plaintext_tip_value(key, os.environ[key])
        )


def ingest_bare_business_into_tip(*, force: bool = False) -> None:
    """After ``load_dotenv``: bare Track B → process_baseline, then pop bare."""
    global _mirrored_once
    legacy_mapped = _map_legacy_office_claw_disable_tool_calling()
    if _mirrored_once and not force:
        if legacy_mapped:
            _ingest_legacy_guard_keys_into_baseline()
        removed = pop_track_b_bare_from_environ()
        if removed:
            logger.info(
                "secondary ingest: re-popped %d Track B bare key(s) from os.environ "
                "(baseline already set; keys not re-ingested): %s",
                len(removed),
                ", ".join(sorted(removed)),
            )
        else:
            logger.debug(
                "secondary ingest: no Track B bare keys present in os.environ to re-pop"
            )
        if legacy_mapped and should_hydrate_default_tip():
            hydrate_default_tip_from_baseline()
        return
    for key in BUSINESS_MIRROR_KEYS:
        if key in SPAWN_ENV_KEYS:
            continue
        raw = None
        source_key = key
        for lookup in product_env_lookup_names(key):
            if lookup not in os.environ:
                continue
            raw = os.environ[lookup]
            source_key = lookup
            break
        if raw is None:
            continue
        if key in _EMPTY_OMIT_ENV_KEYS and not str(raw).strip():
            for lookup in product_env_lookup_names(key):
                os.environ.pop(lookup, None)
            continue
        plain = _plaintext_tip_value(key, raw)
        _process_baseline.setdefault(key, plain)
        for lookup in product_env_lookup_names(key):
            os.environ.pop(lookup, None)
        if source_key != key:
            logger.debug(
                "ingest: remapped bare %s → %s into process baseline",
                source_key,
                key,
            )
    _mirrored_once = True
    if should_hydrate_default_tip():
        hydrate_default_tip_from_baseline()


def ingest_bare_business_into_baseline(*, force: bool = False) -> None:
    """Alias for :func:`ingest_bare_business_into_tip` (baseline + optional hydrate)."""
    ingest_bare_business_into_tip(force=force)


def mirror_bare_business_env_to_default_ns(*, force: bool = False) -> None:
    """Compat alias for :func:`ingest_bare_business_into_tip`."""
    ingest_bare_business_into_tip(force=force)


# ---------------------------------------------------------------------------
# Readers (seal + formula B)
# ---------------------------------------------------------------------------


def _mapping_has_product_key(mapping: Mapping[str, Any], name: str) -> bool:
    return any(n in mapping for n in product_env_lookup_names(name))


def _read_from_mapping(name: str, mapping: dict[str, Any], default: Any = None) -> Any:
    for lookup in product_env_lookup_names(name):
        if lookup not in mapping:
            continue
        value = mapping[lookup]
        if value is None or value == "":
            return default
        return decrypt(lookup, value) if isinstance(value, str) else value
    return default


def get_local_config(name: str, default=None):
    """Track-B reader: bound overlay (seal) → formula B tip → process env.

    Falls back to ``os.environ`` for names not present in the tip (e.g. cold
    start or non-Track-B keys) so ``${VAR}`` config substitution keeps working
    for arbitrary process env vars; Track A keys are still refused.

    ``JIUWENCLAW_*`` / ``JIUWENSWARM_*`` product aliases resolve interchangeably.
    """
    if name in SPAWN_ENV_KEYS:
        logger.warning(
            "get_local_config 不服务轨道 A 键 %s —— 请直读 spawn/path API", name
        )
        return default

    overlay = _task_env_overlay.get()
    if overlay is not _UNBOUND:
        # Seal: miss => unset (no fallthrough to live tip)
        if not _mapping_has_product_key(overlay, name):
            return default
        return _read_from_mapping(name, overlay, default)

    tip = effective_tip()
    if _mapping_has_product_key(tip, name):
        return _read_from_mapping(name, tip, default)

    for lookup in product_env_lookup_names(name):
        value = os.environ.get(lookup)
        if value is None:
            continue
        return default if value == "" else value
    return default


def read_env(name: str, default: str = "") -> str:
    """Overlay-aware tip reader for hot-reload paths."""
    value = get_local_config(name, default or None)
    if value is None:
        return default
    text = str(value)
    return text if text else default


def read_env_if_set(name: str) -> str | None:
    """Return env value when *name* is explicitly set.

    Bound overlay (incl. ``{}``): only overlay; miss → ``None`` (seal).
    Unbound: formula B tip only.
    ``JIUWENCLAW_*`` / ``JIUWENSWARM_*`` aliases resolve interchangeably.
    """
    overlay = _task_env_overlay.get()
    if overlay is not _UNBOUND:
        for lookup in product_env_lookup_names(name):
            if lookup not in overlay:
                continue
            value = overlay[lookup]
            if value is None:
                return ""
            if isinstance(value, str):
                return decrypt(lookup, value)
            return str(value)
        return None

    tip = effective_tip()
    for lookup in product_env_lookup_names(name):
        if lookup not in tip:
            continue
        value = tip[lookup]
        if value is None:
            return ""
        if isinstance(value, str):
            return decrypt(lookup, value)
        return str(value)
    return None


def read_default_headers_raw() -> str:
    """Overlay-aware raw JSON string for default HTTP headers."""
    for env_key in _DEFAULT_HEADERS_ALIASES:
        raw = read_env(env_key, "")
        if raw.strip():
            return raw.strip()
    api_key = read_env("API_KEY", "").strip()
    if api_key and api_key != "huawei-maas-session":
        return ""
    for env_key in _DEFAULT_HEADERS_FALLBACK_ALIASES:
        raw = read_env(env_key, "")
        text = raw.strip()
        if text.startswith("{") and text.endswith("}"):
            return text
    return ""


def parse_default_headers(raw: str) -> dict[str, str] | None:
    """Parse and validate default_headers JSON; return None when empty."""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"default_headers is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("default_headers must be a JSON object")
    return {str(k): str(v) for k, v in parsed.items() if v is not None}


def read_default_headers() -> dict[str, str] | None:
    """Read overlay-aware default_headers as a header map."""
    return parse_default_headers(read_default_headers_raw())


def is_sensitive_env_name(name: str) -> bool:
    lower = name.lower()
    return (
        "api_key" in lower
        or "token" in lower
        or "secret" in lower
        or lower == DEFAULT_HEADERS_ENV_KEY
        or "header" in lower
    )


def set_local_config(name: str, value) -> None:
    """Legacy tip write for current ns (prefer :func:`set_os_environ`)."""
    if value is None or value == "":
        set_os_environ(name, None)
        return
    set_os_environ(name, value)


def decrypt(name, cipher):
    reg_mod = sys.modules.get("jiuwenswarm.extensions.registry")
    if reg_mod is not None and hasattr(reg_mod, "ExtensionRegistry"):
        try:
            crypto = reg_mod.ExtensionRegistry.get_instance().get_crypto_provider()
            if is_sensitive_env_name(name) and crypto:
                return crypto.decrypt(cipher)
        except Exception as e:
            logger.warning(f"Decryption failed exception: {e}")
    return cipher


def encrypt(name, text):
    reg_mod = sys.modules.get("jiuwenswarm.extensions.registry")
    if reg_mod is not None and hasattr(reg_mod, "ExtensionRegistry"):
        try:
            crypto = reg_mod.ExtensionRegistry.get_instance().get_crypto_provider()
            if is_sensitive_env_name(name) and crypto:
                return crypto.encrypt(text)
        except Exception as e:
            logger.warning(f"Encryption failed exception: {e}")
    return text


def reset_local_env_state_for_tests() -> None:
    """Clear bags + baseline + unbound overlay/ns ContextVars (unit tests only)."""
    global _mirrored_once
    _active_bags.clear()
    _staged_bags.clear()
    _process_baseline.clear()
    _mirrored_once = False
    # Best-effort: cannot fully reset ContextVar without tokens; set unbound.
    _task_env_overlay.set(_UNBOUND)
    _agent_env_ns.set(None)
    _invalidate_resolved_config_cache()
