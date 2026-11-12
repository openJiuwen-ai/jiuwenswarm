# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Read and render JiuwenSwarm's localized GitCode Bug Report forms."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Annotated, Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field
import yaml

from jiuwenswarm.agents.harness.common.auto_harness.issue_fix.gitcode_issue_client import GitCodeIssueClient

_GITHUB_TEMPLATE_ROOT = "https://raw.githubusercontent.com/openJiuwen-ai/jiuwenswarm/develop"
_SECTION_KEYS = {
    "🐞 Detailed Description of the Problem": "description",
    "Detailed Environment Information Description": "environment",
    "Additional Information": "additional_information",
    "Version Information": "version",
    "🐞 问题详细描述": "description",
    "详细的环境信息描述": "environment",
    "其他辅助信息": "additional_information",
    "版本信息": "version",
}
_DESCRIPTION_FIELDS = ("description", "reproduction_steps", "expected_behavior", "actual_behavior")
_DESCRIPTION_HEADINGS = {
    "en": ("Reproduction steps", "Expected behavior", "Actual behavior"),
    "zh": ("复现步骤", "预期行为", "实际行为"),
}
_ReportText = Annotated[str, Field(min_length=1, max_length=8000)]


class BugReportFields(BaseModel):
    """Structured answers to the Bug Report form."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)

    description: _ReportText
    reproduction_steps: _ReportText
    expected_behavior: _ReportText
    actual_behavior: _ReportText
    environment: _ReportText
    version: _ReportText | None = None
    additional_information: _ReportText | None = None
    branch: str | None = None
    module: str | None = None
    checklist: list[str] = Field(
        default_factory=list,
        description="Exact labels from get_issue_template for checklist statements the user can confirm.",
    )


class BugReportInput(BaseModel):
    """Model-facing report_issue arguments."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)

    template_id: str = Field(pattern=r"^[a-f0-9]{64}$", description="ID returned by get_issue_template.")
    title: str = Field(min_length=1, max_length=200, pattern=r"^[^\r\n]+$", description="One-line bug title.")
    fields: BugReportFields


class _FormField(BaseModel):
    type: Literal["textarea", "checkboxes", "cascading-dropdown", "markdown"]
    id: str | None = None
    attributes: dict[str, Any]
    validations: dict[str, bool] = Field(default_factory=dict)


class _Form(BaseModel):
    title: str
    labels: tuple[str, ...] = ()
    body: list[_FormField]


class _ChecklistOption(BaseModel):
    label: str = Field(min_length=1)
    required: bool = False


class _BranchOption(BaseModel):
    value: str = Field(min_length=1)
    children: tuple[str, ...] = Field(min_length=1)


@dataclass(frozen=True)
class _Section:
    key: str
    heading: str
    required: bool


@dataclass(frozen=True)
class BugReportTemplate:
    """A fetched revision and its supported form fields."""

    id: str
    path: str
    source: Literal["gitcode", "github"]
    language: str
    title_prefix: str
    labels: tuple[str, ...]
    sections: tuple[_Section, ...]
    checklist: tuple[_ChecklistOption, ...]
    branch_modules: dict[str, tuple[str, ...]]

    def describe(self) -> dict[str, Any]:
        required, optional = [], []
        for section in self.sections:
            if section.key == "description":
                keys = _DESCRIPTION_FIELDS
            elif section.key == "module_select":
                keys = ("branch", "module")
            else:
                keys = (section.key,)
            is_required = section.required or section.key in {"description", "environment"}
            if section.key == "checklist":
                is_required = is_required or any(option.required for option in self.checklist)
            (required if is_required else optional).extend(keys)
        return {
            "status": "ok",
            "template_id": self.id,
            "template_path": self.path,
            "template_source": self.source,
            "title_prefix": self.title_prefix,
            "labels": list(self.labels),
            "required_fields": required,
            "optional_fields": optional,
            "checklist_options": [option.model_dump() for option in self.checklist],
            "branch_modules": self.branch_modules,
        }

    def render(self, report: BugReportInput) -> tuple[str, str]:
        fields = report.fields
        title = report.title.removeprefix(self.title_prefix).strip()
        if not title:
            raise ValueError("Supply a descriptive title after the template prefix.")

        checked = set(fields.checklist)
        choices = {option.label for option in self.checklist}
        if checked - choices:
            raise ValueError("Use checklist option labels returned by get_issue_template.")
        if any(option.required and option.label not in checked for option in self.checklist):
            raise ValueError("Confirm the required checklist items before submitting.")
        if fields.branch or fields.module:
            if fields.module not in self.branch_modules.get(fields.branch or "", ()):
                raise ValueError("Use a branch and module pair returned by get_issue_template.")

        description = [fields.description]
        for key, heading in zip(_DESCRIPTION_FIELDS[1:], _DESCRIPTION_HEADINGS[self.language], strict=True):
            description.append(f"#### {heading}\n\n{getattr(fields, key)}")
        values = {
            "description": "\n\n".join(description),
            "environment": fields.environment,
            "version": fields.version,
            "additional_information": fields.additional_information,
            "checklist": "\n".join(
                f"- [{'x' if option.label in checked else ' '}] {option.label}" for option in self.checklist
            ) if checked else "",
            "module_select": f"{fields.branch}/{fields.module}" if fields.branch and fields.module else "",
        }
        sections = []
        for section in self.sections:
            value = values.get(section.key)
            if section.required and not value:
                raise ValueError(f"Provide the required field: {section.heading}.")
            if value:
                sections.append(f"### {section.heading}\n\n{value}")
        return f"{self.title_prefix} {title}", "\n\n".join(sections)


