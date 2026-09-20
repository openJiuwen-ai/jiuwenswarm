# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentServer-side tokenizer cache warm-up service.

The Gateway only persists model configuration and sends a reload notification.
Tokenizer resolution belongs to the AgentServer process because that is where
the context engine runs and where the downloaded files will be consumed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlunsplit

from jiuwenswarm.common.config import get_config, get_default_models
from jiuwenswarm.common.utils import get_user_workspace_dir

logger = logging.getLogger(__name__)

_DEFAULT_CACHE_DIR = Path("tokenizers")
_NATIVE_WARM_SOURCES = frozenset({"native_tokenizer", "family_tokenizer_fallback"})
_MODEL_VARIANT_SEPARATORS = frozenset({"_", "-", ":", "."})
_HUGGINGFACE_ENDPOINT = urlunsplit(("https", "hf-mirror.com", "", "", ""))
_TOKENIZER_METADATA_TIMEOUT_SECONDS = 10.0
_TOKENIZER_METADATA_CACHE_MAX_ENTRIES = 256
_TOKENIZER_METADATA_CACHE_TTL_SECONDS = 24 * 60 * 60
_TOKENIZER_METADATA_CACHE: OrderedDict[
    tuple[str, str | None], tuple[float, dict[str, Any]]
] = OrderedDict()
_TOKENIZER_METADATA_CACHE_LOCK = threading.Lock()
_NON_FAMILY_TAGS = frozenset(
    {
        "transformers",
        "safetensors",
        "text-generation",
        "conversational",
        "pytorch",
    }
)

# These are model-vendor identities, not API client providers. A model may be
# served through an OpenAI-compatible or another provider adapter, so the
# mapping deliberately resolves by model name and then records the configured
# provider on the generated spec.


@dataclass(frozen=True)
class _DefaultTokenizerRepository:
    """Built-in tokenizer repository and its single family fallback."""

    base: str
    tokenizer_id: str
    engine: str = "tokenizers"
    fallback: tuple[str, str, str] | None = None


_DEFAULT_TOKENIZER_REPOSITORIES: tuple[_DefaultTokenizerRepository, ...] = (
    _DefaultTokenizerRepository(
        "glm-5.2",
        "zai-org/GLM-5.2",
        fallback=("glm-5", "zai-org/GLM-5", "tokenizers"),
    ),
    _DefaultTokenizerRepository(
        "glm-5.1",
        "zai-org/GLM-5.1",
        fallback=("glm-5", "zai-org/GLM-5", "tokenizers"),
    ),
    _DefaultTokenizerRepository("glm-5", "zai-org/GLM-5"),
    _DefaultTokenizerRepository(
        "zai-org/glm-5.2",
        "zai-org/GLM-5.2",
        fallback=("glm-5", "zai-org/GLM-5", "tokenizers"),
    ),
    _DefaultTokenizerRepository(
        "zai-org/glm-5.1",
        "zai-org/GLM-5.1",
        fallback=("glm-5", "zai-org/GLM-5", "tokenizers"),
    ),
    _DefaultTokenizerRepository("zai-org/glm-5", "zai-org/GLM-5"),
    _DefaultTokenizerRepository(
        "deepseek-v4-pro",
        "deepseek-ai/DeepSeek-V4-Pro",
        fallback=(
            "deepseek-v4-flash",
            "deepseek-ai/DeepSeek-V4-Flash",
            "tokenizers",
        ),
    ),
    _DefaultTokenizerRepository(
        "deepseek-v4-flash",
        "deepseek-ai/DeepSeek-V4-Flash",
    ),
    _DefaultTokenizerRepository(
        "deepseek-ai/deepseek-v4-pro",
        "deepseek-ai/DeepSeek-V4-Pro",
        fallback=(
            "deepseek-v4-flash",
            "deepseek-ai/DeepSeek-V4-Flash",
            "tokenizers",
        ),
    ),
    _DefaultTokenizerRepository(
        "deepseek-ai/deepseek-v4-flash",
        "deepseek-ai/DeepSeek-V4-Flash",
    ),
    _DefaultTokenizerRepository(
        "qwen/qwen3.8-27b",
        "Qwen/Qwen3.8-27B",
        fallback=("qwen3-8b", "Qwen/Qwen3-8B", "tokenizers"),
    ),
    _DefaultTokenizerRepository(
        "qwen/qwen3-8b",
        "Qwen/Qwen3-8B",
    ),
    _DefaultTokenizerRepository(
        "qwen3.8",
        "Qwen/Qwen3.8-27B",
        fallback=("qwen3-8b", "Qwen/Qwen3-8B", "tokenizers"),
    ),
    # Kimi publishes model-native ``tiktoken.model`` files. Each configured
    # version is attempted first; the family uses one fixed K2.7 fallback and
    # never walks a version-by-version fallback chain.
    _DefaultTokenizerRepository(
        "moonshotai/kimi-k2.7-code",
        "moonshotai/Kimi-K2.7-Code",
        engine="tiktoken",
    ),
    _DefaultTokenizerRepository(
        "kimi-k2.7-code",
        "moonshotai/Kimi-K2.7-Code",
        engine="tiktoken",
    ),
    _DefaultTokenizerRepository(
        "moonshotai/kimi-k2.7",
        "moonshotai/Kimi-K2.7-Code",
        engine="tiktoken",
    ),
    _DefaultTokenizerRepository(
        "kimi-k2.7",
        "moonshotai/Kimi-K2.7-Code",
        engine="tiktoken",
    ),
    _DefaultTokenizerRepository(
        "moonshotai/kimi-k2.6",
        "moonshotai/Kimi-K2.6",
        engine="tiktoken",
        fallback=("kimi-k2.7", "moonshotai/Kimi-K2.7-Code", "tiktoken"),
    ),
    _DefaultTokenizerRepository(
        "kimi-k2.6",
        "moonshotai/Kimi-K2.6",
        engine="tiktoken",
        fallback=("kimi-k2.7", "moonshotai/Kimi-K2.7-Code", "tiktoken"),
    ),
    _DefaultTokenizerRepository(
        "moonshotai/kimi-k2.5",
        "moonshotai/Kimi-K2.5",
        engine="tiktoken",
        fallback=("kimi-k2.7", "moonshotai/Kimi-K2.7-Code", "tiktoken"),
    ),
    _DefaultTokenizerRepository(
        "kimi-k2.5",
        "moonshotai/Kimi-K2.5",
        engine="tiktoken",
        fallback=("kimi-k2.7", "moonshotai/Kimi-K2.7-Code", "tiktoken"),
    ),
    _DefaultTokenizerRepository(
        "moonshotai/kimi-k2-instruct",
        "moonshotai/Kimi-K2-Instruct",
        engine="tiktoken",
        fallback=("kimi-k2.7", "moonshotai/Kimi-K2.7-Code", "tiktoken"),
    ),
    _DefaultTokenizerRepository(
        "kimi-k2-instruct",
        "moonshotai/Kimi-K2-Instruct",
        engine="tiktoken",
        fallback=("kimi-k2.7", "moonshotai/Kimi-K2.7-Code", "tiktoken"),
    ),
    _DefaultTokenizerRepository(
        "moonshotai/kimi-k2",
        "moonshotai/Kimi-K2-Instruct",
        engine="tiktoken",
        fallback=("kimi-k2.7", "moonshotai/Kimi-K2.7-Code", "tiktoken"),
    ),
    _DefaultTokenizerRepository(
        "kimi-k2",
        "moonshotai/Kimi-K2-Instruct",
        engine="tiktoken",
        fallback=("kimi-k2.7", "moonshotai/Kimi-K2.7-Code", "tiktoken"),
    ),
    _DefaultTokenizerRepository(
        "moonshotai/kimi-k3",
        "moonshotai/Kimi-K3",
        engine="tiktoken",
        fallback=("kimi-k2.7", "moonshotai/Kimi-K2.7-Code", "tiktoken"),
    ),
    _DefaultTokenizerRepository(
        "kimi-k3",
        "moonshotai/Kimi-K3",
        engine="tiktoken",
        fallback=("kimi-k2.7", "moonshotai/Kimi-K2.7-Code", "tiktoken"),
    ),
)
_TOKENIZER_WARMUP_MAX_ATTEMPTS = 3
_TOKENIZER_WARMUP_RETRY_DELAYS_SECONDS = (1.0, 3.0)


