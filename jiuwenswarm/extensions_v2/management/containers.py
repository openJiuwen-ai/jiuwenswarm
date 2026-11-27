# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Materialize system plugin containers and load this host's members.

The AgentServer owns installation for both domains: it copies only the bundle
root into the shared install store (members stay inside it) and writes records.
Each host then loads its own domain independently. A container request that the
Gateway forwards asks this host to wait for Gateway member results, so the
container succeeds only when every member either loaded or is an intentional
standby for a same-version instance; package lifecycle events stay inside each
process.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.events import Event
from openjiuwen.core.extension import ActivationReport, ExtensionFailure
from openjiuwen.core.extension.bundle import (
    BundleMember,
    BundleSpec,
    bundle_version_dir,
    dependency_levels,
    is_bundle_document,
    read_bundle_json,
    read_bundle_spec,
)
from openjiuwen.core.extension.errors import operation_error
from openjiuwen.core.extension.manifest import parse_manifest_mapping
from openjiuwen.core.extension.package_manager import PackageManager
from openjiuwen.core.extension.versions import specifier_matches

from jiuwenswarm.extensions_v2.manifest_io import DOMAIN_AGENT_SERVER, DOMAIN_GATEWAY
from jiuwenswarm.server.runtime.marketplace.extension_install_state import (
    ActiveInstance,
    BundleMemberRecord,
    ExtensionBundleMember,
    ExtensionBundleRecord,
    ExtensionInstallRecord,
    ExtensionInstallState,
)

logger = logging.getLogger(__name__)

_KNOWN_DOMAINS = frozenset({DOMAIN_GATEWAY, DOMAIN_AGENT_SERVER})
_BUNDLE_ACTIVE_STATES = {"active", "partial", "failed", "disabled"}
#: How long one install waits for the Gateway to report a member result. The
#: Gateway reloads on its 0.5 s sync tick; a broken host must fail the install
#: instead of blocking the request for the full dependency timeout.
_GATEWAY_MEMBER_TIMEOUT = 30.0
_GATEWAY_MEMBER_POLL = 0.1


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class ContainerOutcome:
    """Reports and the derived container state for one install request."""

    container_id: str
    version: str
    kind: str
    reports: list[Any]
    members: list[dict[str, Any]]
    overall_status: str
    error: str = ""

    def projection(self) -> dict[str, Any]:
        return {
            "bundleId": self.container_id,
            "version": self.version,
            "kind": self.kind,
            "membersCount": len(self.members),
            "overallStatus": self.overall_status,
            "members": self.members,
        }


async def install_sources(
    manager: Any,
    store: ExtensionInstallState,
    sources: Sequence[Path],
    *,
    origin: str,
    asset_id: str = "",
    version: str = "",
    timeout: float = 300.0,
    preempt: bool = False,
    require_all_members: bool = False,
    discard_on_failure: bool = True,
    reject_installed: bool = False,
) -> list[ContainerOutcome]:
    """Materialize every source, then load this host's members.

    ``reject_installed`` refuses a container whose id and version are already
    installed; uploaded archives set it so the same package is not accepted twice.
    """

    await asyncio.to_thread(reject_batch_cycles, Path(manager.install_dir), store, sources)
    outcomes: list[ContainerOutcome] = []
    for source in sources:
        outcomes.append(
            await _install_one(
                manager, store, Path(source), origin=origin, asset_id=asset_id,
                version=version, timeout=timeout, preempt=preempt,
                require_all_members=require_all_members,
                discard_on_failure=discard_on_failure,
                reject_installed=reject_installed,
            )
        )
    return outcomes


async def _install_one(
    manager: Any,
    store: ExtensionInstallState,
    source: Path,
    *,
    origin: str,
    asset_id: str,
    version: str = "",
    timeout: float = 300.0,
    preempt: bool = False,
    require_all_members: bool = False,
    discard_on_failure: bool = True,
    reject_installed: bool = False,
) -> ContainerOutcome:
    opened = await asyncio.to_thread(_open_source, source)
    try:
        root = opened.root
        document = await asyncio.to_thread(read_bundle_json, root)
        if document is None:
            raise ExtensionFailure(
                StatusCode.EXTENSION_MANIFEST_INVALID,
                reason=f"manifest.json is missing under {root}",
            )
        package = document.get("package")
        source_version = package.get("version") if isinstance(package, Mapping) else None
        if version and str(source_version or "").strip() != version:
            raise ExtensionFailure(
                StatusCode.EXTENSION_MANIFEST_INVALID,
                reason=f"install source is not version {version}",
            )
        if is_bundle_document(document):
            return await _install_bundle(
                manager, store, root, origin=origin, asset_id=asset_id, timeout=timeout,
                preempt=preempt, require_all_members=require_all_members,
                discard_on_failure=discard_on_failure,
                reject_installed=reject_installed,
            )
        return await _install_single(
            manager, store, root, document, origin=origin, asset_id=asset_id, timeout=timeout,
            require_all_members=require_all_members,
        )
    finally:
        await asyncio.to_thread(opened.close)


class _Opened:
    """Hold a package source open while its files are copied."""

    def __init__(self, opened: Any, root: Path) -> None:
        self._opened = opened
        self.root = root

    def close(self) -> None:
        if self._opened is not None:
            self._opened.close()


def _open_source(source: Path) -> _Opened:
    path = Path(source).expanduser().resolve()
    if path.is_dir():
        return _Opened(None, path)
    manager = PackageManager(path.parent)
    try:
        opened = manager.open_source(path)
    except ExtensionFailure as exc:
        raise ExtensionFailure(
            StatusCode.EXTENSION_SOURCE_INVALID,
            reason=f"cannot open install source: {exc.message}",
        ) from exc
    return _Opened(opened, opened.root)


async def _install_bundle(
    manager: Any,
    store: ExtensionInstallState,
    root: Path,
    *,
    origin: str,
    asset_id: str,
    timeout: float,
    preempt: bool = False,
    require_all_members: bool = False,
    discard_on_failure: bool = False,
    reject_installed: bool = False,
) -> ContainerOutcome:
    spec = await asyncio.to_thread(read_bundle_spec, root)
    unknown = sorted({member.domain for member in spec.members} - _KNOWN_DOMAINS)
    if unknown:
        raise ExtensionFailure(
            StatusCode.EXTENSION_DOMAIN_MISMATCH,
            reason=f"bundle member domain is not supported: {', '.join(unknown)}",
        )
    if reject_installed:
        existing = store.get_bundle(spec.bundle_id, spec.version)
        if existing is not None and existing.status not in {"failed", "uninstalling"}:
            error = operation_error(
                StatusCode.EXTENSION_PACKAGE_EXISTS,
                msg=f"system plugin bundle {spec.bundle_id} {spec.version} is already installed",
                package_id=spec.bundle_id,
                display_name=spec.bundle_id,
            )
            return ContainerOutcome(
                container_id=spec.bundle_id,
                version=spec.version,
                kind="bundle",
                reports=[
                    ActivationReport("", spec.bundle_id, "install", "FAILED", None, None, error)
                ],
                members=[_member_state_from(record) for record in existing.member_records],
                overall_status="failed",
                error=error.message,
            )
    _cancel_retry(f"{spec.bundle_id}@{spec.version}")
    levels = dependency_levels(spec)
    install_dir = Path(manager.install_dir)
    digests = {
        member.package_id: await asyncio.to_thread(_package_digest, member.root)
        for member in spec.members
    }
    previous_bundle = store.active_bundle(spec.bundle_id)
    await asyncio.to_thread(_reject_bundle_identity_conflicts, store, spec)

    bundle_root = bundle_version_dir(install_dir, spec.bundle_id, spec.version)
    await asyncio.to_thread(_copy_tree, root, bundle_root)
    member_roots = {
        member.package_id: bundle_root / member.path for member in spec.members
    }
    previous_claims = {
        member.package_id: store.active_instance(member.package_id) for member in spec.members
    }
    await asyncio.to_thread(
        _upsert_container, store, spec, origin=origin, asset_id=asset_id, status="pending",
    )
    await asyncio.to_thread(
        store.upsert_bundle_members,
        spec.bundle_id,
        spec.version,
        [
            BundleMemberRecord(
                package_id=member.package_id,
                version=member.version,
                domain=member.domain,
                root=str(member_roots[member.package_id]),
                enabled=False,
                active=False,
                status="disabled",
                digest=digests[member.package_id],
                updated_at=_timestamp(),
            )
            for member in spec.members
        ],
    )
    await _remove_stale_members(manager, store, spec)

    reports: list[Any] = []
    error = ""
    failed = False
    pending = False
    for level in levels:
        for member in level:
            outcome = await _activate_bundle_member(
                manager,
                store,
                spec,
                member,
                member_roots[member.package_id],
                timeout=timeout,
                preempt=preempt,
                require_all_members=require_all_members,
            )
            if outcome is None:
                pending = True
                continue
            report, ok = outcome
            if report is not None:
                reports.append(report)
            if not ok:
                failed = True
                error = _report_error(report) if report is not None else "member load failed"
                break
        if failed:
            break

    status = _bundle_status_from_records(store, spec.bundle_id, spec.version)
    await asyncio.to_thread(
        _upsert_container, store, spec, origin=origin, asset_id=asset_id, status=status, error=error,
    )
    if failed:
        await _disable_target_member_instances(manager, store, spec)
        await _restore_previous_claims(manager, store, spec, previous_claims)
        member_states = [
            _member_state_from(record)
            for record in store.bundle_members(spec.bundle_id, spec.version)
        ]
        # A failed new version must not stay in the workspace as a card that
        # cannot work. Reinstalling a healthy active version keeps its copy:
        # that attempt is rolled back instead of discarded.
        same_active_version = previous_bundle is not None and previous_bundle.version == spec.version
        previous_healthy = (
            same_active_version and bundle_overall_status(previous_bundle, store) == "enabled"
        )
        discard_version = discard_on_failure and not previous_healthy
        if discard_version:
            if any(member.domain == DOMAIN_GATEWAY for member in spec.members):
                # The Gateway must release its members before the copy is
                # deleted; leave a tombstone for its post-response sync.
                await _tombstone_bundle_version(
                    store, spec.bundle_id, spec.version, error,
                )
            else:
                await _discard_bundle_version(manager, store, spec)
            status = "failed"
        else:
            status = _bundle_status_from_records(store, spec.bundle_id, spec.version)
            await asyncio.to_thread(
                _upsert_container, store, spec, origin=origin, asset_id=asset_id,
                status=status, error=error,
            )
        await asyncio.to_thread(_refresh_bundle_statuses, store, spec.bundle_id)
    else:
        await asyncio.to_thread(store.activate_bundle, spec.bundle_id, spec.version)
        if previous_bundle is not None and previous_bundle.version != spec.version:
            await _deactivate_old_bundle_members(manager, store, previous_bundle, spec)
        await asyncio.to_thread(_refresh_bundle_statuses, store, spec.bundle_id)
        if pending or any(
            member.status == "pending"
            for member in store.bundle_members(spec.bundle_id, spec.version)
        ):
            _schedule_bundle_retry(manager, spec, timeout)
        member_states = [
            _member_state_from(record)
            for record in store.bundle_members(spec.bundle_id, spec.version)
        ]
    return ContainerOutcome(
        container_id=spec.bundle_id,
        version=spec.version,
        kind="bundle",
        reports=reports,
        members=member_states,
        overall_status="failed" if failed else status,
        error=error,
    )


