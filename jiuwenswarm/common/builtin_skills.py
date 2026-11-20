"""Install-time migration for the bundled capability catalog."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
from pathlib import Path
import tempfile

logger = logging.getLogger(__name__)
BUILTIN_CATALOG_NAME = "workswarm-builtin"


def with_builtin_catalog(skills: list[str], skills_dir: Path) -> list[str]:
    """Keep explicit legacy names and add their migrated catalog entry point."""
    try:
        state = json.loads((skills_dir / "skills_state.json").read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            return skills
        migrated = set(state.get("builtin_catalog_migrated", []))
        if (skills_dir / BUILTIN_CATALOG_NAME / "SKILL.md").is_file() and any(
            name in migrated and not (skills_dir / name).exists() for name in skills
        ):
            return list(dict.fromkeys([BUILTIN_CATALOG_NAME, *skills]))
    except (OSError, ValueError, TypeError):
        pass
    return skills


def skill_tree_digest(directory: Path) -> str:
    """Hash resource paths and bytes without including runtime Python caches."""
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*"), key=lambda item: item.relative_to(directory).as_posix()):
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        if path.is_symlink():
            raise ValueError(f"Refusing to migrate a linked skill resource: {path}")
        if path.is_file():
            digest.update(path.relative_to(directory).as_posix().encode("utf-8"))
            digest.update(b"\0")
            content = path.read_bytes()
            try:
                content.decode("utf-8")
            except UnicodeDecodeError:
                pass
            else:
                content = content.replace(b"\r\n", b"\n")
            digest.update(hashlib.sha256(content).digest())
    return digest.hexdigest()


def _install_missing_catalog_capabilities(builtin_dir: Path, catalog: Path) -> None:
    """Publish new bundled capabilities without replacing any existing leaf."""
    source_root = builtin_dir / BUILTIN_CATALOG_NAME / "references" / "capabilities"
    if not source_root.is_dir():
        return
    destination_root = catalog / "references" / "capabilities"
    if destination_root.is_symlink() or not destination_root.resolve().is_relative_to(catalog.resolve()):
        raise ValueError("Catalog capability directory escapes workspace")
    destination_root.mkdir(parents=True, exist_ok=True)
    for source in sorted(source_root.iterdir()):
        if not source.is_dir() or source.is_symlink() or not (source / "SKILL.md").is_file():
            continue
        target = destination_root / source.name
        if target.exists() or target.is_symlink():
            continue
        # The caller holds the install lock. Publish the complete leaf on the
        # same filesystem so interrupted copies never become discoverable.
        with tempfile.TemporaryDirectory(prefix=".builtin-capability-", dir=destination_root) as directory:
            staged = Path(directory) / source.name
            shutil.copytree(source, staged)
            staged.rename(target)


def _refresh_catalog_navigation(builtin_dir: Path, catalog: Path, versions: dict) -> None:
    """Refresh only navigation files that exactly match a known shipped version."""
    for name in ("SKILL.md", "skill_display.md", "references/catalog.md"):
        target = catalog / name
        source = builtin_dir / BUILTIN_CATALOG_NAME / name
        if not target.resolve().is_relative_to(catalog.resolve()):
            raise ValueError("Catalog navigation escapes workspace")
        if not target.is_file() or not source.is_file() or target.is_symlink():
            continue
        previous = target.read_bytes()
        replacement = source.read_bytes()
        if previous.replace(b"\r\n", b"\n") == replacement.replace(b"\r\n", b"\n"):
            continue
        digest = hashlib.sha256(previous.replace(b"\r\n", b"\n")).hexdigest()
        trusted = versions.get("_catalog_files", {}).get(name, [])
        if not any(item.get("digest") == digest for item in trusted):
            continue
        backup = catalog / ".legacy" / "catalog-files" / f"{name}-{digest[:12]}"
        if not backup.resolve().is_relative_to(catalog.resolve()):
            raise ValueError("Catalog navigation archive escapes workspace")
        backup.parent.mkdir(parents=True, exist_ok=True)
        if backup.exists() and backup.read_bytes() != previous:
            raise ValueError("Conflicting catalog navigation archive")
        backup.write_bytes(previous)
        descriptor, temporary = tempfile.mkstemp(prefix=".catalog-navigation-", dir=catalog)
        try:
            try:
                stream = os.fdopen(descriptor, "wb")
            except Exception:
                os.close(descriptor)
                raise
            with stream:
                stream.write(replacement)
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def migrate_builtin_catalog(builtin_dir: Path, skills_dir: Path) -> None:
    """Archive unchanged, source-confirmed legacy installs after catalog install.

    Modified or unregistered copies stay in place. Archives live below the
    catalog's SKILL.md boundary, so neither discovery implementation expands
    them into independent skills. State is committed only after each move;
    retries recognize an archive left by an interrupted migration.
    """
    catalog = skills_dir / BUILTIN_CATALOG_NAME
    if not (catalog / "SKILL.md").is_file():
        return
    state_file = skills_dir / "skills_state.json"
    if not state_file.is_file():
        return
    state = json.loads(state_file.read_text(encoding="utf-8"))
    if not isinstance(state, dict):
        raise ValueError("Skill state must be an object")
    hashes = json.loads((builtin_dir / BUILTIN_CATALOG_NAME / "legacy_hashes.json").read_text(encoding="utf-8"))
    versions_file = builtin_dir / BUILTIN_CATALOG_NAME / "legacy_versions.json"
    versions = json.loads(versions_file.read_text(encoding="utf-8")) if versions_file.is_file() else {}
    _install_missing_catalog_capabilities(builtin_dir, catalog)
    _refresh_catalog_navigation(builtin_dir, catalog, versions)
    plugins = state.get("installed_plugins", [])
    configs = state.get("skill_configs", {})
    if not isinstance(plugins, list) or not isinstance(configs, dict):
        raise ValueError("Invalid legacy skill state")
    local_names = {item.get("name") for item in state.get("local_skills", []) if isinstance(item, dict)}
    migrated = set(state.get("builtin_catalog_migrated", []))
    disabled = set(state.get("builtin_catalog_disabled", []))
    customized = set(state.get("builtin_catalog_customized", []))
    retained = []
    for plugin in plugins:
        name = plugin.get("name") if isinstance(plugin, dict) else None
        if (name not in hashes or name in local_names
                or plugin.get("source", plugin.get("marketplace")) != "builtin"):
            retained.append(plugin)
            continue
        source = skills_dir / name
        archive = catalog / ".legacy" / name
        expected = {hashes[name]}
        for item in versions.get(name, []):
            if isinstance(item, dict) and item.get("digest"):
                expected.add(item["digest"])
        try:
            if source.is_symlink() or archive.is_symlink():
                raise ValueError("Linked legacy skill")
            if source.exists():
                digest = skill_tree_digest(source)
                if digest not in expected:
                    logger.warning("Preserving customized legacy builtin as a local skill: %s", name)
                    customized.add(name)
                    retained.append({**plugin, "source": "local", "marketplace": "local", "legacy_builtin": True})
                    continue
                # A previously archived copy must not prevent a verified old
                # install from being migrated again. Preserve both copies.
                suffix = 0
                while archive.exists():
                    suffix += 1
                    archive = catalog / ".legacy" / f"{name}-{digest[:12]}-{suffix}"
                archive.parent.mkdir(parents=True, exist_ok=True)
                # Both paths must remain inside the selected workspace.
                source_inside = source.resolve().is_relative_to(skills_dir.resolve())
                archive_inside = archive.resolve().is_relative_to(catalog.resolve())
                if not source_inside or not archive_inside:
                    raise ValueError("Legacy skill path escapes workspace")
                source.rename(archive)
            else:
                candidates = [archive, *sorted((catalog / ".legacy").glob(f"{name}-*"))]
                if not any(p.is_dir() and not p.is_symlink() and skill_tree_digest(p) in expected for p in candidates):
                    retained.append(plugin)
                    continue
            migrated.add(name)
            config = configs.get(name, {})
            if (isinstance(config, dict) and config.get("enabled") is False) or plugin.get("enabled") is False:
                disabled.add(name)
        except (OSError, ValueError) as exc:
            logger.warning("Preserving legacy skill %s: %s", name, exc)
            retained.append(plugin)
    state["installed_plugins"] = retained
    state["builtin_catalog_migrated"] = sorted(migrated)
    state["builtin_catalog_disabled"] = sorted(disabled)
    state["builtin_catalog_customized"] = sorted(customized)
    preferences = catalog / "references" / "migration.md"
    preferences.parent.mkdir(parents=True, exist_ok=True)
    preferences.write_text(
        "# 升级偏好 / Upgrade preferences\n\n"
        "以下能力沿用升级前的禁用选择，不加载或执行；仅用户明确重新授权后使用。\n"
        "Do not load or execute these previously disabled capabilities without explicit user authorization.\n\n"
        + ("\n".join(f"- `{name}`" for name in sorted(disabled)) or "无 / None") + "\n",
        encoding="utf-8",
    )
    descriptor, temporary = tempfile.mkstemp(prefix=".catalog-state-", dir=skills_dir)
    try:
        try:
            stream = os.fdopen(descriptor, "w", encoding="utf-8")
        except Exception:
            os.close(descriptor)
            raise
        with stream:
            json.dump(state, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, state_file)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
