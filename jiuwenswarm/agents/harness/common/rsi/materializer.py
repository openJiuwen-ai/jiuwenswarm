"""Create immutable, task-private inputs for a Harness Validation run."""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import yaml

from jiuwenswarm.agents.harness.common.rsi.errors import (
    RsiDatasetInvalid,
    RsiInvalidHarness,
    RsiPathInvalid,
    RsiPathNotAllowed,
)
from jiuwenswarm.agents.harness.common.rsi.validation_dataset import (
    normalize_validation_suite,
)

VALIDATION_PROFILE_NAME = "validation-general-v1"
_YAML_MANIFEST_NAMES = ("harness_config.yaml", "expert_harness.yaml", "harness.yaml")
_MANIFEST_NAMES = ("manifest.json", *_YAML_MANIFEST_NAMES)
_SIDECAR_MANIFEST_NAMES = ("harness_config.yaml", "harness.yaml")
_SIDECAR_DIRECTORIES = ("prompt_sections", "tools", "mcps", "rails", "skills")
_SIDECAR_LIST_FILES = (
    ("tools", "tools.yaml"),
    ("mcps", "mcps.yaml"),
    ("mcps", "mcps.json"),
    ("rails", "rails.yaml"),
    ("skills", "skills.yaml"),
)


@dataclass(frozen=True, slots=True)
class RsiTaskMaterialization:
    """Paths and non-secret hashes created for one task."""

    dataset: dict[str, Any]
    harness: dict[str, Any]
    models: dict[str, dict[str, Any]]
    profile: dict[str, Any]

    def to_manifest(self) -> dict[str, Any]:
        return {
            "input_snapshot": {
                "source_path": self.dataset.get("source_path"),
                "path": self.dataset.get("path"),
                "sha256": self.dataset.get("sha256"),
                "files": deepcopy(self.dataset.get("files", {})),
            },
            "harness_snapshot": {
                "source_path": self.harness.get("source_path"),
                "package_path": self.harness.get("package_path"),
                "refs_path": self.harness.get("path"),
                "sha256": self.harness.get("sha256"),
                "source_sha256": self.harness.get("source_sha256"),
                "target_sha256": self.harness.get("target_sha256"),
            },
            "models": deepcopy(self.models),
            "profile": {
                "name": VALIDATION_PROFILE_NAME,
                "path": self.profile.get("path"),
                "sha256": self.profile.get("sha256"),
            },
        }


