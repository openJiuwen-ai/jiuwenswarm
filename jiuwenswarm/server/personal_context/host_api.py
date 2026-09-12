"""JiuwenSwarm's in-process owner of the embedded PersonalContext runtime.

The Host owns the configuration file and Core lifecycle, and delegates Context
queries to Core.  It does not expose a transport, create a second service
object, or read any Context file itself.
"""

from __future__ import annotations

import asyncio
import contextlib
from copy import deepcopy
import os
from pathlib import Path
import stat
import tempfile
from collections.abc import Awaitable, Callable
from typing import NoReturn, cast

import httpx
import yaml

from openjiuwen.harness.personal_context import PersonalContext

from jiuwenswarm.common.config import get_config, get_default_models


_CONFIG_FILENAME = "personal_context.yaml"
_MAX_CONFIG_BYTES = 4 * 1024 * 1024
_STOP_TIMEOUT_SECONDS = 30.0
_PERSONAL_CONTEXT_MODEL_MAX_RETRIES = 2
_PAT_VALIDATION_TIMEOUT_SECONDS = 15.0
_MAX_PAT_RESPONSE_BYTES = 1024 * 1024
_REPOSITORY_PAT_FIELDS = {"github": "token", "gitcode": "pat"}
_REPOSITORY_USER_URLS = {
    "github": "https://api.github.com/user",
    "gitcode": "https://api.gitcode.com/api/v5/user",
}


def _directory_capacity_defaults() -> tuple[int, int]:
    fields = PersonalContext.Config.model_fields
    pages = fields["max_pages_per_directory"].default
    subdirectories = fields["max_subdirectories_per_directory"].default
    if type(pages) is not int or type(subdirectories) is not int:
        raise RuntimeError("PersonalContext directory capacity defaults are invalid")
    return pages, subdirectories


def _global_embedding_values() -> tuple[str | None, str | None, str | None]:
    try:
        config = get_config() or {}
    except Exception:
        return None, None, None
    embed = config.get("embed") if isinstance(config, dict) else None
    if not isinstance(embed, dict):
        return None, None, None
    model = str(embed.get("embed_model") or "").strip()
    base_url = str(
        embed.get("embed_base_url") or embed.get("embed_api_base") or ""
    ).strip()
    api_key = str(embed.get("embed_api_key") or "").strip()
    return (
        (model, base_url, api_key)
        if model and base_url and api_key
        else (None, None, None)
    )


def _is_usable_model_entry(entry: object) -> bool:
    """条目是否是可用的默认模型（即能被 agent/balanced 策略实际调用）。"""

    if not isinstance(entry, dict):
        return False
    client = entry.get("model_client_config")
    request = entry.get("model_config_obj")
    if not isinstance(client, dict) or not isinstance(request, dict):
        return False
    return bool(str(client.get("model_name") or "").strip())


def _model_entry_id(entry: object) -> str | None:
    """生成跨请求稳定的模型标识。

    model_name 通常已是唯一业务标识；加上 provider 可避免不同网关配置同一个
    model_name 时相互覆盖。该 ID 只用于定位当前 models.list 条目，不落 Core。
    """

    if not isinstance(entry, dict):
        return None
    client = entry.get("model_client_config")
    if not isinstance(client, dict):
        return None
    model_name = str(client.get("model_name") or "").strip()
    if not model_name:
        return None
    provider = str(client.get("client_provider") or "").strip().casefold()
    return f"{provider}:{model_name}" if provider else model_name


def _find_model_index_by_id(model_id: object) -> int | None:
    """按稳定 ID 查找当前下标；ID 不存在或格式非法时返回 None。"""

    if not isinstance(model_id, str) or not model_id:
        return None
    models = get_default_models()
    for index, entry in enumerate(models):
        if _model_entry_id(entry) == model_id:
            return index
    return None


def _first_usable_model() -> tuple[int | None, str | None]:
    """返回第一个可用模型及其稳定 ID；列表为空或条目不可用时均为 None。

    环境变量兜底分支会产出一条 model_name 为空的占位条目，它无法用于智能体
    策略，因此这里按「可用」而非「存在」判断，避免默认值落到坏条目上再报错。
    """

    for index, entry in enumerate(get_default_models()):
        if _is_usable_model_entry(entry):
            return index, _model_entry_id(entry)
    return None, None


def _first_usable_model_index() -> int | None:
    """兼容旧调用点的第一个可用模型下标。"""

    return _first_usable_model()[0]


def _model_index_is_usable(model_index: object) -> bool:
    if type(model_index) is not int or model_index < 0:
        return False
    models = get_default_models()
    if model_index >= len(models):
        return False
    return _is_usable_model_entry(models[model_index])


def _reconcile_model_selection(stored: dict[str, object]) -> None:
    """让「稳定模型 ID + 当前下标 + 采集策略」自洽。

    - model_id 可用：以它为准刷新 model_index，避免 models.list 顺序变化改变选择；
    - 旧 YAML 只有 model_index：沿用当前有效下标并补写 model_id；
    - model_id 失效或下标未设置/已失效：回落到第一个可用模型；一个可用模型都没有则置空，
      并把依赖模型的 balanced/agent 降级为 rules——Core 要求二者必须同时
      提供 model_client 与 model_request，否则整份配置直接校验失败。
    """

    if "model_id" in stored and stored.get("model_id") is not None:
        matched_index = _find_model_index_by_id(stored.get("model_id"))
        if matched_index is not None:
            stored["model_index"] = matched_index
            return
    elif _model_index_is_usable(stored.get("model_index")):
        model_index = cast(int, stored["model_index"])
        stored["model_id"] = _model_entry_id(get_default_models()[model_index])
        return

    fallback_index, fallback_id = _first_usable_model()
    fallback = fallback_index
    if fallback is not None:
        stored["model_index"] = fallback
        stored["model_id"] = fallback_id
        return
    stored["model_index"] = None
    stored["model_id"] = None
    if stored.get("strategy_profile") in {"balanced", "agent"}:
        stored["strategy_profile"] = "rules"


