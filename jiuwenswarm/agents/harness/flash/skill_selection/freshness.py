"""Cheap filesystem snapshots; no polling thread or persistent index cache."""

from pathlib import Path
from dataclasses import dataclass
import os
import stat

from .execution import checkpoint


def file_stamp(path: Path):
    """Include file identity to detect atomic replacements retaining mtime."""
    try:
        value = path.stat()
    except (FileNotFoundError, NotADirectoryError):
        return None
    return _stamp(value)


def _stamp(value):
    return (value.st_mode, value.st_size, value.st_mtime_ns, value.st_ctime_ns, value.st_ino)


def _is_link(value):
    # Junctions and other Windows reparse points also need containment checks.
    return (stat.S_ISLNK(value.st_mode)
            or bool(getattr(value, 'st_file_attributes', 0) & 0x400))


@dataclass(frozen=True)
class SourceStamp:
    target: str
    link: tuple | None
    file: tuple | None


def source_stamp(path: Path):
    """Track missing references and link retargeting as well as target contents."""
    checkpoint()
    path = Path(path)
    try:
        target = path.resolve()
        value = path.lstat()
        link = _stamp(value)
    except (FileNotFoundError, NotADirectoryError):
        return SourceStamp(str(path.resolve()), None, None)
    return SourceStamp(str(target), link, file_stamp(target) if _is_link(value) else link)


def iter_skill_files(roots):
    """Yield checked files without repeatedly resolving ordinary child paths."""
    for root in roots:
        checkpoint()
        root = Path(root).resolve()
        try:
            with os.scandir(root) as scan:
                # Match sorted(Path.iterdir()): Windows paths compare without
                # case, while POSIX paths retain case-sensitive ordering.
                directories = sorted(scan, key=lambda entry: os.path.normcase(entry.name))
        except (FileNotFoundError, NotADirectoryError):
            continue
        for directory in directories:
            checkpoint()
            if directory.name.startswith(('.', '_')):
                continue
            try:
                directory_stat = directory.stat(follow_symlinks=False)
                if _is_link(directory_stat):
                    parent = Path(directory.path).resolve()
                    if not parent.is_relative_to(root) or not parent.is_dir():
                        continue
                elif directory.is_dir(follow_symlinks=False):
                    parent = Path(directory.path)
                else:
                    continue
                path = parent / 'SKILL.md'
                value = path.lstat()
                resolved = path.resolve() if _is_link(value) else path
                if not resolved.is_relative_to(root):
                    continue
                if _is_link(value):
                    value = resolved.stat()
            except (FileNotFoundError, NotADirectoryError):
                continue
            if stat.S_ISREG(value.st_mode):
                yield root, Path(directory.path), parent, resolved, _stamp(value)


def catalog_signature(roots):
    """Scan only root SKILL.md entries, with the same containment policy as indexing."""
    entries = []
    for _, directory, _, resolved, value in iter_skill_files(roots):
        entries.append((str(directory / 'SKILL.md'), str(resolved), value))
        if len(entries) > 10000:
            raise RuntimeError('Local skill catalog exceeds the 10000 entry indexing limit')
    return tuple(entries)


def sources_unchanged(observed):
    for path, value in observed.items():
        checkpoint()
        current = source_stamp(path) if isinstance(value, SourceStamp) else file_stamp(path)
        if current != value:
            return False
    return True