@dataclass(frozen=True)
class TokenizerProfile:
    """One model profile that may need a tokenizer warm-up."""

    provider: str
    model: str
    spec: dict[str, Any] | None = None
    allow_metadata_discovery: bool = False


@dataclass(frozen=True)
class TokenizerWarmupSettings:
    """Resolved policy used by the AgentServer tokenizer warm-up service."""

    enabled: bool
    cache_dir: Path
    offline: bool
    registry: tuple[dict[str, Any], ...]


def _as_bool(value: Any, default: bool = False) -> bool:
    """Parse boolean-like configuration values without truthiness surprises."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = str(value).strip().casefold()
    if normalized in {"1", "true", "yes", "on", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def _context_engine_config(config: dict[str, Any]) -> dict[str, Any]:
    """Return the nested context-engine configuration when it is present."""
    react = config.get("react") if isinstance(config, dict) else None
    if not isinstance(react, dict):
        return {}
    context_config = react.get("context_engine_config")
    return context_config if isinstance(context_config, dict) else {}


def _model_variant_match(base: str, requested: str) -> bool:
    """Match a delimited model variant without matching arbitrary substrings."""
    return (
        bool(base)
        and requested != base
        and requested.startswith(base)
        and len(requested) > len(base)
        and requested[len(base)] in _MODEL_VARIANT_SEPARATORS
    )


def _default_tokenizer_spec(*, provider: str, model: str) -> dict[str, Any] | None:
    """Infer a trusted tokenizer source for known models or repo-shaped IDs.

    The configured API model name remains the source of truth for selecting the
    model client. This helper only supplies a tokenizer artifact identity when
    it is deterministic: a known vendor model family or an explicit
    ``org/model`` repository-shaped name. Arbitrary aliases are left unresolved.
    """
    normalized_model = model.strip().casefold()
    if not normalized_model:
        return None

    candidates: list[tuple[int, _DefaultTokenizerRepository]] = []
    for repository in _DEFAULT_TOKENIZER_REPOSITORIES:
        if normalized_model == repository.base or _model_variant_match(
            repository.base, normalized_model
        ):
            candidates.append((len(repository.base), repository))
    if candidates:
        _, repository = max(candidates, key=lambda item: item[0])
        spec: dict[str, Any] = {
            "provider": provider,
            "model": repository.base,
            "id": repository.tokenizer_id,
            "source": "huggingface",
        }
        if repository.engine != "auto":
            spec["engine"] = repository.engine
        if repository.fallback is not None:
            fallback_model, fallback_id, fallback_engine = repository.fallback
            spec["compatible_fallbacks"] = [
                {
                    "model": fallback_model,
                    "id": fallback_id,
                    "source": "huggingface",
                    "engine": fallback_engine,
                }
            ]
        return spec

    # A slash-delimited model name is already in the canonical repository form.
    # Keep this inference limited to HuggingFace-compatible IDs; plain aliases
    # such as ``glm-5.2`` must use the controlled mapping above.
    if "/" in model and not model.startswith(("/", "./", "../")):
        return {
            "provider": provider,
            "model": model,
            "id": model,
            "source": "huggingface",
        }
    return None


def _is_huggingface_repo_id(value: Any) -> bool:
    """Return whether ``value`` is a safe Hugging Face ``org/model`` ID."""
    if not isinstance(value, str):
        return False
    candidate = value.strip()
    if not candidate or "://" in candidate or candidate.startswith((".", "/")):
        return False
    parts = candidate.split("/")
    if len(parts) != 2:
        return False
    for part in parts:
        if not part or part in {".", ".."}:
            return False
    return True


def _repo_id_from_metadata(value: Any) -> str | None:
    """Extract one repository ID from HF ``base_model`` metadata."""
    if isinstance(value, (list, tuple)):
        for item in value:
            repo_id = _repo_id_from_metadata(item)
            if repo_id:
                return repo_id
        return None
    if isinstance(value, dict):
        for key in ("id", "repo_id", "model", "name", "base_model"):
            repo_id = _repo_id_from_metadata(value.get(key))
            if repo_id:
                return repo_id
        return None
    if _is_huggingface_repo_id(value):
        return str(value).strip()
    return None


def _tokenizer_engine_from_info(info: Any) -> str | None:
    """Detect the native tokenizer format exposed by repository metadata."""
    siblings = getattr(info, "siblings", None) or []
    for sibling in siblings:
        filename = getattr(sibling, "rfilename", None)
        if not filename:
            continue
        filename = Path(str(filename)).name.casefold()
        if filename == "tiktoken.model":
            return "tiktoken"
        if filename == "tokenizer.json":
            return "tokenizers"
    return None


def _first_model_family_tag(tags: set[str]) -> str | None:
    """Choose a stable family tag when repository config has no model type."""
    for tag in sorted(tags):
        if tag and tag not in _NON_FAMILY_TAGS:
            return tag
    return None


def _same_family_repository(
    api: Any,
    repo_id: str,
    *,
    family_key: str | None,
) -> str | None:
    """Find one tokenizer-bearing sibling in the same namespace and family."""
    if not family_key:
        return None
    namespace = repo_id.split("/", 1)[0]
    try:
        try:
            candidates = api.list_models(
                author=namespace,
                filter=family_key,
                sort="downloads",
                limit=20,
                full=True,
                fetch_config=True,
            )
        except TypeError:
            candidates = api.list_models(
                author=namespace,
                search=family_key,
                limit=20,
            )
        for candidate in candidates:
            candidate_id = str(getattr(candidate, "id", None) or "").strip()
            if not _is_huggingface_repo_id(candidate_id):
                continue
            if candidate_id.casefold() == repo_id.casefold():
                continue
            candidate_engine = _tokenizer_engine_from_info(candidate)
            if candidate_engine is None:
                continue
            candidate_config = getattr(candidate, "config", None)
            candidate_config = (
                candidate_config if isinstance(candidate_config, dict) else {}
            )
            candidate_type = str(candidate_config.get("model_type") or "").casefold()
            candidate_tags: set[str] = set()
            for tag in getattr(candidate, "tags", None) or []:
                candidate_tags.add(str(tag).strip().casefold())
            if candidate_type and candidate_type != family_key.casefold():
                continue
            if not candidate_type and family_key.casefold() not in candidate_tags:
                continue
            return candidate_id
    except (
        AttributeError,
        ImportError,
        KeyError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        logger.debug(
            "[TokenizerService] same-family tokenizer discovery failed for %s: %s",
            repo_id,
            exc,
        )
    return None


def _huggingface_repository_metadata(
    repo_id: str,
    *,
    revision: str | None = None,
    allow_network: bool,
) -> dict[str, Any] | None:
    """Read tokenizer/base-model metadata from HF mirror during warm-up.

    This function deliberately has an explicit ``allow_network`` parameter.
    The AgentServer warm-up passes ``True``; ContextEngine integration only
    reads the process cache with ``False`` and therefore cannot trigger a
    network request while a context is being created.
    """
    if not _is_huggingface_repo_id(repo_id):
        return None
    cache_key = (repo_id, revision)
    with _TOKENIZER_METADATA_CACHE_LOCK:
        cached = _TOKENIZER_METADATA_CACHE.get(cache_key)
        if cached is not None:
            cached_at, cached_metadata = cached
            if time.monotonic() - cached_at < _TOKENIZER_METADATA_CACHE_TTL_SECONDS:
                _TOKENIZER_METADATA_CACHE.move_to_end(cache_key)
                return dict(cached_metadata)
            del _TOKENIZER_METADATA_CACHE[cache_key]
    if not allow_network:
        return None

    metadata: dict[str, Any] | None = None
    try:
        from huggingface_hub import HfApi

        # Keep the mirror endpoint scoped to this API instance.  In
        # particular, do not call huggingface_hub.set_client_factory(), which
        # changes the HTTP client used by unrelated Hugging Face consumers in
        # the AgentServer process.
        api = HfApi(endpoint=_HUGGINGFACE_ENDPOINT)
        try:
            info = api.model_info(
                repo_id,
                revision=revision,
                timeout=_TOKENIZER_METADATA_TIMEOUT_SECONDS,
            )
        except TypeError:
            # Keep compatibility with older huggingface_hub releases that do
            # not expose the timeout keyword on ``model_info``.
            info = api.model_info(repo_id, revision=revision)

        engine = _tokenizer_engine_from_info(info)

        card_data = getattr(info, "cardData", None)
        card_base_model = (
            card_data.get("base_model")
            if isinstance(card_data, dict)
            else getattr(card_data, "base_model", None)
        )
        config = getattr(info, "config", None)
        config = config if isinstance(config, dict) else {}
        base_model = _repo_id_from_metadata(getattr(info, "base_model", None))
        if base_model is None:
            base_model = _repo_id_from_metadata(card_base_model)
        if base_model is None:
            base_model = _repo_id_from_metadata(config.get("base_model"))
        if base_model is None:
            base_model = _repo_id_from_metadata(config.get("base_model_name_or_path"))
        if base_model and base_model.casefold() == repo_id.casefold():
            base_model = None

        model_type = str(config.get("model_type") or "").strip()
        tags: set[str] = set()
        for tag in getattr(info, "tags", None) or []:
            tags.add(str(tag).strip().casefold())
        family_key = model_type or _first_model_family_tag(tags)
        same_family = None
        if base_model is None:
            same_family = _same_family_repository(
                api,
                repo_id,
                family_key=family_key,
            )

        metadata = {
            "repo_id": str(getattr(info, "id", None) or repo_id),
            "engine": engine,
            "base_model": base_model,
            "same_family": same_family,
        }
    except (
        AttributeError,
        ImportError,
        KeyError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        logger.debug(
            "[TokenizerService] tokenizer metadata discovery failed for %s: %s",
            repo_id,
            exc,
        )

    # Keep successful metadata (including a successful response with no
    # tokenizer/base-model fields), but do not permanently cache a transient
    # network/import failure. A later model reload can then retry discovery.
    if metadata is not None:
        with _TOKENIZER_METADATA_CACHE_LOCK:
            _TOKENIZER_METADATA_CACHE[cache_key] = (time.monotonic(), dict(metadata))
            _TOKENIZER_METADATA_CACHE.move_to_end(cache_key)
            while len(_TOKENIZER_METADATA_CACHE) > _TOKENIZER_METADATA_CACHE_MAX_ENTRIES:
                _TOKENIZER_METADATA_CACHE.popitem(last=False)
    return dict(metadata) if metadata is not None else None


def _discover_tokenizer_spec(
    profile: TokenizerProfile,
    *,
    allow_network: bool,
) -> dict[str, Any] | None:
    """Enrich an inferred repository spec with its engine and one fallback."""
    spec = profile.spec
    if not profile.allow_metadata_discovery or not isinstance(spec, dict):
        return spec
    if str(spec.get("source") or "").strip().casefold() != "huggingface":
        return spec
    tokenizer_id = str(spec.get("id") or spec.get("tokenizer_id") or "").strip()
    if not _is_huggingface_repo_id(tokenizer_id):
        return spec

    metadata = _huggingface_repository_metadata(
        tokenizer_id,
        revision=spec.get("revision"),
        allow_network=allow_network,
    )
    if metadata is None:
        return spec

    enriched = dict(spec)
    detected_engine = metadata.get("engine")
    configured_engine = str(enriched.get("engine") or "auto").strip().casefold()
    if detected_engine and configured_engine == "auto":
        enriched["engine"] = detected_engine

    existing_fallbacks = enriched.get("compatible_fallbacks")
    if existing_fallbacks:
        return enriched

    base_model = metadata.get("base_model") or metadata.get("same_family")
    if not isinstance(base_model, str) or not _is_huggingface_repo_id(base_model):
        return enriched

    fallback_metadata = _huggingface_repository_metadata(
        base_model,
        allow_network=allow_network,
    )
    fallback_engine = (
        fallback_metadata.get("engine")
        if fallback_metadata is not None and fallback_metadata.get("engine")
        else "auto"
    )
    enriched["compatible_fallbacks"] = [
        {
            "model": base_model,
            "id": base_model,
            "source": "huggingface",
            "engine": fallback_engine,
        }
    ]
    return enriched


def resolve_tokenizer_cache_dir(config: dict[str, Any] | None = None) -> Path:
    """Resolve the single cache directory shared by Swarm and agent-core.

    A configured relative path is rooted in the JiuwenSwarm data directory;
    an unset value defaults to ``~/.jiuwenswarm/tokenizers`` (or the
    directory selected by ``JIUWENSWARM_DATA_DIR``).
    """
    effective_config = config if isinstance(config, dict) else {}
    context_config = _context_engine_config(effective_config)
    raw_dir = context_config.get("tokenizer_cache_dir")
    if raw_dir in (None, ""):
        raw_dir = os.getenv("JIUWENSWARM_TOKENIZER_CACHE_DIR")

    if raw_dir in (None, ""):
        return get_user_workspace_dir() / _DEFAULT_CACHE_DIR

    path = Path(str(raw_dir)).expanduser()
    if not path.is_absolute():
        path = get_user_workspace_dir() / path
    return path


def tokenizer_warmup_settings(
    config: dict[str, Any] | None = None,
) -> TokenizerWarmupSettings:
    """Extract the AgentServer tokenizer policy from the resolved config."""
    effective_config = config if isinstance(config, dict) else get_config()
    context_config = _context_engine_config(effective_config)
    registry = context_config.get("tokenizer_registry")
    normalized_registry: tuple[dict[str, Any], ...] = ()
    if isinstance(registry, list):
        registry_items: list[dict[str, Any]] = []
        for item in registry:
            if isinstance(item, dict):
                registry_items.append(dict(item))
        normalized_registry = tuple(registry_items)
    return TokenizerWarmupSettings(
        enabled=_as_bool(context_config.get("enable_tiktoken_counter"), default=False),
        cache_dir=resolve_tokenizer_cache_dir(effective_config),
        offline=_as_bool(context_config.get("tokenizer_offline"), default=False),
        registry=normalized_registry,
    )


def _entry_tokenizer_spec(
    entry: dict[str, Any],
    *,
    provider: str,
    model: str,
    infer_defaults: bool = True,
) -> dict[str, Any] | None:
    """Read tokenizer metadata declared alongside a model profile.

    Both ``tokenizer`` and ``tokenizer_spec`` are accepted so existing model
    profile naming conventions can be used. Flat ``tokenizer_id``,
    ``tokenizer_source``, ``tokenizer_path``, ``tokenizer_engine``, and
    ``tokenizer_family`` fields are accepted as well. A string is treated as
    the tokenizer id; a mapping is passed through to agent-core.
    """
    model_client_config = entry.get("model_client_config")
    candidates: list[Any] = [entry.get("tokenizer"), entry.get("tokenizer_spec")]
    if isinstance(model_client_config, dict):
        candidates.extend(
            [
                model_client_config.get("tokenizer"),
                model_client_config.get("tokenizer_spec"),
            ]
        )
    raw_spec = None
    for candidate in candidates:
        if candidate is not None:
            raw_spec = candidate
            break
    if raw_spec is None:
        # Accept tokenizer metadata alongside the model client without forcing
        # callers to construct a nested TokenizerSpec object. Explicit
        # metadata is preferred; only known model families and
        # repository-shaped IDs are inferred below.
        metadata_owner = (
            model_client_config if isinstance(model_client_config, dict) else {}
        )
        tokenizer_id = entry.get("tokenizer_id") or metadata_owner.get("tokenizer_id")
        tokenizer_path = entry.get("tokenizer_path") or metadata_owner.get(
            "tokenizer_path"
        )
        model_path = entry.get("model_path") or metadata_owner.get("model_path")
        tokenizer_source = entry.get("tokenizer_source") or metadata_owner.get(
            "tokenizer_source"
        )
        tokenizer_engine = entry.get("tokenizer_engine") or metadata_owner.get(
            "tokenizer_engine"
        )
        tokenizer_family = entry.get("tokenizer_family") or metadata_owner.get(
            "tokenizer_family"
        )
        local_path = tokenizer_path or model_path
        if local_path:
            raw_spec = {"source": "local", "artifact_path": local_path}
        elif Path(model).expanduser().exists():
            raw_spec = {"source": "local", "artifact_path": model}
        elif tokenizer_id:
            raw_spec = {"id": tokenizer_id}
        elif tokenizer_source:
            # A source without an ID is useful only when the model name itself
            # is a valid provider repository ID (for example org/model).
            raw_spec = {"id": model, "source": tokenizer_source}
        elif infer_defaults:
            raw_spec = _default_tokenizer_spec(provider=provider, model=model)
        if raw_spec is not None:
            if tokenizer_source and "source" not in raw_spec:
                raw_spec["source"] = tokenizer_source
            if tokenizer_engine:
                raw_spec["engine"] = tokenizer_engine
            if tokenizer_family:
                raw_spec["family"] = tokenizer_family
    if raw_spec is None:
        return None
    if isinstance(raw_spec, str):
        raw_spec = {"id": raw_spec}
    if not isinstance(raw_spec, dict):
        return None
    spec = dict(raw_spec)
    spec.setdefault("provider", provider)
    spec.setdefault("model", model)
    return spec


def _is_repository_shaped_model(model: str) -> bool:
    """Return whether a model name can be used as a repository identifier."""
    return "/" in model and not model.startswith(("/", "./", "../"))


def _matches_builtin_model(model: str) -> bool:
    """Return whether ``model`` already has a deterministic built-in mapping."""
    normalized_model = model.strip().casefold()
    for repository in _DEFAULT_TOKENIZER_REPOSITORIES:
        if normalized_model == repository.base:
            return True
        if _model_variant_match(repository.base, normalized_model):
            return True
    return False


def configured_tokenizer_profiles(
    config: dict[str, Any] | None = None,
) -> list[TokenizerProfile]:
    """Return distinct text-model profiles from ``models.defaults``.

    The helper deliberately does not construct an LLM client, so warming a
    tokenizer cannot make an API request or require model credentials.
    """
    effective_config = config if isinstance(config, dict) else get_config()
    context_config = _context_engine_config(effective_config)
    raw_registry = context_config.get("tokenizer_registry")
    registry_specs: list[dict[str, Any]] = []
    if isinstance(raw_registry, list):
        for item in raw_registry:
            if isinstance(item, dict):
                registry_specs.append(dict(item))
    tokenizer_registry = None
    if registry_specs:
        try:
            from openjiuwen.core.context_engine import TokenizerRegistry

            tokenizer_registry = TokenizerRegistry(registry_specs)
        except (
            AttributeError,
            ImportError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            logger.debug(
                "[TokenizerService] tokenizer registry initialization skipped: %s",
                exc,
            )
            tokenizer_registry = None

    def resolve_profile_spec(
        entry: dict[str, Any],
        *,
        provider: str,
        model: str,
    ) -> tuple[dict[str, Any] | None, bool]:
        """Resolve explicit, registered, and built-in model tokenizer metadata."""
        # Explicit per-model metadata wins over the registry. For models with
        # no explicit metadata, a user registry entry wins over built-in
        # vendor mappings, then the automatic mapping is attempted.
        spec = _entry_tokenizer_spec(
            entry,
            provider=provider,
            model=model,
            infer_defaults=False,
        )
        if spec is not None:
            return spec, False
        if tokenizer_registry is not None:
            try:
                match = tokenizer_registry.resolve_match(provider, model)
                if match is not None:
                    return match.spec.model_dump(mode="json", by_alias=True), False
            except (
                AttributeError,
                ImportError,
                KeyError,
                TypeError,
                ValueError,
            ) as exc:
                logger.debug(
                    "[TokenizerService] tokenizer registry lookup skipped for %s: %s",
                    model,
                    exc,
                )
        spec = _default_tokenizer_spec(provider=provider, model=model)
        # Built-in mappings are already deterministic. Metadata discovery is
        # reserved for an unknown, repository-shaped model ID so aliases do
        # not trigger fuzzy or accidental mirror requests.
        allow_discovery = bool(
            spec
            and _is_repository_shaped_model(model)
            and not _matches_builtin_model(model)
        )
        return spec, allow_discovery

    profiles: list[TokenizerProfile] = []
    seen: set[str] = set()
    for entry in get_default_models(effective_config):
        if not isinstance(entry, dict):
            continue
        model_client_config = entry.get("model_client_config")
        model_client_config = (
            model_client_config if isinstance(model_client_config, dict) else {}
        )
        model = str(
            model_client_config.get("model_name") or entry.get("model_name") or ""
        ).strip()
        if not model:
            continue
        provider = str(
            model_client_config.get("client_provider")
            or model_client_config.get("provider")
            or entry.get("provider")
            or ""
        ).strip()
        spec, allow_discovery = resolve_profile_spec(
            entry,
            provider=provider,
            model=model,
        )
        if allow_discovery:
            spec = _discover_tokenizer_spec(
                TokenizerProfile(
                    provider=provider,
                    model=model,
                    spec=spec,
                    allow_metadata_discovery=True,
                ),
                allow_network=False,
            )
        identity = json.dumps(
            {
                "provider": provider.casefold(),
                "model": model.casefold(),
                "spec": _identity_value(spec),
            },
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
        if identity in seen:
            continue
        seen.add(identity)
        profiles.append(
            TokenizerProfile(
                provider=provider,
                model=model,
                spec=spec,
                allow_metadata_discovery=allow_discovery,
            )
        )

    # A small legacy configuration can define only ``react.model_name``. Keep
    # that form warmable as well; normal installations use models.defaults.
    if not profiles:
        react = (
            effective_config.get("react")
            if isinstance(effective_config, dict)
            else None
        )
        react = react if isinstance(react, dict) else {}
        model = str(react.get("model_name") or "").strip()
        if model:
            model_client_config = react.get("model_client_config")
            model_client_config = (
                model_client_config if isinstance(model_client_config, dict) else {}
            )
            provider = str(
                react.get("model_provider")
                or model_client_config.get("client_provider")
                or ""
            ).strip()
            spec, allow_discovery = resolve_profile_spec(
                react,
                provider=provider,
                model=model,
            )
            if allow_discovery:
                spec = _discover_tokenizer_spec(
                    TokenizerProfile(
                        provider=provider,
                        model=model,
                        spec=spec,
                        allow_metadata_discovery=True,
                    ),
                    allow_network=False,
                )
            profiles.append(
                TokenizerProfile(
                    provider=provider,
                    model=model,
                    spec=spec,
                    allow_metadata_discovery=allow_discovery,
                )
            )
    return profiles


def _stable_key(
    profile: TokenizerProfile,
    settings: TokenizerWarmupSettings,
) -> str:
    tokenizer_identity: Any = _identity_value(profile.spec)
    resolved_from_registry = False
    if profile.spec is None and settings.registry:
        # A model variant and its registered base model share the same
        # artifact. Use the resolved spec for service-level deduplication so a
        # reload does not schedule a second download for the same tokenizer.
        try:
            from openjiuwen.core.context_engine import TokenizerRegistry

            match = TokenizerRegistry(settings.registry).resolve_match(
                profile.provider,
                profile.model,
            )
            if match is not None:
                tokenizer_identity = _identity_value(
                    match.spec.model_dump(mode="json", by_alias=True)
                )
                resolved_from_registry = True
        except (
            AttributeError,
            ImportError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            logger.debug(
                "[TokenizerService] tokenizer registry deduplication skipped for %s: %s",
                profile.model,
                exc,
            )
    has_explicit_artifact_identity = _has_explicit_artifact_identity(profile.spec)
    same_artifact = resolved_from_registry or has_explicit_artifact_identity
    payload = {
        "provider": "" if same_artifact else profile.provider.strip().casefold(),
        "model": "" if same_artifact else profile.model.strip().casefold(),
        "spec": tokenizer_identity,
        "registry": _identity_value(settings.registry),
        "cache_dir": str(settings.cache_dir),
        "offline": settings.offline,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode(
            "utf-8"
        )
    ).hexdigest()


def _identity_value(value: Any, *, _key: str = "") -> Any:
    """Normalize matching metadata without changing IDs, paths, or revisions."""
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            normalized[str(key)] = _identity_value(
                item,
                _key=str(key).casefold(),
            )
        return normalized
    if isinstance(value, list):
        normalized_items: list[Any] = []
        for item in value:
            normalized_items.append(_identity_value(item, _key=_key))
        return normalized_items
    if isinstance(value, str) and _key in {
        "provider",
        "model",
        "source",
        "engine",
        "family",
    }:
        return value.strip().casefold()
    return value


def _has_explicit_artifact_identity(spec: dict[str, Any] | None) -> bool:
    """Return whether a spec names a concrete tokenizer artifact."""
    if not isinstance(spec, dict):
        return False
    for field in ("id", "tokenizer_id", "artifact_path"):
        if spec.get(field):
            return True
    return False


def _is_remote_tokenizer_spec(spec: dict[str, Any] | None) -> bool:
    """Return whether a spec requires a remote artifact lookup."""
    if not isinstance(spec, dict):
        return False
    return str(spec.get("source") or "").strip().casefold() in {
        "huggingface",
        "modelscope",
    }


def _tokenizer_id(spec: dict[str, Any] | None) -> str | None:
    """Extract the normalized primary tokenizer identifier from a spec."""
    if not isinstance(spec, dict):
        return None
    value = spec.get("id") or spec.get("tokenizer_id")
    return str(value).strip() if value else None


def _fallback_tokenizer_id(spec: dict[str, Any] | None) -> str | None:
    """Extract the first compatible fallback tokenizer identifier."""
    if not isinstance(spec, dict):
        return None
    fallbacks = spec.get("compatible_fallbacks")
    if not isinstance(fallbacks, list) or not fallbacks:
        return None
    return _tokenizer_id(fallbacks[0]) if isinstance(fallbacks[0], dict) else None


def _contains_discoverable_profile(profiles: list[TokenizerProfile]) -> bool:
    """Return whether metadata discovery is needed for one configured profile."""
    for profile in profiles:
        if profile.allow_metadata_discovery:
            return True
    return False


class TokenizerService:
    """Own tokenizer prewarming for one AgentServer process.

    Calls are serialized at the service level so a startup warm-up and a
    simultaneous model reload cannot duplicate downloads. Individual models
    are resolved concurrently in worker threads.
    """

    def __init__(self) -> None:
        """Create a service with process-local deduplication state."""
        self._lock = asyncio.Lock()
        self._warmed_keys: set[str] = set()
        self._last_result: dict[str, Any] = {}

    @property
    def last_result(self) -> dict[str, Any]:
        """Return a copy of the most recent warm-up result."""
        return dict(self._last_result)

    async def warm(
        self,
        config: dict[str, Any] | None = None,
        *,
        reason: str,
    ) -> dict[str, Any]:
        """Warm configured model tokenizers and return a compact status."""
        effective_config = self._merge_config(config)
        settings = tokenizer_warmup_settings(effective_config)
        profiles = configured_tokenizer_profiles(effective_config)
        if not settings.enabled:
            # The switch is the application-level master switch: disabled
            # means no tokenizer warm-up/download. ContextEngine may still
            # consume an already cached artifact when it creates a context.
            self._warmed_keys.clear()
            result = {
                "enabled": False,
                "counter_enabled": False,
                "cache_dir": str(settings.cache_dir),
                "total": len(profiles),
                "warmed": 0,
                "degraded": 0,
                "failed": 0,
                "skipped": len(profiles),
                "reason": reason,
                "models": [],
            }
            self._last_result = result
            logger.info(
                "[TokenizerService] tokenizer warm-up disabled (%s): skipping %d "
                "configured profile(s); no download; counter_enabled=False",
                reason,
                len(profiles),
            )
            return result

        if not profiles:
            self._warmed_keys.clear()
            result = {
                "enabled": False,
                "counter_enabled": settings.enabled,
                "cache_dir": str(settings.cache_dir),
                "total": 0,
                "warmed": 0,
                "degraded": 0,
                "failed": 0,
                "reason": reason,
            }
            self._last_result = result
            logger.info(
                "[TokenizerService] no configured text models (%s): warm-up not needed; "
                "counter_enabled=%s",
                reason,
                settings.enabled,
            )
            return result

        if not settings.offline and _contains_discoverable_profile(profiles):
            discovery_tasks = []
            for profile in profiles:
                if profile.allow_metadata_discovery:
                    discovery_tasks.append(
                        asyncio.to_thread(
                            _discover_tokenizer_spec,
                            profile,
                            allow_network=True,
                        )
                    )
                else:
                    discovery_tasks.append(asyncio.sleep(0, result=profile.spec))
            discovered_specs = await asyncio.gather(
                *discovery_tasks,
                return_exceptions=True,
            )
            discovered_profiles: list[TokenizerProfile] = []
            for profile, discovered_spec in zip(profiles, discovered_specs):
                if not isinstance(discovered_spec, (dict, type(None))):
                    discovered_spec = profile.spec
                discovered_profiles.append(
                    TokenizerProfile(
                        provider=profile.provider,
                        model=profile.model,
                        spec=discovered_spec,
                        allow_metadata_discovery=profile.allow_metadata_discovery,
                    )
                )
            profiles = discovered_profiles

        settings.cache_dir.mkdir(parents=True, exist_ok=True)

        async with self._lock:
            pending: list[tuple[str, TokenizerProfile]] = []
            pending_keys = set(self._warmed_keys)
            for profile in profiles:
                key = _stable_key(profile, settings)
                if key not in pending_keys:
                    pending.append((key, profile))
                    pending_keys.add(key)

            if not pending:
                result = {
                    "enabled": True,
                    "counter_enabled": settings.enabled,
                    "cache_dir": str(settings.cache_dir),
                    "total": len(profiles),
                    "warmed": 0,
                    "degraded": 0,
                    "failed": 0,
                    "skipped": len(profiles),
                    "reason": reason,
                }
                self._last_result = result
                logger.info(
                    "[TokenizerService] no new tokenizer profiles (%s): total=%d cache=%s",
                    reason,
                    len(profiles),
                    settings.cache_dir,
                )
                return result

            logger.info(
                "[TokenizerService] warming %d tokenizer profile(s) (%s), cache=%s "
                "offline=%s counter_enabled=%s",
                len(pending),
                reason,
                settings.cache_dir,
                settings.offline,
                settings.enabled,
            )

            warm_tasks = []
            for _, profile in pending:
                warm_tasks.append(self._warm_one(profile, settings))
            outcomes = await asyncio.gather(
                *warm_tasks,
                return_exceptions=True,
            )

            warmed = 0
            degraded = 0
            failed = 0
            statuses: list[dict[str, Any]] = []
            for (key, profile), outcome in zip(pending, outcomes):
                if isinstance(outcome, BaseException):
                    failed += 1
                    logger.warning(
                        "[TokenizerService] warm failed for provider=%s model=%s: %s",
                        profile.provider,
                        profile.model,
                        outcome,
                    )
                    statuses.append(
                        {
                            "provider": profile.provider,
                            "model": profile.model,
                            "ok": False,
                            "status": "failed",
                            "primary_tokenizer": _tokenizer_id(profile.spec),
                            "fallback_tokenizer": _fallback_tokenizer_id(profile.spec),
                            "error": str(outcome),
                        }
                    )
                    continue
                source = getattr(outcome, "measurement_source", None)
                is_native = source in _NATIVE_WARM_SOURCES
                if is_native:
                    warmed += 1
                    self._warmed_keys.add(key)
                else:
                    # Selector fallback is a usable degraded result, but it
                    # is not a tokenizer warm hit. Keep it retryable so a
                    # later config reload can warm a newly supplied spec.
                    degraded += 1
                fallback_reason = getattr(outcome, "measurement_fallback_reason", None)
                if fallback_reason == "model_tokenizer_spec_missing":
                    status = "unresolved"
                elif is_native:
                    status = "native_warmed"
                else:
                    status = "fallback"
                statuses.append(
                    {
                        "provider": profile.provider,
                        "model": profile.model,
                        "ok": is_native,
                        "status": status,
                        "primary_tokenizer": _tokenizer_id(profile.spec),
                        "fallback_tokenizer": _fallback_tokenizer_id(profile.spec),
                        "source": source,
                        "tokenizer": getattr(outcome, "measurement_tokenizer", None),
                        "selected_tokenizer": getattr(
                            outcome,
                            "measurement_tokenizer",
                            None,
                        ),
                        "fallback_tokenizer_model": getattr(
                            outcome,
                            "measurement_fallback_tokenizer_model",
                            None,
                        ),
                        "fallback_reason": fallback_reason,
                    }
                )
                logger.info(
                    "[TokenizerService] model result provider=%s model=%s status=%s "
                    "source=%s tokenizer=%s fallback_tokenizer=%s "
                    "fallback_reason=%s",
                    profile.provider,
                    profile.model,
                    status,
                    source,
                    getattr(outcome, "measurement_tokenizer", None),
                    getattr(outcome, "measurement_fallback_tokenizer_model", None),
                    fallback_reason,
                )

            result = {
                "enabled": True,
                "counter_enabled": settings.enabled,
                "cache_dir": str(settings.cache_dir),
                "total": len(profiles),
                "warmed": warmed,
                "degraded": degraded,
                "failed": failed,
                "skipped": max(len(profiles) - len(pending), 0),
                "reason": reason,
                "models": statuses,
            }
            self._last_result = result
            return result

    @staticmethod
    def _merge_config(config: dict[str, Any] | None) -> dict[str, Any]:
        """Fill partial reload payloads with the current runtime policy."""
        if not isinstance(config, dict):
            return get_config()
        if "react" in config and "models" in config:
            return config

        current = get_config()
        merged = dict(current) if isinstance(current, dict) else {}
        merged.update(config)
        if (
            isinstance(current, dict)
            and isinstance(current.get("react"), dict)
            and isinstance(config.get("react"), dict)
        ):
            react = dict(current["react"])
            react.update(config["react"])
            if isinstance(
                current["react"].get("context_engine_config"), dict
            ) and isinstance(config["react"].get("context_engine_config"), dict):
                context_config = dict(current["react"]["context_engine_config"])
                context_config.update(config["react"]["context_engine_config"])
                react["context_engine_config"] = context_config
            merged["react"] = react
        return merged

    async def _warm_one(
        self,
        profile: TokenizerProfile,
        settings: TokenizerWarmupSettings,
    ) -> Any:
        """Resolve one tokenizer, retrying transient remote failures."""
        # Lazy import keeps the server importable with an older core package;
        # the feature is activated when configured model profiles exist and
        # the adapted agent-core is present.
        from openjiuwen.core.context_engine import (
            TokenizerArtifactManager,
            TokenizerRegistry,
            TokenizerSelector,
        )

        should_retry = _is_remote_tokenizer_spec(profile.spec) and not settings.offline
        max_attempts = _TOKENIZER_WARMUP_MAX_ATTEMPTS if should_retry else 1
        last_outcome: Any = None

        for attempt in range(1, max_attempts + 1):
            manager = TokenizerArtifactManager(
                cache_dir=str(settings.cache_dir),
                enable_download=True,
                offline=settings.offline,
            )
            selector = TokenizerSelector(
                provider=profile.provider,
                model=profile.model,
                spec=profile.spec,
                registry=TokenizerRegistry(settings.registry),
                manager=manager,
                allow_tiktoken_fallback=False,
            )
            # Artifact resolution may download; keep the event loop responsive.
            last_outcome = await asyncio.to_thread(selector.select)
            source = getattr(last_outcome, "measurement_source", None)
            if source in _NATIVE_WARM_SOURCES:
                if attempt > 1:
                    logger.info(
                        "[TokenizerService] tokenizer retry succeeded for provider=%s "
                        "model=%s attempt=%d/%d",
                        profile.provider,
                        profile.model,
                        attempt,
                        max_attempts,
                    )
                return last_outcome

            error = (
                getattr(manager, "last_error", None)
                or getattr(last_outcome, "measurement_fallback_reason", None)
                or "native_tokenizer_unavailable"
            )
            if attempt >= max_attempts:
                logger.warning(
                    "[TokenizerService] tokenizer warm-up exhausted for provider=%s "
                    "model=%s attempts=%d error=%s",
                    profile.provider,
                    profile.model,
                    attempt,
                    error,
                )
                return last_outcome

            delay = _TOKENIZER_WARMUP_RETRY_DELAYS_SECONDS[attempt - 1]
            logger.warning(
                "[TokenizerService] tokenizer warm-up attempt failed for provider=%s "
                "model=%s attempt=%d/%d error=%s; retrying in %.1fs",
                profile.provider,
                profile.model,
                attempt,
                max_attempts,
                error,
                delay,
            )
            await asyncio.sleep(delay)

        # max_attempts is positive, so this is defensive only.
        return last_outcome


__all__ = [
    "TokenizerProfile",
    "TokenizerService",
    "TokenizerWarmupSettings",
    "configured_tokenizer_profiles",
    "resolve_tokenizer_cache_dir",
    "tokenizer_warmup_settings",
]