class RsiTaskMaterializer:
    """Materialize dataset, Harness refs, and the hidden Validation profile.

    ``dataset_root`` and ``harness_root`` are optional because existing local
    callers may already have performed trust checks.  Production composition
    roots should provide them (or configure ``RSI_DATASET_ROOT`` /
    ``RSI_HARNESS_ROOT``) to reject browser-supplied paths outside approved
    trees.
    """

    def __init__(
        self,
        tasks_root: str | Path,
        *,
        dataset_root: str | Path | None = None,
        harness_root: str | Path | None = None,
        dataset_validator: Callable[[str], Any] | None = None,
        allow_missing_harness: bool = False,
    ) -> None:
        self.tasks_root = Path(tasks_root).expanduser().resolve()
        self.dataset_root = _optional_root(dataset_root)
        self.harness_root = _optional_root(harness_root)
        self.dataset_validator = dataset_validator
        self.allow_missing_harness = bool(allow_missing_harness)

    def task_dir(self, task_id: str) -> Path:
        task = str(task_id or "").strip()
        if not task or Path(task).name != task or task in {".", ".."}:
            raise RsiPathInvalid(f"task_id 非法: {task_id}")
        path = (self.tasks_root / task).resolve()
        _ensure_within(path, self.tasks_root, label="任务目录")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def materialize_dataset(
        self,
        task_id: str,
        source_path: str | Path,
        *,
        domain: str | None = None,
        validator: Callable[[str], Any] | None = None,
    ) -> dict[str, Any]:
        source = self._source_file(source_path, self.dataset_root, label="数据集")
        try:
            normalized = (
                _load_evobench_suite(source, domain=domain)
                if domain
                else normalize_validation_suite(source)
            )
        except RsiDatasetInvalid:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RsiDatasetInvalid(f"数据集校验失败: {exc}") from exc
        check = validator or self.dataset_validator

        target_dir = self.task_dir(task_id) / "input"
        target_dir.mkdir(parents=True, exist_ok=True)
        if normalized is None:
            target = target_dir / _safe_filename(source.name, default="validation.json")
            shutil.copy2(source, target)
        else:
            target = target_dir / "cases.json"
            target.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        try:
            from openjiuwen.rsi.harness_rsi.data_loader import load_json_cases
            from openjiuwen.rsi.harness_rsi.data_loader.case_files import (
                copy_dataset_files,
            )

            cases = normalized["cases"] if normalized is not None else load_json_cases(source)
            files = copy_dataset_files(cases, source, target)
        except (ValueError, TypeError, OSError) as exc:
            raise RsiDatasetInvalid(f"Dataset files are invalid: {exc}") from exc

        if check is not None:
            try:
                result = check(str(target))
            except (RsiDatasetInvalid, RsiPathInvalid, RsiPathNotAllowed):
                raise
            except Exception as exc:  # noqa: BLE001
                raise RsiDatasetInvalid(f"数据集校验失败: {exc}") from exc
            if not _validation_ok(result):
                raise RsiDatasetInvalid(
                    "Provider 输入校验失败",
                    errors=_validation_errors(result),
                )
        elif normalized is None:
            _validate_json_dataset(target)
        return {
            # The task manifest records the immutable task-local copy.  The
            # caller's source path is an input to creation, not a runtime
            # dependency after materialization.
            "source_path": str(target.resolve()),
            "path": str(target.resolve()),
            "sha256": _sha256(target),
            "source_sha256": _sha256(source),
            "files": files,
            "dataset_id": (
                str(normalized.get("dataset_id") or "").strip()
                if normalized is not None
                else None
            ),
        }

    def materialize_harness_refs(
        self,
        task_id: str,
        source_path: str | Path,
        *,
        role: str = "validation_harness",
    ) -> dict[str, Any]:
        requested_source = self._source_path(source_path, self.harness_root, label="Harness")
        source = _resolve_harness_source(requested_source, self.harness_root, role=role)
        config_path = _harness_config_path(source)
        try:
            source_data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise RsiInvalidHarness(f"Harness 配置不可读: {config_path}") from exc
        if not isinstance(source_data, dict) or not source_data:
            raise RsiInvalidHarness(f"Harness 配置必须是非空 mapping: {config_path}")
        try:
            if config_path.name == "manifest.json":
                from openjiuwen.harness.resources import load_plugin_package

                load_plugin_package(config_path)
            else:
                # Reuse the AutoHarness import-time allow-list/path guards for
                # the source package.  This prevents a trusted-ref wrapper from
                # bypassing the existing ``mcps`` and path traversal checks.
                from jiuwenswarm.agents.harness.common.auto_harness.service import (
                    validate_harness_config,
                )

                validate_harness_config(
                    config_path,
                    package_dir=source if source.is_dir() else source.parent,
                )
        except RsiInvalidHarness:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize package guards
            raise RsiInvalidHarness(f"Harness 配置安全校验失败: {config_path}") from exc
        role_name = str(role or "").strip()
        if not role_name or Path(role_name).name != role_name:
            raise RsiInvalidHarness(f"Harness role 非法: {role}")

        task_dir = self.task_dir(task_id)
        target_dir = task_dir / "harness"
        target_dir.mkdir(parents=True, exist_ok=True)
        source_sha256 = _path_sha256(source)
        version_id = f"baseline-{source_sha256[:16]}"
        version_root = target_dir / "versions" / version_id
        if source.is_file():
            package = _materialize_file_harness(source, source_data, version_root)
        else:
            package = version_root / (source.name or "harness")
            _copy_harness_source(source, package)
            if _path_sha256(package) != source_sha256:
                raise RsiInvalidHarness(f"Harness task-local copy hash 不一致: {source}")
        target = target_dir / "harness_refs.yaml"
        payload = {"harness_refs": {role_name: str(package.resolve())}}
        target.write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        return {
            "source_path": str(package.resolve()),
            "package_path": str(package.resolve()),
            "path": str(target.resolve()),
            "sha256": _sha256(target),
            "source_sha256": _path_sha256(package),
            "target_sha256": _path_sha256(package),
            "source_config_path": str(_harness_config_path(package).resolve()),
            "role": role_name,
            "version_id": version_id,
            "wrapper_source_path": (
                str(requested_source) if requested_source != source else None
            ),
        }

    def materialize_validation_profile(
        self,
        task_id: str,
        model_paths: dict[str, str],
        *,
        profile_name: str = VALIDATION_PROFILE_NAME,
        options: Mapping[str, Any] | None = None,
        max_iterations: int = 1,
        search_width: int = 1,
    ) -> dict[str, Any]:
        required_roles = {"evaluation", "analysis", "member_optimization"}
        missing = sorted(required_roles - set(model_paths))
        if missing:
            raise RsiPathInvalid(
                f"Validation profile 缺少模型文件: {', '.join(missing)}"
            )
        normalized_paths: dict[str, str] = {}
        task_dir = self.task_dir(task_id)
        models_dir = (task_dir / "models").resolve()
        for role in required_roles:
            path = Path(str(model_paths.get(role) or "")).expanduser().resolve()
            _ensure_within(path, models_dir, label=f"模型文件({role})")
            if not path.is_file():
                raise RsiPathInvalid(f"模型文件不存在: {path}")
            normalized_paths[role] = str(path)

        run_dir = (task_dir / "run").resolve()
        run_dir.mkdir(parents=True, exist_ok=True)
        target_dir = task_dir / "config"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "harness_orchestrator.yaml"
        profile_options = dict(options or {})
        if max_iterations != 1 or "max_epochs" not in profile_options:
            profile_options["max_epochs"] = max_iterations
        if search_width != 1 or "sibling_candidate_count" not in profile_options:
            profile_options["sibling_candidate_count"] = search_width
        profile_options = _profile_options(profile_options)
        payload = {
            "workspace_dir": str(run_dir),
            "max_epochs": profile_options["max_epochs"],
            "model_configs": normalized_paths,
            "data_loader": {
                "file_pattern": "*.json",
                "batch_size": profile_options["batch_size"],
                "batch_balance_keys": ["difficulty", "dimension", "source", "task_type"],
            },
            "evaluator": {
                "backend": "single_harness",
                "evaluation_method": "script-based",
                "transient_case_retry_limit": 2,
            },
            "evaluation_result_analyzer": {
                "diagnosis_agent_max_retries": 2,
                "diagnosis_agent_max_concurrency": 5,
                "diagnosis_agent_max_iterations": 20,
                "max_issues": 8,
                "evidence_limit_per_issue": 3,
                "output_filename": "issues.yaml",
            },
            "member_optimizer": {
                "action_group_configs": ["prompt", "skill", "tool", "rail"],
                "allowed_action_groups": ["prompt", "skill", "tool", "rail"],
                "allowed_prompt_surfaces": ["prompt_section"],
                "max_roles_per_run": 1,
                "execution_concurrency": 1,
                "role_execution_concurrency": 1,
                "action_execution_concurrency_per_role": 1,
                "sibling_candidate_count": profile_options["sibling_candidate_count"],
                "max_issue_attempts_per_batch": profile_options["max_issue_attempts"],
                "max_repair_rounds_per_batch": profile_options["max_repair_rounds"],
                "candidate_holdout_cases": 0,
                "improver_policy_ref": profile_options["improver_policy_ref"],
            },
            "scheduling": {
                "evaluation_strategy": "hybrid",
                "coordination_strategy": "team_first_single_pass",
                "promotion_policy": "epoch_full_evaluation",
                "full_evaluation_enabled": True,
            },
        }
        if profile_options["runtime"]:
            payload["rsi_runtime"] = profile_options["runtime"]
        target.write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        # Validate against the installed openjiuwen dataclasses immediately;
        # malformed internal fields must fail during create, not worker startup.
        try:
            from openjiuwen.rsi.harness_rsi.config import (
                load_auto_coordinating_harness_config,
            )

            load_auto_coordinating_harness_config(str(target))
        except Exception as exc:  # noqa: BLE001
            raise RsiPathInvalid(f"Validation profile 无效: {exc}") from exc
        return {
            "name": str(profile_name or VALIDATION_PROFILE_NAME),
            "path": str(target.resolve()),
            "sha256": _sha256(target),
        }

    def materialize(
        self,
        task_id: str,
        dataset_path: str | Path,
        harness_path: str | Path | None,
        *,
        model_refs: dict[str, str],
        model_resolver: Any,
        domain: str | None = None,
        profile_options: Mapping[str, Any] | None = None,
        validator: Callable[[str], Any] | None = None,
        max_iterations: int = 1,
        search_width: int = 1,
    ) -> RsiTaskMaterialization:
        """Materialize all task inputs in a deterministic order."""

        dataset = self.materialize_dataset(
            task_id,
            dataset_path,
            domain=domain,
            validator=validator,
        )
        if harness_path:
            harness = self.materialize_harness_refs(task_id, harness_path)
        elif self.allow_missing_harness:
            harness = {
                "source_path": None,
                "package_path": None,
                "path": None,
                "sha256": None,
                "source_sha256": None,
                "target_sha256": None,
                "source_config_path": None,
                "role": None,
                "version_id": None,
                "wrapper_source_path": None,
            }
        else:
            raise RsiInvalidHarness("当前没有可用的活动 Harness 配置")
        models_dir = self.task_dir(task_id) / "models"
        optimizer_ref = str(model_refs.get("optimizer") or "").strip()
        tester_ref = str(model_refs.get("tester") or "").strip()
        models = {
            "evaluation": model_resolver.resolve_to_file(
                tester_ref, "evaluation", models_dir
            ),
            "analysis": model_resolver.resolve_to_file(
                optimizer_ref, "analysis", models_dir
            ),
            "member_optimization": model_resolver.resolve_to_file(
                optimizer_ref, "member_optimization", models_dir
            ),
        }
        effective_profile_options = dict(profile_options or {})
        if max_iterations != 1 or "max_epochs" not in effective_profile_options:
            effective_profile_options["max_epochs"] = max_iterations
        if search_width != 1 or "sibling_candidate_count" not in effective_profile_options:
            effective_profile_options["sibling_candidate_count"] = search_width
        profile = self.materialize_validation_profile(
            task_id,
            {role: str(item["path"]) for role, item in models.items()},
            options=effective_profile_options,
        )
        return RsiTaskMaterialization(dataset, harness, models, profile)

    def _source_path(
        self,
        raw_path: str | Path,
        allowed_root: Path | None,
        *,
        label: str,
    ) -> Path:
        raw = str(raw_path or "").strip()
        if not raw:
            raise RsiPathInvalid(f"{label}路径不能为空")
        path = Path(raw).expanduser().resolve()
        if allowed_root is not None:
            _ensure_within(path, allowed_root, label=label)
        if not path.exists():
            raise RsiPathInvalid(f"{label}路径不存在: {path}")
        return path

    def _source_file(
        self,
        raw_path: str | Path,
        allowed_root: Path | None,
        *,
        label: str,
    ) -> Path:
        path = self._source_path(raw_path, allowed_root, label=label)
        if not path.is_file():
            raise RsiPathInvalid(f"{label}必须是文件: {path}")
        return path


