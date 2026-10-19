"""SKILL.md parsing."""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Tuple

import yaml

from jiuwenswarm.symphony.fingerprint.models import (
    ExtractionDiagnostic,
    RawSkillManifest,
    SkillFolder,
)


class SkillManifestParser:
    """Parse a SkillFolder's SKILL.md into frontmatter and body."""

    def parse(self, folder: SkillFolder) -> RawSkillManifest:
        text = folder.entry.read_text(encoding="utf-8-sig")
        metadata, body, diagnostics = self._split_frontmatter(text, folder)
        body_sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()
        return RawSkillManifest(
            folder=folder,
            frontmatter=metadata,
            body=body,
            body_sha256=body_sha256,
            diagnostics=diagnostics,
        )

    def _split_frontmatter(
        self,
        text: str,
        folder: SkillFolder,
    ) -> Tuple[Dict[str, Any], str, List[ExtractionDiagnostic]]:
        diagnostics: List[ExtractionDiagnostic] = []

        try:
            metadata, body = self._load_frontmatter(text)
        except Exception as exc:
            diagnostics.append(
                ExtractionDiagnostic(
                    stage="manifest",
                    severity="warning",
                    code="invalid_frontmatter",
                    message="frontmatter could not be parsed",
                    path=str(folder.path),
                    details={"error": str(exc)},
                )
            )
            return {}, text, diagnostics

        if not isinstance(metadata, dict):
            diagnostics.append(
                ExtractionDiagnostic(
                    stage="manifest",
                    severity="warning",
                    code="invalid_frontmatter",
                    message="frontmatter must be a YAML mapping",
                    path=str(folder.path),
                )
            )
            return {}, text, diagnostics

        return metadata, body, diagnostics

    @staticmethod
    def _load_frontmatter(text: str) -> Tuple[Any, str]:
        lines = text.splitlines()
        if not lines or lines[0].strip() != "---":
            return {}, text

        end_index = None
        for index, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                end_index = index
                break

        if end_index is None:
            return {}, text

        frontmatter_text = "\n".join(lines[1:end_index])
        body = "\n".join(lines[end_index + 1:])
        metadata = yaml.safe_load(frontmatter_text)
        if metadata is None:
            metadata = {}
        if not isinstance(metadata, dict):
            raise ValueError("frontmatter content must be a YAML mapping")
        return metadata, body
