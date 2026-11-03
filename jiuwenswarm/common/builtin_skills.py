"""Install-time migration for the bundled capability catalog."""

from __future__ import annotations

import hashlib
import json
import logging
import os
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
    plugins = state.get("installed_plugins", [])
    configs = state.get("skill_configs", {})
    if not isinstance(plugins, list) or not isinstance(configs, dict):
        raise ValueError("Invalid legacy skill state")
    local_names = {item.get("name") for item in state.get("local_skills", []) if isinstance(item, dict)}
    migrated = set(state.get("builtin_catalog_migrated", []))
    disabled = set(state.get("builtin_catalog_disabled", []))
    retained = []
    for plugin in plugins:
        name = plugin.get("name") if isinstance(plugin, dict) else None
        if (name not in hashes or name in local_names
                or plugin.get("source", plugin.get("marketplace")) != "builtin"):
            retained.append(plugin)
            continue
        source = skills_dir / name
        archive = catalog / ".legacy" / name
        try:
            if source.is_symlink() or archive.is_symlink():
                raise ValueError("Linked legacy skill")
            if source.exists():
                if archive.exists() or skill_tree_digest(source) != hashes[name]:
                    logger.warning("Preserving modified or conflicting legacy skill: %s", name)
                    retained.append(plugin)
                    continue
                archive.parent.mkdir(parents=True, exist_ok=True)
                # Both paths must remain inside the selected workspace.
                if not source.resolve().is_relative_to(skills_dir.resolve()) or not archive.resolve().is_relative_to(catalog.resolve()):
                    raise ValueError("Legacy skill path escapes workspace")
                source.rename(archive)
            elif not archive.is_dir() or skill_tree_digest(archive) != hashes[name]:
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
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, state_file)
    finally:
        Path(temporary).unlink(missing_ok=True)
