"""阶段声明的纯数据校验；不导入业务入口，不检查任务产物。"""

from __future__ import annotations

import hashlib
import json
import keyword
import logging
import re
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Annotated, Any, Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError as SchemaValidationError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from referencing import Registry as SchemaRegistry
from referencing.exceptions import Unresolvable

logger = logging.getLogger(__name__)
_RESERVED = frozenset({
    "run_dir", "workspace_base", "skill_root", "skill_name", "request_id", "channel_id",
    "session_id", "user_id", "chat_id", "metadata", "permissions", "model_client", "python_cmd",
})
Text = Annotated[str, Field(min_length=1, pattern=r"\S")]


class DeclarationModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class Coverage(DeclarationModel):
    stages: list[Text] = Field(min_length=1)
    steps: list[Text] = Field(min_length=1)
    optional_stages: list[Text] = Field(default_factory=list)


class Condition(DeclarationModel):
    kind: Literal["argument", "ledger_fact", "workspace_file", "capability", "fact"]
    key: Text | None = None
    path_glob: Text | None = None
    base: Literal["run_dir", "workspace_base", "skill_root"] = "run_dir"
    level: Literal["hard", "soft"] = "hard"
    producer: Text | None = None

    @model_validator(mode="after")
    def check_condition(self):
        if self.kind == "workspace_file":
            if not self.path_glob or self.key is not None:
                raise ValueError("文件条件须使用path_glob而非key")
            _check_glob(self.path_glob)
        elif not self.key or self.path_glob is not None:
            raise ValueError("非文件条件须使用key而非path_glob")
        if self.producer is not None and self.kind != "ledger_fact":
            raise ValueError("producer仅适用于ledger_fact")
        return self


def _check_glob(value: str) -> None:
    path = PureWindowsPath(value)
    if path.is_absolute() or path.drive or value.startswith(("/", "\\")) or ".." in path.parts:
        raise ValueError("path_glob必须是无..的相对路径")


class Artifact(DeclarationModel):
    path_glob: Text
    kind: Text
    base: Literal["run_dir", "workspace_base", "skill_root"] = "run_dir"

    @field_validator("path_glob")
    @classmethod
    def check_path(cls, value):
        _check_glob(value)
        return value


class Outputs(DeclarationModel):
    artifacts: list[Artifact] = Field(min_length=1)
    facts: list[Text] = Field(default_factory=list)
    postconditions: list[Condition] = Field(min_length=1)

    @model_validator(mode="after")
    def check_facts(self):
        if len(set(self.facts)) != len(self.facts) or _RESERVED.intersection(self.facts):
            raise ValueError("事实键重复或含引擎保留键")
        for condition in self.postconditions:
            if condition.kind not in {"fact", "workspace_file"}:
                raise ValueError("后置条件仅支持fact/workspace_file")
            if condition.kind == "fact" and condition.key not in self.facts:
                raise ValueError("fact后置条件未在outputs.facts声明")
        return self


class Entry(DeclarationModel):
    module: Text
    attr: Literal["root"] = "root"

    @field_validator("module")
    @classmethod
    def check_module(cls, value):
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", value) or keyword.iskeyword(value):
            raise ValueError("entry.module须为安全Python模块stem")
        return value


class Handoff(DeclarationModel):
    next_steps: list[Text] = Field(default_factory=list)
    continue_hint: str = ""


class Retry(DeclarationModel):
    mode: Literal["resume", "restart", "reuse"] = "resume"
    max: int = Field(default=1, ge=0)


class Hitl(DeclarationModel):
    can_pause: bool = True
    kind: Literal["clarify", "review"] = "clarify"
    continue_after_resume: bool | None = None

    @model_validator(mode="after")
    def derive_continue(self):
        if self.continue_after_resume is None:
            object.__setattr__(self, "continue_after_resume", self.kind == "clarify")
        return self


class Batch(DeclarationModel):
    standalone: bool = True
    stop_after: Literal["review_checkpoint", "never"] = "review_checkpoint"
    auto_next: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,31}$")


class Cost(DeclarationModel):
    p50_seconds: float = Field(default=0, ge=0, allow_inf_nan=False)
    llm_calls: int = Field(default=0, ge=0)


