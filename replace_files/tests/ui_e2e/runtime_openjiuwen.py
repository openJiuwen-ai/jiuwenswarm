from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

DEFAULT_RUNTIME_ENV_VAR = "JIUWENCLAW_E2E_PYTHON"


@dataclass(frozen=True)
class OpenJiuwenRuntimeInfo:
    package_parent: Path
    resolved_ref: str | None
    source_location: str | None


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