async def _discard_bundle_version(
    manager: Any,
    store: ExtensionInstallState,
    spec: BundleSpec,
) -> None:
    """Remove one failed bundle version and its copy without touching other versions."""

    _cancel_retry(f"{spec.bundle_id}@{spec.version}")
    await asyncio.to_thread(store.remove_bundle_members, spec.bundle_id, spec.version)
    await asyncio.to_thread(store.remove_bundle, spec.bundle_id, spec.version)
    await asyncio.to_thread(
        _remove_directory, bundle_version_dir(manager.install_dir, spec.bundle_id, spec.version),
    )
    bundle_dir = Path(manager.install_dir) / spec.bundle_id
    if bundle_dir.is_dir() and not any(bundle_dir.iterdir()):
        await asyncio.to_thread(_remove_directory, bundle_dir)


async def _remove_stale_members(
    manager: Any,
    store: ExtensionInstallState,
    spec: BundleSpec,
) -> None:
    """Drop member instances that the reinstalled version no longer declares."""

    current = store.get_bundle(spec.bundle_id, spec.version)
    if current is None:
        return
    keep = {member.package_id for member in spec.members}
    for member in current.member_records:
        if member.package_id in keep:
            continue
        if member.active and member.domain == DOMAIN_AGENT_SERVER:
            try:
                await manager.disable_version(member.package_id, member.version)
            except Exception:  # noqa: BLE001 - removal must continue
                logger.warning(
                    "[ExtensionsV2] stale member %s could not be disabled",
                    member.package_id,
                    exc_info=True,
                )
        unregister = getattr(manager, "unregister_managed_root", None)
        if unregister is not None and member.root:
            await asyncio.to_thread(
                unregister, member.package_id, member.version, root=Path(member.root),
            )
        await asyncio.to_thread(
            store.remove_bundle_member, spec.bundle_id, spec.version, member.package_id,
        )


async def _activate_bundle_member(
    manager: Any,
    store: ExtensionInstallState,
    spec: BundleSpec,
    member: BundleMember,
    member_root: Path,
    *,
    timeout: float,
    preempt: bool,
    require_all_members: bool = False,
) -> tuple[Any, bool] | None:
    """Activate, stand by or defer one member according to the package-id claim."""

    package_id = member.package_id
    claim = store.active_instance(package_id)
    exact_active = (
        claim is not None
        and claim.kind == "bundle"
        and claim.bundle_id == spec.bundle_id
        and claim.bundle_version == spec.version
        and claim.version == member.version
    )
    if exact_active:
        await asyncio.to_thread(
            store.mark_bundle_member, spec.bundle_id, spec.version, package_id,
            enabled=True, active=True, status="active", root=str(member_root),
        )
        return _success_report(package_id), True
    same_owner = claim is not None and claim.kind == "bundle" and claim.bundle_id == spec.bundle_id
    same_identity = claim is not None and claim.version == member.version
    other_owner_active = claim is not None and not same_owner
    if other_owner_active and same_identity and not preempt:
        await asyncio.to_thread(
            store.mark_bundle_member, spec.bundle_id, spec.version, package_id,
            enabled=True, active=False, status="standby", root=str(member_root),
        )
        return _success_report(package_id), True

    dependency_state = _dependency_state(member.requires, store)
    if dependency_state == "waiting":
        await asyncio.to_thread(
            store.mark_bundle_member, spec.bundle_id, spec.version, package_id,
            enabled=True, active=False, status="pending", root=str(member_root),
        )
        return None
    if dependency_state == "failed":
        await asyncio.to_thread(
            store.mark_bundle_member, spec.bundle_id, spec.version, package_id,
            enabled=False, active=False, status="failed", root=str(member_root),
            error="dependency not met",
        )
        await _emit_outcome(spec, member, False, "dependency not met")
        return _failure_report(package_id, "dependency not met"), False

    if member.domain == DOMAIN_AGENT_SERVER:
        if claim is not None and claim.version == member.version and not exact_active:
            # Same id and version but a different root (another bundle version or
            # standalone copy): Core would short-circuit on the version alone.
            try:
                await manager.disable_version(package_id, member.version)
            except Exception:  # noqa: BLE001 - the target load decides the result
                logger.warning(
                    "[ExtensionsV2] old instance %s@%s could not be released",
                    package_id,
                    member.version,
                    exc_info=True,
                )
        register = getattr(manager, "register_managed_root", None)
        try:
            if register is not None:
                await asyncio.to_thread(register, package_id, member.version, member_root)
            report = await manager.enable_version(
                package_id, member.version, skip_dependencies=True,
            )
        except Exception as exc:  # noqa: BLE001 - report one member, keep the manager alive
            logger.warning("[ExtensionsV2] bundle member %s failed to load", package_id, exc_info=True)
            report = _failure_report(package_id, f"load failed: {exc}")
        ok = getattr(report, "status", "") == "SUCCESS"
    else:
        report = _success_report(package_id)
        ok = True

    if not ok:
        error = _report_error(report)
        await asyncio.to_thread(
            store.mark_bundle_member, spec.bundle_id, spec.version, package_id,
            enabled=True, active=False, status="failed", root=str(member_root), error=error,
        )
        await _emit_outcome(spec, member, False, error)
        return report, False

    await asyncio.to_thread(
        store.switch_active_instance,
        ActiveInstance(
            package_id=package_id,
            version=member.version,
            kind="bundle",
            bundle_id=spec.bundle_id,
            bundle_version=spec.version,
            root=str(member_root),
        ),
    )
    await asyncio.to_thread(
        store.mark_bundle_member, spec.bundle_id, spec.version, package_id,
        enabled=True, active=True,
        status="active" if member.domain == DOMAIN_AGENT_SERVER else "pending",
        root=str(member_root),
    )
    if member.domain == DOMAIN_AGENT_SERVER:
        await _emit_outcome(spec, member, True, "")
        return report, True
    if require_all_members:
        return await _wait_gateway_member(store, spec, member, timeout)
    return report, True


async def _wait_gateway_member(
    store: ExtensionInstallState,
    spec: BundleSpec,
    member: BundleMember,
    timeout: float,
) -> tuple[Any, bool]:
    """Wait for the Gateway host to report one member's load result."""

    package_id = member.package_id
    deadline = time.monotonic() + min(max(timeout, 0.1), _GATEWAY_MEMBER_TIMEOUT)
    while time.monotonic() < deadline:
        record = store.get_bundle_member(spec.bundle_id, spec.version, package_id)
        if record is None:
            break
        if record.active and record.status == "active":
            return _success_report(package_id), True
        if record.status == "failed":
            return _failure_report(package_id, record.error or "member load failed"), False
        if record.status == "standby":
            # Another instance took the claim for the same id while waiting;
            # the soft standby record keeps this member loadable later.
            return _success_report(package_id), True
        await asyncio.sleep(_GATEWAY_MEMBER_POLL)
    error = "Gateway did not load the member in time"
    await asyncio.to_thread(
        store.disable_instance,
        package_id,
        kind="bundle",
        bundle_id=spec.bundle_id,
        bundle_version=spec.version,
        version=member.version,
    )
    await asyncio.to_thread(
        store.mark_bundle_member, spec.bundle_id, spec.version, package_id,
        enabled=False, active=False, status="failed", error=error,
    )
    await _emit_outcome(spec, member, False, error)
    return _failure_report(package_id, error), False