def _optional_root(value: str | Path | None) -> Path | None:
    if value is None or not str(value).strip():
        return None
    return Path(value).expanduser().resolve()


def _ensure_within(path: Path, root: Path, *, label: str) -> None:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise RsiPathNotAllowed(f"{label}路径超出允许根目录: {path}") from exc


def _safe_filename(name: str, *, default: str) -> str:
    candidate = Path(str(name or "")).name.strip()
    if not candidate or candidate in {".", ".."}:
        return default
    return candidate


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _resource_items(payload: dict[str, Any], key: str) -> list[Any]:
    resources = payload.get("resources")
    containers = (payload, resources if isinstance(resources, dict) else {})
    items: list[Any] = []
    for container in containers:
        items.extend(_as_list(container.get(key)))
    return items


def _referenced_package_paths(payload: Any, package_root: Path) -> list[Path]:
    items: list[Any]
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = []
        for key in ("tools", "rails", "prompt_sections", "skills", "mcps"):
            items.extend(_resource_items(payload, key))
        for nested_key in ("servers", "items"):
            nested = payload.get(nested_key)
            if isinstance(nested, dict):
                items.extend(nested.values())
            else:
                items.extend(_as_list(nested))
    else:
        return []
    references: list[Path] = []

    def _add(raw_path: Any) -> None:
        if not isinstance(raw_path, str) or not raw_path.strip():
            return
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = package_root / candidate
        try:
            resolved = candidate.resolve(strict=False)
            resolved.relative_to(package_root.resolve())
        except (OSError, ValueError):
            return
        if resolved.exists():
            references.append(resolved)

    for item in items:
        if not isinstance(item, dict):
            continue
        for field in ("file", "file_path", "dir", "cwd"):
            _add(item.get(field))
        for nested in (item.get("params"), item.get("kwargs")):
            if isinstance(nested, dict):
                for field in ("file", "file_path", "dir", "cwd"):
                    _add(nested.get(field))
        for directory in _as_list(item.get("dirs")):
            _add(directory)
    return references