def _check_schema_refs(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"$ref", "$dynamicRef", "$recursiveRef"} and (
                not isinstance(child, str) or not child.startswith("#")
            ):
                raise ValueError("禁止外部JSON Schema引用")
            _check_schema_refs(child)
    elif isinstance(value, list):
        for child in value:
            _check_schema_refs(child)


class AcceleratorSpec(DeclarationModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    order: int
    summary: str = Field(min_length=1, max_length=60)
    when_to_use: list[Text] = Field(min_length=1, max_length=3)
    when_not_to_use: list[Text] = Field(min_length=1, max_length=3)
    covers: Coverage
    input_schema: dict[str, Any]
    outputs: Outputs
    entry: Entry
    requires: list[Condition] = Field(default_factory=list)
    display_name: str = ""
    version: str = "1.0.0"
    defaults: dict[str, Any] = Field(default_factory=dict)
    handoff: Handoff = Field(default_factory=Handoff)
    retry: Retry = Field(default_factory=Retry)
    exclusive: bool = True
    hitl: Hitl = Field(default_factory=Hitl)
    batch: Batch = Field(default_factory=Batch)
    cost: Cost = Field(default_factory=Cost)
    enabled: bool = True
    plan_code: str = Field(default="", exclude=True)

    @model_validator(mode="after")
    def check_schema_and_defaults(self):
        schema = self.input_schema
        _check_schema_refs(schema)
        Draft202012Validator.check_schema(schema)
        if schema.get("type") != "object":
            raise ValueError("input_schema必须为object")
        properties = schema.get("properties", {})
        if not set(schema.get("required", [])).issubset(properties):
            raise ValueError("required键必须在properties声明")
        if not set(self.defaults).issubset(properties) or _RESERVED.intersection(self.defaults):
            raise ValueError("defaults含未声明或引擎保留字段")
        json.dumps(self.defaults, allow_nan=False)
        for key, value in self.defaults.items():
            Draft202012Validator(schema, registry=SchemaRegistry()).evolve(
                schema={"$ref": "#/properties/" + key.replace("~", "~0").replace("/", "~1")}
            ).validate(value)
        for req in self.requires:
            if req.kind == "fact":
                raise ValueError("requires不支持fact，请用ledger_fact")
        return self


@dataclass(frozen=True)
class ManifestRecord:
    accelerators: tuple[AcceleratorSpec, ...] = ()
    fingerprint: str = ""
    source: str = ""
    valid: bool = True
    errors: tuple[str, ...] = ()


def _parse_manifest(raw: Any, *, skill: str, skill_name: str, code_dir: Path | None = None) -> ManifestRecord:
    errors: list[str] = []
    if not isinstance(raw, dict):
        return ManifestRecord(valid=False, errors=("清单顶层必须为对象",))
    if (type(raw.get("manifest_version")) is not int or raw["manifest_version"] != 1
            or raw.get("skill", skill) != skill or raw.get("skill_name", skill_name) != skill_name
            or not isinstance(raw.get("accelerators"), list) or type(raw.get("enabled", True)) is not bool
            or set(raw) - {"manifest_version", "skill", "skill_name", "accelerators", "enabled"}):
        return ManifestRecord(valid=False, errors=("清单版本、身份或顶层字段错误",))
    try:
        fingerprint = hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    except (ValueError, TypeError) as exc:
        return ManifestRecord(valid=False, errors=(str(exc),))
    specs: list[AcceleratorSpec] = []
    # 唯一性先于逐项丢弃，避免坏项掩盖全局冲突并任意选出赢家。
    for key in ("id", "order", "entry"):
        values = []
        for item in raw["accelerators"]:
            if not isinstance(item, dict) or key not in item:
                continue
            value = item[key]
            if key == "entry" and isinstance(value, dict):
                value = (value.get("module"), value.get("attr", "root"))
            values.append(json.dumps(value, sort_keys=True))
        if len(values) != len(set(values)):
            return ManifestRecord(fingerprint=fingerprint, valid=False, errors=(f"重复{key}，整份拒绝",))
    from jiuwenswarm.server.runtime.skill_turbo.turbo_package_loader import dynamic_package_prefix

    for index, item in enumerate(raw["accelerators"]):
        try:
            if isinstance(item, dict) and "plan_code" in item:
                raise ValueError("plan_code由引擎生成")
            spec = AcceleratorSpec.model_validate(item)
            if code_dir is not None:
                entry = code_dir / (spec.entry.module + ".py")
                if not entry.resolve().is_relative_to(code_dir.resolve()) or not entry.is_file():
                    raise ValueError("入口文件不存在或越界")
            spec = spec.model_copy(update={"plan_code": f"from {dynamic_package_prefix(skill_name)}{skill_name}.{spec.entry.module} import root"})
            specs.append(spec)
        except (ValueError, TypeError, SchemaError, SchemaValidationError, ValidationError, Unresolvable) as exc:
            errors.append(f"条目{index}: {exc}")
    enabled = {s.id: s for s in specs if s.enabled}
    owners: dict[str, str] = {}
    coverage: dict[tuple[str, str], str] = {}
    fatal: list[str] = []
    for spec in enabled.values():
        if spec.batch.auto_next is not None:
            successor = enabled.get(spec.batch.auto_next)
            if (successor is None or successor.order <= spec.order
                    or not spec.batch.standalone or not successor.batch.standalone):
                fatal.append(f"{spec.id}: auto_next须指向同技能启用、顺序更大且非独占的段")
        for kind, values in (("stage", spec.covers.stages + spec.covers.optional_stages), ("step", spec.covers.steps)):
            for value in set(values):
                owner = coverage.setdefault((kind, value), spec.id)
                if owner != spec.id:
                    fatal.append(f"覆盖冲突 {value}: {owner}/{spec.id}")
        for fact in spec.outputs.facts:
            owner = owners.setdefault(fact, spec.id)
            if owner != spec.id:
                fatal.append(f"fact owner冲突 {fact}: {owner}/{spec.id}")
        for req in spec.requires:
            if req.producer:
                producer = enabled.get(req.producer)
                if producer is None or producer.order >= spec.order or req.key not in producer.outputs.facts:
                    fatal.append(f"{spec.id}: producer {req.producer}无效或未产出{req.key}")
    if fatal:
        return ManifestRecord(fingerprint=fingerprint, valid=False, errors=tuple(errors + fatal))
    return ManifestRecord(
        accelerators=tuple(sorted(specs, key=lambda s: s.order)) if raw.get("enabled", True) else (),
        fingerprint=fingerprint, errors=tuple(errors),
    )


def validate_accelerator_manifest(raw: dict, *, skill: str, skill_name: str) -> list[str]:
    return list(_parse_manifest(raw, skill=skill, skill_name=skill_name).errors)


def load_manifest_record(skill_dir: Path, *, skill_name: str, meta: dict[str, Any]) -> ManifestRecord:
    path = skill_dir / "turbo/accelerators.json"
    source = str(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if "accelerators" not in meta:
            return ManifestRecord()
        source = str(skill_dir / "turbo/meta.json")
        raw = {"manifest_version": meta.get("manifest_version", 1), "accelerators": meta["accelerators"]}
    except (ValueError, UnicodeError) as exc:
        logger.warning("清单非法 skill=%s file=%s: %s", skill_name, source, exc)
        return ManifestRecord(source=source, valid=False, errors=(str(exc),))
    try:
        record = _parse_manifest(raw, skill=str(meta.get("external_name") or skill_dir.name), skill_name=skill_name,
                                 code_dir=skill_dir / "turbo/turbo_codes" / skill_name)
    except (ValueError, TypeError) as exc:
        record = ManifestRecord(valid=False, errors=(str(exc),))
    for error in record.errors:
        logger.warning("清单校验 skill=%s file=%s: %s", skill_name, source, error)
    return ManifestRecord(record.accelerators, record.fingerprint, source, record.valid, record.errors)


def load_accelerator_manifest(skill_dir: Path, *, skill_name: str, meta: dict[str, Any]) -> tuple[AcceleratorSpec, ...]:
    return load_manifest_record(skill_dir, skill_name=skill_name, meta=meta).accelerators
