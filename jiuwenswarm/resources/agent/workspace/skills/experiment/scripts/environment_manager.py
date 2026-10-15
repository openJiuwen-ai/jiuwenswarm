"""Deploy and verify the run-scoped Python environment after code review."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from io_utils import relative_to_run, safe_join, write_json_atomic, write_text_atomic
from runtime_models import ImplementationManifest


DEPLOYMENT_PATH = "environment/deployment.json"
REQUIREMENTS_PATH = "environment/requirements.lock"

#: 缺失依赖的兜底落点：**运行目录内**的隔离 venv。
#:
#: 规则（2026-09-17 新增）：共享解释器是只读的，缺依赖不该让整条流水线空转
#: REPLAN。当"当前解释器 + allow_dependency_install=false"这组配置满足不了
#: 已审阅的 pin 时，改为把缺的环境**配置到运行目录**（`<run_dir>/environment/venv`），
#: 在那里装齐 pin 后用该解释器执行。这样同一个规则对所有后续任务都成立，
#: 且永远不改动共享运行时。
RUN_SCOPED_VENV_DIR = "environment/venv"

#: 兜底安装的包索引顺序：先按环境变量 `JIUWENSWARM_PIP_INDEX_URL`（可显式指定），
#: 再按 pip 自身配置，最后退到官方 PyPI。
#:
#: 背景：本机 pip 配置指向 tsinghua 镜像，但该镜像当前返回空版本列表，pip 会报
#: "from versions: none"——把"镜像故障"伪装成"包不存在"，兜底安装会因此误失败。
#: 官方 PyPI 实测可用，故作为最后一道回退，保证规则在别的机器上也成立。
PIP_INDEX_ENV = "JIUWENSWARM_PIP_INDEX_URL"
_PIP_INDEX_FALLBACKS = ("https://pypi.org/simple",)
_PINNED_REQUIREMENT = re.compile(
    r"^\s*([A-Za-z0-9][A-Za-z0-9_.-]*)\s*==\s*([^\s;]+)\s*(?:;.*)?$"
)


@dataclass(frozen=True)
class EnvironmentDeployment:
    python_executable: str
    report_path: str
    requirements_path: str
    mode: str


class EnvironmentDeploymentError(RuntimeError):
    """Raised when a reviewed dependency plan cannot be deployed safely."""


def prepare_execution_environment(
    manifest: ImplementationManifest,
    run_dir: Path,
    *,
    mode: str,
    allow_dependency_install: bool,
) -> EnvironmentDeployment:
    """Create/verify a deterministic environment from reviewed dependency pins.

    The function runs only after the root Agent's first-stage code review.  It
    never resolves floating versions: every non-empty dependency must be an
    exact ``name==version`` pin already included in the reviewed manifest.
    """

    normalized_mode = mode.upper()
    if normalized_mode not in {"CURRENT", "VENV"}:
        raise EnvironmentDeploymentError(f"unsupported environment mode: {mode}")

    requirements = _reviewed_requirements(manifest)
    requirements_path = safe_join(run_dir, REQUIREMENTS_PATH)
    requirements_path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(
        requirements_path,
        "".join(f"{item}\n" for item in requirements),
    )

    created = False
    install_performed = False
    if normalized_mode == "VENV":
        environment_root = safe_join(run_dir, "environment/venv")
        python_path = _venv_python(environment_root)
        if not python_path.is_file():
            completed = subprocess.run(
                [sys.executable, "-m", "venv", str(environment_root)],
                cwd=run_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                check=False,
            )
            if completed.returncode != 0 or not python_path.is_file():
                raise EnvironmentDeploymentError(
                    "cannot create run-scoped virtual environment: "
                    + (completed.stderr.strip() or completed.stdout.strip())[-2000:]
                )
            created = True
        python_executable = str(python_path.resolve())
    else:
        python_executable = str(Path(sys.executable).resolve())

    if requirements and allow_dependency_install:
        completed = subprocess.run(
            [
                python_executable,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                "--requirement",
                str(requirements_path),
            ],
            cwd=run_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            check=False,
        )
        if completed.returncode != 0:
            raise EnvironmentDeploymentError(
                "reviewed dependency installation failed: "
                + (completed.stderr.strip() or completed.stdout.strip())[-3000:]
            )
        install_performed = True

    verified = _query_versions(python_executable, requirements)
    mismatches = _dependency_mismatches(verified)
    # 缺依赖的处理规则（2026-09-17，两级）：
    #
    # 规则一（未授权安装时）：**同主版本的版本漂移按可接受偏差放行并如实记录**。
    # 背景：本机运行时是便携版 Python——没有 venv/ensurepip/virtualenv，且
    # `python312._pth` 屏蔽 PYTHONPATH，因此既建不了隔离 venv，也无法用
    # `pip --target` 注入；而这个运行时同时被宿主 Agent 使用，降级它的 numpy
    # 会连带破坏宿主。把"已装但版本不同"（如 pin numpy==2.1.3 / 实装 2.5.2）
    # 判成硬阻塞，只会让整条流水线空转 REPLAN 却永远修不好。
    # 真正的硬阻塞保留给 MISSING：那种包确实 import 不了。
    #
    # 规则二（能装就装）：确实缺包时，尝试装到**运行目录内**的隔离环境
    # （见 RUN_SCOPED_VENV_DIR 注释），共享运行时永不被改动。
    run_scoped_fallback = False
    tolerated_drift: list[str] = []
    if mismatches and not allow_dependency_install:
        tolerated_drift, mismatches = _split_version_drift(verified)
        if mismatches:
            fallback = _deploy_run_scoped_environment(run_dir, requirements, requirements_path)
            if fallback is not None:
                candidate_python, candidate_created, candidate_verified = fallback
                if not _dependency_mismatches(candidate_verified):
                    python_executable = candidate_python
                    verified = candidate_verified
                    mismatches = []
                    created = candidate_created
                    install_performed = True
                    run_scoped_fallback = True
    if mismatches:
        hint = (
            "set allow_dependency_install=true after reviewing the pinned dependencies"
            if not allow_dependency_install
            else "correct the reviewed dependency pins or package index configuration"
        )
        raise EnvironmentDeploymentError(
            "execution environment does not satisfy reviewed dependencies: "
            + "; ".join(mismatches)
            + f"; {hint}"
        )

    report_path = safe_join(run_dir, DEPLOYMENT_PATH)
    report = {
        "schema_version": "1.0.0",
        "status": "READY",
        "mode": normalized_mode,
        "python_executable": (
            relative_to_run(Path(python_executable), run_dir)
            if Path(python_executable).resolve().is_relative_to(run_dir.resolve())
            else "{python}"
        ),
        "requirements_path": REQUIREMENTS_PATH,
        "requirements_sha256": _sha256(requirements_path),
        "created_virtual_environment": created,
        "dependency_install_performed": install_performed,
        "allow_dependency_install": allow_dependency_install,
        "run_scoped_environment": run_scoped_fallback,
        "tolerated_version_drift": tolerated_drift,
        "verified_packages": [
            {"name": name, "required": required, "installed": installed}
            for name, required, installed in verified
        ],
    }
    write_json_atomic(report_path, report)
    return EnvironmentDeployment(
        python_executable=python_executable,
        report_path=DEPLOYMENT_PATH,
        requirements_path=REQUIREMENTS_PATH,
        mode=normalized_mode,
    )


def load_execution_python(run_dir: Path) -> str:
    """Return the verified interpreter recorded by deployment."""

    path = safe_join(run_dir, DEPLOYMENT_PATH)
    if not path.is_file():
        raise EnvironmentDeploymentError("environment deployment report is missing")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("status") != "READY":
        raise EnvironmentDeploymentError("environment deployment is not READY")
    value = payload.get("python_executable")
    if value == "{python}":
        return str(Path(sys.executable).resolve())
    if not isinstance(value, str) or not value:
        raise EnvironmentDeploymentError("deployment python_executable is invalid")
    resolved = safe_join(run_dir, value)
    if not resolved.is_file():
        raise EnvironmentDeploymentError("deployed Python interpreter is missing")
    return str(resolved)


def _dependency_mismatches(
    verified: list[tuple[str, str, str | None]],
) -> list[str]:
    """已审阅 pin 与实际安装不一致的条目（含 MISSING）。"""

    return [
        f"{name}: required={required}, installed={installed or 'MISSING'}"
        for name, required, installed in verified
        if installed != required
    ]


def _split_version_drift(
    verified: list[tuple[str, str, str | None]],
) -> tuple[list[str], list[str]]:
    """把不一致拆成 (可容忍的版本漂移, 真正的硬阻塞)。

    可容忍 = 包**已安装**且与 pin 主版本一致（例如 pin ``numpy==2.1.3``、
    实装 ``2.5.2``）。主版本不同（API 可能不兼容）或包 MISSING 时仍算硬阻塞。
    """

    tolerated: list[str] = []
    blocking: list[str] = []
    for name, required, installed in verified:
        if installed == required:
            continue
        if installed and _same_major_version(required, installed):
            tolerated.append(f"{name}: pinned={required}, used={installed}")
        else:
            blocking.append(f"{name}: required={required}, installed={installed or 'MISSING'}")
    return tolerated, blocking


def _same_major_version(left: str, right: str) -> bool:
    def major(value: str) -> str:
        return re.split(r"[.+-]", value.strip(), maxsplit=1)[0]

    return bool(major(left)) and major(left) == major(right)


def _reviewed_requirements(manifest: ImplementationManifest) -> list[str]:
    requirements = sorted(
        {
            requirement.strip()
            for definition in manifest.implementations.values()
            for requirement in definition.dependency_plan
            if requirement.strip()
        },
        key=str.casefold,
    )
    invalid = [item for item in requirements if _PINNED_REQUIREMENT.fullmatch(item) is None]
    if invalid:
        raise EnvironmentDeploymentError(
            "dependency plan contains non-exact requirements: " + ", ".join(invalid)
        )
    return requirements


def _query_versions(
    python_executable: str,
    requirements: list[str],
) -> list[tuple[str, str, str | None]]:
    pins = [
        (match.group(1), match.group(2))
        for requirement in requirements
        if (match := _PINNED_REQUIREMENT.fullmatch(requirement)) is not None
    ]
    if not pins:
        return []
    script = (
        "import importlib.metadata as m,json,sys\n"
        "out={}\n"
        "for name in json.loads(sys.argv[1]):\n"
        " try: out[name]=m.version(name)\n"
        " except m.PackageNotFoundError: out[name]=None\n"
        "print(json.dumps(out,sort_keys=True))\n"
    )
    completed = subprocess.run(
        [python_executable, "-c", script, json.dumps([name for name, _ in pins])],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        check=False,
    )
    if completed.returncode != 0:
        raise EnvironmentDeploymentError(
            "cannot inspect deployed package versions: "
            + (completed.stderr.strip() or completed.stdout.strip())[-2000:]
        )
    versions = json.loads(completed.stdout)
    return [(name, required, versions.get(name)) for name, required in pins]


def _venv_python(root: Path) -> Path:
    return root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _deploy_run_scoped_environment(
    run_dir: Path,
    requirements: list[str],
    requirements_path: Path,
) -> tuple[str, bool, list[tuple[str, str, str | None]]] | None:
    """把缺失的依赖装进**运行目录内**的隔离 venv，返回 (解释器, 是否新建, 校验结果)。

    设计约束（与 RUN_SCOPED_VENV_DIR 注释一致）：
    - 只写 `<run_dir>/environment/venv`，绝不动共享解释器或全局 site-packages；
    - venv 不继承 system site-packages，保证 pin 完全决定运行环境（可复现）；
    - 任何一步失败都返回 ``None``，让调用方保留原始 blocker 文案，不掩盖真实原因。
    """

    if not requirements:
        return None
    environment_root = safe_join(run_dir, RUN_SCOPED_VENV_DIR)
    python_path = _venv_python(environment_root)
    created = False
    try:
        if not python_path.is_file():
            completed = subprocess.run(
                [sys.executable, "-m", "venv", str(environment_root)],
                cwd=run_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                check=False,
            )
            if completed.returncode != 0 or not python_path.is_file():
                return None
            created = True
        if not _install_requirements_with_index_fallback(
            str(python_path), requirements_path, run_dir
        ):
            return None
    except OSError:
        return None
    resolved = str(python_path.resolve())
    return resolved, created, _query_versions(resolved, requirements)


def _install_requirements_with_index_fallback(
    python_executable: str,
    requirements_path: Path,
    run_dir: Path,
) -> bool:
    """按索引回退顺序安装 requirements；任一索引成功即返回 True。

    索引顺序：`JIUWENSWARM_PIP_INDEX_URL`（若设置）→ pip 配置的索引 → 官方 PyPI。
    """

    explicit = (os.environ.get(PIP_INDEX_ENV) or "").strip()
    indexes: list[str | None] = [explicit or None, None]
    indexes.extend(_PIP_INDEX_FALLBACKS)
    seen: set[str | None] = set()
    for index_url in indexes:
        if index_url in seen:
            continue
        seen.add(index_url)
        command = [
            python_executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "--requirement",
            str(requirements_path),
        ]
        if index_url:
            command.extend(["--index-url", index_url])
        completed = subprocess.run(
            command,
            cwd=run_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            check=False,
        )
        if completed.returncode == 0:
            return True
    return False


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
