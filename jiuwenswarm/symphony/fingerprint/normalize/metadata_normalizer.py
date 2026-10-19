"""Metadata normalization for Skill identity: id, name, version, description."""

from __future__ import annotations

from typing import Optional

from jiuwenswarm.symphony.fingerprint.models import (
    ExtractedSkillSchema,
    MetadataNormalizationResult,
    NormalizationConfig,
    RawSkillManifest,
)
from jiuwenswarm.symphony.fingerprint.utils import normalize_human_name, normalize_slug


class MetadataNormalizer:
    """Normalize Skill metadata: id, name, version, description.

    This class handles the "metadata" dimension of the three-dimensional
    normalization process:
    - name dimension: I/O semantic names (handled by IONameNormalizer)
    - type dimension: data types (handled by DataTypeVocabulary)
    - metadata dimension: Skill identity fields (handled by this class)

    The metadata normalization derives Skill identity from:
    1. frontmatter.name → normalized slug for id
    2. folder.id_hint → fallback for id derivation
    3. folder.relative_path → final fallback for id
    """

    def __init__(self, config: Optional[NormalizationConfig] = None) -> None:
        self.config = config or NormalizationConfig()

    def normalize(
        self,
        manifest: RawSkillManifest,
        extracted: ExtractedSkillSchema,
    ) -> MetadataNormalizationResult:
        """Normalize Skill metadata from frontmatter and folder path.

        Args:
            manifest: Raw Skill manifest with frontmatter and folder info.
            extracted: LLM-extracted schema with description.

        Returns:
            MetadataNormalizationResult with normalized id, name, version, description.
        """
        frontmatter = manifest.frontmatter

        # Derive id from name → id_hint → relative_path
        raw_name = str(frontmatter.get("name") or manifest.folder.id_hint)
        skill_id = normalize_slug(raw_name) or normalize_slug(
            manifest.folder.relative_path
        )

        # Derive human-readable name
        name = normalize_human_name(raw_name) or skill_id

        # Derive version from frontmatter or config default
        version = str(
            frontmatter.get("version") or self.config.default_version
        )

        # Derive description from extracted schema or frontmatter
        description = str(
            extracted.description or frontmatter.get("description") or ""
        ).strip()

        return MetadataNormalizationResult(
            id=skill_id,
            name=name,
            description=description,
            version=version,
        )
