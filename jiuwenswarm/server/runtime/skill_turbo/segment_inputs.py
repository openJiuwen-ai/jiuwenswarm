"""阶段输入装配与结果验证；文件操作有界offload，不执行模型或工具。"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry
from referencing.exceptions import Unresolvable

from jiuwenswarm.server.runtime.skill_turbo.accelerator_manifest import _RESERVED

_CONTEXT_KEYS = _RESERVED | {"task_key", "skill", "card_id", "capabilities"}
_file_io = asyncio.Semaphore(4)


class SegmentInputError(ValueError):
    def __init__(self, status: str, reason: str, warnings: list[dict] | None = None):
        self.status = status
        self.reason = reason
        self.warnings = warnings or []
        super().__init__(reason)


def _ledger_data(ledger) -> dict:
    return ledger.data if hasattr(ledger, "data") else ledger


def _path_key(key: str) -> bool:
    return key.endswith(("_path", "_dir", "_file", "_files"))


def _paths(ctx: dict, ledger: dict) -> tuple[Path, tuple[Path, ...], dict[str, Path]]:
    output_dir = ctx.get("metadata", {}).get("output_dir")
    if not output_dir or not Path(output_dir).is_absolute():
        raise SegmentInputError("blocked", "workspace_missing")
    workspace = Path(output_dir).resolve()
    if str(workspace) != ledger.get("workspace_base"):
        raise SegmentInputError("blocked", "workspace_mismatch")
    run_dir = Path(ledger["run_dir"]).resolve()
    if not run_dir.is_relative_to(workspace):
        raise SegmentInputError("blocked", "run_dir_outside_workspace")
    bases = {"run_dir": run_dir, "workspace_base": workspace}
    if ctx.get("skill_root"):
        bases["skill_root"] = Path(ctx["skill_root"]).resolve()
    return run_dir, tuple(bases.values()), bases


def _resolve_path(value: Any, run_dir: Path, roots: tuple[Path, ...]) -> str:
    if not isinstance(value, str) or not value:
        raise SegmentInputError("invalid_args", "path_invalid")
    path = Path(value)
    resolved = (path if path.is_absolute() else run_dir / path).resolve()
    if not any(resolved.is_relative_to(root) for root in roots):
        raise SegmentInputError("blocked", "path_outside_allowed_roots")
    return str(resolved)


def _path_value(key: str, value: Any, run_dir: Path, roots: tuple[Path, ...]) -> Any:
    if key.endswith("_files"):
        if not isinstance(value, list):
            raise SegmentInputError("invalid_args", "path_list_invalid")
        return [_resolve_path(v, run_dir, roots) for v in value]
    return _resolve_path(value, run_dir, roots)


def _file_record(path: Path) -> dict:
    stat = path.stat()
    digest = ""
    if path.is_file():
        hasher = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(65536), b""):
                hasher.update(block)
        digest = hasher.hexdigest()
    return {"path": str(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sha256": digest}


def _glob_records(condition, bases: dict[str, Path], roots: tuple[Path, ...]) -> list[dict]:
    base = bases.get(condition.base)
    if base is None:
        return []
    records = []
    for candidate in sorted(base.glob(condition.path_glob)):
        path = candidate.resolve()
        if not any(path.is_relative_to(root) for root in roots):
            raise SegmentInputError("blocked", "artifact_outside_allowed_roots")
        if path.exists():
            records.append(_file_record(path))
    return records


def _small_json(value: Any) -> bool:
    try:
        return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")) <= 2048
    except (TypeError, ValueError):
        return False


def _conflict_value(key: str, value: Any) -> Any:
    if any(word in key.lower() for word in ("secret", "token", "password", "credential", "api_key")):
        return "<redacted>"
    if isinstance(value, (dict, list)):
        return f"<{type(value).__name__}>"
    text = str(value)
    return text[:160] if len(text) > 160 else value


def _assemble(ctx, spec, params, ledger, upstream_facts):
    data = _ledger_data(ledger)
    warnings = []
    run_dir, roots, bases = _paths(ctx, data)
    params = {} if params is None else params
    if not isinstance(params, dict):
        raise SegmentInputError("invalid_args", "params_must_be_object")
    properties = spec.input_schema.get("properties", {})
    if set(params) - set(properties) or _CONTEXT_KEYS.intersection(params):
        raise SegmentInputError("invalid_args", "unknown_or_reserved_params")
    inputs = copy.deepcopy(ctx.get("inputs", {}))
    inputs.update({key: copy.deepcopy(value) for key, value in ctx.items()
                   if key not in {"metadata", "inputs", "initial_inputs"}})
    inputs.update(workspace_base=data["workspace_base"], run_dir=data["run_dir"], task_key=data["task_key"])
    original_facts = copy.deepcopy(data.get("facts", {}))
    provenance = copy.deepcopy(data.get("facts_provenance", {}))
    sources = {}
    upstream = upstream_facts or {}
    known_facts = {**original_facts, **upstream.get("facts", {})}
    for label, facts in (("ledger", original_facts), ("upstream", upstream.get("facts", {})),
                         ("defaults", spec.defaults), ("explicit", params)):
        for key, value in facts.items():
            if key in _CONTEXT_KEYS or (label == "explicit" and value is None):
                continue
            if label == "explicit" and key in known_facts and value != known_facts[key]:
                warnings.append({"code": "fact_conflict", "key": key,
                                 "ledger_value": _conflict_value(key, known_facts[key]),
                                 "explicit_value": _conflict_value(key, value)})
            if label in {"ledger", "upstream"} and not _small_json(value):
                warnings.append({"code": "upstream_fact_invalid", "key": key})
                provenance[key] = {"verified": False}
                continue
            inputs[key] = copy.deepcopy(value)
            sources[key] = label
            if label == "upstream":
                provenance[key] = copy.deepcopy(upstream.get("facts_provenance", {}).get(key, {}))
    # 单字段 required 的 oneOf 表示互斥来源，必须在路径与前提检查前选源。
    branches = spec.input_schema.get("oneOf", [])
    if branches and all(set(branch) == {"required"} and len(branch["required"]) == 1 for branch in branches):
        choices = [branch["required"][0] for branch in branches]
        explicit = [key for key in choices if key in params]
        if len(explicit) > 1:
            raise SegmentInputError("invalid_args", "exclusive_sources_ambiguous", warnings)
        selected = explicit[0] if explicit else choices[0]
        for key in choices:
            if key != selected:
                inputs.pop(key, None)
                sources.pop(key, None)
        if explicit:
            inputs[selected] = copy.deepcopy(params[selected])
            sources[selected] = "explicit"
        elif (selected not in inputs or inputs[selected] is None
                or sources.get(selected) not in {"ledger", "upstream"}
                or not provenance.get(selected, {}).get("verified")):
            raise SegmentInputError("blocked", "exclusive_source_missing_or_unverified", warnings)
    for key, value in list(inputs.items()):
        if _path_key(key) and key not in _CONTEXT_KEYS and value is not None:
            inputs[key] = _path_value(key, value, run_dir, roots)
    return inputs, _validate_preconditions(ctx, spec, inputs, data, sources, provenance, upstream, warnings)


def _validate_preconditions(ctx, spec, inputs, data, sources, provenance, upstream, warnings):
    run_dir, roots, bases = _paths(ctx, data)
    original_facts = copy.deepcopy(data.get("facts", {}))
    properties = spec.input_schema.get("properties", {})
    recorded = {artifact["path"]: artifact for artifact in data.get("artifacts", [])}
    missing_facts = set()
    for key, value in {**original_facts, **upstream.get("facts", {})}.items():
        if key in properties and key not in inputs:
            continue
        if not _path_key(key) or key in _CONTEXT_KEYS:
            continue
        normalized = _path_value(key, value, run_dir, roots)
        for filename in normalized if isinstance(normalized, list) else [normalized]:
            path = Path(filename)
            if not path.exists():
                provenance.setdefault(key, {})["verified"] = False
                if key not in upstream.get("facts", {}):
                    data.setdefault("facts_provenance", {}).setdefault(key, {})["verified"] = False
                missing_facts.add(key)
                warnings.append({"code": "artifact_missing", "key": key, "path": filename})
            elif filename in recorded:
                current = _file_record(path)
                if any(current[field] != recorded[filename].get(field) for field in ("size", "mtime_ns", "sha256")):
                    warnings.append({"code": "artifact_modified", "key": key, "path": filename})
    for requirement in spec.requires:
        reason = ""
        if requirement.kind == "argument":
            if requirement.key not in inputs or inputs[requirement.key] is None:
                reason = "argument_missing"
        elif requirement.kind == "capability":
            if requirement.key not in ctx.get("capabilities", ()):
                reason = "capability_missing"
        elif requirement.kind == "workspace_file":
            if not _glob_records(requirement, bases, roots):
                reason = "artifact_missing"
        elif requirement.kind == "ledger_fact":
            key = requirement.key
            source = provenance.get(key, {})
            auto_value = (upstream.get("facts", {}).get(key) if key in upstream.get("facts", {})
                          else original_facts.get(key))
            if _path_key(key) and auto_value is not None:
                auto_value = _path_value(key, auto_value, run_dir, roots)
            if key not in inputs:
                reason = "ledger_fact_missing"
            elif sources.get(key) in {"explicit", "defaults"} and inputs[key] != auto_value:
                reason = "fact_provenance_mismatch"
            elif requirement.producer and source.get("produced_by") != requirement.producer:
                reason = "fact_provenance_mismatch"
            elif key in missing_facts:
                reason = "artifact_missing"
            elif not source.get("verified"):
                reason = "upstream_unverified"
        if reason:
            issue = {"code": reason, "key": requirement.key, "producer": requirement.producer}
            if requirement.level == "hard":
                raise SegmentInputError("blocked", reason, warnings + [issue])
            warnings.append(issue)
    projected = {key: inputs[key] for key in properties if key in inputs}
    try:
        errors = list(Draft202012Validator(spec.input_schema, registry=Registry()).iter_errors(projected))
    except Unresolvable as exc:
        raise SegmentInputError("invalid_args", "schema_reference_invalid", warnings) from exc
    if errors:
        raise SegmentInputError("invalid_args", "schema_invalid", warnings + [
            {"code": "schema_invalid", "path": list(error.path), "rule": error.validator} for error in errors
        ])
    return warnings


async def assemble_segment_inputs(ctx, spec, params, ledger, upstream_facts=None):
    async with _file_io:
        return await asyncio.to_thread(_assemble, ctx, spec, params, ledger, upstream_facts)


def _validate_replay(ctx, spec, inputs, ledger):
    data = _ledger_data(ledger)
    if any(inputs.get(key) != data.get(key) for key in ("workspace_base", "run_dir", "task_key")):
        raise SegmentInputError("failed", "resume_input_identity_mismatch")
    run_dir, roots, _ = _paths(ctx, data)
    for key, value in inputs.items():
        if _path_key(key) and key not in _CONTEXT_KEYS and value is not None:
            if _path_value(key, value, run_dir, roots) != value:
                raise SegmentInputError("failed", "resume_input_path_changed")
    return _validate_preconditions(ctx, spec, inputs, data, {key: "explicit" for key in inputs},
                                   copy.deepcopy(data.get("facts_provenance", {})), {}, [])


async def validate_segment_replay_inputs(ctx, spec, inputs, ledger):
    """原inputs只做当前文件/来源/前提校验，不重新装配或覆盖断点值。"""
    async with _file_io:
        return await asyncio.to_thread(_validate_replay, ctx, spec, inputs, ledger)


def _finalize(ctx, spec, root_output, inputs, handoff, initial_inputs):
    # final输入中的旧值不能冒充本次产出；执行方必须保留装配时的初始快照。
    data = {"workspace_base": inputs.get("workspace_base"), "run_dir": inputs.get("run_dir")}
    run_dir, roots, bases = _paths(ctx, data)
    root_output = root_output if isinstance(root_output, dict) else {"status": "failed"}
    handoff = handoff if handoff is not None else root_output.get("handoff", {})
    status = root_output.get("status", "ok")
    if status not in {"ok", "partial", "failed", "blocked", "not_applicable", "paused", "cancelled"}:
        status = "failed"
    if root_output.get("success") is False and status in {"ok", "partial"}:
        status = "failed"
    warnings = []
    invalid_fact = False
    if not isinstance(handoff, dict):
        handoff = {}
        invalid_fact = True
        warnings.append({"code": "handoff_invalid"})
    artifacts = {}
    missing_artifact = False
    for artifact in spec.outputs.artifacts:
        try:
            matches = _glob_records(artifact, bases, roots)
        except (OSError, SegmentInputError) as exc:
            matches = []
            warnings.append({"code": "artifact_invalid", "reason": str(exc)})
        if not matches:
            missing_artifact = True
            warnings.append({"code": "artifact_missing", "path_glob": artifact.path_glob})
        artifacts.update({record["path"]: record for record in matches})
    handoff_artifacts = handoff.get("artifacts", [])
    if not isinstance(handoff_artifacts, list):
        handoff_artifacts = []
        invalid_fact = True
        warnings.append({"code": "handoff_artifacts_must_be_array"})
    for record in handoff_artifacts:
        filename = record.get("path") if isinstance(record, dict) else record
        try:
            normalized = _resolve_path(filename, run_dir, roots)
            if normalized not in artifacts:
                warnings.append({"code": "handoff_artifact_undeclared", "path": normalized})
        except SegmentInputError:
            warnings.append({"code": "handoff_artifact_invalid"})
    candidates = {}
    for key, value in inputs.items():
        if key not in spec.outputs.facts:
            continue
        if key not in initial_inputs or initial_inputs[key] != value:
            candidates[key] = value
    root_facts = root_output.get("facts", {key: value for key, value in root_output.items()
                                          if key not in {"status", "success", "error", "handoff", "__artifact__"}})
    for source in (root_facts, handoff.get("facts", {})):
        if isinstance(source, dict):
            candidates.update(source)
        else:
            invalid_fact = True
            warnings.append({"code": "facts_must_be_object"})
    facts = {}
    provenance = {key: {"produced_by": spec.id, "verified": False} for key in spec.outputs.facts}
    for key, value in candidates.items():
        if key not in spec.outputs.facts or key in _CONTEXT_KEYS:
            warnings.append({"code": "fact_undeclared", "key": key})
            continue
        try:
            if not _small_json(value):
                raise SegmentInputError("invalid_args", "fact_not_small_json")
            if _path_key(key):
                value = _path_value(key, value, run_dir, roots)
                for filename in value if isinstance(value, list) else [value]:
                    path = Path(filename)
                    if not path.exists() or (str(path) not in artifacts and not (
                            path.is_dir() and any(Path(a).is_relative_to(path) for a in artifacts))):
                        raise SegmentInputError("blocked", "fact_artifact_missing_or_undeclared")
            if key in spec.input_schema.get("properties", {}):
                validator = Draft202012Validator(spec.input_schema, registry=Registry()).evolve(
                    schema={"$ref": "#/properties/" + key.replace("~", "~0").replace("/", "~1")})
                if not validator.is_valid(value):
                    raise SegmentInputError("invalid_args", "fact_type_invalid")
            if not _small_json(value):
                raise SegmentInputError("invalid_args", "fact_not_small_json")
            facts[key] = value
        except (SegmentInputError, OSError, Unresolvable) as exc:
            invalid_fact = True
            warnings.append({"code": "fact_invalid", "key": key, "reason": str(exc)})
    failed_postcondition = False
    for condition in spec.outputs.postconditions:
        if condition.kind == "fact":
            passed = condition.key in facts
            if passed and status in {"ok", "partial"}:
                provenance[condition.key]["verified"] = True
        else:
            try:
                post_records = _glob_records(condition, bases, roots)
                passed = bool(post_records)
            except (OSError, SegmentInputError):
                passed = False
            if passed and status in {"ok", "partial"}:
                for key, value in facts.items():
                    if _path_key(key):
                        filenames = value if isinstance(value, list) else [value]
                        matched = {record["path"] for record in post_records}
                        if set(filenames).issubset(matched):
                            provenance[key]["verified"] = True
        if not passed:
            failed_postcondition = True
            warnings.append({"code": "postcondition_failed", "key": condition.key, "path_glob": condition.path_glob})
    if status == "ok":
        if missing_artifact or failed_postcondition or invalid_fact:
            status = "partial"
    return {"status": status, "facts": facts, "facts_provenance": provenance,
            "artifacts": list(artifacts.values()), "warnings": warnings}


async def finalize_segment_result(ctx, spec, root_output, inputs, handoff=None, *, initial_inputs):
    async with _file_io:
        return await asyncio.to_thread(_finalize, ctx, spec, root_output, inputs, handoff, initial_inputs)


def _validate_batch_inputs(ctx, spec, params, data, future, earlier_artifacts, shared_params):
    from fnmatch import fnmatchcase
    from jsonschema import validators
    from jsonschema.exceptions import ValidationError

    properties = spec.input_schema.get('properties', {})
    if set(params) - set(properties) or _CONTEXT_KEYS.intersection(params):
        raise SegmentInputError('invalid_args', 'unknown_or_reserved_params')
    values = {key: copy.deepcopy(value) for key, value in data.get('facts', {}).items() if key in properties}
    values.update(copy.deepcopy(spec.defaults))
    values.update({key: copy.deepcopy(value) for key, value in params.items() if value is not None})
    branches = spec.input_schema.get('oneOf', [])
    if branches and all(set(branch) == {'required'} and len(branch['required']) == 1 for branch in branches):
        choices = [branch['required'][0] for branch in branches]
        explicit = [key for key in choices if key in params]
        if len(explicit) > 1:
            raise SegmentInputError('invalid_args', 'exclusive_sources_ambiguous')
        selected = explicit[0] if explicit else choices[0]
        for key in choices:
            if key != selected:
                values.pop(key, None)
        if explicit:
            values[selected] = copy.deepcopy(params[selected])
    run_dir, roots, bases = _paths(ctx, data)
    for key, value in values.items():
        if _path_key(key) and value is not None:
            _path_value(key, value, run_dir, roots)
    warnings = []
    for key, value in (shared_params or {}).items():
        if value is None:
            continue
        field = '#/properties/' + key.replace('~', '~0').replace('/', '~1')
        try:
            validator = Draft202012Validator(spec.input_schema, registry=Registry()).evolve(schema={'$ref': field})
            valid = validator.is_valid(value)
        except Unresolvable as exc:
            raise SegmentInputError('invalid_args', 'schema_reference_invalid') from exc
        if not valid:
            raise SegmentInputError('invalid_args', 'shared_param_schema_invalid')
        if _path_key(key):
            _path_value(key, value, run_dir, roots)
    pending = set(future) - set(values)
    for requirement in spec.requires:
        key = requirement.key
        reason = ''
        if requirement.kind == 'ledger_fact':
            provenance = data.get('facts_provenance', {}).get(key, {})
            reachable = key in future and (not requirement.producer or future[key] == requirement.producer)
            current = key in data.get('facts', {}) and provenance.get('verified') and (
                not requirement.producer or provenance.get('produced_by') == requirement.producer)
            if current and _path_key(key):
                paths = _path_value(key, data['facts'][key], run_dir, roots)
                current = (
                    all(Path(path).exists() for path in paths) if isinstance(paths, list) else Path(paths).exists()
                )
            if not current and not reachable:
                reason = 'unreachable_ledger_fact'
            if current and not reachable:
                if key in params and params[key] is not None:
                    supplied = _path_value(key, params[key], run_dir, roots) if _path_key(key) else params[key]
                    expected = (
                        _path_value(key, data['facts'][key], run_dir, roots) if _path_key(key) else data['facts'][key]
                    )
                    if supplied != expected:
                        reason = 'fact_provenance_mismatch'
        elif requirement.kind == 'argument':
            if key not in values and key not in future:
                reason = 'argument_missing'
        elif requirement.kind == 'capability':
            if key not in ctx.get('capabilities', ()):
                reason = 'capability_missing'
        elif requirement.kind == 'workspace_file':
            reachable = any(item.base == requirement.base and (
                fnmatchcase(item.path_glob, requirement.path_glob)
                or fnmatchcase(requirement.path_glob, item.path_glob))
                for item in earlier_artifacts)
            if not reachable and not _glob_records(requirement, bases, roots):
                reason = 'artifact_missing'
        if reason:
            issue = {'code': reason, 'key': key, 'producer': requirement.producer}
            if requirement.level == 'hard':
                raise SegmentInputError('blocked', reason, warnings + [issue])
            warnings.append(issue)

    def required(validator, names, instance, schema):
        if not isinstance(instance, dict):
            return
        for name in names:
            if name not in instance and not (instance is values and name in pending):
                yield ValidationError(f'{name} is required')

    def one_of(validator, branches, instance, schema):
        if instance is values and pending:
            strict = Draft202012Validator(spec.input_schema, registry=Registry())
            if sum(strict.evolve(schema=branch).is_valid(instance) for branch in branches) > 1:
                yield ValidationError('oneOf already matches multiple branches')
                return
            yield from Draft202012Validator.VALIDATORS['anyOf'](validator, branches, instance, schema)
        else:
            yield from Draft202012Validator.VALIDATORS['oneOf'](validator, branches, instance, schema)

    validator_type = validators.extend(Draft202012Validator, {'required': required, 'oneOf': one_of})
    try:
        errors = list(validator_type(spec.input_schema, registry=Registry()).iter_errors(values))
    except Unresolvable as exc:
        raise SegmentInputError('invalid_args', 'schema_reference_invalid', warnings) from exc
    if errors:
        raise SegmentInputError('invalid_args', 'schema_invalid', warnings + [
            {'code': 'schema_invalid', 'rule': error.validator, 'path': list(error.path)} for error in errors])
    return warnings


async def validate_batch_inputs(ctx, spec, params, data, future, earlier_artifacts, shared_params=None):
    """只读批前检查；仅允许可达的前序声明解释尚未产生的必填值。"""
    async with _file_io:
        return await asyncio.to_thread(
            _validate_batch_inputs, ctx, spec, params, data, future, earlier_artifacts, shared_params,
        )
