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

DEFAULT_HEADERS_ENV_KEY = "default_headers"
_DEFAULT_HEADERS_ALIASES = (
    DEFAULT_HEADERS_ENV_KEY,
    "DEFAULT_HEADERS",
    "OPENAI_DEFAULT_HEADERS",
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Track A / mirror / P2 key tables (authoritative MVP inventories)
# ---------------------------------------------------------------------------

SPAWN_ENV_KEYS: frozenset[str] = frozenset(
    {
        "HOME",
        "JIUWENCLAW_DATA_DIR",
        "JIUWENCLAW_AGENT_ROOT",
        "PYTHONUNBUFFERED",
        "WEB_HOST",
        # Align with relay-claw launchEnv / sync_agents_configs shared_env (short names).
        "OFFICE_CLAW_MCP_SERVER_PATH",
        "OFFICE_CLAW_MCP_COMMAND",
        "OFFICE_CLAW_MCP_ARGS_JSON",
        "OFFICE_CLAW_MCP_CWD",
        "OFFICE_CLAW_MCP_EXCLUDED_TOOLS",
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
        "AGENT_RUNTIME",
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
        "JIUWENCLAW_DISABLED_SKILLS",
        "JIUWENCLAW_SHARED_SKILLS_DIRS",
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
        # Web config.set free-search flags (distinct from JIUWENCLAW_ENABLE_*)
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
        "JIUWENCLAW_ENABLE_DDG_SEARCH",
        "JIUWENCLAW_ENABLE_JINA_SEARCH",
        "JIUWENCLAW_ENABLE_JINA_FETCH",
        "JIUWENCLAW_SSL_VERIFY",
    }
)

PROCESS_UNIQUE_ENV_KEYS: frozenset[str] = frozenset()

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
    "jiuwenclaw_task_env_overlay", default=_UNBOUND
)
_agent_env_ns: ContextVar[EnvNsKey | None] = ContextVar(
    "jiuwenclaw_agent_env_ns", default=None
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


def iter_active_tip_agent_ids(service_id: str | None = None) -> list[str]:
    """Return agent_ids that currently have an active tip bag for ``service_id``.

    Used by team/model credential rematch when catalog registry is empty or
    incomplete. Order is insertion order of tip bags (not sorted).
    """
    sid = normalize_env_ns_id(
        service_id if service_id is not None else _DEFAULT_SERVICE_ID,
        default=_DEFAULT_SERVICE_ID,
    )
    seen: list[str] = []
    for bag_sid, bag_aid in _active_bags.keys():
        if bag_sid != sid:
            continue
        if bag_aid not in seen:
            seen.append(bag_aid)
    return seen


def _invalidate_resolved_config_cache(
    service_id: str | None = None,
    agent_id: str | None = None,
) -> None:
    """Drop get_config() resolved cache for this ns (lazy import avoids cycle)."""
    try:
        from jiuwenclaw.config import clear_config_cache
    except ImportError as e:
        logger.debug("clear_config_cache unavailable during import: %s", e)
        return
    clear_config_cache(service_id=service_id, agent_id=agent_id)


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
    for env_key, env_value in env_overrides.items():
        key = str(env_key)
        if key in SPAWN_ENV_KEYS:
            logger.warning("拒绝 stage 轨道 A 键: %s", key)
            continue
        if env_value is None:
            bag.pop(key, None)
        else:
            text = _stringify_env_value(env_value)
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


def _stringify_env_value(value: Any) -> str:
    """Serialize tip/env values so JSON objects stay valid JSON.

    ``str(dict)`` / ``str(list)`` emit Python repr with single quotes
    (``{'k': 'v'}``), which ``json.loads`` cannot parse. Hot-reload callers
    such as ``agent.reload_config`` pass ``default_headers`` as a dict.
    """
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def _plaintext_tip_value(name: str, value: Any) -> str:
    """Store tip values as plaintext (decrypt ciphertext from .env / legacy)."""
    text = _stringify_env_value(value)
    if not text:
        return text
    return str(decrypt(name, text))


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
        if key not in os.environ:
            continue
        os.environ.pop(key, None)
        removed.append(key)
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
    for env_key, env_value in env_overrides.items():
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
            value = _stringify_env_value(env_value)
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
        for env_key, env_value in env_overrides.items():
            name = str(env_key)
            if name in SPAWN_ENV_KEYS:
                continue
            if env_value is None:
                continue
            text = _stringify_env_value(env_value)
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
    for env_key in removals:
        name = str(env_key)
        active.pop(name, None)
        staged.pop(name, None)
        _pop_bare_if_default_default(sid, aid, name)
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
            for key, value in part.items():
                k = str(key)
                if value is None:
                    merged.pop(k, None)
                else:
                    text = _stringify_env_value(value)
                    if k in _EMPTY_OMIT_ENV_KEYS and not text.strip():
                        # Omit empty credentials from sealed overlay so they do not
                        # block fallthrough; do not actively clear a good tip value.
                        continue
                    merged[k] = text
    # Drop empty credential keys already present in tip so seal does not pin "".
    for k in _EMPTY_OMIT_ENV_KEYS:
        if k in merged and not str(merged.get(k) or "").strip():
            merged.pop(k, None)
    return merged


def bind_agent_env_ns(service_id: str, agent_id: str) -> Token:
    key = resolve_env_ns(service_id, agent_id)
    return _agent_env_ns.set(key)


def reset_agent_env_ns(token: Token) -> None:
    _agent_env_ns.reset(token)


def bind_task_env_overlay(overlay: dict[str, Any] | None) -> Token:
    """Bind task-scoped overlay. Always binds a dict (``None`` → ``{}``).

    Callers must not use truthiness of the return/overlay to skip bind.
    Use :func:`reset_task_env_overlay` to unbind.
    """
    bound: dict[str, Any] = {} if overlay is None else dict(overlay)
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
        return self._target()[str(key)]

    def __setitem__(self, key: str, value: Any) -> None:
        name = str(key)
        bag = self._target()
        if value is None:
            bag.pop(name, None)
        else:
            bag[name] = value

    def __delitem__(self, key: str) -> None:
        del self._target()[str(key)]

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
        for k, v in items:
            bag[str(k)] = v
        for k, v in kwargs.items():
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
        out[str(k)] = _stringify_env_value(v)
    for k in SPAWN_ENV_KEYS:
        if k in os.environ:
            out[k] = os.environ[k]
    for k in PROCESS_UNIQUE_ENV_KEYS:
        if k in os.environ:
            out[k] = os.environ[k]
    _ensure_windows_platform_env(out)
    return out


def export_spawn_environ() -> dict[str, str]:
    """Return only process/runtime variables safe for a child process env."""
    out: dict[str, str] = {}
    for k in SPAWN_ENV_KEYS:
        if k in os.environ:
            out[k] = os.environ[k]
    for k in PROCESS_UNIQUE_ENV_KEYS:
        if k in os.environ:
            out[k] = os.environ[k]
    _ensure_windows_platform_env(out)
    return out


def _ensure_windows_platform_env(out: dict[str, str]) -> None:
    """Pass through OS-level vars a Windows child process needs to function.

    The curated allowlist (B/A/C) only carries business + runtime config; it
    intentionally omits platform vars. On Windows, ``WSAStartup`` (called by
    ``import _overlapped`` -> ``asyncio``) loads the WinSock provider from
    ``%SystemRoot%\\System32``; if ``SYSTEMROOT`` is absent the provider init
    fails (WinError 10106) and the child cannot even ``import asyncio``.
    Copy these through from ``os.environ`` when present and not already set,
    so business/tip config always wins over the inherited OS value.
    """
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
        text = _stringify_env_value(env_value)
        if name in _EMPTY_OMIT_ENV_KEYS and not text.strip():
            continue
        _process_baseline[name] = _plaintext_tip_value(name, text)


def apply_process_baseline_gaps(
    service_id: str | None,
    agent_id: str | None,
    *,
    reserved_keys: Iterable[str] | None = None,
) -> None:
    """Copy baseline keys not in ``reserved_keys`` into the agent tip.

    Does not overwrite keys already present on the tip. ``reserved_keys`` should
    be the raw ``agents[].env`` key set (including ``null`` entries) so explicit
    deletes are not resurrected from baseline.
    """
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
    """True for local processes; False when relay sets ``AGENT_RUNTIME``."""
    return not str(os.environ.get("AGENT_RUNTIME", "") or "").strip()


_LEGACY_OFFICE_CLAW_DISABLE_TOOL_CALLING = "OFFICE_CLAW_DISABLE_TOOL_CALLING"
_LEGACY_OFFICE_CLAW_DISABLE_TRUTHY = frozenset({"1", "true", "yes", "on"})
_LEGACY_TOOL_CALLING_GUARD_STRIP_REASON = "legacy_office_claw_disable_tool_calling"


def _map_legacy_office_claw_disable_tool_calling() -> bool:
    """Map deprecated ``OFFICE_CLAW_DISABLE_TOOL_CALLING`` into Guard tip keys.

    Old kill-switch stripped tools from process env alone. New Guard needs
    ``TOOL_CALLING_GUARD_ENABLED`` + ``TOOL_CALLING_GUARD_DISABLE``. Uses
    ``os.environ.setdefault`` so explicit new Guard env wins. Always pops the
    legacy key. Baseline/tip ingest is left to the caller.
    """
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
    """After ``load_dotenv``: bare Track B → process_baseline, then pop bare.

    Idempotent unless ``force=True``. ``setdefault`` on baseline so a later
    sync/gaps path remains authoritative for per-agent tips. When
    ``AGENT_RUNTIME`` is unset, also hydrates ``default/default`` tip for local
    cold-start readers.
    """
    global _mirrored_once
    legacy_mapped = _map_legacy_office_claw_disable_tool_calling()
    if _mirrored_once and not force:
        # Secondary load_dotenv may have re-seeded bare keys — always re-pop.
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
        # Legacy map may have added Guard keys to baseline after first ingest.
        if legacy_mapped and should_hydrate_default_tip():
            hydrate_default_tip_from_baseline()
        return
    for key in BUSINESS_MIRROR_KEYS:
        if key in SPAWN_ENV_KEYS:
            continue
        if key not in os.environ:
            continue
        raw = os.environ[key]
        if key in _EMPTY_OMIT_ENV_KEYS and not str(raw).strip():
            os.environ.pop(key, None)
            continue
        plain = _plaintext_tip_value(key, raw)
        _process_baseline.setdefault(key, plain)
        os.environ.pop(key, None)
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


def _read_from_mapping(name: str, mapping: dict[str, Any], default: Any = None) -> Any:
    if name not in mapping:
        return default
    value = mapping[name]
    if value is None or value == "":
        return default
    return decrypt(name, value) if isinstance(value, str) else value


def get_local_config(name: str, default=None):
    """Track-B reader: bound overlay (seal) → formula B tip. No os.environ."""
    if name in SPAWN_ENV_KEYS:
        logger.warning(
            "get_local_config 不服务轨道 A 键 %s —— 请直读 spawn/path API", name
        )
        return default

    overlay = _task_env_overlay.get()
    if overlay is not _UNBOUND:
        # Seal: miss => unset (no fallthrough to live tip)
        return _read_from_mapping(name, overlay, default)

    tip = effective_tip()
    if name in tip:
        return _read_from_mapping(name, tip, default)
    return default


def read_env(name: str, default: str = "") -> str:
    """Overlay-aware tip reader for hot-reload paths."""
    value = get_local_config(name, default or None)
    if value is None:
        return default
    text = _stringify_env_value(value)
    return text if text else default


def read_env_if_set(name: str) -> str | None:
    """Return env value when *name* is explicitly set.

    Bound overlay (incl. ``{}``): only overlay; miss → ``None`` (seal).
    Unbound: formula B tip only.
    """
    overlay = _task_env_overlay.get()
    if overlay is not _UNBOUND:
        if name not in overlay:
            return None
        value = overlay[name]
        if value is None:
            return ""
        if isinstance(value, str):
            return decrypt(name, value)
        return _stringify_env_value(value)

    tip = effective_tip()
    if name in tip:
        value = tip[name]
        if value is None:
            return ""
        if isinstance(value, str):
            return decrypt(name, value)
        return _stringify_env_value(value)
    return None


def read_default_headers_raw() -> str:
    """Overlay-aware raw JSON string for default HTTP headers."""
    for env_key in _DEFAULT_HEADERS_ALIASES:
        raw = read_env(env_key, "")
        if raw.strip():
            return raw.strip()
    return ""


def parse_default_headers(raw: str | dict[str, Any] | None) -> dict[str, str] | None:
    """Parse and validate default_headers JSON; return None when empty.

    Accepts a JSON object string or an already-decoded dict. Overlay / reload
    paths may bind ``default_headers`` as a mapping before it is stringified.
    """
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items() if v is not None}
    if raw is None:
        return None
    text = raw.strip() if isinstance(raw, str) else _stringify_env_value(raw).strip()
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
    set_os_environ(name, value if value else None)


def decrypt(name, cipher):
    reg_mod = sys.modules.get("jiuwenclaw.extensions.registry")
    if reg_mod is not None and hasattr(reg_mod, "ExtensionRegistry"):
        try:
            crypto = reg_mod.ExtensionRegistry.get_instance().get_crypto_provider()
            if is_sensitive_env_name(name) and crypto:
                return crypto.decrypt(cipher)
        except Exception as e:
            logger.warning(f"Decryption failed exception: {e}")
    return cipher


def encrypt(name, text):
    reg_mod = sys.modules.get("jiuwenclaw.extensions.registry")
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
