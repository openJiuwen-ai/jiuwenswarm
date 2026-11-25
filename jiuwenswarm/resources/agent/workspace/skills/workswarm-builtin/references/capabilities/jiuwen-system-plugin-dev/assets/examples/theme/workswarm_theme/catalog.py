"""Built-in and imported themes (DESIGN.md sections 2 and 5).

Built-in themes ship converted under the package ``assets/themes/<id>/``; imported themes are
converted into the data directory ``public/themes/<id>/<version>/``. Both are re-validated with
the V1 checker when the catalog loads, so a damaged copy is listed with its errors instead of shown.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .schema import ID_RE, MANIFEST_NAME, VERSION_RE, ThemePack, ThemeValidationError, load_theme_pack

THEMES_DIR = "themes"


@dataclass(frozen=True)
class Theme:
    """One theme the page can show."""

    pack: ThemePack
    source: str  # "builtin" or "imported"

    @property
    def id(self) -> str:
        return self.pack.id

    @property
    def asset_prefix(self) -> str:
        """Path of the theme directory for ``ops.ui.asset_url``."""
        if self.source == "builtin":
            return f"{THEMES_DIR}/{self.pack.id}"
        return f"{THEMES_DIR}/{self.pack.id}/{self.pack.version}"

    @property
    def content_stamp(self) -> str:
        """Changes whenever an imported theme is imported again, even under the same id and version.

        The host keeps a running background whose address is unchanged (host S_07), so URLs of
        imported themes carry this stamp; built-in themes change address with the package version.
        """
        if self.source == "builtin":
            return ""
        try:
            return format(self.pack.root.stat().st_mtime_ns, "x")
        except OSError:
            return ""


@dataclass
class Catalog:
    themes: dict[str, Theme] = field(default_factory=dict)
    failures: list[tuple[str, list[str]]] = field(default_factory=list)

    def get(self, theme_id: str | None) -> Theme | None:
        return self.themes.get(theme_id) if theme_id else None

    def ordered(self) -> list[Theme]:
        builtin = [theme for theme in self.themes.values() if theme.source == "builtin"]
        imported = sorted((theme for theme in self.themes.values() if theme.source == "imported"),
                          key=lambda theme: theme.pack.name)
        return builtin + imported


def _version_key(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def load_catalog(package_root: Path, data_root: Path | None, builtin_order: tuple[str, ...] = ()) -> Catalog:
    """Scan both locations. Imported themes cannot shadow a built-in id."""
    catalog = Catalog()
    builtin_dir = Path(package_root) / "assets" / THEMES_DIR
    names = sorted(path.name for path in builtin_dir.iterdir() if path.is_dir()) if builtin_dir.is_dir() else []
    names.sort(key=lambda name: builtin_order.index(name) if name in builtin_order else len(builtin_order))
    for name in names:
        try:
            pack = load_theme_pack(builtin_dir / name, source="builtin")
        except ThemeValidationError as exc:
            catalog.failures.append((name, exc.errors))
            continue
        catalog.themes[pack.id] = Theme(pack, "builtin")
    imported_dir = Path(data_root) / "public" / THEMES_DIR if data_root is not None else None
    if imported_dir is None or not imported_dir.is_dir():
        return catalog
    for theme_dir in sorted(path for path in imported_dir.iterdir() if path.is_dir() and ID_RE.match(path.name)):
        versions = [path for path in theme_dir.iterdir()
                    if path.is_dir() and VERSION_RE.match(path.name) and (path / MANIFEST_NAME).is_file()]
        if not versions:
            continue
        latest = max(versions, key=lambda path: _version_key(path.name))
        try:
            pack = load_theme_pack(latest, source="imported")
        except ThemeValidationError as exc:
            catalog.failures.append((theme_dir.name, exc.errors))
            continue
        if pack.id != theme_dir.name or pack.version != latest.name or pack.id in catalog.themes:
            catalog.failures.append((theme_dir.name, ["导入目录与主题清单的 ID 或版本不一致"]))
            continue
        catalog.themes[pack.id] = Theme(pack, "imported")
    return catalog
