"""Pin release dependencies to PyPI versions or official Git commits."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
import tomllib

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version


_OFFICIAL_REPOSITORIES = {
    "openjiuwen": {
        "https://gitcode.com/openJiuwen/agent-core.git",
        "https://github.com/openJiuwen-ai/agent-core.git",
    },
    "openjiuwen-agent-gateway": {
        "https://gitcode.com/openJiuwen/agent-gateway.git",
        "https://github.com/openJiuwen-ai/agent-gateway.git",
    },
}


def _official_pin(name: str, repository: str, revision: str, location: str) -> tuple[str, str]:
    if repository not in _OFFICIAL_REPOSITORIES[name] or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError(
            f"Release blocked: {location} must pin {name} to an official repository "
            "and a full commit SHA. Merge the dependency change upstream, update every "
            "reference and regenerate uv.lock before building a release. "
            "Use an editable install to test the candidate."
        )
    return repository, revision



def _pypi_pin(requirement: Requirement, location: str) -> tuple[str, str]:
    specifiers = list(requirement.specifier)
    if len(specifiers) != 1 or specifiers[0].operator != "==" or "*" in specifiers[0].version:
        raise ValueError(f"Release blocked: {location} must pin {requirement.name} to one exact PyPI version.")
    return "https://pypi.org/simple", str(Version(specifiers[0].version))

def validate_release_dependencies(project_root: Path) -> None:
    """Reject candidate dependencies before wheel, sdist, or frozen builds."""
    with (project_root / "pyproject.toml").open("rb") as source:
        config = tomllib.load(source)
    project = config["project"]
    uv = config.get("tool", {}).get("uv", {})
    groups = {"project.dependencies": project.get("dependencies", [])}
    for name, requirements in project.get("optional-dependencies", {}).items():
        groups[f"project.optional-dependencies.{name}"] = requirements
    groups["tool.uv.override-dependencies"] = uv.get("override-dependencies", [])

    pins: dict[str, set[tuple[str, str]]] = {}
    for location, requirements in groups.items():
        for raw in requirements:
            requirement = Requirement(raw)
            name = canonicalize_name(requirement.name)
            if name not in _OFFICIAL_REPOSITORIES:
                continue
            if not requirement.url:
                if not requirement.specifier and location != "project.dependencies":
                    continue
                pins.setdefault(name, set()).add(_pypi_pin(requirement, location))
                continue
            direct_reference = urlsplit((requirement.url or "").removeprefix("git+"))
            repository, _, revision = direct_reference._replace(query="", fragment="").geturl().rpartition("@")
            pins.setdefault(name, set()).add(_official_pin(name, repository, revision, location))

    for source_name, sdk_source in uv.get("sources", {}).items():
        name = canonicalize_name(source_name)
        if name not in _OFFICIAL_REPOSITORIES:
            continue
        if not isinstance(sdk_source, dict):
            raise ValueError(f"Release blocked: {name} must have one pinned official uv source.")
        pins.setdefault(name, set()).add(
            _official_pin(name, sdk_source.get("git", ""), sdk_source.get("rev", ""), "tool.uv.sources")
        )

    lock_path = project_root / "uv.lock"
    if lock_path.is_file():
        with lock_path.open("rb") as source:
            lock = tomllib.load(source)
        for package in lock.get("package", []):
            name = canonicalize_name(package["name"])
            if name not in _OFFICIAL_REPOSITORIES:
                continue
            source_config = package.get("source", {})
            if "registry" in source_config:
                if source_config["registry"].rstrip("/") != "https://pypi.org/simple":
                    raise ValueError(f"Release blocked: {name} must use the official PyPI registry.")
                pins.setdefault(name, set()).add(("https://pypi.org/simple", str(Version(package["version"]))))
                continue
            parsed = urlsplit(source_config.get("git", ""))
            repository = parsed._replace(query="", fragment="").geturl()
            revision = parse_qs(parsed.query).get("rev", [""])[0]
            pin = _official_pin(name, repository, revision, "uv.lock")
            if parsed.fragment != revision:
                raise ValueError(f"Release blocked: {name} lock commit does not match its pinned revision.")
            pins.setdefault(name, set()).add(pin)

    if "openjiuwen" not in pins or any(len(revisions) != 1 for revisions in pins.values()):
        raise ValueError("Release blocked: each protected dependency must use one consistent PyPI version or official Git revision.")
