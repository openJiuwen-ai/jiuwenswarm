"""Conference template discovery and materialization.

Only a package whose manifest and declared official assets are present can be
selected.  This prevents a hand-written approximation from being labelled as a
conference-formatted paper.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path


TEMPLATES_ROOT = Path(__file__).resolve().parent.parent / "templates" / "conferences"
DEFAULT_CONFERENCE = "iclr2024"


@dataclass(frozen=True)
class TemplatePackage:
    conference: str
    root: Path
    manifest: dict

    @property
    def entry_template(self) -> Path:
        return self.root / self.manifest["entry_template"]


def available_conferences() -> list[str]:
    """Return installed and selectable conference package IDs."""
    return sorted(path.parent.name for path in TEMPLATES_ROOT.glob("*/manifest.json"))


def resolve_template(conference: str | None = None) -> TemplatePackage:
    """Load one complete package or raise a useful error before writing."""
    name = (conference or DEFAULT_CONFERENCE).strip().lower()
    root = TEMPLATES_ROOT / name
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        installed = ", ".join(available_conferences()) or "none"
        raise ValueError(f"conference template {name!r} is not installed (available: {installed})")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    missing = [asset for asset in manifest.get("required_assets", []) if not (root / asset).is_file()]
    if missing:
        raise ValueError(f"conference template {name!r} is incomplete: {missing}")
    return TemplatePackage(conference=name, root=root, manifest=manifest)


def render_document(template: TemplatePackage, *, title: str, body: str, camera_ready: bool = False) -> str:
    """Fill the workflow-owned shell without modifying official style assets."""
    source = template.entry_template.read_text(encoding="utf-8-sig")
    replacements = {
        "@@TITLE@@": title,
        "@@BODY@@": body,
        "@@FINAL_COPY@@": r"\iclrfinalcopy" if camera_ready else "",
    }
    for marker, value in replacements.items():
        if marker not in source:
            raise ValueError(f"template {template.conference!r} misses marker {marker}")
        source = source.replace(marker, value)
    return source


def materialize_assets(template: TemplatePackage, output_dir: Path) -> None:
    """Copy official style dependencies beside paper.tex for isolated builds."""
    output_dir.mkdir(parents=True, exist_ok=True)
    excluded = {template.manifest["entry_template"], "manifest.json", "iclr2024_conference.tex"}
    for source in template.root.iterdir():
        if source.is_file() and source.name not in excluded:
            shutil.copy2(source, output_dir / source.name)
