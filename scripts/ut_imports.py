"""Resolve direct and two-hop UT imports."""

import ast
from collections.abc import Iterator
from functools import cache
from pathlib import Path
import tokenize


UNIT_ROOT = 'tests/unit_tests'
SOURCE_ROOT = 'jiuwenswarm/'


def is_unit_test(path: str) -> bool:
    file = Path(path)
    return (
        path.startswith(f'{UNIT_ROOT}/')
        and file.suffix == '.py'
        and (file.name.startswith('test_') or file.stem.endswith('_test'))
        and file.is_file()
    )


def mirrored_tests(path: str) -> set[str]:
    if not path.startswith(SOURCE_ROOT) or not path.endswith('.py'):
        return set()
    source = Path(path.removeprefix(SOURCE_ROOT))
    names = (f'test_{source.name}', f'{source.stem}_test.py')
    candidates = (str(Path(UNIT_ROOT) / source.parent / name) for name in names)
    return {candidate for candidate in candidates if is_unit_test(candidate)}


def normalize_ut_paths(selected: set[str]) -> list[str]:
    root = Path(UNIT_ROOT).resolve()
    for path in sorted(selected):
        candidate = Path(path)
        unsafe_path = not path.startswith(f'{UNIT_ROOT}/') or any(ord(char) < 32 for char in path)
        if unsafe_path or not candidate.exists() or not candidate.resolve().is_relative_to(root):
            raise ValueError(f'invalid or missing UT path {path!r}')
    return sorted(path for path in selected if not any(str(parent) in selected for parent in Path(path).parents))


def worker_groups(rules: dict) -> dict[str, list[str]]:
    groups = rules.get('worker_groups', {})
    if not isinstance(groups, dict):
        raise ValueError('worker_groups must be a JSON object')
    seen = set()
    root = Path.cwd().as_posix()
    for name, files in groups.items():
        if not isinstance(files, list) or len(files) < 2:
            raise ValueError(f'worker group {name!r} needs at least two UT files')
        for path in files:
            if not is_unit_test(path) or path in seen:
                raise ValueError(f'invalid or repeated path in worker group {name!r}: {path!r}')
            if Path(path).resolve().as_posix() != f'{root}/{path}':
                raise ValueError(f'use a canonical repository path in worker group {name!r}: {path!r}')
            seen.add(path)
    return groups


def include_worker_peers(selected: set[str], groups: dict[str, list[str]]) -> None:
    for files in groups.values():
        if any(path in selected or any(str(parent) in selected for parent in Path(path).parents) for path in files):
            selected.update(files)


def module_name(path: Path) -> str:
    parts = path.with_suffix('').parts
    return '.'.join(parts[:-1] if parts[-1] == '__init__' else parts)


def imported_modules(path: Path) -> Iterator[str]:
    module = module_name(path)
    package = module.split('.') if path.name == '__init__.py' else module.split('.')[:-1]
    with tokenize.open(path) as source:
        tree = ast.parse(source.read(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level > len(package):
                continue
            parts = package[:len(package) - node.level + 1] if node.level else []
            if node.module:
                parts.append(node.module)
            base = '.'.join(parts)
            if base:
                yield base
                for alias in node.names:
                    if alias.name != '*':
                        yield f'{base}.{alias.name}'


def import_test_hits(
    changed: list[str], tracked: list[str],
) -> dict[str, tuple[set[str], set[str]]]:
    changed_modules = {
        module_name(Path(path)): path
        for path in changed if path.startswith(SOURCE_ROOT) and path.endswith('.py')
    }
    if not changed_modules:
        return {}

    files = {module_name(Path(path)): Path(path) for path in tracked if path.endswith('.py')}
    packages = {module for module, path in files.items() if path.name == '__init__.py'}

    @cache
    def dependencies(path: Path) -> set[str]:
        imports = set()
        for imported in imported_modules(path):
            if imported in files or imported in changed_modules:
                imports.add(imported)
            parent = imported.rpartition('.')[0]
            while parent:
                if parent in packages:
                    imports.add(parent)
                parent = parent.rpartition('.')[0]
        return imports

    hits = {path: (set(), set()) for path in changed_modules.values()}
    for path in files.values():
        test = path.as_posix()
        if not is_unit_test(test):
            continue
        first = dependencies(path)
        second = set().union(*(dependencies(files[name]) for name in first if name in files))
        for module in first & changed_modules.keys():
            hits[changed_modules[module]][0].add(test)
        for module in (second - first) & changed_modules.keys():
            hits[changed_modules[module]][1].add(test)
    return hits
