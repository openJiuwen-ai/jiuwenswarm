"""Capture the public source revision at the start of a package build."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[1]
UPSTREAM_URL = "https://gitcode.com/openJiuwen/jiuwenswarm.git"
OUTPUT = ROOT / "jiuwenswarm" / "resources" / "build-info.json"
SHA_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
logger = logging.getLogger(__name__)


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return result.stdout.strip()


def upstream_branch() -> str:
    branch = os.getenv("JIUWENSWARM_UPSTREAM_BRANCH", "").strip()
    if not branch:
        parents = _git("show", "-s", "--format=%P", "HEAD").split()
        if len(parents) == 2:
            # CI builds a synthetic merge whose first parent is the target tip.
            refs = _git("for-each-ref", "--format=%(refname:short) %(objectname)", "refs/remotes/origin")
            targets = set()
            for line in refs.splitlines():
                parts = line.split()
                if len(parts) == 2:
                    ref, commit = parts
                    if commit == parents[0] and ref.startswith("origin/") and ref != "origin/HEAD":
                        targets.add(ref.removeprefix("origin/"))
            if len(targets) == 1:
                branch = targets.pop()
        if not branch:
            branch = _git("branch", "--show-current")
    if not branch or _git("check-ref-format", "--branch", branch) != branch:
        raise ValueError("Set JIUWENSWARM_UPSTREAM_BRANCH to an upstream branch name")
    return branch


def upstream_commit(branch: str) -> str:
    ref = f"refs/heads/{branch}"
    response = _git("ls-remote", "--exit-code", UPSTREAM_URL, ref)
    matches = [line.split() for line in response.splitlines()]
    if len(matches) != 1 or len(matches[0]) != 2 or matches[0][1] != ref:
        raise ValueError(f"Unexpected upstream response for {ref}")
    commit = matches[0][0]
    if not SHA_PATTERN.fullmatch(commit):
        raise ValueError(f"Invalid upstream commit for {ref}")
    return commit


def locked_agent_core(*, require_branch: bool = False) -> dict[str, str]:
    with (ROOT / "uv.lock").open("rb") as handle:
        packages = tomllib.load(handle)["package"]
    package = next(item for item in packages if item["name"] == "openjiuwen")
    source = package.get("source", {})
    git_source = source.get("git", "")
    if git_source:
        commit = git_source.rsplit("#", 1)[-1]
        if not SHA_PATTERN.fullmatch(commit):
            raise ValueError("openjiuwen Git commit is missing from uv.lock")
        info = {"source": "git", "version": package["version"], "commit_id": commit}
        branch = os.getenv("JIUWENSWARM_AGENT_CORE_BRANCH", "").strip()
        if require_branch and not branch:
            raise ValueError("Set JIUWENSWARM_AGENT_CORE_BRANCH for the locked openjiuwen Git dependency")
        if branch:
            info["branch"] = branch
        return info
    if source.get("registry"):
        return {"source": "pypi", "version": package["version"]}
    raise ValueError("Unsupported openjiuwen source in uv.lock")


def generate(*, require_agent_core_branch: bool = False) -> dict[str, object]:
    branch = upstream_branch()
    with (ROOT / "pyproject.toml").open("rb") as handle:
        version = tomllib.load(handle)["project"]["version"]
    return {
        "schema_version": 1,
        "package_version": version,
        "jiuwenswarm": {
            "branch": branch,
            "commit_id": upstream_commit(branch),
        },
        "agent_core": locked_agent_core(require_branch=require_agent_core_branch),
    }


def main(*, require_agent_core_branch: bool = False) -> None:
    # A failed lookup must not leave a previous build's provenance available
    # for a later direct PyInstaller invocation.
    OUTPUT.unlink(missing_ok=True)
    payload = generate(require_agent_core_branch=require_agent_core_branch)
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    logger.info("Build source: %s @ %s", payload["jiuwenswarm"]["branch"], payload["jiuwenswarm"]["commit_id"])


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-agent-core-branch", action="store_true")
    args = parser.parse_args()
    main(require_agent_core_branch=args.require_agent_core_branch)
