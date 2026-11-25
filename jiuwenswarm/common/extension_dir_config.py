# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Startup migration that makes bundled extensions discoverable.

``extensions.extension_dirs`` defaults to the relative path
``jiuwenswarm/extensions`` (``resources/config.yaml``), which the Gateway
resolves against its own working directory.  ``jiuwenswarm-start`` and the
desktop launcher both start the Gateway with ``cwd=<data root>``, so the
default resolves to ``<data root>/jiuwenswarm/extensions`` — a directory that
never exists — and the bundled extensions are silently skipped.  Container
images happen to work because their ``WORKDIR`` is the repository root.

The Gateway owns that resolution logic and is an external dependency, so the
only fix available here is to hand it an absolute path: persist
``EXTENSION_DIRS`` into the deployed ``config/.env`` before the children are
spawned.  ``agent_gateway.app_gateway`` reloads that file with
``override=True``, so a shell export alone would be overwritten by the blank
template line; the value has to live in the file.
"""

from __future__ import annotations

import logging
import os
import re
import stat
from collections.abc import MutableMapping
from pathlib import Path

import portalocker
from dotenv import dotenv_values

from jiuwenswarm.common.agentos_runtime import is_agentos_runtime

logger = logging.getLogger(__name__)

EXTENSION_DIRS_ENV = "EXTENSION_DIRS"
# Marks a value this module derived rather than one the operator configured.
# Without it, a later start cannot tell "the install moved" from "the operator
# chose this directory", and would either pin a stale checkout forever or
# silently overwrite an explicit choice.
MANAGED_MARKER_ENV = "JIUWENSWARM_EXTENSION_DIRS_MANAGED"
_LOCK_SUFFIX = "extension-dirs.lock"
_STAGING_SUFFIX = "extension-dirs.pending"


def packaged_extensions_dir() -> Path:
    """Directory holding the extensions shipped with this installation.

    Works for editable/source checkouts (``<repo>/jiuwenswarm/extensions``) and
    for wheels and frozen bundles, where ``__file__`` already sits inside the
    installed ``jiuwenswarm`` package.
    """
    return Path(__file__).resolve().parent.parent / "extensions"


def _render_assignment(key: str, value: str) -> str:
    # Single quotes keep Windows paths intact: python-dotenv interprets
    # backslash escapes inside double quotes, so ``"C:\\tools\\ext"`` would
    # come back as ``C:\tools\ext`` with the backslashes eaten.  Only the quote
    # character itself needs escaping.
    return f"{key}={_quote_single(value)}"


def _quote_single(value: str) -> str:
    return "'" + value.replace("'", "\\'") + "'"


def _assign_assignment(content: str, key: str, line: str) -> str:
    """Give ``key`` exactly one assignment: rewrite the first line, drop the rest.

    The whole line is replaced rather than only the value, so a populated
    assignment cannot leave its old value behind as a stray line.  Later
    duplicates are dropped because ``dotenv_values`` resolves a repeated key to
    the last occurrence, which would make the value written here lose to a
    stale copy further down.
    """
    assignment = re.compile(rf"^[ \t]*{re.escape(key)}[ \t]*=")
    lines = content.splitlines(keepends=True)
    kept: list[str] = []
    written = False
    for current in lines:
        if assignment.match(current):
            if written:
                continue
            kept.append(line + ("\r\n" if current.endswith("\r\n") else "\n"))
            written = True
            continue
        kept.append(current)

    if written:
        return "".join(kept)
    if kept and not kept[-1].endswith(("\n", "\r")):
        kept[-1] += "\n"
    return "".join(kept) + line + "\n"


def _write_env_atomic(env_path: Path, content: str) -> None:
    env_path.parent.mkdir(parents=True, exist_ok=True)
    original_mode = stat.S_IMODE(env_path.stat().st_mode) if env_path.exists() else None
    staged_path = env_path.with_name(f".{env_path.name}.{_STAGING_SUFFIX}")
    try:
        staged_path.write_text(content, encoding="utf-8")
        if original_mode is not None:
            staged_path.chmod(original_mode)
        staged_path.replace(env_path)
    finally:
        staged_path.unlink(missing_ok=True)


def _resolve_value(
    *,
    recorded: str,
    persisted_value: str,
    environment_value: str,
    packaged_value: str,
) -> str:
    """Decide which extension directory this start should use.

    ``recorded`` is the value this module wrote last time.  The marker records
    the exact value, so "the install moved" and "the operator edited the value"
    stay distinguishable: a bare boolean cannot tell them apart, and guessing
    either way loads the wrong install's extensions forever or silently undoes
    an explicit choice.

    An environment value that merely echoes ``recorded`` is inherited, not an
    operator override: the supervisor copies the whole environment to children,
    so treating it as an override would pin a moved install to its old path and
    drop the marker, leaving no later start able to correct it.

    Returns an empty string when nothing is configured.
    """
    still_ours = bool(recorded) and recorded == persisted_value
    if still_ours:
        override = "" if environment_value == recorded else environment_value
        return override or packaged_value
    return persisted_value or environment_value or packaged_value


def _plan_updates(
    value: str,
    *,
    recorded: str,
    persisted_value: str,
    packaged_value: str,
) -> dict[str, str]:
    """Which ``.env`` lines to rewrite for ``value``.

    Only a value this module derived carries the marker.  When the operator has
    taken the setting over, the marker is dropped so the value is never
    recomputed again.
    """
    updates: dict[str, str] = {}
    if persisted_value != value:
        updates[EXTENSION_DIRS_ENV] = value
    derived = value == packaged_value
    if derived and recorded != value:
        updates[MANAGED_MARKER_ENV] = value
    elif not derived and recorded:
        updates[MANAGED_MARKER_ENV] = ""
    return updates


def _apply_updates(target: Path, updates: dict[str, str]) -> None:
    """Rewrite the planned assignments in one atomic pass."""
    content = target.read_text(encoding="utf-8") if target.is_file() else ""
    for key, update in updates.items():
        content = _assign_assignment(content, key, _render_assignment(key, update))
    _write_env_atomic(target, content)
    logger.info(
        "[extension-dirs] persisted %s in %s",
        ", ".join(f"{key}={update}" for key, update in updates.items()),
        target,
    )


def _migrate_locked(
    target: Path, runtime_env: MutableMapping[str, str], packaged: Path
) -> str:
    """Decide and persist the value while the lock is held; ``""`` means unset."""
    persisted = dict(dotenv_values(target)) if target.is_file() else {}
    recorded = str(persisted.get(MANAGED_MARKER_ENV) or "").strip()
    persisted_value = str(persisted.get(EXTENSION_DIRS_ENV) or "").strip()
    environment_value = str(runtime_env.get(EXTENSION_DIRS_ENV) or "").strip()
    packaged_value = str(packaged) if packaged.is_dir() else ""

    value = _resolve_value(
        recorded=recorded,
        persisted_value=persisted_value,
        environment_value=environment_value,
        packaged_value=packaged_value,
    )
    if not value:
        return ""
    updates = _plan_updates(
        value,
        recorded=recorded,
        persisted_value=persisted_value,
        packaged_value=packaged_value,
    )
    if updates:
        _apply_updates(target, updates)
    return value


def migrate_extension_dirs(
    env_path: str | Path,
    *,
    environ: MutableMapping[str, str] | None = None,
    lock_timeout: float = 10.0,
) -> str | None:
    """Persist an absolute extension search directory before the Gateway starts.

    Precedence: a value the operator configured in ``.env``, then the value in
    the process environment, then the directory shipped with this
    installation.  An operator value always wins because the Gateway reloads
    that file with ``override=True`` — anything else would be a value it never
    reads.  Blank values count as unset, matching ``${VAR:-default}`` in
    ``config.yaml``.

    The effective value is written into ``.env`` in every case: a shell export
    alone does not reach the Gateway, because ``override=True`` replaces it with
    the blank template line.  Writing it makes both the documented
    ``EXTENSION_DIRS`` export and the default packaged directory take effect.

    Only a value this function derived carries ``JIUWENSWARM_EXTENSION_DIRS_MANAGED``,
    recording the exact value written, and only while that record still matches
    the persisted value is it recomputed on the next start — so switching between
    a source checkout and a packaged install re-points the search path instead of
    loading the other install's extensions, while a value the operator typed or
    edited is never rewritten and its marker is dropped.

    Returns the effective value, or ``None`` when the deployment owns the
    configuration (AgentOS) or the packaged directory is absent.
    """
    if is_agentos_runtime():
        return None

    target = Path(env_path)
    runtime_env = os.environ if environ is None else environ
    packaged = packaged_extensions_dir()

    try:
        # The lock file lives beside the target, so the directory has to exist
        # before the lock is taken. A fresh deployment has no ``config/`` yet and
        # creating it only inside ``_write_env_atomic`` is too late: the lock
        # fails first and the migration is silently skipped.
        target.parent.mkdir(parents=True, exist_ok=True)
        with portalocker.Lock(
            str(target.with_name(f"{target.name}.{_LOCK_SUFFIX}")),
            timeout=lock_timeout,
        ):
            value = _migrate_locked(target, runtime_env, packaged)
            if not value:
                return None
    # ``TimeoutError`` is an ``OSError`` subclass and is already covered; naming
    # it as well would be a duplicate catch of the same exception class.
    except (OSError, portalocker.exceptions.LockException) as exc:
        # A read-only or contended deployment config dir must not block startup;
        # the Gateway then falls back to its relative default as before.
        logger.warning(
            "[extension-dirs] could not persist %s in %s: %s",
            EXTENSION_DIRS_ENV,
            target,
            exc,
        )
        return None

    runtime_env[EXTENSION_DIRS_ENV] = value
    return value