def _copy_package_path(
    source_path: Path, package_root: Path, target_root: Path
) -> None:
    try:
        resolved = source_path.resolve()
        if resolved == package_root.resolve():
            return
        relative = resolved.relative_to(package_root.resolve())
    except ValueError as exc:
        raise RsiInvalidHarness(f"Harness 资源路径超出包目录: {source_path}") from exc
    target = target_root / relative
    if source_path.is_dir():
        shutil.copytree(source_path, target, dirs_exist_ok=True)
    elif source_path.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target)


def _materialize_file_harness(
    source: Path,
    source_data: dict[str, Any],
    target_dir: Path,
) -> Path:
    package_root = source.parent.resolve()
    package = target_dir / _safe_filename(package_root.name, default="package")
    package.mkdir(parents=True, exist_ok=True)
    manifest_name = (
        source.name if source.name in _MANIFEST_NAMES else "harness_config.yaml"
    )
    shutil.copy2(source, package / manifest_name)

    if manifest_name in _SIDECAR_MANIFEST_NAMES:
        for directory in _SIDECAR_DIRECTORIES:
            candidate = package_root / directory
            if candidate.exists():
                _copy_package_path(candidate, package_root, package)

    payloads: list[Any] = [source_data]
    for directory, filename in _SIDECAR_LIST_FILES:
        list_path = package_root / directory / filename
        if not list_path.is_file():
            continue
        try:
            loaded = (
                json.loads(list_path.read_text(encoding="utf-8"))
                if list_path.suffix.lower() == ".json"
                else yaml.safe_load(list_path.read_text(encoding="utf-8"))
            )
        except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
            raise RsiInvalidHarness(f"Harness sidecar 配置不可读: {list_path}") from exc
        payloads.append(loaded)

    for payload in payloads:
        for reference in _referenced_package_paths(payload, package_root):
            _copy_package_path(reference, package_root, package)

    _validate_materialized_harness(package / manifest_name, package)
    return package


