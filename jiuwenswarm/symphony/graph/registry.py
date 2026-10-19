"""Skill registry validation for graph construction."""

from __future__ import annotations

from typing import Iterable, List

from jiuwenswarm.symphony.graph.models import GraphDiagnostic, SkillRegistry
from jiuwenswarm.symphony.fingerprint.models import Fingerprint


class SkillRegistryBuilder:
    """Register normalized Skill fingerprints by stable ID."""

    @staticmethod
    def register(fingerprints: Iterable[Fingerprint]) -> SkillRegistry:
        skills = {}
        diagnostics: List[GraphDiagnostic] = []

        for fingerprint in sorted(fingerprints, key=lambda item: item.id):
            if fingerprint.type != "skill":
                diagnostics.append(
                    GraphDiagnostic(
                        stage="registry",
                        severity="error",
                        code="unsupported_fingerprint_type",
                        message=(
                            "Skill Graph only accepts fingerprints with "
                            "type='skill'."
                        ),
                        details={"type": fingerprint.type, "id": fingerprint.id},
                    )
                )
                continue

            if not fingerprint.id:
                diagnostics.append(
                    GraphDiagnostic(
                        stage="registry",
                        severity="error",
                        code="missing_skill_id",
                        message="Skill fingerprint is missing an id.",
                        details={"skill": fingerprint.to_dict()},
                    )
                )
                continue

            if fingerprint.id in skills:
                diagnostics.append(
                    GraphDiagnostic(
                        stage="registry",
                        severity="error",
                        code="duplicate_skill_id",
                        message=f"Duplicate Skill id: {fingerprint.id}",
                        skill_id=fingerprint.id,
                    )
                )
                continue

            if not fingerprint.outputs:
                diagnostics.append(
                    GraphDiagnostic(
                        stage="registry",
                        severity="warning",
                        code="missing_outputs",
                        message="Skill has no declared outputs.",
                        skill_id=fingerprint.id,
                    )
                )

            skills[fingerprint.id] = fingerprint

        return SkillRegistry(skills=skills, diagnostics=diagnostics)
