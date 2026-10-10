# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""MCP skill installer.

Copies the bundled skills of a marketplace MCP (form C, CLI-based) into the
workspace runtime skills directory and flips their enabled flag via
``SkillManager.set_skill_enabled``. Idempotent: re-installing overwrites in
place and keeps the enabled state.

Marketplace skill layouts vary, so discovery handles two shapes:
  * nested:  ``skills/<skill>/(SKILL.md|*.md)`` -> one skill per subdir.
  * flat:    ``skills/SKILL.md`` (optionally with references/scripts/) ->
             the whole ``skills/`` dir is one skill named after the MCP.

Form A/B (remote/stdio MCP) have no bundled skills; this module is a no-op.
"""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path
from typing import Any

from jiuwenswarm.common.utils import get_workspace_dir  # re-export for test patches

from jiuwenswarm.server.runtime.mcp.paths import has_skill_file as _has_skill_file

logger = logging.getLogger(__name__)


def _mcp_root() -> Path:
    return get_workspace_dir() / "mcp"


def _packages_dir() -> Path:
    return _mcp_root() / "mcp_builtins"


def _mcp_pkg_dir(name: str) -> Path:
    return _packages_dir() / str(name or "").strip()

# ``@skills/connector-<name>`` is a path placeholder used by skill-only MCPs
# (ctrip-wendao, netease-mail) in their SKILL.md command examples. openjiuwen's
# BashTool does not resolve it — cwd at run time is the workspace root, not the
# skill dir, so the literal path misses. At install time we rewrite it to the
# installed skill's absolute path so ``node <abs path>`` works.
_SKILL_PLACEHOLDER_RE = re.compile(r"@skills/connector-([A-Za-z0-9_.\-]+)")


def _rewrite_skill_paths(skill_md_path: Path, mcp_name: str, installed_dir: Path) -> None:
    """Rewrite ``@skills/connector-<name>`` placeholders in a SKILL.md to the
    installed skill dir's absolute path.

    Only rewrites placeholders that target the MCP itself (``<name>`` ==
    ``mcp_name``) — we know that dir's absolute path. Cross-MCP references are
    left as-is (we can't resolve another MCP's install path at this MCP's
    install time). ``installed_dir`` is the absolute path of the skill's own
    install dir (flat layout: ``mcp/skills/<name>/``; nested: ``<name>/<skill>/``).
    """
    try:
        text = skill_md_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("[skill_installer] read SKILL.md '%s' failed: %s", skill_md_path, exc)
        return
    if "@skills/" not in text:
        return

    def _replace(m: re.Match) -> str:
        target_name = m.group(1)
        # Only rewrite self-references; leave cross-MCP placeholders.
        if target_name != mcp_name:
            return m.group(0)
        return str(installed_dir)

    new_text = _SKILL_PLACEHOLDER_RE.sub(_replace, text)
    if new_text != text:
        try:
            skill_md_path.write_text(new_text, encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "[skill_installer] rewrite SKILL.md '%s' failed: %s",
                skill_md_path, exc,
            )


def _bundled_skills(pkg_dir: Path) -> list[tuple[Path, str]]:
    """Discover bundled skill source dirs + their runtime names.

    Returns ``[(source_dir, skill_name), ...]``. Nested subdirs become one
    skill each (named by subdir). A flat ``skills/SKILL.md`` layout collapses
    to a single skill named after the MCP.
    """
    skills_dir = pkg_dir / "skills"
    if not skills_dir.is_dir():
        return []
    out: list[tuple[Path, str]] = []
    # flat layout: SKILL.md sits directly under skills/ (a stray README.md
    # must NOT trigger this — it would swallow every nested skill into one
    # bogus skill and the agent would see none of them).
    if _has_skill_file(skills_dir):
        out.append((skills_dir, pkg_dir.name))
        return out
    # nested layout: each child dir with a skill file is one skill
    for entry in sorted(skills_dir.iterdir()):
        if entry.is_dir() and _has_skill_file(entry):
            out.append((entry, entry.name))
    return out


def _mcp_skills_root() -> Path:
    """MCP-bundled skills root: <workspace>/mcp/skills/."""
    return _mcp_root() / "skills"


def _mcp_skills_dir(name: str) -> Path:
    """Per-MCP skills dir: <workspace>/mcp/skills/<name>/.

    Each MCP gets its own subdir (namespaced by MCP name) so same-named skills
    across MCPs don't collide. No ``connector-`` prefix — the MCP name itself
    is the namespace.
    """
    return _mcp_skills_root() / str(name or "").strip()


def install_mcp_skills(name: str) -> dict[str, Any]:
    """Install an MCP's bundled skills under mcp/skills/.

    Skills are copied to ``<workspace>/mcp/skills/<name>/<skill>/``
    (physically isolated from user skills). The scan list is derived from
    state.json's connected records, so the installed skills surface to the
    agent only while the MCP is connected.

    Returns ``{"name", "installed": [<skill>, ...]}``.
    """
    n = str(name or "").strip()
    if not n:
        raise ValueError("mcp name is required")
    pkg = _mcp_pkg_dir(n)
    if not pkg.is_dir():
        raise KeyError(f"mcp '{n}' not found in marketplace")

    skills = _bundled_skills(pkg)
    installed: list[str] = []
    if not skills:
        logger.info("[skill_installer] mcp '%s' has no bundled skills", n)
        return {"name": n, "installed": installed}

    # MCP skills live under mcp/skills/<name>/<skill>/ — one subdir per skill.
    # flat layout (SKILL.md directly under pkg/skills/) is normalized to the
    # same shape: the single skill lands in mcp/skills/<name>/<name>/ so
    # SkillUseRail (which only scans root.iterdir() children for SKILL.md,
    # never the root itself) discovers it. See skill_use_rail._refresh_skills_incrementally.
    dest_root = _mcp_skills_dir(n)
    dest_root.mkdir(parents=True, exist_ok=True)

    from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager

    mgr = SkillManager()
    for src, skill_name in skills:
        dest = dest_root / skill_name
        try:
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(src, dest)
        except OSError as exc:
            logger.warning("[skill_installer] copy '%s' failed: %s", skill_name, exc)
            continue
        # Rewrite ``@skills/connector-<name>`` path placeholders in every
        # bundled .md to their installed absolute path (skill-only MCPs like
        # ctrip-wendao/netease-mail use this placeholder in command examples).
        for md_path in dest.rglob("*.md"):
            if md_path.is_file():
                _rewrite_skill_paths(md_path, n, dest)
        installed.append(skill_name)
    logger.info("[skill_installer] installed skills for '%s': %s", n, installed)
    return {"name": n, "installed": installed}


def uninstall_mcp_skills(name: str) -> dict[str, Any]:
    """Remove an MCP's bundled skills.

    Deletes ``<workspace>/mcp/skills/<name>/`` (all bundled skills). The scan
    list is derived from state.json's connected records, so disconnecting the
    MCP removes it from the scan list — no explicit unregister call needed.

    Returns ``{"name", "removed": [...]}``.
    """
    n = str(name or "").strip()
    if not n:
        raise ValueError("mcp name is required")
    removed: list[str] = []
    from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager

    mgr = SkillManager()
    # Remove the MCP skills dir tree (all bundled skills under it).
    skills_dir = _mcp_skills_dir(n)
    if skills_dir.is_dir():
        try:
            # Record skill names before rmtree for the result.
            for child in skills_dir.iterdir():
                if child.is_dir():
                    removed.append(child.name)
                    try:
                        mgr.remove_skill_config(child.name)
                    except Exception:  # noqa: BLE001
                        pass
            shutil.rmtree(skills_dir)
        except OSError as exc:
            logger.warning("[skill_installer] remove skills dir '%s' failed: %s", skills_dir, exc)
    return {"name": n, "removed": removed}


__all__ = ["install_mcp_skills", "uninstall_mcp_skills"]