def _validate_materialized_harness(manifest: Path, package: Path) -> None:
    try:
        from openjiuwen.harness.resources import load_plugin_package
    except ImportError:
        return
    try:
        load_plugin_package(manifest)
    except Exception as exc:  # noqa: BLE001 - normalize SDK parser errors
        raise RsiInvalidHarness(f"任务私有 Harness 包校验失败: {manifest}") from exc
    if manifest.name == "manifest.json":
        return
    try:
        from jiuwenswarm.agents.harness.common.auto_harness.service import (
            validate_harness_config,
        )

        validate_harness_config(manifest, package_dir=package)
    except RsiInvalidHarness:
        raise
    except Exception as exc:  # noqa: BLE001 - normalize security errors
        raise RsiInvalidHarness(
            f"任务私有 Harness 配置安全校验失败: {manifest}"
        ) from exc


def _harness_config_path(source: Path) -> Path:
    """Resolve the manifest used to validate a Harness package directory."""

    if source.is_file():
        return source
    for name in _MANIFEST_NAMES:
        candidate = source / name
        if candidate.is_file():
            return candidate
    raise RsiInvalidHarness(f"Harness 包缺少配置文件: {source}")


def _load_evobench_suite(source: Path, *, domain: str | None) -> dict[str, Any] | None:
    """Convert an Evo-Bench suite into the case file consumed by openjiuwen.

    The standalone Evo-Bench launcher performs this conversion before calling
    the RSI orchestrator.  AgentServer receives the suite through the direct
    API instead, so the same narrow conversion belongs at the materialization
    boundary.  Ordinary ``{"cases": [...]}`` inputs return ``None`` and keep
    their original bytes.
    """

    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RsiDatasetInvalid(f"数据集 JSON 解析失败: {exc}") from exc
    if not isinstance(raw, dict) or "validation" not in raw:
        return None
    tasks = raw.get("validation")
    if not isinstance(tasks, list) or not tasks:
        raise RsiDatasetInvalid("Evo-Bench suite 必须包含非空 validation 数组")

    selected_domain = str(domain or "").strip().lower()
    if selected_domain and selected_domain not in {"general", "office"}:
        raise RsiDatasetInvalid(f"Evo-Bench domain 不支持: {domain}")

    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, task in enumerate(tasks, start=1):
        if not isinstance(task, dict):
            raise RsiDatasetInvalid(f"Evo-Bench validation task 必须是 mapping: #{index}")
        case_id = str(task.get("id") or "").strip()
        task_domain = str(task.get("domain") or "").strip().lower()
        if selected_domain and task_domain != selected_domain:
            continue
        if not case_id:
            raise RsiDatasetInvalid(f"Evo-Bench validation task 缺少 id: #{index}")
        if case_id in seen:
            raise RsiDatasetInvalid(f"数据集 case_id 重复: {case_id}")
        if task_domain not in {"general", "office"}:
            raise RsiDatasetInvalid(f"Evo-Bench task domain 不支持: {case_id}")
        seen.add(case_id)
        metadata = task.get("metadata")
        task_type = metadata.get("task_type") if isinstance(metadata, dict) else None
        cases.append(
            {
                **task,
                "case_id": case_id,
                "task_id": case_id,
                "input": str(task.get("prompt") or ""),
                "domain": task_domain,
                "source": case_id.split("-", 1)[0],
                "task_type": str(task_type or task_domain),
            }
        )
    if not cases:
        suffix = f" domain={selected_domain}" if selected_domain else ""
        raise RsiDatasetInvalid(f"Evo-Bench suite 没有匹配的 validation task{suffix}")
    return {
        "dataset_id": f"evobench_validation_{len(cases)}",
        "cases": cases,
    }


