"""Published RSI Harness validation and activation persistence.

The RSI engine owns optimization and publication.  This module only consumes
the engine's final ``current_harness_refs.yaml`` and keeps the service-side
active version independent from the legacy AutoHarness registry.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from jiuwenswarm.agents.harness.common.rsi.errors import (
    RsiBadRequest,
    RsiHarnessInstallConflict,
    RsiHarnessInstallFailed,
    RsiHarnessInvalid,
    RsiHarnessNotPublished,
    RsiHarnessNotReady,
    RsiTaskStateConflict,
)
from jiuwenswarm.agents.harness.common.rsi.plugin_catalog import register_harness_plugin

logger = logging.getLogger(__name__)

_MANIFEST_NAMES = ("manifest.json", "harness_config.yaml", "expert_harness.yaml", "harness.yaml")
_ACTIVATION_SCHEMA_VERSION = 1
_NATIVE_HARNESS_BASELINE_CONFIG = Path(__file__).with_name("harness_config.yaml")


@dataclass(frozen=True, slots=True)
class PublishedHarnessRef:
    """The one role/package selected from a published refs document."""

    refs_path: Path
    role: str
    package_path: Path
    metadata: dict[str, Any]
    refs_payload: dict[str, Any]

    @property
    def config_path(self) -> Path:
        """Return the package's legacy Harness manifest path."""

        for name in _MANIFEST_NAMES:
            candidate = self.package_path / name
            if candidate.is_file():
                return candidate
        # The parser guarantees this branch is unreachable; retaining a clear
        # error makes the property safe for callers constructing the dataclass.
        raise RsiHarnessInvalid(f"Harness 包缺少配置文件: {self.package_path}")


def _invalid(message: str) -> RsiHarnessInvalid:
    return RsiHarnessInvalid(message)


def _resolve_ref(raw_ref: Any, refs_path: Path) -> Path:
    if not isinstance(raw_ref, str) or not raw_ref.strip():
        raise _invalid("harness_refs 中的 Harness 路径不能为空")
    raw_path = Path(raw_ref).expanduser()
    if raw_path.is_absolute():
        return raw_path.resolve(strict=False)
    return (refs_path.parent / raw_path).resolve(strict=False)


def _ensure_inside(path: Path, root: Path, *, label: str) -> None:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError as exc:
        raise _invalid(f"{label}超出任务 run 根目录: {path}") from exc


def _ensure_package_tree_inside(package: Path) -> None:
    """Reject package symlinks which resolve outside the copied package."""

    package_root = package.resolve(strict=True)
    for item in package.rglob("*"):
        if not item.is_symlink():
            continue
        try:
            item.resolve(strict=True).relative_to(package_root)
        except (OSError, ValueError) as exc:
            raise _invalid(f"Harness 包含越界软链接: {item}") from exc


def _find_manifest(package: Path) -> Path:
    # Keep the package discovery order identical to the public DeepAgent
    # loader: a modern ``manifest.json`` wins over legacy YAML manifests.
    try:
        from openjiuwen.harness.resources import find_plugin_manifest
    except ImportError:  # pragma: no cover - compatibility with old SDKs
        find_plugin_manifest = None
    if find_plugin_manifest is not None:
        try:
            return Path(find_plugin_manifest(package)).expanduser().resolve(strict=True)
        except (FileNotFoundError, OSError, ValueError):
            pass
    for name in _MANIFEST_NAMES:
        candidate = package / name
        if candidate.is_file():
            return candidate
    raise _invalid(f"Harness 包缺少配置文件: {package}")


def _validate_engine_manifest(package: Path) -> Path:
    """Parse the package with openjiuwen before copying it.

    The actual resource construction remains owned by ``DeepAgent.load_plugin``
    during activation.  Parsing here closes the no-live-agent gap: a task can
    still be installed when no AgentServer instance is currently cached, so a
    malformed final package must be rejected before its active pointer is
    committed.  Older SDKs without the public package loader retain the
    structural checks above and defer semantic validation to ``load_plugin``.
    """

    manifest = _find_manifest(package)
    try:
        from openjiuwen.harness.resources import load_plugin_package
    except ImportError:  # pragma: no cover - compatibility with old SDKs
        return manifest
    try:
        load_plugin_package(manifest)
    except Exception as exc:  # noqa: BLE001 - normalize SDK parser errors
        raise _invalid(f"Harness manifest 无法通过 openjiuwen 解析: {manifest}") from exc

    # Reuse JiuwenSwarm's existing security guards for legacy YAML packages.
    # They reject disallowed MCP declarations and resource paths escaping the
    # package root, while modern JSON manifests are fully checked by the SDK
    # parser above.
    if manifest.name != "manifest.json":
        try:
            from jiuwenswarm.agents.harness.common.auto_harness.service import (
                validate_harness_config,
            )

            validate_harness_config(manifest, package_dir=package)
        except RsiHarnessInvalid:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize security errors
            raise _invalid(f"Harness 配置安全校验失败: {manifest}") from exc
    return manifest


