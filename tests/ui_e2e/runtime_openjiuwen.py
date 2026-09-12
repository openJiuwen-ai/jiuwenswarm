from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import psutil

DEFAULT_RUNTIME_ENV_VAR = "JIUWENSWARM_E2E_PYTHON"


@dataclass(frozen=True)
class OpenJiuwenRuntimeInfo:
    package_parent: Path
    resolved_ref: str | None
    source_location: str | None


def build_workspace_env(home: str | Path) -> dict[str, str]:
    """Return a process environment whose user workspace is isolated."""

    env = os.environ.copy()
    home_path = str(Path(home).expanduser().resolve())
    env["HOME"] = home_path
    if os.name == "nt":
        env["USERPROFILE"] = home_path
    return env


def resolve_npm_executable() -> str:
    """Resolve npm on POSIX and Windows without relying on shell lookup."""

    candidates = ("npm.cmd", "npm") if os.name == "nt" else ("npm",)
    for candidate in candidates:
        executable = shutil.which(candidate)
        if executable:
            return executable
    raise RuntimeError("npm is required to build the Web UI for E2E tests")


def resolve_browser_executable() -> str | None:
    """Return an installed Chromium-family browser, or Playwright's default."""

    for candidate in (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "chrome",
        "msedge",
    ):
        executable = shutil.which(candidate)
        if executable:
            return executable

    if os.name != "nt":
        return None

    roots = [
        os.getenv("PROGRAMFILES"),
        os.getenv("PROGRAMFILES(X86)"),
        os.getenv("LOCALAPPDATA"),
    ]
    relative_paths = (
        Path("Google/Chrome/Application/chrome.exe"),
        Path("Microsoft/Edge/Application/msedge.exe"),
    )
    for root in roots:
        if not root:
            continue
        for relative_path in relative_paths:
            executable = Path(root) / relative_path
            if executable.is_file():
                return str(executable)
    return None


def terminate_process_tree(process: subprocess.Popen, timeout: float = 10.0) -> None:
    """Stop an E2E process and every child it spawned."""

    if process.poll() is not None:
        return
    try:
        parent = psutil.Process(process.pid)
        processes = parent.children(recursive=True) + [parent]
    except psutil.Error:
        process.terminate()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        return

    for child in reversed(processes):
        with contextlib.suppress(psutil.Error):
            child.terminate()
    _, alive = psutil.wait_procs(processes, timeout=timeout)
    for child in alive:
        with contextlib.suppress(psutil.Error):
            child.kill()
    psutil.wait_procs(alive, timeout=5)


def build_repo_pythonpath(repo_root: Path, existing: str | None = None) -> str:
    entries = [str(repo_root)]
    if existing:
        entries.extend(item for item in existing.split(os.pathsep) if item)
    return os.pathsep.join(dict.fromkeys(entries))


def resolve_runtime_python(repo_root: Path) -> str:
    env_value = os.getenv(DEFAULT_RUNTIME_ENV_VAR)
    candidates: list[Path] = []

    if env_value:
        env_path = Path(env_value).expanduser()
        if not env_path.is_absolute():
            env_path = repo_root / env_path
        candidates.append(env_path)

    candidates.extend(
        [
            repo_root / ".venv" / "bin" / "python",
        ]
    )

    for candidate in candidates:
        if candidate.exists():
            return str(candidate)

    return sys.executable


def resolve_openjiuwen_runtime(
    python_executable: str,
    *,
    require: bool = False,
) -> OpenJiuwenRuntimeInfo | None:
    script = """
import importlib.metadata
import importlib.util
import json
import subprocess
from pathlib import Path

spec = importlib.util.find_spec("openjiuwen")
if spec is None or not spec.submodule_search_locations:
    raise SystemExit("openjiuwen is not importable in the selected runtime interpreter")

package_dir = Path(next(iter(spec.submodule_search_locations))).resolve()
package_parent = package_dir.parent
source_root = None
git_head = None
for candidate in (package_parent, *package_parent.parents):
    if (candidate / ".git").exists():
        source_root = candidate
        try:
            git_head = subprocess.check_output(
                ["git", "-C", str(candidate), "rev-parse", "HEAD"],
                text=True,
            ).strip()
        except Exception:
            git_head = None
        break

dist_version = None
source_url = None
requested_revision = None
commit_id = None
try:
    dist = importlib.metadata.distribution("openjiuwen")
    dist_version = dist.version
    direct_url_text = dist.read_text("direct_url.json")
    if direct_url_text:
        direct_url = json.loads(direct_url_text)
        source_url = direct_url.get("url")
        vcs_info = direct_url.get("vcs_info") or {}
        requested_revision = vcs_info.get("requested_revision")
        commit_id = vcs_info.get("commit_id")
except Exception:
    pass

resolved_ref = git_head or commit_id or requested_revision or dist_version
source_location = source_url or (str(source_root) if source_root else str(package_parent))

print(
    json.dumps(
        {
            "package_parent": str(package_parent),
            "resolved_ref": resolved_ref,
            "source_location": source_location,
        }
    )
)
""".strip()

    result = subprocess.run(
        [python_executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        if require:
            detail = (result.stderr or result.stdout).strip() or "unknown error"
            raise RuntimeError(
                "Failed to resolve openjiuwen from the selected runtime interpreter: "
                f"{detail}"
            )
        return None

    payload = json.loads(result.stdout)
    return OpenJiuwenRuntimeInfo(
        package_parent=Path(payload["package_parent"]),
        resolved_ref=payload.get("resolved_ref"),
        source_location=payload.get("source_location"),
    )