def _load_failure_message(error: str) -> str:
    """Unwrap the Core status template so the reason reads as one sentence."""

    prefix = "extension load failed, reason="
    if not error.startswith(prefix):
        return error
    reason = error[len(prefix):].strip()
    if len(reason) >= 2 and reason[0] == reason[-1] and reason[0] in {"'", '"'}:
        reason = reason[1:-1]
    return reason or error


async def _wait_gateway_package(
    store: ExtensionInstallState,
    package_id: str,
    version: str,
    timeout: float,
) -> str:
    """Wait for the Gateway host to load one standalone copy.

    Returns the failure message, or ``""`` when the copy loaded or the wait
    timed out; a slow Gateway keeps the pending result instead of a failure.
    """

    deadline = time.monotonic() + min(max(timeout, 0.1), _GATEWAY_MEMBER_TIMEOUT)
    while time.monotonic() < deadline:
        record = store.get(package_id, version)
        if record is not None:
            if record.active and record.status == "active":
                return ""
            if record.status in {"failed", "disabled", "uninstalling"}:
                error = record.error or f"{package_id} {version} did not load"
                return _load_failure_message(error)
        await asyncio.sleep(_GATEWAY_MEMBER_POLL)
    return ""


async def _discard_failed_standalone(
    manager: Any,
    store: ExtensionInstallState,
    package_id: str,
    version: str,
) -> None:
    """Remove a first install that could not load instead of leaving it installed."""

    target = Path(manager.install_dir) / package_id / version
    await asyncio.to_thread(_remove_tree, target)
    if target.exists():
        logger.warning(
            "[ExtensionsV2] failed copy %s@%s could not be removed", package_id, version,
        )
        return
    await asyncio.to_thread(store.remove, package_id, version)
    package_dir = target.parent
    if package_dir.is_dir() and not any(package_dir.iterdir()):
        await asyncio.to_thread(_remove_directory, package_dir)


async def _install_single(
    manager: Any,
    store: ExtensionInstallState,
    root: Path,
    document: Mapping[str, Any],
    *,
    origin: str,
    asset_id: str,
    timeout: float,
    require_all_members: bool = False,
) -> ContainerOutcome:
    manifest = parse_manifest_mapping(document, root, require_domain=True)
    package_id = manifest.package_id
    version = manifest.version
    domain = (manifest.domain or "").strip().lower()
    if domain not in _KNOWN_DOMAINS:
        raise ExtensionFailure(
            StatusCode.EXTENSION_DOMAIN_MISMATCH,
            reason=f"unsupported package domain: {domain}",
        )
    requires = tuple((item.package_id, item.version) for item in manifest.requires_packages)
    _reject_single_cycle(package_id, requires, store, Path(manager.install_dir))
    if store.get_bundle(package_id) is not None:
        raise ExtensionFailure(
            StatusCode.EXTENSION_PACKAGE_EXISTS,
            package_id=package_id,
            display_name=package_id,
        )
    _reject_domain_conflict(store, package_id, domain)
    digest = await asyncio.to_thread(_package_digest, root)
    await asyncio.to_thread(
        _reject_digest_conflict, store, package_id, version, digest,
    )

    target = Path(manager.install_dir) / package_id / version
    claim = store.active_instance(package_id)
    same_owner = claim is not None and claim.kind == "standalone"
    same_version_standalone = (
        claim is not None
        and claim.kind == "standalone"
        and claim.version == version
    )
    if same_version_standalone and target.is_dir():
        reports = [
            ActivationReport(
                "",
                package_id,
                "install",
                "FAILED",
                None,
                None,
                operation_error(
                    StatusCode.EXTENSION_PACKAGE_EXISTS,
                    package_id=package_id,
                    display_name=manifest.display_name or package_id,
                ),
            )
        ]
        return ContainerOutcome(
            container_id=package_id,
            version=version,
            kind="package",
            reports=reports,
            members=[_standalone_member_state(store, package_id, version, domain)],
            overall_status="enabled",
        )
    same_version = claim is not None and claim.version == version
    if not same_owner and same_version:
        # An identical version owned by another instance stays a soft standby
        # record: the copy is installed and enabled but never loaded twice.
        await _store_standby_single(
            manager, store, root, package_id, version,
            domain=domain, origin=origin, asset_id=asset_id, digest=digest,
        )
        return ContainerOutcome(
            container_id=package_id,
            version=version,
            kind="package",
            reports=[_success_report(package_id)],
            members=[_standalone_member_state(store, package_id, version, domain)],
            overall_status="partial",
        )

    state = _dependency_state(requires, store)
    if state == "failed":
        raise ExtensionFailure(
            StatusCode.EXTENSION_DEPENDENCY_NOT_MET,
            package_id=package_id,
            reason="dependency not met",
        )
    if state == "waiting":
        await _store_standby_single(
            manager, store, root, package_id, version,
            domain=domain, origin=origin, asset_id=asset_id, status="pending", digest=digest,
        )
        if domain == DOMAIN_AGENT_SERVER:
            _schedule_single_retry(manager, package_id, version, requires, timeout)
        else:
            # The Gateway owns this runtime; publish the claim and let its own
            # retry load the package once the dependency becomes active.
            await asyncio.to_thread(
                store.switch_active_instance,
                ActiveInstance(package_id=package_id, version=version, kind="standalone"),
            )
            await asyncio.to_thread(
                store.mark, package_id, version=version, status="pending",
            )
        return ContainerOutcome(
            container_id=package_id,
            version=version,
            kind="package",
            reports=[],
            members=[_standalone_member_state(store, package_id, version, domain)],
            overall_status="pending",
        )

    if domain == DOMAIN_AGENT_SERVER:
        if claim is not None and claim.kind != "standalone" and claim.version == version:
            try:
                await manager.disable_version(package_id, version)
            except Exception:  # noqa: BLE001 - the target load decides the result
                logger.warning(
                    "[ExtensionsV2] old instance %s@%s could not be released",
                    package_id,
                    version,
                    exc_info=True,
                )
        unregister = getattr(manager, "unregister_managed_root", None)
        if unregister is not None:
            await asyncio.to_thread(unregister, package_id, version)
        reports = []
        if target.is_dir():
            accept = getattr(manager, "accept_version", None)
            if accept is not None:
                await asyncio.to_thread(accept, package_id, version)
            reports = [await manager.enable_version(package_id, version)]
        else:
            reports = list(await manager.install([root]))
        ok = bool(reports) and getattr(reports[0], "status", "") == "SUCCESS"
        if not ok:
            await asyncio.to_thread(
                _mark_failed, store, package_id, version,
                _report_error(reports[0]) if reports else "no report",
            )
            return ContainerOutcome(
                container_id=package_id,
                version=version,
                kind="package",
                reports=reports,
                members=[_standalone_member_state(store, package_id, version, domain)],
                overall_status="failed",
                error="member load failed",
            )
    else:
        previous_claim = claim
        await _store_standby_single(
            manager, store, root, package_id, version,
            domain=domain, origin=origin, asset_id=asset_id, status="pending", digest=digest,
        )
        reports = [_success_report(package_id)]
        await asyncio.to_thread(
            store.switch_active_instance,
            ActiveInstance(package_id=package_id, version=version, kind="standalone"),
        )
        await asyncio.to_thread(
            store.mark, package_id, version=version, status="pending",
        )
        overall = "pending"
        if require_all_members:
            # The Gateway loads this copy in its own process; wait for the
            # result so a version that cannot load is not reported as enabled.
            error = await _wait_gateway_package(store, package_id, version, timeout)
            if error:
                member_state = _standalone_member_state(store, package_id, version, domain)
                if previous_claim is not None:
                    await asyncio.to_thread(store.switch_active_instance, previous_claim)
                elif not any(
                    record.package_id == package_id and record.version != version
                    for record in store.packages()
                ):
                    # A failing first install must not stay behind as an
                    # installed plugin the detail page cannot reload.
                    await _discard_failed_standalone(manager, store, package_id, version)
                return ContainerOutcome(
                    container_id=package_id,
                    version=version,
                    kind="package",
                    reports=[_failure_report(package_id, error)],
                    members=[member_state],
                    overall_status="failed",
                    error=error,
                )
            record = store.get(package_id, version)
            if record is not None and record.status == "active":
                overall = "enabled"
        return ContainerOutcome(
            container_id=package_id,
            version=version,
            kind="package",
            reports=reports,
            members=[_standalone_member_state(store, package_id, version, domain)],
            overall_status=overall,
        )

    await asyncio.to_thread(
        _activate_single, store, package_id, version,
        domain=domain, origin=origin, asset_id=asset_id,
        install_path=str(target), digest=digest,
    )
    return ContainerOutcome(
        container_id=package_id,
        version=version,
        kind="package",
        reports=reports,
        members=[_standalone_member_state(store, package_id, version, domain)],
        overall_status="enabled" if domain == DOMAIN_AGENT_SERVER else "pending",
    )


