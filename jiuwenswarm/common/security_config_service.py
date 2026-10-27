"""Source storage and publication for the thin security configuration facade."""

import json
import hashlib
import os
import threading
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

import portalocker
from openjiuwen.harness.security.permission_engine.policy_sources import (
    SOURCE_BUILTIN,
    SOURCE_CLOUD,
    SOURCE_USER,
)

from jiuwenswarm.common.config import dump_yaml_round_trip, load_yaml_round_trip
from jiuwenswarm.common.security_config_compose import (
    compose_candidate,
    validate_settings,
    validate_source,
)


def read_builtin_source() -> dict[str, Any]:
    """Read package data using the same core loaders as the existing engine."""
    from openjiuwen.harness.security.permission_engine.fileguard.sensitive_paths import load_package_sensitive_paths
    from openjiuwen.harness.security.permission_engine.netguard.net_urls import load_package_net_urls
    from openjiuwen.harness.security.permission_engine.toolguard.builtin_rules import load_package_command_rules
    from jiuwenswarm.common.utils import get_package_resources_dir

    resources = get_package_resources_dir()
    if resources is None:
        raise FileNotFoundError("package security template unavailable")
    template = load_yaml_round_trip(resources / "config.yaml")
    if not isinstance(template, dict):
        raise ValueError("package security template is invalid")
    template_permissions = template.get("permissions") or {}
    paths = []
    for path in load_package_sensitive_paths():
        paths.append({key: deepcopy(path[key]) for key in ("path", "match", "read", "write", "exec") if key in path})
    return {
        "permissions": {
            "tools": deepcopy(template_permissions.get("tools") or {}),
            "rules": deepcopy(load_package_command_rules()),
            "file_guard": {"paths": paths},
            "net_guard": {"urls": deepcopy(load_package_net_urls())},
        },
    }