def resolve_native_harness_baseline() -> Path | None:
    """Return the module-local no-capability baseline used without a plugin."""

    if not _NATIVE_HARNESS_BASELINE_CONFIG.is_file():
        return None
    try:
        _validate_engine_manifest(_NATIVE_HARNESS_BASELINE_CONFIG.parent)
    except RsiHarnessInvalid:
        return None
    return _NATIVE_HARNESS_BASELINE_CONFIG.resolve()


def _load_refs(refs_path: Path) -> dict[str, Any]:
    if not refs_path.is_file():
        raise _invalid(f"published harness refs 不存在: {refs_path}")
    try:
        payload = yaml.safe_load(refs_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise _invalid(f"published harness refs 不可读: {refs_path}") from exc
    if not isinstance(payload, dict):
        raise _invalid("published harness refs 必须是 mapping")
    return payload


def _role_metadata(
    payload: dict[str, Any],
    *,
    role: str,
    resolved_path: Path,
    refs_path: Path,
) -> dict[str, Any]:
    raw_roles = payload.get("roles")
    if raw_roles is None:
        return {"role": role, "member_name": role, "harness_ref_path": str(resolved_path)}
    if not isinstance(raw_roles, list):
        raise _invalid("published harness refs 的 roles 必须是 list")

    matches: list[dict[str, Any]] = []
    for raw_entry in raw_roles:
        if not isinstance(raw_entry, dict):
            continue
        entry_role = str(raw_entry.get("role") or "").strip()
        entry_member = str(raw_entry.get("member_name") or "").strip()
        if entry_role == role or entry_member == role:
            matches.append(raw_entry)
    if len(matches) != 1:
        raise _invalid(f"roles 中必须存在与 harness_refs role '{role}' 对应的唯一项")

    metadata = dict(matches[0])
    entry_ref = metadata.get("harness_ref_path")
    if entry_ref is not None and str(entry_ref).strip():
        entry_path = _resolve_ref(entry_ref, refs_path)
        if entry_path != resolved_path:
            raise _invalid(f"roles[{role}] 的 harness_ref_path 与 harness_refs 不一致")
    entry_role = str(metadata.get("role") or "").strip()
    entry_member = str(metadata.get("member_name") or "").strip()
    if entry_role and entry_role != role and entry_member != role:
        raise _invalid(f"roles[{role}] 的 role 与 harness_refs 不一致")
    if entry_member and entry_member != role and entry_role != role:
        raise _invalid(f"roles[{role}] 的 member_name 与 harness_refs 不一致")
    metadata["harness_ref_path"] = str(resolved_path)
    return metadata


def parse_published_harness_refs(
    refs_path: Path | str,
    *,
    task_run_root: Path | str,
) -> PublishedHarnessRef:
    """Parse one final engine refs file and enforce its task boundary.

    Relative refs are deliberately resolved against ``refs_path.parent`` to
    match openjiuwen's engine loader.  The install API accepts only the final
    refs file produced under a task run, so the resolved package must remain
    below ``task_run_root``.
    """

    refs = Path(refs_path).expanduser().resolve(strict=False)
    run_root = Path(task_run_root).expanduser().resolve(strict=False)
    _ensure_inside(refs, run_root, label="published refs")
    payload = _load_refs(refs)
    raw_harness_refs = payload.get("harness_refs")
    if not isinstance(raw_harness_refs, dict) or not raw_harness_refs:
        raise _invalid("published harness refs 缺少 harness_refs mapping")

    entries = [
        (str(role).strip(), raw_ref)
        for role, raw_ref in raw_harness_refs.items()
        if str(role).strip() and isinstance(raw_ref, str) and raw_ref.strip()
    ]
    if len(entries) != 1:
        raise _invalid("单 Harness 发布物必须恰好一个 role")
    role, raw_ref = entries[0]
    package = _resolve_ref(raw_ref, refs)
    _ensure_inside(package, run_root, label="Harness 包")
    if not package.is_dir():
        raise _invalid(f"Harness 包目录不存在: {package}")
    _ensure_package_tree_inside(package)
    _validate_engine_manifest(package)
    metadata = _role_metadata(payload, role=role, resolved_path=package, refs_path=refs)
    return PublishedHarnessRef(
        refs_path=refs,
        role=role,
        package_path=package,
        metadata=metadata,
        refs_payload=payload,
    )


def hash_harness_package(package_path: Path | str) -> str:
    """Compute a deterministic SHA-256 over package paths and contents."""

    package = Path(package_path).expanduser().resolve(strict=True)
    if not package.is_dir():
        raise ValueError(f"Harness package must be a directory: {package}")
    _ensure_package_tree_inside(package)

    digest = hashlib.sha256()
    for item in sorted(package.rglob("*"), key=lambda path: path.relative_to(package).as_posix()):
        relative = item.relative_to(package).as_posix().encode("utf-8")
        if item.is_symlink():
            target = os.readlink(item).encode("utf-8")
            digest.update(b"L")
            digest.update(len(relative).to_bytes(8, "big"))
            digest.update(relative)
            digest.update(len(target).to_bytes(8, "big"))
            digest.update(target)
            continue
        if not item.is_file():
            continue
        digest.update(b"F")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        with item.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


class RsiHarnessActivationStore:
    """Persistent RSI-only active pointer and immutable installation history."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser().resolve(strict=False)
        self.activation_path = self.root / "activation.json"

    def _read_document(self) -> dict[str, Any]:
        if not self.activation_path.is_file():
            return {"schema_version": _ACTIVATION_SCHEMA_VERSION, "active": None, "history": []}
        try:
            payload = json.loads(self.activation_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"无法读取 RSI Harness activation.json: {self.activation_path}") from exc
        if not isinstance(payload, dict):
            raise ValueError("RSI Harness activation.json 必须是 mapping")
        history = payload.get("history", [])
        if not isinstance(history, list):
            history = []
        return {
            "schema_version": payload.get("schema_version", _ACTIVATION_SCHEMA_VERSION),
            "active": payload.get("active"),
            "history": history,
        }

    def _validate_runtime_path(self, value: Any, *, require_exists: bool) -> Path | None:
        if not isinstance(value, str) or not value.strip():
            return None
        runtime = Path(value).expanduser().resolve(strict=False)
        try:
            runtime.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(f"RSI Harness runtime_path 不在 activation root 内: {runtime}") from exc
        if require_exists and not runtime.is_dir():
            return None
        return runtime

    def get_active(self) -> dict[str, Any] | None:
        active = self._read_document().get("active")
        if not isinstance(active, dict):
            return None
        if self._validate_runtime_path(active.get("runtime_path"), require_exists=True) is None:
            return None
        return dict(active)

    def list_history(self) -> list[dict[str, Any]]:
        history = self._read_document().get("history", [])
        return [dict(item) for item in history if isinstance(item, dict)]

    def list_versions(self) -> list[dict[str, Any]]:
        """Return every retained installation once, ordered by first install."""

        document = self._read_document()
        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in [*document.get("history", []), document.get("active")]:
            if not isinstance(item, dict):
                continue
            installation_id = str(item.get("installation_id") or "").strip()
            if not installation_id or installation_id in seen:
                continue
            seen.add(installation_id)
            records.append(dict(item))

        def _order(item: dict[str, Any]) -> tuple[int, str]:
            value = item.get("version_sequence")
            try:
                sequence = int(value)
            except (TypeError, ValueError):
                sequence = 2**31 - 1
            return sequence if sequence > 0 else 2**31 - 1, str(item.get("installed_at") or "")

        return sorted(records, key=_order)

    def get_version(self, installation_id: str) -> dict[str, Any] | None:
        """Return one retained installation record without changing activation."""

        wanted = str(installation_id or "").strip()
        if not wanted:
            return None
        for record in self.list_versions():
            if record.get("installation_id") == wanted:
                return record
        return None

    def snapshot(self) -> dict[str, Any]:
        """Return the normalized document for a later transactional restore."""

        return self._read_document()

    def restore(self, document: dict[str, Any]) -> None:
        """Restore a document captured by :meth:`snapshot` atomically."""

        if not isinstance(document, dict):
            raise ValueError("RSI Harness activation snapshot 必须是 mapping")
        active = document.get("active")
        if active is not None:
            if not isinstance(active, dict):
                raise ValueError("RSI Harness active snapshot 必须是 mapping 或 null")
            runtime = self._validate_runtime_path(active.get("runtime_path"), require_exists=True)
            if runtime is None:
                raise ValueError("RSI Harness active snapshot runtime_path 不存在")
            active = dict(active)
            active["runtime_path"] = str(runtime)
        history = document.get("history", [])
        if not isinstance(history, list) or any(not isinstance(item, dict) for item in history):
            raise ValueError("RSI Harness activation history snapshot 无效")
        self._write_document(
            {
                "schema_version": _ACTIVATION_SCHEMA_VERSION,
                "active": active,
                "history": [dict(item) for item in history],
            }
        )

    def commit(self, record: dict[str, Any]) -> None:
        if not isinstance(record, dict):
            raise ValueError("RSI Harness activation record 必须是 mapping")
        installation_id = str(record.get("installation_id") or "").strip()
        if not installation_id or Path(installation_id).name != installation_id:
            raise ValueError("RSI Harness installation_id 非法")
        runtime_path = self._validate_runtime_path(record.get("runtime_path"), require_exists=True)
        if runtime_path is None:
            raise ValueError("RSI Harness runtime_path 不存在或不是目录")
        sha256 = str(record.get("sha256") or "").strip()
        if len(sha256) != 64 or any(char not in "0123456789abcdefABCDEF" for char in sha256):
            raise ValueError("RSI Harness sha256 非法")

        document = self._read_document()
        previous = document.get("active")
        history: list[dict[str, Any]] = []
        for item in document.get("history", []):
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("installation_id") or "").strip()
            if not item_id or item_id == installation_id:
                continue
            history.append(dict(item))
        if isinstance(previous, dict) and previous.get("installation_id") != installation_id:
            previous_id = str(previous.get("installation_id") or "").strip()
            history = [item for item in history if item.get("installation_id") != previous_id]
            history.append(dict(previous))
        normalized = dict(record)
        normalized["runtime_path"] = str(runtime_path)
        try:
            sequence = int(normalized.get("version_sequence"))
        except (TypeError, ValueError):
            sequence = 0
        if sequence < 1:
            sequences = []
            for item in [*history, previous] if isinstance(previous, dict) else history:
                try:
                    value = int(item.get("version_sequence"))
                except (AttributeError, TypeError, ValueError):
                    continue
                if value > 0:
                    sequences.append(value)
            normalized["version_sequence"] = max(sequences, default=0) + 1
        document = {
            "schema_version": _ACTIVATION_SCHEMA_VERSION,
            "active": normalized,
            "history": history,
        }
        self._write_document(document)

    def _write_document(self, document: dict[str, Any]) -> None:
        """Atomically persist an already validated activation document."""

        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.root / f".activation.{uuid.uuid4().hex}.tmp"
        try:
            temporary.write_text(
                json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.activation_path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def resolve_active_runtime_path(self) -> str | None:
        active = self.get_active()
        return str(active["runtime_path"]) if active else None


def _read_manifest_extension_name(package: Path) -> tuple[str, Path]:
    manifest = _find_manifest(package)
    try:
        if manifest.name == "manifest.json":
            payload = json.loads(manifest.read_text(encoding="utf-8")) or {}
        else:
            payload = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise RsiHarnessInvalid(f"Harness manifest 不可读: {manifest}") from exc
    except json.JSONDecodeError as exc:
        raise RsiHarnessInvalid(f"Harness manifest 不可读: {manifest}") from exc
    if not isinstance(payload, dict):
        raise RsiHarnessInvalid(f"Harness manifest 必须是 mapping: {manifest}")
    extension_name = str(
        payload.get("extension_name") or payload.get("id") or package.name
    ).strip()
    if (
        not extension_name
        or extension_name in {".", ".."}
        or Path(extension_name).name != extension_name
        or any(char in extension_name for char in ("/", "\\", ":"))
    ):
        raise RsiHarnessInvalid(f"Harness extension_name 非法: {extension_name!r}")
    return extension_name, manifest


def _write_version_refs(
    refs_path: Path,
    *,
    parsed: PublishedHarnessRef,
    package_name: str,
) -> None:
    """Preserve engine metadata while making the installed ref self-contained."""

    payload = dict(parsed.refs_payload)
    payload["harness_refs"] = {parsed.role: package_name}
    roles = payload.get("roles")
    if isinstance(roles, list):
        rewritten_roles: list[Any] = []
        for raw_role in roles:
            if not isinstance(raw_role, dict):
                continue
            role_entry = dict(raw_role)
            role_name = str(role_entry.get("role") or "").strip()
            member_name = str(role_entry.get("member_name") or "").strip()
            if role_name == parsed.role or member_name == parsed.role:
                role_entry["harness_ref_path"] = package_name
            rewritten_roles.append(role_entry)
        payload["roles"] = rewritten_roles
    refs_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = refs_path.with_name(f".{refs_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        os.replace(temporary, refs_path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


class RsiHarnessInstaller:
    """Install one engine-published Harness version and hot-load it safely."""

    def __init__(
        self,
        store: Any,
        adapter_resolver: Any = None,
        agent_manager: Any = None,
        *,
        activation_root: Path | str | None = None,
        activation_store: RsiHarnessActivationStore | None = None,
    ) -> None:
        self.store = store
        self.adapter_resolver = adapter_resolver
        self.agent_manager = agent_manager
        if activation_store is not None:
            self.activation_store = activation_store
        else:
            root = activation_root
            if root is None:
                # Keep only the activation index at the workspace root.  The
                # immutable installed copy is task-owned and is written below
                # ``<tasks_root>/<task_id>/harness/versions``.
                root = Path(store.tasks_root).expanduser().resolve()
            self.activation_store = RsiHarnessActivationStore(root)
        # The WebSocket handler can receive two clicks for the same task at
        # once.  Serialize copy/load/pointer transitions in this process so
        # both requests cannot race on the same content-addressed version.
        self._install_lock = asyncio.Lock()

    async def install(self, task_id: str) -> dict[str, Any]:
        async with self._install_lock:
            return await self._install_unlocked(task_id)

    def list_versions(self) -> dict[str, Any]:
        """List retained RSI Harness versions without exposing local paths."""

        active = self.activation_store.get_active()
        active_id = str((active or {}).get("installation_id") or "").strip() or None
        records = self.activation_store.list_versions()
        initial_id = (
            str(records[0].get("installation_id") or "").strip() if records else None
        )
        versions = []
        for record in records:
            runtime = self.activation_store._validate_runtime_path(  # noqa: SLF001 - store boundary validator
                record.get("runtime_path"), require_exists=False
            )
            installation_id = str(record.get("installation_id") or "").strip()
            versions.append(
                {
                    "installation_id": installation_id,
                    "task_id": record.get("task_id"),
                    "node_id": record.get("node_id"),
                    "harness_name": record.get("harness_name") or record.get("extension_name"),
                    "sha256": record.get("sha256"),
                    "installed_at": record.get("installed_at"),
                    "is_active": installation_id == active_id,
                    "is_initial": installation_id == initial_id,
                    "available": bool(runtime and runtime.is_dir()),
                }
            )
        return {"active_installation_id": active_id, "versions": versions}

    async def rollback(self, installation_id: str) -> dict[str, Any]:
        """Activate any retained version after checking all RSI tasks are idle."""

        async with self._install_lock:
            return await self._rollback_unlocked(installation_id)

    async def _rollback_unlocked(self, installation_id: str) -> dict[str, Any]:
        wanted = str(installation_id or "").strip()
        if not wanted:
            raise RsiBadRequest("installation_id 必填")
        target = self.activation_store.get_version(wanted)
        if target is None:
            raise RsiBadRequest(f"未找到已安装的 RSI Harness 版本: {wanted}")
        old = self.activation_store.get_active()
        if old and old.get("installation_id") == wanted:
            response = self._response(
                old,
                task_id=str(old.get("task_id") or ""),
                node_id=str(old.get("node_id") or "") or None,
                already_active=True,
                hot_load=old.get("hot_load") or {"attempted": 0, "succeeded": 0, "failed": []},
            )
            response.update({"from_installation_id": wanted, "rolled_back": False})
            return response

        self._assert_rollback_allowed()
        self._validate_rollback_target(target)
        target = dict(target)
        target["status"] = "ACTIVE"
        target["activated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        target["activation_reason"] = "rollback"
        try:
            hot_load = await self._broadcast(old, target)
        except RsiHarnessInstallConflict:
            raise
        except Exception as exc:  # noqa: BLE001 - manager guarantees local compensation
            raise RsiHarnessInstallFailed("RSI Harness 回退热加载失败，旧版本保持激活") from exc
        target["hot_load"] = hot_load
        try:
            self.activation_store.commit(target)
        except Exception as exc:
            live_restored = await self._restore_live(target, old)
            if not live_restored:
                raise RsiHarnessInstallConflict(
                    "RSI Harness 回退指针写入失败且 live Agent 恢复失败"
                ) from exc
            raise RsiHarnessInstallFailed(
                "RSI Harness 回退指针写入失败，已恢复旧版本"
            ) from exc

        response = self._response(
            target,
            task_id=str(target.get("task_id") or ""),
            node_id=str(target.get("node_id") or "") or None,
            already_active=False,
            hot_load=hot_load,
        )
        response.update(
            {
                "from_installation_id": old.get("installation_id") if old else None,
                "rolled_back": True,
            }
        )
        return response

    def _assert_rollback_allowed(self) -> None:
        blocking = [
            task.task_id
            for task in self.store.list()
            if str(task.status or "").upper() in {"QUEUED", "RUNNING", "PAUSED"}
        ]
        if blocking:
            raise RsiTaskStateConflict(
                "存在排队、运行或暂停的 RSI 任务，不能回退 Harness: "
                + ", ".join(sorted(blocking))
            )

    def _validate_rollback_target(self, record: dict[str, Any]) -> None:
        try:
            runtime = self.activation_store._validate_runtime_path(  # noqa: SLF001 - store boundary validator
                record.get("runtime_path"), require_exists=True
            )
        except ValueError as exc:
            raise RsiHarnessInvalid("回退目标的 runtime_path 非法") from exc
        if runtime is None:
            raise RsiHarnessNotReady("回退目标的本地 Harness 版本不存在")
        expected_hash = str(record.get("sha256") or "").strip().lower()
        if hash_harness_package(runtime).lower() != expected_hash:
            raise RsiHarnessInvalid("回退目标 Harness 内容已变化")
        _validate_engine_manifest(runtime)

    async def _install_unlocked(self, task_id: str) -> dict[str, Any]:
        normalized_task_id = str(task_id or "").strip()
        if (
            not normalized_task_id
            or Path(normalized_task_id).name != normalized_task_id
            or normalized_task_id in {".", ".."}
        ):
            raise RsiHarnessInvalid(f"任务 task_id 非法: {task_id}")
        task_id = normalized_task_id
        task = self.store.get(task_id)
        if str(task.scenario or "").upper() != "HARNESS":
            raise RsiHarnessNotReady("只有 HARNESS 任务可以安装优化 Harness")
        if str(task.status or "").upper() != "COMPLETED":
            raise RsiHarnessNotReady(f"任务 {task_id} 尚未完成")

        run_root = self._task_run_root(task)
        state = self._read_publication_state(task_id, task, run_root)
        publication_status = str(state.get("publication_status") or "").strip().lower()
        if publication_status != "published":
            if publication_status in {"not_published", "not_published_no_improvement", "no_improvement"}:
                raise RsiHarnessNotPublished(f"任务 {task_id} 没有可安装的最终 Harness")
            raise RsiHarnessNotReady(f"任务 {task_id} 的 Harness 尚未发布")
        raw_refs_path = str(state.get("published_harness_refs_path") or "").strip()
        if not raw_refs_path:
            raise RsiHarnessNotPublished(f"任务 {task_id} 缺少 published_harness_refs_path")
        refs_path = Path(raw_refs_path).expanduser()
        if not refs_path.is_absolute():
            refs_path = run_root / refs_path
        refs_path = refs_path.resolve(strict=False)
        _ensure_inside(refs_path, run_root, label="published refs")
        if not refs_path.is_file():
            raise RsiHarnessNotPublished(f"published harness refs 不存在: {refs_path}")
        try:
            parsed = parse_published_harness_refs(refs_path, task_run_root=run_root)
            extension_name, _ = _read_manifest_extension_name(parsed.package_path)
            package_sha256 = hash_harness_package(parsed.package_path)
        except RsiHarnessInvalid:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize package failures
            raise RsiHarnessInvalid(f"Harness 发布物校验失败: {exc}") from exc

        try:
            old = self.activation_store.get_active()
            snapshot = self.activation_store.snapshot()
        except Exception as exc:  # noqa: BLE001 - normalize corrupt pointer state
            raise RsiHarnessInstallFailed("RSI Harness active 指针不可读") from exc
        if (
            old
            and old.get("task_id") == task_id
            and str(old.get("sha256") or "").lower() == package_sha256.lower()
        ):
            # Also repair installations made before catalog registration existed.
            try:
                register_harness_plugin(Path(old["runtime_path"]), old["installation_id"])
            except Exception as exc:
                raise RsiHarnessInstallFailed("RSI Harness 扩展登记失败，原激活版本未改变") from exc
            return self._response(
                old,
                task_id=task_id,
                node_id=str(state.get("final_node_id") or state.get("best_node_id") or "") or None,
                already_active=True,
                hot_load=old.get("hot_load") or {"attempted": 0, "succeeded": 0, "failed": []},
            )

        installation_id = f"rsi-harness-{package_sha256[:16]}"
        # A published Harness is an output of this task.  Keep its immutable
        # version and rewritten refs beside the task's other materials so a
        # task can be inspected or removed as one unit.
        task_root = run_root.parent
        version_root = task_root / "harness" / "versions" / installation_id
        # Keep the temporary path short: Windows still applies the legacy
        # 248-character directory limit on installations without long-path
        # support, while the task id and content-addressed version are fixed.
        staging_root = task_root / f".rsi-harness-{uuid.uuid4().hex[:8]}"
        runtime_path = version_root / extension_name
        created_copy = False
        pointer_committed = False
        undo_catalog = None
        try:
            if runtime_path.exists():
                if not runtime_path.is_dir() or hash_harness_package(runtime_path) != package_sha256:
                    raise RsiHarnessInstallConflict(
                        f"安装版本 {installation_id} 已存在但内容 hash 不一致"
                    )
            else:
                version_root.mkdir(parents=True, exist_ok=True)
                staging_root.mkdir(parents=True, exist_ok=True)
                staging = staging_root / f".{extension_name}.tmp"
                shutil.copytree(parsed.package_path, staging, symlinks=False)
                staging.replace(runtime_path)
                shutil.rmtree(staging_root, ignore_errors=True)
                created_copy = True
            installed_refs_path = version_root / "harness_refs.yaml"
            if not installed_refs_path.is_file():
                _write_version_refs(installed_refs_path, parsed=parsed, package_name=extension_name)

            record = {
                "installation_id": installation_id,
                "task_id": task_id,
                "node_id": str(state.get("final_node_id") or state.get("best_node_id") or "") or None,
                "role": parsed.role,
                "extension_name": extension_name,
                "harness_name": extension_name,
                "runtime_path": str(runtime_path.resolve()),
                "config_path": str((runtime_path / _read_manifest_extension_name(runtime_path)[1].name).resolve()),
                "refs_path": str(installed_refs_path.resolve()),
                "sha256": package_sha256,
                "status": "ACTIVE",
                "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            undo_catalog = register_harness_plugin(runtime_path, installation_id)
            hot_load = await self._broadcast(old, record)
            record["hot_load"] = hot_load
            try:
                self.activation_store.commit(record)
                pointer_committed = True
            except Exception as exc:
                live_restored = await self._restore_live(record, old)
                if not live_restored:
                    raise RsiHarnessInstallConflict(
                        "RSI Harness active 指针写入失败且 live Agent 恢复失败"
                    ) from exc
                raise RsiHarnessInstallFailed("RSI Harness active 指针写入失败，已恢复旧版本") from exc
            provenance = {
                key: record.get(key)
                for key in (
                    "installation_id",
                    "task_id",
                    "node_id",
                    "role",
                    "extension_name",
                    "sha256",
                    "installed_at",
                )
            }
            try:
                self.store.merge_config(task_id, {"rsi_installation": provenance})
            except Exception as exc:  # noqa: BLE001 - pointer remains authoritative
                live_restored = await self._restore_live(record, old)
                try:
                    self.activation_store.restore(snapshot)
                except Exception as restore_exc:  # noqa: BLE001 - report split-brain explicitly
                    raise RsiHarnessInstallConflict(
                        "RSI Harness provenance 写入失败且 active 指针恢复失败"
                    ) from restore_exc
                if not live_restored:
                    raise RsiHarnessInstallConflict(
                        "RSI Harness provenance 写入失败且 live Agent 恢复失败"
                    ) from exc
                pointer_committed = False
                raise RsiHarnessInstallFailed("RSI Harness 任务 provenance 写入失败，已恢复旧版本") from exc
            return self._response(
                record,
                task_id=task_id,
                node_id=record.get("node_id"),
                already_active=False,
                hot_load=hot_load,
            )
        except RsiHarnessInstallFailed:
            shutil.rmtree(staging_root, ignore_errors=True)
            if created_copy and not pointer_committed:
                shutil.rmtree(version_root, ignore_errors=True)
            raise
        except RsiHarnessInstallConflict:
            shutil.rmtree(staging_root, ignore_errors=True)
            raise
        except Exception as exc:  # noqa: BLE001 - normalize DeepAgent failures
            shutil.rmtree(staging_root, ignore_errors=True)
            if created_copy:
                shutil.rmtree(version_root, ignore_errors=True)
            raise RsiHarnessInstallFailed(f"RSI Harness 热加载失败: {exc}") from exc
        finally:
            if undo_catalog is not None and not pointer_committed:
                try:
                    undo_catalog()
                except Exception as exc:
                    raise RsiHarnessInstallConflict("RSI Harness 安装失败且扩展登记回滚失败") from exc

    def _task_run_root(self, task: Any) -> Path:
        tasks_root = Path(self.store.tasks_root).expanduser().resolve(strict=False)
        task_root = (tasks_root / str(task.task_id)).resolve(strict=False)
        try:
            task_root.relative_to(tasks_root)
        except ValueError as exc:
            raise RsiHarnessInvalid(f"任务目录超出 RSI 根目录: {task_root}") from exc
        run_root = Path(str(task.run_dir or task_root / "run")).expanduser().resolve(strict=False)
        try:
            run_root.relative_to(task_root)
        except ValueError as exc:
            raise RsiHarnessInvalid(f"任务 run 目录非法: {run_root}") from exc
        if not run_root.is_dir():
            raise RsiHarnessNotPublished(f"任务 run 目录不存在: {run_root}")
        return run_root

    def _read_publication_state(self, task_id: str, task: Any, run_root: Path) -> dict[str, Any]:
        adapter = None
        if callable(self.adapter_resolver):
            try:
                adapter = self.adapter_resolver(task_id)
            except Exception:  # noqa: BLE001 - state-file fallback remains authoritative
                adapter = None
        reader = getattr(adapter, "read_publication_state", None)
        if callable(reader):
            try:
                state = reader(task_id)
            except Exception:  # noqa: BLE001 - state-file fallback
                state = None
            # HarnessEngineAdapter keeps the boundary stable for older
            # providers by returning ``{}`` when raw publication state is not
            # available.  Treat that sentinel as “no reader” and continue to
            # the task run's durable state file instead of masking it.
            if isinstance(state, dict) and any(
                key in state
                for key in (
                    "publication_status",
                    "published_harness_refs_path",
                    "current_harness_refs_path",
                    "best_harness_refs_path",
                )
            ):
                return state
        path = run_root / _STATE_FILE_NAME
        if not path.is_file():
            raise RsiHarnessNotPublished(f"任务 {task_id} 缺少 single_harness_state.yaml")
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise RsiHarnessNotPublished(f"任务 {task_id} 的引擎状态不可读") from exc
        if not isinstance(payload, dict):
            raise RsiHarnessNotPublished(f"任务 {task_id} 的引擎状态无效")
        return payload

    async def _broadcast(self, old: dict[str, Any] | None, new: dict[str, Any]) -> dict[str, Any]:
        if self.agent_manager is None:
            return {"attempted": 0, "succeeded": 0, "failed": []}
        callback = getattr(self.agent_manager, "broadcast_rsi_harness_change", None)
        if not callable(callback):
            return {"attempted": 0, "succeeded": 0, "failed": []}
        result = callback(old_installation=old, new_installation=new)
        if inspect.isawaitable(result):
            result = await result
        return result if isinstance(result, dict) else {"attempted": 0, "succeeded": 0, "failed": []}

    async def _restore_live(self, new: dict[str, Any], old: dict[str, Any] | None) -> bool:
        if self.agent_manager is None:
            return True
        callback = getattr(self.agent_manager, "broadcast_rsi_harness_change", None)
        if not callable(callback):
            return True
        try:
            result = callback(old_installation=new, new_installation=old)
            if inspect.isawaitable(result):
                await result
            return True
        except Exception:  # noqa: BLE001 - original pointer-write error is primary
            logger.exception("[RSI] live Harness restore failed")
            return False

    @staticmethod
    def _response(
        record: dict[str, Any],
        *,
        task_id: str,
        node_id: str | None,
        already_active: bool,
        hot_load: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "installation_id": record.get("installation_id"),
            "task_id": task_id,
            "node_id": node_id,
            "harness_name": record.get("harness_name") or record.get("extension_name"),
            "sha256": record.get("sha256"),
            "status": "ACTIVE",
            "already_active": already_active,
            "hot_load": hot_load,
        }


_STATE_FILE_NAME = "single_harness_state.yaml"


__all__ = [
    "PublishedHarnessRef",
    "RsiHarnessActivationStore",
    "RsiHarnessInstaller",
    "hash_harness_package",
    "parse_published_harness_refs",
    "resolve_native_harness_baseline",
]