def load_bug_report_template(owner: str, repo: str, language: str) -> BugReportTemplate:
    """Read the Bug Report file, with the GitHub mirror available during outages."""
    locale = "en" if language == "en" else "zh"
    path = f".gitcode/ISSUE_TEMPLATE.{locale}/001-bug-report.yml"
    source: Literal["gitcode", "github"] = "gitcode"
    try:
        contents = GitCodeIssueClient(token="", timeout=8).get_repository_file(owner=owner, repo=repo, path=path)
    except httpx.HTTPError as exc:
        if (owner.casefold(), repo.casefold()) != ("openjiuwen", "jiuwenswarm"):
            raise exc
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            if status != 429 and status < 500:
                raise exc
        with httpx.Client(timeout=8, trust_env=False) as client:
            response = client.get(f"{_GITHUB_TEMPLATE_ROOT}/{path}")
            response.raise_for_status()
            contents = response.content.decode("utf-8-sig")
        source = "github"
    form = _Form.model_validate(yaml.safe_load(contents))
    sections: dict[str, _Section] = {}
    checklist: tuple[_ChecklistOption, ...] = ()
    branch_modules: dict[str, tuple[str, ...]] = {}
    for field in form.body:
        if field.type == "markdown":
            continue
        heading = field.attributes.get("label")
        if not isinstance(heading, str) or not heading.strip() or any(char in heading for char in ("\n", "\r")):
            raise ValueError("The Bug Report template has an invalid section heading.")
        if field.type == "textarea":
            key = _SECTION_KEYS.get(heading)
        elif field.type == "checkboxes" and field.id == "checklist":
            key = "checklist"
            checklist = tuple(_ChecklistOption.model_validate(item) for item in field.attributes.get("options", []))
            if not checklist or len({item.label for item in checklist}) != len(checklist):
                raise ValueError("The Bug Report template has invalid checklist options.")
        elif field.type == "cascading-dropdown" and field.id == "module_select":
            key = "module_select"
            options = [_BranchOption.model_validate(item) for item in field.attributes.get("options", [])]
            branch_modules = {item.value: item.children for item in options}
            if not options or len(branch_modules) != len(options):
                raise ValueError("The Bug Report template has invalid branch options.")
        else:
            key = None
        if key is None or key in sections:
            raise ValueError("The Bug Report template contains an unsupported field.")
        sections[key] = _Section(key, heading, field.validations.get("required", False))

    prefix = form.title.strip()
    if not prefix or not {"description", "environment", "checklist"}.issubset(sections):
        raise ValueError("The Bug Report template is missing expected fields.")
    template_id = hashlib.sha256(f"{path}\n{contents}".encode()).hexdigest()
    return BugReportTemplate(
        template_id, path, source, locale, prefix, form.labels, tuple(sections.values()), checklist, branch_modules,
    )