async def _store_standby_single(
    manager: Any,
    store: ExtensionInstallState,
    root: Path,
    package_id: str,
    version: str,
    *,
    domain: str,
    origin: str,
    asset_id: str,
    status: str = "standby",
    digest: str = "",
) -> None:
    """Keep an installed standalone copy without making it the active runtime."""

    target = Path(manager.install_dir) / package_id / version
    if not target.is_dir():
        opened = await asyncio.to_thread(_open_source, root)
        try:
            await asyncio.to_thread(_copy_tree, opened.root, target)
        finally:
            await asyncio.to_thread(opened.close)
    await asyncio.to_thread(_write_enable_flag, target, False)
    await asyncio.to_thread(
        _seed_single, store, package_id, version,
        domain=domain, origin=origin, asset_id=asset_id,
        enabled=True, active=False, status=status,
        install_path=str(target), digest=digest,
    )
    accept = getattr(manager, "accept_version", None)
    if accept is not None:
        await asyncio.to_thread(accept, package_id, version)


def set_manifest_enable(root: Path, enabled: bool) -> None:
    """Persist the loading flag on one standalone copy."""
    _write_enable_flag(root, enabled)


def _write_enable_flag(root: Path, enabled: bool) -> None:
    manifest = Path(root) / "manifest.json"
    try:
        document = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(document, dict):
        return
    document["enable"] = enabled
    manifest.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")


# ── startup recovery ──────────────────────────────────────────────────────


async def recover_containers(manager: Any) -> None:
    """Reconcile container member instances with what this process actually loaded."""

    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    install_dir = Path(manager.install_dir)
    active_pairs: set[tuple[str, str]] = set()
    for record in store.bundles():
        for member in record.member_records:
            if member.active:
                active_pairs.add((member.package_id, member.version))
    for record in store.bundles():
        if not record.active:
            for member in record.member_records:
                if member.active and (member.package_id, member.version) not in active_pairs:
                    if member.domain == DOMAIN_AGENT_SERVER:
                        try:
                            await manager.disable_version(member.package_id, member.version)
                        except Exception:  # noqa: BLE001 - startup must not fail on one record
                            logger.warning(
                                "[ExtensionsV2] inactive member %s@%s could not be disabled",
                                member.package_id,
                                member.version,
                                exc_info=True,
                            )
                    await asyncio.to_thread(
                        store.disable_instance,
                        member.package_id,
                        kind="bundle",
                        bundle_id=record.bundle_id,
                        bundle_version=record.version,
                        version=member.version,
                    )
                    await asyncio.to_thread(
                        store.mark_bundle_member, record.bundle_id, record.version,
                        member.package_id, enabled=False, active=False, status="disabled",
                    )
                elif member.status != "disabled":
                    await asyncio.to_thread(
                        store.mark_bundle_member, record.bundle_id, record.version,
                        member.package_id, active=False, status="disabled",
                    )
            if record.status == "uninstalling":
                # Keep the tombstone; the Gateway releases its members and
                # deletes the copy through its own sync.
                continue
            if record.status != "disabled":
                await asyncio.to_thread(
                    store.upsert_bundle, replace(record, status="disabled", active=False),
                )
            continue
        for member in record.member_records:
            if member.domain != DOMAIN_AGENT_SERVER:
                continue
            claim = store.active_instance(member.package_id)
            # No claim at all means nothing else owns this id; the runtime copy
            # found below is this member, so it may be promoted back to active.
            owns = _claim_owns_member(claim, record, member) or claim is None
            if owns and _member_is_loaded(manager, member, record, install_dir):
                await asyncio.to_thread(
                    store.mark_bundle_member, record.bundle_id, record.version,
                    member.package_id, active=True, enabled=True, status="active", error="",
                )
            elif member.active:
                if owns:
                    await asyncio.to_thread(
                        store.mark_bundle_member, record.bundle_id, record.version,
                        member.package_id, active=False,
                        status="failed" if member.enabled else "disabled",
                        error="not loaded at startup" if member.enabled else "",
                    )
                else:
                    await asyncio.to_thread(
                        store.mark_bundle_member, record.bundle_id, record.version,
                        member.package_id, active=False,
                        status="standby" if member.enabled else "disabled",
                        error="",
                    )
        status = bundle_overall_status(record, store)
        mapped = "active" if status == "enabled" else status
        if mapped != record.status and status in _BUNDLE_ACTIVE_STATES:
            await asyncio.to_thread(
                store.upsert_bundle, replace(record, status=mapped),
            )
        members = _pending_members(store, record, install_dir)
        if members:
            _schedule_bundle_retry(
                manager,
                BundleSpec(record.bundle_id, record.version, tuple(members)),
                _retry_timeout(),
            )
    for package in store.packages():
        if (
            package.status == "failed"
            and package.enabled
            and _active_identical_member(store, package.package_id, package.version) is not None
        ):
            # The standalone copy failed only because the identical bundle
            # member owns the id; keep it as a soft standby instead.
            await asyncio.to_thread(
                store.mark, package.package_id, version=package.version,
                active=False, enabled=True, status="standby", error="",
            )
            continue
        if (
            package.domain == DOMAIN_AGENT_SERVER
            and package.enabled
            and package.status == "pending"
        ):
            root = install_dir / package.package_id / package.version
            _schedule_single_retry(
                manager,
                package.package_id,
                package.version,
                _requirements(root),
                _retry_timeout(),
            )


def register_active_managed_roots(manager: Any) -> None:
    """Register the bundle member roots that are allowed to load at startup."""

    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    register = getattr(manager, "register_managed_root", None)
    if register is None:
        return
    store.reconcile_claims()
    for record in store.bundles():
        if not record.active:
            continue
        for member in record.member_records:
            if member.domain != DOMAIN_AGENT_SERVER or not member.active:
                continue
            claim = store.active_instance(member.package_id)
            if not _claim_owns_member(claim, record, member):
                continue
            root = Path(member.root) if member.root else (
                bundle_version_dir(manager.install_dir, record.bundle_id, record.version)
                / "plugins" / member.package_id
            )
            if not root.is_dir():
                continue
            try:
                register(member.package_id, member.version, root)
            except Exception:  # noqa: BLE001 - one bad member must not block startup
                logger.warning(
                    "[ExtensionsV2] managed root %s@%s could not be registered",
                    member.package_id,
                    member.version,
                    exc_info=True,
                )


# ── container lifecycle ───────────────────────────────────────────────────


async def uninstall_container(
    manager: Any,
    store: ExtensionInstallState,
    record: ExtensionBundleRecord,
) -> dict[str, Any]:
    """Remove one bundle version, its member instances and its root copy."""

    _cancel_retry(f"{record.bundle_id}@{record.version}")
    if record.active or any(member.active for member in record.member_records):
        await _deactivate_bundle_members(manager, store, record)
    for member in record.member_records:
        await promote_same_version_standby(
            manager, store, member.package_id, member.version,
        )
    if any(member.domain == DOMAIN_GATEWAY for member in record.members):
        # The Gateway must release its members before the copy can be deleted.
        # Keep a tombstone; the Gateway removes the directory and records in its
        # post-response sync (or at startup) after the runtime is idle.
        await _tombstone_bundle_version(store, record.bundle_id, record.version)
        return {"reports": [_success_report(record.bundle_id)], "bundle": None}
    await asyncio.to_thread(store.remove_bundle_members, record.bundle_id, record.version)
    await asyncio.to_thread(store.remove_bundle, record.bundle_id, record.version)
    await asyncio.to_thread(
        _remove_directory, bundle_version_dir(manager.install_dir, record.bundle_id, record.version),
    )
    bundle_dir = Path(manager.install_dir) / record.bundle_id
    if bundle_dir.is_dir() and not any(bundle_dir.iterdir()):
        await asyncio.to_thread(_remove_directory, bundle_dir)
    await asyncio.to_thread(_refresh_bundle_statuses, store, record.bundle_id)
    return {"reports": [_success_report(record.bundle_id)], "bundle": None}


async def _tombstone_bundle_version(
    store: ExtensionInstallState,
    bundle_id: str,
    version: str,
    error: str = "",
) -> None:
    """Mark one bundle version for the Gateway to release and delete."""

    current = store.get_bundle(bundle_id, version)
    if current is None:
        return
    # upsert_bundle preserves a previously active flag; clear it first so the
    # Gateway neither reloads this version nor treats it as an active switch.
    await asyncio.to_thread(store.deactivate_bundle, bundle_id, version)
    current = store.get_bundle(bundle_id, version) or current
    await asyncio.to_thread(
        store.upsert_bundle,
        replace(current, status="uninstalling", active=False, error=error),
    )


