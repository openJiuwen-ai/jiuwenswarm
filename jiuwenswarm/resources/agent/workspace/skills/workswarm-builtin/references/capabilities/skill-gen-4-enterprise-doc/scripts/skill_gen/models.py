"""Data models for SOP structure (skill-gen extraction)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, TypedDict


class SOPStepDict(TypedDict):
    """JSON-serializable shape for a single SOP step (``SOPStep.to_dict()``)."""

    step_number: int | str
    actor: str
    action: str
    system: str
    output: str
    notes: str


@dataclass
class SOPStep:
    """One procedural step extracted from an SOP document."""

    step_number: int | str
    actor: str = ""
    action: str = ""
    system: str = ""
    output: str = ""
    notes: str = ""

    def to_dict(self) -> SOPStepDict:
        return {
            "step_number": self.step_number,
            "actor": self.actor,
            "action": self.action,
            "system": self.system,
            "output": self.output,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], fallback_idx: int = 0) -> "SOPStep":
        raw_step_number = data.get("step_number", fallback_idx)
        if isinstance(raw_step_number, int):
            step_number: int | str = raw_step_number
        elif isinstance(raw_step_number, float) and raw_step_number.is_integer():
            step_number = int(raw_step_number)
        else:
            text = str(raw_step_number).strip()
            step_number = int(text) if text.isdigit() else (text or fallback_idx)
        return cls(
            step_number=step_number,
            actor=str(data.get("actor", "")),
            action=str(data.get("action", "")),
            system=str(data.get("system", "")),
            output=str(data.get("output", "")),
            notes=str(data.get("notes", "")),
        )


@dataclass
class SOPStructure:
    """Structured view of an SOP-style document for extraction and drafting.

    ``sop_type`` is one of ``procedural``, ``knowledge``, or ``hybrid`` (see
    ``sop_parser`` prompt definitions). ``steps`` holds ordered actions;
    ``knowledge_items`` holds rules, thresholds, and penalties that are not
    linear steps; ``sections`` mirrors the source outline.
    """

    title: str = ""
    purpose: str = ""
    scope: str = ""
    sop_type: str = ""
    roles: list[str] = field(default_factory=list)
    steps: list[SOPStep] = field(default_factory=list)
    knowledge_items: list[str] = field(default_factory=list)
    sections: list[dict[str, Any]] = field(default_factory=list)
    decision_points: list[str] = field(default_factory=list)
    exceptions: list[str] = field(default_factory=list)
    references: list[str] = field(default_factory=list)
    raw_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "purpose": self.purpose,
            "scope": self.scope,
            "sop_type": self.sop_type,
            "roles": list(self.roles),
            "steps": [step.to_dict() for step in self.steps],
            "knowledge_items": list(self.knowledge_items),
            "sections": list(self.sections),
            "decision_points": list(self.decision_points),
            "exceptions": list(self.exceptions),
            "references": list(self.references),
            "raw_text": self.raw_text,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SOPStructure":
        steps_raw = data.get("steps", [])
        steps = [
            SOPStep.from_dict(s, fallback_idx=i) if isinstance(s, dict) else SOPStep(step_number=i, action=str(s))
            for i, s in enumerate(steps_raw)
        ]
        sections_raw = data.get("sections", [])
        sections = [dict(s) for s in sections_raw if isinstance(s, dict)]
        return cls(
            title=str(data.get("title", "")),
            purpose=str(data.get("purpose", "")),
            scope=str(data.get("scope", "")),
            sop_type=str(data.get("sop_type", "")),
            roles=[str(r) for r in data.get("roles", [])],
            steps=steps,
            knowledge_items=[str(k) for k in data.get("knowledge_items", []) if str(k).strip()],
            sections=sections,
            decision_points=[str(d) for d in data.get("decision_points", [])],
            exceptions=[str(e) for e in data.get("exceptions", [])],
            references=[str(r) for r in data.get("references", [])],
            raw_text=str(data.get("raw_text", "")),
        )

    def step_summary(self, max_steps: int = 30) -> str:
        """Return a plain-text list of steps (for prompts)."""
        lines: list[str] = []
        for step in self.steps[:max_steps]:
            actor_part = f" [{step.actor}]" if step.actor else ""
            system_part = f" (系统: {step.system})" if step.system else ""
            lines.append(f"{step.step_number}. {step.action}{actor_part}{system_part}")
            if step.output:
                lines.append(f"   输出: {step.output}")
            if step.notes:
                lines.append(f"   备注: {step.notes}")
        return "\n".join(lines)

    def knowledge_summary(self, max_items: int = 30) -> str:
        """Return sections and knowledge items as plain text (for prompts)."""
        lines: list[str] = []
        if self.sections:
            lines.append("=== 章节结构 ===")
            for sec in self.sections[:20]:
                sid = sec.get("id", "")
                stitle = sec.get("title", "")
                summary = sec.get("content_summary", "")
                lines.append(f"[{sid}] {stitle}: {summary}")
        if self.knowledge_items:
            if lines:
                lines.append("")
            lines.append("=== 关键规则与标准 ===")
            for i, item in enumerate(self.knowledge_items[:max_items], 1):
                lines.append(f"K{i}. {item}")
        return "\n".join(lines)

    def full_summary(self, max_steps: int = 30, max_knowledge: int = 30) -> str:
        """Return ``sop_type``, step list, and knowledge block as plain text."""
        parts: list[str] = []
        if self.sop_type:
            parts.append(f"SOP 类型: {self.sop_type}")
        ss = self.step_summary(max_steps)
        if ss:
            parts.append(ss)
        ks = self.knowledge_summary(max_knowledge)
        if ks:
            parts.append(ks)
        return "\n\n".join(parts)