class SecurityConfigService:
    def __init__(self, config_path: Path | None = None, *, builtin_reader: Callable[[], dict[str, Any]] = read_builtin_source):
        if config_path is None:
            from jiuwenswarm.common.utils import get_config_file
            config_path = get_config_file()
        self.config_path = Path(config_path)
        root = self.config_path.parent
        self.settings_path = root / "security.settings.yaml"
        self.local_path = root / "security.local.yaml"
        self.cloud_path = root / "security.cloud.yaml"
        self.state_path = root / "security.apply-state.json"
        self.initial_snapshot_path = root / "security.initial-config.yaml"
        self.lock_path = root / "security.config.lock"
        self._builtin_reader = builtin_reader
        self._thread_lock = threading.Lock()

    def _locked(self):
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        return portalocker.Lock(str(self.lock_path), timeout=10)

    @staticmethod
    def _read_yaml(path: Path) -> dict[str, Any]:
        if not path.is_file():
            raise FileNotFoundError(path)
        value = load_yaml_round_trip(path)
        if not isinstance(value, dict):
            raise ValueError(f"invalid YAML object: {path}")
        return value

    def _state(self) -> dict[str, Any]:
        if not self.state_path.is_file():
            state = self._pending_state()
            self._write_state(state)
            return state
        try:
            with self.state_path.open(encoding="utf-8") as stream:
                state = json.load(stream)
        except (json.JSONDecodeError, UnicodeError):
            state = None
        if not isinstance(state, dict) or not isinstance(state.get("revision"), str):
            state = self._pending_state()
            self._write_state(state)
            return state
        if state.get("source_hashes") != self._source_hashes():
            state = self._pending_state()
            self._write_state(state)
        elif state.get("effective_hash") is not None:
            live_hash = self._effective_hash(self._read_yaml(self.config_path))
            if state["effective_hash"] != live_hash:
                state = self._pending_state()
                self._write_state(state)
        return state

    @staticmethod
    def _effective_hash(config: dict[str, Any]) -> str:
        section = config.get("security") or {}
        managed = {
            "permissions": config.get("permissions") or {},
            "fail_closed": section.get("fail_closed") if isinstance(section, dict) else None,
        }
        data = json.dumps(managed, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(data.encode("utf-8")).hexdigest()

    def _source_hashes(self) -> dict[str, str | None]:
        hashes = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
            for path in (self.settings_path, self.local_path, self.cloud_path)
        }
        if self.settings_path.is_file():
            settings = validate_settings(self._read_yaml(self.settings_path))
            if any(item["id"] == SOURCE_BUILTIN and item["enabled"] for item in settings["security"]["policy_sources"]):
                builtin = json.dumps(self._builtin_reader(), sort_keys=True, ensure_ascii=False, default=str)
                hashes[SOURCE_BUILTIN] = hashlib.sha256(builtin.encode("utf-8")).hexdigest()
        return hashes

    def _write_state(self, state: dict[str, Any]) -> None:
        from jiuwenswarm.common.config import _atomic_replace
        state["source_hashes"] = self._source_hashes()
        temp = self.state_path.with_name(f"{self.state_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temp.open("w", encoding="utf-8") as stream:
                json.dump(state, stream, ensure_ascii=False, indent=2)
            _atomic_replace(temp, self.state_path)
        finally:
            if temp.exists():
                os.unlink(temp)

    @staticmethod
    def _pending_state(previous: dict[str, Any] | None = None) -> dict[str, Any]:
        state = {
            "revision": f"sec-{uuid.uuid4()}",
            "status": "applying",
            "saved": True,
            "applied": False,
            "targets": {"rail": "pending", "host_exit": "pending", "sandbox": "pending"},
        }
        for key in ("sandbox_pending", "sandbox_retry"):
            if previous and previous.get(key):
                state[key] = deepcopy(previous[key])
        return state

    def _pending_after_edit(self) -> dict[str, Any]:
        # Carry only an unfinished sandbox target; do not recompute the old
        # candidate after settings may have disabled a source.
        try:
            with self.state_path.open(encoding="utf-8") as stream:
                previous = json.load(stream)
        except (FileNotFoundError, json.JSONDecodeError, UnicodeError):
            previous = None
        return self._pending_state(previous if isinstance(previous, dict) else None)

    @staticmethod
    def _sandbox_changed_axes(before: dict[str, Any], after: dict[str, Any]) -> dict[str, bool]:
        old = before.get("permissions") or {}
        new = after.get("permissions") or {}
        return {
            axis: (old.get(module) or {}).get(field) != (new.get(module) or {}).get(field)
            or (old.get(module) or {}).get("enabled") != (new.get(module) or {}).get("enabled")
            or (
                axis == "network"
                and (old.get(module) or {}).get("defaults", "allow") != (new.get(module) or {}).get("defaults", "allow")
            )
            for axis, module, field in (("file", "file_guard", "paths"), ("network", "net_guard", "urls"))
        }

    def _compose_checked(self, settings, local, cloud, state, validator=None):
        revision = state["revision"]
        candidate = compose_candidate(settings, self._sources(settings, local, cloud), revision)
        live = self._read_yaml(self.config_path)
        if validator is not None:
            from openjiuwen.harness.security.permission_engine import core as permission_core

            if getattr(permission_core, "COMPOSITION_SCHEMA_VERSION", None) != 1:
                raise RuntimeError("installed core does not support security composition v1")
            permission_core.prepare_permissions_for_engine(candidate)
            validator(candidate, live)
        if (live.get("sandbox") or {}).get("enabled"):
            changed = self._sandbox_changed_axes(live, {"permissions": candidate})
            pending = state.setdefault("sandbox_pending", {})
            for axis, value in changed.items():
                pending[axis] = bool(pending.get(axis) or value)
        return candidate

    def _load_sources(self, settings: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        selected = {item["id"] for item in settings["security"]["policy_sources"] if item["enabled"]}
        local = validate_source(self._read_yaml(self.local_path)) if SOURCE_USER in selected else {"permissions": {}}
        cloud = validate_source(self._read_yaml(self.cloud_path), cloud=True) if self.cloud_path.is_file() and SOURCE_CLOUD in selected else {"permissions": {}}
        return local, cloud

    def _load(self) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        settings = validate_settings(self._read_yaml(self.settings_path))
        local, cloud = self._load_sources(settings)
        return settings, local, cloud

    def is_initialized(self) -> bool:
        ready = self.settings_path.is_file() and self.local_path.is_file()
        if not ready and any(path.exists() for path in (
            self.settings_path, self.local_path, self.cloud_path, self.state_path,
            self.initial_snapshot_path,
        )):
            raise RuntimeError("security sources are only partially initialized")
        return ready

    def edit_native(self, mutate: Callable[[dict[str, Any], dict[str, Any]], Any]) -> tuple[Any, dict[str, Any]]:
        """Apply one legacy module patch to its original-shaped source and settings."""
        with self._thread_lock, self._locked():
            settings = validate_settings(self._read_yaml(self.settings_path))
            local = validate_source(self._read_yaml(self.local_path))
            cloud = (
                validate_source(self._read_yaml(self.cloud_path), cloud=True)
                if self.cloud_path.is_file() and any(
                    item["id"] == SOURCE_CLOUD and item["enabled"]
                    for item in settings["security"]["policy_sources"]
                ) else {"permissions": {}}
            )
            result = mutate(settings, local)
            settings = validate_settings(settings)
            local = validate_source(local)
            state = self._pending_after_edit()
            self._compose_checked(settings, local, cloud, state)
            dump_yaml_round_trip(self.settings_path, settings)
            dump_yaml_round_trip(self.local_path, local)
            self._write_state(state)
            return result, deepcopy(state)

    def _sources(self, settings: dict[str, Any], local: dict[str, Any], cloud: dict[str, Any]) -> dict[str, dict[str, Any]]:
        sources = {SOURCE_USER: local, SOURCE_CLOUD: cloud}
        if any(item["id"] == SOURCE_BUILTIN and item["enabled"] for item in settings["security"]["policy_sources"]):
            sources[SOURCE_BUILTIN] = self._builtin_reader()
        return sources

    def initialize_from_live(self, *, validator=None) -> dict[str, Any]:
        """Adopt the old config once; never extract from a derived snapshot again."""
        with self._thread_lock, self._locked():
            if any(path.exists() for path in (
                self.settings_path, self.local_path, self.cloud_path,
                self.state_path, self.initial_snapshot_path,
            )):
                raise RuntimeError("security sources already initialized or partially initialized")
            live = self._read_yaml(self.config_path)
            permissions = deepcopy(live.get("permissions") or {})
            if not isinstance(permissions, dict):
                raise ValueError("permissions must be an object")
            local_permissions = {}
            for key in ("tools", "rules", "approval_overrides"):
                if key in permissions:
                    local_permissions[key] = permissions.pop(key)
            for guard_name, rule_key in (("file_guard", "paths"), ("net_guard", "urls")):
                guard = permissions.get(guard_name)
                if isinstance(guard, dict) and rule_key in guard:
                    local_permissions[guard_name] = {rule_key: guard.pop(rule_key)}
            settings = validate_settings({
                "security": {}, "permissions": permissions,
                **{key: deepcopy(live[key]) for key in ("sandbox", "xiaoyi_work_security") if key in live},
            })
            local = validate_source({"permissions": local_permissions})
            state = self._pending_state()
            self._compose_checked(settings, local, {"permissions": {}}, state, validator)
            from jiuwenswarm.server.sandbox_policy_render import initialize_native_sandbox_network
            initialize_native_sandbox_network(self.config_path, (live.get("sandbox") or {}).get("urls") or {})
            dump_yaml_round_trip(self.initial_snapshot_path, live)
            dump_yaml_round_trip(self.settings_path, settings)
            dump_yaml_round_trip(self.local_path, local)
            self._write_state(state)
            return deepcopy(state)

    def get_candidate(self, *, include_disabled_sources: bool = False) -> dict[str, Any]:
        with self._thread_lock, self._locked():
            settings, local, cloud = self._load()
            state = self._state()
            sources = self._sources(settings, local, cloud)
            candidate = compose_candidate(settings, sources, state["revision"])
            source_errors: dict[str, str] = {}
            builtin: dict[str, Any] = {"permissions": {}}
            if include_disabled_sources:
                # Only the public read view inspects saved, disabled rules.
                # Startup and apply must not parse dormant source files.
                enabled = {item["id"] for item in settings["security"]["policy_sources"] if item["enabled"]}
                if SOURCE_BUILTIN in enabled:
                    builtin = sources[SOURCE_BUILTIN]
                else:
                    try:
                        builtin = self._builtin_reader()
                    except Exception as exc:
                        source_errors[SOURCE_BUILTIN] = str(exc)
                for source_id, path, is_cloud in (
                    (SOURCE_USER, self.local_path, False),
                    (SOURCE_CLOUD, self.cloud_path, True),
                ):
                    if source_id in enabled or (is_cloud and not path.is_file()):
                        continue
                    try:
                        view = validate_source(self._read_yaml(path), cloud=is_cloud)
                    except Exception as exc:
                        source_errors[source_id] = str(exc)
                        continue
                    if is_cloud:
                        cloud = view
                    else:
                        local = view
            result = {
                "settings": settings, "local": local, "cloud": cloud,
                "permissions": candidate, "apply": state,
            }
            if include_disabled_sources:
                result.update(builtin=builtin, source_errors=source_errors)
            return result

    def get_builtin(self, module: str) -> dict[str, Any]:
        key_map = {
            "file": ("file_guard", "paths"),
            "network": ("net_guard", "urls"),
            "command": (None, "rules"),
            "tool": (None, "tools"),
        }
        if not isinstance(module, str) or module not in key_map:
            raise ValueError(f"unknown security module: {module}")
        with self._thread_lock, self._locked():
            settings = validate_settings(self._read_yaml(self.settings_path))
            enabled = any(
                item["id"] == SOURCE_BUILTIN and item["enabled"]
                for item in settings["security"]["policy_sources"]
            )
            section, field = key_map[module]
            permissions = self._builtin_reader().get("permissions") or {}
            value = (permissions.get(section) or {}).get(field) if section else permissions.get(field)
            if value is None:
                value = [] if module in {"file", "command"} else {}
            return {"module": module, "enabled": enabled, "value": deepcopy(value)}

    def publish_candidate(
        self, *, expected_revision: str | None = None,
        validator: Callable[[dict[str, Any], dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Publish the current candidate only when the imported core can enforce it."""
        from openjiuwen.harness.security.permission_engine import core as permission_core

        if getattr(permission_core, "COMPOSITION_SCHEMA_VERSION", None) != 1:
            raise RuntimeError("installed core does not support security composition v1")
        with self._thread_lock, self._locked():
            self._check_revision(expected_revision)
            settings, local, cloud = self._load()
            state = self._state()
            permissions = compose_candidate(settings, self._sources(settings, local, cloud), state["revision"])
            permission_core.prepare_permissions_for_engine(permissions)
            from jiuwenswarm.common import config as config_module

            previous = {}

            def mutate(config):
                if validator is not None:
                    validator(permissions, config)
                previous["permissions"] = deepcopy(config.get("permissions") or {})
                previous["sandbox"] = deepcopy(config.get("sandbox") or {})
                config["permissions"] = deepcopy(permissions)
                config.setdefault("security", {})["fail_closed"] = deepcopy(settings["security"]["fail_closed"])
                return config

            if self.config_path.resolve() == Path(config_module.CONFIG_YAML_PATH).resolve():
                published = config_module.update_config(mutate)
            else:
                with portalocker.Lock(str(config_module._config_lock_path(self.config_path)), timeout=10):
                    published = mutate(self._read_yaml(self.config_path))
                    dump_yaml_round_trip(self.config_path, published)
            state.update(status="applying", saved=True, applied=False)
            state["targets"] = {"rail": "pending", "host_exit": "pending", "sandbox": "pending"}
            if (published.get("sandbox") or {}).get("enabled"):
                changed = self._sandbox_changed_axes(
                    {"permissions": previous["permissions"]}, published,
                )
                pending = state.setdefault("sandbox_pending", {})
                for axis, value in changed.items():
                    pending[axis] = bool(pending.get(axis) or value)
            state.pop("sandbox_file_skipped", None)
            state.pop("sandbox_network_skipped", None)
            state["effective_hash"] = self._effective_hash(published)
            self._write_state(state)
            return {"config": published, "previous": previous, "apply": deepcopy(state)}

    def snapshot_for_rollback(self) -> dict[str, Any]:
        """Capture the last accepted files before a managed update changes them."""
        with self._thread_lock, self._locked():
            paths = (self.config_path, self.settings_path, self.local_path, self.cloud_path)
            return {
                "files": {path: path.read_bytes() if path.is_file() else None for path in paths},
                "state": deepcopy(self._state()) if self.state_path.is_file() else None,
            }

    def restore_snapshot(self, snapshot: dict[str, Any], *, expected_revision: str) -> dict[str, Any] | None:
        """Discard a failed revision and restore its predecessor's disk state."""
        from jiuwenswarm.common.config import _atomic_replace, _config_lock_path

        with self._thread_lock, self._locked():
            if self._state()["revision"] != expected_revision:
                raise ValueError("VERSION_CONFLICT")

            def restore(path: Path, contents: bytes | None) -> None:
                if contents is None:
                    path.unlink(missing_ok=True)
                    return
                temp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
                try:
                    temp.write_bytes(contents)
                    _atomic_replace(temp, path)
                finally:
                    temp.unlink(missing_ok=True)

            files = snapshot["files"]
            with portalocker.Lock(str(_config_lock_path(self.config_path)), timeout=10):
                restore(self.config_path, files[self.config_path])
            for path in (self.settings_path, self.local_path, self.cloud_path):
                restore(path, files[path])
            old_state = snapshot["state"]
            if old_state is None:
                self.state_path.unlink(missing_ok=True)
                self.initial_snapshot_path.unlink(missing_ok=True)
            else:
                self._write_state(old_state)
            return deepcopy(old_state)

    def record_apply(
        self, revision: str, targets: dict[str, str],
        *, sandbox_file_skipped: list[dict[str, str]] | None = None,
        sandbox_network_skipped: list[dict[str, str]] | None = None,
        sandbox_retry: dict[str, bool] | None = None,
    ) -> dict[str, Any]:
        """Record acknowledgements for one revision; a newer save cannot be overwritten."""
        with self._thread_lock, self._locked():
            state = self._state()
            if state["revision"] != revision:
                raise ValueError("VERSION_CONFLICT")
            if set(targets) != {"rail", "host_exit", "sandbox"}:
                raise ValueError("all apply targets must be reported")
            state["targets"] = deepcopy(targets)
            state["sandbox_file_skipped"] = deepcopy(sandbox_file_skipped or [])
            state["sandbox_network_skipped"] = deepcopy(sandbox_network_skipped or [])
            state["sandbox_retry"] = deepcopy(state.get("sandbox_retry", {}) if sandbox_retry is None else sandbox_retry)
            if targets["sandbox"] == "applied" or sandbox_retry is not None:
                state["sandbox_pending"] = {}
            state["applied"] = (
                all(value in {"applied", "not_required"} for value in targets.values())
                and not any((state.get("sandbox_pending") or {}).values())
                and not any((state.get("sandbox_retry") or {}).values())
            )
            state["status"] = "applied" if state["applied"] else "failed"
            self._write_state(state)
            return deepcopy(state)

    def _check_revision(self, expected_revision: str | None) -> None:
        if expected_revision is not None and self._state()["revision"] != expected_revision:
            raise ValueError("VERSION_CONFLICT")

    def update_settings(self, patch: dict[str, Any], *, expected_revision: str | None = None, validator=None) -> dict[str, Any]:
        if not isinstance(patch, dict) or set(patch) - {"policy_sources", "fail_closed"}:
            raise ValueError("settings patch accepts policy_sources/fail_closed only")
        with self._thread_lock, self._locked():
            self._check_revision(expected_revision)
            settings = validate_settings(self._read_yaml(self.settings_path))
            settings["security"].update(deepcopy(patch))
            settings = validate_settings(settings)
            local, cloud = self._load_sources(settings)
            state = self._pending_after_edit()
            self._compose_checked(settings, local, cloud, state, validator)
            dump_yaml_round_trip(self.settings_path, settings)
            self._write_state(state)
            return deepcopy(state)

    def update_local(self, module: str, value: Any, *, expected_revision: str | None = None, validator=None) -> dict[str, Any]:
        """Replace one module's user rules; native validation remains in the helpers."""
        with self._thread_lock, self._locked():
            self._check_revision(expected_revision)
            settings, local, cloud = self._load()
            if not any(item["id"] == SOURCE_USER and item["enabled"] for item in settings["security"]["policy_sources"]):
                local = validate_source(self._read_yaml(self.local_path))
            perms = local.setdefault("permissions", {})
            if module == "file":
                from jiuwenswarm.common.file_guard_config import patch_file_guard
                patch_file_guard(local, {"paths": value})
            elif module == "network":
                perms["net_guard"] = {"urls": deepcopy(value)}
            elif module == "command":
                perms["rules"] = deepcopy(value)
            elif module == "tool":
                perms["tools"] = deepcopy(value)
            else:
                raise ValueError(f"unknown security module: {module}")
            local = validate_source(local)
            state = self._pending_after_edit()
            self._compose_checked(settings, local, cloud, state, validator)
            dump_yaml_round_trip(self.local_path, local)
            self._write_state(state)
            return deepcopy(state)

    def update_module_settings(self, module: str, patch: dict[str, Any], *, expected_revision: str | None = None, validator=None) -> dict[str, Any]:
        """Route module switches/defaults to their native configuration shape."""
        if not isinstance(patch, dict):
            raise ValueError("settings patch must be an object")
        with self._thread_lock, self._locked():
            self._check_revision(expected_revision)
            settings, local, cloud = self._load()
            if module == "file":
                from jiuwenswarm.common.file_guard_config import patch_file_guard
                if "paths" in patch:
                    raise ValueError("file paths belong to the local source")
                patch_file_guard(settings, patch)
            elif module == "network":
                from jiuwenswarm.agents.harness.common.rails.permissions.permissions_persist import normalize_net_guard_patch
                if set(patch) - {"enabled", "defaults", "enforce_host_exit", "max_request_mb", "timeout_seconds"}:
                    raise ValueError("network settings accept enabled/defaults/enforce_host_exit/max_request_mb/timeout_seconds")
                normalized = normalize_net_guard_patch(patch)
                settings.setdefault("permissions", {}).setdefault("net_guard", {}).update(normalized)
            elif module == "command":
                from jiuwenswarm.common.shell_guard_config import patch_shell_guard
                patch_shell_guard(settings, patch)
            elif module == "permissions":
                if set(patch) - {"enabled", "permission_mode", "package_builtin_rules", "defaults"}:
                    raise ValueError("unsupported permissions setting")
                for key in ("enabled", "package_builtin_rules"):
                    if key in patch and not isinstance(patch[key], bool):
                        raise ValueError(f"permissions.{key} must be boolean")
                if "permission_mode" in patch and patch["permission_mode"] not in {"normal", "strict"}:
                    raise ValueError("permission_mode must be normal or strict")
                if "defaults" in patch:
                    defaults = patch["defaults"]
                    if not isinstance(defaults, dict) or any(
                        not isinstance(name, str) or not isinstance(level, str) or level not in {"allow", "ask", "deny"}
                        for name, level in defaults.items()
                    ):
                        raise ValueError("permissions.defaults must map tools to allow/ask/deny")
                settings.setdefault("permissions", {}).update(deepcopy(patch))
            else:
                raise ValueError(f"unknown security module: {module}")
            state = self._pending_after_edit()
            self._compose_checked(settings, local, cloud, state, validator)
            dump_yaml_round_trip(self.settings_path, settings)
            self._write_state(state)
            return deepcopy(state)

    def update_permission_profile(self, patch: dict[str, Any], *, expected_revision: str | None = None, validator=None) -> dict[str, Any]:
        """Apply the desktop permission preset to settings and user tool baselines together."""
        fields = {"enabled", "permission_mode", "bash_level", "network_level", "file_guard_default_rw"}
        if not isinstance(patch, dict) or set(patch) != fields:
            raise ValueError("permission profile requires enabled/mode/tool/file levels")
        if (not isinstance(patch["enabled"], bool) or not isinstance(patch["permission_mode"], str)
                or patch["permission_mode"] not in {"normal", "strict"}):
            raise ValueError("invalid permission profile mode")
        for key in ("bash_level", "network_level", "file_guard_default_rw"):
            if not isinstance(patch[key], str) or patch[key] not in {"allow", "ask"}:
                raise ValueError(f"invalid permission profile {key}")
        with self._thread_lock, self._locked():
            self._check_revision(expected_revision)
            settings, local, cloud = self._load()
            if not any(
                item["id"] == SOURCE_USER and item["enabled"]
                for item in settings["security"]["policy_sources"]
            ):
                local = validate_source(self._read_yaml(self.local_path))
            permissions = settings.setdefault("permissions", {})
            permissions["enabled"] = patch["enabled"]
            permissions["permission_mode"] = patch["permission_mode"]
            file_guard = permissions.setdefault("file_guard", {})
            file_guard.setdefault("enabled", True)
            defaults = file_guard.setdefault("defaults", {})
            defaults.setdefault("exec", "allow")
            defaults.update(read=patch["file_guard_default_rw"], write=patch["file_guard_default_rw"])
            tools = local.setdefault("permissions", {}).setdefault("tools", {})
            tools.update({
                "bash": patch["bash_level"],
                "mcp_free_search": patch["network_level"],
                "mcp_paid_search": patch["network_level"],
                "mcp_fetch_webpage": patch["network_level"],
            })
            settings = validate_settings(settings)
            local = validate_source(local)
            state = self._pending_after_edit()
            self._compose_checked(settings, local, cloud, state, validator)
            dump_yaml_round_trip(self.settings_path, settings)
            dump_yaml_round_trip(self.local_path, local)
            self._write_state(state)
            return deepcopy(state)

    def persist_user_approval(self, grant: dict[str, Any]) -> bool:
        """Persist one Rail-generated grant without copying composed rules to user."""
        return self.persist_user_approvals([grant])

    def persist_user_approvals(self, approvals: list[dict[str, Any]]) -> bool:
        """Validate and persist an approval batch under one lock and revision."""
        if not approvals or any(
            not isinstance(grant, dict) or not isinstance(grant.get("id"), str) or not grant["id"]
            for grant in approvals
        ):
            raise ValueError("approval grant requires an id")
        with self._thread_lock, self._locked():
            settings, local, cloud = self._load()
            if not any(item["id"] == SOURCE_USER and item["enabled"] for item in settings["security"]["policy_sources"]):
                return False
            grants = local.setdefault("permissions", {}).setdefault("approval_overrides", [])
            by_id = {item["id"]: deepcopy(item) for item in approvals}
            grants[:] = [item for item in grants if item.get("id") not in by_id]
            grants.extend(by_id.values())
            local = validate_source(local)
            state = self._pending_after_edit()
            self._compose_checked(settings, local, cloud, state)
            dump_yaml_round_trip(self.local_path, local)
            self._write_state(state)
        try:
            self.publish_candidate(expected_revision=state["revision"])
            sandbox_pending = any((state.get("sandbox_pending") or {}).values())
            sandbox_retry = any((state.get("sandbox_retry") or {}).values())
            self.record_apply(state["revision"], {
                "rail": "applied", "host_exit": "not_required",
                "sandbox": "pending" if sandbox_pending or sandbox_retry else "not_required",
            })
        except Exception:
            return False
        return True

    def replace_cloud_policy(self, policy_version: int, permissions: dict[str, Any], *, validator=None) -> dict[str, Any]:
        """Trusted internal adapter only; do not register this as a public RPC."""
        cloud = validate_source({"policy_version": policy_version, "permissions": permissions}, cloud=True)
        if policy_version < 1:
            raise ValueError("policy_version must be positive")
        with self._thread_lock, self._locked():
            settings, local, current = self._load()
            if self.cloud_path.is_file():
                current = validate_source(self._read_yaml(self.cloud_path), cloud=True)
            previous_version = current.get("policy_version", 0)
            if policy_version < previous_version:
                raise ValueError("stale cloud policy_version")
            if policy_version == previous_version:
                if cloud == current:
                    return deepcopy(self._state())
                raise ValueError("conflicting cloud policy_version")
            state = self._pending_after_edit()
            self._compose_checked(settings, local, cloud, state, validator)
            dump_yaml_round_trip(self.cloud_path, cloud)
            self._write_state(state)
            return deepcopy(state)