async def apply_container_action(
    manager: Any,
    store: ExtensionInstallState,
    record: ExtensionBundleRecord,
    action: str,
    enable: bool | None = None,
    *,
    require_all_members: bool = False,
) -> dict[str, Any]:
    """Apply enable/reload to the selected bundle version and persist both domains."""

    install_dir = Path(manager.install_dir)
    if action == "set_enabled" and enable is True:
        current = store.get_bundle(record.bundle_id, record.version) or record
        # Enabling a bundle means every member is active; a partial or inactive
        # version goes through the same atomic activation as a fresh install.
        if not record.active or bundle_overall_status(current, store) != "enabled":
            root = bundle_version_dir(install_dir, record.bundle_id, record.version)
            spec = await asyncio.to_thread(read_bundle_spec, root)
            outcome = await _install_bundle(
                manager, store, root, origin=record.origin, asset_id=record.asset_id,
                timeout=_retry_timeout(), preempt=True,
                require_all_members=require_all_members,
            )
            return {"reports": list(outcome.reports), "bundle": outcome.projection()}
        return {"reports": [_success_report(record.bundle_id)], "bundle": _container_projection(current, store)}
    if action == "set_enabled" and enable is False:
        if record.active or any(member.active for member in record.member_records):
            await _deactivate_bundle_members(manager, store, record)
        await asyncio.to_thread(_refresh_bundle_statuses, store, record.bundle_id)
        for member in record.member_records:
            await promote_same_version_standby(
                manager, store, member.package_id, member.version,
            )
        current = store.get_bundle(record.bundle_id, record.version) or record
        return {"reports": [_success_report(record.bundle_id)], "bundle": _container_projection(current, store)}
    reports: list[Any] = []
    if not record.active:
        return {"reports": reports, "bundle": _container_projection(record, store)}
    for member in record.member_records:
        claim = store.active_instance(member.package_id)
        if not _claim_owns_member(claim, record, member):
            # Another instance owns this package id; leave its record untouched.
            continue
        if member.domain == DOMAIN_AGENT_SERVER:
            if not _member_is_loaded(manager, member, record, install_dir):
                await asyncio.to_thread(
                    store.mark_bundle_member, record.bundle_id, record.version,
                    member.package_id, active=False, status="pending", error="",
                )
                continue
            report = await manager.reload(member.package_id)
            if getattr(report, "status", "") == "SUCCESS":
                await asyncio.to_thread(
                    store.mark_bundle_member, record.bundle_id, record.version,
                    member.package_id, active=True, enabled=True, status="active", error="",
                )
            reports.append(report)
        else:
            await asyncio.to_thread(
                store.mark_bundle_member, record.bundle_id, record.version,
                member.package_id, status="pending",
            )
    await asyncio.to_thread(_refresh_bundle_statuses, store, record.bundle_id)
    current = store.get_bundle(record.bundle_id, record.version) or record
    return {"reports": reports, "bundle": _container_projection(current, store)}


# ── record helpers ────────────────────────────────────────────────────────


def bundle_overall_status(record: ExtensionBundleRecord | None, store: ExtensionInstallState) -> str:
    """Derive the container state from its member instance records."""

    if record is None or not record.members:
        return "not-installed"
    records = {member.package_id: member for member in record.member_records}
    total = len(record.members)
    enabled = 0
    installed = 0
    pending = 0
    standby = 0
    failed = 0
    for member in record.members:
        member_record = records.get(member.package_id)
        if member_record is None:
            pending += 1
            continue
        installed += 1
        if member_record.status == "failed":
            failed += 1
        elif member_record.status == "pending":
            pending += 1
        elif member_record.status == "standby":
            standby += 1
        if member_record.active and member_record.enabled and member_record.status == "active":
            enabled += 1
    if enabled == total:
        return "enabled"
    if failed and enabled == 0:
        return "failed"
    if pending or standby or failed:
        return "partial"
    if installed == total:
        return "disabled"
    return "partial" if installed else "failed"



_RETRY_TASKS: dict[str, asyncio.Task[None]] = {}


def _dependency_state(requires: Sequence[tuple[str, str]], store: ExtensionInstallState) -> str:
    """Return ready / waiting / failed from the active instance of each package id."""

    waiting = False
    for package_id, spec in requires:
        claim = store.active_instance(package_id)
        if claim is None:
            failed = any(
                record.package_id == package_id and record.status == "failed"
                for record in store.packages()
            ) or any(
                member.package_id == package_id and member.status == "failed"
                for member in store.bundle_members()
            )
            if failed:
                return "failed"
            waiting = True
            continue
        record = (
            store.get(package_id, claim.version)
            if claim.kind == "standalone"
            else store.get_bundle_member(claim.bundle_id, claim.bundle_version, package_id)
        )
        status = record.status if record is not None else ""
        if status == "failed":
            return "failed"
        if record is None or not record.active or status != "active":
            waiting = True
            continue
        try:
            if not specifier_matches(claim.version, spec):
                return "failed"
        except ValueError:
            return "failed"
    return "waiting" if waiting else "ready"


async def _notify_ui(package_ids: Sequence[str]) -> None:
    """Refresh mounted WebUI slots after a background member load."""

    if not package_ids:
        return
    try:
        from jiuwenswarm.extensions_v2.assembly import get_webui_runtime

        runtime = get_webui_runtime()
        if runtime is not None:
            await runtime.notify_packages_changed(list(package_ids))
    except Exception:  # noqa: BLE001 - notification failure does not undo the load
        logger.warning("[ExtensionsV2] package change UI notification failed", exc_info=True)


async def _emit_outcome(spec: BundleSpec, member: BundleMember, success: bool, error: str = "") -> None:
    """Publish this member's completion on the host bus."""

    try:
        from jiuwenswarm.extensions_v2.assembly import get_extension_bus
        from openjiuwen.core.events import PackageInstallOutcome

        bus = get_extension_bus()
        if bus is None:
            return
        outcome = PackageInstallOutcome(
            event_id="",
            operation="install",
            package_id=member.package_id,
            version=member.version,
            status="success" if success else "failed",
            active=success,
            enabled=success,
            error=error,
            container_id=spec.bundle_id,
            container_version=spec.version,
        )
        await bus.trigger(
            Event("package.install_finish" if success else "package.install_failed", outcome)
        )
    except Exception:  # noqa: BLE001 - a notification failure does not undo the record
        logger.warning("[ExtensionsV2] package outcome event failed", exc_info=True)


def _cancel_retry(key: str) -> None:
    task = _RETRY_TASKS.pop(key, None)
    if task is not None and not task.done():
        task.cancel()


async def cancel_retries() -> None:
    """Stop background member retries during host shutdown."""

    tasks = [task for task in _RETRY_TASKS.values() if not task.done()]
    _RETRY_TASKS.clear()
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _schedule_bundle_retry(manager: Any, spec: BundleSpec, timeout: float) -> None:
    """Retry waiting members in the background; never block the install caller."""

    key = f"{spec.bundle_id}@{spec.version}"
    task = _RETRY_TASKS.get(key)
    if task is not None and not task.done():
        return

    async def loop() -> None:
        deadline = time.monotonic() + max(0.1, timeout)
        delay = 0.5
        try:
            while time.monotonic() < deadline:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 5.0)
                store = ExtensionInstallState.for_install_dir(manager.install_dir)
                waiting = False
                for member in spec.members:
                    if member.domain != DOMAIN_AGENT_SERVER:
                        continue
                    record = store.get_bundle_member(spec.bundle_id, spec.version, member.package_id)
                    if record is None or record.status != "pending":
                        continue
                    if _dependency_state(member.requires, store) == "waiting":
                        waiting = True
                        continue
                    root = Path(record.root) if record.root else (
                        bundle_version_dir(manager.install_dir, spec.bundle_id, spec.version)
                        / "plugins" / member.package_id
                    )
                    outcome = await _activate_bundle_member(
                        manager, store, spec, member, root, timeout=timeout, preempt=False,
                    )
                    if outcome is not None and outcome[1]:
                        await _notify_ui([member.package_id])
                await asyncio.to_thread(refresh_container_status, store, spec.bundle_id, spec.version)
                if not waiting:
                    return
            store = ExtensionInstallState.for_install_dir(manager.install_dir)
            for member in spec.members:
                if member.domain != DOMAIN_AGENT_SERVER:
                    continue
                record = store.get_bundle_member(spec.bundle_id, spec.version, member.package_id)
                if record is not None and record.status == "pending":
                    await asyncio.to_thread(
                        store.mark_bundle_member, spec.bundle_id, spec.version,
                        member.package_id, enabled=False, active=False, status="failed",
                        error="dependency timeout",
                    )
                    await _emit_outcome(spec, member, False, "dependency timeout")
            await asyncio.to_thread(refresh_container_status, store, spec.bundle_id, spec.version)
        finally:
            if _RETRY_TASKS.get(key) is asyncio.current_task():
                _RETRY_TASKS.pop(key, None)

    _RETRY_TASKS[key] = asyncio.create_task(loop())