def _resolve_harness_source(source: Path, allowed_root: Path | None, *, role: str) -> Path:
    """Resolve a refs-wrapper file to the package directory it names."""

    if not source.is_file():
        return source
    try:
        data = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise RsiInvalidHarness(f"Harness refs 不可读: {source}") from exc
    if not isinstance(data, dict):
        return source
    refs = data.get("harness_refs")
    if refs is None and any(
        key in data for key in ("schema_version", "id", "name", "description", "tools", "rails", "skills")
    ):
        # A legacy ``harness_config.yaml`` is itself a valid package manifest,
        # not a top-level ``role: path`` refs wrapper.
        return source
    if isinstance(refs, dict):
        candidates = {
            str(key): str(value).strip()
            for key, value in refs.items()
            if str(value or "").strip()
        }
    else:
        candidates = {
            str(key): str(value).strip()
            for key, value in data.items()
            if isinstance(value, str)
            and str(key) not in {"version", "source_harness_refs_path"}
            and str(value).strip()
        }
    if not candidates:
        return source
    raw_ref = candidates.get(str(role or "").strip())
    if raw_ref is None and len(candidates) == 1:
        raw_ref = next(iter(candidates.values()))
    if raw_ref is None:
        raise RsiInvalidHarness(
            f"Harness refs 必须只包含一个可用 role，或包含 {role}: {source}"
        )
    target = Path(raw_ref).expanduser()
    if not target.is_absolute():
        target = source.parent / target
    target = target.resolve()
    if allowed_root is not None:
        _ensure_within(target, allowed_root, label="Harness ref")
    if not target.exists():
        raise RsiInvalidHarness(f"Harness ref 目标不存在: {target}")
    return target


def _harness_ref_target(source: Path) -> Path:
    """Return the path the engine should load for a Harness source."""

    return source.resolve()


