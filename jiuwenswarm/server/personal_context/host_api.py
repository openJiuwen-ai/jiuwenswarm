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

import yaml

from openjiuwen.harness.personal_context import PersonalContext

from jiuwenswarm.common.config import get_default_models


_CONFIG_FILENAME = "personal_context.yaml"
_MAX_CONFIG_BYTES = 4 * 1024 * 1024
_STOP_TIMEOUT_SECONDS = 30.0
_PERSONAL_CONTEXT_MODEL_MAX_RETRIES = 2


def _initial_stored_config(*, collection_enabled: bool) -> dict[str, object]:
    return {
        "collection_enabled": collection_enabled,
        "agent_use_enabled": True,
        "strategy_profile": "rules",
        "fetch_services": [],
    }


def _unconfigured_projection() -> dict[str, object]:
    return {
        "configured": False,
        "collection_enabled": False,
        "agent_use_enabled": False,
        "strategy_profile": "rules",
        "model_index": None,
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


def _build_core_config(stored: dict[str, object]) -> PersonalContext.Config:
    raw = deepcopy(stored)
    model_index = raw.pop("model_index", None)
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


def _prepare_stored_config(
    config: dict[str, object],
) -> tuple[dict[str, object], PersonalContext.Config]:
    if not isinstance(config, dict):
        _raise_host_error("PersonalContext configuration must be an object")
    stored = deepcopy(config)
    stored.pop("model_client", None)
    stored.pop("model_request", None)
    candidate = _build_core_config(stored)
    normalized = candidate.model_dump(mode="json", by_alias=True)
    normalized.pop("model_client", None)
    normalized.pop("model_request", None)
    if "model_index" in stored:
        normalized["model_index"] = stored["model_index"]
    return normalized, candidate


class PersonalContextHostAPI:
    """The only JiuwenSwarm API for configuring and controlling embedded PersonalContext."""

    def __init__(self, *, home: str | Path) -> None:
        self._home = Path(home).expanduser().resolve()
        self._config_path = self._home / _CONFIG_FILENAME
        self._personal_context = PersonalContext(home=self._home)
        self._config: PersonalContext.Config | None = None
        self._stored_config: dict[str, object] | None = None
        self._operation_lock = asyncio.Lock()

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
        same_configuration = previous is not None and previous == candidate

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
            config = deepcopy(self._stored_config)
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
            return deepcopy(self._stored_config)

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
            return deepcopy(stored)

    async def select_model(self, model_index: int) -> dict[str, object]:
        """Select one current JiuwenSwarm model by its models.list index."""

        if type(model_index) is not int or model_index < 0:
            _raise_host_error("model_index must be a non-negative integer")
        async with self._operation_lock:
            if self._stored_config is None:
                _raise_host_error("PersonalContext is not configured")
            stored = deepcopy(self._stored_config)
            stored["model_index"] = model_index
            stored, candidate = _prepare_stored_config(stored)
            await self._apply_configuration_locked(
                candidate,
                stored,
                _serialize_config(stored),
            )
            return deepcopy(stored)

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
                        self._personal_context.start_collection
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
                        else self._personal_context.start_collection
                    ),
                    publish_before_apply=not enabled,
                )
            result = deepcopy(stored)
            if first_start:
                result["model_index"] = None
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
            return deepcopy(stored)

    async def list_fetch_services(self) -> list[dict[str, object]]:
        """Return every fixed fetch service configuration."""

        async with self._operation_lock:
            if self._stored_config is None:
                return []
            return cast(
                list[dict[str, object]],
                deepcopy(self._stored_config["fetch_services"]),
            )

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
            services.append(deepcopy(service))
            stored, candidate = _prepare_stored_config(stored)
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
            return deepcopy(created)

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
            try:
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
            "credentials",
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
            return deepcopy(updated)

    async def get_fetch_run_status(
        self,
        service_id: str | None = None,
    ) -> dict[str, object]:
        """Return current run state and last retained error for fetch services."""

        if service_id is not None and (
            not isinstance(service_id, str) or not service_id.strip()
        ):
            _raise_host_error("service_id must be a non-empty string")
        normalized_id = service_id.strip() if service_id is not None else None
        async with self._operation_lock:
            configured_ids: list[str] = []
            if self._stored_config is not None:
                services = cast(
                    list[dict[str, object]],
                    self._stored_config["fetch_services"],
                )
                configured_ids = [cast(str, item["service_id"]) for item in services]
            if normalized_id is not None and normalized_id not in configured_ids:
                _raise_host_error("unknown PersonalContext fetch service")
            status = await self._personal_context.snapshot()
            progress_by_service = getattr(status, "fetch_run_progress", {})

            def project(item_id: str) -> dict[str, object]:
                progress = progress_by_service.get(item_id)
                if isinstance(progress, dict):
                    return deepcopy(progress)
                return {
                    "service_id": item_id,
                    "run_state": "idle",
                    "progress_percent": 0,
                    "total_items": 0,
                    "completed_items": 0,
                    "last_error": None,
                }

            if normalized_id is not None:
                return project(normalized_id)
            return {"services": [project(item_id) for item_id in configured_ids]}

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
        """Stop one service's active fetch run without changing its enabled flag.

        Unlike stop_service (which toggles the scheduled-fetch `enabled` flag),
        this aborts the in-flight fetch task only — the auto-fetch schedule is
        preserved. Delegates to the Core's stop_fetch_service.
        """

        if not isinstance(service_id, str) or not service_id.strip():
            _raise_host_error("service_id must be a non-empty string")
        normalized_id = service_id.strip()
        async with self._operation_lock:
            try:
                await self._personal_context.stop_fetch_service(normalized_id)
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                raise _as_host_error(
                    exc,
                    "PersonalContext fetch run could not be stopped",
                    status_name="CONTEXT_PROACTIVE_FETCH_EXECUTION_ERROR",
                ) from None
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
        """Read provider authorization status without storing Host-side state."""

        async with self._operation_lock:
            if self._config is None:
                _raise_host_error(
                    "PersonalContext configuration must be set before provider authorization"
                )
            result: dict[str, object] | None = None
            cancelled: asyncio.CancelledError | None = None
            try:
                result = await self._personal_context.get_authorization_status(provider)
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

    async def authorize_provider(self, provider: str) -> dict[str, object]:
        """Check or begin user authorization for a configured provider."""

        async with self._operation_lock:
            if self._config is None:
                _raise_host_error(
                    "PersonalContext configuration must be set before provider authorization"
                )
            try:
                return await self._personal_context.authorize_provider(provider)
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
            await self._personal_context.activate_runtime()
        self._config = previous
        self._stored_config = deepcopy(previous_stored)