def _schedule_single_retry(
    manager: Any,
    package_id: str,
    version: str,
    requires: Sequence[tuple[str, str]],
    timeout: float,
) -> None:
    key = f"single:{package_id}@{version}"
    task = _RETRY_TASKS.get(key)
    if task is not None and not task.done():
        return
    record = ExtensionInstallState.for_install_dir(manager.install_dir).get(package_id, version)
    if record is None or record.domain != DOMAIN_AGENT_SERVER:
        # Gateway-domain packages are loaded and retried by the Gateway runtime.
        return

    async def loop() -> None:
        deadline = time.monotonic() + max(0.1, timeout)
        store = ExtensionInstallState.for_install_dir(manager.install_dir)
        delay = 0.5
        try:
            while time.monotonic() < deadline:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 5.0)
                record = store.get(package_id, version)
                if record is None or record.domain != DOMAIN_AGENT_SERVER:
                    return
                state = _dependency_state(requires, store)
                if state == "waiting":
                    continue
                if state == "failed":
                    await asyncio.to_thread(_mark_failed, store, package_id, version, "dependency not met")
                    return
                root = Path(manager.install_dir) / package_id / version
                claim = store.active_instance(package_id)
                if (
                    claim is not None
                    and claim.kind != "standalone"
                    and claim.version == version
                ):
                    await asyncio.to_thread(
                        store.mark, package_id, version=version,
                        active=False, enabled=True, status="standby",
                    )
                    return
                try:
                    if root.is_dir():
                        report = await manager.enable_version(package_id, version)
                    else:
                        reports = list(await manager.install([root]))
                        report = reports[0] if reports else None
                except Exception as exc:  # noqa: BLE001 - report one package, keep the host alive
                    await asyncio.to_thread(
                        _mark_failed, store, package_id, version, f"load failed: {exc}",
                    )
                    return
                if report is not None and getattr(report, "status", "") == "SUCCESS":
                    await asyncio.to_thread(
                        _activate_single, store, package_id, version,
                        domain=DOMAIN_AGENT_SERVER, origin="user", asset_id="",
                        install_path=str(root),
                    )
                    await _notify_ui([package_id])
                else:
                    await asyncio.to_thread(
                        _mark_failed, store, package_id, version,
                        _report_error(report) if report is not None else "no report",
                    )
                return
            await asyncio.to_thread(
                _mark_failed, store, package_id, version, "dependency timeout",
            )
        finally:
            if _RETRY_TASKS.get(key) is asyncio.current_task():
                _RETRY_TASKS.pop(key, None)

    _RETRY_TASKS[key] = asyncio.create_task(loop())


def refresh_container_status(store: ExtensionInstallState, bundle_id: str, version: str) -> None:
    """Recompute and persist one container status from member records."""

    record = store.get_bundle(bundle_id, version)
    if record is None:
        return
    if not record.active:
        return
    status = bundle_overall_status(record, store)
    mapped = "active" if status == "enabled" else status
    if mapped != record.status:
        store.upsert_bundle(replace(record, status=mapped))


def _container_projection(record: ExtensionBundleRecord, store: ExtensionInstallState) -> dict[str, Any]:
    records = {member.package_id: member for member in record.member_records}
    results = []
    for member in record.members:
        member_record = records.get(member.package_id)
        results.append(
            _member_state_from(member_record)
            if member_record is not None
            else {
                "packageId": member.package_id,
                "version": member.version,
                "domain": member.domain,
                "status": "pending",
                "active": False,
                "enabled": False,
                "error": "",
            }
        )
    return {
        "bundleId": record.bundle_id,
        "version": record.version,
        "kind": "bundle",
        "membersCount": len(record.members),
        "overallStatus": bundle_overall_status(record, store),
        "members": results,
    }


def _upsert_container(
    store: ExtensionInstallState,
    spec: BundleSpec,
    *,
    origin: str,
    asset_id: str,
    status: str,
    error: str = "",
) -> None:
    record_status = "active" if status == "enabled" else status
    store.upsert_bundle(
        ExtensionBundleRecord(
            bundle_id=spec.bundle_id,
            version=spec.version,
            origin=origin,
            asset_id=asset_id,
            members=_container_members(spec),
            status=record_status,
            error=error,
        )
    )


def _container_members(spec: BundleSpec) -> tuple[ExtensionBundleMember, ...]:
    return tuple(
        ExtensionBundleMember(package_id=member.package_id, version=member.version, domain=member.domain)
        for member in spec.members
    )


def _seed_single(
    store: ExtensionInstallState,
    package_id: str,
    version: str,
    *,
    domain: str,
    origin: str,
    asset_id: str,
    enabled: bool = True,
    active: bool = False,
    status: str = "pending",
    install_path: str = "",
    digest: str = "",
) -> None:
    store.upsert(
        ExtensionInstallRecord(
            package_id=package_id,
            version=version,
            origin=origin,
            asset_id=asset_id,
            install_path=install_path,
            active=active,
            enabled=enabled,
            domain=domain,
            status=status,
            digest=digest,
        )
    )


def _activate_single(
    store: ExtensionInstallState,
    package_id: str,
    version: str,
    *,
    domain: str,
    origin: str,
    asset_id: str,
    install_path: str = "",
    digest: str = "",
) -> None:
    _seed_single(
        store, package_id, version,
        domain=domain, origin=origin, asset_id=asset_id,
        active=True, status="active", install_path=install_path, digest=digest,
    )
    store.switch_active_instance(
        ActiveInstance(package_id=package_id, version=version, kind="standalone"),
    )


def _mark_failed(store: ExtensionInstallState, package_id: str, version: str, error: str) -> None:
    store.mark(package_id, version=version, active=False, enabled=False, status="failed", error=error)


def _active_package_record(manager: Any, package_id: str) -> Any:
    for record in manager.active_packages():
        if record.package_id == package_id:
            return record
    return None


def _member_root(member: BundleMemberRecord, record: ExtensionBundleRecord, install_dir: Path) -> Path:
    return Path(member.root) if member.root else (
        bundle_version_dir(install_dir, record.bundle_id, record.version)
        / "plugins" / member.package_id
    )


def _member_is_loaded(
    manager: Any,
    member: BundleMemberRecord,
    record: ExtensionBundleRecord,
    install_dir: Path,
) -> bool:
    """True when this exact instance root is the one loaded by the runtime."""

    active = _active_package_record(manager, member.package_id)
    if active is None or str(active.version) != member.version:
        return False
    try:
        return Path(active.root).resolve() == _member_root(member, record, install_dir).resolve()
    except OSError:
        return False


def _claim_owns_member(
    claim: ActiveInstance | None,
    record: ExtensionBundleRecord,
    member: BundleMemberRecord,
) -> bool:
    return (
        claim is not None
        and claim.kind == "bundle"
        and claim.bundle_id == record.bundle_id
        and claim.bundle_version == record.version
        and claim.version == member.version
    )



def _package_digest(root: Path) -> str:
    """Fingerprint a package's manifest and Python sources for identity checks."""

    digest = hashlib.sha256()
    root = Path(root)
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ExtensionFailure(
                StatusCode.EXTENSION_INSTALL_COPY_FAILED,
                reason=f"symbolic links are not allowed in a package: {path}",
            )
        if not path.is_file():
            continue
        if path.name != "manifest.json" and path.suffix != ".py":
            continue
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\x00")
        digest.update(path.read_bytes())
        digest.update(b"\x00")
    return digest.hexdigest()


def _reject_digest_conflict(
    store: ExtensionInstallState, package_id: str, version: str, digest: str,
) -> None:
    """Reject replacing one standalone instance with different bytes under the same version.

    Bundle members are never checked here: a bundle install is validated by its own
    id and version, and the same member id+version may appear in several bundle
    versions with different content.
    """

    if not digest:
        return
    install_dir = store.path.resolve().parents[1]
    for record in store.packages():
        if record.package_id != package_id or record.version != version:
            continue
        existing = record.digest
        if not existing:
            root = Path(record.install_path) if record.install_path else install_dir / package_id / version
            existing = _package_digest(root) if root.is_dir() else ""
        if existing and existing != digest:
            raise ExtensionFailure(
                StatusCode.EXTENSION_PACKAGE_EXISTS,
                msg=(
                    f"standalone {package_id}@{version} is already installed with different content"
                ),
                package_id=package_id,
                display_name=package_id,
            )


def _reject_domain_conflict(
    store: ExtensionInstallState, package_id: str, domain: str,
) -> None:
    """One package id cannot mean a gateway plugin in one owner and an agentserver one in another."""

    for record in store.packages():
        if record.package_id == package_id and record.domain and record.domain != domain:
            raise ExtensionFailure(
                StatusCode.EXTENSION_DOMAIN_MISMATCH,
                package_id=package_id,
                reason=f"package id is already used by domain {record.domain}",
            )
    for member in store.bundle_members():
        if member.package_id == package_id and member.domain and member.domain != domain:
            raise ExtensionFailure(
                StatusCode.EXTENSION_DOMAIN_MISMATCH,
                package_id=package_id,
                reason=f"package id is already used by domain {member.domain}",
            )


def _reject_bundle_identity_conflicts(
    store: ExtensionInstallState, spec: BundleSpec,
) -> None:
    """Reject bundle-root namespace collisions only.

    Member conflicts are not judged here: installing or switching a bundle version
    is decided by the bundle's own id and version, and member instances stay
    independent across owners.
    """

    root_record = store.get(spec.bundle_id)
    if root_record is not None:
        raise ExtensionFailure(
            StatusCode.EXTENSION_PACKAGE_EXISTS,
            msg=f"a standalone package already uses id {spec.bundle_id}",
            package_id=spec.bundle_id,
            display_name=root_record.package_id or "a standalone install",
        )
    owner = store.bundle_of(spec.bundle_id)
    if owner is not None and owner.bundle_id != spec.bundle_id:
        raise ExtensionFailure(
            StatusCode.EXTENSION_PACKAGE_EXISTS,
            msg=f"another bundle already uses id {spec.bundle_id} as a member",
            package_id=spec.bundle_id,
            display_name=owner.bundle_id,
        )
    for member in spec.members:
        container = store.get_bundle(member.package_id)
        if container is not None and container.bundle_id != spec.bundle_id:
            raise ExtensionFailure(
                StatusCode.EXTENSION_PACKAGE_EXISTS,
                msg=f"member id {member.package_id} is already a bundle root",
                package_id=member.package_id,
                display_name=container.bundle_id,
            )
        _reject_domain_conflict(store, member.package_id, member.domain)