def _default_strategy_and_model() -> tuple[str, int | None, str | None]:
    """默认采集策略与模型：有可用模型则默认第一个模型走智能体，否则模型置空并回退规则模式。

    agent/balanced 策略强依赖模型（Core 校验要求 model_client 与 model_request 同时提供），
    因此无可用模型时只能回退 rules——否则首次启用会直接抛 invalid configuration。
    """

    model_index, model_id = _first_usable_model()
    if model_index is None:
        return "rules", None, None
    return "agent", model_index, model_id


def _initial_stored_config(*, collection_enabled: bool) -> dict[str, object]:
    max_pages, max_subdirectories = _directory_capacity_defaults()
    strategy_profile, model_index, model_id = _default_strategy_and_model()
    return {
        "collection_enabled": collection_enabled,
        "agent_use_enabled": False,
        "strategy_profile": strategy_profile,
        "max_pages_per_directory": max_pages,
        "max_subdirectories_per_directory": max_subdirectories,
        "fetch_services": [],
        "model_index": model_index,
        "model_id": model_id,
        "provider_credentials": {},
    }


def _unconfigured_projection() -> dict[str, object]:
    max_pages, max_subdirectories = _directory_capacity_defaults()
    strategy_profile, model_index, model_id = _default_strategy_and_model()
    return {
        "configured": False,
        "collection_enabled": False,
        "agent_use_enabled": False,
        "strategy_profile": strategy_profile,
        "max_pages_per_directory": max_pages,
        "max_subdirectories_per_directory": max_subdirectories,
        "model_index": model_index,
        "model_id": model_id,
        "fetch_services": [],
    }


def _host_error(
    message: str,
    *,
    status_name: str = "CONTEXT_PROACTIVE_CONFIG_INVALID",
    cause: BaseException | None = None,
) -> PersonalContext.Error:
    """Create the existing Core error type without adding a Host exception."""

    # PersonalContext.Config.from_dict() is the Core's public error-construction boundary.
    # Deliberately use it instead of importing another Core error class here:
    # the JiuwenSwarm side has exactly one Core import, PersonalContext.
    try:
        PersonalContext.Config.from_dict({})
    except PersonalContext.Error as baseline:
        # Core intentionally keeps PersonalContext-owned status values behind the single
        # PersonalContext import; this Host compatibility bridge therefore uses its
        # protected resolver without importing a second Core symbol.
        status = PersonalContext._status_for_name(status_name)  # pylint: disable=protected-access
        return type(baseline)(status, msg=message, cause=cause)
    return PersonalContext.Error(PersonalContext.Error.status, msg=message, cause=cause)


def _raise_host_error(
    message: str,
    *,
    status_name: str = "CONTEXT_PROACTIVE_CONFIG_INVALID",
    cause: BaseException | None = None,
) -> NoReturn:
    raise _host_error(message, status_name=status_name, cause=cause) from None


def _as_host_error(
    exc: BaseException,
    message: str,
    *,
    status_name: str = "CONTEXT_PROACTIVE_STATE_INVALID",
) -> PersonalContext.Error:
    if isinstance(exc, PersonalContext.Error):
        return exc
    return _host_error(message, status_name=status_name, cause=exc)


def _reject_symlink_chain(path: Path) -> None:
    current = path
    while True:
        if current.is_symlink():
            _raise_host_error(
                "PersonalContext configuration path must not traverse a symlink",
                status_name="CONTEXT_PROACTIVE_FILE_EXECUTION_ERROR",
            )
        parent = current.parent
        if parent == current:
            return
        current = parent


def _serialize_config(config: dict[str, object]) -> bytes:
    try:
        text = yaml.safe_dump(config, allow_unicode=True, sort_keys=False)
        payload = text.encode("utf-8")
        if len(payload) > _MAX_CONFIG_BYTES:
            _raise_host_error("PersonalContext configuration exceeds the 4 MiB limit")
        return payload
    except PersonalContext.Error:
        raise
    except Exception as exc:
        _raise_host_error(
            "PersonalContext configuration could not be serialized", cause=exc
        )


