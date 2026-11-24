#!/usr/bin/env python3
"""Install an optional HarmonyOS atomic Skill from a local source checkout."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

_STDOUT_FD = 1


def _write_stdout(text: str) -> None:
    """Write user-facing CLI output without treating it as a log message."""
    os.write(_STDOUT_FD, text.encode("utf-8"))


def _load_manifest() -> dict:
    manifest_path = (
        Path(__file__).resolve().parents[1] / "assets" / "atomic-skills.json"
    )
    with manifest_path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _safe_child(root: Path, relative: str) -> Path:
    if not relative or relative.startswith("/") or ".." in Path(relative).parts:
        raise ValueError(f"unsafe relative path: {relative!r}")
    candidate = (root / relative).resolve()
    root_resolved = root.resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise ValueError(f"path escapes root: {relative!r}")
    return candidate


def _find_skill(manifest: dict, name: str) -> dict | None:
    for item in manifest.get("skills", []):
        if item.get("name") == name:
            return item
    return None


def _find_symlink(root: Path) -> Path | None:
    if root.is_symlink():
        return root
    for path in root.rglob("*"):
        if path.is_symlink():
            return path
    return None


def _list_skills(manifest: dict) -> None:
    for item in sorted(
        manifest.get("skills", []),
        key=lambda row: (row.get("category", ""), row.get("name", "")),
    ):
        _write_stdout(
            f"{item.get('category', 'Other')}\t"
            f"{item.get('name', '')}\t{item.get('path', '')}\n"
        )


def install_skill(source: Path, target: Path, name: str, *, force: bool) -> dict:
    manifest = _load_manifest()
    item = _find_skill(manifest, name)
    if item is None:
        return {"ok": False, "error": f"unknown atomic Skill: {name}"}

    src_dir = _safe_child(source, str(item["path"]))
    if not (src_dir / "SKILL.md").is_file():
        return {"ok": False, "error": f"source Skill is missing SKILL.md: {src_dir}"}
    unsafe_link = _find_symlink(src_dir)
    if unsafe_link is not None:
        return {
            "ok": False,
            "error": f"source Skill contains a symbolic link: {unsafe_link}",
        }

    target.mkdir(parents=True, exist_ok=True)
    dest_dir = _safe_child(target, name)
    if dest_dir.is_symlink():
        return {"ok": False, "error": f"target must not be a symbolic link: {dest_dir}"}
    if dest_dir.exists() and not force:
        return {
            "ok": False,
            "error": f"target already exists: {dest_dir}; pass --force to overwrite",
        }

    with tempfile.TemporaryDirectory(prefix=f".{name}.", dir=str(target)) as tmp:
        tmp_dir = Path(tmp) / name
        shutil.copytree(src_dir, tmp_dir, symlinks=False)
        if not (tmp_dir / "SKILL.md").is_file():
            return {"ok": False, "error": "staged Skill is missing SKILL.md"}
        metadata = {
            "name": name,
            "source": manifest.get("source"),
            "source_path": str(item["path"]),
        }
        (tmp_dir / ".jiuwenswarm-source.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if dest_dir.exists():
            if dest_dir.is_dir():
                shutil.rmtree(dest_dir)
            else:
                dest_dir.unlink()
        tmp_dir.rename(dest_dir)

    return {
        "ok": True,
        "name": name,
        "source": str(src_dir),
        "target": str(dest_dir),
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", help="Local harmonyos-agent-skills checkout")
    parser.add_argument(
        "--target",
        default=None,
        help="JiuwenSwarm skills directory (default: <workspace>/skills)",
    )
    parser.add_argument("--skill", help="Atomic Skill name to install")
    parser.add_argument(
        "--force", action="store_true", help="Overwrite an existing installed Skill"
    )
    parser.add_argument(
        "--list", action="store_true", help="List indexed atomic Skills"
    )
    args = parser.parse_args(argv)

    if not args.target:
        # 默认目标：本脚本所在工作空间的 skills/（个人版与企业版租户各自正确）；
        # 脚本不在 skills 树内运行时，回退运行期注入 / 数据根个人版路径。
        skills_root = None
        for parent in Path(__file__).resolve().parents:
            if parent.name == "skills":
                skills_root = parent
                break
        if skills_root is None:
            agent_root_env = os.getenv("JIUWENSWARM_AGENT_ROOT", "").strip()
            if agent_root_env:
                skills_root = Path(agent_root_env).expanduser() / "workspace" / "skills"
            else:
                env_root = os.getenv("JIUWENSWARM_DATA_DIR", "").strip()
                if env_root:
                    data_root = Path(env_root).expanduser()
                else:
                    home = os.getenv("JIUWENSWARM_HOME", "").strip()
                    data_root = (Path(home) if home else Path.home()) / ".jiuwenswarm"
                skills_root = data_root / "profile" / "agent" / "workspace" / "skills"
        args.target = str(skills_root)

    manifest = _load_manifest()
    if args.list:
        _list_skills(manifest)
        return 0
    if not args.skill:
        parser.error("--skill is required unless --list is used")
    if not args.source:
        parser.error("--source is required when installing a Skill")

    result = install_skill(
        Path(args.source).expanduser(),
        Path(args.target).expanduser(),
        args.skill,
        force=args.force,
    )
    _write_stdout(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