async def _disable_target_member_instances(
    manager: Any,
    store: ExtensionInstallState,
    spec: BundleSpec,
) -> None:
    """Disable target instances after a failed install without touching other owners."""

    for member in spec.members:
        record = store.get_bundle_member(spec.bundle_id, spec.version, member.package_id)
        if record is None:
            continue
        if record.active and member.domain == DOMAIN_AGENT_SERVER:
            try:
                await manager.disable_version(member.package_id, member.version)
            except Exception:  # noqa: BLE001 - the failure path must keep going
                logger.warning(
                    "[ExtensionsV2] target member %s could not be disabled",
                    member.package_id,
                    exc_info=True,
                )
        unregister = getattr(manager, "unregister_managed_root", None)
        if unregister is not None and record.root:
            await asyncio.to_thread(
                unregister, member.package_id, member.version, root=Path(record.root),
            )
        await asyncio.to_thread(
            store.disable_instance,
            member.package_id,
            kind="bundle",
            bundle_id=spec.bundle_id,
            bundle_version=spec.version,
            version=member.version,
        )
        failed_before = member.domain == DOMAIN_AGENT_SERVER or record.status == "failed"
        await asyncio.to_thread(
            store.mark_bundle_member, spec.bundle_id, spec.version, member.package_id,
            enabled=False, active=False,
            status="failed" if failed_before else "pending",
            error=(record.error or "install failed") if failed_before else "",
        )


async def _restore_previous_claims(
    manager: Any,
    store: ExtensionInstallState,
    spec: BundleSpec,
    claims: Mapping[str, ActiveInstance | None],
) -> None:
    """Best-effort restore the instances that owned the package ids before a failed switch."""

    for member in spec.members:
        unregister = getattr(manager, "unregister_managed_root", None)
        record = store.get_bundle_member(spec.bundle_id, spec.version, member.package_id)
        if unregister is not None and record is not None and record.root:
            await asyncio.to_thread(
                unregister, member.package_id, member.version, root=Path(record.root),
            )
    for package_id, claim in claims.items():
        if claim is None:
            continue
        if claim.kind == "bundle":
            record = store.get_bundle_member(claim.bundle_id, claim.bundle_version, package_id)
            if record is None:
                continue
            root = Path(record.root)
            if record.domain == DOMAIN_AGENT_SERVER:
                register = getattr(manager, "register_managed_root", None)
                try:
                    if register is not None and root.is_dir():
                        await asyncio.to_thread(register, package_id, claim.version, root)
                    await manager.enable_version(
                        package_id, claim.version, skip_dependencies=True,
                    )
                except Exception:  # noqa: BLE001 - keep the original install failure
                    logger.warning(
                        "[ExtensionsV2] previous member %s could not be restored",
                        package_id,
                        exc_info=True,
                    )
                    continue
            await asyncio.to_thread(store.switch_active_instance, claim)
            await asyncio.to_thread(
                store.mark_bundle_member, claim.bundle_id, claim.bundle_version, package_id,
                enabled=True, active=True,
                status="active" if record.domain == DOMAIN_AGENT_SERVER else "pending",
                root=str(root),
            )
        else:
            unregister = getattr(manager, "unregister_managed_root", None)
            if unregister is not None:
                await asyncio.to_thread(unregister, package_id, claim.version)
            try:
                await manager.enable_version(package_id, claim.version)
            except Exception:  # noqa: BLE001 - keep the original install failure
                logger.warning(
                    "[ExtensionsV2] previous standalone %s could not be restored",
                    package_id,
                    exc_info=True,
                )
                continue
            await asyncio.to_thread(store.switch_active_instance, claim)


async def _deactivate_old_bundle_members(
    manager: Any,
    store: ExtensionInstallState,
    previous: ExtensionBundleRecord,
    spec: BundleSpec,
) -> None:
    """Disable members left active by the previously active bundle version."""

    targets = {
        member.package_id: store.get_bundle_member(spec.bundle_id, spec.version, member.package_id)
        for member in spec.members
    }
    for record in previous.member_records:
        target = targets.get(record.package_id)
        if record.active and not (target is not None and target.active):
            if record.domain == DOMAIN_AGENT_SERVER:
                try:
                    await manager.disable_version(record.package_id, record.version)
                except Exception:  # noqa: BLE001 - the new version stays active
                    logger.warning(
                        "[ExtensionsV2] previous member %s could not be disabled",
                        record.package_id,
                        exc_info=True,
                    )
            unregister = getattr(manager, "unregister_managed_root", None)
            if unregister is not None and record.root:
                await asyncio.to_thread(
                    unregister, record.package_id, record.version, root=Path(record.root),
                )
            await asyncio.to_thread(
                store.disable_instance,
                record.package_id,
                kind="bundle",
                bundle_id=previous.bundle_id,
                bundle_version=previous.version,
                version=record.version,
            )
        await asyncio.to_thread(
            store.mark_bundle_member, previous.bundle_id, previous.version, record.package_id,
            enabled=False, active=False, status="disabled",
        )
    current = store.get_bundle(previous.bundle_id, previous.version)
    if current is not None:
        await asyncio.to_thread(
            store.upsert_bundle, replace(current, status="disabled", active=False),
        )


async def _deactivate_bundle_members(
    manager: Any,
    store: ExtensionInstallState,
    record: ExtensionBundleRecord,
) -> None:
    """Disable every member instance of one bundle version, keeping all roots."""

    _cancel_retry(f"{record.bundle_id}@{record.version}")
    for member in record.member_records:
        if member.active and member.domain == DOMAIN_AGENT_SERVER:
            try:
                await manager.disable_version(member.package_id, member.version)
            except Exception:  # noqa: BLE001 - one member must not block the switch
                logger.warning(
                    "[ExtensionsV2] member %s could not be disabled",
                    member.package_id,
                    exc_info=True,
                )
        unregister = getattr(manager, "unregister_managed_root", None)
        if unregister is not None and member.root:
            await asyncio.to_thread(
                unregister, member.package_id, member.version, root=Path(member.root),
            )
        await asyncio.to_thread(
            store.disable_instance,
            member.package_id,
            kind="bundle",
            bundle_id=record.bundle_id,
            bundle_version=record.version,
            version=member.version,
        )
    await asyncio.to_thread(store.deactivate_bundle, record.bundle_id, record.version)
    current = store.get_bundle(record.bundle_id, record.version)
    if current is not None:
        await asyncio.to_thread(
            store.upsert_bundle, replace(current, status="disabled", active=False),
        )


def _active_identical_member(
    store: ExtensionInstallState, package_id: str, version: str,
) -> BundleMemberRecord | None:
    for bundle in store.bundles():
        if not bundle.active:
            continue
        for member in bundle.member_records:
            if member.package_id != package_id:
                continue
            if member.version != version:
                continue
            if member.active and member.status == "active":
                return member
    return None


async def promote_same_version_standby(
    manager: Any,
    store: ExtensionInstallState,
    package_id: str,
    version: str,
) -> bool:
    """Let the single standby copy of an identical version take over an id.

    The caller has just disabled or removed the instance that owned the id.
    Ambiguous pools stay on standby so the user can choose explicitly.
    """

    if not package_id or not version or store.active_instance(package_id) is not None:
        return False
    candidates: list[tuple[str, Any, Any]] = []
    for record in store.packages():
        if record.package_id != package_id:
            continue
        if record.version != version:
            continue
        if record.status != "standby":
            continue
        if record.enabled and not record.active:
            candidates.append(("standalone", None, record))
    for bundle in store.bundles():
        if not bundle.active:
            continue
        for member in bundle.member_records:
            if member.package_id != package_id:
                continue
            if member.version != version:
                continue
            if member.status != "standby":
                continue
            if member.enabled and not member.active:
                candidates.append(("bundle", bundle, member))
    if len(candidates) != 1:
        if len(candidates) > 1:
            logger.info(
                "[ExtensionsV2] %s@%s has several standby copies; waiting for an explicit choice",
                package_id, version,
            )
        return False
    kind, bundle, record = candidates[0]
    root = _standby_root(manager, kind, bundle, record)
    if root is None:
        return False
    if kind == "bundle":
        return await _promote_bundle_member(manager, store, bundle, record, root)
    return await _promote_standalone(manager, store, record, root)


def _standby_root(manager: Any, kind: str, bundle: Any, record: Any) -> Path | None:
    if kind == "bundle":
        root = Path(record.root) if record.root else (
            bundle_version_dir(manager.install_dir, bundle.bundle_id, bundle.version)
            / "plugins" / record.package_id
        )
    else:
        root = Path(record.install_path) if record.install_path else (
            Path(manager.install_dir) / record.package_id / record.version
        )
    return root if root.is_dir() else None