def _stage_yaml(path: Path, payload: bytes) -> Path:
    temporary: Path | None = None
    try:
        _reject_symlink_chain(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        with contextlib.suppress(OSError):
            os.chmod(temporary, 0o600)
        return temporary
    except PersonalContext.Error:
        if temporary is not None:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()
        raise
    except Exception as exc:
        if temporary is not None:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()
        _raise_host_error(
            "PersonalContext configuration temporary file could not be written",
            status_name="CONTEXT_PROACTIVE_FILE_EXECUTION_ERROR",
            cause=exc,
        )


def _replace_yaml(temporary: Path, path: Path) -> None:
    try:
        _reject_symlink_chain(path)
        os.replace(temporary, path)
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
    except PersonalContext.Error:
        raise
    except Exception as exc:
        _raise_host_error(
            "PersonalContext configuration file could not be replaced",
            status_name="CONTEXT_PROACTIVE_FILE_EXECUTION_ERROR",
            cause=exc,
        )


def _cleanup_temporary(path: Path | None) -> None:
    if path is not None:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


def _publish_yaml(path: Path, payload: bytes) -> None:
    temporary = _stage_yaml(path, payload)
    try:
        _replace_yaml(temporary, path)
    finally:
        _cleanup_temporary(temporary)


def _read_yaml(path: Path) -> dict[str, object] | None:
    try:
        _reject_symlink_chain(path)
        try:
            with path.open("rb") as handle:
                opened = os.fstat(handle.fileno())
                if not stat.S_ISREG(opened.st_mode):
                    _raise_host_error(
                        "PersonalContext configuration path is not a file",
                        status_name="CONTEXT_PROACTIVE_FILE_EXECUTION_ERROR",
                    )
                payload = handle.read(_MAX_CONFIG_BYTES + 1)
            if len(payload) > _MAX_CONFIG_BYTES:
                _raise_host_error(
                    "PersonalContext configuration exceeds the 4 MiB limit"
                )
            text = payload.decode("utf-8")
        except FileNotFoundError:
            return None
        except PersonalContext.Error:
            raise
        except Exception as exc:
            _raise_host_error(
                "PersonalContext configuration YAML could not be read",
                status_name="CONTEXT_PROACTIVE_FILE_EXECUTION_ERROR",
                cause=exc,
            )
        try:
            loaded = yaml.safe_load(text)
        except Exception as exc:
            _raise_host_error(
                "PersonalContext configuration YAML is invalid", cause=exc
            )
        if not isinstance(loaded, dict):
            _raise_host_error(
                "PersonalContext configuration YAML must contain an object"
            )
        return loaded
    except PersonalContext.Error:
        raise
    except Exception as exc:
        _raise_host_error(
            "PersonalContext configuration YAML could not be read", cause=exc
        )


def _is_runtime_active(status: object) -> bool:
    return getattr(status, "state", None) in {"STARTING", "RUNNING"}


def _resolve_model_reference(
    model_index: object,
) -> tuple[dict[str, object], dict[str, object]]:
    if type(model_index) is not int or model_index < 0:
        _raise_host_error("model_index must be a non-negative integer")
    models = get_default_models()
    if model_index >= len(models):
        _raise_host_error("selected JiuwenSwarm model no longer exists")
    entry = models[model_index]
    if not isinstance(entry, dict):
        _raise_host_error("selected JiuwenSwarm model is invalid")
    client_raw = entry.get("model_client_config")
    request_raw = entry.get("model_config_obj")
    if not isinstance(client_raw, dict) or not isinstance(request_raw, dict):
        _raise_host_error("selected JiuwenSwarm model is invalid")
    client = deepcopy(client_raw)
    request = deepcopy(request_raw)
    model_name = str(client.pop("model_name", "")).strip()
    if not model_name:
        _raise_host_error("selected JiuwenSwarm model is invalid")
    client["max_retries"] = _PERSONAL_CONTEXT_MODEL_MAX_RETRIES
    request["model"] = model_name
    return client, request


def _normalize_provider_credentials(value: object) -> dict[str, dict[str, str]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        _raise_host_error("provider_credentials must be an object")
    unknown = set(value) - set(_REPOSITORY_PAT_FIELDS)
    if unknown:
        _raise_host_error("provider_credentials contains an unsupported provider")
    normalized: dict[str, dict[str, str]] = {}
    for provider, raw_credentials in value.items():
        if not isinstance(raw_credentials, dict):
            _raise_host_error(f"{provider} provider credentials must be an object")
        field = _REPOSITORY_PAT_FIELDS[provider]
        if set(raw_credentials) != {field}:
            _raise_host_error(
                f"{provider} provider credentials must contain only {field}"
            )
        secret = raw_credentials[field]
        if not isinstance(secret, str) or not secret.strip():
            _raise_host_error(
                f"{provider} provider credential must be a non-empty string"
            )
        normalized[provider] = {field: secret.strip()}
    return normalized


def _project_service(service: dict[str, object]) -> dict[str, object]:
    projected = deepcopy(service)
    projected.pop("credentials", None)
    return projected


def _project_stored_config(stored: dict[str, object]) -> dict[str, object]:
    projected = deepcopy(stored)
    projected.pop("provider_credentials", None)
    services = projected.get("fetch_services", [])
    if not isinstance(services, list) or any(
        not isinstance(service, dict) for service in services
    ):
        _raise_host_error("PersonalContext fetch_services are invalid")
    projected["fetch_services"] = [_project_service(service) for service in services]
    return projected


def _build_core_config(stored: dict[str, object]) -> PersonalContext.Config:
    raw = deepcopy(stored)
    raw.pop("provider_credentials", None)
    model_index = raw.pop("model_index", None)
    raw.pop("model_id", None)
    raw.pop("model_client", None)
    raw.pop("model_request", None)
    if model_index is not None:
        client, request = _resolve_model_reference(model_index)
        raw["model_client"] = client
        raw["model_request"] = request
    try:
        return PersonalContext.Config.from_dict(raw)
    except PersonalContext.Error:
        raise
    except Exception as exc:
        _raise_host_error("PersonalContext configuration is invalid", cause=exc)


def _semantic_config_dump(config: PersonalContext.Config) -> dict[str, object]:
    """可比较的配置快照：剔除每次构造都会变的 Core 自动生成字段。"""

    dumped = config.model_dump(mode="json", by_alias=True)
    client = dumped.get("model_client")
    if isinstance(client, dict):
        # Core 的 ModelClientConfig.client_id 是 uuid4 默认值，每次 from_dict 都不同。
        client.pop("client_id", None)
    return dumped


def _configs_equivalent(
    left: PersonalContext.Config, right: PersonalContext.Config
) -> bool:
    """两份 Core 配置语义是否一致（忽略 volatile 的自动生成字段）。

    直接比较对象时，只要配置里带 model_client 就永远不等，会让幂等快路径失效，
    于是每次写配置都会走一遍 deactivate → set → activate 重启运行时。
    """

    if left == right:
        return True
    return _semantic_config_dump(left) == _semantic_config_dump(right)


def _prepare_stored_config(
    config: dict[str, object],
) -> tuple[dict[str, object], PersonalContext.Config]:
    if not isinstance(config, dict):
        _raise_host_error("PersonalContext configuration must be an object")
    stored = deepcopy(config)
    stored.pop("model_client", None)
    stored.pop("model_request", None)
    provider_credentials = _normalize_provider_credentials(
        stored.pop("provider_credentials", {})
    )
    # 模型不可用时先归一化，再交给 Core 校验；否则会整份配置判为非法。
    _reconcile_model_selection(stored)
    candidate = _build_core_config(stored)
    normalized = candidate.model_dump(mode="json", by_alias=True)
    normalized.pop("model_client", None)
    normalized.pop("model_request", None)
    if "model_index" in stored:
        normalized["model_index"] = stored["model_index"]
    normalized["model_id"] = stored.get("model_id")
    normalized["provider_credentials"] = provider_credentials
    return normalized, candidate


def _repository_authorization_result(
    provider: str,
    state: str,
    *,
    account: dict[str, str] | None = None,
    error: str | None = None,
) -> dict[str, object]:
    return {
        "provider": provider,
        "state": state,
        "account": deepcopy(account),
        "verification_url": None,
        "expires_at": None,
        "error": error,
    }


async def _validate_repository_pat(provider: str, secret: str) -> dict[str, str]:
    url = _REPOSITORY_USER_URLS.get(provider)
    if url is None:
        _raise_host_error("unsupported repository provider")
    try:
        async with httpx.AsyncClient(
            timeout=_PAT_VALIDATION_TIMEOUT_SECONDS,
            follow_redirects=False,
        ) as client:
            response = await client.get(
                url,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {secret}",
                },
            )
    except httpx.TimeoutException:
        _raise_host_error("repository provider credential validation timed out")
    except httpx.HTTPError:
        _raise_host_error("repository provider credential validation request failed")
    except Exception:
        _raise_host_error("repository provider credential validation request failed")
    if response.status_code != 200:
        _raise_host_error(
            f"repository provider credential validation returned HTTP {response.status_code}"
        )
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            if int(content_length) > _MAX_PAT_RESPONSE_BYTES:
                _raise_host_error(
                    "repository provider credential response exceeds the size limit"
                )
        except ValueError:
            _raise_host_error(
                "repository provider credential response has an invalid size"
            )
    content = response.content
    if len(content) > _MAX_PAT_RESPONSE_BYTES:
        _raise_host_error(
            "repository provider credential response exceeds the size limit"
        )
    try:
        payload = response.json()
    except (ValueError, TypeError):
        _raise_host_error("repository provider credential response is invalid")
    if not isinstance(payload, dict):
        _raise_host_error("repository provider credential response is invalid")
    login = payload.get("login") or payload.get("username")
    display_name = payload.get("name") or payload.get("display_name") or login
    if not isinstance(login, str) or not isinstance(display_name, str):
        _raise_host_error("repository provider account response is invalid")
    for value in (login, display_name):
        if (
            not value.strip()
            or len(value) > 256
            or any(ord(character) < 32 for character in value)
        ):
            _raise_host_error("repository provider account response is invalid")
    return {"login": login.strip(), "display_name": display_name.strip()}


async def _validate_repository_pat_for_write(
    provider: str,
    secret: str,
) -> dict[str, str]:
    try:
        return await _validate_repository_pat(provider, secret)
    except Exception as exc:
        raise _as_host_error(
            exc,
            f"{provider} credential verification failed",
            status_name="CONTEXT_PROACTIVE_CONFIG_INVALID",
        ) from None


class PersonalContextHostAPI:
    """The only JiuwenSwarm API for configuring and controlling embedded PersonalContext."""

    def __init__(self, *, home: str | Path) -> None:
        self._home = Path(home).expanduser().resolve()
        self._config_path = self._home / _CONFIG_FILENAME
        self._personal_context = PersonalContext(home=self._home)
        self._config: PersonalContext.Config | None = None
        self._stored_config: dict[str, object] | None = None
        self._operation_lock = asyncio.Lock()
        self._fetch_run_stop_lock = asyncio.Lock()

    def _refresh_embedding_configuration(self) -> None:
        model_name, base_url, api_key = _global_embedding_values()
        # Merged Core exposes this Host-owned seam as a protected method.
        self._personal_context._set_embedding_configuration(  # pylint: disable=protected-access
            model_name=model_name,
            base_url=base_url,
            api_key=api_key,
        )

    async def _start_collection_with_embedding(self) -> None:
        self._refresh_embedding_configuration()
        await self._personal_context.start_collection()

    async def configure(self, config: dict[str, object]) -> None:
        """Validate, save, and apply one complete configuration."""

        stored, candidate = _prepare_stored_config(config)
        payload = _serialize_config(stored)

        async with self._operation_lock:
            await self._apply_configuration_locked(candidate, stored, payload)

    async def _apply_configuration_locked(
        self,
        candidate: PersonalContext.Config,
        stored: dict[str, object],
        payload: bytes,
        *,
        known_previous_active: bool | None = None,
    ) -> None:
        """Apply one validated complete configuration while the Host lock is held."""

        previous = self._config
        previous_stored = self._stored_config
        same_configuration = previous is not None and _configs_equivalent(
            previous, candidate
        )

        previous_active = False
        if previous is not None:
            if known_previous_active is not None:
                previous_active = known_previous_active
            else:
                try:
                    previous_active = _is_runtime_active(
                        await self._personal_context.snapshot()
                    )
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:
                    raise _as_host_error(
                        exc,
                        "PersonalContext runtime status could not be read",
                    ) from None
        if same_configuration and previous_active == candidate.collection_enabled:
            _publish_yaml(self._config_path, payload)
            self._stored_config = deepcopy(stored)
            return

        temporary: Path | None = _stage_yaml(self._config_path, payload)
        disabling = (
            previous is not None
            and previous.collection_enabled
            and not candidate.collection_enabled
        )
        disabled_yaml_published = False
        rollback_runtime = False
        phase = "stop"
        try:
            if disabling:
                phase = "replace"
                if temporary is None:
                    _raise_host_error("PersonalContext configuration staging failed")
                _replace_yaml(temporary, self._config_path)
                temporary = None
                disabled_yaml_published = True

            if previous is not None:
                phase = "stop"
                rollback_runtime = True
                await self._personal_context.deactivate_runtime(
                    timeout_seconds=_STOP_TIMEOUT_SECONDS
                )

            phase = "set"
            rollback_runtime = True
            await self._personal_context.set_configuration(candidate)

            if candidate.collection_enabled:
                phase = "activate"
                self._refresh_embedding_configuration()
                await self._personal_context.activate_runtime()

            if not disabled_yaml_published:
                phase = "replace"
                if temporary is None:
                    _raise_host_error("PersonalContext configuration staging failed")
                _replace_yaml(temporary, self._config_path)
                temporary = None

            self._config = candidate
            self._stored_config = deepcopy(stored)
        except BaseException as exc:
            rollback_error: BaseException | None = None
            if rollback_runtime or disabled_yaml_published:
                try:
                    await self._restore_previous(
                        previous,
                        previous_stored,
                        previous_active,
                    )
                except BaseException as restore_exc:
                    rollback_error = restore_exc
            if disabled_yaml_published and previous_stored is not None:
                try:
                    _publish_yaml(
                        self._config_path,
                        _serialize_config(previous_stored),
                    )
                except BaseException as restore_exc:
                    rollback_error = rollback_error or restore_exc
            if isinstance(exc, asyncio.CancelledError):
                raise
            if rollback_error is not None:
                raise _as_host_error(
                    rollback_error,
                    "PersonalContext previous configuration could not be restored",
                ) from None
            if phase == "set":
                raise _as_host_error(
                    exc,
                    "PersonalContext configuration could not be applied",
                    status_name="CONTEXT_PROACTIVE_CONFIG_INVALID",
                ) from None
            if phase == "activate":
                raise _as_host_error(
                    exc,
                    "PersonalContext runtime could not be started",
                ) from None
            if phase == "replace":
                raise _as_host_error(
                    exc,
                    "PersonalContext configuration file could not be replaced",
                    status_name="CONTEXT_PROACTIVE_FILE_EXECUTION_ERROR",
                ) from None
            raise _as_host_error(
                exc,
                "PersonalContext previous runtime could not be stopped",
            ) from None
        finally:
            _cleanup_temporary(temporary)

    async def _apply_live_update_locked(
        self,
        candidate: PersonalContext.Config,
        stored: dict[str, object],
        payload: bytes,
        *,
        apply: Callable[[], Awaitable[None]],
        rollback: Callable[[], Awaitable[None]],
        publish_before_apply: bool = False,
    ) -> None:
        """Atomically couple one hot Core update with its complete YAML snapshot."""

        if self._stored_config is None:
            _raise_host_error("PersonalContext is not configured")
        previous_payload = _serialize_config(self._stored_config)
        temporary: Path | None = _stage_yaml(self._config_path, payload)
        published = False
        apply_started = False
        try:
            if publish_before_apply:
                if temporary is None:
                    _raise_host_error("PersonalContext configuration staging failed")
                _replace_yaml(temporary, self._config_path)
                temporary = None
                published = True
            apply_started = True
            await apply()
            if not published:
                if temporary is None:
                    _raise_host_error("PersonalContext configuration staging failed")
                _replace_yaml(temporary, self._config_path)
                temporary = None
                published = True
            self._config = candidate
            self._stored_config = deepcopy(stored)
        except BaseException as exc:
            rollback_error: BaseException | None = None
            if apply_started:
                try:
                    await rollback()
                except BaseException as restore_exc:
                    rollback_error = restore_exc
            if published:
                try:
                    _publish_yaml(self._config_path, previous_payload)
                except BaseException as restore_exc:
                    rollback_error = rollback_error or restore_exc
            if isinstance(exc, asyncio.CancelledError):
                raise
            if rollback_error is not None:
                raise _as_host_error(
                    rollback_error,
                    "PersonalContext previous live configuration could not be restored",
                ) from None
            raise _as_host_error(
                exc,
                "PersonalContext live configuration could not be applied",
            ) from None
        finally:
            _cleanup_temporary(temporary)

    async def get_overview(self) -> dict[str, object]:
        """Return one consistent copy of the full configuration and Core status."""

        async with self._operation_lock:
            config = (
                _project_stored_config(self._stored_config)
                if self._stored_config is not None
                else None
            )
            status = await self._personal_context.snapshot()
            return {
                "configured": self._stored_config is not None,
                "config": config,
                "status": status.model_dump(mode="json"),
            }

    async def get_runtime_config(self) -> dict[str, object]:
        """Return the complete persistent PersonalContext configuration."""

        async with self._operation_lock:
            if self._stored_config is None:
                return _unconfigured_projection()
            return _project_stored_config(self._stored_config)

    async def patch_runtime_config(
        self,
        patch: dict[str, object],
    ) -> dict[str, object]:
        """Atomically patch the allowed runtime configuration fields."""

        if not isinstance(patch, dict):
            _raise_host_error("patch must be an object")
        unknown = set(patch) - {
            "collection_enabled",
            "agent_use_enabled",
            "strategy_profile",
        }
        if unknown:
            _raise_host_error("runtime patch contains unsupported fields")
        async with self._operation_lock:
            if self._stored_config is None:
                _raise_host_error("PersonalContext is not configured")
            stored = deepcopy(self._stored_config)
            stored.update(deepcopy(patch))
            stored, candidate = _prepare_stored_config(stored)
            await self._apply_configuration_locked(
                candidate,
                stored,
                _serialize_config(stored),
            )
            return _project_stored_config(stored)

    async def select_model(self, model_index: int) -> dict[str, object]:
        """Select one current JiuwenSwarm model by its models.list index."""

        if type(model_index) is not int or model_index < 0:
            _raise_host_error("model_index must be a non-negative integer")
        async with self._operation_lock:
            if self._stored_config is None:
                _raise_host_error("PersonalContext is not configured")
            # 校验放在锁内，避免 models.list 在校验和写入之间发生变化。
            # 显式选择必须拦截；_reconcile_model_selection 会把不可用下标静默回退。
            if not _model_index_is_usable(model_index):
                _raise_host_error("selected JiuwenSwarm model no longer exists")
            stored = deepcopy(self._stored_config)
            stored["model_index"] = model_index
            stored["model_id"] = _model_entry_id(get_default_models()[model_index])
            stored, candidate = _prepare_stored_config(stored)
            await self._apply_configuration_locked(
                candidate,
                stored,
                _serialize_config(stored),
            )
            return _project_stored_config(stored)

    async def set_collection_enabled(self, enabled: bool) -> dict[str, object]:
        """Persist and apply the PersonalContext collection switch."""

        if not isinstance(enabled, bool):
            _raise_host_error("enabled must be a boolean")
        async with self._operation_lock:
            first_start = self._stored_config is None
            if self._stored_config is None:
                if not enabled:
                    return _unconfigured_projection()
                stored = _initial_stored_config(collection_enabled=True)
            else:
                stored = deepcopy(self._stored_config)
            stored["collection_enabled"] = enabled
            stored, candidate = _prepare_stored_config(stored)
            if first_start:
                await self._apply_configuration_locked(
                    candidate,
                    stored,
                    _serialize_config(stored),
                )
            else:
                await self._apply_live_update_locked(
                    candidate,
                    stored,
                    _serialize_config(stored),
                    apply=(
                        self._start_collection_with_embedding
                        if enabled
                        else lambda: self._personal_context.stop_collection(
                            timeout_seconds=_STOP_TIMEOUT_SECONDS
                        )
                    ),
                    rollback=(
                        (
                            lambda: self._personal_context.stop_collection(
                                timeout_seconds=_STOP_TIMEOUT_SECONDS
                            )
                        )
                        if enabled
                        else self._start_collection_with_embedding
                    ),
                    publish_before_apply=not enabled,
                )
            result = _project_stored_config(stored)
            return result

    async def set_agent_use_enabled(self, enabled: bool) -> dict[str, object]:
        """Persist the Agent-use switch without creating an initial configuration."""

        if not isinstance(enabled, bool):
            _raise_host_error("enabled must be a boolean")
        async with self._operation_lock:
            if self._stored_config is None:
                _raise_host_error("PersonalContext is not configured")
            stored = deepcopy(self._stored_config)
            stored["agent_use_enabled"] = enabled
            stored, candidate = _prepare_stored_config(stored)
            await self._apply_live_update_locked(
                candidate,
                stored,
                _serialize_config(stored),
                apply=(
                    self._personal_context.start_agent_use
                    if enabled
                    else self._personal_context.stop_agent_use
                ),
                rollback=(
                    self._personal_context.stop_agent_use
                    if enabled
                    else self._personal_context.start_agent_use
                ),
            )
            return _project_stored_config(stored)

    async def list_fetch_services(self) -> list[dict[str, object]]:
        """Return every fixed fetch service configuration."""

        async with self._operation_lock:
            if self._stored_config is None:
                return []
            services = cast(
                list[dict[str, object]], self._stored_config["fetch_services"]
            )
            return [_project_service(service) for service in services]

    async def create_fetch_service(
        self,
        service: dict[str, object],
    ) -> dict[str, object]:
        """Validate, persist, and apply one new fixed-provider fetch service."""

        async with self._operation_lock:
            if self._stored_config is None:
                _raise_host_error("PersonalContext is not configured")
            if not isinstance(service, dict):
                _raise_host_error("service must be an object")
            stored = deepcopy(self._stored_config)
            services = cast(list[dict[str, object]], stored["fetch_services"])
            service_id = service.get("service_id")
            normalized_id = service_id.strip() if isinstance(service_id, str) else None
            existing_ids = {cast(str, item["service_id"]) for item in services}
            if normalized_id is not None and normalized_id in existing_ids:
                _raise_host_error("PersonalContext fetch service already exists")
            provider = service.get("provider")
            normalized_provider = (
                provider.strip().casefold() if isinstance(provider, str) else None
            )
            if normalized_provider is not None:
                provider_count = sum(
                    item.get("provider") == normalized_provider for item in services
                )
                if provider_count >= 20:
                    _raise_host_error(
                        f"{normalized_provider} fetch service limit of 20 has been reached"
                    )
            internal_service = deepcopy(service)
            if normalized_provider in _REPOSITORY_PAT_FIELDS:
                if "credentials" in service:
                    _raise_host_error(
                        "repository fetch service credentials are managed by the provider authorization"
                    )
                provider_credentials = cast(
                    dict[str, dict[str, str]],
                    stored.get("provider_credentials", {}),
                )
                current = provider_credentials.get(normalized_provider)
                field = _REPOSITORY_PAT_FIELDS[normalized_provider]
                if current is None or field not in current:
                    _raise_host_error(
                        f"{normalized_provider} must be authorized before creating a fetch service"
                    )
                internal_service["credentials"] = {field: current[field]}
            services.append(internal_service)
            stored, candidate = _prepare_stored_config(stored)
            if normalized_provider in _REPOSITORY_PAT_FIELDS:
                current_credentials = cast(
                    dict[str, dict[str, str]],
                    stored["provider_credentials"],
                )[normalized_provider]
                await _validate_repository_pat_for_write(
                    normalized_provider,
                    current_credentials[_REPOSITORY_PAT_FIELDS[normalized_provider]],
                )
            await self._apply_configuration_locked(
                candidate,
                stored,
                _serialize_config(stored),
            )
            normalized_services = cast(
                list[dict[str, object]],
                stored["fetch_services"],
            )
            created = next(
                item
                for item in normalized_services
                if cast(str, item["service_id"]) not in existing_ids
            )
            return _project_service(created)

    async def delete_fetch_service(self, service_id: str) -> None:
        """Remove one stopped service and its cursor while retaining Context files."""

        async with self._operation_lock:
            if self._stored_config is None:
                _raise_host_error("PersonalContext is not configured")
            if not isinstance(service_id, str) or not service_id.strip():
                _raise_host_error("service_id must be a non-empty string")
            normalized_id = service_id.strip()
            stored = deepcopy(self._stored_config)
            services = cast(list[dict[str, object]], stored["fetch_services"])
            if not any(item["service_id"] == normalized_id for item in services):
                _raise_host_error("unknown PersonalContext fetch service")
            try:
                status = await self._personal_context.snapshot()
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                raise _as_host_error(
                    exc,
                    "PersonalContext runtime status could not be read",
                ) from None
            states = getattr(status, "fetch_service_states", {})
            fetch_state = states.get(normalized_id)
            if fetch_state != "STOPPED":
                if fetch_state in {"STARTING", "RUNNING", "STOPPING"}:
                    _raise_host_error("PersonalContext 抓取服务正在执行，无法删除")
                _raise_host_error("PersonalContext 抓取服务尚未停止，请先停止后再删除")
            try:
                cursor_payload = self._personal_context.remove_fetch_cursor(
                    normalized_id
                )
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                raise _as_host_error(
                    exc,
                    "PersonalContext fetch cursor could not be removed",
                    status_name="CONTEXT_PROACTIVE_FILE_EXECUTION_ERROR",
                ) from None
            history_payload: list[dict[str, object]] | None = None
            try:
                history_payload = self._personal_context.remove_fetch_run_history(
                    normalized_id
                )
                stored["fetch_services"] = [
                    item for item in services if item["service_id"] != normalized_id
                ]
                stored, candidate = _prepare_stored_config(stored)
                await self._apply_configuration_locked(
                    candidate,
                    stored,
                    _serialize_config(stored),
                    known_previous_active=_is_runtime_active(status),
                )
            except BaseException as exc:
                restore_error: BaseException | None = None
                try:
                    self._personal_context.restore_fetch_cursor(
                        normalized_id,
                        cursor_payload,
                    )
                except BaseException as restore_exc:
                    restore_error = restore_exc
                if history_payload is not None:
                    try:
                        self._personal_context.restore_fetch_run_history(
                            normalized_id, history_payload
                        )
                    except BaseException as restore_exc:
                        restore_error = restore_exc
                if isinstance(exc, asyncio.CancelledError):
                    raise
                if restore_error is not None:
                    raise _as_host_error(
                        restore_error,
                        "PersonalContext fetch cursor could not be restored",
                        status_name="CONTEXT_PROACTIVE_FILE_EXECUTION_ERROR",
                    ) from None
                raise _as_host_error(
                    exc,
                    "PersonalContext fetch service could not be deleted",
                ) from None

    async def patch_fetch_service(
        self,
        service_id: str,
        patch: dict[str, object],
    ) -> dict[str, object]:
        """Atomically patch one existing fixed fetch service."""

        if not isinstance(service_id, str) or not service_id.strip():
            _raise_host_error("service_id must be a non-empty string")
        if not isinstance(patch, dict):
            _raise_host_error("patch must be an object")
        allowed = {
            "interval_seconds",
            "max_items_per_run",
            "source",
            "time_range",
        }
        if set(patch) - allowed:
            _raise_host_error("fetch service patch contains unsupported fields")
        normalized_id = service_id.strip()
        async with self._operation_lock:
            if self._stored_config is None:
                _raise_host_error("PersonalContext is not configured")
            stored = deepcopy(self._stored_config)
            services = cast(list[dict[str, object]], stored["fetch_services"])
            target = next(
                (
                    service
                    for service in services
                    if service["service_id"] == normalized_id
                ),
                None,
            )
            if target is None:
                _raise_host_error("unknown PersonalContext fetch service")
            target.update(deepcopy(patch))
            stored, candidate = _prepare_stored_config(stored)
            await self._apply_configuration_locked(
                candidate,
                stored,
                _serialize_config(stored),
            )
            updated_services = cast(
                list[dict[str, object]],
                stored["fetch_services"],
            )
            updated = next(
                service
                for service in updated_services
                if service["service_id"] == normalized_id
            )
            return _project_service(updated)

    async def get_fetch_run_status(
        self,
        service_id: str | None = None,
        *,
        run_id: str | None = None,
    ) -> dict[str, object]:
        """Return a selected round or retained runs grouped by service."""
        if service_id is not None and (
            not isinstance(service_id, str) or not service_id.strip()
        ):
            _raise_host_error("service_id must be a non-empty string")
        if run_id is not None:
            if service_id is None or not isinstance(run_id, str) or not run_id.strip():
                _raise_host_error("run_id requires service_id and a non-empty string")
        normalized_id = service_id.strip() if service_id is not None else None
        async with self._operation_lock:
            return await self._personal_context.get_fetch_run_status(
                normalized_id,
                run_id=run_id,
            )

    async def set_fetch_service_enabled(
        self,
        service_id: str,
        enabled: bool,
    ) -> None:
        """Persist and hot-apply one service's future scheduling switch."""

        if not isinstance(enabled, bool):
            _raise_host_error("enabled must be a boolean")
        if not isinstance(service_id, str):
            _raise_host_error("service_id must be a string")
        async with self._operation_lock:
            if self._stored_config is None:
                _raise_host_error(
                    "PersonalContext configuration must be set before changing a fetch service"
                )
            raw = deepcopy(self._stored_config)
            normalized_id = service_id.strip()
            if not normalized_id:
                _raise_host_error("service_id must not be empty")
            services = cast(list[dict[str, object]], raw["fetch_services"])
            target = next(
                (item for item in services if item["service_id"] == normalized_id),
                None,
            )
            if target is None:
                _raise_host_error("unknown PersonalContext fetch service")
            target["enabled"] = enabled
            raw, candidate = _prepare_stored_config(raw)
            await self._apply_live_update_locked(
                candidate,
                raw,
                _serialize_config(raw),
                apply=lambda: self._personal_context.set_fetch_service_enabled(
                    normalized_id,
                    enabled,
                ),
                rollback=lambda: self._personal_context.set_fetch_service_enabled(
                    normalized_id,
                    not enabled,
                ),
            )

    async def run_fetch(
        self,
        *,
        service_id: str | None = None,
    ) -> dict[str, object]:
        """Delegate one immediate fetch request without changing configuration."""

        async with self._operation_lock:
            return await self._personal_context.run_fetch(service_id=service_id)

    async def stop_fetch_run(self, service_id: str) -> dict[str, object]:
        """Stop one active fetch round without changing persisted configuration."""

        if not isinstance(service_id, str) or not service_id.strip():
            _raise_host_error("service_id must be a non-empty string")
        normalized_id = service_id.strip()
        # stop_fetch_run 不修改 Host 配置。它改用独立锁，避免一次 Core 收尾
        # 阻塞 create/patch/delete；Core 内部仍由 _fetch_lock 保护运行任务。
        async with self._fetch_run_stop_lock:
            await self._personal_context.stop_fetch_run(normalized_id)
        return {"ok": True}

    async def get_graph(
        self,
        *,
        root_id: str | None = None,
        depth: int = 3,
    ) -> dict[str, object]:
        """Read one graph slice without starting Core."""

        return await self._personal_context.get_graph(root_id=root_id, depth=depth)

    async def get_tree(
        self,
        *,
        root_id: str | None = None,
        depth: int = 3,
    ) -> dict[str, object]:
        """Read one file-tree slice without starting Core."""

        return await self._personal_context.get_tree(root_id=root_id, depth=depth)

    async def search_graph(self, query: str) -> dict[str, object]:
        """Search the last published Context pages without starting Core."""

        return await self._personal_context.search_graph(query)

    async def get_graph_page(self, node_id: str) -> dict[str, object]:
        """Read one published Context page without starting Core."""

        return await self._personal_context.get_graph_page(node_id)

    async def get_source(self, source_id: str) -> dict[str, object]:
        """Read one structured atomic-source detail without starting Core."""

        return await self._personal_context.get_source(source_id)

    async def get_authorization_status(self, provider: str) -> dict[str, object]:
        """Read the current provider credential state without exposing credentials."""

        async with self._operation_lock:
            if not isinstance(provider, str) or not provider.strip():
                _raise_host_error("provider must be a non-empty string")
            normalized_provider = provider.strip().casefold()
            if normalized_provider in _REPOSITORY_PAT_FIELDS:
                if self._stored_config is None:
                    return _repository_authorization_result(
                        normalized_provider,
                        "not_authorized",
                    )
                provider_credentials = cast(
                    dict[str, dict[str, str]],
                    self._stored_config.get("provider_credentials", {}),
                )
                current = provider_credentials.get(normalized_provider)
                field = _REPOSITORY_PAT_FIELDS[normalized_provider]
                if current is None or field not in current:
                    return _repository_authorization_result(
                        normalized_provider,
                        "not_authorized",
                    )
                try:
                    account = await _validate_repository_pat(
                        normalized_provider,
                        current[field],
                    )
                except Exception:
                    return _repository_authorization_result(
                        normalized_provider,
                        "authorization_failed",
                        error="credential verification failed",
                    )
                return _repository_authorization_result(
                    normalized_provider,
                    "authorized",
                    account=account,
                )
            if normalized_provider != "feishu":
                _raise_host_error("provider does not support authorization")
            if self._config is None:
                _raise_host_error(
                    "PersonalContext configuration must be set before provider authorization"
                )
            result: dict[str, object] | None = None
            cancelled: asyncio.CancelledError | None = None
            try:
                result = await self._personal_context.get_authorization_status(
                    normalized_provider
                )
            except asyncio.CancelledError as exc:
                cancelled = exc
            except Exception as exc:
                raise _as_host_error(
                    exc, "PersonalContext provider authorization status failed"
                ) from None
            if cancelled is not None:
                raise cancelled
            if result is None:
                _raise_host_error(
                    "PersonalContext provider authorization status returned no result"
                )
            return result

    async def authorize_provider(
        self,
        provider: str,
        credentials: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Check or begin user authorization for a configured provider."""

        async with self._operation_lock:
            if not isinstance(provider, str) or not provider.strip():
                _raise_host_error("provider must be a non-empty string")
            normalized_provider = provider.strip().casefold()
            if normalized_provider in _REPOSITORY_PAT_FIELDS:
                field = _REPOSITORY_PAT_FIELDS[normalized_provider]
                if not isinstance(credentials, dict) or set(credentials) != {field}:
                    _raise_host_error(
                        f"{normalized_provider} credentials must contain only {field}"
                    )
                secret = credentials[field]
                if not isinstance(secret, str) or not secret.strip():
                    _raise_host_error(
                        f"{normalized_provider} credential must be a non-empty string"
                    )
                normalized_secret = secret.strip()
                account = await _validate_repository_pat_for_write(
                    normalized_provider,
                    normalized_secret,
                )
                stored = (
                    deepcopy(self._stored_config)
                    if self._stored_config is not None
                    else _initial_stored_config(collection_enabled=False)
                )
                provider_credentials = _normalize_provider_credentials(
                    stored.get("provider_credentials", {})
                )
                provider_credentials[normalized_provider] = {field: normalized_secret}
                stored["provider_credentials"] = provider_credentials
                stored, candidate = _prepare_stored_config(stored)
                payload = _serialize_config(stored)
                if self._stored_config is None:
                    await self._apply_configuration_locked(candidate, stored, payload)
                else:
                    try:
                        _publish_yaml(self._config_path, payload)
                    except Exception as exc:
                        raise _as_host_error(
                            exc,
                            "PersonalContext provider credential could not be saved",
                            status_name="CONTEXT_PROACTIVE_FILE_EXECUTION_ERROR",
                        ) from None
                    self._stored_config = deepcopy(stored)
                return _repository_authorization_result(
                    normalized_provider,
                    "authorized",
                    account=account,
                )
            if normalized_provider != "feishu":
                _raise_host_error("provider does not support authorization")
            if credentials is not None:
                _raise_host_error("feishu authorization does not accept credentials")
            if self._config is None:
                _raise_host_error(
                    "PersonalContext configuration must be set before provider authorization"
                )
            try:
                return await self._personal_context.authorize_provider(
                    normalized_provider
                )
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                raise _as_host_error(
                    exc, "PersonalContext provider authorization failed"
                ) from None

    async def start(self) -> None:
        """Load the file once when needed and start the configured Core."""

        async with self._operation_lock:
            if self._config is None:
                raw = _read_yaml(self._config_path)
                if raw is None:
                    return
                stored, config = _prepare_stored_config(raw)
                try:
                    await self._personal_context.set_configuration(config)
                except BaseException as exc:
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    raise _as_host_error(
                        exc,
                        "PersonalContext configuration could not be applied",
                        status_name="CONTEXT_PROACTIVE_CONFIG_INVALID",
                    ) from None
                self._config = config
                self._stored_config = stored
            config = self._config
            if config is None or not config.collection_enabled:
                return
            try:
                self._refresh_embedding_configuration()
                await self._personal_context.activate_runtime()
            except BaseException as exc:
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise _as_host_error(
                    exc, "PersonalContext runtime could not be started"
                ) from None

    async def is_runtime_enabled(self) -> bool:
        """Return the loaded Agent-use switch without reading or parsing YAML."""

        async with self._operation_lock:
            config = self._config
            return bool(config is not None and config.agent_use_enabled)

    async def get_status(self) -> PersonalContext.Status:
        """Return the Core's bounded, credential-free status snapshot."""

        return await self._personal_context.snapshot()

    async def stop(self, *, timeout_seconds: float = _STOP_TIMEOUT_SECONDS) -> None:
        """Stop Core runtime while preserving configuration and published files."""

        if timeout_seconds <= 0:
            _raise_host_error(
                "timeout_seconds must be greater than zero",
                status_name="CONTEXT_PROACTIVE_RUNTIME_TIMEOUT",
            )
        async with self._operation_lock:
            try:
                await self._personal_context.deactivate_runtime(
                    timeout_seconds=timeout_seconds
                )
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                raise _as_host_error(
                    exc, "PersonalContext runtime could not be stopped"
                ) from None

    async def _restore_previous(
        self,
        previous: PersonalContext.Config | None,
        previous_stored: dict[str, object] | None,
        was_active: bool,
    ) -> None:
        """Restore after a failed candidate configuration operation."""

        await self._personal_context.deactivate_runtime(
            timeout_seconds=_STOP_TIMEOUT_SECONDS
        )
        if previous is None:
            self._personal_context = PersonalContext(home=self._home)
            self._config = None
            self._stored_config = None
            return
        await self._personal_context.set_configuration(previous)
        if was_active and previous.collection_enabled:
            self._refresh_embedding_configuration()
            await self._personal_context.activate_runtime()
        self._config = previous
        self._stored_config = deepcopy(previous_stored)
