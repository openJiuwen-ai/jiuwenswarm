from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from shared.rich_compat import BarColumn, Console, Progress, SpinnerColumn, TaskProgressColumn, TextColumn
from shared.tags import normalize_tags


console = Console()


@dataclass
class ScannedItem:
    """Normalized scanned item used by tree/catalog builders."""

    id: str
    name: str
    description: str
    item_path: str
    content: str = ""
    github_url: str = ""
    stars: int = 0
    is_official: bool = False
    author: str = ""
    tags: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "skill_path": self.item_path,
            "path": self.item_path,
            "content": self.content,
            "github_url": self.github_url,
            "stars": self.stars,
            "is_official": self.is_official,
            "author": self.author,
            "tags": list(normalize_tags(self.tags)),
        }


class BaseScanner(ABC):
    """Common scanner contract for item-type-specific scanners."""

    item_type = "item"

    def __init__(self, items_dir: Path | str, *, display_items_dir: Path | str | None = None) -> None:
        self.items_dir = Path(items_dir)
        self.display_items_dir = Path(display_items_dir) if display_items_dir is not None else self.items_dir

    def scan(self, show_progress: bool = True) -> list[ScannedItem]:
        items: list[ScannedItem] = []
        if not self.items_dir.exists():
            console.print(f"[red]{self.item_type.title()}s directory not found: {self.items_dir}[/red]")
            return items

        subdirs = [path for path in sorted(self.items_dir.iterdir()) if path.is_dir() and not path.name.startswith(".")]
        if show_progress:
            items = self._scan_with_progress(subdirs)
        else:
            items = self._scan_simple(subdirs)
        items.sort(key=lambda item: item.name.lower())
        if show_progress:
            console.print(f"[green]Found {len(items)} {self.item_type}s in {self.display_items_dir}[/green]")
        return items

    def to_dict_list(self, items: list[ScannedItem] | None = None) -> list[dict[str, object]]:
        resolved_items = items if items is not None else self.scan()
        return [item.to_dict() for item in resolved_items]

    def _scan_with_progress(self, subdirs: list[Path]) -> list[ScannedItem]:
        items: list[ScannedItem] = []
        with Progress(
            SpinnerColumn(),
            TextColumn(f"[bold blue]Scanning {self.item_type} files..."),
            BarColumn(bar_width=40),
            TaskProgressColumn(),
            TextColumn("({task.completed}/{task.total})"),
            console=console,
        ) as progress:
            task = progress.add_task("Scanning", total=len(subdirs))
            for item_dir in subdirs:
                item = self.scan_item_dir(item_dir)
                if item is not None:
                    items.append(item)
                progress.update(task, advance=1)
        return items

    def _scan_simple(self, subdirs: list[Path]) -> list[ScannedItem]:
        items: list[ScannedItem] = []
        for item_dir in subdirs:
            item = self.scan_item_dir(item_dir)
            if item is not None:
                items.append(item)
        return items

    @classmethod
    @abstractmethod
    def detect_item_root(cls, path: Path) -> Path | None:
        """Return the canonical item root when this scanner recognizes the path."""

    @abstractmethod
    def scan_item_dir(self, item_dir: Path) -> ScannedItem | None:
        """Scan a single item directory into a normalized record."""