async def _promote_bundle_member(
    manager: Any,
    store: ExtensionInstallState,
    bundle: ExtensionBundleRecord,
    member: BundleMemberRecord,
    root: Path,
) -> bool:
    if member.domain == DOMAIN_AGENT_SERVER:
        register = getattr(manager, "register_managed_root", None)
        try:
            if register is not None:
                await asyncio.to_thread(register, member.package_id, member.version, root)
            report = await manager.enable_version(
                member.package_id, member.version, skip_dependencies=True,
            )
        except Exception:  # noqa: BLE001 - the copy stays standby for a later retry
            logger.warning(
                "[ExtensionsV2] standby member %s could not take over", member.package_id,
                exc_info=True,
            )
            return False
        if getattr(report, "status", "") != "SUCCESS":
            return False
    await asyncio.to_thread(
        store.switch_active_instance,
        ActiveInstance(
            package_id=member.package_id,
            version=member.version,
            kind="bundle",
            bundle_id=bundle.bundle_id,
            bundle_version=bundle.version,
            root=str(root),
        ),
    )
    await asyncio.to_thread(
        store.mark_bundle_member, bundle.bundle_id, bundle.version, member.package_id,
        enabled=True, active=True,
        status="active" if member.domain == DOMAIN_AGENT_SERVER else "pending",
        root=str(root), error="",
    )
    await asyncio.to_thread(refresh_container_status, store, bundle.bundle_id, bundle.version)
    return True


async def _promote_standalone(
    manager: Any,
    store: ExtensionInstallState,
    record: ExtensionInstallRecord,
    root: Path,
) -> bool:
    if record.domain == DOMAIN_AGENT_SERVER:
        accept = getattr(manager, "accept_version", None)
        if accept is not None:
            await asyncio.to_thread(accept, record.package_id, record.version)
        try:
            report = await manager.enable_version(record.package_id, record.version)
        except Exception:  # noqa: BLE001 - the copy stays standby for a later retry
            logger.warning(
                "[ExtensionsV2] standby %s could not take over", record.package_id,
                exc_info=True,
            )
            return False
        if getattr(report, "status", "") != "SUCCESS":
            return False
    await asyncio.to_thread(
        store.switch_active_instance,
        ActiveInstance(
            package_id=record.package_id,
            version=record.version,
            kind="standalone",
            root=str(root),
        ),
    )
    await asyncio.to_thread(
        store.mark, record.package_id, version=record.version,
        active=True, enabled=True,
        status="active" if record.domain == DOMAIN_AGENT_SERVER else "pending",
        error="",
    )
    return True


def _member_state_from(record: BundleMemberRecord) -> dict[str, Any]:
    return {
        "packageId": record.package_id,
        "version": record.version,
        "domain": record.domain,
        "status": record.status,
        "active": record.active,
        "enabled": record.enabled,
        "error": record.error,
    }


def _standalone_member_state(
    store: ExtensionInstallState, package_id: str, version: str, domain: str,
) -> dict[str, Any]:
    record = store.get(package_id, version)
    return {
        "packageId": package_id,
        "version": version,
        "domain": domain,
        "status": record.status if record is not None else "pending",
        "active": bool(record.active) if record is not None else False,
        "enabled": bool(record.enabled) if record is not None else False,
        "error": record.error if record is not None else "",
    }


def _bundle_status_from_records(
    store: ExtensionInstallState, bundle_id: str, version: str,
) -> str:
    return bundle_overall_status(store.get_bundle(bundle_id, version), store)


def _refresh_bundle_statuses(store: ExtensionInstallState, bundle_id: str) -> None:
    """Recompute status for every installed version of one bundle."""

    for record in store.bundles():
        if record.bundle_id == bundle_id:
            refresh_container_status(store, bundle_id, record.version)


def _pending_members(
    store: ExtensionInstallState,
    record: ExtensionBundleRecord,
    install_dir: Path,
) -> list[BundleMember]:
    """Rebuild retry inputs for AgentServer members that are still pending."""

    members: list[BundleMember] = []
    for member_record in record.member_records:
        if member_record.domain != DOMAIN_AGENT_SERVER:
            continue
        if member_record.status != "pending":
            continue
        root = Path(member_record.root) if member_record.root else (
            bundle_version_dir(install_dir, record.bundle_id, record.version)
            / "plugins" / member_record.package_id
        )
        members.append(
            BundleMember(
                package_id=member_record.package_id,
                version=member_record.version,
                domain=member_record.domain,
                path="",
                root=root,
                requires=_requirements(root),
            )
        )
    return members


def _requirements(root: Path) -> tuple[tuple[str, str], ...]:
    """Read declared package requirements from an installed member."""

    try:
        document = read_bundle_json(root)
        if document is None:
            return ()
        manifest = parse_manifest_mapping(document, root, require_domain=False)
    except (ExtensionFailure, OSError, TypeError, ValueError):
        return ()
    return tuple(
        (dependency.package_id, dependency.version)
        for dependency in manifest.requires_packages
    )


def _reject_single_cycle(
    package_id: str,
    requires: Sequence[tuple[str, str]],
    store: ExtensionInstallState,
    install_dir: Path,
) -> None:
    """Reject a standalone dependency cycle visible in persisted records."""

    pending = [dependency for dependency, _ in requires]
    seen: set[str] = set()
    while pending:
        current = pending.pop()
        if current == package_id:
            raise ExtensionFailure(StatusCode.EXTENSION_DEPENDENCY_CYCLE, packages=package_id)
        if current in seen:
            continue
        seen.add(current)
        claim = store.active_instance(current)
        if claim is not None:
            root = Path(claim.root) if claim.root else install_dir / current / claim.version
        else:
            record = store.get(current)
            if record is None:
                continue
            root = install_dir / current / record.version
        pending.extend(
            dependency for dependency, _ in _requirements(root)
        )


def reject_batch_cycles(
    install_dir: Path,
    store: ExtensionInstallState,
    sources: Sequence[Path],
) -> None:
    """Reject a cycle formed by packages and bundle members installed in one request."""

    pending: dict[str, set[str]] = {}
    for source in sources:
        try:
            opened = _open_source(Path(source))
        except (ExtensionFailure, OSError, TypeError, ValueError):
            continue
        try:
            document = read_bundle_json(opened.root)
            if document is None:
                continue
            if is_bundle_document(document):
                spec = read_bundle_spec(opened.root)
                for member in spec.members:
                    pending.setdefault(member.package_id, set()).update(
                        dependency for dependency, _ in member.requires
                    )
                continue
            manifest = parse_manifest_mapping(document, opened.root, require_domain=True)
        except (ExtensionFailure, OSError, TypeError, ValueError):
            continue
        finally:
            opened.close()
        pending.setdefault(manifest.package_id, set()).update(
            dependency.package_id for dependency in manifest.requires_packages
        )
    for package_id in pending:
        seen: set[str] = set()
        stack = list(pending[package_id])
        while stack:
            current = stack.pop()
            if current == package_id:
                raise ExtensionFailure(StatusCode.EXTENSION_DEPENDENCY_CYCLE, packages=package_id)
            if current in seen:
                continue
            seen.add(current)
            dependencies = pending.get(current)
            if dependencies is None:
                record = store.get(current)
                dependencies = (
                    {dependency for dependency, _ in _requirements(install_dir / current / record.version)}
                    if record is not None
                    else set()
                )
            stack.extend(dependencies)


def _retry_timeout() -> float:
    from jiuwenswarm.extensions_v2.config import resolve_install_report_settings

    return float(resolve_install_report_settings().dependency_timeout)


def _success_report(package_id: str) -> ActivationReport:
    return ActivationReport("", package_id, "install", "SUCCESS", None, None)


def _failure_report(package_id: str, error: str) -> ActivationReport:
    return ActivationReport(
        "",
        package_id,
        "install",
        "FAILED",
        None,
        None,
        operation_error(StatusCode.EXTENSION_LOAD_FAILED, msg=error),
    )


def _report_error(report: Any) -> str:
    error = getattr(report, "error", None)
    message = getattr(error, "message", None)
    return str(message or getattr(report, "status", "failed"))


# ── file helpers ──────────────────────────────────────────────────────────


def _copy_tree(source: Path, target: Path) -> Path:
    """Copy a package tree into the store, rejecting symlinks and staging first."""

    raw_source = Path(source).expanduser()
    if raw_source.is_symlink():
        raise ExtensionFailure(
            StatusCode.EXTENSION_INSTALL_COPY_FAILED, reason=f"source is a symbolic link: {raw_source}",
        )
    source = raw_source.resolve()
    if not source.is_dir():
        raise ExtensionFailure(
            StatusCode.EXTENSION_INSTALL_COPY_FAILED, reason=f"source is not a directory: {source}",
        )
    _assert_no_symlinks(source)
    target = Path(target).expanduser().resolve()
    if source == target:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
    try:
        staged = staging / "root"
        shutil.copytree(source, staged, symlinks=True)
        if target.exists():
            _remove_tree(target)
        os.replace(staged, target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return target


def _assert_no_symlinks(root: Path) -> None:
    for entry in root.rglob("*"):
        if entry.is_symlink():
            raise ExtensionFailure(
                StatusCode.EXTENSION_INSTALL_COPY_FAILED,
                reason=f"symbolic links are not allowed in a package: {entry}",
            )


def _remove_tree(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
        return
    shutil.rmtree(path, ignore_errors=True)


def _remove_directory(path: Path) -> None:
    _remove_tree(path)
