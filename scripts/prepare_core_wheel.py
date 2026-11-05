"""Prepare paired wheels in a disposable release checkout, never at runtime.

Requires tomlkit and packaging only when explicitly invoked by build_config.
The caller owns dependency locking and the main wheel/frontend build.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from email.parser import BytesParser
from pathlib import Path
from zipfile import ZipFile

import tomlkit
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version


def _named(text: str, name: str) -> bool:
    return canonicalize_name(Requirement(text).name) == name


def _core_requirement(text: str) -> Requirement:
    req = Requirement(text)
    if not _named(text, "openjiuwen") or req.extras or req.marker:
        raise ValueError("Core must be an unmarked openjiuwen requirement without extras")
    if req.url:
        if not re.fullmatch(r"git\+https://[^\s]+@[0-9a-fA-F]{40}", req.url):
            raise ValueError("Core Git URL must use HTTPS and a full commit ID")
    else:
        specs = list(req.specifier)
        if len(specs) != 1 or specs[0].operator != "==" or "*" in specs[0].version:
            raise ValueError("Core PyPI requirement must pin one exact version")
        Version(specs[0].version)
    return req


def _replace_core(text: str, core: Requirement) -> str:
    req = Requirement(text)
    if canonicalize_name(req.name) != "openjiuwen":
        return text
    extras = "[" + ",".join(sorted(req.extras)) + "]" if req.extras else ""
    source = " @ " + core.url if core.url else str(core.specifier)
    marker = " ; " + str(req.marker) if req.marker else ""
    return "openjiuwen" + extras + source + marker


def _rewrite_core(doc: dict, core: Requirement) -> None:
    project = doc["project"]
    uv = doc.setdefault("tool", {}).setdefault("uv", {})
    groups = [project.get("dependencies", [])]
    groups.extend(project.get("optional-dependencies", {}).values())
    groups.append(uv.get("override-dependencies", []))
    for group in groups:
        for i, text in enumerate(group):
            group[i] = _replace_core(text, core)
    uv.get("sources", {}).pop("openjiuwen", None)


def _gateway_source(doc: dict, requirement: str) -> tuple[str, str]:
    source = doc["tool"]["uv"].get("sources", {}).get("agent-gateway", {})
    if source:
        url, rev = source.get("git", ""), source.get("rev", "")
    else:
        url = Requirement(requirement).url or ""
        if not url.startswith("git+") or "@" not in url:
            raise ValueError("Gateway must be pinned to a Git commit in a fresh build checkout")
        url, rev = url[4:].rsplit("@", 1)
    if not url.startswith("https://") or not re.fullmatch(r"[0-9a-fA-F]{40}", rev):
        raise ValueError("Gateway must use HTTPS and a full commit ID in a fresh build checkout")
    return url, rev


def _verify_wheel(path: Path, version: str, core: Requirement) -> None:
    with ZipFile(path) as archive:
        names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(names) != 1:
            raise ValueError("Gateway wheel must have exactly one METADATA file")
        metadata = BytesParser().parsebytes(archive.read(names[0]))
    if canonicalize_name(metadata["Name"]) != "agent-gateway" or metadata["Version"] != version:
        raise ValueError("Gateway wheel identity does not match prepared sources")
    requirements = [Requirement(text) for text in metadata.get_all("Requires-Dist", [])]
    cores = [req for req in requirements if canonicalize_name(req.name) == "openjiuwen"]
    if not cores or any(req.url != core.url or req.specifier != core.specifier for req in cores):
        raise ValueError("Gateway wheel still declares a different Core dependency")


def prepare_core_wheel(root: Path, requirement: str, output: Path) -> None:
    """Build Gateway first; mutate the main manifest only after metadata checks."""
    core = _core_requirement(requirement)
    manifest = root / "pyproject.toml"
    doc = tomlkit.parse(manifest.read_text(encoding="utf-8"))
    deps = doc["project"]["dependencies"]
    if not any(_named(text, "openjiuwen") for text in deps):
        raise ValueError("Main project must declare Core directly")
    indices = [i for i, text in enumerate(deps) if _named(text, "agent-gateway")]
    if len(indices) > 1:
        raise ValueError("Expected at most one Gateway dependency")
    _rewrite_core(doc, core)
    if indices:
        index = indices[0]
        original = Requirement(deps[index])
        if original.extras or original.marker:
            raise ValueError("Gateway release dependency must be unconditional and without extras")
        url, rev = _gateway_source(doc, deps[index])
        with tempfile.TemporaryDirectory(prefix="workswarm-gateway-") as temp:
            staging = Path(temp)
            checkout = staging / "source"
            # stdout is reserved for build_config's machine-readable output.
            subprocess.run(["git", "init", str(checkout)], check=True, stdout=sys.stderr)
            subprocess.run(["git", "-C", str(checkout), "fetch", "--depth=1", url, rev], check=True, stdout=sys.stderr)
            subprocess.run(["git", "-C", str(checkout), "checkout", "--detach", "FETCH_HEAD"], check=True, stdout=sys.stderr)
            gateway_manifest = checkout / "pyproject.toml"
            gateway = tomlkit.parse(gateway_manifest.read_text(encoding="utf-8"))
            _rewrite_core(gateway, core)
            tag = hashlib.sha256((rev + "\n" + str(core)).encode()).hexdigest()[:16]
            version = str(Version(gateway["project"]["version"])).split("+", 1)[0] + "+core." + tag
            gateway["project"]["version"] = version
            gateway_manifest.write_text(tomlkit.dumps(gateway), encoding="utf-8")
            wheels = staging / "wheels"
            subprocess.run(["uv", "build", "--wheel", "--out-dir", str(wheels), str(checkout)], check=True, stdout=sys.stderr)
            built = list(wheels.glob("*.whl"))
            if len(built) != 1:
                raise ValueError("Expected exactly one Gateway wheel")
            _verify_wheel(built[0], version, core)
            output.mkdir(parents=True, exist_ok=True)
            destination = output / built[0].name
            shutil.copyfile(built[0], destination)
            deps[index] = "agent-gateway==" + version
            doc["tool"]["uv"].setdefault("sources", {})["agent-gateway"] = {"path": str(destination)}
            info = {"repository": url, "commit_id": rev, "version": version, "core": str(core)}
            (output / "gateway-build-info.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    manifest.write_text(tomlkit.dumps(doc), encoding="utf-8")