def _copy_harness_source(source: Path, target: Path) -> None:
    """Copy a validated baseline package/file into the task directory."""

    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        if target.exists():
            if not target.is_dir():
                raise RsiInvalidHarness(f"Harness task-local 版本路径冲突: {target}")
            return
        shutil.copytree(source, target, symlinks=False)
        return
    if target.exists():
        if not target.is_file():
            raise RsiInvalidHarness(f"Harness task-local 配置路径冲突: {target}")
        return
    shutil.copy2(source, target)


def _profile_options(options: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = options or {}
    values = {
        "max_epochs": _profile_int(raw, "max_epochs", default=1, minimum=1),
        "batch_size": _profile_int(raw, "batch_size", default=1, minimum=1),
        "max_issue_attempts": _profile_int(raw, "max_issue_attempts", default=8, minimum=0),
        "max_repair_rounds": _profile_int(raw, "max_repair_rounds", default=3, minimum=1),
        "sibling_candidate_count": _profile_int(
            raw, "sibling_candidate_count", default=1, minimum=1
        ),
        "rollout_concurrency": _profile_int(raw, "rollout_concurrency", default=1, minimum=1),
        "improver_policy_ref": str(raw.get("improver_policy_ref") or "").strip(),
    }
    if values["sibling_candidate_count"] != 1 or values["improver_policy_ref"]:
        raise RsiPathInvalid(
            "single-harness optimization requires one candidate and no improver evolution policy"
        )
    runtime: dict[str, Any] = {}
    for key in ("domain", "execution_mode"):
        value = str(raw.get(key) or "").strip()
        if value:
            runtime[key] = value
    if "rollout_concurrency" in raw:
        runtime["rollout_concurrency"] = values["rollout_concurrency"]
    values["runtime"] = runtime
    return values


def _profile_int(raw: Mapping[str, Any], name: str, *, default: int, minimum: int) -> int:
    value = raw.get(name)
    if value is None:
        return default
    if isinstance(value, bool):
        raise RsiPathInvalid(f"Validation profile 参数 {name} 必须是整数")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RsiPathInvalid(f"Validation profile 参数 {name} 必须是整数") from exc
    if parsed < minimum:
        raise RsiPathInvalid(f"Validation profile 参数 {name} 小于允许下限 {minimum}")
    return parsed


def _path_sha256(path: Path) -> str:
    """Hash a file or a Harness package directory deterministically."""

    if path.is_file():
        return _sha256(path)
    digest = hashlib.sha256()
    for item in sorted(
        (candidate for candidate in path.rglob("*") if candidate.is_file()),
        key=lambda p: p.relative_to(path).as_posix(),
    ):
        relative = item.relative_to(path).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(bytes.fromhex(_sha256(item)))
    return digest.hexdigest()


def _validate_json_dataset(path: Path) -> None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RsiDatasetInvalid(f"数据集 JSON 解析失败: {exc}") from exc
    cases = raw.get("cases") if isinstance(raw, dict) else raw
    if not isinstance(cases, list) or not cases:
        raise RsiDatasetInvalid("数据集必须包含非空 cases 数组")
    seen: set[str] = set()
    for item in cases:
        if not isinstance(item, dict):
            raise RsiDatasetInvalid("数据集 case 必须是 mapping")
        case_id = str(item.get("case_id") or "").strip()
        if not case_id:
            raise RsiDatasetInvalid("数据集 case_id 不能为空")
        if case_id in seen:
            raise RsiDatasetInvalid(f"数据集 case_id 重复: {case_id}")
        seen.add(case_id)


def _validation_ok(result: Any) -> bool:
    return (
        bool(result.get("valid"))
        if isinstance(result, dict)
        else bool(getattr(result, "valid", False))
    )


def _validation_errors(result: Any) -> list[dict[str, str]]:
    raw = (
        result.get("errors")
        if isinstance(result, dict)
        else getattr(result, "errors", None)
    )
    errors: list[dict[str, str]] = []
    for item in raw or []:
        if isinstance(item, dict):
            errors.append(
                {
                    "code": str(item.get("code") or "DATASET_INVALID"),
                    "reason": str(
                        item.get("reason") or item.get("message") or "数据集校验失败"
                    ),
                }
            )
        else:
            errors.append({"code": "DATASET_INVALID", "reason": str(item)})
    return errors


__all__ = [
    "RsiTaskMaterialization",
    "RsiTaskMaterializer",
    "VALIDATION_PROFILE_NAME",
]
